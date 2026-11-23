
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
from flask import Flask, request, jsonify


# =========================
# إعدادات البوت
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_FILE_SIZE = 49 * 1024 * 1024  # أقل من 50 MB

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)

executor = ThreadPoolExecutor(max_workers=2)
download_lock = threading.Lock()


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
        description = result.get("description", "Unknown Telegram error")
        raise RuntimeError(f"Telegram {method}: {description}")

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
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "instagr.am",
    "tiktok.com",
)


def extract_url(text):
    match = re.search(r"https?://\S+", text or "")

    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}،")

    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        if parsed.scheme != "https" and parsed.scheme != "http":
            return None

        allowed = any(
            host == domain or host.endswith("." + domain)
            for domain in ALLOWED_DOMAINS
        )

        return url if allowed else None

    except Exception:
        return None


# =========================
# تنزيل الفيديو
# =========================

def download_video(url, folder):
    options = {
        "outtmpl": os.path.join(folder, "%(id)s.%(ext)s"),

        # اختيار أفضل صيغة فيديو متاحة في ملف واحد
        "format": "best[ext=mp4]/best",

        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,

        "max_filesize": MAX_FILE_SIZE,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,

        "restrictfilenames": True,
        "overwrites": True,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("No video information returned")

        files = glob.glob(os.path.join(folder, "*"))

        video_files = [
            path for path in files
            if os.path.isfile(path)
            and not path.endswith((".part", ".ytdl"))
        ]

        if not video_files:
            raise RuntimeError("Downloaded file was not found")

        video_files.sort(
            key=lambda path: os.path.getsize(path),
            reverse=True
        )

        video_path = video_files[0]

        size = os.path.getsize(video_path)

        if size <= 0:
            raise RuntimeError("Downloaded file is empty")

        if size > MAX_FILE_SIZE:
            raise RuntimeError("Video exceeds Telegram upload limit")

        return video_path


# =========================
# إرسال الفيديو
# =========================

def upload_video(chat_id, video_path):
    extension = os.path.splitext(video_path)[1].lower()

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
            # إذا رفض تيليجرام إرسال الملف كفيديو،
            # نحاول إرساله كمستند.
            if method != "sendVideo":
                raise

            video_file.seek(0)

            telegram_request(
                "sendDocument",
                data={"chat_id": chat_id},
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

            logger.info("Starting download request")

            video_path = download_video(url, folder)

            logger.info(
                "Download completed. File size: %s bytes",
                os.path.getsize(video_path)
            )

            send_message(
                chat_id,
                "📤 اكتمل التنزيل، جاري إرسال الفيديو..."
            )

            upload_video(chat_id, video_path)

            logger.info("Video sent successfully")

    except Exception as error:

        # تسجيل السبب الحقيقي في Render Logs
        logger.exception("DOWNLOAD ERROR: %r", error)

        error_text = str(error).lower()

        if "max-filesize" in error_text or "file size" in error_text:
            message = "❌ حجم الفيديو أكبر من الحد المسموح (49 MB)."

        elif "private" in error_text or "login" in error_text:
            message = "❌ الفيديو خاص أو يتطلب تسجيل دخول."

        elif "unsupported url" in error_text:
            message = "❌ الرابط غير مدعوم."

        elif "telegram" in error_text:
            message = "❌ تم تنزيل الفيديو لكن تعذر إرساله إلى تيليجرام."

        else:
            message = (
                "❌ تعذر تنزيل الفيديو.\n"
                "سجل الخطأ موجود في Render Logs."
            )

        send_message(chat_id, message)

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
            "X-Telegram-Bot-Api-Secret-Token", ""
        )

        if received_secret != WEBHOOK_SECRET:
            return jsonify({"ok": False}), 403

    update = request.get_json(silent=True) or {}

    message = update.get("message")

    if not message:
        return jsonify({"ok": True})

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id:
        return jsonify({"ok": True})

    if text.startswith("/start"):
        send_message(
            chat_id,
            "أهلاً بك في MJD Video Downloader\n\n"
            "أرسل رابط فيديو عام من:\n"
            "Instagram\n"
            "TikTok\n"
            "YouTube\n\n"
            "وسأحاول تجهيزه لك."
        )

        return jsonify({"ok": True})

    if text.startswith("/help"):
        send_message(
            chat_id,
            "أرسل رابط فيديو عام من المواقع المدعومة.\n"
            "لا يمكن تنزيل المحتوى الخاص أو المحمي."
        )

        return jsonify({"ok": True})

    url = extract_url(text)

    if not url:
        send_message(
            chat_id,
            "❌ أرسل رابط فيديو صحيح من Instagram أو TikTok أو YouTube."
        )

        return jsonify({"ok": True})

    executor.submit(process_download, chat_id, url)

    return jsonify({"ok": True})


# =========================
# إعداد Webhook
# =========================

def setup_webhook():

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is missing")
        return

    if not APP_URL:
        logger.error("RENDER_EXTERNAL_URL is missing")
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

        logger.info("Webhook setup successful: %s", result)

    except Exception as error:
        logger.exception("WEBHOOK SETUP ERROR: %s", error)


setup_webhook()
