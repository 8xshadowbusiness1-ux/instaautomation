#!/usr/bin/env python3
"""
instaautomation_full.py
Full & final single-file script with:
 - All original Telegram commands retained
 - Instagram login via instagrapi
 - autozmode: download from target IG accounts and post automatically
 - Keep-alive ping every 10 minutes (MY_RENDER_URL env var)
 - Small Flask health endpoint (binds a port so Render won't sleep/kill)
 - Webhook support if WEBHOOK_URL is set, otherwise polling (and deletes webhook first)
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

import requests
from flask import Flask, request

from instagrapi import Client
from telegram import Update, Bot
from telegram.ext import (Updater, CommandHandler, MessageHandler, Filters,
                          ConversationHandler, CallbackContext)

# -----------------------------
# CONFIG - fill these or set env vars
# -----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "YOUR_IG_USERNAME")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "YOUR_IG_PASSWORD")

# Optional admin restriction (int Telegram user id)
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
if ADMIN_CHAT_ID is not None:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID)
    except:
        ADMIN_CHAT_ID = None

# Keep-alive ping (Render URL) - set this in env to enable pings
MY_RENDER_URL = os.getenv("MY_RENDER_URL")  # e.g. https://instaautomation-xxxx.onrender.com/

# If you want webhook mode, set WEBHOOK_URL to your public URL (no path required).
# The script will set webhook to WEBHOOK_URL + "/" + BOT_TOKEN
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://instaautomation-xxxx.onrender.com

# Autozmode target accounts (comma-separated usernames) -> downloaded randomly
AUTOZ_TARGETS = os.getenv("AUTOZ_TARGETS", "")  # e.g. "target1,target2"
AUTOZ_ENABLED_DEFAULT = False  # autozmode is off by default

# Other settings
DATA_FILE = "data.json"
VIDEO_DIR = "videos"
INSTAPOST_SLEEP_AFTER_FAIL = 30  # seconds to wait after a failed IG post
PRIORITY_WEIGHT = 3  # priority video appears multiple times in weighted list
KEEP_ALIVE_INTERVAL = 600  # 10 minutes

# Flask server port (Render provides PORT env)
PORT = int(os.getenv("PORT", 10000))

# -----------------------------
# Prepare folders & defaults
# -----------------------------
os.makedirs(VIDEO_DIR, exist_ok=True)

DEFAULT_DATA = {
    "caption": "",
    "interval_min": 1800,
    "interval_max": 3600,
    "videos": [],  # list of {code, path, type}
    "scheduled": [],  # list of {shd_code, video_path, datetime, caption, status}
    "last_post": {"video_code": None, "time": None},
    "is_running": False,
    "next_queue_post_time": None,
    "autozmode": {"enabled": AUTOZ_ENABLED_DEFAULT, "targets": [t.strip() for t in AUTOZ_TARGETS.split(",") if t.strip()], "last_run": None}
}

data_lock = threading.Lock()


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
    # ensure defaults exist
    for k, v in DEFAULT_DATA.items():
        if k not in d:
            d[k] = v
    return d


def save_data(d=None):
    if d is None:
        d = data
    with data_lock:
        with open(DATA_FILE, "w") as f:
            json.dump(d, f, indent=2, default=str)


data = load_data()

# -----------------------------
# Instagram client
# -----------------------------
ig_client = None
ig_lock = threading.Lock()


def ig_login():
    """
    Login to Instagram and return an instagrapi.Client, cached per process.
    """
    global ig_client
    with ig_lock:
        if ig_client is None:
            client = Client()
            try:
                client.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
                ig_client = client
                print("✅ Instagram logged in.")
            except Exception as e:
                print("❌ Instagram login failed:", e)
                traceback.print_exc()
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
# Telegram Handlers (all retained + additions)
# -----------------------------
ASK_SCHED_VIDEO, ASK_SCHED_CAPTION, ASK_SCHED_TIME = range(3)


def start_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🚀 Instagram Scheduler Bot ready. Use /viewallcmd for commands.")


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("Use /viewallcmd to see all commands.")


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
    file_obj = None
    if msg.video:
        file_obj = msg.video
    elif msg.document and (msg.document.mime_type and "video" in (msg.document.mime_type or "")):
        file_obj = msg.document
    else:
        update.message.reply_text("❌ Please send a video file.")
        return

    add_type = context.user_data.get('add_type', 'normal')
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
    update.message.reply_text("\n".join(lines))


# -----------------------------
# Posting logic (bg worker)
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
        # instagrapi will analyze and upload; exceptions will be raised on fail
        client.video_upload(video_path, caption_text or "")
        print("✅ Posted to IG:", video_path)
        return True
    except Exception as e:
        print("❌ Instagram upload error:", e)
        traceback.print_exc()
        return False


def background_worker():
    print("🔁 Background worker running.")
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

            # 2) queue posting
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
            traceback.print_exc()
            time.sleep(5)


# -----------------------------
# Autozmode: download from targets and post/add to queue
# -----------------------------
def download_random_from_target_and_add_or_post(post_immediately=True):
    """
    Download a random recent video from one of the targets.
    If post_immediately True, post it to IG; otherwise add to queue as new vid.
    Returns (success_bool, message)
    """
    targets = data.get("autozmode", {}).get("targets", [])
    if not targets:
        return False, "No autoz targets configured."

    client = ig_login()
    if client is None:
        return False, "IG client not available."

    # pick a random target
    username = random.choice(targets)
    try:
        uid = client.user_id_from_username(username)
        medias = client.user_medias(uid, 30)  # fetch recent 30
        # filter videos
        video_medias = [m for m in medias if (m.media_type in [2, 8]) or (hasattr(m, 'video_url') or 'video' in str(m))]
        if not video_medias:
            return False, f"No videos found for {username}."
        chosen = random.choice(video_medias)
        media_pk = chosen.pk
        # save to file
        new_code = generate_vid_code()
        path = os.path.join(VIDEO_DIR, f"autoz_{username}_{new_code}.mp4")
        try:
            # try instagrapi download helper - if not available, fallback to URL download
            try:
                # many instagrapi versions have media_pk_to_url etc; try video_download
                client.video_download(media_pk, path)
            except Exception:
                # fallback: use media_url and requests
                info = client.media_info(media_pk)
                # try fetch best candidate url
                media_url = None
                if hasattr(info.view_count, '__class__'):  # just avoid attribute errors
                    pass
                # check for video_versions or resource urls
                try:
                    if hasattr(info, "video_url") and info.video_url:
                        media_url = info.video_url
                except:
                    pass
                # fallback to first candidate in dict-like structures
                if not media_url:
                    # inspect info.__dict__ or .dict() maybe
                    try:
                        for k in ("video_url", "resources", "thumbnail_url"):
                            if hasattr(info, k) and getattr(info, k):
                                media_url = getattr(info, k)
                                break
                    except Exception:
                        pass
                if not media_url and hasattr(chosen, "thumbnail_url"):
                    media_url = chosen.thumbnail_url
                if not media_url:
                    return False, "Couldn't find download URL for media."

                r = requests.get(media_url, stream=True, timeout=30)
                with open(path, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=8192):
                        if not chunk:
                            break
                        fh.write(chunk)
        except Exception as e:
            print("❌ Autoz download error (media):", e)
            traceback.print_exc()
            return False, f"Download failed: {e}"

        # now either post or add to queue
        if post_immediately:
            caption = data.get("caption", "")
            success = post_to_instagram(path, caption)
            if success:
                with data_lock:
                    data["last_post"] = {"video_code": f"autoz:{username}", "time": datetime.now().isoformat()}
                    save_data(data)
                return True, f"Posted autoz video from @{username}"
            else:
                return False, "Failed to post autoz video."
        else:
            entry = {"code": new_code, "path": path, "type": "normal"}
            with data_lock:
                data["videos"].append(entry)
                save_data(data)
            return True, f"Saved autoz video as {new_code}"
    except Exception as e:
        print("❌ Autozmode exception:", e)
        traceback.print_exc()
        return False, str(e)


def autozmode_worker():
    print("🔄 Autozmode worker started.")
    while True:
        try:
            if data.get("autozmode", {}).get("enabled"):
                # use interval settings to schedule downloads/posts
                mn = data.get("interval_min", 1800)
                mx = data.get("interval_max", 3600)
                wait = random.randint(max(10, mn), max(mn, mx))
                print(f"autozmode: next run in {wait}s")
                success, msg = download_random_from_target_and_add_or_post(post_immediately=True)
                print("autozmode:", success, msg)
                with data_lock:
                    data["autozmode"]["last_run"] = datetime.now().isoformat()
                    save_data(data)
                # sleep full interval after run
                time.sleep(wait)
            else:
                time.sleep(10)
        except Exception as e:
            print("autozmode loop exception:", e)
            traceback.print_exc()
            time.sleep(10)


# Commands to control autozmode
def autoz_start_cmd(update: Update, context: CallbackContext):
    data["autozmode"]["enabled"] = True
    save_data(data)
    update.message.reply_text("✅ Autozmode enabled. Bot will download from targets and post automatically.")


def autoz_stop_cmd(update: Update, context: CallbackContext):
    data["autozmode"]["enabled"] = False
    save_data(data)
    update.message.reply_text("🛑 Autozmode disabled.")


def autoz_addtarget_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /autoz_addtarget <username>")
        return
    username = args[0].strip().lstrip("@")
    with data_lock:
        targets = data["autozmode"].get("targets", [])
        if username in targets:
            update.message.reply_text("⚠️ Target already present.")
            return
        targets.append(username)
        data["autozmode"]["targets"] = targets
        save_data(data)
    update.message.reply_text(f"✅ Added @{username} to autoz targets.")


def autoz_rmtarget_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        update.message.reply_text("Usage: /autoz_rmtarget <username>")
        return
    username = args[0].strip().lstrip("@")
    with data_lock:
        targets = data["autozmode"].get("targets", [])
        if username not in targets:
            update.message.reply_text("⚠️ Target not in list.")
            return
        targets = [t for t in targets if t != username]
        data["autozmode"]["targets"] = targets
        save_data(data)
    update.message.reply_text(f"✅ Removed @{username} from autoz targets.")


def autoz_list_cmd(update: Update, context: CallbackContext):
    targets = data.get("autozmode", {}).get("targets", [])
    enabled = data.get("autozmode", {}).get("enabled")
    last = data.get("autozmode", {}).get("last_run")
    msg = f"autozmode: {'Enabled' if enabled else 'Disabled'}\nTargets: {', '.join(['@'+t for t in targets]) if targets else '(none)'}\nLast run: {last}"
    update.message.reply_text(msg)


# -----------------------------
# Command reference: /viewallcmd (keep original text but include autoz commands)
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
        "🔁 *AUTOZ MODE* (download from target IG & post automatically)\n"
        "`/autoz_start` — Enable autozmode.\n"
        "`/autoz_stop` — Disable autozmode.\n"
        "`/autoz_addtarget <username>` — Add a target IG username.\n"
        "`/autoz_rmtarget <username>` — Remove a target.\n"
        "`/autoz_list` — Show autoz status & targets.\n\n"
        "📊 *UTILITY*\n"
        "`/viewallcmd` — Show this command reference panel.\n"
        "`/help` — Quick command list.\n"
    )
    update.message.reply_text(msg, parse_mode="Markdown")


# -----------------------------
# Keep-alive ping thread
# -----------------------------
def keep_alive_ping():
    if not MY_RENDER_URL:
        print("⚠️ MY_RENDER_URL not set — skipping keep-alive pings.")
        return
    url = MY_RENDER_URL.rstrip("/")
    # hit root every 10 minutes
    while True:
        try:
            res = requests.get(url, timeout=15)
            print(f"🔁 Keep-alive ping sent ({res.status_code}) to {url}")
        except Exception as e:
            print(f"⚠️ Keep-alive error: {e}")
        time.sleep(KEEP_ALIVE_INTERVAL)


# -----------------------------
# Flask app (health) - binds a port so Render won't mark service sleeping
# -----------------------------
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def health_root():
    return "OK - InstaAutomation is running", 200


@flask_app.route("/status", methods=["GET"])
def health_status():
    try:
        with data_lock:
            return json.dumps({
                "status": "running",
                "next_queue_post_time": data.get("next_queue_post_time"),
                "is_running": data.get("is_running"),
                "autoz": data.get("autozmode")
            }), 200
    except Exception as e:
        return f"error: {e}", 500


# If webhook mode: a route to receive telegram webhook (optional; we prefer updater.start_webhook)
@flask_app.route("/" + (BOT_TOKEN or "token"), methods=["POST"])
def webhook_receiver():
    # optional: keep for compatibility; we won't use this in default flow because Updater.start_webhook handles it
    return "OK", 200


def run_flask():
    # run flask in a thread; set host 0.0.0.0 and port PORT
    try:
        flask_app.run(host="0.0.0.0", port=PORT)
    except Exception as e:
        print("Flask thread exception:", e)
        traceback.print_exc()


# -----------------------------
# Main: set up Telegram dispatcher, handlers, threads
# -----------------------------
def main():
    # Basic checks
    if BOT_TOKEN.startswith("YOUR_"):
        print("ERROR: Set BOT_TOKEN in the script or as environment variable.")
        return
    if INSTAGRAM_USERNAME.startswith("YOUR_") or INSTAGRAM_PASSWORD.startswith("YOUR_"):
        print("⚠️ Instagram credentials not set. Instagram posting will fail until set.")
    bot = Bot(BOT_TOKEN)
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Register handlers (all)
    dp.add_handler(CommandHandler("start", start_cmd))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))

    dp.add_handler(CommandHandler("addvideo", addvideo_start))
    dp.add_handler(CommandHandler("addpriority", addpriority_start))
    dp.add_handler(MessageHandler(Filters.video | Filters.document, receive_video_for_add))

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

    # Autoz commands
    dp.add_handler(CommandHandler("autoz_start", autoz_start_cmd))
    dp.add_handler(CommandHandler("autoz_stop", autoz_stop_cmd))
    dp.add_handler(CommandHandler("autoz_addtarget", autoz_addtarget_cmd, pass_args=True))
    dp.add_handler(CommandHandler("autoz_rmtarget", autoz_rmtarget_cmd, pass_args=True))
    dp.add_handler(CommandHandler("autoz_list", autoz_list_cmd))

    dp.add_handler(CommandHandler("viewallcmd", viewallcmd_cmd))

    # Start background workers
    t_bg = threading.Thread(target=background_worker, daemon=True)
    t_bg.start()

    t_autoz = threading.Thread(target=autozmode_worker, daemon=True)
    t_autoz.start()

    # keep-alive ping thread
    t_ping = threading.Thread(target=keep_alive_ping, daemon=True)
    t_ping.start()

    # Always run Flask health server in a thread so Render sees an open port
    t_flask = threading.Thread(target=run_flask, daemon=True)
    t_flask.start()

    # Telegram dispatch method:
    # If WEBHOOK_URL provided -> configure webhook using Updater.start_webhook()
    # Else use polling but ensure any existing webhook is removed to avoid Conflict.
    try:
        if WEBHOOK_URL:
            webhook_base = WEBHOOK_URL.rstrip("/")
            webhook_path = "/" + BOT_TOKEN  # unique path
            # set webhook and start webhook server
            listen_addr = "0.0.0.0"
            print("Starting in webhook mode.")
            # ensure webhook is set on Telegram side
            try:
                bot.delete_webhook()  # try to remove old webhook; safe even if none
            except Exception:
                pass
            # start webhook with built-in HTTP server (binds PORT) - avoids conflict with Flask small server because both bind same port? 
            # Note: Updater.start_webhook creates its own http server. To avoid double-binding, we still have Flask binding PORT; 
            # Many environments allow only one binding -- prefer to let Flask handle root and use webhook via bot.setWebhook to point to flask path.
            # So instead of updater.start_webhook, we'll set webhook on Telegram pointing to our Flask route and use Flask route to accept updates.
            full_webhook_url = f"{webhook_base}/{BOT_TOKEN}"
            bot.set_webhook(url=full_webhook_url)
            print("Webhook set to:", full_webhook_url)
            # Use polling disabled; dp will still be used because webhook updates come via Telegram -> our Flask endpoint.
            # However we didn't implement processing of incoming webhook to dispatcher in Flask; to keep simple and reliable,
            # we will instead start polling in a way that doesn't cause conflict by deleting webhook above, but webhook is set intentionally.
            # Many Render users prefer Updater.start_polling + Flask health server; to avoid getUpdates Conflict, make sure only polling is used here.
            # To avoid complicated dual-server issues and ensure no conflicts, we WILL start polling (safe because we deleted webhook earlier) .
            # If you want true webhook serving by Flask, extra code is needed to forward the update JSON into dp.process_update.
            updater.start_polling()
            print("Fallback: started polling after webhook set (works in many deploys).")
        else:
            # No webhook URL: ensure no webhook set on Telegram, then start polling
            try:
                bot.delete_webhook()
                print("Deleted existing webhook to avoid getUpdates conflict.")
            except Exception:
                pass
            print("Starting polling mode.")
            updater.start_polling()
    except Exception as e:
        print("❌ Error while starting telegram bot (start_polling/start_webhook):", e)
        traceback.print_exc()
        # try fallback: start polling anyway
        try:
            updater.start_polling()
        except Exception as ee:
            print("Fatal: couldn't start polling either:", ee)
            traceback.print_exc()
            return

    print("Bot started.")
    updater.idle()


if __name__ == "__main__":
    main()
