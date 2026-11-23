
import os
import re
import glob
import json
import sys
import logging
import tempfile
import threading
import subprocess
import queue
import sqlite3

from contextlib import closing
from urllib.parse import urlparse

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
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

DB_PATH = os.getenv("STATS_DB_PATH", "stats.db")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_PHOTO_SIZE = 49 * 1024 * 1024
MAX_QUEUE_SIZE = 30

# عدد التنزيلات المتزامنة
MAX_CONCURRENT_DOWNLOADS = 3

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

logger = logging.getLogger(__name__)

download_queue = queue.Queue()
queue_lock = threading.Lock()

active_download_count = 0
active_download_lock = threading.Lock()


def get_active_download_count():
    with active_download_lock:
        return active_download_count


# =========================
# قاعدة بيانات الإحصائيات
# =========================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_database():
    with closing(get_db()) as conn:

        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                customer_name TEXT,
                username TEXT,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                customer_name TEXT,
                username TEXT,
                platform TEXT,
                status TEXT DEFAULT 'queued',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                completed_at TEXT
            )
        """)

        conn.commit()

    logger.info("Statistics database initialized")


def save_user(chat_id, customer_name, username):

    with closing(get_db()) as conn:

        conn.execute("""
            INSERT INTO users (
                chat_id,
                customer_name,
                username
            )
            VALUES (?, ?, ?)

            ON CONFLICT(chat_id) DO UPDATE SET
                customer_name = excluded.customer_name,
                username = excluded.username,
                last_seen = CURRENT_TIMESTAMP
        """, (
            str(chat_id),
            customer_name,
            username
        ))

        conn.commit()


def create_download_record(
    chat_id,
    customer_name,
    username,
    platform
):

    with closing(get_db()) as conn:

        cursor = conn.execute("""
            INSERT INTO downloads (
                chat_id,
                customer_name,
                username,
                platform,
                status
            )
            VALUES (?, ?, ?, ?, 'queued')
        """, (
            str(chat_id),
            customer_name,
            username,
            platform
        ))

        conn.commit()
        return cursor.lastrowid


def update_download_status(download_id, status):

    if not download_id:
        return

    with closing(get_db()) as conn:

        if status in ("success", "failed"):

            conn.execute("""
                UPDATE downloads
                SET status = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, download_id))

        else:

            conn.execute("""
                UPDATE downloads
                SET status = ?
                WHERE id = ?
            """, (status, download_id))

        conn.commit()


