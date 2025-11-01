#!/usr/bin/env python3
"""
instagram_scheduler_bot - final single-file script (with /viewallcmd)
All previous commands preserved + AutoZMode (auto repost from a target IG account)
Keep-alive ping included (MY_RENDER_URL env var)
Supports polling (deletes webhook first) and optional webhook mode via WEBHOOK_URL
"""

import os
import json
import threading
import time
import random
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

# Instagram + Telegram libs
from instagrapi import Client
from telegram import Update, Bot
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)
import requests

# =========================
# CONFIG from env (or edit here)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "YOUR_IG_USERNAME")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "YOUR_IG_PASSWORD")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_ADMIN_ID")  # string -> convert to int later if set
if ADMIN_CHAT_ID:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
    except:
        ADMIN_CHAT_ID = None

DATA_FILE = os.getenv("DATA_FILE", "data.json")
VIDEO_DIR = os.getenv("VIDEO_DIR", "videos")  # ensure relative dir (not /data)
INSTAPOST_SLEEP_AFTER_FAIL = int(os.getenv("INSTAPOST_SLEEP_AFTER_FAIL", "30"))
PRIORITY_WEIGHT = int(os.getenv("PRIORITY_WEIGHT", "3"))

# Keep-alive URL (render/other) - ping every 10 minutes if set
MY_RENDER_URL = os.getenv("MY_RENDER_URL", None)

# Telegram webhook optional:
WEBHOOK_URL = os.getenv("WEBHOOK_URL", None)  # if provided, will attempt to set webhook (full URL)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", None)
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "10000"))

# AutoZMode defaults (can be changed with commands)
AUTOZ_DEFAULT_TARGET = os.getenv("AUTOZ_TARGET", "")  # username
AUTOZ_DEFAULT_MIN = int(os.getenv("AUTOZ_MIN", "1800"))   # seconds
AUTOZ_DEFAULT_MAX = int(os.getenv("AUTOZ_MAX", "3600"))

# =========================
# Ensure directories
# =========================
os.makedirs(VIDEO_DIR, exist_ok=True)

# =========================
# Default data structure
# =========================
DEFAULT_DATA = {
    "caption": "",
    "interval_min": 1800,
    "interval_max": 3600,
    "videos": [],  # {code, path, type}
    "scheduled": [],  # {shd_code, video_path, datetime, caption, status}
    "last_post": {"video_code": None, "time": None},
    "is_running": False,
    "next_queue_post_time": None,
    # AutoZMode
    "autoz": {
        "enabled": False,
        "target_username": AUTOZ_DEFAULT_TARGET,
        "min_interval": AUTOZ_DEFAULT_MIN,
        "max_interval": AUTOZ_DEFAULT_MAX,
        "caption": "",
        "seen_media_ids": []  # track media ids we've already downloaded/posted
    }
}

data_lock = threading.Lock()

# =========================
# Data load/save
# =========================
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
    # fill missing keys
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    # ensure nested autoz keys
    if "autoz" not in d:
        d["autoz"] = DEFAULT_DATA["autoz"].copy()
    else:
        for k, v in DEFAULT_DATA["autoz"].items():
            if k not in d["autoz"]:
                d["autoz"][k] = v
    return d

def save_data(d):
    with data_lock:
        with open(DATA_FILE, "w") as f:
            json.dump(d, f, indent=2, default=str)

data = load_data()

# =========================
# Instagram client (lazy)
# =========================
ig_client = None
ig_lock = threading.Lock()

def ig_login():
    global ig_client
    with ig_lock:
        if ig_client is not None:
            return ig_client
        client = Client()
        client.settings_delay_range = (1, 2)
        try:
            # try load session from file if exists
            session_file = f"ig_{INSTAGRAM_USERNAME}.json"
            if os.path.exists(session_file):
                try:
                    client.load_settings(session_file)
                except Exception:
                    pass
            client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            # save settings
            try:
                client.dump_settings(session_file)
            except Exception:
                pass
            ig_client = client
            print("✅ Instagram: logged in.")
            return ig_client
        except Exception as e:
            print("❌ Instagram login failed:", e)
            traceback.print_exc()
            ig_client = None
            return None

# =========================
# Helpers
# =========================
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

# =========================
# Telegram Handlers / Commands
# =========================
ASK_SCHED_VIDEO, ASK_SCHED_CAPTION, ASK_SCHED_TIME = range(3)

def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🚀 Instagram Scheduler Bot ready. Use /viewallcmd for commands.")

def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("Use /viewallcmd to see all commands.")

# Add video (normal/priority) flow flags
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
        file.download(path)
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

# Schedule conversation (video -> caption -> time)
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
        file.download(path)
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

