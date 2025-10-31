import os
import threading
import requests
import time
import schedule
import subprocess
from flask import Flask, request
from instagrapi import Client
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

# -----------------------------
# Environment Variables
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
MY_RENDER_URL = os.getenv("MY_RENDER_URL", "").rstrip("/")
START_PORT = int(os.getenv("PORT", "10000"))

# -----------------------------
# Directories (Render Safe)
# -----------------------------
try:
    VIDEO_DIR = "/opt/render/project/src/videos"
    os.makedirs(VIDEO_DIR, exist_ok=True)
    print("📂 Video folder ready:", VIDEO_DIR)
except Exception as e:
    print("⚠️ Using fallback /tmp/videos:", e)
    VIDEO_DIR = "/tmp/videos"
    os.makedirs(VIDEO_DIR, exist_ok=True)

# -----------------------------
# Instagram Client Setup
# -----------------------------
cl = Client()
session_path = os.path.join(VIDEO_DIR, "ig_session.json")

try:
    if os.path.exists(session_path):
        cl.load_settings(session_path)
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        print("✅ Loaded existing Instagram session")
    else:
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        cl.dump_settings(session_path)
        print("✅ New Instagram session created")
except Exception as e:
    print("❌ Instagram login failed:", e)

# -----------------------------
# Flask Server Setup
# -----------------------------
app = Flask(__name__)

# -----------------------------
# Telegram Bot Setup
# -----------------------------
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# -----------------------------
# FFmpeg Video Upload Function
# -----------------------------
def upload_video_to_instagram(video_path, caption=""):
    try:
        if not os.path.exists(video_path):
            print("❌ Video not found:", video_path)
            return False

        converted_path = os.path.join(VIDEO_DIR, f"converted_{os.path.basename(video_path)}")
        print("🎬 Converting video for Instagram upload...")
        subprocess.run([
            "ffmpeg", "-i", video_path,
            "-vf", "scale=720:-2",  # Resize to safe width
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            converted_path,
            "-y"
        ], check=True)

        print("🎥 Uploading converted video to Instagram:", converted_path)
        cl.video_upload(converted_path, caption)
        print("✅ Video posted successfully")
        return True
    except subprocess.CalledProcessError as e:
        print("⚠️ Video conversion failed:", e)
        return False
    except Exception as e:
        print("⚠️ Video upload failed:", e)
        return False

# -----------------------------
# Telegram Handlers
# -----------------------------
def start(update, context):
    update.message.reply_text("👋 Bot is online! Send me a video to post on Instagram.")

def handle_video(update, context):
    try:
        file = update.message.video.get_file()
        caption = update.message.caption or ""
        video_path = os.path.join(VIDEO_DIR, f"{file.file_id}.mp4")
        file.download(video_path)
        update.message.reply_text("📥 Video received! Uploading to Instagram...")
        print(f"📥 Downloaded: {video_path}")

        success = upload_video_to_instagram(video_path, caption)
        if success:
            update.message.reply_text("✅ Video uploaded to Instagram successfully!")
        else:
            update.message.reply_text("⚠️ Failed to upload video. Check logs.")
    except Exception as e:
        print("⚠️ Telegram video handler error:", e)
        update.message.reply_text("❌ Something went wrong while uploading.")

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(Filters.video, handle_video))

# -----------------------------
# Webhook Route
# -----------------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok"

@app.route("/", methods=["GET"])
def home():
    return "Telegram-Instagram Bot is Live 🚀", 200

# -----------------------------
# Keep-Alive Ping
# -----------------------------
def keep_alive_ping():
    while True:
        if MY_RENDER_URL:
            try:
                requests.head(MY_RENDER_URL)
                print("🔁 Keep-alive ping sent.")
            except Exception:
                print("⚠️ Keep-alive ping failed.")
        time.sleep(300)

# -----------------------------
# Background Scheduler
# -----------------------------
def background_worker():
    while True:
        schedule.run_pending()
        time.sleep(10)

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    # Start background worker
    threading.Thread(target=background_worker, daemon=True).start()

    # Start keep-alive ping
    if MY_RENDER_URL:
        threading.Thread(target=keep_alive_ping, daemon=True).start()

    # Set webhook
    webhook_url = f"{MY_RENDER_URL}/{BOT_TOKEN}"
    try:
        bot.delete_webhook()
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook OK: {webhook_url}")
    except Exception as e:
        print("⚠️ Failed to set webhook:", e)

    print("🚀 Starting Telegram webhook bot...")
    app.run(host="0.0.0.0", port=START_PORT)
