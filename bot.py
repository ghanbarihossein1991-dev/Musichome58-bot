"""
ربات تلگرام مدیریت کانال موزیک — Music Home
------------------------------------------------
قابلیت‌ها:
  1) اصلاح متادیتای فایل‌های موزیک (Album/Comment = کانال، Title/Artist دست‌نخورده)
  2) واترمارک روی کاور آلبوم
  3) امضای خودکار کانال زیر کپشن هر پست
  4) صف پیشنهاد آهنگ از طرف اعضا (با تأیید ادمین)
  5) کشف و پست خودکار موزیک‌های Creative Commons بر اساس ژانر (Jamendo)
  6) دکمه‌ی اشتراک‌گذاری زیر هر پست
  7) سیستم دعوت/ریفرال با لینک اختصاصی (فقط شمارش، بدون جایزه)
  8) پیام خوش‌آمد برای اعضای جدید کانال (best-effort — فقط اگه کاربر قبلاً با ربات چت کرده باشه)
  9) حالت اینلاین برای جست‌وجوی آهنگ‌های پست‌شده در هر چتی (@your_bot اسم‌آهنگ)

نصب پیش‌نیازها:
    pip install -r requirements.txt

اجرا:
    export BOT_TOKEN="توکنی که از BotFather گرفتی"
    export ADMIN_CHAT_ID="آیدی عددی تلگرام خودت"
    export JAMENDO_CLIENT_ID="کلاینت آیدی رایگان از jamendo.com/developer"
    python bot.py

⚠️ برای حالت اینلاین، باید توی BotFather با دستور /setinline برای رباتت فعالش کنی.
"""

import os
import io
import json
import uuid
import logging
import tempfile
from urllib.parse import quote

import requests

from telegram import (
    Update,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultCachedAudio,
)
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

from PIL import Image, ImageDraw, ImageFont

from mutagen.id3 import ID3, TALB, COMM, APIC
from mutagen.mp4 import MP4, MP4Cover
from mutagen.flac import FLAC, Picture
from mutagen.oggvorbis import OggVorbis

# ----------------------------------------------------------------------
# تنظیمات — این بخش رو با اطلاعات خودت پر کن
# ----------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT-YOUR-TOKEN-HERE")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "8245114388")

CHANNEL_USERNAME = "@musichome58"
CHANNEL_LINK = "https://t.me/musichome58"
CHANNEL_CHAT_ID = CHANNEL_USERNAME

ADD_COVER_WATERMARK = True
WATERMARK_TEXT = CHANNEL_USERNAME
WATERMARK_LOGO_PATH = None

JAMENDO_CLIENT_ID = os.environ.get("JAMENDO_CLIENT_ID", "PUT-YOUR-JAMENDO-CLIENT-ID")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(BASE_DIR, "catalog.json")
REFERRALS_PATH = os.path.join(BASE_DIR, "referrals.json")

WELCOME_TEXT = (
    f"سلام! 🎶 خوش اومدی به {CHANNEL_USERNAME}\n\n"
    "هر روز موزیک‌های تازه اینجا می‌ذاریم. اگه آهنگی مدنظرته که ببینیش اینجا، "
    "می‌تونی همینجا برای ربات بفرستیش تا به تیم پیشنهاد بشه."
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

PENDING_SUGGESTIONS: dict[str, dict] = {}
PENDING_DISCOVERIES: dict[str, list] = {}


def is_admin(update: Update) -> bool:
    return str(update.effective_user.id) == str(ADMIN_CHAT_ID)


# ----------------------------------------------------------------------
# ذخیره‌سازی ساده (JSON) برای کاتالوگ آهنگ‌ها و دعوت‌ها
# ----------------------------------------------------------------------
def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_to_catalog(title: str, artist: str, file_id: str) -> None:
    catalog = _load_json(CATALOG_PATH, [])
    catalog.append({"title": title or "بدون‌نام", "artist": artist or "", "file_id": file_id})
    catalog = catalog[-500:]  # فقط ۵۰۰ تای آخر رو نگه دار
    _save_json(CATALOG_PATH, catalog)


def share_keyboard() -> InlineKeyboardMarkup:
    share_text = quote(f"این آهنگو ببین 🎧 {CHANNEL_USERNAME}")
    share_url = f"https://t.me/share/url?url={quote(CHANNEL_LINK)}&text={share_text}"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 اشتراک‌گذاری با دوستان", url=share_url)]]
    )


