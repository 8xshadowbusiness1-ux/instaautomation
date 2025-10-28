"""
instaautomation.py - Webhook + Full Commands (Render optimized)

Features:
- Full command set (addvideo, addpriority, list, show, remove, removepriority, setcaption,
  viewcaption, removecaption, settimer, startposting, stopposting, schedule, listscheduled,
  removescheduled, status, viewallcmd, help, start, login).
- Webhook mode for Telegram (Flask route) — no polling, no getUpdates conflict.
- Background worker for scheduled & queue posting preserved.
- Safe: loads secrets from environment (do NOT hardcode).
- Keep-alive self-ping thread (set MY_RENDER_URL).
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
# CONFIG - via ENVIRONMENT (safe)
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
MY_RENDER_URL = os.getenv("MY_RENDER_URL", "").rstrip("/")

DATA_FILE = "data.json"
VIDEO_DIR = "videos"
START_PORT = int(os.getenv("PORT", 10000))

# posting behavior
PRIORITY_WEIGHT = int(os.getenv("PRIORITY_WEIGHT", 3))
INSTAPOST_SLEEP_AFTER_FAIL = int(os.getenv("INSTAPOST_SLEEP_AFTER_FAIL", 30))

# -----------------------------
# Ensure directories
# -----------------------------
os.makedirs(VIDEO_DIR, exist_ok=True)

# -----------------------------
# Default data structure
# -----------------------------
DEFAULT_DATA = {
    "caption": "",
    "interval_min": 1800,
    "interval_max": 3600,
    "videos": [],         # list of {"code", "path", "type": "normal"|"priority"}
    "scheduled": [],      # list of {"shd_code", "video_path", "datetime", "caption", "status"}
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
# Instagram client (instagrapi)
# -----------------------------
ig_client = None
ig_lock = threading.Lock()


def ig_login(force=False):
    global ig_client
    with ig_lock:
        if ig_client is None or force:
            ig_client = Client()
            try:
                if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
                    print("Instagram credentials not set in environment.")
                    ig_client = None
                    return None
                ig_client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                print("✅ Instagram: logged in.")
            except Exception as e:
                print("⚠️ Instagram login error:", e)
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
        user_id = update.effective_user.id
        if user_id != ADMIN_CHAT_ID:
            update.message.reply_text("❌ You are not authorized to use this bot.")
            return
        return func(update, context, *args, **kwargs)
    return wrapper


def generate_vid_code():
    existing = {v["code"] for v in data.get("videos", [])}
    n = 1
    while True:
        code = f"vid{n}"
        if code not in existing:
            return code
        n += 1


def generate_shd_code():
    existing = {s["shd_code"] for s in data.get("scheduled", [])}
    n = 1
    while True:
        code = f"shd{n}"
        if code not in existing:
            return code
        n += 1


def find_video_by_code(code):
    for v in data.get("videos", []):
        if v.get("code") == code:
            return v
    return None


def human_timedelta_seconds(seconds):
    if seconds is None:
        return "N/A"
    if seconds <= 60:
        return f"in {seconds} seconds"
    mins = seconds // 60
    return f"in {mins} mins"


def parse_datetime(text):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None


# -----------------------------
# Conversation states
# -----------------------------
ASK_SCHED_VIDEO, ASK_SCHED_CAPTION, ASK_SCHED_TIME = range(3)

# -----------------------------
# Telegram handlers
# -----------------------------
def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🚀 InstaAutomation Bot ready. Use /viewallcmd to see commands.")


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("Use /viewallcmd to see all commands.")


# Add Video flows
@admin_only
def addvideo_start(update: Update, context: CallbackContext):
    update.message.reply_text("📥 Please send the video file you want to add to queue (normal).")
    context.user_data['add_type'] = 'normal'


@admin_only
def addpriority_start(update: Update, context: CallbackContext):
    update.message.reply_text("📥 Please send the video file you want to add as PRIORITY.")
    context.user_data['add_type'] = 'priority'


def receive_video_for_add(update: Update, context: CallbackContext):
    msg = update.message
    if not msg:
        return
    if not (msg.video or msg.document):
        return
    add_type = context.user_data.get('add_type', 'normal')
    file_obj = msg.video or msg.document
    file_id = file_obj.file_id
    new_code = generate_vid_code()
    path = os.path.join(VIDEO_DIR, f"{new_code}.mp4")
    try:
        file = context.bot.get_file(file_id)
        file.download(custom_path=path)
    except Exception as e:
        update.message.reply_text(f"❌ Failed to download video: {e}")
        context.user_data.pop('add_type', None)
        return
    entry = {"code": new_code, "path": path, "type": add_type}
    data["videos"].append(entry)
    save_data(data)
    update.message.reply_text(f"✅ Video saved as `{new_code}` (type: {add_type})", parse_mode="Markdown")
    context.user_data.pop('add_type', None)


def list_cmd(update: Update, context: CallbackContext):
    vs = data.get("videos", [])
    if not vs:
        update.message.reply_text("No videos in queue.")
        return
    lines = ["🎬 Video List:"]
    for v in vs:
        lines.append(f"{v['code']} - {v['type']}")
    update.message.reply_text("\n".join(lines))


def show_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /show <vid_code>")
        return
    code = args[0].strip()
    v = find_video_by_code(code)
    if not v:
        update.message.reply_text(f"❌ Video {code} not found.")
        return
    path = v.get("path")
    if not path or not os.path.exists(path):
        update.message.reply_text(f"❌ Video file missing on server: {path}")
        return
    try:
        update.message.reply_video(video=open(path, "rb"))
    except Exception as e:
        update.message.reply_text(f"❌ Failed to send video: {e}")


def remove_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /remove <vid_code>")
        return
    code = args[0].strip()
    v = find_video_by_code(code)
    if not v:
        update.message.reply_text(f"❌ Video {code} not found.")
        return
    try:
        if os.path.exists(v.get("path", "")):
            os.remove(v.get("path"))
    except Exception:
        pass
    data["videos"] = [x for x in data.get("videos", []) if x.get("code") != code]
    save_data(data)
    update.message.reply_text(f"✅ Removed video {code}.")


def removepriority_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /removepriority <vid_code>")
        return
    code = args[0].strip()
    v = find_video_by_code(code)
    if not v:
        update.message.reply_text(f"❌ Video {code} not found.")
        return
    v["type"] = "normal"
    save_data(data)
    update.message.reply_text(f"✅ {code} is now normal.")


# Caption commands
def setcaption_cmd(update: Update, context: CallbackContext):
    text = " ".join(context.args).strip()
    if not text and update.message.text and update.message.text != "/setcaption":
        text = update.message.text.replace("/setcaption", "").strip()
    if not text:
        update.message.reply_text("Usage: /setcaption <text>")
        return
    data["caption"] = text
    save_data(data)
    update.message.reply_text("✅ Caption updated.")


def viewcaption_cmd(update: Update, context: CallbackContext):
    caption = data.get("caption", "")
    if caption:
        update.message.reply_text(f"📜 Current Caption:\n\"{caption}\"")
    else:
        update.message.reply_text("⚠️ No caption set yet. Use /setcaption to add one.")


def removecaption_cmd(update: Update, context: CallbackContext):
    data["caption"] = ""
    save_data(data)
    update.message.reply_text("❌ Caption removed. (Posts will be uploaded without captions.)")


# Timer & Start/Stop
def settimer_cmd(update: Update, context: CallbackContext):
    args = context.args
    if len(args) != 2:
        update.message.reply_text("Usage: /settimer <min_seconds> <max_seconds>")
        return
    try:
        mn = int(args[0]); mx = int(args[1])
        if mn < 10 or mx < mn:
            update.message.reply_text("Invalid values. Keep min >= 10 and max >= min.")
            return
        data["interval_min"] = mn
        data["interval_max"] = mx
        save_data(data)
        update.message.reply_text(f"⏱️ Interval set to {mn}–{mx} sec.")
    except Exception:
        update.message.reply_text("Invalid numbers.")


def startposting_cmd(update: Update, context: CallbackContext):
    if data.get("is_running"):
        update.message.reply_text("⚠️ Already running.")
        return
    data["is_running"] = True
    next_t = datetime.now() + timedelta(seconds=random.randint(data["interval_min"], data["interval_max"]))
    data["next_queue_post_time"] = next_t.isoformat()
    save_data(data)
    update.message.reply_text("🚀 Auto-posting started.")


def stopposting_cmd(update: Update, context: CallbackContext):
    if not data.get("is_running"):
        update.message.reply_text("⚠️ Not running.")
        return
    data["is_running"] = False
    data["next_queue_post_time"] = None
    save_data(data)
    update.message.reply_text("🛑 Auto-posting stopped.")


# -----------------------------
# Scheduling conversation (video -> caption -> time)
# -----------------------------
def schedule_start(update: Update, context: CallbackContext):
    update.message.reply_text("🎥 Please send the video or post you want to schedule.")
    return ASK_SCHED_VIDEO


def schedule_receive_video(update: Update, context: CallbackContext):
    msg = update.message
    if not msg or not (msg.video or msg.document):
        update.message.reply_text("❌ Please send a video file.")
        return ASK_SCHED_VIDEO
    file_obj = msg.video or msg.document
    file_id = file_obj.file_id
    shd_tmp_code = generate_shd_code()
    path = os.path.join(VIDEO_DIR, f"{shd_tmp_code}.mp4")
    try:
        file = context.bot.get_file(file_id)
        file.download(custom_path=path)
    except Exception as e:
        update.message.reply_text(f"❌ Failed to download video: {e}")
        return ConversationHandler.END
    context.user_data['sched_video_path'] = path
    update.message.reply_text("✍️ Please type your caption for this scheduled post (or send empty message for no caption).")
    return ASK_SCHED_CAPTION


def schedule_receive_caption(update: Update, context: CallbackContext):
    caption_text = update.message.text or ""
    context.user_data['sched_caption'] = caption_text
    update.message.reply_text("⏰ Great! Now please type the time of post (format: YYYY-MM-DD HH:MM).")
    return ASK_SCHED_TIME


def schedule_receive_time(update: Update, context: CallbackContext):
    time_text = update.message.text
    dt = parse_datetime(time_text)
    if not dt:
        update.message.reply_text("❌ Invalid datetime format. Use YYYY-MM-DD HH:MM")
        return ASK_SCHED_TIME
    shd_code = generate_shd_code()
    video_path = context.user_data.get('sched_video_path')
    caption = context.user_data.get('sched_caption', "")
    entry = {
        "shd_code": shd_code,
        "video_path": video_path,
        "datetime": dt.isoformat(),
        "caption": caption,
        "status": "Pending"
    }
    data["scheduled"].append(entry)
    save_data(data)
    update.message.reply_text(
        f"🗓️ Scheduled successfully!\n"
        f"• Code: {shd_code}\n"
        f"• Time: {dt.strftime('%Y-%m-%d %H:%M')}\n"
        f"• Caption: \"{caption}\"\n"
        f"• Status: ⏳ Pending"
    )
    context.user_data.pop('sched_video_path', None)
    context.user_data.pop('sched_caption', None)
    return ConversationHandler.END


def listscheduled_cmd(update: Update, context: CallbackContext):
    sch = data.get("scheduled", [])
    if not sch:
        update.message.reply_text("No scheduled posts.")
        return
    lines = ["🗓️ Scheduled Posts:"]
    for s in sch:
        dt = s.get("datetime")
        status = s.get("status", "Pending")
        caption = s.get("caption", "")
        lines.append(f"• {s['shd_code']} → {dt} → {status}\n   Caption: \"{caption}\"")
    update.message.reply_text("\n".join(lines))


def removescheduled_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /removescheduled <shd_code>")
        return
    code = args[0].strip()
    exists = [s for s in data.get("scheduled", []) if s.get("shd_code") == code]
    if not exists:
        update.message.reply_text(f"❌ Scheduled {code} not found.")
        return
    entry = exists[0]
    try:
        if os.path.exists(entry.get("video_path", "")):
            os.remove(entry.get("video_path"))
    except Exception:
        pass
    data["scheduled"] = [s for s in data.get("scheduled", []) if s.get("shd_code") != code]
    save_data(data)
    update.message.reply_text(f"✅ Removed scheduled {code}.")


# -----------------------------
# Status dashboard
# -----------------------------
def status_cmd(update: Update, context: CallbackContext):
    now = datetime.now()
    mode = "Running ✅" if data.get("is_running") else "Stopped ❌"

    # earliest pending scheduled post (future)
    next_scheduled = None
    for s in data.get("scheduled", []):
        if s.get("status") == "Pending":
            try:
                dt = datetime.fromisoformat(s["datetime"])
            except Exception:
                continue
            if dt >= now:
                if next_scheduled is None or dt < datetime.fromisoformat(next_scheduled["datetime"]):
                    next_scheduled = s

    next_queue_delta = None
    next_queue_iso = data.get("next_queue_post_time")
    if next_queue_iso:
        try:
            t = datetime.fromisoformat(next_queue_iso)
            if t > now:
                next_queue_delta = int((t - now).total_seconds())
            else:
                next_queue_delta = 0
        except:
            next_queue_delta = None

    total_videos = len(data.get("videos", []))
    priority_videos = sum(1 for v in data.get("videos", []) if v.get("type") == "priority")
    mn = data.get("interval_min")
    mx = data.get("interval_max")
    caption = data.get("caption") or "(none)"
    last_post = data.get("last_post", {})
    last_post_time = last_post.get("time")
    last_post_code = last_post.get("video_code")

    lines = []
    lines.append("📊 STATUS DASHBOARD")
    lines.append("──────────────────────────────")
    lines.append(f"• Mode: {mode}")
    # Next Post logic
    next_post_text = "N/A"
    if next_queue_delta is not None:
        next_post_text = human_timedelta_seconds(next_queue_delta)
    if next_scheduled:
        dt = datetime.fromisoformat(next_scheduled["datetime"])
        cand = f"Scheduled {next_scheduled['shd_code']} at {dt.strftime('%Y-%m-%d %H:%M')}"
        if next_queue_delta is not None:
            try:
                next_queue_abs = datetime.fromisoformat(next_queue_iso)
                if next_queue_abs <= dt:
                    lines.append(f"• Next Post: {next_post_text}")
                else:
                    lines.append(f"• Next Post: {cand}")
            except:
                lines.append(f"• Next Post: {cand}")
        else:
            lines.append(f"• Next Post: {cand}")
    else:
        lines.append(f"• Next Post: {next_post_text}")

    lines.append(f"• Total Videos: {total_videos}")
    lines.append(f"• Priority Videos: {priority_videos}")
    lines.append(f"• Interval: {mn}–{mx} sec")
    lines.append(f"• Caption: \"{caption}\"")
    lines.append("──────────────────────────────")

    sch = data.get("scheduled", [])
    if sch:
        lines.append("🗓️ Scheduled Posts:")
        for s in sch:
            status = s.get("status", "Pending")
            dt = s.get("datetime")
            lines.append(f"   • {s['shd_code']} → {dt} → {status}")
    else:
        lines.append("🗓️ Scheduled Posts: None")

    lines.append("──────────────────────────────")
    lines.append("📦 Queue Details:")
    lines.append(f"   • Normal Videos: {total_videos - priority_videos}")
    lines.append(f"   • Priority Videos: {priority_videos}")
    lines.append("──────────────────────────────")
    lines.append(f"⏱️ Last Post: {last_post_time}")
    lines.append(f"• Last Posted Video: {last_post_code}")
    lines.append("──────────────────────────────")
    update.message.reply_text("\n".join(lines))


# -----------------------------
# Posting logic (background worker)
# -----------------------------
def weighted_random_choice(videos):
    weighted = []
    for v in videos:
        if v.get("type") == "priority":
            weighted.extend([v] * PRIORITY_WEIGHT)
        else:
            weighted.append(v)
    return random.choice(weighted) if weighted else None


def post_to_instagram(video_path, caption_text):
    try:
        client = ig_login()
        if client is None:
            print("IG client unavailable.")
            return False
        client.video_upload(video_path, caption_text or "")
        print("Posted to IG:", video_path)
        return True
    except Exception as e:
        print("Instagram upload error:", e)
        return False


def background_worker():
    print("Background worker running.")
    while True:
        try:
            now = datetime.now()
            # 1) scheduled posts
            with data_lock:
                scheduled_copy = list(data.get("scheduled", []))
            for s in scheduled_copy:
                try:
                    if s.get("status") != "Pending":
                        continue
                    post_time = datetime.fromisoformat(s.get("datetime"))
                    if now >= post_time:
                        caption = s.get("caption") or data.get("caption", "")
                        success = post_to_instagram(s.get("video_path"), caption)
                        if success:
                            with data_lock:
                                for el in data["scheduled"]:
                                    if el["shd_code"] == s["shd_code"]:
                                        el["status"] = "Posted"
                                data["last_post"] = {"video_code": s.get("shd_code"), "time": datetime.now().isoformat()}
                                save_data(data)
                        else:
                            print("Scheduled post failed; retrying later.")
                            time.sleep(INSTAPOST_SLEEP_AFTER_FAIL)
                except Exception as e:
                    print("Error in scheduled loop:", e)

            # 2) queue posting if enabled
            if data.get("is_running"):
                next_iso = data.get("next_queue_post_time")
                do_post = False
                if next_iso:
                    try:
                        next_dt = datetime.fromisoformat(next_iso)
                        if now >= next_dt:
                            do_post = True
                    except:
                        do_post = True
                else:
                    do_post = True

                if do_post:
                    with data_lock:
                        vids_copy = list(data.get("videos", []))
                    chosen = weighted_random_choice(vids_copy) if vids_copy else None
                    if chosen:
                        caption_use = data.get("caption", "")
                        success = post_to_instagram(chosen.get("path"), caption_use)
                        if success:
                            with data_lock:
                                data["last_post"] = {"video_code": chosen.get("code"), "time": datetime.now().isoformat()}
                                next_interval = random.randint(data.get("interval_min", 1800), data.get("interval_max", 3600))
                                next_dt = datetime.now() + timedelta(seconds=next_interval)
                                data["next_queue_post_time"] = next_dt.isoformat()
                                save_data(data)
                            print(f"Posted {chosen.get('code')} — next in {next_interval}s")
                        else:
                            print("Queue post failed; scheduling retry.")
                            time.sleep(INSTAPOST_SLEEP_AFTER_FAIL)
                            with data_lock:
                                data["next_queue_post_time"] = (datetime.now() + timedelta(seconds=60)).isoformat()
                                save_data(data)
                    else:
                        with data_lock:
                            data["next_queue_post_time"] = (datetime.now() + timedelta(seconds=60)).isoformat()
                            save_data(data)

            time.sleep(5)
        except Exception as e:
            print("Background worker exception:", e)
            time.sleep(5)


# -----------------------------
# Command reference (/viewallcmd)
# -----------------------------
def viewallcmd_cmd(update: Update, context: CallbackContext):
    msg = (
        "📘 *COMMAND REFERENCE*\n"
        "──────────────────────────────\n"
        "🎬 *VIDEO MANAGEMENT*\n"
        "`/addvideo` — Add a normal video to the queue.\n"
        "`/addpriority` — Add a priority video (higher posting chance).\n"
        "`/list` — List all queued videos.\n"
        "`/show <vid_code>` — View any specific video.\n"
        "`/remove <vid_code>` — Delete a video from queue.\n"
        "`/removepriority <vid_code>` — Convert a priority video to normal.\n\n"
        "🕒 *POSTING CONTROL*\n"
        "`/settimer <min> <max>` — Set random posting interval (seconds).\n"
        "`/startposting` — Start automatic posting.\n"
        "`/stopposting` — Stop posting.\n"
        "`/status` — View complete system dashboard.\n\n"
        "🏷️ *CAPTION CONTROL*\n"
        "`/setcaption <text>` — Set global caption for all posts.\n"
        "`/viewcaption` — Show current global caption.\n"
        "`/removecaption` — Remove global caption.\n\n"
        "🗓️ *SCHEDULING SYSTEM*\n"
        "`/schedule` — Schedule a one-time post (interactive: video → caption → time).\n"
        "`/listscheduled` — View all scheduled posts.\n"
        "`/removescheduled <shd_code>` — Delete a scheduled post.\n\n"
        "📊 *UTILITY*\n"
        "`/viewallcmd` — Show this command reference panel.\n"
        "`/help` — Quick command list.\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


# -----------------------------
# Flask + Telegram webhook setup
# -----------------------------
app = Flask(__name__)
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set in environment. Exiting.")
    raise SystemExit("BOT_TOKEN not set")

bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher(bot, None, use_context=True)

# Register handlers to dispatcher
dispatcher.add_handler(CommandHandler("start", start_cmd))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))

# Add video flows
dispatcher.add_handler(CommandHandler("addvideo", addvideo_start))
dispatcher.add_handler(CommandHandler("addpriority", addpriority_start))
dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, receive_video_for_add), group=1)

# Video management
dispatcher.add_handler(CommandHandler("list", list_cmd))
dispatcher.add_handler(CommandHandler("show", show_cmd))
dispatcher.add_handler(CommandHandler("remove", remove_cmd))
dispatcher.add_handler(CommandHandler("removepriority", removepriority_cmd))

# Caption
dispatcher.add_handler(CommandHandler("setcaption", setcaption_cmd))
dispatcher.add_handler(CommandHandler("viewcaption", viewcaption_cmd))
dispatcher.add_handler(CommandHandler("removecaption", removecaption_cmd))

# Timer & posting
dispatcher.add_handler(CommandHandler("settimer", settimer_cmd))
dispatcher.add_handler(CommandHandler("startposting", startposting_cmd))
dispatcher.add_handler(CommandHandler("stopposting", stopposting_cmd))

# Schedule conversation
conv_handler = ConversationHandler(
    entry_points=[CommandHandler('schedule', schedule_start)],
    states={
        ASK_SCHED_VIDEO: [MessageHandler(Filters.video | Filters.document, schedule_receive_video)],
        ASK_SCHED_CAPTION: [MessageHandler(Filters.text & ~Filters.command, schedule_receive_caption),
                            MessageHandler(Filters.command, schedule_receive_caption)],
        ASK_SCHED_TIME: [MessageHandler(Filters.text & ~Filters.command, schedule_receive_time)]
    },
    fallbacks=[],
    allow_reentry=True
)
dispatcher.add_handler(conv_handler)

dispatcher.add_handler(CommandHandler("listscheduled", listscheduled_cmd))
dispatcher.add_handler(CommandHandler("removescheduled", removescheduled_cmd))
dispatcher.add_handler(CommandHandler("status", status_cmd))

# Basic webhook route
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive!", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(update)
    except Exception as e:
        print("Webhook processing error:", e)
    return "ok", 200


def keep_alive_ping(url):
    while True:
        try:
            requests.get(url, timeout=20)
            print(f"🔁 Self ping sent to {url}")
        except Exception as e:
            print(f"Ping failed: {e}")
        time.sleep(3600)


# -----------------------------
# /login command (Instagram)
# -----------------------------
def login_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🔐 Trying to log in to Instagram...")
    cl = ig_login(force=True)
    if cl:
        update.message.reply_text(f"✅ Logged in as {INSTAGRAM_USERNAME}")
    else:
        update.message.reply_text("⚠️ Login failed. Check IG credentials or challenge/2FA.")


dispatcher.add_handler(CommandHandler("login", login_cmd))


# -----------------------------
# Start background worker & Flask
# -----------------------------
if __name__ == "__main__":
    # Start Telegram Bot
    print("🤖 Starting Telegram bot...")
    updater.start_polling()
    print("✅ Bot started (Telegram polling)")

    # Start background worker
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()

    # Start self-ping if MY_RENDER_URL set
    if MY_RENDER_URL and "YOUR-RENDER" not in MY_RENDER_URL:
        ping_thread = threading.Thread(target=keep_alive_ping, args=(MY_RENDER_URL,), daemon=True)
        ping_thread.start()
    else:
        print("⚠️ Warning: MY_RENDER_URL not set or placeholder. Set it to your deployed app URL to enable keep-alive.")

    # Start Flask Server (keep-alive)
    print("🚀 Starting Flask webhook server...")
    app.run(host="0.0.0.0", port=START_PORT)
