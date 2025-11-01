import os
import time
import random
import threading
import requests
from flask import Flask
from instagrapi import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, CallbackQueryHandler

# ==========================
# CONFIGURATION
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
INSTAGRAM_USERNAME = os.getenv("IG_USERNAME", "your_ig_username")
INSTAGRAM_PASSWORD = os.getenv("IG_PASSWORD", "your_ig_password")
MY_RENDER_URL = os.getenv("MY_RENDER_URL", "https://yourapp.onrender.com")
VIDEO_DIR = "videos"
AUTOZ_INTERVAL = int(os.getenv("AUTOZ_INTERVAL", "900"))  # default 15 min
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))  # optional

app = Flask(__name__)
bot_status = {
    "videos_posted": 0,
    "last_post_time": None,
    "next_post_in": AUTOZ_INTERVAL,
    "last_error": None,
    "ping_interval": 600,
    "last_ping": None,
    "is_running": False
}

# ==========================
# KEEP ALIVE PING THREAD
# ==========================
def keep_alive_ping():
    while True:
        try:
            res = requests.get(MY_RENDER_URL)
            bot_status["last_ping"] = time.strftime("%H:%M:%S")
            print(f"🔁 Keep-alive ping sent ({res.status_code}) to {MY_RENDER_URL}")
        except Exception as e:
            bot_status["last_error"] = f"Ping Error: {e}"
            print(f"⚠️ Keep-alive error: {e}")
        time.sleep(bot_status["ping_interval"])  # default every 10 min

threading.Thread(target=keep_alive_ping, daemon=True).start()

# ==========================
# LOGIN FUNCTION
# ==========================
def ig_login():
    cl = Client()
    try:
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        print("✅ Logged in to Instagram")
        return cl
    except Exception as e:
        bot_status["last_error"] = f"IG Login Error: {e}"
        print(f"⚠️ Instagram login failed: {e}")
        return None

# ==========================
# DOWNLOAD RANDOM VIDEO
# ==========================
def download_random_video(username):
    cl = ig_login()
    if not cl:
        return False, "Login failed"
    try:
        # Use private API (v1) to fetch user media safely
        uid = cl.user_id_from_username(username)
        medias = cl.user_medias_v1(uid, amount=30)
    except Exception as e:
        print(f"[⚠️] user_medias_v1 failed for {username}: {e}")
        medias = []

    vids = [m for m in medias if getattr(m, "video_url", None)]
    if not vids:
        return False, "No videos found"

    ch = random.choice(vids)
    os.makedirs(VIDEO_DIR, exist_ok=True)
    video_path = cl.video_download(ch.pk, folder=VIDEO_DIR)
    return True, video_path

# ==========================
# AUTOZ WORKER
# ==========================
AUTOZ_TARGET = None
AUTOZ_RUNNING = False

def autoz_worker():
    global AUTOZ_RUNNING
    AUTOZ_RUNNING = True
    bot_status["is_running"] = True
    while AUTOZ_RUNNING:
        try:
            if AUTOZ_TARGET:
                ok, msg = download_random_video(AUTOZ_TARGET)
                if ok:
                    print(f"✅ Downloaded and posting: {msg}")
                    cl = ig_login()
                    if cl:
                        cl.clip_upload(msg, caption=f"Autoz repost from @{AUTOZ_TARGET}")
                        bot_status["videos_posted"] += 1
                        bot_status["last_post_time"] = time.strftime("%H:%M:%S")
                        bot_status["last_error"] = None
                        print("📤 Posted to Instagram")
                else:
                    bot_status["last_error"] = msg
                    print(f"⚠️ {msg}")
            else:
                print("⚠️ No target set for autozmode.")
            time.sleep(bot_status["next_post_in"])
        except Exception as e:
            bot_status["last_error"] = str(e)
            print(f"❌ Autoz error: {e}")
            time.sleep(600)  # wait 10 min before retry
    bot_status["is_running"] = False

# ==========================
# TELEGRAM BOT COMMANDS
# ==========================
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 *InstaAutomation Bot is Active!*\n"
        "Commands:\n"
        "/settarget <username> — Set target Instagram\n"
        "/start_auto — Start auto repost\n"
        "/stop_auto — Stop auto repost\n"
        "/setinterval <seconds> — Change posting interval\n"
        "/status — Show current bot status\n"
        "/ping — Check ping status\n",
        parse_mode="Markdown"
    )

def settarget(update: Update, context: CallbackContext):
    global AUTOZ_TARGET
    if len(context.args) == 0:
        update.message.reply_text("⚠️ Usage: /settarget <username>")
        return
    AUTOZ_TARGET = context.args[0]
    update.message.reply_text(f"🎯 Target set to: {AUTOZ_TARGET}")

def start_auto(update: Update, context: CallbackContext):
    if not AUTOZ_TARGET:
        update.message.reply_text("⚠️ Set a target first using /settarget <username>")
        return
    threading.Thread(target=autoz_worker, daemon=True).start()
    update.message.reply_text("🚀 Autoz Mode Started!")

def stop_auto(update: Update, context: CallbackContext):
    global AUTOZ_RUNNING
    AUTOZ_RUNNING = False
    update.message.reply_text("🛑 Autoz Mode Stopped!")

def setinterval(update: Update, context: CallbackContext):
    if len(context.args) == 0:
        update.message.reply_text("⚠️ Usage: /setinterval <seconds>")
        return
    try:
        sec = int(context.args[0])
        bot_status["next_post_in"] = sec
        update.message.reply_text(f"⏱ Interval set to {sec} seconds.")
    except:
        update.message.reply_text("⚠️ Invalid input.")

def status(update: Update, context: CallbackContext):
    msg = (
        "📊 *Bot Status:*\n"
        f"🏃 Running: {'✅ Yes' if bot_status['is_running'] else '❌ No'}\n"
        f"🎯 Target: {AUTOZ_TARGET or 'Not set'}\n"
        f"📹 Videos Posted: {bot_status['videos_posted']}\n"
        f"🕒 Last Post: {bot_status['last_post_time'] or 'N/A'}\n"
        f"⏳ Next Post In: {bot_status['next_post_in']} sec\n"
        f"📡 Last Ping: {bot_status['last_ping'] or 'N/A'}\n"
        f"💥 Last Error: {bot_status['last_error'] or 'None'}\n"
        f"🔁 Ping Interval: {bot_status['ping_interval']} sec\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

def ping(update: Update, context: CallbackContext):
    try:
        res = requests.get(MY_RENDER_URL)
        update.message.reply_text(f"✅ Ping OK ({res.status_code})")
    except Exception as e:
        update.message.reply_text(f"⚠️ Ping failed: {e}")

# ==========================
# TELEGRAM BOT RUNNER
# ==========================
def run_bot():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("settarget", settarget))
    dp.add_handler(CommandHandler("start_auto", start_auto))
    dp.add_handler(CommandHandler("stop_auto", stop_auto))
    dp.add_handler(CommandHandler("setinterval", setinterval))
    dp.add_handler(CommandHandler("status", status))
    dp.add_handler(CommandHandler("ping", ping))
    print("🤖 Telegram Bot Started")
    updater.start_polling()
    updater.idle()

threading.Thread(target=run_bot, daemon=True).start()

# ==========================
# FLASK KEEP-ALIVE
# ==========================
@app.route('/')
def home():
    return "✅ InstaAutomation is Live!"

if __name__ == '__main__':
    print("🚀 InstaAutomation Booting...")
    app.run(host='0.0.0.0', port=10000)
