import os
import re
import json
import base64
import logging
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone, time as dt_time
from dotenv import load_dotenv
import httpx
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

def load_allowed_ids() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids

ALLOWED_USER_IDS: set[int] = load_allowed_ids()

def get_system_prompt() -> str:
    now = datetime.now(tz=KYIV_TZ)
    date_str = now.strftime("%A, %d %B %Y, %H:%M")
    return f"""Ти Ава — особистий асистент Ані. Відповідай українською, коротко і з легкою іронією.

Зараз: {date_str} (часовий пояс Київ, UTC+3).

ФОРМАТУВАННЯ ВІДПОВІДЕЙ (обов'язково):
- Використовуй ЛИШЕ HTML теги: <b>жирний</b>, <i>курсив</i>, <code>код</code>
- Заголовки — просто текст ВЕЛИКИМИ ЛІТЕРАМИ або без форматування, БЕЗ #
- НЕ використовуй Markdown: ніяких **текст**, *текст*, # заголовків, `код`
- Символи < > & в звичайному тексті не використовуй (тільки в HTML тегах)

У тебе є доступ до Google Calendar через інструменти:
- get_today_events — події на сьогодні (повертає з ID)
- get_week_events — події на тиждень (повертає з ID)
- find_events — пошук подій за назвою і/або датою
- create_event — створити нову подію (повертає посилання на подію)
- delete_event — видалити подію за event_id
- update_event — змінити назву або час існуючої події за event_id
- add_attendees — додати учасників до вже існуючої події за event_id
- web_fetch — прочитати вміст веб-сторінки за URL

Правила роботи з Calendar:
1. При СТВОРЕННІ події — виклич create_event. Він поверне посилання. Відповідай: "Зустріч створена! Додай учасника вручну — ось посилання: [посилання]". Ніяких згадок про "технічні обмеження".
2. При ВИДАЛЕННІ, РЕДАГУВАННІ або додаванні до ІСНУЮЧОЇ події — спочатку виклич find_events щоб отримати event_id.
3. Якщо знайдено кілька схожих подій — уточни у користувача яку саме.
4. Якщо час не вказано явно при створенні — уточни.
5. Якщо інструмент повернув результат без слова "Помилка" — це успіх. Підтверджуй без власних застережень.
6. Якщо в повідомленні є URL — ОБОВ'ЯЗКОВО виклич web_fetch щоб прочитати вміст перед відповіддю.
"""

TOOLS = [
    {
        "name": "get_today_events",
        "description": "Отримати всі події на сьогодні з їх ID",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_week_events",
        "description": "Отримати всі події на поточний тиждень (7 днів) з їх ID",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_events",
        "description": "Знайти події за назвою і/або датою. Використовуй перед delete_event, update_event, add_attendees щоб отримати event_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Пошуковий запит (частина назви події)"},
                "date_from": {"type": "string", "description": "Початок діапазону пошуку ISO 8601 (напр. 2025-05-28T00:00:00). Якщо не вказано — шукає від сьогодні."},
                "date_to": {"type": "string", "description": "Кінець діапазону пошуку ISO 8601. Якщо не вказано — шукає 30 днів вперед."},
            },
        },
    },
    {
        "name": "create_event",
        "description": "Створити нову подію в Google Calendar. Повертає посилання на подію.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Назва події"},
                "start_time": {"type": "string", "description": "Початок ISO 8601 (напр. 2025-05-28T14:00:00)"},
                "end_time": {"type": "string", "description": "Кінець ISO 8601"},
                "description": {"type": "string", "description": "Опис (опційно)"},
            },
            "required": ["title", "start_time", "end_time"],
        },
    },
    {
        "name": "delete_event",
        "description": "Видалити подію з Google Calendar за її ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID події (отримай через find_events)"},
                "title": {"type": "string", "description": "Назва події для підтвердження у відповіді"},
            },
            "required": ["event_id", "title"],
        },
    },
    {
        "name": "update_event",
        "description": "Змінити назву або час існуючої події",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID події (отримай через find_events)"},
                "title": {"type": "string", "description": "Нова назва (опційно, якщо не змінюється — не передавай)"},
                "start_time": {"type": "string", "description": "Новий початок ISO 8601 (опційно)"},
                "end_time": {"type": "string", "description": "Новий кінець ISO 8601 (опційно)"},
                "description": {"type": "string", "description": "Новий опис (опційно)"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "add_attendees",
        "description": "Додати учасників на існуючу подію",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "ID події (отримай через find_events)"},
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Список email-адрес учасників яких треба додати",
                },
            },
            "required": ["event_id", "emails"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Прочитати вміст веб-сторінки за URL. Використовуй автоматично коли в повідомленні є посилання.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL сторінки для читання"},
            },
            "required": ["url"],
        },
    },
]

