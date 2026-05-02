rm /mnt/user-data/outputs/bot.py && cat > /mnt/user-data/outputs/bot.py << 'ENDOFFILE'
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
NEWS_API_KEY     = os.getenv("NEWS_API_KEY", "")
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

_prices_cache: dict = {}
_prices_cache_time: float = 0
_news_cache: list = []
_news_cache_time: float = 0
CACHE_TTL = 30
NEWS_CACHE_TTL = 300  # 5 хвилин

SYSTEM_PROMPT = """Ти досвідчений трейдер і аналітик для інтрадей торгівлі.
Відповідай українською мовою. Будь конкретним і структурованим.
Для КОЖНОГО активу обов'язково вказуй:
📈 ЛОНГ або 📉 ШОРТ — чітко і однозначно
Рівні: вхід / стоп-лос / тейк-профіт
Враховуй як технічний аналіз так і новинний фон.
В кінці торгових рекомендацій завжди пиши: ⚠️ Це не фінансова порада."""

# ── Ціни ──────────────────────────────────────────────────────────────────────
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
                            "price":  float(data["close"]),
                            "open":   float(data.get("open", 0)),
                            "high":   float(data.get("high", 0)),
                            "low":    float(data.get("low", 0)),
                            "change": round(float(data.get("percent_change", 0)), 3),
                        }
        except Exception:
            logger.warning("Ціна %s недоступна", sym)
        await asyncio.sleep(0.05)
    if results:
        _prices_cache = results
        _prices_cache_time = now
    return results

async def get_quick_prices() -> dict:
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
        logger.warning("Швидкі ціни недоступні")
    return results

# ── Новини ────────────────────────────────────────────────────────────────────
async def get_market_news() -> list:
    global _news_cache, _news_cache_time
    now = time.monotonic()
    if _news_cache and now - _news_cache_time < NEWS_CACHE_TTL:
        return _news_cache
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=forex+gold+bitcoin+stock+market+trading&"
        f"language=en&sortBy=publishedAt&pageSize=10&"
        f"apiKey={NEWS_API_KEY}"
    )
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=8)) as resp:
                data = await resp.json()
                articles = data.get("articles", [])
                news = []
                for a in articles[:8]:
                    title = a.get("title", "")
                    source = a.get("source", {}).get("name", "")
                    if title and "[Removed]" not in title:
                        news.append(f"• [{source}] {title}")
                _news_cache = news
                _news_cache_time = now
                return news
    except Exception:
        logger.warning("Новини недоступні")
        return []

def format_news(news: list) -> str:
    if not news:
        return "Новини недоступні"
    return "\n".join(news)

# ── Форматування цін ──────────────────────────────────────────────────────────
def format_prices_pretty(prices: dict) -> str:
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
            change = d.get("change", None)
            high = d.get("high")
            low = d.get("low")
            arrow = f"{'🟢' if change >= 0 else '🔴'} {'+' if change >= 0 else ''}{change}%" if change is not None else ""
            hl = f"  H:{d['high']} L:{d['low']}" if high and low else ""
            lines.append(f"  {sym}: {d['price']}  {arrow}{hl}")
    return "\n".join(lines)

def format_prices_context(prices: dict) -> str:
    lines = []
    for sym, d in prices.items():
        change = d.get("change")
        arrow = f"({'+' if change >= 0 else ''}{change}%)" if change is not None else ""
        lines.append(f"{sym}: {d.get('price','N/A')} {arrow}")
    return "\n".join(lines)

# ── Groq ──────────────────────────────────────────────────────────────────────
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
            max_tokens=1500,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception:
        logger.exception("Groq помилка")
        return "❌ Помилка запиту. Спробуй ще раз."

# ── Сесійний звіт ─────────────────────────────────────────────────────────────
async def send_session_report(session: str):
    session_name = SESSION_NAMES.get(session, session.upper())
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, f"⏳ Готую аналіз для {session_name}...")
        except Exception:
            pass
    prices, news = await asyncio.gather(get_all_quotes(), get_market_news())
    prices_pretty  = format_prices_pretty(prices)
    prices_context = format_prices_context(prices)
    news_text      = format_news(news)
    prompt = (
        f"Починається {session_name} ({ts}).\n\n"
        f"📊 Реальні ціни:\n{prices_context}\n\n"
        f"📰 Останні новини:\n{news_text}\n\n"
        "Зроби повний аналіз:\n"
        "1. 🌍 Загальний настрій ринку (враховуй новини)\n"
        "2. 🏆 Топ-3 найкращі можливості для інтрадей\n"
        "3. Для кожного активу:\n"
        "   📈 ЛОНГ або 📉 ШОРТ\n"
        "   🎯 Вхід: [ціна]\n"
        "   🛑 Стоп-лос: [ціна]\n"
        "   💰 Тейк-профіт: [ціна]\n"
        "   📰 Вплив новин на актив\n"
        "4. ⚡ Ключові події та ризики на сьогодні"
    )
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
            max_tokens=1500,
        )
        analysis = response.choices[0].message.content
        report = (
            f"📊 АНАЛІЗ — {session_name}\n"
            f"🕐 {ts}\n"
            f"{prices_pretty}\n\n"
            f"📰 Новини:\n{news_text}\n\n"
            f"{analysis}"
        )
        for chat_id in ALLOWED_CHATS:
            try:
                await bot.send_message(chat_id, report)
            except Exception:
                logger.exception("Помилка надсилання %s", chat_id)
    except Exception:
        logger.exception("Помилка звіту")
    _session_buffer.pop(session, None)
    _session_timers.pop(session, None)

