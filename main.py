
import os
import re
import glob
import logging
import tempfile
import threading

from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor

import requests
import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from flask import Flask, request, jsonify


# =========================
# إعدادات البوت
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_FILE_SIZE = 49 * 1024 * 1024

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=2)
download_lock = threading.Lock()


# =========================
# رسائل البوت
# =========================

WELCOME_MESSAGE = (
    "مرحباً بك في MJD Video Downloader\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "حمّل مقاطعك بسهولة وسرعة!\n\n"
    "المنصات المدعومة:\n"
    "📱 TikTok\n"
    "📸 Instagram\n\n"
    "طريقة الاستخدام:\n"
    "1. انسخ رابط الفيديو.\n"
    "2. أرسله هنا.\n"
    "3. انتظر وصول الفيديو إليك.\n\n"
    "للمساعدة أرسل /help\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MJD | Video Downloader"
)

HELP_MESSAGE = (
    "📖 طريقة استخدام MJD Video Downloader\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "1️⃣ افتح الفيديو في TikTok أو Instagram.\n"
    "2️⃣ اضغط على مشاركة ثم نسخ الرابط.\n"
    "3️⃣ أرسل الرابط إلى البوت.\n"
    "4️⃣ انتظر وصول الفيديو إليك.\n\n"
    "✅ المنصات المدعومة:\n"
    "• TikTok\n"
    "• Instagram\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MJD | Video Downloader"
)


# =========================
# التواصل مع تيليجرام
# =========================

def telegram_request(method, data=None, files=None, timeout=60):

    response = requests.post(
        f"{API_URL}/{method}",
        data=data,
        files=files,
        timeout=timeout
    )

    try:
        result = response.json()
    except Exception:
        raise RuntimeError(
            f"Telegram returned HTTP {response.status_code}"
        )

    if not response.ok or not result.get("ok"):
        description = result.get(
            "description",
            "Unknown Telegram error"
        )
        raise RuntimeError(
            f"Telegram {method}: {description}"
        )

    return result


def send_message(chat_id, text):

    try:
        telegram_request(
            "sendMessage",
            data={
                "chat_id": chat_id,
                "text": text
            }
        )

    except Exception as error:
        logger.error("SEND MESSAGE ERROR: %s", error)


def send_action(chat_id):

    try:
        telegram_request(
            "sendChatAction",
            data={
                "chat_id": chat_id,
                "action": "upload_video"
            }
        )

    except Exception:
        pass


# =========================
# التحقق من الروابط
# =========================

ALLOWED_DOMAINS = (
    "instagram.com",
    "instagr.am",
    "tiktok.com",
)

YOUTUBE_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
)


def get_url(text):

    match = re.search(r"https?://\S+", text or "")

    if not match:
        return None

    return match.group(0).rstrip(".,!?)]}،")


def get_domain(url):

    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("https", "http"):
            return None

        return (parsed.hostname or "").lower()

    except Exception:
        return None


def domain_matches(host, domains):

    return any(
        host == domain or host.endswith("." + domain)
        for domain in domains
    )


def is_youtube_url(url):

    host = get_domain(url)

    return bool(
        host and domain_matches(host, YOUTUBE_DOMAINS)
    )


def extract_url(text):

    url = get_url(text)

    if not url:
        return None

    host = get_domain(url)

    if not host:
        return None

    if domain_matches(host, ALLOWED_DOMAINS):
        return url

    return None


# =========================
# تنزيل الفيديو
# =========================

def download_video(url, folder):

    options = {
        "outtmpl": os.path.join(
            folder,
            "%(id)s.%(ext)s"
        ),

        "format": "best[ext=mp4]/best",

        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "max_filesize": MAX_FILE_SIZE,

        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,

        "restrictfilenames": True,
        "overwrites": True,
    }

    # استخدام نوع ImpersonateTarget الصحيح
    options["impersonate"] = ImpersonateTarget.from_str("chrome")

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

        if not info:
            raise RuntimeError(
                "No video information returned"
            )

        files = glob.glob(
            os.path.join(folder, "*")
        )

        video_files = [
            path for path in files
            if os.path.isfile(path)
            and not path.endswith(
                (".part", ".ytdl")
            )
        ]

        if not video_files:
            raise RuntimeError(
                "Downloaded file was not found"
            )

        video_files.sort(
            key=lambda path: os.path.getsize(path),
            reverse=True
        )

        video_path = video_files[0]

        size = os.path.getsize(video_path)

        if size <= 0:
            raise RuntimeError(
                "Downloaded file is empty"
            )

        if size > MAX_FILE_SIZE:
            raise RuntimeError(
                "Video exceeds Telegram upload limit"
            )

        return video_path


# =========================
# إرسال الفيديو
# =========================

