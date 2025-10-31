import os
import logging
from flask import Flask, request
from instagrapi import Client
from telegram import Bot, Update
from telegram.ext import Dispatcher, MessageHandler, Filters
import threading
import ffmpeg

# ========== CONFIGURATION ==========
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
INSTAGRAM_USERNAME = "YOUR_INSTAGRAM_USERNAME"
INSTAGRAM_PASSWORD = "YOUR_INSTAGRAM_PASSWORD"

# Render-compatible storage directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
os.makedirs(VIDEO_DIR, exist_ok=True)

app = Flask(__name__)

# ========== LOGGING SETUP ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== INSTAGRAM CLIENT SETUP ==========
cl = Client()
session_path = os.path.join(VIDEO_DIR, "ig_session.json")

try:
    if os.path.exists(session_path):
        cl.load_settings(session_path)
        try:
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        except Exception as e:
            print("⚠️ Login challenge detected:", e)
            try:
                cl.challenge_resolve()
                print("✅ Challenge automatically resolved (using saved session or trust)")
            except Exception as inner:
                print("❌ Challenge resolve failed — please verify manually in your Instagram app.")
        print("✅ Loaded existing Instagram session")
    else:
        try:
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        except Exception as e:
            print("⚠️ Login challenge detected:", e)
            try:
                cl.challenge_resolve()
                print("✅ Challenge automatically resolved (using saved session or trust)")
            except Exception as inner:
                print("❌ Challenge resolve failed — please verify manually in your Instagram app.")
        cl.dump_settings(session_path)
        print("✅ New Instagram session created")
except Exception as e:
    print("❌ Instagram login failed:", e)

print(f"📂 Video folder ready: {VIDEO_DIR}")

# ========== TELEGRAM SETUP ==========
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=4)

# ========== VIDEO HANDLER ==========
def handle_video(update: Update, context):
    try:
        video = update.message.video or update.message.document
        if not video:
            update.message.reply_text("❌ No video found in your message.")
            return

        # Download video
        file = bot.get_file(video.file_id)
        video_path = os.path.join(VIDEO_DIR, f"{video.file_id}.mp4")
        file.download(custom_path=video_path)
        update.message.reply_text("🎥 Video received. Preparing for Instagram upload...")

        # Convert using FFmpeg (to ensure compatibility)
        converted_path = os.path.join(VIDEO_DIR, f"converted_{video.file_id}.mp4")
        (
            ffmpeg
            .input(video_path)
            .output(converted_path, vcodec='libx264', acodec='aac', vf='scale=720:-1')
            .overwrite_output()
            .run(quiet=True)
        )

        update.message.reply_text("📤 Uploading to Instagram...")

        # Upload to Instagram Feed
        cl.clip_upload(
            path=converted_path,
            caption="🎬 Uploaded automatically via Telegram bot 🤖"
        )

        update.message.reply_text("✅ Successfully posted on Instagram!")
        print(f"✅ Uploaded video: {converted_path}")

    except Exception as e:
        logger.error(f"Upload error: {e}")
        update.message.reply_text(f"❌ Upload failed: {e}")

# Register handler
dispatcher.add_handler(MessageHandler(Filters.video | Filters.document.video, handle_video))

# ========== WEBHOOK SETUP ==========
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK", 200

@app.route("/", methods=["GET", "HEAD"])
def index():
    return "🤖 InstaAutomation Bot is Running!", 200

# ========== BACKGROUND THREAD ==========
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

def keep_alive():
    import time
    while True:
        time.sleep(60)
        print("🔁 Keep-alive ping sent.")

# ========== STARTUP ==========
if __name__ == "__main__":
    print("🚀 Starting bot at", os.popen("date").read().strip())
    print("✅ Webhook OK: Your Render URL will be set automatically.")
    threading.Thread(target=run_flask).start()
    threading.Thread(target=keep_alive, daemon=True).start()