# Status dashboard
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
            next_queue_abs = datetime.fromisoformat(next_queue_iso)
            if next_queue_abs <= dt:
                lines.append(f"• Next Post: {next_post_text}")
            else:
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

    # AutoZMode status
    az = data.get("autoz", {})
    az_enabled = az.get("enabled", False)
    az_target = az.get("target_username", "")
    az_caption = az.get("caption", "")
    az_min = az.get("min_interval", AUTOZ_DEFAULT_MIN)
    az_max = az.get("max_interval", AUTOZ_DEFAULT_MAX)
    lines.append("⚙️ AutoZMode:")
    lines.append(f"  • Enabled: {'Yes' if az_enabled else 'No'}")
    lines.append(f"  • Target: {az_target or '(not set)'}")
    lines.append(f"  • Caption: \"{az_caption or '(none)'}\"")
    lines.append(f"  • Interval: {az_min}–{az_max} sec")
    lines.append("──────────────────────────────")

    update.message.reply_text("\n".join(lines))

# -----------------------------
# Posting logic & worker
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
        # instagrapi handles video metadata; ensure file exists
        if not os.path.exists(video_path):
            print("Video file missing:", video_path)
            return False
        client.video_upload(video_path, caption_text or "")
        print("Posted to IG:", video_path)
        return True
    except Exception as e:
        print("Instagram upload error:", e)
        traceback.print_exc()
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
                            print("Scheduled post failed; retrying after delay.")
                            time.sleep(INSTAPOST_SLEEP_AFTER_FAIL)
                except Exception as e:
                    print("Error in scheduled loop:", e)
                    traceback.print_exc()

            # 2) AutoZMode posting (prioritized before queue)
            az = data.get("autoz", {})
            if az.get("enabled"):
                # Simple schedule: pick next based on random interval; but ensure we post only when time arrived
                # We'll check next_queue_post_time for AutoZ priority if set to now or earlier.
                # For autoz, we maintain next_queue_post_time same as queue - share the same trigger
                pass  # handled in the unified "do_post" below

            # 3) queue (and AutoZMode) unified trigger
            if data.get("is_running") or data.get("autoz", {}).get("enabled"):
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
                    # Priority: if AutoZ is enabled, attempt AutoZMode first (download from target and post)
                    az = data.get("autoz", {})
                    az_enabled = az.get("enabled", False)
                    az_target = az.get("target_username", "")
                    if az_enabled and az_target:
                        try:
                            # run AutoZ once (download a random new video from target, post it)
                            performed_autoz = autoz_fetch_and_post_once(az_target, az.get("caption", ""))
                            if performed_autoz:
                                # schedule next
                                with data_lock:
                                    next_interval = random.randint(az.get("min_interval", AUTOZ_DEFAULT_MIN), az.get("max_interval", AUTOZ_DEFAULT_MAX))
                                    data["next_queue_post_time"] = (datetime.now() + timedelta(seconds=next_interval)).isoformat()
                                    data["last_post"] = {"video_code": "autoz", "time": datetime.now().isoformat()}
                                    save_data(data)
                                print(f"AutoZMode posted — next in {next_interval}s")
                                time.sleep(1)
                                continue
                        except Exception as e:
                            print("AutoZMode error:", e)
                            traceback.print_exc()
                            # fall through to normal queue

                    # Normal queue posting
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

            time.sleep(4)
        except Exception as e:
            print("Background worker exception:", e)
            traceback.print_exc()
            time.sleep(5)