anthropic_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

conversation_history: dict[int, list[dict]] = {}

# Дедуплікація: зберігаємо останні 200 message_id щоб не відповідати двічі
# (Telegram ретраїть доставку якщо бот не відповів за ~30с)
_processed_ids: deque = deque(maxlen=200)

KYIV_TZ = timezone(timedelta(hours=3))


def serialize_content(content) -> list:
    """Перетворює Pydantic content-блоки на прості dict для збереження в conversation_history."""
    result = []
    for block in content:
        if hasattr(block, "model_dump"):
            result.append(block.model_dump())
        elif isinstance(block, dict):
            result.append(block)
        else:
            result.append({"type": "text", "text": str(block)})
    return result


def normalize_dt(dt_str: str) -> str:
    """Нормалізує datetime-рядок для Google Calendar API.
    Прибирає timezone offset (+HH:MM або Z), бо timeZone задається окремо.
    Python 3.9 fromisoformat не підтримує +HH:MM.
    """
    if not dt_str:
        return dt_str
    # Прибираємо Z
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1]
    # Прибираємо +HH:MM або -HH:MM в кінці
    for i in range(len(dt_str) - 1, max(len(dt_str) - 7, 9), -1):
        if dt_str[i] in ("+", "-"):
            dt_str = dt_str[:i]
            break
    return dt_str


# --- Google Calendar ---