# ----------------------------------------------------------------------
# واترمارک روی عکس کاور
# ----------------------------------------------------------------------
def _load_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def watermark_image(image_bytes: bytes) -> bytes:
    base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))

    if WATERMARK_LOGO_PATH and os.path.exists(WATERMARK_LOGO_PATH):
        logo = Image.open(WATERMARK_LOGO_PATH).convert("RGBA")
        logo_w = base.width // 5
        logo_h = int(logo.height * (logo_w / logo.width))
        logo = logo.resize((logo_w, logo_h))
        margin = base.width // 40
        pos = (base.width - logo_w - margin, base.height - logo_h - margin)
        overlay.paste(logo, pos, logo)
    else:
        draw = ImageDraw.Draw(overlay)
        font_size = max(16, base.width // 18)
        font = _load_font(font_size)
        bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
        text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        margin = base.width // 40
        x = base.width - text_w - margin
        y = base.height - text_h - margin
        draw.text((x + 2, y + 2), WATERMARK_TEXT, font=font, fill=(0, 0, 0, 150))
        draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 220))

    combined = Image.alpha_composite(base, overlay).convert("RGB")
    out = io.BytesIO()
    combined.save(out, format="JPEG", quality=92)
    return out.getvalue()


def add_cover_watermark(file_path: str, ext: str) -> None:
    if not ADD_COVER_WATERMARK:
        return
    try:
        if ext == "mp3":
            tags = ID3(file_path)
            apic_keys = [k for k in tags.keys() if k.startswith("APIC")]
            if not apic_keys:
                return
            for key in apic_keys:
                frame = tags[key]
                frame.data = watermark_image(frame.data)
                frame.mime = "image/jpeg"
            tags.save(file_path)

        elif ext in ("m4a", "mp4"):
            tags = MP4(file_path)
            if "covr" not in tags or not tags["covr"]:
                return
            new_covers = [
                MP4Cover(watermark_image(bytes(c)), imageformat=MP4Cover.FORMAT_JPEG)
                for c in tags["covr"]
            ]
            tags["covr"] = new_covers
            tags.save()

        elif ext == "flac":
            audio = FLAC(file_path)
            if not audio.pictures:
                return
            new_pictures = []
            for pic in audio.pictures:
                new_data = watermark_image(pic.data)
                new_pic = Picture()
                new_pic.data = new_data
                new_pic.type = pic.type
                new_pic.mime = "image/jpeg"
                w, h = Image.open(io.BytesIO(new_data)).size
                new_pic.width, new_pic.height = w, h
                new_pic.depth = 24
                new_pictures.append(new_pic)
            audio.clear_pictures()
            for p in new_pictures:
                audio.add_picture(p)
            audio.save()

    except Exception as e:
        logger.warning("واترمارک روی کاور اعمال نشد (%s): %s", file_path, e)


def embed_cover_from_url(file_path: str, image_url: str) -> None:
    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        tags = ID3(file_path)
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=resp.content))
        tags.save(file_path)
    except Exception as e:
        logger.warning("جاسازی کاور از URL شکست خورد: %s", e)


