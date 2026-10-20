# -*- coding: utf-8 -*-
"""AI Food Super Pro Telegram bot: food-photo analysis, nutrition chat, recipes and history."""

import os
import io
import json
import asyncio
import sqlite3
import logging
import time
import hashlib
import hmac
from datetime import datetime, timezone, timedelta

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters, ApplicationHandlerStop
)
from google import genai
from google.genai import types
from PIL import Image

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ai_food_super_pro")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# Comma-separated one-time access codes; configure only in GitHub Secrets.
BOT_ACCESS_CODES = [x.strip() for x in os.getenv("BOT_ACCESS_CODES", "").split(",") if x.strip()]
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
DB_PATH = os.getenv("DB_PATH", "food_history.sqlite3")
MAX_IMAGE_BYTES = 15 * 1024 * 1024

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN لە GitHub Secrets دانەنراوە.")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY لە GitHub Secrets دانەنراوە.")
if not BOT_ACCESS_CODES:
    raise RuntimeError("BOT_ACCESS_CODES لە GitHub Secrets دانەنراوە؛ کۆدی یەکجارە بە کۆما جیا بکەوە.")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📸 شیکردنەوەی وێنە"), KeyboardButton("💬 گفتوگۆ لەگەڵ AI")],
        [KeyboardButton("📊 کورتەی ئەمڕۆ"), KeyboardButton("📚 مێژوو")],
        [KeyboardButton("🥗 پێشنیاری خواردن"), KeyboardButton("ℹ️ یارمەتی")]
    ],
    resize_keyboard=True,
    is_persistent=True
)

PORTION_KEYBOARD = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("بچووک", callback_data="portion:small"),
        InlineKeyboardButton("مامناوەند", callback_data="portion:medium"),
        InlineKeyboardButton("گەورە", callback_data="portion:large"),
    ],
    [
        InlineKeyboardButton("ڕۆنی زۆر", callback_data="oil:yes"),
        InlineKeyboardButton("ڕۆنی کەم/نییە", callback_data="oil:no"),
    ],
    [InlineKeyboardButton("نادیارە", callback_data="oil:unknown")]
])

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS food_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                food_name TEXT NOT NULL,
                calories_min INTEGER,
                calories_max INTEGER,
                protein_g REAL,
                carbs_g REAL,
                fat_g REAL,
                notes TEXT
            )
        """)
        # Safe migration for an older database created by earlier bot versions.
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(food_history)").fetchall()
        }
        for column, sql_type in (
            ("protein_g", "REAL"), ("carbs_g", "REAL"), ("fat_g", "REAL")
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE food_history ADD COLUMN {column} {sql_type}")
        conn.commit()
    init_access_db()

def access_db_connect():
    conn = sqlite3.connect("access_state.sqlite3", timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_access_db():
    with access_db_connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS authorized_users (
            user_hash TEXT PRIMARY KEY,
            authorized_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS used_access_codes (
            code_hash TEXT PRIMARY KEY,
            user_hash TEXT NOT NULL,
            used_at TEXT NOT NULL
        )""")
        conn.commit()

def code_hash(code):
    return hashlib.sha256(code.strip().encode("utf-8")).hexdigest()

def user_hash(user_id):
    # Store a salted hash, not the user's raw Telegram ID, in the auth-state cache.
    return hashlib.sha256((BOT_TOKEN + ":" + str(user_id)).encode("utf-8")).hexdigest()

def is_authorized(user_id):
    with access_db_connect() as conn:
        return conn.execute(
            "SELECT 1 FROM authorized_users WHERE user_hash=?", (user_hash(user_id),)
        ).fetchone() is not None

