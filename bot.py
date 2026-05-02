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

RATE_LIMIT  = 10
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
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "AUDUSD": "AUD/USD", "NZDUSD": "NZD/USD",
    "USDJPY": "USD/JPY", "EURJPY": "EUR/JPY", "GBPJPY": "GBP/JPY",
    "XAUUSD": "XAU/USD", "BTCUSD": "BTC/USD",
    "GER40": "DAX", "NAS100": "NDX", "US30": "DJI",
}

SESSION_NAMES = {
    "asian":    "🌏 Азійська сесія",
    "european": "🇪🇺 Європейська сесія",
    "american": "🗽 Американська сесія",
}

_session_buffer: dict[str, dict] = defaultdict(dict)
_session_timers: dict[str, asyncio.Task] = {}
_chat_history: dict[int, list] = defaultdict(list)
MAX_HISTORY = 20

# Кеш цін — оновлюється раз на 30 секунд
_prices_cache: dict = {}
_prices_cache_time: float = 0
CACHE_TTL = 30

SYSTEM_PROMPT = """Ти досвідчений трейдер і аналітик для інтрадей торгівлі.
Відповідай українською мовою. Будь конкретним і зрозумілим.
Використовуй реальні дані які тобі надаються.
Структуруй відповідь чітко: використовуй емодзі для наочності.
В кінці торгових рекомендацій завжди пиши: ⚠️ Це не фінансова порада."""

async def get_all_quotes() -> dict:
    global _prices_cache, _prices_cache_time
    now = time.monotonic()
    if _prices_cache and now - _prices_cache_time < CACHE_TTL:
        return _prices_cache

    results = {}
    for sym, td_sym in TWELVE_SYMBOLS.items():
        url = f"https://api.twelvedata.com/quote?symbol={td_sym}&apikey={TWELVE_API_KEY}"
        try:
            async with aiohttp_client.ClientSession() as session:
                async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=8)) as resp:
                    data = await resp.json()
                    if "close" in data:
                        results[sym] = {
                            "price": float(data["close"]),
                            "open":  float(data.get("open", 0)),
                            "high":  float(data.get("high", 0)),
                            "low":   float(data.get("low", 0)),
                            "change": round(float(data.get("percent_change", 0)), 3),
                        }
        except Exception:
            logger.warning("Не вдалось отримати %s", sym)
        await asyncio.sleep(0.05)

    if results:
        _prices_cache = results
        _prices_cache_time = now
    return results

async def get_quick_prices() -> dict:
    """Швидкий запит лише поточних цін (без H/L) для контексту чату"""
    global _prices_cache, _prices_cache_time
    now = time.monotonic()
    if _prices_cache and now - _prices_cache_time < CACHE_TTL:
        return _prices_cache
    symbols_str = ",".join(TWELVE_SYMBOLS.values())
    url = f"https://api.twelvedata.com/price?symbol={symbols_str}&apikey={TWELVE_API_KEY}"
    results = {}
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                for sym, td_sym in TWELVE_SYMBOLS.items():
                    if td_sym in data and "price" in data[td_sym]:
                        results[sym] = {"price": float(data[td_sym]["price"])}
    except Exception:
        logger.warning("Не вдалось отримати швидкі ціни")
    return results

def format_prices_pretty(prices: dict) -> str:
    """Красивий формат цін для відображення"""
    sections = {
        "💱 Валюти": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "EURJPY", "GBPJPY"],
        "🏅 Метали та Крипто": ["XAUUSD", "BTCUSD"],
        "📈 Індекси": ["GER40", "NAS100", "US30"],
    }
    lines = []
    for section, syms in sections.items():
        lines.append(f"\n{section}")
        for sym in syms:
            if sym not in prices:
                continue
            d = prices[sym]
            price = d["price"]
            change = d.get("change", None)
            high = d.get("high")
            low = d.get("low")
            arrow = f"{'🟢' if change >= 0 else '🔴'} {'+' if change >= 0 else ''}{change}%" if change is not None else ""
            hl = f"  📊 H: {high}  L: {low}" if high and low else ""
            lines.append(f"  {sym}: {price}  {arrow}{hl}")
    return "\n".join(lines)

def format_prices_context(prices: dict) -> str:
    """Компактний формат для контексту Groq"""
    lines = []
    for sym, d in prices.items():
        price = d.get("price", "N/A")
        change = d.get("change")
        arrow = f"({'+'if change>=0 else ''}{change}%)" if change is not None else ""
        lines.append(f"{sym}: {price} {arrow}")
    return "\n".join(lines)

async def ask_groq(user_id: int, user_message: str, extra_context: str = "") -> str:
    history = _chat_history[user_id]
    content = f"{extra_context}\n\nПитання користувача: {user_message}" if extra_context else user_message
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
        return "❌ Помилка запиту. Спробуй ще раз."

