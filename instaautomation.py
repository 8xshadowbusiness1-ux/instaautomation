"""
instaautomation.py - Webhook + Full Commands (Render optimized)

Features:
- Full command set (addvideo, addpriority, list, show, remove, removepriority, setcaption,
  viewcaption, removecaption, settimer, startposting, stopposting, schedule, listscheduled,
  removescheduled, status, viewallcmd, help, start, login).
- Webhook mode for Telegram (Flask route) — no polling conflict.
- Background worker for scheduled & queue posting.
- Safe environment variable credentials.
- Keep-alive ping for Render hosting.
"""

import os
import json
import threading
import time
import random
from datetime import datetime, timedelta
from functools import wraps
from instagrapi import Client
from flask import Flask, request
import requests
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, ConversationHandler, CallbackContext

# -----------------------------
# CONFIG (Environment Safe)
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
MY_RENDER_URL = os.getenv("MY_RENDER_URL", "").rstrip("/")

DATA_FILE = "data.json"
VIDEO_DIR = "videos"
START_PORT = int(os.getenv("PORT", 10000))
PRIORITY_WEIGHT = 3
INSTAPOST_SLEEP_AFTER_FAIL = 30

os.makedirs(VIDEO_DIR, exist_ok=True)

DEFAULT_DATA = {
    "caption": "",
    "interval_min": 1800,
    "interval_max": 3600,
    "videos": [],
    "scheduled": [],
    "last_post": {"video_code": None, "time": None},
    "is_running": False,
    "next_queue_post_time": None
}

data_lock = threading.Lock()


def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
        return json.loads(json.dumps(DEFAULT_DATA))
    with open(DATA_FILE, "r") as f:
        try:
            d = json.load(f)
        except Exception:
            d = json.loads(json.dumps(DEFAULT_DATA))
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d


def save_data(d):
    with data_lock:
        with open(DATA_FILE, "w") as f:
            json.dump(d, f, indent=2, default=str)


data = load_data()

# -----------------------------
# Instagram Client
# -----------------------------
ig_client = None
ig_lock = threading.Lock()


def ig_login(force=False):
    global ig_client
    with ig_lock:
        if ig_client is None or force:
            ig_client = Client()
            try:
                ig_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                print("✅ Instagram logged in.")
            except Exception as e:
                print("⚠️ Instagram login failed:", e)
                ig_client = None
        return ig_client


