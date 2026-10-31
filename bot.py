import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

import telebot
import yt_dlp
from flask import Flask, jsonify
from telebot import types


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("telegram_video_bot")

MAX_FILE_SIZE = 45 * 1024 * 1024
MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 180
MAX_DURATION_SECONDS = 20 * 60

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
    "x.com",
    "twitter.com",
    "reddit.com",
    "redd.it",
    "vimeo.com",
    "twitch.tv",
    "pinterest.com",
    "snapchat.com",
    "threads.net",
    "linkedin.com",
)

URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
active_downloads: set[int] = set()
active_downloads_lock = threading.Lock()

bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
required_channel = os.getenv("REQUIRED_CHANNEL", "").strip()
channel_join_url = os.getenv("CHANNEL_JOIN_URL", "").strip()

if not bot_token:
    raise RuntimeError(
        "أضف رمز البوت إلى متغير البيئة TELEGRAM_BOT_TOKEN."
    )
if not required_channel:
    raise RuntimeError(
        "حدد اسم القناة الذي يبدأ بالرمز @ أو معرّفها الرقمي في تيليغرام."
    )

if required_channel.startswith("-") and required_channel[1:].isdigit():
    required_channel = int(required_channel)
elif required_channel.startswith("@"):
    if not re.fullmatch(r"@[A-Za-z0-9_]{5,}", required_channel):
        raise RuntimeError(
            "يجب أن يكون اسم القناة بعد الرمز @ صالحًا، أو استخدم معرّفها الرقمي."
        )
else:
    raise RuntimeError(
        "استخدم اسم قناة يبدأ بالرمز @ أو معرّفًا رقميًا في تيليغرام."
    )

if not channel_join_url and isinstance(required_channel, str):
    channel_join_url = f"https://t.me/{required_channel[1:]}"

if channel_join_url:
    parsed_join_url = urlsplit(channel_join_url)
    if (
        parsed_join_url.scheme != "https"
        or parsed_join_url.hostname not in {"t.me", "telegram.me"}
        or parsed_join_url.username
        or parsed_join_url.password
    ):
        raise RuntimeError(
            "يجب أن يكون رابط القناة آمنًا ومن نطاق تيليغرام t.me أو telegram.me."
        )
if not channel_join_url:
    raise RuntimeError("أضف رابط دعوة تيليغرام للقناة الخاصة.")

bot = telebot.TeleBot(bot_token, threaded=True, num_threads=4)
app = Flask(__name__)


def check_subscription(user_id: int) -> bool:
    """Return whether a user can access the required channel."""
    try:
        member = bot.get_chat_member(required_channel, user_id)
        if member.status in ("creator", "administrator", "member"):
            return True
        return member.status == "restricted" and bool(
            getattr(member, "is_member", False)
        )
    except Exception as error:
        print(f"خطأ أثناء التحقق من الاشتراك: {error}", flush=True)
        return False


def verify_bot_is_channel_admin() -> None:
    """Fail early if Telegram cannot reliably verify channel membership."""
    try:
        bot_user = bot.get_me()
        membership = bot.get_chat_member(required_channel, bot_user.id)
    except Exception as error:
        logger.exception("تعذّر على البوت التحقق من صلاحياته في القناة.")
        raise RuntimeError(
            "أضف البوت مشرفًا في القناة وتحقق من إعداد القناة."
        ) from error

    if membership.status not in {"administrator", "creator"}:
        raise RuntimeError(
            "أضف البوت مشرفًا في القناة قبل تشغيل بوت التنزيل."
        )

    logger.info("تم التحقق من صلاحية البوت كمشرف في القناة المطلوبة.")


def subscription_keyboard() -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton(
            "📢 اضغط هنا للانضمام للقناة",
            url="https://t.me/Taqdeer_Mawkif",
        ),
        types.InlineKeyboardButton(
            "✅ تحقق من الاشتراك",
            callback_data="check_sub",
        ),
    )
    return keyboard


def prompt_to_join(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "عذراً، لاستخدام البوت يجب عليك الاشتراك في قناتنا أولاً.",
        reply_markup=subscription_keyboard(),
    )