def redeem_access_code(user_id, submitted_code):
    """Redeem one owner-issued code once and bind it to one Telegram account."""
    submitted_hash = code_hash(submitted_code)
    allowed_hashes = {code_hash(code) for code in BOT_ACCESS_CODES}
    if not any(hmac.compare_digest(submitted_hash, allowed) for allowed in allowed_hashes):
        return False
    uid_hash = user_hash(user_id)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with access_db_connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM authorized_users WHERE user_hash=?", (uid_hash,)).fetchone():
            conn.commit()
            return True
        if conn.execute("SELECT 1 FROM used_access_codes WHERE code_hash=?", (submitted_hash,)).fetchone():
            conn.commit()
            return False
        conn.execute("INSERT INTO used_access_codes(code_hash, user_hash, used_at) VALUES (?, ?, ?)",
                     (submitted_hash, uid_hash, now))
        conn.execute("INSERT INTO authorized_users(user_hash, authorized_at) VALUES (?, ?)",
                     (uid_hash, now))
        conn.commit()
    return True

async def access_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for a single-use code before any bot feature can be used."""
    user = update.effective_user
    message = update.effective_message
    query = update.callback_query
    if user is None:
        raise ApplicationHandlerStop
    if is_authorized(user.id):
        return
    if query is not None:
        await query.answer("سەرەتا کۆدی دەستگەیشتن داخل بکە.", show_alert=True)
        raise ApplicationHandlerStop
    if message is None:
        raise ApplicationHandlerStop
    text = (message.text or "").strip()
    if text and not text.startswith("/") and redeem_access_code(user.id, text):
        await message.reply_text(
            "✅ کۆدەکە دروست بوو و دەستگەیشتنت چالاک کرا. ئەم کۆدە تەنها بۆ تۆ بەکارهاتووە؛ "
            "لە جارەکانی دواتردا پێویست ناکات دووبارە بیخەیتەوە.\n\n" +
            "بەخێربێیت بۆ AI Food Super Pro 🍽️",
            reply_markup=MAIN_KEYBOARD
        )
        raise ApplicationHandlerStop
    await message.reply_text(
        "🔐 بۆ بەکارهێنانی بۆتەکە، کۆدی تایبەتی دەستگەیشتن داخل بکە.\n\n"
        "هەر کۆدێک تەنها یەک کەس دەتوانێت بەکاریبهێنێت. ئەگەر کۆدت نییە، داوای لە بەڕێوەبەری بۆتەکە بکە."
    )
    raise ApplicationHandlerStop

def safe_int(value):
    try:
        return max(0, min(int(float(value)), 100000))
    except (TypeError, ValueError, OverflowError):
        return None

def safe_float(value):
    try:
        return max(0.0, min(float(value), 100000.0))
    except (TypeError, ValueError, OverflowError):
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

def retryable_error(exc):
    message = str(exc).lower()
    return any(marker in message for marker in (
        "503", "unavailable", "high demand", "429", "resource_exhausted",
        "500", "internal server error", "temporarily", "deadline exceeded",
        "service unavailable"
    ))

def generate_text(prompt, image_bytes=None, mime_type="image/jpeg", max_tokens=500):
    contents = [prompt]
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
    last_exc = None
    for attempt in range(3):
        try:
            config = types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_level="low")
            )
            response = ai_client.models.generate_content(
                model=MODEL_NAME, contents=contents, config=config
            )
            answer = (getattr(response, "text", None) or "").strip()
            if not answer:
                raise RuntimeError("Gemini returned an empty response")
            return answer
        except Exception as exc:
            last_exc = exc
            if not retryable_error(exc) or attempt == 2:
                raise
            delay = (1.5, 3.0)[attempt] if attempt < 2 else 3.0
            log.warning("Temporary Gemini error; retry %s/2 in %.1f seconds.", attempt + 1, delay)
            time.sleep(delay)
    raise last_exc

def optimize_image(image_bytes):
    """Reduce upload size and processing time while preserving useful food detail."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
            image.thumbnail((768, 768))
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=62, optimize=False)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, "image/jpeg"

