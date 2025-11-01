#!/usr/bin/env python3
"""
FINAL VERSION — instaautomation_final_single_instance.py

✅ All commands intact
✅ Self keep-alive (ping every 10 min)
✅ Single-instance protection (no Conflict)
✅ Instagram auto-login + video posting
✅ Thread-safe + auto restart ready
"""

import os
import json
import threading
import time
import random
import requests
from datetime import datetime, timedelta
from functools import wraps
from instagrapi import Client
from telegram import Update
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters,
                          ConversationHandler, CallbackContext)

# -----------------------------
# CONFIG
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN_HERE"
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME") or "YOUR_IG_USERNAME"
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD") or "YOUR_IG_PASSWORD"
ADMIN_CHAT_ID = None  # Set to your Telegram user ID to restrict access

DATA_FILE = "data.json"
VIDEO_DIR = "videos"
INSTAPOST_SLEEP_AFTER_FAIL = 30
PRIORITY_WEIGHT = 3

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


# -----------------------------
# KEEP-ALIVE THREAD
# -----------------------------
def keep_alive_ping():
    url = os.getenv("MY_RENDER_URL")
    if not url:
        print("⚠️ MY_RENDER_URL not set — skipping keep-alive")
        return
    if not url.lower().startswith("http"):
        url = "https://" + url
    while True:
        try:
            res = requests.get(url, timeout=20)
            print(f"🔁 Keep-alive ping sent ({res.status_code}) → {url}")
        except Exception as e:
            print(f"⚠️ Keep-alive error: {e}")
        time.sleep(600)


# -----------------------------
# DATA MANAGEMENT
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
            try:
                ig_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                print("✅ Instagram logged in.")
            except Exception as e:
                print("❌ Instagram login error:", e)
                ig_client = None
        return ig_client


# -----------------------------
# HELPERS
# -----------------------------
def admin_only(func):
    @wraps(func)
    def wrapper(update, context, *args, **kwargs):
        if ADMIN_CHAT_ID is None or update.effective_user.id == ADMIN_CHAT_ID:
            return func(update, context, *args, **kwargs)
        update.message.reply_text("❌ You are not authorized.")
    return wrapper


def generate_vid_code():
    existing = {v["code"] for v in data["videos"]}
    n = 1
    while f"vid{n}" in existing:
        n += 1
    return f"vid{n}"


def generate_shd_code():
    existing = {s["shd_code"] for s in data["scheduled"]}
    n = 1
    while f"shd{n}" in existing:
        n += 1
    return f"shd{n}"


def find_video_by_code(code):
    for v in data["videos"]:
        if v["code"] == code:
            return v
    return None


def human_timedelta(seconds):
    if seconds is None:
        return "N/A"
    if seconds < 60:
        return f"in {seconds}s"
    return f"in {seconds // 60}m"


def parse_datetime(text):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None


# -----------------------------
# TELEGRAM HANDLERS
# -----------------------------
ASK_SCHED_VIDEO, ASK_SCHED_CAPTION, ASK_SCHED_TIME = range(3)


def start_cmd(update, context):
    update.message.reply_text("🚀 Instagram Scheduler Bot ready!\nUse /viewallcmd for full command list.")


def help_cmd(update, context):
    update.message.reply_text("Use /viewallcmd to see all commands.")


@admin_only
def addvideo_start(update, context):
    update.message.reply_text("📥 Send video to add (normal).")
    context.user_data['add_type'] = 'normal'


@admin_only
def addpriority_start(update, context):
    update.message.reply_text("📥 Send video to add (PRIORITY).")
    context.user_data['add_type'] = 'priority'


def receive_video_for_add(update, context):
    msg = update.message
    if not msg or not (msg.video or msg.document):
        return
    add_type = context.user_data.get('add_type', 'normal')
    file = context.bot.get_file(msg.video.file_id if msg.video else msg.document.file_id)
    code = generate_vid_code()
    path = os.path.join(VIDEO_DIR, f"{code}.mp4")
    file.download(path)
    entry = {"code": code, "path": path, "type": add_type}
    data["videos"].append(entry)
    save_data(data)
    update.message.reply_text(f"✅ Saved as `{code}` (type: {add_type})", parse_mode="Markdown")
    context.user_data.pop('add_type', None)


