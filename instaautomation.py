#!/usr/bin/env python3
"""
Final instaautomation.py
- Uses ffmpeg CLI via subprocess (no python-ffmpeg package)
- Saves IG session to videos/ig_session.json
- Webhook-based Telegram integration (Flask)
- Video uploads via instagrapi.video_upload
"""

import os
import sys
import json
import time
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path

from flask import Flask, request
from instagrapi import Client
from telegram import Bot, Update
from telegram.ext import Dispatcher, MessageHandler, Filters, CallbackContext

# -----------------------
# CONFIG (use env vars)
# -----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # set in Render
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID")) if os.getenv("ADMIN_CHAT_ID") else None
PORT = int(os.getenv("PORT", 10000))

# -----------------------
# Setup paths & folders
# -----------------------
BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
SESSION_PATH = VIDEO_DIR / "ig_session.json"

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("instaautomation")

# -----------------------
# Check config early
# -----------------------
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not set. Exiting.")
    sys.exit(1)
if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
    logger.warning("Instagram credentials not set. /login command will be available to try login later.")

# -----------------------
# Instagram client helper
# -----------------------
ig_lock = threading.Lock()
ig_client = None


def ig_login(force=False):
    """
    Return logged-in instagrapi.Client or None.
    Saves/loads session to SESSION_PATH.
    """
    global ig_client
    with ig_lock:
        if ig_client is not None and not force:
            return ig_client
        cl = Client()
        try:
            # load settings if present
            if SESSION_PATH.exists():
                try:
                    cl.load_settings(str(SESSION_PATH))
                    logger.info("Loaded IG settings from session file.")
                except Exception as e:
                    logger.warning("Failed loading session file: %s", e)
            # attempt login
            cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            # dump settings (session) so next run is reused
            cl.dump_settings(str(SESSION_PATH))
            logger.info("✅ Instagram login successful and session saved.")
            ig_client = cl
            return ig_client
        except Exception as e:
            # challenge or other issues often require interactive input — notify admin
            logger.exception("Instagram login failed: %s", e)
            if ADMIN_CHAT_ID and BOT_TOKEN:
                try:
                    Bot(BOT_TOKEN).send_message(ADMIN_CHAT_ID,
                        f"⚠️ Instagram login failed:\n{e}\nYou may need to verify login / solve challenge manually.")
                except Exception:
                    logger.exception("Failed to notify admin about IG login error.")
            return None


# -----------------------
# ffmpeg conversion (uses ffmpeg binary)
# -----------------------
def ffmpeg_exists():
    return shutil_which("ffmpeg") is not None


def shutil_which(cmd):
    # tiny wrapper to avoid importing shutil repeatedly
    import shutil
    return shutil.which(cmd)


def convert_video_to_ig(src_path: str, dst_path: str) -> bool:
    """
    Convert input to IG-friendly MP4 using ffmpeg CLI.
    Returns True on success, False otherwise.
    """
    ff = shutil_which("ffmpeg")
    if not ff:
        logger.warning("ffmpeg binary not found in PATH. Skipping conversion (may still work).")
        # try copying as-is
        try:
            from shutil import copyfile
            copyfile(src_path, dst_path)
            return True
        except Exception as e:
            logger.exception("Failed copying video as fallback: %s", e)
            return False

    cmd = [
        ff,
        "-y",
        "-i", src_path,
        "-c:v", "libx264",
        "-profile:v", "baseline",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=720:-2",   # scale width to 720 keeping aspect ratio
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        dst_path
    ]
    try:
        logger.info("Running ffmpeg: %s", " ".join(cmd))
        # run and capture output for logs
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode != 0:
            logger.error("ffmpeg failed: %s", res.stderr.decode(errors="ignore"))
            return False
        logger.info("ffmpeg conversion succeeded: %s", dst_path)
        return True
    except Exception as e:
        logger.exception("ffmpeg conversion exception: %s", e)
        return False


