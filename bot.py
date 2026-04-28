import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import google.generativeai as genai
from collections import defaultdict
import time

# ── Конфігурація ──────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT           = int(os.getenv("PORT", 8080))
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH   = "/webhook"
ALLOWED_CHATS  = {c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()}

# ── Валідація при старті ──────────────────────────────────────────────────────
def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not WEBHOOK_SECRET:
        missing.append("WEBHOOK_SECRET")
    if not WEBHOOK_HOST:
        missing.append("WEBHOOK_HOST")
    if not ALLOWED_CHATS:
        missing.append("ALLOWED_CHATS")
    if missing:
        raise ValueError(f"Відсутні обов'язкові змінні середовища: {', '.join(missing)}")

# ── Логування ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Gemini клієнт (один раз при старті) ──────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# ── Rate limiting (макс 5 запитів на хвилину на юзера) ───────────────────────
RATE_LIMIT     = 5
RATE_WINDOW    = 60  # секунд
_rate_buckets: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now   = time.monotonic()
    calls = _rate_buckets[user_id]
    # видаляємо старі дзвінки поза вікном
    _rate_buckets[user_id] = [t for t in calls if now - t < RATE_WINDOW]
    if len(_rate_buckets[user_id]) >= RATE_LIMIT:
        return True
    _rate_buckets[user_id].append(now)
    return False

# ── Перевірка дозволеного чату ────────────────────────────────────────────────
def is_allowed(message: Message) -> bool:
    return str(message.chat.id) in ALLOWED_CHATS

# ── Активи ───────────────────────────────────────────────────────────────────
ASSET_EMOJI = {
    "EURUSD": "EU",  "GBPUSD": "GB",  "AUDUSD": "AU",  "NZDUSD": "NZ",
    "USDJPY": "JP",  "EURJPY": "EJ",  "GBPJPY": "GJ",
    "XAUUSD": "XAU", "BTCUSD": "BTC",
    "GER40":  "DE",  "NAS100": "NQ",  "US30": "DJ",
}

# ── Аналіз графіка ────────────────────────────────────────────────────────────
async def analyze_chart(image_bytes: bytes) -> str:
    import base64
    img_part = {
        "mime_type": "image/png",
        "data": base64.b64encode(image_bytes).decode(),
    }
    prompt = (
        "Ти досвідчений трейдер. Проаналізуй графік:\n"
        "1. Тренд: бичачий / ведмежий / боковий\n"
        "2. Рівні підтримки та опору\n"
        "3. Патерни свічок\n"
        "4. Рекомендація: BUY / SELL / WAIT\n"
        "Коротко, українською мовою."
    )
    response = gemini_model.generate_content([img_part, prompt])
    return response.text

# ── Команди ───────────────────────────────────────────────────────────────────
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "Привіт! Я Trading Signal Bot\n\n"
        "Що я вмію:\n"
        "- Аналізую графіки — надішли скріншот\n"
        "- Авто-сигнали з TradingView\n"
        "- EUR, GBP, XAU, BTC, GER40, NAS100, US30\n\n"
        "Команди:\n"
        "/start — меню\n"
        "/assets — список активів\n"
        "/help — як підключити TradingView\n\n"
        "Надішли фото графіка — і я його проаналізую!"
    )

@dp.message(Command("assets"))
async def cmd_assets(message: Message):
    if not is_allowed(message):
        return
    lines = ["Відстежувані активи:\n"]
    for asset, code in ASSET_EMOJI.items():
        lines.append(f"{code} - {asset}")
    await message.answer("\n".join(lines))

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "Як підключити TradingView Webhook:\n\n"
        "1. TradingView → Alerts → Create Alert\n"
        "2. Webhook URL:\n"
        f"{WEBHOOK_HOST}/tradingview\n\n"
        "3. Message JSON:\n"
        '{\n  "secret": "<твій WEBHOOK_SECRET>",\n'
        '  "symbol": "{{ticker}}",\n'
        '  "action": "BUY",\n'
        '  "price": {{close}},\n'
        '  "timeframe": "1H"\n}\n\n'
        "4. Збережи — сигнали надходитимуть автоматично!"
    )

# ── Обробник фото ─────────────────────────────────────────────────────────────
@dp.message(F.photo)
async def handle_photo(message: Message):
    # 1. Дозволений чат?
    if not is_allowed(message):
        logger.warning("Фото від незнайомого чату %s — ігноруємо", message.chat.id)
        return

    # 2. Rate limit
    if is_rate_limited(message.from_user.id):
        await message.answer("⏳ Забагато запитів. Зачекай хвилину і спробуй знову.")
        return

    await message.answer("Аналізую графік... зачекай")
    try:
        photo     = message.photo[-1]
        file      = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        analysis  = await analyze_chart(file_data.read())
        now       = datetime.now().strftime("%d.%m.%Y %H:%M")
        await message.answer(f"Аналіз графіка — {now}\n\n{analysis}")
    except Exception as e:
        logger.exception("Помилка аналізу фото від %s", message.chat.id)
        await message.answer("Помилка аналізу. Спробуй ще раз.")

# ── TradingView webhook ───────────────────────────────────────────────────────
_tv_rate: dict[str, float] = {}
TV_COOLDOWN = 10  # секунд між однаковими сигналами

async def tradingview_webhook(request: web.Request) -> web.Response:
    # 1. JSON
    try:
        data = await request.json()
    except Exception:
        logger.warning("Невалідний JSON від %s", request.remote)
        return web.Response(status=400, text="Invalid JSON")

    # 2. Секрет
    if data.get("secret", "") != WEBHOOK_SECRET:
        logger.warning("Невірний секрет від %s", request.remote)
        return web.Response(status=403, text="Forbidden")

    symbol    = data.get("symbol", "UNKNOWN").upper()
    action    = data.get("action", "").upper()
    price     = data.get("price", "N/A")
    timeframe = data.get("timeframe", "")

    # 3. Захист від дублів
    key = f"{symbol}:{action}"
    now = time.monotonic()
    if now - _tv_rate.get(key, 0) < TV_COOLDOWN:
        logger.info("Дубльований сигнал %s — пропускаємо", key)
        return web.Response(text="OK")
    _tv_rate[key] = now

    code        = ASSET_EMOJI.get(symbol, "")
    action_text = "КУПИТИ" if action == "BUY" else "ПРОДАТИ" if action == "SELL" else action
    ts          = datetime.now().strftime("%d.%m.%Y %H:%M")

    signal = (
        f"НОВИЙ СИГНАЛ — {ts}\n\n"
        f"{code} {symbol}\n"
        f"{action_text}\n"
        f"Ціна: {price}\n"
        f"Таймфрейм: {timeframe}\n\n"
        f"Це не фінансова порада."
    )

    logger.info("Сигнал: %s %s @ %s (%s)", symbol, action, price, timeframe)

    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, signal)
        except Exception:
            logger.exception("Не вдалось надіслати сигнал у чат %s", chat_id)

    return web.Response(text="OK")

# ── Старт / стоп ──────────────────────────────────────────────────────────────
async def on_startup(app: web.Application):
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logger.info("Webhook встановлено: %s", webhook_url)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    logger.info("Webhook видалено")

# ── Точка входу ───────────────────────────────────────────────────────────────
def main():
    validate_config()

    app = web.Application()
    app.router.add_post("/tradingview", tradingview_webhook)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    logger.info("Запуск на порту %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