# ----------------------------------------------------------------------
# تابع اصلی ویرایش متادیتا
# ----------------------------------------------------------------------
def edit_metadata(file_path: str) -> None:
    ext = file_path.lower().rsplit(".", 1)[-1]

    try:
        if ext == "mp3":
            try:
                tags = ID3(file_path)
            except Exception:
                tags = ID3()
            tags["TALB"] = TALB(encoding=3, text=CHANNEL_USERNAME)
            tags["COMM"] = COMM(encoding=3, lang="eng", desc="", text=CHANNEL_LINK)
            tags.save(file_path)

        elif ext in ("m4a", "mp4"):
            tags = MP4(file_path)
            tags["\xa9alb"] = [CHANNEL_USERNAME]
            tags["\xa9cmt"] = [CHANNEL_LINK]
            tags.save()

        elif ext == "flac":
            tags = FLAC(file_path)
            tags["album"] = [CHANNEL_USERNAME]
            tags["comment"] = [CHANNEL_LINK]
            tags.save()

        elif ext == "ogg":
            tags = OggVorbis(file_path)
            tags["album"] = [CHANNEL_USERNAME]
            tags["comment"] = [CHANNEL_LINK]
            tags.save()

        else:
            logger.warning("فرمت پشتیبانی‌نشده برای ویرایش متادیتا: %s", ext)

    except Exception as e:
        logger.exception("خطا در ویرایش متادیتا: %s", e)
        raise

    add_cover_watermark(file_path, ext)


# ----------------------------------------------------------------------
# حالت ۱: کاربر فایل رو توی پی‌وی می‌فرسته
# ----------------------------------------------------------------------
async def handle_private_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    audio = message.audio or message.document

    if audio is None:
        return

    if is_admin(update):
        file_name = audio.file_name or "track.mp3"
        await message.reply_text("در حال پردازش فایل... ⏳")

        with tempfile.TemporaryDirectory() as tmp_dir:
            local_path = os.path.join(tmp_dir, file_name)
            tg_file = await context.bot.get_file(audio.file_id)
            await tg_file.download_to_drive(local_path)

            try:
                edit_metadata(local_path)
            except Exception:
                await message.reply_text("متأسفم، توی ویرایش متادیتای این فایل مشکلی پیش اومد.")
                return

            await message.reply_audio(
                audio=open(local_path, "rb"),
                filename=file_name,
                caption=f"✅ اصلاح شد\n{CHANNEL_USERNAME}",
            )
        return

    suggestion_id = uuid.uuid4().hex[:10]
    PENDING_SUGGESTIONS[suggestion_id] = {
        "file_id": audio.file_id,
        "file_name": getattr(audio, "file_name", None) or "track.mp3",
        "from_user": update.effective_user.id,
        "from_name": update.effective_user.full_name,
    }

    await message.reply_text("پیشنهادت برای ادمین کانال ارسال شد. ممنون از همکاریت! 🎵")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأیید و پست", callback_data=f"sugg_ok:{suggestion_id}"),
                InlineKeyboardButton("❌ رد", callback_data=f"sugg_no:{suggestion_id}"),
            ]
        ]
    )
    await context.bot.send_audio(
        chat_id=ADMIN_CHAT_ID,
        audio=audio.file_id,
        caption=(
            f"🎧 پیشنهاد آهنگ جدید\n"
            f"از طرف: {update.effective_user.full_name}\n\n"
            f"تأیید کنی، خودکار پردازش و توی کانال پست می‌شه."
        ),
        reply_markup=keyboard,
    )


async def handle_suggestion_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("فقط ادمین می‌تونه تصمیم بگیره.", show_alert=True)
        return

    action, suggestion_id = query.data.split(":", 1)
    suggestion = PENDING_SUGGESTIONS.pop(suggestion_id, None)

    if suggestion is None:
        await query.edit_message_caption(caption="⚠️ این پیشنهاد قبلاً پردازش شده.")
        return

    if action == "sugg_no":
        await query.edit_message_caption(caption="❌ رد شد.")
        return

    await query.edit_message_caption(caption="⏳ در حال پردازش و پست‌کردن...")

    file_name = suggestion["file_name"]
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, file_name)
        tg_file = await context.bot.get_file(suggestion["file_id"])
        await tg_file.download_to_drive(local_path)

        try:
            edit_metadata(local_path)
        except Exception:
            await query.edit_message_caption(caption="⚠️ خطا توی پردازش فایل. پست نشد.")
            return

        sent = await context.bot.send_audio(
            chat_id=CHANNEL_CHAT_ID,
            audio=open(local_path, "rb"),
            filename=file_name,
            caption=CHANNEL_USERNAME,
            reply_markup=share_keyboard(),
        )
        if sent.audio:
            add_to_catalog(sent.audio.title or file_name, sent.audio.performer or "", sent.audio.file_id)

    await query.edit_message_caption(caption="✅ تأیید شد و توی کانال پست شد.")