def list_cmd(update, context):
    vs = data["videos"]
    if not vs:
        update.message.reply_text("No videos in queue.")
        return
    msg = "🎬 Videos:\n" + "\n".join(f"{v['code']} - {v['type']}" for v in vs)
    update.message.reply_text(msg)


def show_cmd(update, context):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /show <vid_code>")
        return
    code = args[0]
    v = find_video_by_code(code)
    if not v or not os.path.exists(v["path"]):
        update.message.reply_text("❌ Video not found.")
        return
    update.message.reply_video(video=open(v["path"], "rb"))


def remove_cmd(update, context):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /remove <vid_code>")
        return
    code = args[0]
    v = find_video_by_code(code)
    if not v:
        update.message.reply_text("❌ Not found.")
        return
    if os.path.exists(v["path"]):
        os.remove(v["path"])
    data["videos"] = [x for x in data["videos"] if x["code"] != code]
    save_data(data)
    update.message.reply_text(f"✅ Removed {code}")


def removepriority_cmd(update, context):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /removepriority <vid_code>")
        return
    code = args[0]
    v = find_video_by_code(code)
    if v:
        v["type"] = "normal"
        save_data(data)
        update.message.reply_text(f"✅ {code} downgraded to normal.")
    else:
        update.message.reply_text("❌ Not found.")


def setcaption_cmd(update, context):
    text = " ".join(context.args)
    if not text:
        update.message.reply_text("Usage: /setcaption <text>")
        return
    data["caption"] = text
    save_data(data)
    update.message.reply_text("✅ Caption set.")


def viewcaption_cmd(update, context):
    caption = data.get("caption", "")
    update.message.reply_text(f"📜 Caption: {caption or '(none)'}")


def removecaption_cmd(update, context):
    data["caption"] = ""
    save_data(data)
    update.message.reply_text("❌ Caption removed.")


def settimer_cmd(update, context):
    try:
        mn, mx = map(int, context.args)
        if mn < 10 or mx < mn:
            raise ValueError
        data["interval_min"], data["interval_max"] = mn, mx
        save_data(data)
        update.message.reply_text(f"⏱ Interval set to {mn}-{mx} sec.")
    except Exception:
        update.message.reply_text("Usage: /settimer <min> <max>")


def startposting_cmd(update, context):
    if data["is_running"]:
        update.message.reply_text("⚠ Already running.")
        return
    data["is_running"] = True
    next_t = datetime.now() + timedelta(seconds=random.randint(data["interval_min"], data["interval_max"]))
    data["next_queue_post_time"] = next_t.isoformat()
    save_data(data)
    update.message.reply_text("🚀 Auto-posting started.")


def stopposting_cmd(update, context):
    data["is_running"] = False
    data["next_queue_post_time"] = None
    save_data(data)
    update.message.reply_text("🛑 Auto-posting stopped.")


# -----------------------------
# BACKGROUND POSTING
# -----------------------------
def weighted_random_choice(videos):
    weighted = []
    for v in videos:
        weighted += [v] * (PRIORITY_WEIGHT if v["type"] == "priority" else 1)
    return random.choice(weighted) if weighted else None


def post_to_instagram(path, caption):
    try:
        cl = ig_login()
        if not cl:
            return False
        cl.video_upload(path, caption or "")
        print(f"✅ Posted {path}")
        return True
    except Exception as e:
        print(f"❌ Instagram post failed: {e}")
        return False


def background_worker():
    print("🧠 Background worker active.")
    while True:
        now = datetime.now()
        try:
            # Handle scheduled
            for s in list(data["scheduled"]):
                if s["status"] != "Pending":
                    continue
                post_time = datetime.fromisoformat(s["datetime"])
                if now >= post_time:
                    ok = post_to_instagram(s["video_path"], s["caption"] or data["caption"])
                    if ok:
                        s["status"] = "Posted"
                        data["last_post"] = {"video_code": s["shd_code"], "time": now.isoformat()}
                        save_data(data)
                    else:
                        time.sleep(INSTAPOST_SLEEP_AFTER_FAIL)

            # Handle queue
            if data["is_running"]:
                next_iso = data["next_queue_post_time"]
                if not next_iso or now >= datetime.fromisoformat(next_iso):
                    v = weighted_random_choice(data["videos"])
                    if v:
                        ok = post_to_instagram(v["path"], data["caption"])
                        next_dt = now + timedelta(seconds=random.randint(data["interval_min"], data["interval_max"]))
                        data["next_queue_post_time"] = next_dt.isoformat()
                        if ok:
                            data["last_post"] = {"video_code": v["code"], "time": now.isoformat()}
                        save_data(data)
            time.sleep(5)
        except Exception as e:
            print("⚠ Worker error:", e)
            time.sleep(5)