def analyze_food(image_bytes, mime_type="image/jpeg", portion="نادیار", oil="نادیار"):
    prompt = f"""
وەک یاریدەدەری خواردن و تەندروستی، وێنەکە بە کورتی شیکاری بکە.
زمان: کوردیی سۆرانی. تەنها JSON بگەڕێنەوە، هیچ دەقی دەرەوەی JSON مەنووسە.
بڕی پێشبینیکراو: {portion}. ڕۆن/کەرە: {oil}.
JSON keys:
food_name (string), confidence (string: نزم/مامناوەند/بەرز),
portion_assumption (string), calories_min (integer), calories_max (integer),
protein_g (number), carbs_g (number), fat_g (number),
question (string; empty if no useful follow-up), notes (short string).
کالۆری و ماکرۆکان تەنها خەمڵاندنن. بڕی خواردن لە وێنەوە بە وردی نادیارە،
مەودای گونجاو بدە و دڵنیایی زۆر مەدە. ئەگەر خواردن نییە، بە ڕوونی بڵێ.
هەڵسەنگاندنی خواردن پزیشکی نییە؛ ڕێژیمی توند یان ڕێنمایی زیانبەخش مەدە.
"""
    small_image, mime_type = optimize_image(image_bytes)
    raw = generate_text(prompt, small_image, mime_type, max_tokens=350)
    result = parse_json(raw)
    # Normalize numeric fields so malformed model output doesn't break the UI.
    for key in ("calories_min", "calories_max"):
        result[key] = safe_int(result.get(key))
    for key in ("protein_g", "carbs_g", "fat_g"):
        result[key] = safe_float(result.get(key))
    return result

