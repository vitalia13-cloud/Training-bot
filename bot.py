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
 
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT           = int(os.getenv("PORT", 8080))
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH   = "/webhook"
ALLOWED_CHATS  = {c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()}
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
 
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
 
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")
 
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
 
ASSET_EMOJI = {
    "EURUSD": "EU", "GBPUSD": "GB", "AUDUSD": "AU", "NZDUSD": "NZ",
    "USDJPY": "JP", "EURJPY": "EJ", "GBPJPY": "GJ",
    "XAUUSD": "XAU", "BTCUSD": "BTC",
    "GER40": "DE", "NAS100": "NQ", "US30": "DJ",
}
 
SESSION_NAMES = {
    "asian":    "Азійська сесія",
    "european": "Європейська сесія",
    "american": "Американська сесія",
}
 
_session_buffer: dict[str, dict] = defaultdict(dict)
_session_timers: dict[str, asyncio.Task] = {}
 
async def analyze_text(prompt: str) -> str:
    response = gemini_model.generate_content(prompt)
    return response.text
 
async def analyze_chart(image_bytes: bytes) -> str:
    img_part = {"mime_type": "image/png", "data": base64.b64encode(image_bytes).decode()}
    prompt = (
        "Ти досвідчений трейдер. Проаналізуй графік:\n"
        "1. Тренд: бичачий / ведмежий / боковий\n"
        "2. Рівні підтримки та опору\n"
        "3. Патерни свічок\n"
        "4. Рекомендація: BUY / SELL / WAIT\n"
        "5. Рівень впевненості: низький / середній / високий\n"
        "Коротко, українською мовою."
    )
    response = gemini_model.generate_content([img_part, prompt])
    return response.text
 
async def send_session_report(session: str):
    buffer = _session_buffer.get(session, {})
    if not buffer:
        return
    session_name = SESSION_NAMES.get(session, session.upper())
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    assets_info = "\n".join(
        f"- {symbol}: ціна {data['price']}, таймфрейм {data['timeframe']}"
        for symbol, data in buffer.items()
    )
    prompt = (
        f"Ти досвідчений трейдер. Зараз починається {session_name}.\n"
        f"Ось поточні ціни активів:\n{assets_info}\n\n"
        "Зроби короткий аналіз:\n"
        "1. Загальний настрій ринку\n"
        "2. Топ-3 активи з найбільшим потенціалом\n"
        "3. Для кожного: напрямок (BUY/SELL), ключові рівні, рівень впевненості\n"
        "4. Загальна рекомендація на сесію\n"
        "Відповідай українською, коротко і по суті."
    )
    try:
        analysis = await analyze_text(prompt)
        report = f"АНАЛІЗ ПЕРЕД СЕСІЄЮ\n{session_name} - {ts}\n\n{analysis}\n\nЦе не фінансова порада."
        for chat_id in ALLOWED_CHATS:
            try:
                await bot.send_message(chat_id, report)
            except Exception:
                logger.exception("Не вдалось надіслати звіт у чат %s", chat_id)
    except Exception:
        logger.exception("Помилка генерації сесійного звіту")
    _session_buffer.pop(session, None)
    _session_timers.pop(session, None)
 
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "Привіт! Я Trading Signal Bot\n\n"
        "Що я вмію:\n"
        "- Аналізую графіки — надішли скріншот\n"
        "- Авто-сигнали з TradingView\n"
        "- Аналіз перед сесіями (Азія/Європа/Америка)\n\n"
        "Команди:\n"
        "/start — меню\n"
        "/assets — список активів\n"
        "/sessions — розклад сесій\n"
        "/help — як підключити TradingView\n\n"
        "Надішли фото графіка — і я його проаналізую!"
    )
 
@dp.message(Command("sessions"))
async def cmd_sessions(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "Розклад торгових сесій (Київський час):\n\n"
        "Азійська:     02:00 — 10:00\n"
        "Європейська:  10:00 — 18:00\n"
        "Американська: 16:30 — 23:00\n\n"
        "Бот надсилає аналіз за 15 хв до початку кожної сесії.\n"
        "Налаштуй alerts у TradingView — /help"
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
        "Як підключити TradingView:\n\n"
        "1. TradingView - Alerts - Create Alert\n"
        "2. Webhook URL:\n"
        f"{WEBHOOK_HOST}/tradingview\n\n"
        "3. Для сесійного аналізу — Message:\n"
        '{"secret":"твій_секрет","type":"session_start","session":"european","symbol":"{{ticker}}","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "session: asian / european / american\n\n"
        "4. Для звичайних сигналів — Message:\n"
        '{"secret":"твій_секрет","type":"signal","symbol":"{{ticker}}","action":"BUY","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "5. Збережи — все готово!"
    )
 
@dp.message(F.photo)
async def handle_photo(message: Message):
    if not is_allowed(message):
        return
    if is_rate_limited(message.from_user.id):
        await message.answer("Забагато запитів. Зачекай хвилину.")
        return
    await message.answer("Аналізую графік... зачекай")
    try:
        photo     = message.photo[-1]
        file      = await bot.get_file(photo.file_id)
        file_data = await bot.download_file(file.file_path)
        analysis  = await analyze_chart(file_data.read())
        now       = datetime.now().strftime("%d.%m.%Y %H:%M")
        await message.answer(f"Аналіз графіка — {now}\n\n{analysis}")
    except Exception:
        logger.exception("Помилка аналізу фото від %s", message.chat.id)
        await message.answer("Помилка аналізу. Спробуй ще раз.")
 
_tv_rate: dict[str, float] = {}
TV_COOLDOWN = 10
 
async def tradingview_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    if data.get("secret", "") != WEBHOOK_SECRET:
        return web.Response(status=403, text="Forbidden")
 
    signal_type = data.get("type", "signal")
    symbol      = data.get("symbol", "UNKNOWN").upper()
    price       = data.get("price", "N/A")
    timeframe   = data.get("timeframe", "")
 
    if signal_type == "session_start":
        session = data.get("session", "").lower()
        if session not in SESSION_NAMES:
            return web.Response(status=400, text="Unknown session")
        _session_buffer[session][symbol] = {"price": price, "timeframe": timeframe}
        logger.info("Сесійний сигнал: %s %s @ %s", session, symbol, price)
        if session not in _session_timers:
            async def delayed_report(s=session):
                await asyncio.sleep(60)
                await send_session_report(s)
            _session_timers[session] = asyncio.create_task(delayed_report())
        return web.Response(text="OK")
 
    action = data.get("action", "").upper()
    key    = f"{symbol}:{action}"
    now    = time.monotonic()
    if now - _tv_rate.get(key, 0) < TV_COOLDOWN:
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
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, signal)
        except Exception:
            logger.exception("Не вдалось надіслати сигнал у чат %s", chat_id)
    return web.Response(text="OK")
 
async def on_startup(app: web.Application):
    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url)
    logger.info("Webhook встановлено: %s", webhook_url)
 
async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    logger.info("Webhook видалено")
 
def main():
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