async def send_session_report(session: str):
    session_name = SESSION_NAMES.get(session, session.upper())
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, f"⏳ Збираю дані для {session_name}...")
        except Exception:
            pass
    prices = await get_all_quotes()
    prices_pretty = format_prices_pretty(prices)
    prices_context = format_prices_context(prices)
    prompt = (
        f"Починається {session_name} ({ts}).\n"
        f"Реальні ціни:\n{prices_context}\n\n"
        "Зроби аналіз для інтрадей:\n"
        "1. 🌍 Загальний настрій ринку\n"
        "2. 🏆 Топ-3 найкращі можливості зараз\n"
        "3. Для кожного активу: напрямок, вхід, стоп-лос, тейк-профіт\n"
        "4. ⚡ На що звернути особливу увагу"
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        analysis = response.choices[0].message.content
        report = f"📊 АНАЛІЗ — {session_name}\n🕐 {ts}\n{prices_pretty}\n\n{analysis}"
        for chat_id in ALLOWED_CHATS:
            try:
                await bot.send_message(chat_id, report)
            except Exception:
                logger.exception("Помилка надсилання звіту %s", chat_id)
    except Exception:
        logger.exception("Помилка генерації звіту")
    _session_buffer.pop(session, None)
    _session_timers.pop(session, None)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    _chat_history[message.from_user.id].clear()
    await message.answer(
        "👋 Привіт! Я Trading Bot\n\n"
        "📌 Команди:\n"
        "/prices — реальні ціни зараз\n"
        "/analyze — аналіз ринку\n"
        "/sessions — розклад сесій\n"
        "/assets — список активів\n"
        "/help — підключення TradingView\n"
        "/clear — очистити чат\n\n"
        "💬 Або просто пиши питання про ринок!"
    )

@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    if not is_allowed(message):
        return
    await message.answer("⏳ Отримую реальні ціни...")
    prices = await get_all_quotes()
    if not prices:
        await message.answer("❌ Не вдалось отримати ціни. Спробуй пізніше.")
        return
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    await message.answer(f"📈 Ринок — {ts}\n{format_prices_pretty(prices)}")

@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    if not is_allowed(message):
        return
    await message.answer("🔍 Аналізую ринок...")
    prices = await get_all_quotes()
    prices_context = format_prices_context(prices) if prices else "дані недоступні"
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"Реальні ціни на {ts}:\n{prices_context}\n\n"
        "Дай аналіз для інтрадей:\n"
        "1. 🌍 Загальний настрій ринку\n"
        "2. 🏆 Топ-3 найкращі можливості прямо зараз\n"
        "3. Для кожної: напрямок, вхід, стоп, тейк\n"
        "4. ⚡ Ключові рівні на сьогодні"
    )
    reply = await ask_groq(message.from_user.id, prompt)
    await message.answer(f"📊 Аналіз ринку — {ts}\n\n{reply}")

@dp.message(Command("clear"))
async def cmd_clear(message: Message):
    if not is_allowed(message):
        return
    _chat_history[message.from_user.id].clear()
    await message.answer("🗑 Чат очищено.")

@dp.message(Command("sessions"))
async def cmd_sessions(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "🕐 Торгові сесії (Київський час):\n\n"
        "🌏 Азійська:     02:00 — 10:00\n"
        "🇪🇺 Європейська: 10:00 — 18:00\n"
        "🗽 Американська: 16:30 — 23:00\n\n"
        "📊 Бот надсилає аналіз перед кожною сесією.\n"
        "⚙️ Налаштуй alerts у TradingView — /help"
    )

@dp.message(Command("assets"))
async def cmd_assets(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "📋 Відстежувані активи:\n\n"
        "💱 Валюти:\n"
        "  EURUSD • GBPUSD • AUDUSD • NZDUSD\n"
        "  USDJPY • EURJPY • GBPJPY\n\n"
        "🏅 Метали та Крипто:\n"
        "  XAUUSD (Золото) • BTCUSD (Bitcoin)\n\n"
        "📈 Індекси:\n"
        "  GER40 • NAS100 • US30"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "⚙️ Підключення TradingView:\n\n"
        "1️⃣ TradingView → Alerts → Create Alert\n"
        "2️⃣ Webhook URL:\n"
        f"   {WEBHOOK_HOST}/tradingview\n\n"
        "3️⃣ Для сесійного аналізу:\n"
        '{"secret":"SECRET","type":"session_start","session":"european","symbol":"{{ticker}}","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "session: asian / european / american\n\n"
        "4️⃣ Для сигналів:\n"
        '{"secret":"SECRET","type":"signal","symbol":"{{ticker}}","action":"BUY","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "5️⃣ Збережи — готово! ✅"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_allowed(message):
        return
    if is_rate_limited(message.from_user.id):
        await message.answer("⏳ Забагато запитів. Зачекай хвилину.")
        return
    await message.answer("💭 Думаю...")
    # Використовуємо кеш — не гальмуємо відповідь
    prices = await get_quick_prices()
    context = f"Поточні ринкові ціни:\n{format_prices_context(prices)}" if prices else ""
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
    key = f"{symbol}:{action}"
    now = time.monotonic()
    if now - _tv_rate.get(key, 0) < TV_COOLDOWN:
        return web.Response(text="OK")
    _tv_rate[key] = now
    action_text = "🟢 КУПИТИ" if action == "BUY" else "🔴 ПРОДАТИ" if action == "SELL" else action
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    signal = f"🔔 СИГНАЛ — {ts}\n\n{symbol}\n{action_text}\n💰 Ціна: {price}\n⏱ Таймфрейм: {timeframe}\n\n⚠️ Це не фінансова порада."
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, signal)
        except Exception:
            logger.exception("Помилка надсилання сигналу %s", chat_id)
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")
    logger.info("Webhook: %s%s", WEBHOOK_HOST, WEBHOOK_PATH)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()

def main():
    app = web.Application()
    app.router.add_post("/tradingview", tradingview_webhook)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    logger.info("Порт: %s", PORT)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
