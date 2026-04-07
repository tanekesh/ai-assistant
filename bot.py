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
2. Кино — искать сеансы конкретного фильма на Kino.kz, показывать все фильмы в прокате, давать ссылку на покупку

ПРАВИЛА:
- Отвечай коротко, как живой помощник в мессенджере
- Если не указано время окончания встречи — ставь +1 час
- Если не указана дата — предположи сегодня/завтра
- Город по умолчанию — Алматы
- При поиске кино: если пользователь называет конкретный фильм — используй search_movie_sessions, если спрашивает что идёт в кино — используй list_cinema_movies
- Если поиск на Kino.kz не нашёл фильм — дай прямую ссылку на поиск
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
import re
CITY_SLUGS = {"алматы": "almaty", "астана": "astana", "шымкент": "shymkent", "караганда": "karaganda"}
KINO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def extract_next_data(html: str) -> dict | None:
    """Extract __NEXT_DATA__ JSON from Next.js page."""
    match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except:
            pass
    return None


def deep_search_keys(obj, target_keys, depth=0, max_depth=6):
    """Recursively search for keys in nested dict/list structure."""
    results = {}
    if depth > max_depth:
        return results
    if isinstance(obj, dict):
        for key, val in obj.items():
            if key in target_keys:
                results[key] = val
            child = deep_search_keys(val, target_keys, depth + 1, max_depth)
            results.update(child)
    elif isinstance(obj, list):
        for item in obj[:20]:
            child = deep_search_keys(item, target_keys, depth + 1, max_depth)
            results.update(child)
    return results


async def fetch_kino_page(client, url):
    """Fetch a kino.kz page and log details."""
    resp = await client.get(url)
    logger.info(f"Kino fetch {url} -> status={resp.status_code}, length={len(resp.text)}")
    
    next_data = extract_next_data(resp.text)
    if next_data:
        # Log structure for debugging
        props = next_data.get("props", {})
        page_props = props.get("pageProps", {})
        logger.info(f"__NEXT_DATA__ found! pageProps keys: {list(page_props.keys())[:20]}")
        
        # Log deeper structure
        for key, val in page_props.items():
            if isinstance(val, list):
                logger.info(f"  pageProps['{key}'] = list of {len(val)} items")
                if val and isinstance(val[0], dict):
                    logger.info(f"    first item keys: {list(val[0].keys())[:15]}")
            elif isinstance(val, dict):
                logger.info(f"  pageProps['{key}'] = dict with keys: {list(val.keys())[:15]}")
            else:
                logger.info(f"  pageProps['{key}'] = {type(val).__name__}: {str(val)[:100]}")
        
        return next_data, page_props
    
    # No __NEXT_DATA__, log what scripts are on page
    script_srcs = re.findall(r'<script[^>]*src="([^"]*)"', resp.text)
    logger.info(f"No __NEXT_DATA__. Scripts on page: {len(script_srcs)}")
    for src in script_srcs[:5]:
        logger.info(f"  script: {src}")
    
    # Log any inline JSON data
    json_matches = re.findall(r'(?:window\.__\w+__|self\.__next)\s*=\s*(\{.{20,500})', resp.text)
    for jm in json_matches[:3]:
        logger.info(f"  inline JSON found: {jm[:200]}")
    
    return None, {}