def save_result(user_id, result):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db_connect() as conn:
        conn.execute(
            """INSERT INTO food_history
               (user_id, created_at, food_name, calories_min, calories_max,
                protein_g, carbs_g, fat_g, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, now,
                str(result.get("food_name", "خواردنی نەناسراو"))[:200],
                safe_int(result.get("calories_min")),
                safe_int(result.get("calories_max")),
                safe_float(result.get("protein_g")),
                safe_float(result.get("carbs_g")),
                safe_float(result.get("fat_g")),
                str(result.get("notes", ""))[:1000]
            )
        )

def format_result(result):
    name = str(result.get("food_name") or "خواردنی نەناسراو")[:250]
    confidence = str(result.get("confidence") or "نادیار")[:50]
    low, high = safe_int(result.get("calories_min")), safe_int(result.get("calories_max"))
    calories = f"{min(low, high)}–{max(low, high)} کالۆری" if low is not None and high is not None else "نەتوانرا بە باوەڕپێکراوی خەمڵێندرێت"
    def macro(key):
        value = safe_float(result.get(key))
        return f"{value:g} گرام" if value is not None else "نادیار"
    text = (
        f"🍽️ **خواردن:** {name}\n"
        f"🔥 **کالۆری:** {calories}\n"
        f"🥩 **پرۆتین:** {macro('protein_g')}\n"
        f"🍞 **کاربۆهایدرەت:** {macro('carbs_g')}\n"
        f"🫒 **چەوری:** {macro('fat_g')}\n"
        f"🔎 **دڵنیایی:** {confidence}\n"
        f"📏 **بڕی پێشبینیکراو:** {str(result.get('portion_assumption') or 'نادیار')[:400]}\n"
        f"📝 **تێبینی:** {str(result.get('notes') or 'بڕ و پێکهاتەکان لە وێنەوە تەواو دیار نین.')[:500]}"
    )
    question = str(result.get("question") or "").strip()
    if question:
        text += f"\n\n❓ **پرسیاری AI:** {question[:500]}"
    text += "\n\n⚠️ ئەمانە خەمڵاندنن؛ قەبارە، ڕۆن و شێوازی لێنان ئەنجامەکە دەگۆڕن."
    return text

def explain_ai_error(exc):
    message = str(exc).lower()
    if any(x in message for x in ("503", "unavailable", "high demand", "500", "internal server error")):
        return "سێرڤەری AI بە کاتی قەرەباڵغە. تکایە چەند خولەکێک دواتر دووبارە هەوڵ بدە."
    if any(x in message for x in ("429", "resource_exhausted", "quota", "rate limit")):
        return "سنووری بەکارهێنان (quota) پڕ بووە یان داواکاری زۆرە. تکایە دواتر هەوڵ بدە و quota ـی Gemini API بپشکنە."
    if any(x in message for x in ("401", "403", "api key", "permission denied", "unauthorized")):
        return "کێشە لە API key یان مۆڵەتی Gemini هەیە. GitHub Settings → Secrets and variables → Actions پشکنە؛ کلیلی نهێنی مەینێرە."
    return "نەتوانرا داواکارییەکە تەواو بکرێت. تکایە دواتر هەوڵ بدە؛ ئەگەر دووبارە بووەوە کۆتا هێڵەکانی Actions بنێرە."

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "سڵاو! 👋 بەخێربێیت بۆ **AI Food Super Pro** 🍽️\n\n"
        "📸 وێنەی خواردن بنێرە بۆ خەمڵاندنی کالۆری و ماکرۆکان.\n"
        "💬 پرسیاری خواردن، پێکهاتە، شێوازی لێنان یان تەندروستی گشتی بکە.\n"
        "🥗 داوای پێشنیاری خواردن و بیرۆکەی ڕێچەتە بکە.\n"
        "📊 کورتەی ئەمڕۆ و 📚 مێژووی خواردنت ببینە.\n\n"
        "⚠️ ئەنجامەکان خەمڵاندنن، نە پێوانەی پزیشکی.",
        reply_markup=MAIN_KEYBOARD, parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🧭 **ڕێنمایی بۆ بەکارهێنان**\n\n"
        "• وێنەی خواردن بنێرە بۆ ناسینەوە و خەمڵاندنی کالۆری.\n"
        "• هەر پرسیارێکی پەیوەندیدار بە خواردن بنووسە بۆ گفتوگۆ لەگەڵ AI.\n"
        "• «🥗 پێشنیاری خواردن» بنووسە بۆ بیرۆکەی خواردن.\n"
        "• /history بۆ مێژوو؛ /today بۆ کورتەی ئەمڕۆ.\n"
        "• /clearhistory بۆ سڕینەوەی مێژووی خۆت.\n\n"
        "بۆ خەمڵاندنی باشتر، وێنەی ڕوون و لە ڕووناکی باش بگرە. قەبارەی پارچە و ڕۆن لە وێنەوە بە وردی نادیار دەبن.",
        parse_mode="Markdown"
    )

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT created_at, food_name, calories_min, calories_max
               FROM food_history WHERE user_id=? ORDER BY id DESC LIMIT 10""",
            (user_id,)
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("📚 هێشتا هیچ ئەنجامێکت تۆمار نەکردووە.")
        return
    lines = ["📚 دوایین ١٠ ئەنجام:"]
    for created, name, low, high in rows:
        kcal = f"{low}–{high}" if low is not None and high is not None else "نادیار"
        lines.append(f"• {created[:10]} — {name}: {kcal} کالۆری")
    await update.effective_message.reply_text("\n".join(lines))

async def today_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # UTC day boundary keeps behaviour consistent on GitHub-hosted runners.
    today = datetime.now(timezone.utc).date().isoformat()
    with db_connect() as conn:
        rows = conn.execute(
            """SELECT COUNT(*), SUM(calories_min), SUM(calories_max),
                      SUM(protein_g), SUM(carbs_g), SUM(fat_g)
               FROM food_history WHERE user_id=? AND substr(created_at,1,10)=?""",
            (user_id, today)
        ).fetchone()
    count, low, high, protein, carbs, fat = rows
    if not count:
        await update.effective_message.reply_text("📊 ئەمڕۆ هیچ خواردنێکت تۆمار نەکردووە. وێنەی خواردنێک بنێرە بۆ دەستپێکردن.")
        return
    kcal = f"{int(low or 0)}–{int(high or 0)}" if low is not None and high is not None else "نادیار"
    await update.effective_message.reply_text(
        f"📊 **کورتەی ئەمڕۆ (UTC)**\n\n"
        f"🍽️ ژمارەی خەمڵاندنەکان: {count}\n"
        f"🔥 کالۆریی کۆ: {kcal}\n"
        f"🥩 پرۆتین: {round(protein or 0)} گرام\n"
        f"🍞 کاربۆهایدرەت: {round(carbs or 0)} گرام\n"
        f"🫒 چەوری: {round(fat or 0)} گرام\n\n"
        "⚠️ ئەمە تەنها کۆی خواردنە تۆمارکراوەکانە، نە پێوانەی تەواوی ڕۆژ و نە ڕێنمایی پزیشکی.",
        parse_mode="Markdown"
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("بەڵێ، بیسڕەوە", callback_data="clear:yes"),
        InlineKeyboardButton("نەخێر", callback_data="clear:no")
    ]])
    await update.effective_message.reply_text(
        "دڵنیایت دەتەوێت هەموو مێژووی خواردنی خۆت بسڕیتەوە؟ ئەم کارە ناگەڕێتەوە.",
        reply_markup=keyboard
    )

