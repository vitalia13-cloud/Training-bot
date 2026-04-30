
  import os
import logging
import base64
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import google.generativeai as genai
from collections import defaultdict
import time
import asyncio

# ── Конфігурація ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT           = int(os.getenv("PORT", 8080))
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH   = "/webhook"
ALLOWED_CHATS  = {c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()}

# ── Логування ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Bot і Dispatcher ──────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

# ── Gemini ────────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ── Rate limiting ─────────────────────────────────────────────────────────────
RATE_LIMIT   = 5
RATE_WINDOW  = 60
_rate_buckets: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    _rate_buckets[user_id] = [t for t in _rate_buckets[user_id] if now - t < RATE_WINDOW]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT:
        return True
    _rate_buckets[user_id].append(now)
    return False

def is_allowed(message: Message) -> bool:
    return str(message.chat.id) in ALLOWED_CHATS

# ── Активи ────────────────────────────────────────────────────────────────────
ASSET_EMOJI = {
    "EURUSD": "🇪🇺", "GBPUSD": "🇬🇧", "AUDUSD": "🇦🇺", "NZDUSD": "🇳🇿",
    "USDJPY": "🇯🇵", "EURJPY": "🔀",  "GBPJPY": "🔀",
    "XAUUSD": "🥇",  "BTCUSD": "₿",
    "GER40":  "🇩🇪", "NAS100": "💻",  "US30": "🏦",
}

SESSION_NAMES = {
    "asian":    "🌏 Азійська сесія",
    "european": "🇪🇺 Європейська сесія",
    "american": "🗽 Американська сесія",
}

_session_buffer: dict[str, dict] = defaultdict(dict)
_session_timers: dict[str, asyncio.Task] = {}

# ── Gemini функції ────────────────────────────────────────────────────────────
async def analyze_text(prompt: str) -> str:
    response = gemini_model.generate_content(prompt)
    return response.text

async def analyze_chart(image_bytes: bytes) -> str:
    img_part = {
        "mime_type": "image/png",
        "data": base64.b64encode(image_bytes).decode(),
    }
    prompt = (
        "Проаналізуй трейдинг графік коротко українською:\n"
        "Тренд, рівні, сигнал (BUY/SELL/WAIT), впевненість."
    )
    response = gemini_model.generate_content([img_part, prompt])
    return response.text

# ── Команди ───────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def start(message: Message):
    if not is_allowed(message):
        return
    await message.answer("Бот працює ✅ Надішли фото графіка")

# ── Фото ──────────────────────────────────────────────────────────────────────
@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_allowed(message):
        return

    if is_rate_limited(message.from_user.id):
        await message.answer("⏳ Зачекай трохи")
        return

    await message.answer("Аналізую...")

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)

        result = await analyze_chart(file_data.read())

        await message.answer(result)

    except Exception as e:
        logger.exception(e)
        await message.answer("Помилка ❌")

# ── Webhook ───────────────────────────────────────────────────────────────────
async def webhook(request: web.Request):
    return web.Response(text="OK")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    app = web.Application()

    app.router.add_post("/tradingview", webhook)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
