import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from collections import defaultdict
import time
import asyncio
import aiohttp as aiohttp_client
from groq import Groq

BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY     = os.getenv("GROQ_API_KEY", "")
TWELVE_API_KEY   = os.getenv("TWELVE_API_KEY", "")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")
PORT             = int(os.getenv("PORT", 8080))
WEBHOOK_HOST     = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH     = "/webhook"
ALLOWED_CHATS    = {c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)

RATE_LIMIT  = 5
RATE_WINDOW = 60
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

TWELVE_SYMBOLS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "AUDUSD": "AUD/USD",
    "NZDUSD": "NZD/USD",
    "USDJPY": "USD/JPY",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
    "XAUUSD": "XAU/USD",
    "BTCUSD": "BTC/USD",
    "GER40":  "DAX",
    "NAS100": "NDX",
    "US30":   "DJI",
}

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
_chat_history: dict[int, list] = defaultdict(list)
MAX_HISTORY = 20

SYSTEM_PROMPT = """Ти досвідчений трейдер і фінансовий аналітик для інтрадей торгівлі.
Ти аналізуєш реальні ринкові дані які тобі надаються і даєш конкретні рекомендації.
Активи: EURUSD, GBPUSD, XAUUSD, BTCUSD, GER40, NAS100, US30.
Відповідай завжди українською мовою, коротко і конкретно.
Завжди додавай: "Це не фінансова порада." в кінці торгових рекомендацій."""

async def get_real_prices(symbols: list = None) -> dict:
    if symbols is None:
        symbols = list(TWELVE_SYMBOLS.keys())
    symbols_str = ",".join(TWELVE_SYMBOLS[s] for s in symbols if s in TWELVE_SYMBOLS)
    url = f"https://api.twelvedata.com/price?symbol={symbols_str}&apikey={TWELVE_API_KEY}"
    prices = {}
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                for sym, td_sym in TWELVE_SYMBOLS.items():
                    if sym not in symbols:
                        continue
                    key = td_sym
                    if key in data and "price" in data[key]:
                        prices[sym] = {"price": float(data[key]["price"])}
                    elif "price" in data:
                        prices[symbols[0]] = {"price": float(data["price"])}
    except Exception:
        logger.exception("Помилка отримання цін з Twelve Data")
    return prices

async def get_quote(symbol: str) -> dict:
    td_sym = TWELVE_SYMBOLS.get(symbol, symbol)
    url = f"https://api.twelvedata.com/quote?symbol={td_sym}&apikey={TWELVE_API_KEY}"
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if "close" in data:
                    change = float(data.get("percent_change", 0))
                    return {
                        "price": float(data["close"]),
                        "open": float(data.get("open", 0)),
                        "high": float(data.get("high", 0)),
                        "low": float(data.get("low", 0)),
                        "change": round(change, 3),
                    }
    except Exception:
        logger.exception("Помилка quote %s", symbol)
    return {}

async def get_all_quotes() -> dict:
    results = {}
    for sym in TWELVE_SYMBOLS:
        q = await get_quote(sym)
        if q:
            results[sym] = q
        await asyncio.sleep(0.1)
    return results

def format_prices(prices: dict) -> str:
    lines = []
    for symbol, data in prices.items():
        price = data.get("price", "N/A")
        change = data.get("change", None)
        high = data.get("high")
        low = data.get("low")
        arrow = ""
        if change is not None:
            arrow = f" {'▲' if change >= 0 else '▼'}{abs(change)}%"
        extra = ""
        if high and low:
            extra = f" | H:{high} L:{low}"
        lines.append(f"{ASSET_EMOJI.get(symbol, '')} {symbol}: {price}{arrow}{extra}")
    return "\n".join(lines)

async def ask_groq(user_id: int, user_message: str, extra_context: str = "") -> str:
    history = _chat_history[user_id]
    content = f"{extra_context}\n\nПитання: {user_message}" if extra_context else user_message
    history.append({"role": "user", "content": content})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        _chat_history[user_id] = history
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=1024,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception:
        logger.exception("Groq помилка")
        return "Помилка запиту. Спробуй ще раз."