async def handle_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "clear:no":
        await query.edit_message_text("باشە، مێژووەکەت نەسڕدرایەوە.")
        return
    with db_connect() as conn:
        conn.execute("DELETE FROM food_history WHERE user_id=?", (query.from_user.id,))
    await query.edit_message_text("✅ مێژووی خواردنەکەت سڕایەوە.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    photo = message.photo[-1]
    status = await message.reply_text("🔎 وێنەکە شیکاری دەکەم…")
    try:
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
        if len(image_bytes) > MAX_IMAGE_BYTES:
            await status.edit_text("وێنەکە زۆر گەورەیە؛ تکایە وێنەیەکی بچووکتر بنێرە.")
            return
        result = await asyncio.to_thread(analyze_food, image_bytes, "image/jpeg")
        context.user_data["last_food_image"] = optimize_image(image_bytes)[0]
        context.user_data["last_food_result"] = result
        save_result(update.effective_user.id, result)
        await status.edit_text(format_result(result), parse_mode="Markdown")
        await message.reply_text("دەتەوێت خەمڵاندنەکە وردتر بێت؟", reply_markup=PORTION_KEYBOARD)
    except Exception as exc:
        log.exception("Photo analysis failed")
        await status.edit_text("ببورە، نەمتوانی وێنەکە شیکاری بکەم.\n\n" + explain_ai_error(exc))

async def handle_refinement(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if "last_food_image" not in context.user_data:
        await query.message.reply_text("تکایە سەرەتا وێنەی خواردنێک بنێرە.")
        return
    portion = context.user_data.get("portion", "نادیار")
    oil = context.user_data.get("oil", "نادیار")
    if data.startswith("portion:"):
        portion = {"small": "بچووک", "medium": "مامناوەند", "large": "گەورە"}.get(data.split(":", 1)[1], "نادیار")
        context.user_data["portion"] = portion
        await query.message.reply_text(
            f"قەبارە: {portion}. ڕۆن چۆن بوو؟",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("ڕۆنی زۆر", callback_data="oil:yes"),
                InlineKeyboardButton("ڕۆنی کەم/نییە", callback_data="oil:no"),
                InlineKeyboardButton("نادیارە", callback_data="oil:unknown")
            ]])
        )
        return
    if data.startswith("oil:"):
        oil = {"yes": "ڕۆنی زۆر هەیە", "no": "ڕۆنی کەم یان نییە", "unknown": "نادیار"}.get(data.split(":", 1)[1], "نادیار")
        context.user_data["oil"] = oil
    status = await query.message.reply_text("🔄 خەمڵاندنەکە نوێ دەکەمەوە…")
    try:
        result = await asyncio.to_thread(
            analyze_food, context.user_data["last_food_image"], "image/jpeg",
            portion=portion, oil=oil
        )
        context.user_data["last_food_result"] = result
        save_result(query.from_user.id, result)
        await status.edit_text(format_result(result), parse_mode="Markdown")
    except Exception as exc:
        log.exception("Refinement failed")
        await status.edit_text("نوێکردنەوە سەرکەوتوو نەبوو.\n\n" + explain_ai_error(exc))