def get_statistics():

    with closing(get_db()) as conn:

        users_count = conn.execute("""
            SELECT COUNT(*) AS total
            FROM users
        """).fetchone()["total"]

        downloads = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status IN ('queued', 'running') THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN platform = 'TikTok' THEN 1 ELSE 0 END) AS tiktok,
                SUM(CASE WHEN platform = 'Instagram' THEN 1 ELSE 0 END) AS instagram
            FROM downloads
        """).fetchone()

        today = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM downloads
            WHERE date(created_at) = date('now')
        """).fetchone()

        recent = conn.execute("""
            SELECT
                customer_name,
                username,
                platform,
                status,
                created_at
            FROM downloads
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

    return {
        "users": users_count,
        "total": downloads["total"] or 0,
        "success": downloads["success"] or 0,
        "failed": downloads["failed"] or 0,
        "pending": downloads["pending"] or 0,
        "tiktok": downloads["tiktok"] or 0,
        "instagram": downloads["instagram"] or 0,
        "today_total": today["total"] or 0,
        "today_success": today["success"] or 0,
        "today_failed": today["failed"] or 0,
        "recent": recent
    }


def build_statistics_message():

    stats = get_statistics()

    waiting = download_queue.qsize()
    running = get_active_download_count()

    message = (
        "📊 إحصائيات MJD Video Downloader\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "👥 المستخدمون\n"
        f"إجمالي المستخدمين: {stats['users']}\n\n"

        "📥 إجمالي التنزيلات\n"
        f"كل الطلبات: {stats['total']}\n"
        f"✅ ناجحة: {stats['success']}\n"
        f"❌ فاشلة: {stats['failed']}\n"
        f"⏳ قيد الانتظار أو التنفيذ: {stats['pending']}\n\n"

        "📱 حسب المنصة\n"
        f"TikTok: {stats['tiktok']}\n"
        f"Instagram: {stats['instagram']}\n\n"

        "📅 إحصائيات اليوم\n"
        f"الطلبات: {stats['today_total']}\n"
        f"الناجحة: {stats['today_success']}\n"
        f"الفاشلة: {stats['today_failed']}\n\n"

        "⚙️ حالة الطابور الآن\n"
        f"التنزيلات الجارية: {running}/{MAX_CONCURRENT_DOWNLOADS}\n"
        f"طلبات منتظرة: {waiting}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "🧾 آخر 10 عمليات تنزيل\n"
    )

    if not stats["recent"]:

        message += "\nلا توجد عمليات مسجلة حتى الآن."

    else:

        for index, item in enumerate(stats["recent"], 1):

            customer = (
                item["customer_name"]
                or "مستخدم غير معروف"
            )

            username = item["username"]

            if username:
                customer += f" (@{username})"

            platform = item["platform"] or "غير معروف"

            status = {
                "queued": "⏳ في الانتظار",
                "running": "🔄 جاري التنزيل",
                "success": "✅ ناجح",
                "failed": "❌ فاشل"
            }.get(item["status"], item["status"])

            created_at = item["created_at"] or ""

            message += (
                f"\n\n{index}. {customer}\n"
                f"المنصة: {platform}\n"
                f"الحالة: {status}\n"
                f"التاريخ: {created_at}"
            )

    message += (
        "\n\n━━━━━━━━━━━━━━━━━━\n"
        "MJD | Admin Statistics"
    )

    return message


init_database()


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
    "يدعم الفيديوهات ومنشورات الصور.\n\n"
    "طريقة الاستخدام:\n"
    "1. انسخ رابط المنشور.\n"
    "2. أرسله هنا.\n"
    "3. انتظر وصول المحتوى إليك.\n\n"
    "للمساعدة أرسل /help\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "MJD | Video Downloader"
)

HELP_MESSAGE = (
    "📖 طريقة استخدام MJD Video Downloader\n"
    "━━━━━━━━━━━━━━━━━━\n\n"
    "1️⃣ افتح المنشور في TikTok أو Instagram.\n"
    "2️⃣ اضغط على مشاركة ثم نسخ الرابط.\n"
    "3️⃣ أرسل الرابط إلى البوت.\n"
    "4️⃣ انتظر وصول المحتوى إليك.\n\n"
    "✅ المنصات المدعومة:\n"
    "• TikTok\n"
    "• Instagram\n\n"
    "يدعم البوت فيديوهات TikTok ومنشورات الصور.\n\n"
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


def send_action(chat_id, action="upload_video"):

    try:

        telegram_request(
            "sendChatAction",
            data={
                "chat_id": chat_id,
                "action": action
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

IMAGE_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
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


def is_tiktok_url(url):

    host = get_domain(url)

    return bool(
        host and domain_matches(host, ("tiktok.com",))
    )


def is_tiktok_photo_url(url):

    if not is_tiktok_url(url):
        return False

    parsed = urlparse(url)

    return "/photo/" in parsed.path.lower()


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


def get_platform(url):

    host = get_domain(url) or ""

    if domain_matches(host, ("tiktok.com",)):
        return "TikTok"

    if domain_matches(
        host,
        ("instagram.com", "instagr.am")
    ):
        return "Instagram"

    return "Unknown"


# =========================
# تحويل روابط TikTok المختصرة
# =========================

def resolve_tiktok_url(url):

    if not is_tiktok_url(url):
        return url

    logger.info("Resolving TikTok URL")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        )
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            allow_redirects=True,
            timeout=25,
            stream=True
        )

        final_url = response.url
        response.close()

        if is_tiktok_url(final_url):

            logger.info(
                "TikTok URL resolved successfully: %s",
                final_url
            )

            return final_url

    except requests.RequestException as error:

        logger.warning(
            "TikTok URL resolution failed: %s",
            error
        )

    return url


# =========================
# تنزيل صور TikTok
# =========================

def download_tiktok_photos(url, folder):

    logger.info("Starting TikTok photo download")

    command = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--directory",
        folder,
        "--no-input",
        "--no-mtime",
        "--quiet",
        url
    ]

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=240
        )

    except subprocess.TimeoutExpired:
        raise RuntimeError("Photo download timed out")

    if result.returncode != 0:

        error_details = (
            result.stderr
            or result.stdout
            or "Unknown gallery-dl error"
        )

        logger.error(
            "GALLERY-DL ERROR: %s",
            error_details[-2000:]
        )

        raise RuntimeError("TikTok photo download failed")

    photo_files = []

    for root, directories, filenames in os.walk(folder):

        for filename in filenames:

            path = os.path.join(root, filename)

            if (
                os.path.isfile(path)
                and filename.lower().endswith(IMAGE_EXTENSIONS)
                and os.path.getsize(path) > 0
            ):
                photo_files.append(path)

    photo_files.sort()

    if not photo_files:
        raise RuntimeError("No photos were downloaded")

    for path in photo_files:

        if os.path.getsize(path) > MAX_PHOTO_SIZE:
            raise RuntimeError(
                "One of the photos exceeds the upload limit"
            )

    logger.info(
        "TikTok photos downloaded: %s",
        len(photo_files)
    )

    return photo_files


# =========================
# إرسال الصور
# =========================

def send_single_photo(chat_id, photo_path):

    with open(photo_path, "rb") as photo_file:

        try:

            telegram_request(
                "sendPhoto",
                data={"chat_id": chat_id},
                files={
                    "photo": (
                        os.path.basename(photo_path),
                        photo_file
                    )
                },
                timeout=180
            )

        except Exception:

            photo_file.seek(0)

            telegram_request(
                "sendDocument",
                data={"chat_id": chat_id},
                files={
                    "document": (
                        os.path.basename(photo_path),
                        photo_file
                    )
                },
                timeout=180
            )


def send_photo_group(chat_id, photo_paths):

    if len(photo_paths) == 1:
        send_single_photo(chat_id, photo_paths[0])
        return

    for start in range(0, len(photo_paths), 10):

        batch = photo_paths[start:start + 10]

        if any(
            os.path.getsize(path) > 10 * 1024 * 1024
            for path in batch
        ):

            for path in batch:
                send_single_photo(chat_id, path)

            continue

        media = []
        files = {}
        opened_files = []

        try:

            for index, path in enumerate(batch):

                field_name = f"photo{index}"

                photo_file = open(path, "rb")
                opened_files.append(photo_file)

                files[field_name] = (
                    os.path.basename(path),
                    photo_file
                )

                media.append({
                    "type": "photo",
                    "media": f"attach://{field_name}"
                })

            telegram_request(
                "sendMediaGroup",
                data={
                    "chat_id": chat_id,
                    "media": json.dumps(media)
                },
                files=files,
                timeout=180
            )

        finally:

            for photo_file in opened_files:
                photo_file.close()


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

    options["impersonate"] = ImpersonateTarget.from_str("chrome")

    with yt_dlp.YoutubeDL(options) as ydl:

        info = ydl.extract_info(
            url,
            download=True
        )

        if not info:
            raise RuntimeError("No video information returned")

        files = glob.glob(os.path.join(folder, "*"))

        video_files = [
            path for path in files
            if os.path.isfile(path)
            and not path.endswith((".part", ".ytdl"))
            and not path.lower().endswith(IMAGE_EXTENSIONS)
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

def process_download(chat_id, url, download_id):

    update_download_status(download_id, "running")

    try:

        send_message(
            chat_id,
            "⏳ بدأ تنزيل طلبك..."
        )

        resolved_url = resolve_tiktok_url(url)

        logger.info("Original URL: %s", url)
        logger.info("Resolved URL: %s", resolved_url)

        with tempfile.TemporaryDirectory() as folder:

            # منشور صور TikTok
            if is_tiktok_photo_url(resolved_url):

                send_message(
                    chat_id,
                    "📸 تم التعرف على منشور صور.\n"
                    "جاري تنزيل الصور..."
                )

                send_action(chat_id, "upload_photo")

                photo_paths = download_tiktok_photos(
                    resolved_url,
                    folder
                )

                send_message(
                    chat_id,
                    f"📤 تم تنزيل {len(photo_paths)} صورة، "
                    "جاري إرسالها..."
                )

                send_photo_group(chat_id, photo_paths)

                send_message(
                    chat_id,
                    "✅ تم إرسال جميع صور المنشور."
                )

            # منشور فيديو
            else:

                send_message(
                    chat_id,
                    "⏳ جاري تنزيل الفيديو..."
                )

                send_action(chat_id, "upload_video")

                video_path = download_video(
                    resolved_url,
                    folder
                )

                logger.info(
                    "Video downloaded: %s bytes",
                    os.path.getsize(video_path)
                )

                send_message(
                    chat_id,
                    "📤 اكتمل التنزيل، جاري إرسال الفيديو..."
                )

                upload_video(chat_id, video_path)

                logger.info("Video sent successfully")

        update_download_status(download_id, "success")

    except Exception as error:

        update_download_status(download_id, "failed")

        logger.exception("DOWNLOAD ERROR: %r", error)

        error_text = str(error).lower()

        if (
            "max-filesize" in error_text
            or "file size" in error_text
            or "exceeds the upload limit" in error_text
        ):

            message = "❌ حجم الملف أكبر من الحد المسموح."

        elif "private" in error_text or "login" in error_text:

            message = "❌ المنشور خاص أو يتطلب تسجيل دخول."

        elif "unsupported url" in error_text:

            message = (
                "❌ تعذر التعرف على نوع المنشور "
                "أو أن الرابط غير مدعوم."
            )

        elif "telegram" in error_text:

            message = "❌ تعذر إرسال المحتوى إلى تيليجرام."

        else:

            message = (
                "❌ تعذر تنزيل المحتوى.\n"
                "تأكد من أن المنشور متاح للعامة وحاول مرة أخرى."
            )

        send_message(chat_id, message)


# =========================
# عمال معالجة الطابور
# =========================

def download_queue_worker():

    global active_download_count

    logger.info(
        "Download queue worker started"
    )

    while True:

        chat_id, url, download_id = download_queue.get()

        with active_download_lock:
            active_download_count += 1

        try:

            logger.info(
                "Processing download: chat_id=%s active=%s waiting=%s",
                chat_id,
                get_active_download_count(),
                download_queue.qsize()
            )

            process_download(
                chat_id,
                url,
                download_id
            )

        except Exception:

            logger.exception("QUEUE WORKER ERROR")

            try:
                update_download_status(
                    download_id,
                    "failed"
                )
            except Exception:
                logger.exception("FAILED TO UPDATE DOWNLOAD STATUS")

        finally:

            with active_download_lock:
                active_download_count -= 1

            download_queue.task_done()


# تشغيل 3 عمال بالتوازي
for worker_number in range(MAX_CONCURRENT_DOWNLOADS):

    worker_thread = threading.Thread(
        target=download_queue_worker,
        name=f"download-worker-{worker_number + 1}",
        daemon=True
    )

    worker_thread.start()


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

    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id:
        return jsonify({"ok": True})

    sender = message.get("from", {})

    first_name = sender.get("first_name", "").strip()
    last_name = sender.get("last_name", "").strip()
    username = sender.get("username", "").strip()

    customer_name = " ".join(
        part for part in (first_name, last_name)
        if part
    )

    if not customer_name:
        customer_name = username or "مستخدم غير معروف"

    try:

        save_user(
            chat_id,
            customer_name,
            username
        )

    except Exception:

        logger.exception("USER DATABASE ERROR")

    command = (
        text.split(maxsplit=1)[0].split("@")[0]
        if text else ""
    )

    if command == "/start":

        send_message(chat_id, WELCOME_MESSAGE)

        return jsonify({"ok": True})

    if command == "/help":

        send_message(chat_id, HELP_MESSAGE)

        return jsonify({"ok": True})

    # إحصائيات الإدارة فقط
    if command == "/stats":

        if not ADMIN_ID:

            send_message(
                chat_id,
                "⚠️ لم يتم إعداد ADMIN_ID في Render."
            )

            return jsonify({"ok": True})

        if str(chat_id) != ADMIN_ID:

            send_message(
                chat_id,
                "⛔ هذا الأمر مخصص للإدارة فقط."
            )

            logger.warning(
                "Unauthorized stats command from chat_id=%s",
                chat_id
            )

            return jsonify({"ok": True})

        try:

            stats_message = build_statistics_message()

            send_message(
                chat_id,
                stats_message
            )

        except Exception:

            logger.exception("STATS ERROR")

            send_message(
                chat_id,
                "❌ حدث خطأ أثناء جلب الإحصائيات."
            )

        return jsonify({"ok": True})

    url = get_url(text)

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
            "أرسل رابط فيديو أو منشور صور من:\n"
            "📱 TikTok\n"
            "📸 Instagram\n\n"
            "للمساعدة أرسل /help"
        )

        return jsonify({"ok": True})

    platform = get_platform(url)

    # إضافة الطلب إلى الطابور
    with queue_lock:

        waiting_count = download_queue.qsize()
        running_count = get_active_download_count()

        if waiting_count >= MAX_QUEUE_SIZE:

            send_message(
                chat_id,
                "⚠️ طابور التحميل ممتلئ حالياً.\n"
                "انتظر قليلاً ثم أرسل الرابط مرة أخرى."
            )

            return jsonify({"ok": True})

        if running_count < MAX_CONCURRENT_DOWNLOADS:
            position = 1
        else:
            position = waiting_count + 1

        try:

            download_id = create_download_record(
                chat_id,
                customer_name,
                username,
                platform
            )

        except Exception:

            logger.exception("DOWNLOAD RECORD ERROR")

            send_message(
                chat_id,
                "❌ حدث خطأ أثناء تسجيل طلبك. حاول مرة أخرى."
            )

            return jsonify({"ok": True})

        download_queue.put((
            chat_id,
            url,
            download_id
        ))

    if running_count < MAX_CONCURRENT_DOWNLOADS:

        confirmation = (
            "✅ تم استلام طلبك.\n"
            "🚀 يوجد مسار تنزيل متاح، وسيبدأ طلبك قريباً."
        )

    else:

        confirmation = (
            "✅ تم استلام طلبك وإضافته إلى طابور التحميل.\n"
            f"📍 ترتيبك التقريبي في الانتظار: {position}\n"
            "سيبدأ التنزيل عند توفر مسار."
        )

    send_message(chat_id, confirmation)

    logger.info(
        "Download queued: chat_id=%s position=%s active=%s waiting=%s",
        chat_id,
        position,
        get_active_download_count(),
        download_queue.qsize()
    )

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