# -----------------------------
# /viewallcmd Command Reference
# -----------------------------
def viewallcmd_cmd(update, context):
    msg = (
        "📘 *COMMAND LIST*\n"
        "──────────────────────────────\n"
        "🎬 *VIDEO MANAGEMENT*\n"
        "`/addvideo` - Add normal video\n"
        "`/addpriority` - Add priority video\n"
        "`/list` - Show all videos\n"
        "`/show <vid>` - Show specific video\n"
        "`/remove <vid>` - Delete video\n"
        "`/removepriority <vid>` - Convert to normal\n\n"
        "🏷️ *CAPTION*\n"
        "`/setcaption <text>` - Set caption\n"
        "`/viewcaption` - View caption\n"
        "`/removecaption` - Remove caption\n\n"
        "🕒 *POSTING*\n"
        "`/settimer <min> <max>` - Set interval\n"
        "`/startposting` - Start auto post\n"
        "`/stopposting` - Stop auto post\n\n"
        "📊 *UTILITY*\n"
        "`/status` - View status\n"
        "`/viewallcmd` - Show this help\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


# -----------------------------
# STATUS COMMAND
# -----------------------------
def status_cmd(update, context):
    now = datetime.now()
    next_iso = data.get("next_queue_post_time")
    next_in = None
    if next_iso:
        try:
            dt = datetime.fromisoformat(next_iso)
            next_in = int((dt - now).total_seconds())
        except:
            next_in = None

    msg = (
        f"📊 *Status*\n"
        f"Running: {'✅' if data['is_running'] else '❌'}\n"
        f"Next Post: {human_timedelta(next_in)}\n"
        f"Videos: {len(data['videos'])}\n"
        f"Caption: {data.get('caption') or '(none)'}"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


# -----------------------------
# MAIN FUNCTION
# -----------------------------
def main():
    print("🚀 Starting bot (webhook mode)...")

    # KEEP ALIVE THREAD
    threading.Thread(target=keep_alive_ping, daemon=True).start()

    # BACKGROUND WORKER
    threading.Thread(target=background_worker, daemon=True).start()

    # TELEGRAM BOT SETUP
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Command handlers
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))
    dp.add_handler(CommandHandler("list", list_cmd))
    dp.add_handler(CommandHandler("show", show_cmd, pass_args=True))
    dp.add_handler(CommandHandler("remove", remove_cmd, pass_args=True))
    dp.add_handler(CommandHandler("removepriority", removepriority_cmd, pass_args=True))
    dp.add_handler(CommandHandler("setcaption", setcaption_cmd, pass_args=True))
    dp.add_handler(CommandHandler("viewcaption", viewcaption_cmd))
    dp.add_handler(CommandHandler("removecaption", removecaption_cmd))
    dp.add_handler(CommandHandler("settimer", settimer_cmd, pass_args=True))
    dp.add_handler(CommandHandler("startposting", startposting_cmd))
    dp.add_handler(CommandHandler("stopposting", stopposting_cmd))
    dp.add_handler(CommandHandler("status", status_cmd))
    dp.add_handler(CommandHandler("addvideo", addvideo_start))
    dp.add_handler(CommandHandler("addpriority", addpriority_start))
    dp.add_handler(MessageHandler(Filters.video | Filters.document, receive_video_for_add))

    # -----------------------------------
    # ✅ SWITCH TO WEBHOOK MODE
    # -----------------------------------
    PORT = int(os.environ.get('PORT', '8443'))
    APP_URL = os.getenv("MY_RENDER_URL")
    if not APP_URL:
        raise RuntimeError("❌ MY_RENDER_URL environment variable not set!")

    updater.bot.delete_webhook()
    time.sleep(1)
    updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{APP_URL}/{BOT_TOKEN}"
    )

    print(f"✅ Webhook started at {APP_URL}/{BOT_TOKEN}")
    updater.idle()


if __name__ == "__main__":
    main()

