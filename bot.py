"""
ИИ-помощник для Казахстана — всё в одном файле.
Telegram бот: Calendar + Kino.kz + голосовые
"""

import os, json, logging, asyncio, urllib.parse
from datetime import datetime
from aiohttp import web
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode
import anthropic

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
PORT = int(os.environ.get("PORT", "8080"))
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Almaty")

SYSTEM_PROMPT = f"""Ты — персональный ИИ-помощник в Telegram для жизни в Казахстане.

ВОЗМОЖНОСТИ:
1. Google Calendar — создавать, просматривать встречи
2. Кино — искать сеансы на Kino.kz, давать ссылку на покупку

ПРАВИЛА:
- Отвечай коротко, как живой помощник в мессенджере
- Если не указано время окончания встречи — ставь +1 час
- Если не указана дата — предположи сегодня/завтра
- Город по умолчанию — Алматы
- При поиске кино дай ссылку на покупку с Kino.kz
- Никогда не показывай JSON, ID, технические данные
- Используй эмодзи умеренно: 📅 🎬
- Ссылки оформляй как [текст](url)

Часовой пояс: {TIMEZONE} (UTC+6).
Формат дат в tools: ISO 8601 (YYYY-MM-DDTHH:MM:SS).
"""

# ── Clients ──────────────────────────────────────────────
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = {}

# ── Google Calendar ──────────────────────────────────────
calendar_service = None
google_flow = None
auth_owner_id = None  # Telegram user ID who initiated auth


def get_google_flow():
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    creds_data = json.loads(GOOGLE_CREDENTIALS_JSON)
    return Flow.from_client_config(
        creds_data,
        scopes=["https://www.googleapis.com/auth/calendar"],
        redirect_uri=f"{RENDER_URL}/google-callback",
    )


def init_calendar_from_token(token_json: str):
    global calendar_service
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    calendar_service = build("calendar", "v3", credentials=creds)
    logger.info("Google Calendar подключён!")


# Try loading saved token from env
GOOGLE_TOKEN_JSON = os.environ.get("GOOGLE_TOKEN_JSON", "")
if GOOGLE_TOKEN_JSON:
    try:
        init_calendar_from_token(GOOGLE_TOKEN_JSON)
    except Exception as e:
        logger.warning(f"Не удалось загрузить Calendar token: {e}")


async def create_event(summary, start_time, end_time, description="", location="", reminder_minutes=30):
    if not calendar_service:
        return {"error": "Google Calendar не подключён. Отправьте /auth для подключения."}
    body = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": TIMEZONE},
        "end": {"dateTime": end_time, "timeZone": TIMEZONE},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]},
    }
    if description: body["description"] = description
    if location: body["location"] = location
    try:
        event = calendar_service.events().insert(calendarId="primary", body=body).execute()
        return {"success": True, "summary": event["summary"], "start": event["start"]["dateTime"], "link": event.get("htmlLink", "")}
    except Exception as e:
        return {"error": str(e)}


async def list_events(date_from, date_to):
    if not calendar_service:
        return {"error": "Google Calendar не подключён. Отправьте /auth для подключения."}
    if "+" not in date_from and not date_from.endswith("Z"): date_from += "+06:00"
    if "+" not in date_to and not date_to.endswith("Z"): date_to += "+06:00"
    try:
        result = calendar_service.events().list(
            calendarId="primary", timeMin=date_from, timeMax=date_to,
            maxResults=20, singleEvents=True, orderBy="startTime",
        ).execute()
        events = [{"id": e["id"], "summary": e.get("summary", "—"),
                    "start": e["start"].get("dateTime", e["start"].get("date")),
                    "location": e.get("location", "")} for e in result.get("items", [])]
        return {"events": events, "count": len(events)}
    except Exception as e:
        return {"error": str(e)}


# ── Kino.kz ──────────────────────────────────────────────
CITY_SLUGS = {"алматы": "almaty", "астана": "astana", "шымкент": "shymkent", "караганда": "karaganda"}


