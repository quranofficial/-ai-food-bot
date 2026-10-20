import os
import io
import json
import sqlite3
import logging
import time
from datetime import datetime, timezone

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from google import genai
from google.genai import types

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ai_food_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
DB_PATH = os.getenv("DB_PATH", "food_history.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN لە GitHub Secrets دانەنراوە.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY لە GitHub Secrets دانەنراوە.")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📸 شیکردنەوەی وێنەی خواردن")],
        [KeyboardButton("📚 مێژووی خواردن"), KeyboardButton("ℹ️ ڕێنمایی")]
    ],
    resize_keyboard=True
)

PORTION_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("بچووک", callback_data="portion:small"),
        InlineKeyboardButton("مامناوەند", callback_data="portion:medium"),
        InlineKeyboardButton("گەورە", callback_data="portion:large"),
    ],
    [
        InlineKeyboardButton("ڕۆنی زۆر هەیە", callback_data="oil:yes"),
        InlineKeyboardButton("ڕۆنی کەم/نییە", callback_data="oil:no"),
    ],
    [InlineKeyboardButton("ناتوانم دیاری بکەم", callback_data="oil:unknown")]
])

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                food_name TEXT NOT NULL,
                calories_min INTEGER,
                calories_max INTEGER,
                notes TEXT
            )
        """)
        conn.commit()

def save_result(user_id, result):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO food_history
               (user_id, created_at, food_name, calories_min, calories_max, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id, now,
                str(result.get("food_name", "خواردنی نەناسراو"))[:200],
                safe_int(result.get("calories_min")),
                safe_int(result.get("calories_max")),
                str(result.get("notes", ""))[:1000]
            )
        )
        conn.commit()

def safe_int(value):
    try:
        n = int(value)
        return max(0, min(n, 100000))
    except (TypeError, ValueError):
        return None

def parse_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI وەڵامی JSON ـی دروست نەگەڕاندەوە.")
    return json.loads(text[start:end + 1])

def analyze_food(image_bytes, mime_type, portion="نادیار", oil="نادیار"):
    prompt = f"""
تۆ یاریدەدەری ناسینەوەی خواردنیت. وێنەکە تەنها بە شێوەی خەمڵاندن شیکەرەوە.
زمانی وەڵام: کوردیی سۆرانی.
بڕی خواردن: {portion}
ڕۆن/چەوری زیادکراو: {oil}

بە JSON ـی تەنها و بێ دەقی دەرەوە وەڵام بدە:
{{
 "food_name": "ناوی خواردن بە کوردی",
 "confidence": "بەرز/مامناوەند/نزم",
 "portion_assumption": "چۆن بڕەکەت خەمڵاندووە",
 "calories_min": 100,
 "calories_max": 200,
 "protein_g": "خەمڵاندنی گرام یان نادیار",
 "carbs_g": "خەمڵاندنی گرام یان نادیار",
 "fat_g": "خەمڵاندنی گرام یان نادیار",
 "question": "ئەگەر زانیارییەکی گرنگ نادیارە، پرسیارێکی کورت؛ ئەگەر نا، بەتاڵ",
 "notes": "هۆکارە سەرەکییەکانی نادڵنیایی"
}}

