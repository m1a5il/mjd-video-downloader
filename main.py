
import os
import re
import glob
import tempfile
import threading
import requests
import yt_dlp

from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SUPPORTED = (
    "instagram.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
)

def send_message(chat_id, text):
    requests.post(
        f"{API}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=30
    )

def download_video(url, chat_id):
    try:
        send_message(chat_id, "⏳ جاري تجهيز الفيديو...")

        with tempfile.TemporaryDirectory() as folder:
            options = {
                "format": "best[ext=mp4][filesize<45M]/best[filesize<45M]/best",
                "outtmpl": folder + "/video.%(ext)s",
                "max_filesize": 45 * 1024 * 1024,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "merge_output_format": "mp4",
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])

            files = glob.glob(folder + "/video.*")

            if not files:
                raise Exception("لم يتم العثور على ملف الفيديو")

            video = max(files, key=os.path.getsize)

            if os.path.getsize(video) > 45 * 1024 * 1024:
                raise Exception("حجم الفيديو أكبر من الحد المسموح")

            with open(video, "rb") as file:
                response = requests.post(
                    f"{API}/sendVideo",
                    data={
                        "chat_id": chat_id,
                        "caption": "تم تجهيز الفيديو"
                    },
                    files={"video": file},
                    timeout=180
                )

            if not response.ok:
                raise Exception("تعذر إرسال الفيديو إلى تيليجرام")

    except Exception:
        send_message(
            chat_id,
            "❌ تعذر تنزيل هذا المقطع.\n"
            "قد يكون خاصاً أو محمياً أو غير متاح "
            "أو أكبر من الحد المسموح."
        )


def process_update(update):
    message = update.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()

    if not chat_id:
        return

    if text == "/start":
        send_message(
            chat_id,
            "أهلاً بك في MJD Video Downloader\n\n"
            "أرسل رابط فيديو عام من:\n"
            "Instagram\nTikTok\nYouTube\n\n"
            "وسأحاول تجهيزه لك."
        )
        return

    if text == "/help":
        send_message(
            chat_id,
            "أرسل رابط فيديو عام من المنصات المدعومة."
        )
        return

    if not text.startswith("http"):
        send_message(chat_id, "أرسل رابط فيديو صحيحاً.")
        return

    if not any(site in text.lower() for site in SUPPORTED):
        send_message(chat_id, "هذه المنصة غير مدعومة.")
        return

    threading.Thread(
        target=download_video,
        args=(text, chat_id),
        daemon=True
    ).start()


@app.route("/", methods=["GET"])
def home():
    return "MJD Downloader Bot is running!"


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if supplied != WEBHOOK_SECRET:
            return "Unauthorized", 401

    update = request.get_json(silent=True) or {}

    threading.Thread(
        target=process_update,
        args=(update,),
        daemon=True
    ).start()

    return "OK", 200


def setup_webhook():
    if BOT_TOKEN and APP_URL:
        response = requests.post(
            f"{API}/setWebhook",
            json={
                "url": APP_URL.rstrip("/") + "/webhook",
                "secret_token": WEBHOOK_SECRET
            },
            timeout=30
        )
        print("Webhook:", response.text)


setup_webhook()