async def search_movie_sessions(movie, city="Алматы", cinema="", date=""):
    city_slug = CITY_SLUGS.get(city.lower().strip(), "almaty")
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    kino_base = "https://kino.kz"

    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36"}) as client:
        try:
            # Search movie
            resp = await client.get(f"{kino_base}/{city_slug}/search", params={"query": movie})
            soup = BeautifulSoup(resp.text, "html.parser")

            movies = []
            for link in soup.select("a[href]"):
                href = link.get("href", "")
                text = link.get_text(strip=True)
                if ("/movie/" in href or "/film/" in href) and text and movie.lower() in text.lower():
                    movies.append({"title": text, "url": href})

            if not movies:
                return {"sessions": [], "message": f"Фильм '{movie}' не найден. Посмотрите на kino.kz/{city_slug}", "kino_url": f"{kino_base}/{city_slug}"}

            best = movies[0]
            url = f"{kino_base}{best['url']}" if not best["url"].startswith("http") else best["url"]
            resp = await client.get(url, params={"date": target_date})
            soup = BeautifulSoup(resp.text, "html.parser")

            sessions = []
            for block in soup.select(".session, .showtime, [data-session-id], .cinema-sessions"):
                cinema_el = block.select_one(".cinema-name, .theater-name, h3, h4")
                cinema_name = cinema_el.get_text(strip=True) if cinema_el else "—"
                for time_el in block.select(".time, .session-time, a[href*='session'], .showtime-item"):
                    time_text = time_el.get_text(strip=True)
                    link = time_el.get("href", "") or (time_el.parent.get("href", "") if time_el.parent else "")
                    buy_url = f"{kino_base}{link}" if link and not link.startswith("http") else link
                    sessions.append({"cinema": cinema_name, "time": time_text, "buy_url": buy_url})

            if cinema:
                sessions = [s for s in sessions if cinema.lower() in s.get("cinema", "").lower()]

            return {"movie": best["title"], "date": target_date, "sessions": sessions,
                    "count": len(sessions), "movie_url": f"{kino_base}{best['url']}"}
        except Exception as e:
            return {"error": str(e), "fallback_url": f"{kino_base}/{city_slug}",
                    "message": f"Не удалось загрузить Kino.kz. Прямая ссылка: {kino_base}/{city_slug}"}


# ── Voice transcription ──────────────────────────────────
async def transcribe_voice(file_id: str) -> str:
    file = await bot.get_file(file_id)
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file.file_path}"
    async with httpx.AsyncClient() as client:
        audio = (await client.get(file_url)).content

    if not OPENAI_API_KEY:
        return ""

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1", "language": "ru"},
        )
        return resp.json().get("text", "")


# ── Claude Tools ─────────────────────────────────────────
TOOLS = [
    {"name": "create_calendar_event", "description": "Создать событие в Google Calendar.",
     "input_schema": {"type": "object", "properties": {
         "summary": {"type": "string", "description": "Название"},
         "start_time": {"type": "string", "description": "Начало ISO 8601"},
         "end_time": {"type": "string", "description": "Конец ISO 8601"},
         "description": {"type": "string", "description": "Описание (опц.)"},
         "location": {"type": "string", "description": "Место (опц.)"},
         "reminder_minutes": {"type": "integer", "description": "Напомнить за N минут (30)"},
     }, "required": ["summary", "start_time", "end_time"]}},
    {"name": "list_calendar_events", "description": "Показать события из Calendar за период.",
     "input_schema": {"type": "object", "properties": {
         "date_from": {"type": "string", "description": "Начало ISO 8601"},
         "date_to": {"type": "string", "description": "Конец ISO 8601"},
     }, "required": ["date_from", "date_to"]}},
    {"name": "search_movie_sessions",
     "description": "Найти сеансы фильма на Kino.kz. Возвращает кинотеатры, время и ссылки на покупку.",
     "input_schema": {"type": "object", "properties": {
         "movie": {"type": "string", "description": "Название фильма"},
         "city": {"type": "string", "description": "Город (по умолч. Алматы)"},
         "cinema": {"type": "string", "description": "Кинотеатр (опц.)"},
         "date": {"type": "string", "description": "Дата YYYY-MM-DD (опц.)"},
     }, "required": ["movie"]}},
]


async def execute_tool(name, params):
    try:
        if name == "create_calendar_event": result = await create_event(**params)
        elif name == "list_calendar_events": result = await list_events(**params)
        elif name == "search_movie_sessions": result = await search_movie_sessions(**params)
        else: result = {"error": f"Unknown tool: {name}"}
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def ask_claude(user_id, user_message):
    if user_id not in conversations: conversations[user_id] = []
    conversations[user_id].append({"role": "user", "content": user_message})
    history = conversations[user_id][-20:]
    now = datetime.now().strftime("%Y-%m-%d %H:%M, %A")
    system = f"{SYSTEM_PROMPT}\n\nСейчас: {now}"

    while True:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514", max_tokens=2048,
            system=system, tools=TOOLS, messages=history,
        )
        content = response.content
        history.append({"role": "assistant", "content": content})

        if response.stop_reason == "tool_use":
            results = []
            for block in content:
                if block.type == "tool_use":
                    logger.info(f"Tool: {block.name}")
                    result = await execute_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
            history.append({"role": "user", "content": results})
        else:
            text = "\n".join(b.text for b in content if hasattr(b, "text"))
            conversations[user_id] = history
            return text