# ----------------------------------------------------------------------
# حالت ۲: پست جدید توی خود کانال -> اصلاح خودکار
# ----------------------------------------------------------------------
async def handle_channel_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or message.audio is None:
        return

    audio = message.audio
    file_name = audio.file_name or "track.mp3"

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, file_name)
        tg_file = await context.bot.get_file(audio.file_id)
        await tg_file.download_to_drive(local_path)

        try:
            edit_metadata(local_path)
        except Exception:
            logger.warning("رد شدن از پست به دلیل خطای متادیتا: %s", file_name)
            return

        signature = CHANNEL_USERNAME
        new_caption = f"{message.caption}\n\n{signature}" if message.caption else signature

        edited_msg = await context.bot.edit_message_media(
            chat_id=message.chat_id,
            message_id=message.message_id,
            media=InputMediaAudio(
                media=open(local_path, "rb"),
                caption=new_caption,
                title=audio.title,
                performer=audio.performer,
            ),
        )

        try:
            await context.bot.edit_message_reply_markup(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reply_markup=share_keyboard(),
            )
        except BadRequest as e:
            logger.warning("اضافه‌کردن دکمه‌ی اشتراک‌گذاری شکست خورد: %s", e)

        if isinstance(edited_msg, object) and getattr(edited_msg, "audio", None):
            add_to_catalog(edited_msg.audio.title or file_name, edited_msg.audio.performer or "", edited_msg.audio.file_id)


# ----------------------------------------------------------------------
# حالت ۳: کشف موزیک‌های Creative Commons بر اساس ژانر (Jamendo)
# ----------------------------------------------------------------------
def search_jamendo(genre: str, limit: int = 5) -> list:
    url = "https://api.jamendo.com/v3.0/tracks/"
    params = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": limit,
        "tags": genre,
        "audioformat": "mp32",
        "boost": "popularity_month",
        "include": "musicinfo",
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json().get("results", [])


async def discover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    if not context.args:
        await update.message.reply_text("طوری استفاده کن: /discover lofi  یا  /discover deep-house")
        return

    genre = " ".join(context.args)
    await update.message.reply_text(f"در حال جستجوی آهنگ‌های «{genre}» با مجوز آزاد (Jamendo)... 🔎")

    try:
        tracks = search_jamendo(genre)
    except Exception as e:
        logger.exception("خطای جستجوی Jamendo: %s", e)
        await update.message.reply_text("جستجو با خطا مواجه شد. بعداً دوباره امتحان کن.")
        return

    if not tracks:
        await update.message.reply_text("چیزی با این ژانر پیدا نشد. یه اسم دیگه امتحان کن.")
        return

    token = uuid.uuid4().hex[:8]
    PENDING_DISCOVERIES[token] = tracks

    buttons = []
    for i, t in enumerate(tracks):
        label = f"{t['name']} — {t['artist_name']}"[:60]
        buttons.append([InlineKeyboardButton(label, callback_data=f"disc:{token}:{i}")])

    await update.message.reply_text(
        "این‌ها رو پیدا کردم؛ روی هر کدوم بزنی پردازش و توی کانال پست می‌شه:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_discovery_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.answer("فقط ادمین می‌تونه انتخاب کنه.", show_alert=True)
        return

    _, token, idx = query.data.split(":", 2)
    tracks = PENDING_DISCOVERIES.get(token)
    if tracks is None:
        await query.edit_message_text("⚠️ این جستجو منقضی شده، دوباره /discover بزن.")
        return

    track = tracks[int(idx)]
    await query.edit_message_text(f"⏳ در حال دانلود و پردازش «{track['name']}»...")

    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, f"{track['name']}.mp3")

        try:
            resp = requests.get(track["audio"], timeout=30)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(resp.content)
        except Exception as e:
            logger.exception("دانلود از Jamendo شکست خورد: %s", e)
            await query.edit_message_text("⚠️ دانلود فایل شکست خورد.")
            return

        if track.get("image"):
            embed_cover_from_url(local_path, track["image"])

        try:
            edit_metadata(local_path)
        except Exception:
            await query.edit_message_text("⚠️ خطا توی پردازش فایل.")
            return

        license_url = track.get("license_ccurl", "")
        caption = (
            f"{CHANNEL_USERNAME}\n\n"
            f"🎵 {track['name']} — {track['artist_name']}\n"
            f"مجوز: Creative Commons ({license_url})"
        )

        sent = await context.bot.send_audio(
            chat_id=CHANNEL_CHAT_ID,
            audio=open(local_path, "rb"),
            title=track["name"],
            performer=track["artist_name"],
            caption=caption,
            reply_markup=share_keyboard(),
        )
        if sent.audio:
            add_to_catalog(sent.audio.title or track["name"], sent.audio.performer or track["artist_name"], sent.audio.file_id)

    await query.edit_message_text(f"✅ «{track['name']}» توی کانال پست شد.")
    PENDING_DISCOVERIES.pop(token, None)