# =========================
# AutoZMode: fetch from target IG user and post
# =========================
def autoz_fetch_and_post_once(target_username, caption_override=None):
    """
    Fetch a recent media from target_username that we haven't seen, download it (video only),
    save into VIDEO_DIR as autoz_<media_id>.mp4 and post to IG.
    Returns True if it downloaded+posted a new media.
    """
    client = ig_login()
    if client is None:
        print("AutoZMode: IG client not available.")
        return False
    try:
        uid = client.user_id_from_username(target_username)
    except Exception as e:
        print("AutoZMode: cannot resolve username:", e)
        return False

    # fetch recent medias (videos preferred)
    try:
        medias = client.user_medias(uid, amount=20)  # adjust amount if desired
    except Exception as e:
        print("AutoZMode: failed to fetch medias:", e)
        return False

    # iterate and find first unseen video
    seen = set(data.get("autoz", {}).get("seen_media_ids", []))
    candidate = None
    for m in medias:
        # check type: 2 = video, 8 = album (may contain video) - we'll try to handle video
        media_id = str(m.id)
        if media_id in seen:
            continue
        # check if media has video_url
        try:
            if getattr(m, "media_type", None) == 2 or getattr(m, "view_count", None) is not None:
                # treat as video if instagrapi marks it as such
                candidate = m
                break
            # alternative: check resource_type or video_url
            if hasattr(m, "video_url") and m.video_url:
                candidate = m
                break
            # albums: try to find video within album
            if getattr(m, "media_type", None) == 8:
                # try to fetch children
                try:
                    children = client.media_galleries(m.id)
                except Exception:
                    children = []
                for c in children:
                    if getattr(c, "media_type", None) == 2 or hasattr(c, "video_url"):
                        candidate = m
                        break
                if candidate:
                    break
        except Exception:
            continue

    if not candidate:
        print("AutoZMode: no new candidate video found.")
        return False

    # download video to file
    media_id = str(candidate.id)
    # prefer candidate.video_url if available
    video_url = getattr(candidate, "video_url", None)
    # if not, try to get high resolution url via client.media_download? instagrapi has client.video_download but we'll use client.video_download to path
    filename = os.path.join(VIDEO_DIR, f"autoz_{media_id}.mp4")
    try:
        if video_url:
            # direct fetch
            r = requests.get(video_url, stream=True, timeout=30)
            if r.status_code == 200:
                with open(filename, "wb") as fh:
                    for chunk in r.iter_content(1024 * 64):
                        fh.write(chunk)
            else:
                # fallback to instagrapi download helper
                client.video_download(candidate.pk, filename)
        else:
            # try instagrapi's video_download
            client.video_download(candidate.pk, filename)
    except Exception as e:
        print("AutoZMode: download failed:", e)
        traceback.print_exc()
        return False

    # update seen list
    with data_lock:
        if media_id not in data["autoz"]["seen_media_ids"]:
            data["autoz"]["seen_media_ids"].append(media_id)
            save_data(data)

    # Post it using instagrapi (but we already used same client)
    caption_text = caption_override if caption_override is not None else data.get("autoz", {}).get("caption", "") or data.get("caption", "")
    success = post_to_instagram(filename, caption_text)
    if success:
        print("AutoZMode: posted", filename)
        return True
    else:
        print("AutoZMode: failed to post", filename)
        return False

# =========================
# AutoZMode commands
# =========================
def autoz_set_target_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /setautoz <username>")
        return
    username = args[0].strip().lstrip("@")
    data["autoz"]["target_username"] = username
    save_data(data)
    update.message.reply_text(f"✅ AutoZ target set to: {username}")

def autoz_view_cmd(update: Update, context: CallbackContext):
    az = data.get("autoz", {})
    update.message.reply_text(
        f"AutoZMode:\n"
        f"Enabled: {'Yes' if az.get('enabled') else 'No'}\n"
        f"Target: {az.get('target_username')}\n"
        f"Caption: \"{az.get('caption') or '(none)'}\"\n"
        f"Interval: {az.get('min_interval')}–{az.get('max_interval')} sec\n"
        f"Seen IDs: {len(az.get('seen_media_ids', []))}"
    )

def autoz_start_cmd(update: Update, context: CallbackContext):
    data["autoz"]["enabled"] = True
    save_data(data)
    update.message.reply_text("✅ AutoZMode enabled.")

def autoz_stop_cmd(update: Update, context: CallbackContext):
    data["autoz"]["enabled"] = False
    save_data(data)
    update.message.reply_text("⛔ AutoZMode disabled.")

def autoz_set_interval_cmd(update: Update, context: CallbackContext):
    args = context.args
    if len(args) != 2:
        update.message.reply_text("Usage: /setautozinterval <min_seconds> <max_seconds>")
        return
    try:
        mn = int(args[0]); mx = int(args[1])
        if mn < 10 or mx < mn:
            update.message.reply_text("Invalid values.")
            return
        data["autoz"]["min_interval"] = mn
        data["autoz"]["max_interval"] = mx
        save_data(data)
        update.message.reply_text(f"✅ AutoZ interval set to {mn}–{mx} sec.")
    except Exception:
        update.message.reply_text("Invalid numbers.")

def autoz_set_caption_cmd(update: Update, context: CallbackContext):
    text = " ".join(context.args).strip()
    if not text and update.message.text and update.message.text != "/setautozcaption":
        text = update.message.text.replace("/setautozcaption", "").strip()
    data["autoz"]["caption"] = text
    save_data(data)
    update.message.reply_text("✅ AutoZ caption set.")

# =========================
# Command Reference
# =========================
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
        "⚙️ *AUTOZMODE (Auto repost from target IG user)*\n"
        "`/setautoz <username>` — Set target username.\n"
        "`/viewautoz` — View AutoZ settings.\n"
        "`/startautoz` — Enable AutoZMode.\n"
        "`/stopautoz` — Disable AutoZMode.\n"
        "`/setautozinterval <min> <max>` — Set AutoZ interval in seconds.\n"
        "`/setautozcaption <text>` — Set caption used by AutoZ posts.\n\n"
        "📊 *UTILITY*\n"
        "`/viewallcmd` — Show this command reference panel.\n"
        "`/help` — Quick command list.\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")

