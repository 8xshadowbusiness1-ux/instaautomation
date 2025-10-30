import os, threading, requests, time, json
from flask import Flask, request
from instagrapi import Client
from telegram import Bot, Update
from telegram.ext import Dispatcher, MessageHandler, Filters, CommandHandler
from PIL import Image

# --------------------------
# Environment Variables
# --------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
MY_RENDER_URL = os.getenv("MY_RENDER_URL", "").rstrip("/")
START_PORT = int(os.getenv("PORT", 10000))

# --------------------------
# Instagram Login (with session save)
# --------------------------
SESSION_FILE = "session.json"
cl = Client()

def login_instagram():
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            print("✅ Loaded existing Instagram session")
            return
        except Exception as e:
            print("⚠️ Reload failed, re-login:", e)

    cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    print("✅ New Instagram session created")

login_instagram()

# --------------------------
# Flask + Telegram Bot Setup
# --------------------------
app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

# Auto-correct Telegram Webhook
def ensure_webhook():
    correct_url = f"{MY_RENDER_URL}/{BOT_TOKEN}"
    try:
        info = bot.get_webhook_info()
        if not info or info.url != correct_url:
            bot.delete_webhook()
            bot.set_webhook(url=correct_url)
            print(f"⚙️ Webhook fixed → {correct_url}")
        else:
            print(f"✅ Webhook OK: {correct_url}")
    except Exception as e:
        print("⚠️ Webhook setup failed:", e)

ensure_webhook()

# --------------------------
# Directories
# --------------------------
VIDEO_DIR = "/data/videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

# --------------------------
# Instagram Upload Function
# --------------------------
def upload_video_to_instagram(video_path, caption=""):
    try:
        if not os.path.exists(video_path):
            print("❌ Video not found:", video_path)
            return False
        print("🎥 Uploading video to Instagram:", video_path)
        cl.video_upload(video_path, caption)
        print("✅ Video posted successfully")
        return True
    except Exception as e:
        print("⚠️ Video upload failed:", e)
        return False

# --------------------------
# Telegram Commands
# --------------------------
def start(update: Update, context):
    update.message.reply_text("🤖 Bot online! Send me a video to post on Instagram.")

def handle_video(update: Update, context):
    try:
        file = update.message.video or update.message.document
        if not file:
            update.message.reply_text("⚠️ No video found in your message.")
            return

        file_info = bot.get_file(file.file_id)
        filename = f"{int(time.time())}.mp4"
        filepath = os.path.join(VIDEO_DIR, filename)
        file_info.download(custom_path=filepath)

        update.message.reply_text("📥 Video received! Uploading to Instagram...")
        caption = update.message.caption or ""

        if upload_video_to_instagram(filepath, caption):
            update.message.reply_text("✅ Video posted successfully on Instagram!")
        else:
            update.message.reply_text("❌ Failed to post video.")
    except Exception as e:
        update.message.reply_text(f"⚠️ Error: {e}")
        print("Video handling failed:", e)

# --------------------------
# Flask Webhook Endpoint
# --------------------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "ok"

# --------------------------
# Telegram Dispatcher
# --------------------------
dp = Dispatcher(bot, None, workers=2)
dp.add_handler(CommandHandler("start", start))
dp.add_handler(MessageHandler(Filters.video | Filters.document.video, handle_video))

# --------------------------
# Background Keep-Alive Ping
# --------------------------
def keep_alive_ping(url):
    while True:
        try:
            requests.get(url, timeout=10)
            print(f"🔁 Ping sent to {url}")
        except Exception as e:
            print("Ping failed:", e)
        time.sleep(600)  # every 10 minutes

# --------------------------
# Main Entry Point
# --------------------------
if __name__ == "__main__":
    threading.Thread(target=keep_alive_ping, args=(MY_RENDER_URL,), daemon=True).start()
    print("🚀 Starting Telegram webhook bot...")
    app.run(host="0.0.0.0", port=START_PORT)
