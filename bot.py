import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
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
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY", "")
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")
PORT             = int(os.getenv("PORT", 8080))
WEBHOOK_HOST     = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PATH     = "/webhook"
ALLOWED_CHATS    = {c.strip() for c in os.getenv("ALLOWED_CHATS", "").split(",") if c.strip()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Copenhagen")

def now_local() -> str:
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

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
NEWS_CACHE_TTL = 300

SYSTEM_PROMPT = """Ти досвідчений трейдер і аналітик для інтрадей торгівлі.
Відповідай українською мовою. Будь конкретним і структурованим.
Для КОЖНОГО активу обов'язково вказуй:
📈 ЛОНГ або 📉 ШОРТ — чітко і однозначно
Рівні: вхід / стоп-лос / тейк-профіт
Враховуй як технічний аналіз так і новинний фон.
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

async def get_market_news() -> list:
    global _news_cache, _news_cache_time
    now = time.monotonic()
    if _news_cache and now - _news_cache_time < NEWS_CACHE_TTL:
        return _news_cache
    url = (
        f"https://newsapi.org/v2/everything?"
        f"q=forex+gold+bitcoin+stock+market&"
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
    return "\n".join(news) if news else "Новини недоступні"

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
            change = d.get("change")
            high = d.get("high")
            low = d.get("low")
            arrow = f"{'🟢' if change >= 0 else '🔴'} {'+' if change >= 0 else ''}{change}%" if change is not None else ""
            hl = f"  H:{high} L:{low}" if high and low else ""
            lines.append(f"  {sym}: {d['price']}  {arrow}{hl}")
    return "\n".join(lines)

def format_prices_context(prices: dict) -> str:
    lines = []
    for sym, d in prices.items():
        change = d.get("change")
        arrow = f"({'+' if change >= 0 else ''}{change}%)" if change is not None else ""
        lines.append(f"{sym}: {d.get('price', 'N/A')} {arrow}")
    return "\n".join(lines)


def calc_rsi(closes: list, period: int = 14) -> float:
    """RSI по списку закриттів (найновіший перший)"""
    if len(closes) < period + 1:
        return 50.0
    closes = list(reversed(closes[:period + 5]))  # старіші спочатку
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

def calc_ema(closes: list, period: int) -> float:
    """EMA по списку закриттів (найновіший перший)"""
    if len(closes) < period:
        return closes[0] if closes else 0
    data = list(reversed(closes))  # старіші спочатку
    k = 2 / (period + 1)
    ema = sum(data[:period]) / period
    for price in data[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 6)

def calc_macd(closes: list) -> dict:
    """MACD (12,26,9) по списку закриттів (найновіший перший)"""
    if len(closes) < 26:
        return {"signal": "нейтр", "hist": 0}
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = ema12 - ema26
    # Спрощений сигнал
    ema12_prev = calc_ema(closes[1:], 12)
    ema26_prev = calc_ema(closes[1:], 26)
    macd_prev = ema12_prev - ema26_prev
    hist = macd_line - macd_prev
    if macd_line > 0 and hist > 0:
        signal = "▲ бичачий"
    elif macd_line < 0 and hist < 0:
        signal = "▼ ведмежий"
    elif hist > 0:
        signal = "↗ розворот вгору"
    else:
        signal = "↘ розворот вниз"
    return {"signal": signal, "hist": round(hist, 6), "line": round(macd_line, 6)}

def calc_indicators(closes: list, sym: str) -> dict:
    """Розраховує всі індикатори"""
    rsi = calc_rsi(closes)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50) if len(closes) >= 50 else None
    macd = calc_macd(closes)
    current = closes[0]

    # RSI зона
    if rsi >= 70:
        rsi_zone = "перекупленість ⚠️"
    elif rsi <= 30:
        rsi_zone = "перепроданість ⚠️"
    elif rsi >= 55:
        rsi_zone = "бичача зона"
    elif rsi <= 45:
        rsi_zone = "ведмежа зона"
    else:
        rsi_zone = "нейтральна"

    # EMA тренд
    ema_trend = None
    if ema50:
        ema_trend = "EMA20>EMA50 ✅" if ema20 > ema50 else "EMA20<EMA50 ❌"

    # Підтвердження сигналу індикаторами
    bull_signals = 0
    bear_signals = 0
    if rsi > 50: bull_signals += 1
    else: bear_signals += 1
    if "бичач" in macd["signal"] or "вгору" in macd["signal"]: bull_signals += 1
    else: bear_signals += 1
    if ema50 and ema20 > ema50: bull_signals += 1
    elif ema50: bear_signals += 1

    return {
        "rsi": rsi,
        "rsi_zone": rsi_zone,
        "macd": macd["signal"],
        "ema20": ema20,
        "ema50": ema50,
        "ema_trend": ema_trend,
        "bull_signals": bull_signals,
        "bear_signals": bear_signals,
    }

def calc_bollinger(closes: list, period: int = 20) -> dict:
    """Bollinger Bands"""
    if len(closes) < period:
        return {}
    data = list(reversed(closes[:period]))
    sma = sum(data) / period
    std = (sum((x - sma) ** 2 for x in data) / period) ** 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    current = closes[0]
    width = (upper - lower) / sma * 100
    if current > upper:
        position = "вище верхньої ⚠️"
    elif current < lower:
        position = "нижче нижньої ⚠️"
    elif current > sma:
        position = "вище середньої"
    else:
        position = "нижче середньої"
    return {
        "upper": round(upper, 6),
        "lower": round(lower, 6),
        "sma":   round(sma, 6),
        "width": round(width, 2),
        "position": position,
    }

def calc_stoch_rsi(closes: list, rsi_period: int = 14, stoch_period: int = 14) -> float:
    """Stochastic RSI"""
    if len(closes) < rsi_period + stoch_period:
        return 50.0
    # Рахуємо RSI для кожної точки
    rsi_values = []
    for i in range(stoch_period):
        rsi_val = calc_rsi(closes[i:], rsi_period)
        rsi_values.append(rsi_val)
    min_rsi = min(rsi_values)
    max_rsi = max(rsi_values)
    if max_rsi == min_rsi:
        return 50.0
    stoch_rsi = (rsi_values[0] - min_rsi) / (max_rsi - min_rsi) * 100
    return round(stoch_rsi, 1)

def calc_fibonacci(highs: list, lows: list) -> dict:
    """Рівні Fibonacci по останніх 20 барах"""
    if not highs or not lows:
        return {}
    high = max(highs)
    low  = min(lows)
    diff = high - low
    return {
        "0.236": round(high - diff * 0.236, 6),
        "0.382": round(high - diff * 0.382, 6),
        "0.500": round(high - diff * 0.500, 6),
        "0.618": round(high - diff * 0.618, 6),
        "0.786": round(high - diff * 0.786, 6),
    }

def detect_candle_pattern(candles: list) -> str:
    """Визначає свічковий патерн по останніх 3 свічках"""
    if len(candles) < 3:
        return ""
    c0 = candles[0]  # поточна
    c1 = candles[1]  # попередня
    c2 = candles[2]  # позапопередня

    o0, h0, l0, c_0 = float(c0["open"]), float(c0["high"]), float(c0["low"]), float(c0["close"])
    o1, h1, l1, c_1 = float(c1["open"]), float(c1["high"]), float(c1["low"]), float(c1["close"])
    o2, h2, l2, c_2 = float(c2["open"]), float(c2["high"]), float(c2["low"]), float(c2["close"])

    body0 = abs(c_0 - o0)
    body1 = abs(c_1 - o1)
    range0 = h0 - l0
    range1 = h1 - l1

    # Doji
    if range0 > 0 and body0 / range0 < 0.1:
        return "🕯 Doji (невизначеність)"

    # Hammer (бичачий розворот)
    lower_wick0 = min(o0, c_0) - l0
    upper_wick0 = h0 - max(o0, c_0)
    if lower_wick0 > body0 * 2 and upper_wick0 < body0 * 0.5 and c_1 < o1:
        return "🔨 Hammer (бичачий розворот)"

    # Shooting Star (ведмежий розворот)
    if upper_wick0 > body0 * 2 and lower_wick0 < body0 * 0.5 and c_1 > o1:
        return "💫 Shooting Star (ведмежий розворот)"

    # Bullish Engulfing
    if c_1 < o1 and c_0 > o0 and o0 < c_1 and c_0 > o1:
        return "🟢 Bullish Engulfing (бичаче поглинання)"

    # Bearish Engulfing
    if c_1 > o1 and c_0 < o0 and o0 > c_1 and c_0 < o1:
        return "🔴 Bearish Engulfing (ведмеже поглинання)"

    # Morning Star
    if c_2 < o2 and body1 < body0 * 0.3 and c_0 > o0 and c_0 > (o2 + c_2) / 2:
        return "⭐ Morning Star (бичачий розворот)"

    # Evening Star
    if c_2 > o2 and body1 < body0 * 0.3 and c_0 < o0 and c_0 < (o2 + c_2) / 2:
        return "🌙 Evening Star (ведмежий розворот)"

    # Three White Soldiers
    if all([
        float(candles[i]["close"]) > float(candles[i]["open"]) for i in range(3)
    ]) and c_0 > c_1 > c_2:
        return "🪖 Three White Soldiers (сильний бичачий)"

    # Three Black Crows
    if all([
        float(candles[i]["close"]) < float(candles[i]["open"]) for i in range(3)
    ]) and c_0 < c_1 < c_2:
        return "🐦 Three Black Crows (сильний ведмежий)"

    return ""



async def get_fear_greed() -> dict:
    """Fear & Greed Index для крипто (безкоштовно)"""
    try:
        async with aiohttp_client.ClientSession() as session:
            async with session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=aiohttp_client.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                item = data.get("data", [{}])[0]
                value = int(item.get("value", 50))
                label = item.get("value_classification", "Neutral")
                if value <= 25:
                    emoji = "😱"
                elif value <= 45:
                    emoji = "😰"
                elif value <= 55:
                    emoji = "😐"
                elif value <= 75:
                    emoji = "😊"
                else:
                    emoji = "🤑"
                return {"value": value, "label": label, "emoji": emoji}
    except Exception:
        return {}

# Polygon тікери для крипто (реальний час)
POLYGON_CRYPTO = {
    "BTCUSD": "X:BTCUSD",
    "ETHUSD": "X:ETHUSD",
}

# Polygon мультиплікатори для таймфреймів
POLYGON_TF = {
    "1D":  ("1", "day"),
    "4H":  ("4", "hour"),
    "1H":  ("1", "hour"),
}

async def get_polygon_mtf(symbol: str) -> dict:
    """MTF через Polygon для крипто — реальний час з обсягами"""
    from datetime import timedelta, date
    ticker = POLYGON_CRYPTO.get(symbol)
    if not ticker:
        return {}
    result = {}
    today = date.today()
    from_date = (today - timedelta(days=30)).isoformat()
    to_date = today.isoformat()
    for tf_label, (mult, span) in POLYGON_TF.items():
        url = (
            f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{mult}/{span}/"
            f"{from_date}/{to_date}?adjusted=true&sort=desc&limit=20&apiKey={POLYGON_API_KEY}"
        )
        try:
            async with aiohttp_client.ClientSession() as session:
                async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    bars = data.get("results", [])
                    if len(bars) >= 5:
                        curr = bars[0]
                        prev = bars[1]
                        close  = float(curr["c"])
                        open_  = float(curr["o"])
                        high   = float(curr["h"])
                        low    = float(curr["l"])
                        volume = float(curr.get("v", 0))
                        prev_close = float(prev["c"])

                        # ATR по останніх 5 барах
                        atr = sum(float(b["h"]) - float(b["l"]) for b in bars[:5]) / 5

                        # Середній обсяг
                        avg_vol = sum(float(b.get("v", 0)) for b in bars[1:11]) / 10
                        vol_ratio = volume / avg_vol if avg_vol > 0 else 1.0

                        # Рівні підтримки/опору з останніх 10 барів
                        recent_highs = sorted([float(b["h"]) for b in bars[1:11]], reverse=True)
                        recent_lows  = sorted([float(b["l"]) for b in bars[1:11]])
                        resistance = recent_highs[0]
                        support    = recent_lows[0]

                        # Напрямок
                        if close > open_ and close > prev_close:
                            trend, direction = "📈", "ЛОНГ"
                        elif close < open_ and close < prev_close:
                            trend, direction = "📉", "ШОРТ"
                        else:
                            trend, direction = "➡️", "НЕЙТР"

                        result[tf_label] = {
                            "price":      round(close, 2),
                            "open":       round(open_, 2),
                            "high":       round(high, 2),
                            "low":        round(low, 2),
                            "trend":      trend,
                            "direction":  direction,
                            "atr":        round(atr, 2),
                            "support":    round(support, 2),
                            "resistance": round(resistance, 2),
                            "volume":     round(volume, 0),
                            "vol_ratio":  round(vol_ratio, 2),
                            "source":     "🟢RT",
                        }
        except Exception:
            logger.warning("Polygon MTF помилка %s %s", symbol, tf_label)
        await asyncio.sleep(1.5)
    return result

async def get_mtf_data(symbol: str) -> dict:
    """MTF: Polygon для крипто (реальний час), Twelve Data для решти (15хв)"""
    if symbol in POLYGON_CRYPTO:
        return await get_polygon_mtf(symbol)
    td_sym = TWELVE_SYMBOLS.get(symbol, symbol)
    timeframes = {"1day": "1D", "4h": "4H", "1h": "1H"}
    result = {}
    for tf, label in timeframes.items():
        url = (
            f"https://api.twelvedata.com/time_series?"
            f"symbol={td_sym}&interval={tf}&outputsize=60&apikey={TWELVE_API_KEY}"
        )
        try:
            async with aiohttp_client.ClientSession() as session:
                async with session.get(url, timeout=aiohttp_client.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    values = data.get("values", [])
                    if len(values) >= 5:
                        curr = values[0]
                        prev = values[1]
                        close  = float(curr["close"])
                        open_  = float(curr["open"])
                        high   = float(curr["high"])
                        low    = float(curr["low"])
                        prev_close = float(prev["close"])

                        # Список закриттів для індикаторів (найновіший перший)
                        closes = [float(v["close"]) for v in values]

                        # ATR
                        atr = sum(float(b["high"]) - float(b["low"]) for b in values[:5]) / 5

                        # Рівні з останніх 10 барів
                        recent_highs = sorted([float(b["high"]) for b in values[1:11]], reverse=True)
                        recent_lows  = sorted([float(b["low"])  for b in values[1:11]])
                        resistance = recent_highs[0]
                        support    = recent_lows[0]

                        # Індикатори
                        ind = calc_indicators(closes, symbol)

                        # Напрямок з урахуванням індикаторів
                        bull = 0
                        bear = 0
                        if close > open_ and close > prev_close: bull += 2
                        elif close < open_ and close < prev_close: bear += 2
                        bull += ind["bull_signals"]
                        bear += ind["bear_signals"]

                        if bull > bear + 1:
                            trend, direction = "📈", "ЛОНГ"
                        elif bear > bull + 1:
                            trend, direction = "📉", "ШОРТ"
                        else:
                            trend, direction = "➡️", "НЕЙТР"

                        # Нові індикатори
                        highs_list = [float(v["high"]) for v in values[:20]]
                        lows_list  = [float(v["low"])  for v in values[:20]]
                        bb     = calc_bollinger(closes)
                        stoch  = calc_stoch_rsi(closes)
                        fibs   = calc_fibonacci(highs_list, lows_list)
                        pattern = detect_candle_pattern(values[:3])

                        # Stoch RSI зона
                        if stoch >= 80:
                            stoch_zone = "перекупленість ⚠️"
                        elif stoch <= 20:
                            stoch_zone = "перепроданість ⚠️"
                        else:
                            stoch_zone = f"{stoch}"

                        result[label] = {
                            "price":      close,
                            "open":       open_,
                            "high":       high,
                            "low":        low,
                            "trend":      trend,
                            "direction":  direction,
                            "atr":        atr,
                            "support":    support,
                            "resistance": resistance,
                            "source":     "🕐15хв",
                            "rsi":        ind["rsi"],
                            "rsi_zone":   ind["rsi_zone"],
                            "macd":       ind["macd"],
                            "ema_trend":  ind["ema_trend"],
                            "bb":         bb,
                            "stoch_rsi":  stoch_zone,
                            "fibs":       fibs,
                            "pattern":    pattern,
                        }
        except Exception:
            logger.warning("MTF помилка %s %s", symbol, tf)
        await asyncio.sleep(0.15)
    return result

async def get_all_mtf() -> dict:
    """MTF для всіх основних активів"""
    main_symbols = ["EURUSD", "GBPUSD", "XAUUSD", "BTCUSD", "NAS100", "US30"]
    results = {}
    for sym in main_symbols:
        data = await get_mtf_data(sym)
        if data:
            results[sym] = data
        await asyncio.sleep(0.2)
    return results

def smart_price(sym: str, price: float) -> str:
    if sym in ("XAUUSD", "GER40", "NAS100", "US30"):
        return f"{price:,.0f}".replace(",", " ")
    elif sym == "BTCUSD":
        return f"{price:,.0f}".replace(",", " ")
    elif sym in ("USDJPY", "EURJPY", "GBPJPY"):
        return f"{price:.3f}"
    else:
        return f"{price:.5f}"

def calc_levels(sym: str, direction: str, price: float, atr: float, support: float, resistance: float) -> dict:
    """Розраховує вхід, стоп і тейк з фіксованим R/R 1:2"""
    if direction == "ЛОНГ":
        entry = price
        # Стоп — за рівнем підтримки або ATR, беремо менший ризик
        stop_atr     = price - atr * 1.0
        stop_support = support - atr * 0.2
        stop  = max(stop_atr, stop_support)  # вищий стоп = менший ризик
        risk  = abs(entry - stop)
        if risk == 0:
            return {}
        target = entry + risk * 2.0  # завжди R/R 1:2
    elif direction == "ШОРТ":
        entry = price
        stop_atr        = price + atr * 1.0
        stop_resistance = resistance + atr * 0.2
        stop  = min(stop_atr, stop_resistance)  # нижчий стоп = менший ризик
        risk  = abs(entry - stop)
        if risk == 0:
            return {}
        target = entry - risk * 2.0  # завжди R/R 1:2
    else:
        return {}
    return {
        "entry":  entry,
        "stop":   stop,
        "target": target,
        "rr":     2.0,
    }

def format_mtf(mtf_data: dict) -> str:
    ASSET_FLAGS = {
        "EURUSD": "🇪🇺", "GBPUSD": "🇬🇧", "AUDUSD": "🇦🇺", "NZDUSD": "🇳🇿",
        "USDJPY": "🇯🇵", "EURJPY": "🔀", "GBPJPY": "🔀",
        "XAUUSD": "🥇", "BTCUSD": "₿", "GER40": "🇩🇪", "NAS100": "💻", "US30": "🏦",
    }
    SIGNAL_ICONS = {"ЛОНГ": "🟢", "ШОРТ": "🔴", "НЕЙТР": "⚪"}
    lines = []
    ts = now_local()
    lines.append(f"📊 MTF — {ts}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    for sym, tfs in mtf_data.items():
        # Пропускаємо якщо немає жодного таймфрейму
        available = [tf for tf in ["1D", "4H", "1H"] if tf in tfs]
        if not available:
            continue

        flag   = ASSET_FLAGS.get(sym, "")
        source = tfs.get("1H", tfs.get("1D", {})).get("source", "")
        best_tf = tfs.get("1H") or tfs.get("4H") or tfs.get("1D")
        price_str = smart_price(sym, best_tf["price"])

        lines.append(f"\n{flag} {sym}  {price_str}  {source}")

        # Таймфрейми в один рядок
        row = []
        for tf_label in ["1D", "4H", "1H"]:
            if tf_label in tfs:
                d = tfs[tf_label]
                icon = SIGNAL_ICONS.get(d["direction"], "⚪")
                row.append(f"{tf_label}{icon}")
        lines.append("  " + "  ".join(row))

        # Визначаємо загальний напрямок — більшість виграє
        directions = [tfs[tf]["direction"] for tf in available]
        long_count  = directions.count("ЛОНГ")
        short_count = directions.count("ШОРТ")

        if long_count >= 2:
            overall = "ЛОНГ"
        elif short_count >= 2:
            overall = "ШОРТ"
        else:
            overall = "НЕЙТР"

        # Індикатори з найкращого таймфрейму (1H або 4H)
        rsi       = best_tf.get("rsi")
        rsi_zone  = best_tf.get("rsi_zone", "")
        macd_sig  = best_tf.get("macd", "")
        ema_trend = best_tf.get("ema_trend", "")
        stoch     = best_tf.get("stoch_rsi", "")
        bb        = best_tf.get("bb", {})
        pattern   = best_tf.get("pattern", "")
        fibs      = best_tf.get("fibs", {})

        # Рядок індикаторів
        ind_parts = []
        if rsi: ind_parts.append(f"RSI {rsi} {rsi_zone}")
        if stoch and stoch != "50.0": ind_parts.append(f"StochRSI {stoch}")
        if macd_sig: ind_parts.append(f"MACD {macd_sig}")
        if ema_trend: ind_parts.append(ema_trend)
        if ind_parts:
            lines.append(f"  📊 {' │ '.join(ind_parts)}")

        # Bollinger Bands
        if bb.get("position"):
            bb_width = f" (шир. {bb['width']}%)" if bb.get("width") else ""
            lines.append(f"  📐 BB: {bb['position']}{bb_width}")

        # Свічковий патерн
        if pattern:
            lines.append(f"  🕯 {pattern}")

        # Ключові рівні Fib (найближчі до ціни)
        if fibs and best_tf.get("price"):
            price = best_tf["price"]
            fib_levels = sorted(fibs.items(), key=lambda x: abs(float(x[1]) - price))[:2]
            fib_str = "  ".join(f"Fib{k}: {smart_price(sym, v)}" for k, v in fib_levels)
            if fib_str:
                lines.append(f"  🌀 {fib_str}")

        if overall != "НЕЙТР" and "atr" in best_tf:
            levels = calc_levels(
                sym, overall,
                best_tf["price"],
                best_tf["atr"],
                best_tf.get("support", best_tf["price"] * 0.99),
                best_tf.get("resistance", best_tf["price"] * 1.01),
            )
            if levels:
                icon = SIGNAL_ICONS[overall]
                lines.append(f"  {icon} {overall}")
                lines.append(f"  🎯 Вхід:  {smart_price(sym, levels['entry'])}")
                lines.append(f"  🛑 Стоп:  {smart_price(sym, levels['stop'])}")
                lines.append(f"  💰 Тейк:  {smart_price(sym, levels['target'])}")
                lines.append(f"  ⚖️ R/R:   1:{levels['rr']}")

                # Обсяг для крипто
                vol = best_tf.get("vol_ratio")
                if vol is not None:
                    vol_str = "🔥 Підвищений" if vol > 1.3 else "📉 Знижений" if vol < 0.7 else "Нормальний"
                    lines.append(f"  📊 Обсяг: {vol_str} (x{vol})")
        else:
            lines.append(f"  ⚪ Немає чіткого сигналу")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🟢 ЛОНГ  🔴 ШОРТ  ⚪ НЕЙТР   ⚠️ Не фін. порада")
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
            max_tokens=1500,
            temperature=0.7,
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception:
        logger.exception("Groq помилка")
        return "❌ Помилка запиту. Спробуй ще раз."

def calc_signal_score(tfs: dict) -> int:
    """Розраховує % ймовірності відпрацювання сигналу (0-100)"""
    score = 0
    available = [tf for tf in ["1D", "4H", "1H"] if tf in tfs]
    if not available:
        return 0

    best = tfs.get("1H") or tfs.get("4H") or tfs.get("1D")
    directions = [tfs[tf]["direction"] for tf in available]
    long_count  = directions.count("ЛОНГ")
    short_count = directions.count("ШОРТ")

    # MTF збіг (до 30 балів)
    if long_count == 3 or short_count == 3:
        score += 30  # всі 3 таймфрейми збігаються
    elif long_count == 2 or short_count == 2:
        score += 18

    # RSI не в екстремальній зоні (до 15 балів)
    rsi = best.get("rsi", 50)
    if 40 <= rsi <= 60:
        score += 15
    elif 35 <= rsi <= 65:
        score += 10
    elif rsi > 70 or rsi < 30:
        score -= 10  # перекупленість/перепроданість — ризик

    # MACD підтверджує (до 15 балів)
    macd = best.get("macd", "")
    if "бичачий" in macd or "вгору" in macd:
        if long_count >= 2: score += 15
    elif "ведмежий" in macd or "вниз" in macd:
        if short_count >= 2: score += 15

    # EMA тренд збігається (до 10 балів)
    ema = best.get("ema_trend", "")
    if "✅" in ema and long_count >= 2: score += 10
    elif "❌" in ema and short_count >= 2: score += 10

    # Bollinger Bands (до 10 балів)
    bb = best.get("bb", {})
    bb_pos = bb.get("position", "")
    if "середньої" in bb_pos and ("вище" in bb_pos and long_count >= 2):
        score += 10
    elif "середньої" in bb_pos and ("нижче" in bb_pos and short_count >= 2):
        score += 10

    # Свічковий патерн (до 10 балів)
    pattern = best.get("pattern", "")
    if pattern:
        if ("бичачий" in pattern and long_count >= 2) or            ("ведмежий" in pattern and short_count >= 2):
            score += 10

    # StochRSI (до 10 балів)
    stoch_str = str(best.get("stoch_rsi", "50"))
    try:
        stoch_val = float(stoch_str.replace("перекупленість ⚠️", "85").replace("перепроданість ⚠️", "15"))
        if 30 <= stoch_val <= 70:
            score += 10
    except:
        pass

    return min(max(score, 0), 100)

def format_session_mtf(mtf_data: dict, top_n: int = 3) -> str:
    """Форматує сесійний аналіз з ранжуванням по % відпрацювання"""
    ASSET_FLAGS = {
        "EURUSD": "🇪🇺", "GBPUSD": "🇬🇧", "AUDUSD": "🇦🇺", "NZDUSD": "🇳🇿",
        "USDJPY": "🇯🇵", "EURJPY": "🔀", "GBPJPY": "🔀",
        "XAUUSD": "🥇", "BTCUSD": "₿", "GER40": "🇩🇪", "NAS100": "💻", "US30": "🏦",
    }
    SIGNAL_ICONS = {"ЛОНГ": "🟢", "ШОРТ": "🔴", "НЕЙТР": "⚪"}

    # Рахуємо скори для всіх активів
    scored = []
    for sym, tfs in mtf_data.items():
        score = calc_signal_score(tfs)
        available = [tf for tf in ["1D", "4H", "1H"] if tf in tfs]
        if not available:
            continue
        directions = [tfs[tf]["direction"] for tf in available]
        long_count  = directions.count("ЛОНГ")
        short_count = directions.count("ШОРТ")
        if long_count >= 2:
            overall = "ЛОНГ"
        elif short_count >= 2:
            overall = "ШОРТ"
        else:
            overall = "НЕЙТР"
        scored.append((sym, tfs, score, overall))

    # Сортуємо по скору, беремо топ без НЕЙТР
    scored.sort(key=lambda x: x[2], reverse=True)
    top = [x for x in scored if x[3] != "НЕЙТР"][:top_n]

    lines = []
    for i, (sym, tfs, score, overall) in enumerate(top, 1):
        flag = ASSET_FLAGS.get(sym, "")
        best = tfs.get("1H") or tfs.get("4H") or tfs.get("1D")
        price_str = smart_price(sym, best["price"])
        icon = SIGNAL_ICONS[overall]

        # Рівні
        levels = {}
        if "atr" in best:
            levels = calc_levels(
                sym, overall, best["price"], best["atr"],
                best.get("support", best["price"] * 0.99),
                best.get("resistance", best["price"] * 1.01),
            )

        lines.append(f"{'🥇' if i==1 else '🥈' if i==2 else '🥉'} #{i} {flag} {sym}  {price_str}")
        lines.append(f"  {icon} {overall}  📊 Ймовірність: {score}%")

        # Таймфрейми
        row = []
        for tf_label in ["1D", "4H", "1H"]:
            if tf_label in tfs:
                d = tfs[tf_label]
                row.append(f"{tf_label}{SIGNAL_ICONS.get(d['direction'], '⚪')}")
        lines.append(f"  {'  '.join(row)}")

        # Індикатори
        rsi = best.get("rsi")
        macd = best.get("macd", "")
        pattern = best.get("pattern", "")
        if rsi: lines.append(f"  RSI {rsi} │ MACD {macd}")
        if pattern: lines.append(f"  {pattern}")

        # Рівні входу
        if levels:
            lines.append(f"  🎯 Вхід: {smart_price(sym, levels['entry'])}")
            lines.append(f"  🛑 Стоп: {smart_price(sym, levels['stop'])}")
            lines.append(f"  💰 Тейк: {smart_price(sym, levels['target'])}")
            lines.append(f"  ⚖️ R/R: 1:{levels['rr']}")
        lines.append("")

    return "\n".join(lines)

async def send_session_report(session: str):
    session_name = SESSION_NAMES.get(session, session.upper())
    ts = now_local()
    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, f"⏳ Готую аналіз для {session_name}...")
        except Exception:
            pass

    # Збираємо всі дані паралельно
    mtf_data, news, fg = await asyncio.gather(
        get_all_mtf(),
        get_market_news(),
        get_fear_greed(),
    )

    news_text = format_news(news)
    session_analysis = format_session_mtf(mtf_data, top_n=3)

    # Заголовок
    header = (
        f"📊 АНАЛІЗ ПЕРЕД СЕСІЄЮ\n"
        f"{session_name}\n"
        f"🕐 {ts}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏆 ТОП-3 УГОДИ\n"
        f"(відсортовано по % відпрацювання)\n\n"
    )

    # Fear & Greed для BTC
    fg_line = ""
    if fg:
        fg_line = f"\n😨 Fear & Greed: {fg['emoji']} {fg['value']}/100 ({fg['label']})"

    # Новини коротко
    news_short = "\n".join(news[:3]) if news else ""

    report = (
        f"{header}"
        f"{session_analysis}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 Ключові новини:\n{news_short}"
        f"{fg_line}\n\n"
        f"⚠️ Це не фінансова порада."
    )

    for chat_id in ALLOWED_CHATS:
        try:
            await bot.send_message(chat_id, report)
        except Exception:
            logger.exception("Помилка надсилання %s", chat_id)

    _session_buffer.pop(session, None)
    _session_timers.pop(session, None)

@dp.message(Command("mtf"))
async def cmd_mtf(message: Message):
    if not is_allowed(message):
        return
    await message.answer("⏳ Завантажую MTF аналіз...")
    mtf_data, fg = await asyncio.gather(get_all_mtf(), get_fear_greed())
    if not mtf_data:
        await message.answer("❌ Не вдалось отримати дані.")
        return
    result = format_mtf(mtf_data)
    if fg:
        fg_line = f"\n😨 Fear & Greed: {fg['emoji']} {fg['value']}/100 ({fg['label']})"
        result = result.replace("🟢 ЛОНГ  🔴 ШОРТ", fg_line + "\n🟢 ЛОНГ  🔴 ШОРТ")
    await message.answer(result)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_allowed(message):
        return
    _chat_history[message.from_user.id].clear()
    await message.answer(
        "👋 Привіт! Я Trading Bot\n\n"
        "📌 Команди:\n"
        "/mtf — аналіз 1D/4H/1H по активах\n"
        "/prices — реальні ціни\n"
        "/analyze — аналіз з новинами\n"
        "/news — останні новини\n"
        "/sessions — розклад сесій\n"
        "/assets — список активів\n"
        "/help — підключення TradingView\n"
        "/clear — очистити чат\n\n"
        "💬 Пиши питання — відповім з реальними даними!"
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
    ts = now_local()
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
    ts = now_local()
    await message.answer(f"📰 Новини — {ts}\n\n{format_news(news)}")

@dp.message(Command("analyze"))
async def cmd_analyze(message: Message):
    if not is_allowed(message):
        return
    await message.answer("🔍 Аналізую ринок з новинами...")
    prices, news = await asyncio.gather(get_all_quotes(), get_market_news())
    prices_context = format_prices_context(prices) if prices else "недоступно"
    news_text      = format_news(news)
    ts = now_local()
    prompt = (
        f"Реальні ціни на {ts}:\n{prices_context}\n\n"
        f"Останні новини:\n{news_text}\n\n"
        "Дай аналіз для інтрадей:\n"
        "1. 🌍 Настрій ринку\n"
        "2. 🏆 Топ-3 можливості\n"
        "3. Для кожного:\n"
        "   📈 ЛОНГ або 📉 ШОРТ\n"
        "   🎯 Вхід / 🛑 Стоп / 💰 Тейк\n"
        "   📰 Зв'язок з новинами\n"
        "4. ⚡ Головні ризики"
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
        "📊 Перед кожною — автоматичний аналіз з новинами.\n"
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

    # Розпізнавання тікерів — якщо написали XAU, BTC, EUR і т.д.
    TICKER_ALIASES = {
        "XAU": "XAUUSD", "GOLD": "XAUUSD", "ЗОЛОТО": "XAUUSD",
        "BTC": "BTCUSD", "BITCOIN": "BTCUSD", "БІТКОІН": "BTCUSD",
        "EUR": "EURUSD", "EURUSD": "EURUSD",
        "GBP": "GBPUSD", "GBPUSD": "GBPUSD",
        "AUD": "AUDUSD", "AUDUSD": "AUDUSD",
        "NZD": "NZDUSD", "NZDUSD": "NZDUSD",
        "JPY": "USDJPY", "USDJPY": "USDJPY",
        "EURJPY": "EURJPY", "GBPJPY": "GBPJPY",
        "NAS": "NAS100", "NAS100": "NAS100", "NASDAQ": "NAS100",
        "DJ": "US30", "US30": "US30", "DOW": "US30",
        "DAX": "GER40", "GER40": "GER40", "GER": "GER40",
    }
    text_upper = message.text.strip().upper()
    matched_sym = TICKER_ALIASES.get(text_upper)

    if matched_sym:
        await message.answer(f"⏳ MTF аналіз {matched_sym}...")
        mtf = await get_mtf_data(matched_sym)
        if mtf:
            result = format_mtf({matched_sym: mtf})
            if matched_sym == "BTCUSD":
                fg = await get_fear_greed()
                if fg:
                    result += f"\n😨 Fear & Greed: {fg['emoji']} {fg['value']}/100 ({fg['label']})"
            await message.answer(result)
        else:
            await message.answer(f"❌ Не вдалось отримати дані для {matched_sym}.")
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
    ts = now_local()
    signal = (
        f"🔔 СИГНАЛ — {ts}\n\n"
        f"{symbol}\n{action_text}\n"
        f"💰 Ціна: {price}\n"
        f"⏱ Таймфрейм: {timeframe}\n\n"
        f"⚠️ Це не фінансова порада."
    )
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