# =========================
# Keep-alive ping thread for Render/Heroku-like services
# =========================
def keep_alive_ping_worker():
    if not MY_RENDER_URL:
        print("⚠️ MY_RENDER_URL not set — skipping keep-alive")
        return
    while True:
        try:
            res = requests.get(MY_RENDER_URL, timeout=10)
            print(f"🔁 Keep-alive ping sent ({res.status_code}) to {MY_RENDER_URL}")
        except Exception as e:
            print(f"⚠️ Keep-alive error: {e}")
        time.sleep(600)  # 10 minutes

# =========================
# Startup: register handlers & start bot
# =========================
def main():
    # safety checks
    if BOT_TOKEN.startswith("YOUR_"):
        print("ERROR: Set BOT_TOKEN in environment or script.")
        return

    # Start background worker thread
    t = threading.Thread(target=background_worker, daemon=True)
    t.start()

    # Keep alive ping
    kp = threading.Thread(target=keep_alive_ping_worker, daemon=True)
    kp.start()

    # Start updater & handlers
    updater = Updater(BOT_TOKEN, use_context=True)
    bot = updater.bot

    # if there is a webhook set, delete it to avoid "Conflict: terminated by other getUpdates request"
    try:
        # only delete webhook if we plan to poll (i.e., no WEBHOOK_URL provided)
        if not WEBHOOK_URL:
            try:
                bot.delete_webhook()
                print("Deleted existing webhook to allow polling (if any).")
            except Exception as e:
                print("Warning deleting webhook:", e)
    except Exception:
        pass

    dp = updater.dispatcher

    # Basic
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))

    # Video add flows
    dp.add_handler(CommandHandler("addvideo", addvideo_start))
    dp.add_handler(CommandHandler("addpriority", addpriority_start))
    dp.add_handler(MessageHandler(Filters.video | Filters.document, receive_video_for_add))

    # Video management
    dp.add_handler(CommandHandler("list", list_cmd))
    dp.add_handler(CommandHandler("show", show_cmd, pass_args=True))
    dp.add_handler(CommandHandler("remove", remove_cmd, pass_args=True))
    dp.add_handler(CommandHandler("removepriority", removepriority_cmd, pass_args=True))

    # Caption
    dp.add_handler(CommandHandler("setcaption", setcaption_cmd, pass_args=True))
    dp.add_handler(CommandHandler("viewcaption", viewcaption_cmd))
    dp.add_handler(CommandHandler("removecaption", removecaption_cmd))

    # Timer & posting
    dp.add_handler(CommandHandler("settimer", settimer_cmd, pass_args=True))
    dp.add_handler(CommandHandler("startposting", startposting_cmd))
    dp.add_handler(CommandHandler("stopposting", stopposting_cmd))

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
    dp.add_handler(conv_handler)

    dp.add_handler(CommandHandler("listscheduled", listscheduled_cmd))
    dp.add_handler(CommandHandler("removescheduled", removescheduled_cmd, pass_args=True))

    dp.add_handler(CommandHandler("status", status_cmd))

    # AutoZ commands
    dp.add_handler(CommandHandler("setautoz", autoz_set_target_cmd, pass_args=True))
    dp.add_handler(CommandHandler("viewautoz", autoz_view_cmd))
    dp.add_handler(CommandHandler("startautoz", autoz_start_cmd))
    dp.add_handler(CommandHandler("stopautoz", autoz_stop_cmd))
    dp.add_handler(CommandHandler("setautozinterval", autoz_set_interval_cmd, pass_args=True))
    dp.add_handler(CommandHandler("setautozcaption", autoz_set_caption_cmd, pass_args=True))

    # Start polling or webhook
    if WEBHOOK_URL:
        # attempt to set webhook
        try:
            bot.set_webhook(WEBHOOK_URL)
            print("Webhook set to", WEBHOOK_URL)
            # Updater.start_polling still starts a thread for job queue etc; but we avoid getUpdates by having webhook set
            updater.start_polling()  # still safe; most traffic will go to webhook
            print("Bot started (webhook mode).")
        except Exception as e:
            print("Failed to set webhook, falling back to polling:", e)
            try:
                updater.start_polling()
                print("Bot started (polling fallback).")
            except Exception as e2:
                print("Failed to start polling:", e2)
                return
    else:
        # polling mode - safe: delete webhook above
        try:
            updater.start_polling()
            print("Bot started (polling mode).")
        except Exception as e:
            print("Failed to start polling:", e)
            traceback.print_exc()
            return

    updater.idle()

if __name__ == "__main__":
    main()