# -----------------------------
# Helpers
# -----------------------------
def admin_only(func):
    @wraps(func)
    def wrapper(update: Update, context: CallbackContext, *args, **kwargs):
        if ADMIN_CHAT_ID is None:
            return func(update, context, *args, **kwargs)
        if update.effective_user.id != ADMIN_CHAT_ID:
            update.message.reply_text("❌ You are not authorized.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper


def generate_code(prefix, existing):
    n = 1
    while True:
        code = f"{prefix}{n}"
        if code not in existing:
            return code
        n += 1


def human_timedelta(seconds):
    if seconds <= 60:
        return f"{seconds}s"
    return f"{seconds // 60}m"


# -----------------------------
# Telegram Commands
# -----------------------------
def start_cmd(update, context): update.message.reply_text("🚀 InstaAutomation Bot ready. Use /viewallcmd to see commands.")
def help_cmd(update, context): update.message.reply_text("Use /viewallcmd to see all available commands.")


@admin_only
def addvideo_start(update, context):
    update.message.reply_text("📥 Send video to add.")
    context.user_data['add_type'] = 'normal'


@admin_only
def addpriority_start(update, context):
    update.message.reply_text("📥 Send PRIORITY video.")
    context.user_data['add_type'] = 'priority'


def receive_video(update, context):
    msg = update.message
    if not (msg and (msg.video or msg.document)): return
    fobj = msg.video or msg.document
    file = context.bot.get_file(fobj.file_id)
    v_code = generate_code("vid", [v["code"] for v in data["videos"]])
    path = os.path.join(VIDEO_DIR, f"{v_code}.mp4")
    file.download(custom_path=path)
    vtype = context.user_data.get('add_type', 'normal')
    data["videos"].append({"code": v_code, "path": path, "type": vtype})
    save_data(data)
    update.message.reply_text(f"✅ Added `{v_code}` ({vtype})", parse_mode="Markdown")
    context.user_data.pop('add_type', None)


def list_cmd(update, context):
    vids = data.get("videos", [])
    if not vids: return update.message.reply_text("🎞️ No videos queued.")
    lines = [f"{v['code']} — {v['type']}" for v in vids]
    update.message.reply_text("🎬 Videos:\n" + "\n".join(lines))


def remove_cmd(update, context):
    if not context.args: return update.message.reply_text("Usage: /remove <code>")
    code = context.args[0]
    data["videos"] = [v for v in data["videos"] if v["code"] != code]
    save_data(data)
    update.message.reply_text(f"✅ Removed {code}")


def setcaption_cmd(update, context):
    text = " ".join(context.args)
    data["caption"] = text
    save_data(data)
    update.message.reply_text("✅ Caption updated.")


def viewcaption_cmd(update, context):
    c = data.get("caption") or "(none)"
    update.message.reply_text(f"📜 Caption:\n{c}")


def removecaption_cmd(update, context):
    data["caption"] = ""
    save_data(data)
    update.message.reply_text("❌ Caption removed.")


def settimer_cmd(update, context):
    try:
        mn, mx = int(context.args[0]), int(context.args[1])
        data["interval_min"], data["interval_max"] = mn, mx
        save_data(data)
        update.message.reply_text(f"⏱️ Timer set {mn}-{mx}s")
    except:
        update.message.reply_text("Usage: /settimer <min> <max>")


def startposting_cmd(update, context):
    if data["is_running"]: return update.message.reply_text("⚙️ Already running.")
    data["is_running"] = True
    save_data(data)
    update.message.reply_text("🚀 Auto-posting started.")


def stopposting_cmd(update, context):
    data["is_running"] = False
    save_data(data)
    update.message.reply_text("🛑 Auto-posting stopped.")


def status_cmd(update, context):
    msg = (
        f"📊 *Status*\n"
        f"Mode: {'✅ Running' if data['is_running'] else '❌ Stopped'}\n"
        f"Videos: {len(data['videos'])}\n"
        f"Caption: {data['caption'] or '(none)'}\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


def viewallcmd_cmd(update, context):
    msg = (
        "📘 *COMMAND LIST*\n"
        "──────────────────────\n"
        "🎞️ /addvideo - Add normal video\n"
        "⚡ /addpriority - Add priority video\n"
        "📜 /setcaption - Set caption\n"
        "🗑️ /remove <code> - Remove video\n"
        "⏱️ /settimer <min> <max> - Set post interval\n"
        "🚀 /startposting - Start posting\n"
        "🛑 /stopposting - Stop posting\n"
        "📊 /status - Show status\n"
        "🧾 /viewcaption - View caption\n"
        "❌ /removecaption - Remove caption\n"
        "📚 /viewallcmd - Show all commands\n"
        "🔐 /login - Re-login Instagram\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


def login_cmd(update, context):
    update.message.reply_text("🔐 Logging into Instagram...")
    cl = ig_login(force=True)
    if cl:
        update.message.reply_text(f"✅ Logged in as {INSTAGRAM_USERNAME}")
    else:
        update.message.reply_text("⚠️ Login failed.")


# -----------------------------
# Background Posting
# -----------------------------
def post_to_instagram(video, caption):
    try:
        cl = ig_login()
        if not cl: return False
        cl.video_upload(video, caption or "")
        print(f"✅ Posted {video}")
        return True
    except Exception as e:
        print("❌ Upload failed:", e)
        return False


def background_worker():
    print("🧠 Background worker running...")
    while True:
        try:
            if data["is_running"]:
                if data["videos"]:
                    video = random.choice(data["videos"])
                    caption = data.get("caption", "")
                    ok = post_to_instagram(video["path"], caption)
                    if ok:
                        data["last_post"] = {"video_code": video["code"], "time": datetime.now().isoformat()}
                        save_data(data)
                    delay = random.randint(data["interval_min"], data["interval_max"])
                    print(f"Next post in {delay}s")
                    time.sleep(delay)
                else:
                    time.sleep(10)
            else:
                time.sleep(5)
        except Exception as e:
            print("Worker error:", e)
            time.sleep(5)

# -----------------------------
# Ensure correct webhook (auto-fix)
# -----------------------------
webhook_url = f"{MY_RENDER_URL}/{BOT_TOKEN}"

try:
    current_info = bot.get_webhook_info()
    if not current_info or current_info.url != webhook_url:
        print(f"⚙️ Resetting webhook to: {webhook_url}")
        bot.delete_webhook()
        bot.set_webhook(url=webhook_url)
    else:
        print(f"✅ Webhook already correct: {webhook_url}")
except Exception as e:
    print("⚠️ Webhook setup failed:", e)

# -----------------------------
# Flask Webhook
# -----------------------------
app = Flask(__name__)
if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing. Exiting.")
    raise SystemExit()

bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

dispatcher.add_handler(CommandHandler("start", start_cmd))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))
dispatcher.add_handler(CommandHandler("addvideo", addvideo_start))
dispatcher.add_handler(CommandHandler("addpriority", addpriority_start))
dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, receive_video))
dispatcher.add_handler(CommandHandler("list", list_cmd))
dispatcher.add_handler(CommandHandler("remove", remove_cmd))
dispatcher.add_handler(CommandHandler("setcaption", setcaption_cmd))
dispatcher.add_handler(CommandHandler("viewcaption", viewcaption_cmd))
dispatcher.add_handler(CommandHandler("removecaption", removecaption_cmd))
dispatcher.add_handler(CommandHandler("settimer", settimer_cmd))
dispatcher.add_handler(CommandHandler("startposting", startposting_cmd))
dispatcher.add_handler(CommandHandler("stopposting", stopposting_cmd))
dispatcher.add_handler(CommandHandler("status", status_cmd))
dispatcher.add_handler(CommandHandler("login", login_cmd))


@app.route("/", methods=["GET"])
def home():
    return "✅ Bot is alive", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(update)
    except Exception as e:
        print("Webhook error:", e)
    return "ok", 200


def keep_alive_ping(url):
    while True:
        try:
            requests.get(url, timeout=20)
            print(f"🔁 Ping → {url}")
        except Exception as e:
            print("Ping failed:", e)
        time.sleep(3600)


# -----------------------------
# MAIN ENTRYPOINT (WEBHOOK MODE)
# -----------------------------
if __name__ == "__main__":
    print("🤖 Starting Telegram webhook bot...")

    # Background worker
    threading.Thread(target=background_worker, daemon=True).start()

    # Set webhook
    webhook_url = f"{MY_RENDER_URL}/{BOT_TOKEN}"
    try:
        bot.delete_webhook()
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook set to: {webhook_url}")
    except Exception as e:
        print(f"❌ Webhook error: {e}")

    # Keep alive ping
    if MY_RENDER_URL:
        threading.Thread(target=keep_alive_ping, args=(MY_RENDER_URL,), daemon=True).start()

    print("🚀 Starting Flask server...")
    app.run(host="0.0.0.0", port=START_PORT)