# -----------------------
# Telegram / Flask setup
# -----------------------
app = Flask(__name__)
bot = Bot(BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)


# -----------------------
# Helper: send admin message
# -----------------------
def notify_admin(text: str):
    if ADMIN_CHAT_ID is None:
        return
    try:
        bot.send_message(ADMIN_CHAT_ID, text)
    except Exception:
        logger.exception("Failed to notify admin.")


# -----------------------
# Handler: receive video or document and post to IG
# -----------------------
def handle_video(update: Update, context: CallbackContext):
    msg = update.effective_message
    user = update.effective_user
    logger.info("Received message from %s (%s)", user.username if user else "unknown", user.id if user else "?")

    # Accept video or document
    file_obj = None
    if msg.video:
        file_obj = msg.video
    elif msg.document and (msg.document.mime_type and msg.document.mime_type.startswith("video")):
        file_obj = msg.document
    else:
        msg.reply_text("❌ Please send a video file (as video or as a video document).")
        return

    # Save original
    try:
        remote_file = bot.get_file(file_obj.file_id)
        orig_filename = f"{file_obj.file_id}.mp4"
        orig_path = str(VIDEO_DIR / orig_filename)
        remote_file.download(custom_path=orig_path)
        msg.reply_text("🎥 Video received. Converting for Instagram...")
        logger.info("Downloaded video to %s", orig_path)
    except Exception as e:
        logger.exception("Failed downloading file: %s", e)
        msg.reply_text(f"❌ Failed to download video: {e}")
        return

    # Convert
    converted_filename = f"converted_{file_obj.file_id}.mp4"
    converted_path = str(VIDEO_DIR / converted_filename)
    ok = convert_video_to_ig(orig_path, converted_path)
    if not ok:
        msg.reply_text("❌ Conversion failed. Check server logs.")
        return

    # Ensure IG logged in
    cl = ig_login()
    if cl is None:
        msg.reply_text("⚠️ Instagram client not logged in. Use /login or check admin for details.")
        return

    # Upload
    try:
        caption = "Uploaded via bot"
        msg.reply_text("📤 Uploading to Instagram...")
        # Use feed video upload; if you want reel use clip_upload
        cl.video_upload(converted_path, caption)
        msg.reply_text("✅ Successfully posted to Instagram!")
        logger.info("Uploaded to IG: %s", converted_path)
    except Exception as e:
        logger.exception("Instagram upload failed: %s", e)
        msg.reply_text(f"❌ Instagram upload failed: {e}")
        # optionally notify admin
        notify_admin(f"⚠️ IG upload failed:\n{e}")


# Register Telegram handler
dispatcher.add_handler(MessageHandler(Filters.video | Filters.document, handle_video))


# -----------------------
# /login command endpoint
# -----------------------
from telegram.ext import CommandHandler


def login_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("🔐 Attempting Instagram login (server-side)...")
    cl = ig_login(force=True)
    if cl:
        update.message.reply_text("✅ Instagram login successful (session saved).")
    else:
        update.message.reply_text("❌ Instagram login failed — check logs and solve any challenge in the IG app.")


dispatcher.add_handler(CommandHandler("login", login_cmd))


# -----------------------
# Webhook route
# -----------------------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        upd = Update.de_json(request.get_json(force=True), bot)
        dispatcher.process_update(upd)
    except Exception:
        logger.exception("Failed to process update")
    return "OK", 200


@app.route("/", methods=["GET", "HEAD"])
def index():
    return "🤖 InstaAutomation (webhook) is up", 200


# -----------------------
# Startup
# -----------------------
def run_flask():
    # set debug False in production
    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    logger.info("📂 Video folder ready: %s", VIDEO_DIR)
    # try to login once at startup (non-blocking)
    threading.Thread(target=ig_login, daemon=True).start()
    logger.info("🚀 Starting Flask webhook server on port %s", PORT)
    run_flask()