def welcome_message() -> str:
    return (
        "مرحبًا بك. أرسل رابطًا عامًا واحدًا لفيديو من إحدى المنصات المدعومة، وسأرسله إليك هنا.\n\n"
        "المنصات المدعومة تشمل يوتيوب، إنستغرام، تيك توك، فيسبوك، إكس، ريديت، "
        "فيميو، تويتش، بينترست، سناب شات، ثريدز، ولينكد إن.\n\n"
        "يُرجى تنزيل أو مشاركة المحتوى الذي تملكه أو لديك إذن بحفظه فقط."
    )


def extract_video_url(text: str) -> str | None:
    matches = URL_PATTERN.findall(text or "")
    if len(matches) != 1:
        return None

    url = matches[0].rstrip(".,!?;:)]}")
    if len(url) > MAX_URL_LENGTH:
        return None

    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if port is not None and port not in {80, 443}:
        return None
    if not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in SUPPORTED_DOMAINS
    ):
        return None

    return url


def download_video(url: str, directory: str) -> tuple[Path, str]:
    options = {
        "format": "best[ext=mp4][height<=720]/best[height<=720]/best[ext=mp4]/best",
        "outtmpl": str(Path(directory) / "video.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 1,
        "max_filesize": MAX_FILE_SIZE,
        "restrictfilenames": True,
        "overwrites": True,
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
        if not info:
            raise ValueError("لم أتمكن من العثور على معلومات الفيديو.")
        if info.get("_type") in {"playlist", "multi_video"}:
            raise ValueError("لا يمكن تنزيل قوائم التشغيل. أرسل رابط فيديو واحدًا.")

        duration = info.get("duration")
        if duration and duration > MAX_DURATION_SECONDS:
            raise ValueError("مدة هذا الفيديو تتجاوز الحد المسموح به.")

        title = str(info.get("title") or "Video")[:MAX_TITLE_LENGTH]
        downloaded_info = downloader.process_ie_result(info, download=True)
        filename = downloader.prepare_filename(downloaded_info)

    video_path = Path(filename)
    if not video_path.is_file():
        candidates = [
            path
            for path in Path(directory).iterdir()
            if path.is_file()
            and not path.name.endswith((".part", ".ytdl", ".json"))
        ]
        if len(candidates) != 1:
            raise ValueError("تعذّر العثور على ملف الفيديو بعد التنزيل.")
        video_path = candidates[0]

    if video_path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("حجم الفيديو كبير جدًا لإرساله عبر تيليغرام.")

    return video_path, title


@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message) -> None:
    print(f"تم استلام الأمر: {message.text or '/start'}", flush=True)
    print("جارٍ التحقق من اشتراك المستخدم.", flush=True)
    if check_subscription(message.from_user.id):
        print("المستخدم مشترك؛ جارٍ إرسال رسالة الترحيب.", flush=True)
        bot.send_message(message.chat.id, welcome_message())
    else:
        print("المستخدم غير مشترك؛ جارٍ إرسال رسالة الاشتراك.", flush=True)
        prompt_to_join(message.chat.id)


@bot.message_handler(commands=["check"])
def handle_check(message: types.Message) -> None:
    try:
        if check_subscription(message.from_user.id):
            bot.send_message(
                message.chat.id,
                "تم التحقق من اشتراكك. أرسل رابط الفيديو عندما تكون مستعدًا.",
            )
        else:
            prompt_to_join(message.chat.id)
    except Exception:
        logger.exception("تعذّر التحقق من اشتراك المستخدم.")
        bot.send_message(
            message.chat.id,
            "تعذّر التحقق من اشتراكك. يُرجى المحاولة لاحقًا أو التواصل مع مسؤول البوت.",
        )


@bot.callback_query_handler(func=lambda query: query.data == "check_sub")
def handle_subscription_check(query: types.CallbackQuery) -> None:
    try:
        if check_subscription(query.from_user.id):
            bot.answer_callback_query(query.id, "تم التحقق من اشتراكك.")
            if query.message:
                bot.send_message(
                    query.message.chat.id,
                    welcome_message(),
                )
        else:
            bot.answer_callback_query(
                query.id,
                "لم يظهر اشتراكك بعد. انضم إلى القناة ثم حاول مرة أخرى.",
                show_alert=True,
            )
    except Exception:
        logger.exception("تعذّر التحقق من الاشتراك عبر الزر.")
        bot.answer_callback_query(
            query.id,
            "تعذّر التحقق من اشتراكك. يُرجى المحاولة لاحقًا.",
            show_alert=True,
        )


@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message) -> None:
    url = extract_video_url(message.text or "")
    if not url:
        bot.reply_to(
            message,
            "أرسل رابطًا عامًا واحدًا لفيديو من إحدى المنصات المدعومة: "
            "يوتيوب، إنستغرام، تيك توك، فيسبوك، إكس، ريديت، فيميو، تويتش، "
            "بينترست، سناب شات، ثريدز، أو لينكد إن.",
        )
        return

    try:
        if not check_subscription(message.from_user.id):
            prompt_to_join(message.chat.id)
            return
    except Exception:
        logger.exception("تعذّر التحقق من الاشتراك قبل التنزيل.")
        bot.reply_to(
            message,
            "تعذّر التحقق من اشتراكك، لذلك لم أتمكن من بدء التنزيل. يُرجى المحاولة لاحقًا.",
        )
        return

    user_id = message.from_user.id
    with active_downloads_lock:
        if user_id in active_downloads:
            bot.reply_to(
                message,
                "لا يزال تنزيلك السابق قيد التنفيذ. يُرجى الانتظار حتى يكتمل.",
            )
            return
        active_downloads.add(user_id)

    progress_message: types.Message | None = None
    try:
        progress_message = bot.reply_to(
            message,
            "جارٍ تنزيل الفيديو، يُرجى الانتظار...",
        )
        with tempfile.TemporaryDirectory(prefix="telegram-video-") as directory:
            video_path, _title = download_video(url, directory)
            bot.edit_message_text(
                "اكتمل التنزيل، جارٍ إرسال الفيديو...",
                chat_id=message.chat.id,
                message_id=progress_message.message_id,
            )
            bot.send_chat_action(message.chat.id, "upload_video")
            caption = "تم تنزيل الفيديو بنجاح."
            with video_path.open("rb") as video_file:
                if video_path.suffix.lower() == ".mp4":
                    bot.send_video(
                        message.chat.id,
                        video_file,
                        caption=caption,
                        supports_streaming=True,
                        timeout=120,
                    )
                else:
                    bot.send_document(
                        message.chat.id,
                        video_file,
                        caption=caption,
                        timeout=120,
                    )
    except yt_dlp.utils.DownloadError:
        logger.info("تعذّر تنزيل الفيديو.")
        bot.reply_to(
            message,
            "تعذّر تنزيل هذا الرابط. تأكد من أن الفيديو متاح للعامة وأن المنصة مدعومة، ثم حاول مرة أخرى.",
        )
    except ValueError as error:
        bot.reply_to(message, str(error))
    except Exception:
        logger.exception("تعذّرت معالجة الفيديو أو إرساله.")
        bot.reply_to(
            message,
            "تعذّر إرسال الفيديو. قد يكون غير متاح أو يتجاوز الحجم المسموح به في تيليغرام.",
        )
    finally:
        if progress_message:
            try:
                bot.delete_message(message.chat.id, progress_message.message_id)
            except Exception:
                logger.debug("تعذّر حذف رسالة التقدم.")
        with active_downloads_lock:
            active_downloads.discard(user_id)


@app.get("/health")
def health_check():
    return jsonify({"status": "ok", "service": "telegram-video-bot"}), 200


@app.get("/")
def home():
    return "Bot is running", 200, {"Content-Type": "text/plain; charset=utf-8"}


def main() -> None:
    if os.getenv("CLEAR_WEBHOOK_BEFORE_POLLING", "").strip().lower() == "true":
        if not bot.delete_webhook(drop_pending_updates=False):
            raise RuntimeError("تعذّر تأكيد إزالة خطاف الويب.")
        logger.info("تمت إزالة خطاف الويب الحالي مع الحفاظ على التحديثات المنتظرة.")

    verify_bot_is_channel_admin()

    port = int(os.getenv("PORT", "8080"))
    health_server = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=port,
            use_reloader=False,
            threaded=True,
        ),
        name="health-server",
        daemon=True,
    )
    health_server.start()
    logger.info("جارٍ تشغيل بوت الفيديو على تيليغرام.")
    bot.infinity_polling(
        skip_pending=False,
        timeout=20,
        long_polling_timeout=20,
        restart_on_change=False,
    )


if __name__ == "__main__":
    main()