# ── Telegram Handlers ────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    cal_status = "подключён" if calendar_service else "не подключён (/auth)"
    voice_status = "включены" if OPENAI_API_KEY else "выключены"
    await msg.answer(
        f"Привет! Я твой ИИ-помощник.\n\n"
        f"📅 Google Calendar: {cal_status}\n"
        f"🎬 Kino.kz: работает\n"
        f"🎤 Голосовые: {voice_status}\n\n"
        f"Пиши или отправляй голосовые:\n"
        f"• «Запиши встречу с Маратом на пятницу в 15:00»\n"
        f"• «Что у меня на завтра?»\n"
        f"• «Хочу на Мстители в Chaplin MEGA»"
    )


@dp.message(Command("auth"))
async def cmd_auth(msg: types.Message):
    global google_flow, auth_owner_id
    if not GOOGLE_CREDENTIALS_JSON:
        await msg.answer("Google credentials не настроены. Добавьте GOOGLE_CREDENTIALS_JSON в переменные Render.")
        return
    google_flow = get_google_flow()
    auth_owner_id = msg.from_user.id
    auth_url, _ = google_flow.authorization_url(prompt="consent", access_type="offline")
    await msg.answer(
        f"Для подключения Google Calendar:\n\n"
        f"1. Откройте эту ссылку:\n{auth_url}\n\n"
        f"2. Войдите в Google и дайте доступ\n"
        f"3. Вас перенаправит обратно — Calendar подключится автоматически"
    )


@dp.message(F.voice | F.audio)
async def handle_voice(msg: types.Message):
    if not OPENAI_API_KEY:
        await msg.answer("Голосовые не настроены. Добавьте OPENAI_API_KEY или пишите текстом.")
        return
    await msg.chat.do("typing")
    try:
        fid = msg.voice.file_id if msg.voice else msg.audio.file_id
        text = await transcribe_voice(fid)
        if not text:
            await msg.answer("Не удалось распознать. Попробуйте ещё раз.")
            return
        await msg.answer(f"🎤 _{text}_", parse_mode=ParseMode.MARKDOWN)
        reply = await ask_claude(msg.from_user.id, text)
        for i in range(0, len(reply), 4000):
            await msg.answer(reply[i:i+4000], parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Voice error: {e}")
        await msg.answer("Ошибка. Попробуйте текстом.")


@dp.message(F.text)
async def handle_text(msg: types.Message):
    await msg.chat.do("typing")
    try:
        reply = await ask_claude(msg.from_user.id, msg.text)
        for i in range(0, len(reply), 4000):
            await msg.answer(reply[i:i+4000], parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error: {e}")
        await msg.answer("Ошибка. Попробуйте ещё раз.")


# ── Web server (OAuth callback + health check) ──────────
async def handle_health(request):
    return web.Response(text="OK")


async def handle_google_callback(request):
    global calendar_service
    code = request.query.get("code")
    if not code or not google_flow:
        return web.Response(text="Ошибка: нет кода авторизации", status=400)

    try:
        google_flow.fetch_token(code=code)
        creds = google_flow.credentials
        calendar_service = build("calendar", "v3", credentials=creds)

        # Notify user in Telegram
        if auth_owner_id:
            token_json = creds.to_json()
            await bot.send_message(auth_owner_id,
                "Google Calendar подключён!\n\n"
                "Чтобы Calendar работал после перезапуска сервера, "
                "добавьте эту переменную в Render → Environment:\n\n"
                f"`GOOGLE_TOKEN_JSON`\n\nСо значением:\n`{token_json}`"
            )

        return web.Response(text="<h1>Google Calendar подключён!</h1><p>Вернитесь в Telegram.</p>",
                          content_type="text/html")
    except Exception as e:
        logger.error(f"OAuth error: {e}")
        return web.Response(text=f"Ошибка: {e}", status=500)


# ── Main ─────────────────────────────────────────────────
async def main():
    # Start web server
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/google-callback", handle_google_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

    # Start Telegram bot
    logger.info("Bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
