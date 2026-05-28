import os
import json
import logging
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ти Ава — особистий асистент Ані. Відповідай українською, коротко і з легкою іронією.

У тебе є доступ до Google Calendar через інструменти:
- get_today_events — отримати події на сьогодні
- get_week_events — отримати події на поточний тиждень
- create_event — створити нову подію

Використовуй ці інструменти коли:
- питають "що у мене сьогодні", "які зустрічі сьогодні", "що заплановано"
- питають "що на тижні", "зустрічі на тиждень", "що заплановано на тиждень"
- просять "додай зустріч", "запиши зустріч", "створи подію", "заплануй"

Для create_event потрібні: title (назва), start_time (ISO 8601, напр. 2025-05-28T14:00:00),
end_time (ISO 8601), опційно description.
Якщо час не вказано явно — уточни у користувача.
"""

TOOLS = [
    {
        "name": "get_today_events",
        "description": "Отримати всі події з Google Calendar на сьогодні",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_week_events",
        "description": "Отримати всі події з Google Calendar на поточний тиждень (7 днів)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_event",
        "description": "Створити нову подію в Google Calendar",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Назва події"},
                "start_time": {"type": "string", "description": "Початок у форматі ISO 8601 (напр. 2025-05-28T14:00:00)"},
                "end_time": {"type": "string", "description": "Кінець у форматі ISO 8601"},
                "description": {"type": "string", "description": "Опис події (опційно)"},
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
]

anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

conversation_history: dict[int, list[dict]] = {}


# --- Google Calendar ---

def get_calendar_service():
    # Railway: credentials передаються як JSON-рядок в GOOGLE_CREDENTIALS_JSON
    # Локально: читаємо з файлу credentials.json
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    else:
        credentials = service_account.Credentials.from_service_account_file(
            os.path.join(os.path.dirname(__file__), "credentials.json"),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
    return build("calendar", "v3", credentials=credentials)


def get_events(time_min: datetime, time_max: datetime) -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано в .env"

    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = result.get("items", [])
        if not events:
            return "Подій немає."

        lines = []
        for e in events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            if "T" in start:
                dt = datetime.fromisoformat(start)
                time_str = dt.strftime("%d.%m %H:%M")
            else:
                time_str = start
            lines.append(f"• {time_str} — {e.get('summary', 'Без назви')}")

        return "\n".join(lines)

    except Exception as e:
        logger.error("Calendar get_events error: %s", e)
        return f"Помилка отримання подій: {e}"


def create_calendar_event(title: str, start_time: str, end_time: str, description: str = "") -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано в .env"

    try:
        service = get_calendar_service()
        event = {
            "summary": title,
            "start": {"dateTime": start_time, "timeZone": "Europe/Kyiv"},
            "end": {"dateTime": end_time, "timeZone": "Europe/Kyiv"},
        }
        if description:
            event["description"] = description

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        dt = datetime.fromisoformat(start_time)
        return f"Подію '{title}' створено на {dt.strftime('%d.%m.%Y о %H:%M')}."

    except Exception as e:
        logger.error("Calendar create_event error: %s", e)
        return f"Помилка створення події: {e}"


def execute_tool(tool_name: str, tool_input: dict) -> str:
    now = datetime.now(tz=timezone(timedelta(hours=3)))  # Kyiv UTC+3

    if tool_name == "get_today_events":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return get_events(day_start, day_end)

    if tool_name == "get_week_events":
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_end = week_start + timedelta(days=7)
        return get_events(week_start, week_end)

    if tool_name == "create_event":
        return create_calendar_event(
            title=tool_input["title"],
            start_time=tool_input["start_time"],
            end_time=tool_input["end_time"],
            description=tool_input.get("description", ""),
        )

    return f"Невідомий інструмент: {tool_name}"


# --- Telegram handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conversation_history[chat_id] = []
    await update.message.reply_text("Привіт! Я Ава, твій особистий асистент. Можу показати календар або додати зустріч — просто питай.")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    conversation_history[chat_id] = []
    await update.message.reply_text("Контекст очищено.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_text})

    try:
        messages = conversation_history[chat_id]

        # Agentic loop: Claude може викликати інструменти кілька разів
        while True:
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            # Якщо Claude закінчив — повертаємо відповідь
            if response.stop_reason == "end_turn":
                assistant_text = next(
                    (b.text for b in response.content if b.type == "text"), ""
                )
                conversation_history[chat_id].append(
                    {"role": "assistant", "content": response.content}
                )
                await update.message.reply_text(assistant_text)
                break

            # Claude хоче викликати інструмент
            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Tool call: %s %s", block.name, block.input)
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            # Інша причина зупинки
            break

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        await update.message.reply_text("Щось пішло не так з API. Спробуй ще раз.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не знайдено в .env")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Ava bot запущено...")
    app.run_polling()


if __name__ == "__main__":
    main()