def get_calendar_service():
    # Пріоритет 1: base64-encoded JSON (Railway)
    creds_b64 = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if creds_b64:
        info = json.loads(base64.b64decode(creds_b64).decode("utf-8"))
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    # Пріоритет 2: сирий JSON рядок (старий формат, для сумісності)
    elif os.getenv("GOOGLE_CREDENTIALS_JSON"):
        info = json.loads(os.getenv("GOOGLE_CREDENTIALS_JSON"))
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/calendar"]
        )
    # Пріоритет 3: файл (локальна розробка)
    else:
        credentials = service_account.Credentials.from_service_account_file(
            os.path.join(os.path.dirname(__file__), "credentials.json"),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
    return build("calendar", "v3", credentials=credentials)


def format_events_with_ids(items: list) -> str:
    """Форматує список подій з ID для Claude."""
    lines = []
    for e in items:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in start:
            dt = datetime.fromisoformat(start)
            time_str = dt.strftime("%d.%m %H:%M")
        else:
            time_str = start
        attendees = e.get("attendees", [])
        attendee_str = ""
        if attendees:
            emails = [a["email"] for a in attendees if not a.get("self")]
            if emails:
                attendee_str = f" (учасники: {', '.join(emails)})"
        lines.append(f"• {time_str} — {e.get('summary', 'Без назви')}{attendee_str} [id:{e['id']}]")
    return "\n".join(lines)


def get_events(time_min: datetime, time_max: datetime) -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        service = get_calendar_service()
        result = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        items = result.get("items", [])
        if not items:
            return "Подій немає."
        return format_events_with_ids(items)
    except Exception as e:
        logger.error("get_events error: %s", e)
        return f"Помилка: {e}"


def find_events(query: str = "", date_from: str = "", date_to: str = "") -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        now = datetime.now(tz=KYIV_TZ)
        time_min = datetime.fromisoformat(date_from) if date_from else now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = datetime.fromisoformat(date_to) if date_to else time_min + timedelta(days=30)

        service = get_calendar_service()
        params = dict(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        if query:
            params["q"] = query

        result = service.events().list(**params).execute()
        items = result.get("items", [])
        if not items:
            return f"Подій за запитом '{query}' не знайдено."
        return format_events_with_ids(items)
    except Exception as e:
        logger.error("find_events error: %s", e)
        return f"Помилка пошуку: {e}"


def create_calendar_event(title: str, start_time: str, end_time: str,
                          description: str = "") -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        start_norm = normalize_dt(start_time)
        end_norm = normalize_dt(end_time)
        logger.info(
            "create_event: calendarId=%r title=%r start=%r end=%r",
            calendar_id, title, start_norm, end_norm,
        )

        service = get_calendar_service()
        event = {
            "summary": title,
            "start": {"dateTime": start_norm, "timeZone": "Europe/Kiev"},
            "end": {"dateTime": end_norm, "timeZone": "Europe/Kiev"},
        }
        if description:
            event["description"] = description

        created = service.events().insert(calendarId=calendar_id, body=event).execute()
        html_link = created.get("htmlLink", "")
        organizer = created.get("organizer", {}).get("email", "?")
        logger.info(
            "create_event: SUCCESS id=%s organizer=%s calendarId=%r link=%s",
            created.get("id"), organizer, calendar_id, html_link,
        )
        dt = datetime.fromisoformat(start_norm)
        return f"СТВОРЕНО: '{title}' на {dt.strftime('%d.%m.%Y о %H:%M')}. ПОСИЛАННЯ: {html_link}"
    except Exception as e:
        logger.error("create_event error: %s\n%s", e, traceback.format_exc())
        return f"Помилка створення: {e}"


def delete_calendar_event(event_id: str, title: str = "") -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        service = get_calendar_service()
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return f"Подію '{title}' видалено." if title else "Подію видалено."
    except Exception as e:
        logger.error("delete_event error: %s", e)
        return f"Помилка видалення: {e}"


def update_calendar_event(event_id: str, title: str = "", start_time: str = "",
                          end_time: str = "", description: str = "") -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        changes = []
        if title:
            event["summary"] = title
            changes.append(f"назва → '{title}'")
        if start_time:
            start_norm = normalize_dt(start_time)
            event["start"] = {"dateTime": start_norm, "timeZone": "Europe/Kiev"}
            dt = datetime.fromisoformat(start_norm)
            changes.append(f"початок → {dt.strftime('%d.%m %H:%M')}")
        if end_time:
            end_norm = normalize_dt(end_time)
            event["end"] = {"dateTime": end_norm, "timeZone": "Europe/Kiev"}
            dt = datetime.fromisoformat(end_norm)
            changes.append(f"кінець → {dt.strftime('%d.%m %H:%M')}")
        if description:
            event["description"] = description
            changes.append("опис оновлено")

        service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        summary = event.get("summary", "Подія")
        return f"'{summary}' оновлено: {', '.join(changes)}." if changes else f"'{summary}' — нічого не змінилось."
    except Exception as e:
        logger.error("update_event error: %s", e)
        return f"Помилка оновлення: {e}"


def add_attendees_to_event(event_id: str, emails: list) -> str:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not calendar_id:
        return "GOOGLE_CALENDAR_ID не налаштовано"
    try:
        service = get_calendar_service()
        event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

        existing = {a["email"] for a in event.get("attendees", [])}
        new_attendees = [e for e in emails if e not in existing]

        if not new_attendees:
            return f"Всі учасники вже додані на '{event.get('summary', 'подію')}'."

        event.setdefault("attendees", []).extend([{"email": e} for e in new_attendees])
        service.events().update(calendarId=calendar_id, eventId=event_id, body=event).execute()
        return f"На '{event.get('summary', 'подію')}' додано: {', '.join(new_attendees)}."
    except Exception as e:
        logger.error("add_attendees error: %s", e)
        return f"Помилка додавання учасників: {e}"


def fetch_url(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AvaBot/1.0)"}
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        text = resp.text
        if "html" in content_type:
            # Прибираємо script/style блоки
            text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', text, flags=re.DOTALL | re.IGNORECASE)
            # Замінюємо block-теги на переноси рядків
            text = re.sub(r'<(?:br|p|div|h[1-6]|li|tr)[^>]*>', '\n', text, flags=re.IGNORECASE)
            # Прибираємо всі інші теги
            text = re.sub(r'<[^>]+>', '', text)
            # Розкодовуємо HTML entities
            text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ').replace('&quot;', '"')
            # Нормалізуємо пробіли
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r'[ \t]+', ' ', text)
            text = text.strip()
        return text[:6000] if text else "Сторінка порожня."
    except httpx.HTTPStatusError as e:
        return f"Не вдалось прочитати сторінку: HTTP {e.response.status_code}"
    except httpx.TimeoutException:
        return "Не вдалось прочитати сторінку: timeout (сайт не відповів за 15с)"
    except Exception as e:
        logger.error("fetch_url error: %s", e)
        return f"Не вдалось прочитати сторінку: {e}"


def execute_tool(tool_name: str, tool_input: dict) -> str:
    now = datetime.now(tz=KYIV_TZ)

    if tool_name == "get_today_events":
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return get_events(day_start, day_start + timedelta(days=1))

    if tool_name == "get_week_events":
        week_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return get_events(week_start, week_start + timedelta(days=7))

    if tool_name == "find_events":
        return find_events(
            query=tool_input.get("query", ""),
            date_from=tool_input.get("date_from", ""),
            date_to=tool_input.get("date_to", ""),
        )

    if tool_name == "create_event":
        return create_calendar_event(
            title=tool_input["title"],
            start_time=tool_input["start_time"],
            end_time=tool_input["end_time"],
            description=tool_input.get("description", ""),
        )

    if tool_name == "delete_event":
        return delete_calendar_event(
            event_id=tool_input["event_id"],
            title=tool_input.get("title", ""),
        )

    if tool_name == "update_event":
        return update_calendar_event(
            event_id=tool_input["event_id"],
            title=tool_input.get("title", ""),
            start_time=tool_input.get("start_time", ""),
            end_time=tool_input.get("end_time", ""),
            description=tool_input.get("description", ""),
        )

    if tool_name == "add_attendees":
        return add_attendees_to_event(
            event_id=tool_input["event_id"],
            emails=tool_input["emails"],
        )

    if tool_name == "web_fetch":
        return fetch_url(tool_input["url"])

    return f"Невідомий інструмент: {tool_name}"


# --- Telegram handlers ---

def is_allowed(update: Update) -> bool:
    user_id = update.effective_user.id
    if not ALLOWED_USER_IDS:
        return True  # якщо список порожній — не блокуємо (зручно при першому налаштуванні)
    allowed = user_id in ALLOWED_USER_IDS
    if not allowed:
        logger.warning("Blocked user_id=%s username=%s", user_id, update.effective_user.username)
    return allowed


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Твій Telegram ID: `{user.id}`", parse_mode="Markdown")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    conversation_history[chat_id] = []
    await update.message.reply_text(
        "Привіт! Я Ава, твій особистий асистент.\n\n"
        "Можу з календарем:\n"
        "• показати події на сьогодні або тиждень\n"
        "• створити зустріч\n"
        "• видалити або перенести подію\n"
        "• додати учасників\n\n"
        "Просто пиши що треба — розберемось."
    )


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    conversation_history[chat_id] = []
    await update.message.reply_text("Контекст очищено.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    msg_id = update.message.message_id
    if msg_id in _processed_ids:
        logger.warning("Duplicate message_id=%s — skipping", msg_id)
        return
    _processed_ids.append(msg_id)

    chat_id = update.effective_chat.id
    user_text = update.message.text

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({"role": "user", "content": user_text})

    try:
        messages = conversation_history[chat_id]

        while True:
            response = await anthropic_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=get_system_prompt(),
                tools=TOOLS,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                assistant_text = next(
                    (b.text for b in response.content if b.type == "text"), ""
                )
                conversation_history[chat_id].append(
                    {"role": "assistant", "content": serialize_content(response.content)}
                )
                try:
                    await update.message.reply_text(assistant_text, parse_mode="HTML")
                except Exception:
                    await update.message.reply_text(assistant_text)
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": serialize_content(response.content)})

                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Tool: %s | input: %s", block.name, block.input)
                        result = execute_tool(block.name, block.input)
                        logger.info("Tool result: %s", result)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                messages.append({"role": "user", "content": tool_results})
                continue

            break

    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        await update.message.reply_text("Щось пішло не так з API. Спробуй ще раз.")


BRIEFING_CHAT_ID = 8563840820


async def morning_briefing(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(tz=KYIV_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events_raw = get_events(day_start, day_start + timedelta(days=1))

    if events_raw == "Подій немає.":
        body = "День вільний, насолоджуйся 😌"
    else:
        # Прибираємо [id:...] теги — у брифінгу вони зайві
        body = re.sub(r"\s*\[id:[^\]]+\]", "", events_raw)

    text = f"Доброго ранку! Ось твій план на сьогодні:\n\n{body}"
    try:
        await context.bot.send_message(chat_id=BRIEFING_CHAT_ID, text=text)
        logger.info("Morning briefing sent to %s", BRIEFING_CHAT_ID)
    except Exception as e:
        logger.error("Morning briefing failed: %s", e)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN не знайдено в .env")

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(
        morning_briefing,
        time=dt_time(9, 0, 0, tzinfo=KYIV_TZ),
        name="morning_briefing",
    )
    logger.info("Morning briefing scheduled at 09:00 Kyiv time")

    logger.info("Ava bot запущено...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
