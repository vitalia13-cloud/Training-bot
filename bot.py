import os
import logging
import base64
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import google.generativeai as genai
# ── Config ──────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret123")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://your-app.railway.app")
WEBHOOK_PATH = "/webhook"
ALLOWED_CHATS = os.getenv("ALLOWED_CHATS", "").split(",")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ASSET_EMOJI = {
"EURUSD": " ", "GBPUSD": " ", "AUDUSD": " ", "NZDUSD": " ",
"USDJPY": " ", "EURJPY": " ", "GBPJPY": " ",
"XAUUSD": " ", "BTCUSD": " ",
"GER40": " ", "NAS100": " ", "US30": " "
}
async def analyze_chart(image_bytes: bytes) -> str:
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")
img_part = {
"mime_type": "image/png",
"data": base64.b64encode(image_bytes).decode()
}
prompt = (
"Ти досвідчений трейдер. Проаналізуй цей торговий графік:\n"
"1. Тренд: бичачий / ведмежий / боковий\n"
"2. Ключові рівні підтримки та опору\n"
"3. Патерни свічок або технічні сигнали\n"
"4. Рекомендація: BUY / SELL / WAIT + коротке пояснення\n"
"Відповідай коротко, використовуй емодзі. Мова: українська."
)
response = model.generate_content([img_part, prompt])
return response.text
@dp.message(Command("start"))
async def cmd_start(message: Message):
await message.answer(
" *Привіт! Я Trading Signal Bot*\n\n"
" Що я вмію:\n"
"• Аналізую графіки — надішли скріншот\n"
"• Отримую авто-сигнали з TradingView\n"
"• EUR, GBP, XAU, BTC, GER40, NAS100, US30\n\n"
" *Команди:*\n"
"/start — меню\n"
"/assets — список активів\n"
"/help — як підключити TradingView\n\n"
" Надішли фото графіка — і я його проаналізую!",
parse_mode="Markdown"
)
@dp.message(Command("assets"))
async def cmd_assets(message: Message):
lines = [" *Відстежувані активи:*\n"]
for asset, emoji in ASSET_EMOJI.items():
lines.append(f"{emoji} {asset}")
await message.answer("\n".join(lines), parse_mode="Markdown")
@dp.message(Command("help"))
async def cmd_help(message: Message):
await message.answer(
" *Як підключити TradingView Webhook:*\n\n"
" TradingView → Alerts → Create Alert\n"
" Webhook URL:\n"
f"`{WEBHOOK_HOST}/tradingview`\n\n"
" Message (JSON):\n"
"```\n"
"{\n"
' "secret": "mysecret123",\n'
' "symbol": "{{ticker}}",\n'
' "action": "BUY",\n'
' "price": {{close}},\n'
' "timeframe": "{{interval}}"\n'
"}\n"
"```\n"
" Збережи — сигнали надходитимуть автоматично ",
parse_mode="Markdown"
)
@dp.message(F.photo)
async def handle_photo(message: Message):
await message.answer(" try:
Аналізую графік... зачекай кілька секунд")
photo = message.photo[-1]
file = await bot.get_file(photo.file_id)
file_bytes = await bot.download_file(file.file_path)
analysis = await analyze_chart(file_bytes.read())
now = datetime.now().strftime("%d.%m.%Y %H:%M")
await message.answer(f" except Exception as e:
*Аналіз графіка* — {now}\n\n{analysis}", parse_mode="Markdo
logger.error(f"Chart analysis error: {e}")
await message.answer(" Помилка аналізу. Спробуй ще раз.")
@dp.message(F.document)
async def handle_document(message: Message):
if message.document.mime_type and message.document.mime_type.startswith("image/"):
await message.answer(" Аналізую графік з файлу...")
try:
file = await bot.get_file(message.document.file_id)
file_bytes = await bot.download_file(file.file_path)
analysis = await analyze_chart(file_bytes.read())
now = datetime.now().strftime("%d.%m.%Y %H:%M")
await message.answer(f" *Аналіз графіка* — {now}\n\n{analysis}", parse_mode="Ma
except Exception as e:
logger.error(f"Document error: {e}")
await message.answer(" Помилка аналізу файлу.")
else:
await message.answer(" Надішли зображення графіка (PNG/JPG).")
async def tradingview_webhook(request: web.Request) -> web.Response:
try:
data = await request.json()
except Exception:
return web.Response(status=400, text="Invalid JSON")
if data.get("secret", "") != WEBHOOK_SECRET:
return web.Response(status=403, text="Forbidden")
symbol = data.get("symbol", "UNKNOWN").upper()
action = data.get("action", "").upper()
price = data.get("price", "N/A")
timeframe = data.get("timeframe", "")
msg_text = data.get("message", "")
emoji = ASSET_EMOJI.get(symbol, " ")
action_emoji = " " if action == "BUY" else " now = datetime.now().strftime("%d.%m.%Y %H:%M")
signal = (
f" *НОВИЙ СИГНАЛ* — {now}\n\n"
f"{emoji} *{symbol}*\n"
f"{action_emoji} *{action}*\n"
f" Ціна: `{price}`\n"
f" Таймфрейм: {timeframe}\n"
" if action == "SELL" else " "
)
if msg_text:
signal += f" {msg_text}\n"
signal += "\n _Це не фінансова порада._"
for chat_id in ALLOWED_CHATS:
chat_id = chat_id.strip()
if chat_id:
try:
await bot.send_message(chat_id, signal, parse_mode="Markdown")
except Exception as e:
logger.error(f"Send error {chat_id}: {e}")
return web.Response(text="OK")
async def on_startup(app):
await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")
async def on_shutdown(app):
await bot.delete_webhook()
def main():
app = web.Application()
app.router.add_post("/tradingview", tradingview_webhook)
SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
setup_application(app, dp, bot=bot)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
web.run_app(app, host="0.0.0.0", port=PORT)
if __name__ == "__main__":
main()