async def ask_food_ai(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    status = await update.effective_message.reply_text("💬 بیر لە وەڵامەکە دەکەمەوە…")
    prompt = (
        "تۆ یاریدەدەری زیرەکی خواردن و شێوازی ژیانی تەندروستییت. "
        "بە کوردیی سۆرانی، بە ڕوونی و بە کورتی وەڵام بدە. "
        "لە پرسیارەکانی خواردن، پێکهاتە، کالۆری، ماکرۆ، خواردنی هاوسەنگ، "
        "پاراستنی خواردن و ڕێچەتە یارمەتی بدە. ئەگەر ژمارەی کالۆری خەمڵاندنە، "
        "ئەمە ڕوون بکەوە. خۆت وەک پزیشک مەناسێنە، نەخۆشی دیاری مەکە، "
        "ڕێژیمی توند، گرسنگی، یان سنووردارکردنی زیانبەخش پێشنیار مەکە. "
        "ئەگەر پرسیارەکە پزیشکی یان هەستیارە، ڕێنمایی گشتی و پارێزراو بدە و "
        "پێشنیاری پەیوەندی بە پسپۆڕ بکە. وەڵامەکە زۆر درێژ مەکە.\n\n"
        f"پرسیاری بەکارهێنەر: {user_text[:2500]}"
    )
    try:
        answer = await asyncio.to_thread(generate_text, prompt, None, "image/jpeg", 500)
        await status.edit_text(answer[:4000])
    except Exception as exc:
        log.exception("Nutrition chat failed")
        await status.edit_text("ببورە، ئێستا نەتوانیم وەڵام بدەم.\n\n" + explain_ai_error(exc))

async def recipe_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ask_food_ai(
        update, context,
        "٣ پێشنیاری خواردنی هاوسەنگ و ئاسان بدە بە پێکهاتە و هەنگاوەکانی لێنان. "
        "پێکهاتەکانی ئاسایی و بەردەست بەکاربهێنە؛ کالۆرییەکان ئەگەر دەیڵێیت خەمڵاندن بن."
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    if text == "📚 مێژوو":
        await history(update, context)
    elif text == "📊 کورتەی ئەمڕۆ":
        await today_summary(update, context)
    elif text == "ℹ️ یارمەتی":
        await help_command(update, context)
    elif text == "📸 شیکردنەوەی وێنە":
        await update.effective_message.reply_text("تکایە ئێستا وێنەی خواردنێک بنێرە 📸")
    elif text == "💬 گفتوگۆ لەگەڵ AI":
        context.user_data["chat_mode"] = True
        await update.effective_message.reply_text("پرسیارەکەت بنووسە؛ من وەک یاریدەدەری خواردن وەڵامت دەدەم. بۆ گەڕانەوە بۆ مێنیو /start بنووسە.")
    elif text == "🥗 پێشنیاری خواردن":
        await recipe_suggestions(update, context)
    else:
        # ChatGPT-like free text: answer food/nutrition questions by default.
        await ask_food_ai(update, context, text)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled Telegram update error: %s", context.error)

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, access_gate), group=-1)
    app.add_handler(CallbackQueryHandler(access_gate), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("today", today_summary))
    app.add_handler(CommandHandler("clearhistory", clear_history))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_clear_callback, pattern=r"^clear:"))
    app.add_handler(CallbackQueryHandler(handle_refinement, pattern=r"^(portion|oil):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(error_handler)
    log.info("AI Food Super Pro starting with model %s and one-time access-code gate enabled", MODEL_NAME)
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