ڕێنمایی گرنگ:
- کالۆری و ماکرۆکان تەنها خەمڵاندنن؛ ژمارەکان بە وردیی پزیشکی مەناسێنە.
- ئەگەر جۆری خواردن ڕوون نییە، confidence نزم بنووسە و پرسیار بکە.
- لە وێنەوە بڕ و پێکهاتە بە دڵنیایی نازانرێت؛ ئەمە لە notes ڕوون بکەرەوە.
- بۆ وێنەی نەخواردن، بە ڕوونی بنووسە کە خواردن دیاری نەکراوە و ژمارەی کالۆری صفر دابنێ.
- هیچ ڕێنمایی کەمکردنەوەی کێش، سنووری کالۆریی ڕۆژانە، یان ڕێژیمی توند پێشنیار مەکە.
"""
    # Retry temporary Gemini overload / rate-limit errors with backoff.
    response = None
    for attempt in range(4):
        try:
            # Avoid temperature settings because newer Gemini models may not
            # support legacy sampling parameters.
            response = ai_client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    prompt,
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                ]
            )
            break
        except Exception as exc:
            error_text = str(exc).lower()
            retryable = any(marker in error_text for marker in (
                "503", "unavailable", "high demand", "429",
                "resource_exhausted", "500", "internal server error",
                "temporarily", "deadline exceeded"
            ))
            if not retryable or attempt == 3:
                raise
            delay = 2 ** (attempt + 1)  # 2, 4, then 8 seconds
            log.warning(
                "Temporary Gemini error; retry %s/3 in %s seconds: %s",
                attempt + 1, delay, str(exc)[:300]
            )
            time.sleep(delay)

    if response is None or not getattr(response, "text", None):
        raise RuntimeError("Gemini returned an empty response")
    return parse_json(response.text)


def explain_ai_error(exc):
    """Return a helpful Sorani message without exposing API credentials."""
    message = str(exc).lower()
    if any(x in message for x in ("503", "unavailable", "high demand", "500", "internal server error")):
        return (
            "سێرڤەری Gemini بە کاتی کێشەی هەیە، تەنانەت دوای چەند هەوڵێک. "
            "تکایە چەند خولەکێک چاوەڕێ بکە و دووبارە وێنەکە بنێرە."
        )
    if any(x in message for x in ("429", "resource_exhausted", "quota", "rate limit")):
        return (
            "سنووری بەکارهێنان (quota) یان خێرایی داواکارییەکان پڕ بووە. "
            "تکایە دواتر هەوڵ بدە و quota ـی Gemini API بپشکنە."
        )
    if any(x in message for x in ("401", "403", "api key", "permission denied", "unauthorized")):
        return (
            "کێشە لە API key یان مۆڵەتی Gemini هەیە. "
            "لە GitHub Settings → Secrets and variables → Actions، "
            "GEMINI_API_KEY بپشکنە؛ کلیلی نهێنی لە چاتدا مەینێرە."
        )
    return (
        "نەتوانرا وێنەکە شیکاری بکرێت. تکایە دواتر هەوڵ بدە. "
        "ئەگەر دووبارە بووەوە، کۆتا هەڵەی Actions بنێرە."
    )

def format_result(result):
    name = str(result.get("food_name", "خواردنی نەناسراو"))
    confidence = str(result.get("confidence", "نادیار"))
    pmin = safe_int(result.get("calories_min"))
    pmax = safe_int(result.get("calories_max"))
    if pmin is None or pmax is None:
        calories = "نەتوانرا بە شێوەیەکی باوەڕپێکراو خەمڵێندرێت"
    else:
        pmin, pmax = min(pmin, pmax), max(pmin, pmax)
        calories = f"{pmin}–{pmax} کالۆری"
    question = str(result.get("question", "")).strip()
    text = (
        f"🍽️ خواردنی پێشبینیکراو: {name}\n"
        f"🔥 کالۆریی خەمڵێنراو: {calories}\n"
        f"🥩 پرۆتین: {result.get('protein_g', 'نادیار')}\n"
        f"🍞 کاربۆهایدرەت: {result.get('carbs_g', 'نادیار')}\n"
        f"🫒 چەوری: {result.get('fat_g', 'نادیار')}\n"
        f"🔎 دڵنیایی ناسینەوە: {confidence}\n"
        f"📏 بنەمای خەمڵاندن: {result.get('portion_assumption', 'نادیار')}\n"
        f"📝 تێبینی: {result.get('notes', 'بڕ و پێکهاتەکان لە وێنەوە تەواو دیار نین.')}"
    )
    if question:
        text += f"\n\n❓ پرسیاری AI: {question}"
    text += "\n\n⚠️ ئەمانە خەمڵاندنن، نە پێوانەی ورد. ڕۆن، قەبارە و شێوازی لێنان دەتوانن ئەنجامەکە بگۆڕن."
    return text

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سڵاو! 👋 من بۆتی AI Food ـم.\n\n"
        "📸 وێنەی خواردنێک بنێرە تا جۆری خواردن و مەودای کالۆرییەکەی خەمڵێنم.\n"
        "ئەگەر پێویست بوو، لە بڕی خواردن و ڕۆن پرسیارت لێ دەکەم.\n\n"
        "تێبینی: ئەنجامەکان خەمڵاندنن، نە پێوانەی پزیشکی.",
        reply_markup=MAIN_KEYBOARD
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 چۆن بەکاری بهێنیت؟\n"
        "١. وێنەی خواردنێکی ڕوون بنێرە.\n"
        "٢. ئەگەر داوای بڕ یان ڕۆن کرا، وەڵام بدە.\n"
        "٣. ئەنجامی خەمڵاندنی کالۆری و پێکهاتەکان بخوێنەوە.\n\n"
        "بۆ باشترکردنی خەمڵاندن، وێنەکە لە سەرەوە و لە ڕووناکی باش بگرە."
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """SELECT created_at, food_name, calories_min, calories_max
               FROM food_history WHERE user_id = ?
               ORDER BY id DESC LIMIT 10""",
            (user_id,)
        ).fetchall()
    if not rows:
        await update.message.reply_text("هێشتا هیچ ئەنجامێکت تۆمار نەکردووە.")
        return
    lines = ["📚 دوایین ئەنجامەکانت:"]
    for created, name, low, high in rows:
        day = created[:10]
        kcal = f"{low}–{high}" if low is not None and high is not None else "نادیار"
        lines.append(f"• {day} — {name}: {kcal} کالۆری")
    await update.message.reply_text("\n".join(lines))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    photo = message.photo[-1]
    await message.reply_text("🔎 وێنەکە شیکاری دەکەم، تکایە چاوەڕێ بکە...")
    try:
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        # Telegram getFile download currently has a size limit; reject very large photos.
        if len(image_bytes) > 15 * 1024 * 1024:
            await message.reply_text("وێنەکە زۆر گەورەیە. تکایە وێنەیەکی بچووکتر بنێرە.")
            return
        result = analyze_food(image_bytes, "image/jpeg")
        context.user_data["last_food_image"] = image_bytes
        context.user_data["last_food_result"] = result
        context.user_data["last_food_mime"] = "image/jpeg"
        save_result(update.effective_user.id, result)
        await message.reply_text(format_result(result))
        await message.reply_text(
            "دەتەوێت خەمڵاندنەکە وردتر بێت؟ قەبارە و ڕۆن دیاری بکە:",
            reply_markup=PORTION_KEYBOARD
        )
    except Exception as exc:
        log.exception("Photo analysis failed")
        await message.reply_text("ببورە، شیکردنەوە سەرکەوتوو نەبوو.\n\n" + explain_ai_error(exc))

async def refine_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if "last_food_image" not in context.user_data:
        await query.message.reply_text("تکایە سەرەتا وێنەی خواردنێک بنێرە.")
        return

    portion = context.user_data.get("portion", "نادیار")
    oil = context.user_data.get("oil", "نادیار")
    if data.startswith("portion:"):
        portion = {
            "small": "بچووک",
            "medium": "مامناوەند",
            "large": "گەورە"
        }.get(data.split(":", 1)[1], "نادیار")
        context.user_data["portion"] = portion
        await query.message.reply_text(
            f"قەبارە هەڵبژێردرا: {portion}. ئێستا ڕۆن دیاری بکە:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ڕۆنی زۆر هەیە", callback_data="oil:yes"),
                 InlineKeyboardButton("ڕۆنی کەم/نییە", callback_data="oil:no")],
                [InlineKeyboardButton("ناتوانم دیاری بکەم", callback_data="oil:unknown")]
            ])
        )
        return
    if data.startswith("oil:"):
        oil = {"yes": "ڕۆنی زۆر هەیە", "no": "ڕۆنی کەم یان نییە",
               "unknown": "نادیار"}.get(data.split(":", 1)[1], "نادیار")
        context.user_data["oil"] = oil

    await query.message.reply_text("🔄 بە زانیاریی نوێ خەمڵاندنەکە نوێ دەکەمەوە...")
    try:
        result = analyze_food(
            context.user_data["last_food_image"],
            context.user_data.get("last_food_mime", "image/jpeg"),
            portion=portion,
            oil=oil
        )
        context.user_data["last_food_result"] = result
        save_result(update.effective_user.id, result)
        await query.message.reply_text(format_result(result))
    except Exception as exc:
        log.exception("Refinement failed")
        await query.message.reply_text("نوێکردنەوە سەرکەوتوو نەبوو.\n\n" + explain_ai_error(exc))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text == "📚 مێژووی خواردن":
        await history(update, context)
    elif text in ("ℹ️ ڕێنمایی", "/help"):
        await help_command(update, context)
    elif text == "📸 شیکردنەوەی وێنەی خواردن":
        await update.message.reply_text("تکایە ئێستا وێنەی خواردنێک بنێرە 📸")
    else:
        await update.message.reply_text(
            "تکایە وێنەی خواردنێک بنێرە، یان لە دوگمەکانی خوارەوە بەکاربهێنە.",
            reply_markup=MAIN_KEYBOARD
        )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error: %s", context.error)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(refine_result, pattern=r"^(portion|oil):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("AI Food Bot starting with model %s", MODEL_NAME)
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
