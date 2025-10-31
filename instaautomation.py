import os
import json
import threading
import time
import random
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from flask import Flask, request

from instagrapi import Client
from telegram import Update, Bot
from telegram.ext import (Dispatcher, CommandHandler, MessageHandler, Filters,
                          ConversationHandler, CallbackContext)

# -----------------------------
# CONFIGURATION
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0")) or None

DATA_FILE = "data.json"
VIDEO_DIR = "videos"
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

# -----------------------------
# GLOBAL STATE
# -----------------------------
data_lock = threading.Lock()
app = Flask(__name__)
bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=4)

# -----------------------------
# DATA HANDLING
# -----------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r") as f:
        try:
            d = json.load(f)
        except Exception:
            d = DEFAULT_DATA.copy()
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
# INSTAGRAM LOGIN
# -----------------------------
ig_client = None
ig_lock = threading.Lock()

def ig_login():
    global ig_client
    with ig_lock:
        if ig_client is None:
            ig_client = Client()
            session_path = os.path.join(VIDEO_DIR, "ig_session.json")
            try:
                if os.path.exists(session_path):
                    ig_client.load_settings(session_path)
                    ig_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                    print("✅ Loaded IG session")
                else:
                    ig_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                    ig_client.dump_settings(session_path)
                    print("✅ New IG login successful")
            except Exception as e:
                print("⚠️ IG Login Error:", e)
                ig_client = None
        return ig_client