async def send_session_report(session: str):
    session_name = SESSION_NAMES.get(session, session.upper())
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, f"Збираю реальні ціни для {session_name}...")
        except Exception:
            pass
    prices = await get_all_quotes()
    prices_text = format_prices(prices) if prices else "Не вдалось отримати ціни"
    prompt = (
        f"Зараз починається {session_name}.\n"
        f"Реальні ринкові дані:\n{prices_text}\n\n"
        "Зроби аналіз для інтрадей торгівлі:\n"
        "1. Загальний настрій ринку\n"
        "2. Топ-3 активи з найбільшим потенціалом прямо зараз\n"
        "3. Для кожного: напрямок BUY/SELL, точка входу, стоп-лос, тейк-профіт\n"
        "4. На що звернути особливу увагу сьогодні"
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        analysis = response.choices[0].message.content
        report = f"АНАЛІЗ ПЕРЕД СЕСІЄЮ\n{session_name} - {ts}\n\nПоточні ціни:\n{prices_text}\n\n{analysis}"
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
    _chat_history[message.from_user.id].clear()
    await message.answer(
        "Привіт! Я Trading Signal Bot\n\n"
        "Команди:\n"
        "/prices — реальні ціни зараз\n"
        "/analyze — аналіз ринку з реальними даними\n"
        "/assets — список активів\n"
        "/sessions — розклад сесій\n"
        "/help — як підключити TradingView\n"
        "/clear — очистити чат\n\n"
        "Або просто пиши питання про ринок!"
    )

@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    if not is_allowed(message):
        return
    await message.answer("Отримую реальні ціни...")
    prices = await get_all_quotes()
    if not prices:
        await message.answer("Не вдалось отримати ціни. Спробуй пізніше.")
        return
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    await message.answer(f"Реальні ціни — {ts}\n\n{format_prices(prices)}")

@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    if not is_allowed(message):
        return
    await message.answer("Аналізую ринок з реальними даними...")
    prices = await get_all_quotes()
    prices_text = format_prices(prices) if prices else "Дані недоступні"
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"Реальні ринкові дані на {ts}:\n{prices_text}\n\n"
        "Дай короткий аналіз для інтрадей торгівлі:\n"
        "1. Загальний настрій ринку\n"
        "2. Топ-3 найкращих можливості прямо зараз\n"
        "3. Для кожної: напрямок, вхід, стоп, тейк"
    )
    reply = await ask_groq(message.from_user.id, prompt)
    await message.answer(f"Аналіз ринку — {ts}\n\n{reply}")

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_allowed(message):
        return
    _chat_history[message.from_user.id].clear()
    await message.answer("Історію чату очищено.")

@dp.message(Command("sessions"))
async def cmd_sessions(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "Розклад торгових сесій (Київський час):\n\n"
        "Азійська:     02:00 — 10:00\n"
        "Європейська:  10:00 — 18:00\n"
        "Американська: 16:30 — 23:00\n\n"
        "Бот надсилає аналіз перед кожною сесією.\n"
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
        "4. Для сигналів — Message:\n"
        '{"secret":"твій_секрет","type":"signal","symbol":"{{ticker}}","action":"BUY","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "5. Збережи — все готово!"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_allowed(message):
        return
    if is_rate_limited(message.from_user.id):
        await message.answer("Забагато запитів. Зачекай хвилину.")
        return
    await message.answer("Думаю...")
    prices = await get_real_prices()
    prices_text = format_prices(prices) if prices else ""
    context = f"Поточні реальні ринкові дані:\n{prices_text}" if prices_text else ""
    reply = await ask_groq(message.from_user.id, message.text, context)
    await message.answer(reply)

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
    signal = f"НОВИЙ СИГНАЛ — {ts}\n\n{code} {symbol}\n{action_text}\nЦіна: {price}\nТаймфрейм: {timeframe}\n\nЦе не фінансова порада."
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, signal)
        except Exception:
            logger.exception("Не вдалось надіслати сигнал у чат %s", chat_id)
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")
    logger.info("Webhook встановлено: %s%s", WEBHOOK_HOST, WEBHOOK_PATH)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()

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
