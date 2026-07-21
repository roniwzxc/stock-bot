import asyncio
import logging
import os
import re
import time
from io import StringIO

import pandas as pd
import requests
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# --- Настройки -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_БОТА")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL", "ВСТАВЬТЕ_СЮДА_ССЫЛКУ_НА_CSV")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", 8080))

CACHE_TTL = 300      # раз в сколько секунд обновлять данные из таблицы
MAX_RESULTS = 10     # сколько товаров показывать за один раз
# -----------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

_cache = {"data": None, "ts": 0}

CYR_TO_LAT = str.maketrans(
    "АВЕКМНОРСТУХавекмнорстух",
    "ABEKMHOPCTYXabekmhopctyx",
)


def normalize(text: str) -> str:
    """Приводит артикул/запрос к единому виду для сравнения:
    без учёта регистра, без пробелов и дефисов, без путаницы
    русских и латинских букв-двойников."""
    text = str(text).translate(CYR_TO_LAT).upper()
    return re.sub(r"[\s\-_]+", "", text)


def humanize_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин. назад"
    hours = minutes // 60
    return f"{hours} ч. назад"


def load_table() -> pd.DataFrame:
    """Скачивает таблицу из Google Sheets и кэширует её на CACHE_TTL секунд."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    response = requests.get(SHEET_CSV_URL, timeout=15)
    response.raise_for_status()
    df = pd.read_csv(StringIO(response.text))
    df = df.dropna(how="all")

    name_col = df.columns[0]
    df["_normalized"] = df[name_col].apply(normalize)

    _cache["data"] = df
    _cache["ts"] = now
    return df


def search_products(query: str, df: pd.DataFrame) -> pd.DataFrame:
    """Ищет товар по артикулу — без учёта регистра, пробелов и раскладки."""
    normalized_query = normalize(query)
    mask = df["_normalized"].str.contains(normalized_query, na=False)
    return df[mask].head(MAX_RESULTS)


def format_row(row: pd.Series) -> str:
    """Форматирует строку: артикул + количество с индикатором 🟢/🔴."""
    lines = [f"🔹 <b>{row.iloc[0]}</b>"]
    other_cols = [c for c in row.index[1:] if not c.startswith("_")]

    for i, col in enumerate(other_cols):
        value = row[col]
        if pd.isna(value) or not str(value).strip():
            continue

        if i == 0:
            # первая колонка после артикула — количество на складе
            try:
                qty = float(str(value).replace(",", "."))
            except ValueError:
                qty = None
            if qty is not None:
                if qty > 0:
                    lines.append(f"🟢 В наличии: {value} шт.")
                else:
                    lines.append("🔴 Нет в наличии")
            else:
                lines.append(f"    {col}: {value}")
        else:
            lines.append(f"    {col}: {value}")

    return "\n".join(lines)


HELP_TEXT = (
    "Просто напишите артикул (или его часть) — например, 885 или TM885, "
    "регистр и раскладка не важны."
)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    name = message.from_user.first_name or "друг"
    await message.answer(f"Здравствуйте, {name}! 👋\n\n{HELP_TEXT}")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT)


@dp.message(F.text)
async def handle_search(message: Message):
    query = message.text.strip()
    if not query:
        return

    try:
        df = load_table()
    except Exception:
        logging.exception("Не удалось загрузить таблицу")
        await message.answer(
            "Не получилось загрузить данные из таблицы. Попробуйте чуть позже."
        )
        return

    if df.empty:
        await message.answer("Таблица пуста или не загрузилась.")
        return

    results = search_products(query, df)

    if results.empty:
        await message.answer(f"По запросу «{query}» ничего не найдено.")
        return

    text = "\n\n".join(format_row(row) for _, row in results.iterrows())
    if len(results) == MAX_RESULTS:
        text += f"\n\nПоказаны первые {MAX_RESULTS} результатов. Уточните запрос, если нужно другое."

    elapsed = time.time() - _cache["ts"]
    text += f"\n\n🕓 Данные обновлены: {humanize_elapsed(elapsed)}"

    await message.answer(text, parse_mode="HTML")


async def health(request):
    return web.Response(text="OK")


async def on_startup(app: web.Application):
    if WEBHOOK_HOST:
        await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")
        logging.info("Webhook установлен: %s%s", WEBHOOK_HOST, WEBHOOK_PATH)


async def on_shutdown(app: web.Application):
    await bot.session.close()


def run_webhook():
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)


async def run_polling():
    logging.info("Бот запущен (polling)")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    if WEBHOOK_HOST:
        run_webhook()
    else:
        asyncio.run(run_polling())