async def search_movie_sessions(movie, city="Алматы", cinema="", date=""):
    city_slug = CITY_SLUGS.get(city.lower().strip(), "almaty")
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    kino_base = "https://kino.kz"
    movie_lower = movie.lower().strip()

    async with httpx.AsyncClient(timeout=25, headers=KINO_HEADERS, follow_redirects=True) as client:
        try:
            # Step 1: Fetch main city page to get movie listings
            main_urls = [
                f"{kino_base}/ru/{city_slug}",
                f"{kino_base}/{city_slug}",
                f"{kino_base}/ru/{city_slug}/movies",
                f"{kino_base}/{city_slug}/movies",
            ]
            
            all_movies = []
            page_props = {}
            
            for url in main_urls:
                next_data, pp = await fetch_kino_page(client, url)
                if pp:
                    page_props = pp
                    # Search for movie lists in all values
                    movie_keys = ["movies", "films", "items", "data", "nowShowing", 
                                  "releases", "results", "allMovies", "moviesList",
                                  "coming", "showing", "premieres"]
                    
                    for key in movie_keys:
                        if key in pp and isinstance(pp[key], list):
                            for item in pp[key]:
                                if isinstance(item, dict):
                                    title = (item.get("title") or item.get("name") or 
                                            item.get("nameRu") or item.get("originalTitle") or "")
                                    slug = (item.get("slug") or item.get("id") or 
                                           item.get("movieId") or "")
                                    if title:
                                        all_movies.append({
                                            "title": title, 
                                            "slug": str(slug),
                                            "data": item
                                        })
                    
                    # Also search deeper in nested structures
                    found = deep_search_keys(pp, set(movie_keys))
                    for key, val in found.items():
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, dict):
                                    title = (item.get("title") or item.get("name") or 
                                            item.get("nameRu") or "")
                                    slug = (item.get("slug") or item.get("id") or "")
                                    if title and not any(m["title"] == title for m in all_movies):
                                        all_movies.append({"title": title, "slug": str(slug), "data": item})
                    
                    if all_movies:
                        break
            
            logger.info(f"Total movies found on kino.kz: {len(all_movies)}")
            for m in all_movies[:10]:
                logger.info(f"  Movie: '{m['title']}', slug='{m['slug']}'")
            
            # Step 2: Find matching movie
            matches = []
            for m in all_movies:
                if movie_lower in m["title"].lower():
                    matches.append(m)
            
            # Fuzzy match if exact not found
            if not matches:
                for m in all_movies:
                    title_words = set(m["title"].lower().split())
                    search_words = set(movie_lower.split())
                    if search_words & title_words:
                        matches.append(m)
            
            if not matches:
                movie_list = ", ".join(m["title"] for m in all_movies[:10])
                return {
                    "sessions": [],
                    "message": f"Фильм '{movie}' не найден.",
                    "available_movies": movie_list if all_movies else "Не удалось загрузить список фильмов",
                    "kino_url": f"{kino_base}/ru/{city_slug}",
                }
            
            best = matches[0]
            logger.info(f"Best match: '{best['title']}', slug='{best['slug']}'")
            
            # Step 3: Try to get sessions
            movie_url = f"{kino_base}/ru/{city_slug}/movie/{best['slug']}"
            sessions = []
            
            # Check if session data is already in the movie data
            movie_data = best.get("data", {})
            for key in ["sessions", "showtimes", "schedules", "seances"]:
                if key in movie_data and isinstance(movie_data[key], list):
                    for s in movie_data[key]:
                        if isinstance(s, dict):
                            sessions.append({
                                "cinema": s.get("cinemaName") or s.get("cinema", {}).get("name", "—") if isinstance(s.get("cinema"), dict) else s.get("cinema", "—"),
                                "time": s.get("time") or s.get("startTime", ""),
                                "format": s.get("format") or s.get("technology", ""),
                                "price": str(s.get("price") or s.get("minPrice", "")),
                                "buy_url": f"{kino_base}/ru/{city_slug}/session/{s.get('id', '')}" if s.get("id") else movie_url,
                            })
            
            # If no sessions in data, fetch movie page
            if not sessions:
                _, movie_pp = await fetch_kino_page(client, movie_url)
                if movie_pp:
                    for key in ["sessions", "showtimes", "schedules", "seances", "shows"]:
                        if key in movie_pp and isinstance(movie_pp[key], list):
                            for s in movie_pp[key]:
                                if isinstance(s, dict):
                                    sessions.append({
                                        "cinema": s.get("cinemaName") or (s.get("cinema", {}).get("name", "—") if isinstance(s.get("cinema"), dict) else s.get("cinema", "—")),
                                        "time": s.get("time") or s.get("startTime", ""),
                                        "format": s.get("format") or s.get("technology", ""),
                                        "price": str(s.get("price") or s.get("minPrice", "")),
                                        "buy_url": f"{kino_base}/ru/{city_slug}/session/{s.get('id', '')}" if s.get("id") else movie_url,
                                    })
                    
                    # Deep search for sessions
                    if not sessions:
                        found = deep_search_keys(movie_pp, {"sessions", "showtimes", "schedules", "seances"})
                        for key, val in found.items():
                            if isinstance(val, list):
                                for s in val:
                                    if isinstance(s, dict) and (s.get("time") or s.get("startTime")):
                                        sessions.append({
                                            "cinema": s.get("cinemaName") or s.get("cinema", "—"),
                                            "time": s.get("time") or s.get("startTime", ""),
                                            "buy_url": movie_url,
                                        })
            
            if cinema and sessions:
                sessions = [s for s in sessions if cinema.lower() in s.get("cinema", "").lower()]
            
            logger.info(f"Sessions found: {len(sessions)}")
            
            return {
                "movie": best["title"],
                "date": target_date,
                "sessions": sessions,
                "count": len(sessions),
                "movie_url": movie_url,
            }

        except Exception as e:
            logger.error(f"Kino.kz error: {e}", exc_info=True)
            return {
                "error": str(e),
                "kino_url": f"{kino_base}/ru/{city_slug}",
                "message": f"Ошибка. Прямая ссылка: {kino_base}/ru/{city_slug}",
            }