# -----------------------------
# HELPERS
# -----------------------------
def admin_only(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        if ADMIN_CHAT_ID and update.effective_user.id != ADMIN_CHAT_ID:
            update.message.reply_text("❌ Unauthorized user.")
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

def post_to_instagram(video_path, caption_text):
    try:
        client = ig_login()
        if not client:
            print("IG client unavailable.")
            return False
        client.video_upload(video_path, caption_text or "")
        print("✅ Posted:", video_path)
        return True
    except Exception as e:
        print("❌ IG upload failed:", e)
        return False

# -----------------------------
# TELEGRAM COMMANDS
# -----------------------------
def start_cmd(update, context):
    update.message.reply_text("🚀 Instagram Scheduler Bot is online!\nUse /viewallcmd for full commands.")

def viewallcmd_cmd(update, context):
    msg = (
        "📘 *ALL COMMANDS*\n"
        "──────────────────────────────\n"
        "🎬 *VIDEO QUEUE*\n"
        "`/addvideo` — Upload a normal video.\n"
        "`/addpriority` — Upload a priority video.\n"
        "`/list` — List all videos.\n"
        "`/remove <vid>` — Remove a video.\n\n"
        "🏷️ *CAPTION*\n"
        "`/setcaption <text>` — Set caption.\n"
        "`/viewcaption` — View caption.\n"
        "`/removecaption` — Remove caption.\n\n"
        "🕒 *POSTING*\n"
        "`/settimer <min> <max>` — Set posting interval (sec)\n"
        "`/startposting` — Start auto posting.\n"
        "`/stopposting` — Stop auto posting.\n"
        "`/status` — View system status.\n\n"
        "🗓️ *SCHEDULER*\n"
        "`/schedule` — Schedule post (video → caption → time)\n"
        "`/listscheduled` — View scheduled posts.\n"
        "`/removescheduled <code>` — Remove scheduled post.\n\n"
        "📊 *UTILITY*\n"
        "`/viewallcmd` — This help panel.\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

@admin_only
def addvideo(update, context):
    update.message.reply_text("📥 Send the video to add.")
    context.user_data["type"] = "normal"

@admin_only
def addpriority(update, context):
    update.message.reply_text("📥 Send the PRIORITY video.")
    context.user_data["type"] = "priority"

def receive_video(update, context):
    msg = update.message
    if not msg.video and not msg.document:
        return
    file_obj = msg.video or msg.document
    file = context.bot.get_file(file_obj.file_id)
    code = generate_code("vid", [v["code"] for v in data["videos"]])
    path = os.path.join(VIDEO_DIR, f"{code}.mp4")
    file.download(path)
    vtype = context.user_data.get("type", "normal")
    data["videos"].append({"code": code, "path": path, "type": vtype})
    save_data(data)
    update.message.reply_text(f"✅ Saved {code} ({vtype})")

def list_cmd(update, context):
    vs = data["videos"]
    if not vs:
        update.message.reply_text("No videos found.")
        return
    lines = [f"{v['code']} - {v['type']}" for v in vs]
    update.message.reply_text("🎬 Videos:\n" + "\n".join(lines))

def setcaption_cmd(update, context):
    text = " ".join(context.args)
    if not text:
        update.message.reply_text("Usage: /setcaption <text>")
        return
    data["caption"] = text
    save_data(data)
    update.message.reply_text("✅ Caption updated.")

def viewcaption_cmd(update, context):
    update.message.reply_text(f"📜 {data.get('caption','(none)')}")

def removecaption_cmd(update, context):
    data["caption"] = ""
    save_data(data)
    update.message.reply_text("❌ Caption removed.")

def settimer_cmd(update, context):
    args = context.args
    if len(args) != 2:
        update.message.reply_text("Usage: /settimer <min> <max>")
        return
    mn, mx = int(args[0]), int(args[1])
    data["interval_min"] = mn
    data["interval_max"] = mx
    save_data(data)
    update.message.reply_text(f"⏱️ Interval set: {mn}-{mx}s")

def startposting_cmd(update, context):
    data["is_running"] = True
    next_t = datetime.now() + timedelta(seconds=random.randint(data["interval_min"], data["interval_max"]))
    data["next_queue_post_time"] = next_t.isoformat()
    save_data(data)
    update.message.reply_text("🚀 Auto-posting started!")

def stopposting_cmd(update, context):
    data["is_running"] = False
    save_data(data)
    update.message.reply_text("🛑 Auto-posting stopped.")

def status_cmd(update, context):
    txt = (
        f"📊 STATUS:\n"
        f"Mode: {'Running ✅' if data['is_running'] else 'Stopped ❌'}\n"
        f"Videos: {len(data['videos'])}\n"
        f"Caption: {data.get('caption','')}\n"
        f"Next Post: {data.get('next_queue_post_time')}\n"
    )
    update.message.reply_text(txt)

# -----------------------------
# BACKGROUND WORKER
# -----------------------------
def weighted_choice(videos):
    weighted = []
    for v in videos:
        if v["type"] == "priority":
            weighted.extend([v] * 3)
        else:
            weighted.append(v)
    return random.choice(weighted) if weighted else None

def background_worker():
    print("🧠 Background worker running...")
    while True:
        try:
            now = datetime.now()
            # handle scheduled posts
            for s in data.get("scheduled", []):
                if s.get("status") == "Pending":
                    dt = datetime.fromisoformat(s["datetime"])
                    if now >= dt:
                        success = post_to_instagram(s["video_path"], s.get("caption"))
                        s["status"] = "Posted" if success else "Failed"
                        save_data(data)
            # handle queue
            if data.get("is_running"):
                nxt = data.get("next_queue_post_time")
                if not nxt or now >= datetime.fromisoformat(nxt):
                    vid = weighted_choice(data.get("videos", []))
                    if vid:
                        success = post_to_instagram(vid["path"], data.get("caption"))
                        if success:
                            data["last_post"] = {"video_code": vid["code"], "time": datetime.now().isoformat()}
                        data["next_queue_post_time"] = (datetime.now() + timedelta(
                            seconds=random.randint(data["interval_min"], data["interval_max"])
                        )).isoformat()
                        save_data(data)
            time.sleep(5)
        except Exception as e:
            print("Worker error:", e)
            time.sleep(5)

# -----------------------------
# FLASK WEBHOOK
# -----------------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK", 200

@app.route("/", methods=["GET", "HEAD"])
def index():
    return "🤖 Insta Scheduler Bot Running!", 200

# -----------------------------
# STARTUP
# -----------------------------
def main():
    dispatcher.add_handler(CommandHandler("start", start_cmd))
    dispatcher.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))
    dispatcher.add_handler(CommandHandler("addvideo", addvideo))
    dispatcher.add_handler(CommandHandler("addpriority", addpriority))
    dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, receive_video))
    dispatcher.add_handler(CommandHandler("list", list_cmd))
    dispatcher.add_handler(CommandHandler("setcaption", setcaption_cmd))
    dispatcher.add_handler(CommandHandler("viewcaption", viewcaption_cmd))
    dispatcher.add_handler(CommandHandler("removecaption", removecaption_cmd))
    dispatcher.add_handler(CommandHandler("settimer", settimer_cmd))
    dispatcher.add_handler(CommandHandler("startposting", startposting_cmd))
    dispatcher.add_handler(CommandHandler("stopposting", stopposting_cmd))
    dispatcher.add_handler(CommandHandler("status", status_cmd))

    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    main()