# ----------------------------------------------------------------------
# سیستم دعوت/ریفرال (فقط شمارش، بدون جایزه)
# ----------------------------------------------------------------------
async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    data = _load_json(REFERRALS_PATH, {"links": {}, "user_links": {}})

    existing_link = data["user_links"].get(user_id)
    if existing_link:
        await update.message.reply_text(
            f"لینک اختصاصی قبلی‌ت هنوز فعاله:\n{existing_link}"
        )
        return

    try:
        invite_link_obj = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_CHAT_ID,
            name=f"ref-{user_id}",
        )
    except Exception as e:
        logger.exception("ساخت لینک دعوت شکست خورد: %s", e)
        await update.message.reply_text(
            "نتونستم لینک بسازم. مطمئن شو ربات توی کانال ادمینه و دسترسی "
            "'دعوت کاربران با لینک' داره."
        )
        return

    link = invite_link_obj.invite_link
    data["links"][link] = {"user_id": user_id, "count": 0}
    data["user_links"][user_id] = link
    _save_json(REFERRALS_PATH, data)

    await update.message.reply_text(
        f"این لینک اختصاصی توئه 🎯\n{link}\n\n"
        "هرکی با این لینک جوین بشه، توی حساب دعوت‌های تو شمرده می‌شه. "
        "با /myinvites می‌تونی تعدادشو ببینی."
    )


async def myinvites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    data = _load_json(REFERRALS_PATH, {"links": {}, "user_links": {}})
    link = data["user_links"].get(user_id)

    if not link:
        await update.message.reply_text("هنوز لینک دعوت نساختی. با /invite یکی بساز.")
        return

    count = data["links"].get(link, {}).get("count", 0)
    await update.message.reply_text(f"تا الان {count} نفر با لینک تو جوین شدن. 🎉")


async def topinviters_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    data = _load_json(REFERRALS_PATH, {"links": {}, "user_links": {}})
    rows = sorted(data["links"].values(), key=lambda r: r["count"], reverse=True)[:10]

    if not rows:
        await update.message.reply_text("هنوز هیچ دعوتی ثبت نشده.")
        return

    lines = [f"{i+1}. آیدی {r['user_id']} — {r['count']} دعوت" for i, r in enumerate(rows)]
    await update.message.reply_text("🏆 برترین دعوت‌کننده‌ها:\n" + "\n".join(lines))