async def list_cinema_movies(city="Алматы", cinema=""):
    """List all movies currently showing."""
    city_slug = CITY_SLUGS.get(city.lower().strip(), "almaty")
    kino_base = "https://kino.kz"

    async with httpx.AsyncClient(timeout=25, headers=KINO_HEADERS, follow_redirects=True) as client:
        try:
            urls = [
                f"{kino_base}/ru/{city_slug}",
                f"{kino_base}/{city_slug}",
            ]
            
            all_movies = []
            
            for url in urls:
                _, pp = await fetch_kino_page(client, url)
                if pp:
                    movie_keys = ["movies", "films", "items", "data", "nowShowing", 
                                  "releases", "allMovies", "moviesList", "showing", "premieres"]
                    
                    for key in movie_keys:
                        if key in pp and isinstance(pp[key], list):
                            for item in pp[key]:
                                if isinstance(item, dict):
                                    title = (item.get("title") or item.get("name") or 
                                            item.get("nameRu") or "")
                                    if title:
                                        all_movies.append({
                                            "title": title,
                                            "genre": str(item.get("genre") or item.get("genres", "")),
                                            "rating": str(item.get("rating") or item.get("imdbRating", "")),
                                            "url": f"{kino_base}/ru/{city_slug}/movie/{item.get('slug', item.get('id', ''))}",
                                        })
                    
                    # Deep search
                    found = deep_search_keys(pp, set(movie_keys))
                    for key, val in found.items():
                        if isinstance(val, list):
                            for item in val:
                                if isinstance(item, dict):
                                    title = (item.get("title") or item.get("name") or 
                                            item.get("nameRu") or "")
                                    if title and not any(m["title"] == title for m in all_movies):
                                        all_movies.append({
                                            "title": title,
                                            "url": f"{kino_base}/ru/{city_slug}/movie/{item.get('slug', item.get('id', ''))}",
                                        })
                    
                    if all_movies:
                        break
            
            # Deduplicate
            seen = set()
            unique = []
            for m in all_movies:
                if m["title"] not in seen:
                    seen.add(m["title"])
                    unique.append(m)
            
            logger.info(f"Listed {len(unique)} movies for {city}")
            
            return {
                "movies": unique[:20],
                "count": len(unique),
                "city": city,
                "kino_url": f"{kino_base}/ru/{city_slug}",
            }
        except Exception as e:
            logger.error(f"Cinema listing error: {e}", exc_info=True)
            return {"error": str(e), "kino_url": f"{kino_base}/ru/{city_slug}"}


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
    {"name": "list_cinema_movies",
     "description": "Показать все фильмы которые сейчас идут в кинотеатрах. Используй когда пользователь спрашивает 'что идёт в кино', 'какие фильмы в Chaplin' и т.д.",
     "input_schema": {"type": "object", "properties": {
         "city": {"type": "string", "description": "Город (по умолч. Алматы)"},
         "cinema": {"type": "string", "description": "Конкретный кинотеатр (опц.)"},
     }, "required": []}},
]


async def execute_tool(name, params):
    try:
        if name == "create_calendar_event": result = await create_event(**params)
        elif name == "list_calendar_events": result = await list_events(**params)
        elif name == "search_movie_sessions": result = await search_movie_sessions(**params)
        elif name == "list_cinema_movies": result = await list_cinema_movies(**params)
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