# ── Команди ───────────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    _chat_history[message.from_user.id].clear()
    await message.answer(
        "👋 Привіт! Я Trading Bot\n\n"
        "📌 Команди:\n"
        "/prices — реальні ціни\n"
        "/analyze — аналіз з новинами\n"
        "/news — останні новини\n"
        "/sessions — розклад сесій\n"
        "/assets — список активів\n"
        "/help — підключення TradingView\n"
        "/clear — очистити чат\n\n"
        "💬 Або пиши питання — відповім з реальними даними!"
    )

@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    if not is_allowed(message):
        return
    await message.answer("⏳ Отримую ціни...")
    prices = await get_all_quotes()
    if not prices:
        await message.answer("❌ Не вдалось отримати ціни.")
        return
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    await message.answer(f"📈 Ринок — {ts}\n{format_prices_pretty(prices)}")

@dp.message(Command("news"))
async def cmd_news(message: Message):
    if not is_allowed(message):
        return
    await message.answer("⏳ Завантажую новини...")
    news = await get_market_news()
    if not news:
        await message.answer("❌ Новини недоступні.")
        return
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    await message.answer(f"📰 Фінансові новини — {ts}\n\n{format_news(news)}")

@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    if not is_allowed(message):
        return
    await message.answer("🔍 Аналізую ринок з новинами...")
    prices, news = await asyncio.gather(get_all_quotes(), get_market_news())
    prices_context = format_prices_context(prices) if prices else "недоступно"
    news_text      = format_news(news)
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    prompt = (
        f"Реальні ціни на {ts}:\n{prices_context}\n\n"
        f"Останні новини:\n{news_text}\n\n"
        "Дай аналіз для інтрадей торгівлі:\n"
        "1. 🌍 Настрій ринку з урахуванням новин\n"
        "2. 🏆 Топ-3 можливості прямо зараз\n"
        "3. Для кожного:\n"
        "   📈 ЛОНГ або 📉 ШОРТ\n"
        "   🎯 Вхід / 🛑 Стоп / 💰 Тейк\n"
        "   📰 Зв'язок з новинами\n"
        "4. ⚡ Головні ризики зараз"
    )
    reply = await ask_groq(message.from_user.id, prompt)
    await message.answer(f"📊 Аналіз — {ts}\n\n{reply}")

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
        "📊 Перед кожною сесією — автоматичний аналіз з новинами.\n"
        "⚙️ Налаштуй alerts — /help"
    )

@dp.message(Command("assets"))
async def cmd_assets(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "📋 Активи:\n\n"
        "💱 Валюти: EURUSD • GBPUSD • AUDUSD • NZDUSD • USDJPY • EURJPY • GBPJPY\n"
        "🏅 Метали: XAUUSD (Золото)\n"
        "₿ Крипто: BTCUSD\n"
        "📈 Індекси: GER40 • NAS100 • US30"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not is_allowed(message):
        return
    await message.answer(
        "⚙️ Підключення TradingView:\n\n"
        "1️⃣ Alerts → Create Alert\n"
        "2️⃣ Webhook URL:\n"
        f"   {WEBHOOK_HOST}/tradingview\n\n"
        "3️⃣ Сесійний аналіз:\n"
        '{"secret":"SECRET","type":"session_start","session":"european","symbol":"{{ticker}}","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "4️⃣ Сигнал:\n"
        '{"secret":"SECRET","type":"signal","symbol":"{{ticker}}","action":"BUY","price":{{close}},"timeframe":"{{interval}}"}\n\n'
        "session: asian / european / american"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_allowed(message):
        return
    if is_rate_limited(message.from_user.id):
        await message.answer("⏳ Забагато запитів. Зачекай хвилину.")
        return
    await message.answer("💭 Думаю...")
    prices, news = await asyncio.gather(get_quick_prices(), get_market_news())
    prices_text = format_prices_context(prices) if prices else ""
    news_text   = format_news(news) if news else ""
    context = ""
    if prices_text:
        context += f"Поточні ціни:\n{prices_text}\n"
    if news_text:
        context += f"\nОстанні новини:\n{news_text}"
    reply = await ask_groq(message.from_user.id, message.text, context)
    await message.answer(reply)

# ── TradingView webhook ───────────────────────────────────────────────────────
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
    action_text = "📈 ЛОНГ" if action == "BUY" else "📉 ШОРТ" if action == "SELL" else action
    ts = datetime.now().strftime("%d.%m.%Y %H:%M")
    signal = f"🔔 СИГНАЛ — {ts}\n\n{symbol}\n{action_text}\n💰 Ціна: {price}\n⏱ Таймфрейм: {timeframe}\n\n⚠️ Це не фінансова порада."
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, signal)
        except Exception:
            logger.exception("Помилка сигналу %s", chat_id)
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
ENDOFFILE
Output

exit code 0