async def track_channel_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """وقتی یکی از طریق لینک اختصاصی جوین کانال می‌شه، شمارشش می‌کنیم و
    (در صورت امکان) یه پیام خوش‌آمد براش می‌فرستیم."""
    cmu = update.chat_member
    if cmu is None:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    just_joined = old_status in ("left", "kicked", "banned") and new_status == "member"
    if not just_joined:
        return

    invite_link = cmu.invite_link.invite_link if cmu.invite_link else None
    if invite_link:
        data = _load_json(REFERRALS_PATH, {"links": {}, "user_links": {}})
        if invite_link in data["links"]:
            data["links"][invite_link]["count"] += 1
            _save_json(REFERRALS_PATH, data)

    new_member_id = cmu.new_chat_member.user.id
    try:
        await context.bot.send_message(chat_id=new_member_id, text=WELCOME_TEXT)
    except Forbidden:
        # کاربر قبلاً با ربات چت نکرده؛ تلگرام اجازه‌ی پیام اول رو به ربات نمی‌ده.
        logger.info("امکان ارسال پیام خوش‌آمد نبود (کاربر با ربات چت نکرده): %s", new_member_id)
    except Exception as e:
        logger.warning("خطا در ارسال پیام خوش‌آمد: %s", e)


# ----------------------------------------------------------------------
# حالت اینلاین — جست‌وجوی آهنگ‌های آرشیو در هر چتی
# ----------------------------------------------------------------------
async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query_text = (update.inline_query.query or "").strip().lower()
    catalog = _load_json(CATALOG_PATH, [])

    if query_text:
        matches = [
            t for t in catalog
            if query_text in t["title"].lower() or query_text in t["artist"].lower()
        ][:15]
    else:
        matches = catalog[-15:][::-1]

    results = []
    for i, t in enumerate(matches):
        results.append(
            InlineQueryResultCachedAudio(
                id=str(i),
                audio_file_id=t["file_id"],
                caption=CHANNEL_USERNAME,
            )
        )

    await update.inline_query.answer(results, cache_time=30, is_personal=False)


# ----------------------------------------------------------------------
# راهنما
# ----------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        f"سلام! 👋 به {CHANNEL_USERNAME} خوش اومدی.\n\n"
        "برای پیشنهاد آهنگ: فقط فایل موزیک رو همینجا بفرست.\n"
        "برای گرفتن لینک دعوت اختصاصی: /invite\n"
        "برای دیدن تعداد دعوت‌هات: /myinvites\n"
        f"توی هر چتی هم می‌تونی با تایپ @{context.bot.username} و اسم آهنگ، آرشیو کانال رو سرچ کنی."
    )
    if is_admin(update):
        text += (
            "\n\nدستورات ادمین:\n"
            "/discover <ژانر> — جستجوی موزیک‌های Creative Commons\n"
            "/topinviters — لیست برترین دعوت‌کننده‌ها"
        )
    await update.message.reply_text(text)


# ----------------------------------------------------------------------
# راه‌اندازی ربات
# ----------------------------------------------------------------------
def main() -> None:
    if BOT_TOKEN == "PUT-YOUR-TOKEN-HERE":
        raise SystemExit("توکن ربات رو توی متغیر محیطی BOT_TOKEN یا داخل کد ست کن.")
    if ADMIN_CHAT_ID == "PUT-YOUR-NUMERIC-USER-ID":
        logger.warning("ADMIN_CHAT_ID ست نشده — صف پیشنهادها کار نمی‌کنه تا وقتی ستش کنی.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("discover", discover_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("myinvites", myinvites_command))
    app.add_handler(CommandHandler("topinviters", topinviters_command))

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & (filters.AUDIO | filters.Document.AUDIO),
            handle_private_audio,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.CHANNEL & filters.AUDIO,
            handle_channel_audio,
        )
    )

    app.add_handler(CallbackQueryHandler(handle_suggestion_decision, pattern=r"^sugg_"))
    app.add_handler(CallbackQueryHandler(handle_discovery_pick, pattern=r"^disc:"))

    app.add_handler(ChatMemberHandler(track_channel_join, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(InlineQueryHandler(inline_search))

    logger.info("ربات روشن شد...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