def upload_video(chat_id, video_path):

    extension = os.path.splitext(
        video_path
    )[1].lower()

    if extension == ".mp4":
        method = "sendVideo"
        field = "video"
    else:
        method = "sendDocument"
        field = "document"

    with open(video_path, "rb") as video_file:

        try:

            telegram_request(
                method,

                data={
                    "chat_id": chat_id,
                    "supports_streaming": "true"
                } if method == "sendVideo" else {
                    "chat_id": chat_id
                },

                files={
                    field: (
                        os.path.basename(video_path),
                        video_file
                    )
                },

                timeout=180
            )

        except Exception:

            if method != "sendVideo":
                raise

            video_file.seek(0)

            telegram_request(
                "sendDocument",

                data={
                    "chat_id": chat_id
                },

                files={
                    "document": (
                        os.path.basename(video_path),
                        video_file
                    )
                },

                timeout=180
            )


# =========================
# معالجة طلب التنزيل
# =========================

def process_download(chat_id, url):

    if not download_lock.acquire(blocking=False):

        send_message(
            chat_id,
            "⏳ يوجد طلب تنزيل آخر قيد التنفيذ. حاول بعد قليل."
        )

        return

    try:

        send_message(
            chat_id,
            "⏳ جاري تجهيز الفيديو وتنزيله..."
        )

        send_action(chat_id)

        with tempfile.TemporaryDirectory() as folder:

            logger.info(
                "Starting download request"
            )

            video_path = download_video(
                url,
                folder
            )

            logger.info(
                "Download completed. File size: %s bytes",
                os.path.getsize(video_path)
            )

            send_message(
                chat_id,
                "📤 اكتمل التنزيل، جاري إرسال الفيديو..."
            )

            upload_video(
                chat_id,
                video_path
            )

            logger.info(
                "Video sent successfully"
            )

    except Exception as error:

        logger.exception(
            "DOWNLOAD ERROR: %r",
            error
        )

        error_text = str(error).lower()

        if (
            "max-filesize" in error_text
            or "file size" in error_text
        ):

            message = (
                "❌ حجم الفيديو أكبر من الحد المسموح (49 MB)."
            )

        elif (
            "private" in error_text
            or "login" in error_text
        ):

            message = (
                "❌ الفيديو خاص أو يتطلب تسجيل دخول."
            )

        elif "unsupported url" in error_text:

            message = "❌ الرابط غير مدعوم."

        elif "telegram" in error_text:

            message = (
                "❌ تم تنزيل الفيديو لكن تعذر إرساله إلى تيليجرام."
            )

        else:

            message = (
                "❌ تعذر تنزيل الفيديو.\n"
                "تأكد من أن الرابط متاح للعامة وحاول مرة أخرى."
            )

        send_message(
            chat_id,
            message
        )

    finally:

        download_lock.release()


# =========================
# استقبال رسائل تيليجرام
# =========================

@app.route("/", methods=["GET"])
def home():

    return "MJD Video Downloader is running."


@app.route("/webhook", methods=["POST"])
def webhook():

    if WEBHOOK_SECRET:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if received_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    message = update.get("message")

    if not message:
        return jsonify({"ok": True})

    chat_id = message.get(
        "chat", {}
    ).get("id")

    text = message.get(
        "text", ""
    ).strip()

    if not chat_id:
        return jsonify({"ok": True})

    command = text.split(maxsplit=1)[0].split("@")[0] if text else ""

    # أمر البداية
    if command == "/start":

        send_message(
            chat_id,
            WELCOME_MESSAGE
        )

        return jsonify({"ok": True})

    # أمر المساعدة
    if command == "/help":

        send_message(
            chat_id,
            HELP_MESSAGE
        )

        return jsonify({"ok": True})

    url = get_url(text)

    # منع روابط يوتيوب
    if url and is_youtube_url(url):

        send_message(
            chat_id,
            "❌ عذراً، YouTube غير مدعوم حالياً.\n\n"
            "المنصات المتاحة حالياً:\n"
            "📱 TikTok\n"
            "📸 Instagram"
        )

        return jsonify({"ok": True})

    url = extract_url(text)

    if not url:

        send_message(
            chat_id,
            "❌ الرابط غير مدعوم.\n\n"
            "أرسل رابط فيديو من:\n"
            "📱 TikTok\n"
            "📸 Instagram\n\n"
            "للمساعدة أرسل /help"
        )

        return jsonify({"ok": True})

    executor.submit(
        process_download,
        chat_id,
        url
    )

    return jsonify({"ok": True})


# =========================
# إعداد Webhook
# =========================

def setup_webhook():

    if not BOT_TOKEN:

        logger.error(
            "BOT_TOKEN is missing"
        )

        return

    if not APP_URL:

        logger.error(
            "RENDER_EXTERNAL_URL is missing"
        )

        return

    webhook_url = APP_URL + "/webhook"

    data = {
        "url": webhook_url,
        "allowed_updates": '["message"]'
    }

    if WEBHOOK_SECRET:
        data["secret_token"] = WEBHOOK_SECRET

    try:

        result = telegram_request(
            "setWebhook",
            data=data
        )

        logger.info(
            "Webhook setup successful: %s",
            result
        )

    except Exception as error:

        logger.exception(
            "WEBHOOK SETUP ERROR: %s",
            error
        )


setup_webhook()
