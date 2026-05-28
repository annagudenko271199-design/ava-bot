# Ava Bot

Особистий Telegram-асистент з інтеграцією Google Calendar на базі Claude AI.

## Можливості

- Відповідає на повідомлення через Claude (claude-sonnet-4-5)
- Пам'ятає контекст розмови
- Показує події з Google Calendar на сьогодні і тиждень
- Створює нові події за запитом природньою мовою
- Команди: `/start`, `/clear`

## Деплой на Railway

### 1. Змінні середовища

У панелі Railway → Variables додай:

| Змінна | Опис |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота від @BotFather |
| `ANTHROPIC_API_KEY` | API ключ з console.anthropic.com |
| `GOOGLE_CALENDAR_ID` | Email або ID календаря (напр. `user@gmail.com`) |
| `GOOGLE_CREDENTIALS_JSON` | Вміст credentials.json одним рядком (див. нижче) |

### 2. Як отримати GOOGLE_CREDENTIALS_JSON

Скопіюй вміст файлу `credentials.json` і встав як значення змінної.
В терміналі:

```bash
cat credentials.json | tr -d '\n'
```

Або просто відкрий файл і скопіюй весь JSON — Railway зберігає його коректно.

### 3. Кроки деплою

1. Завантаж код на GitHub (без `.env` і `credentials.json` — вони в `.gitignore`)
2. Зайди на [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
3. Обери репозиторій `ava-bot`
4. Додай змінні середовища (таблиця вище)
5. Railway автоматично підхопить `Procfile` і запустить `worker: python bot.py`

### 4. Важливо

- Сервіс-акаунт Google повинен мати доступ до календаря:
  відкрий Google Calendar → Налаштування календаря → Поділитися з людьми →
  додай `client_email` зі service account з правами "Вносити зміни в події"
- Railway автоматично перезапускає бота при падінні

## Локальний запуск

```bash
pip install -r requirements.txt
# Створи .env з усіма змінними (credentials.json має лежати поруч)
python bot.py
```
