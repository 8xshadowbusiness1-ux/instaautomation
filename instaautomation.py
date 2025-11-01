#!/usr/bin/env python3
"""
insta_autoz_final.py
AutozMode Only - Final Version
- Auto download & post random videos from target IG accounts
- Interval control via /setinterval
- Status, Target management, Buttons
- Safe background threads, 5min keep-alive ping
"""

import os, json, threading, time, random, traceback, requests
from datetime import datetime
from functools import wraps
from flask import Flask, request
from instagrapi import Client
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TgConflict
from telegram.ext import (
    Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters, CallbackContext
)

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
IG_USER = os.getenv("INSTAGRAM_USERNAME", "YOUR_IG_USERNAME")
IG_PASS = os.getenv("INSTAGRAM_PASSWORD", "YOUR_IG_PASSWORD")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0) or None
MY_RENDER_URL = os.getenv("MY_RENDER_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))
KEEP_ALIVE_INTERVAL = 300  # 5 min

DATA_FILE = "autoz_data.json"
VIDEO_DIR = "videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

DEFAULT = {
    "autoz": {"enabled": False, "targets": [], "interval_min": 1800, "interval_max": 3600, "last_run": None},
    "last_post": {"video_code": None, "time": None}
}
data_lock = threading.Lock()

# === UTILS ===
def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT)
        return DEFAULT.copy()
    try:
        d = json.load(open(DATA_FILE))
    except Exception:
        d = DEFAULT.copy()
    for k,v in DEFAULT.items():
        if k not in d: d[k]=v
    return d

def save_data(d=None):
    with data_lock:
        json.dump(d or data, open(DATA_FILE,"w"), indent=2, default=str)

data = load_data()
ig_client=None

# === IG LOGIN ===
def ig_login():
    global ig_client
    if ig_client: return ig_client
    c=Client()
    try:
        c.login(IG_USER, IG_PASS)
        ig_client=c; print("✅ IG Logged In")
        return c
    except Exception as e:
        print("❌ IG login failed:", e)
        traceback.print_exc()
        return None

# === HELPERS ===
def admin_only(f):
    @wraps(f)
    def w(u,c,*a,**k):
        if ADMIN_CHAT_ID and u.effective_user.id!=ADMIN_CHAT_ID:
            u.message.reply_text("❌ Not authorized")
            return
        return f(u,c,*a,**k)
    return w

def safe_thread(fn):
    @wraps(fn)
    def wrap(u,c,*a,**k):
        threading.Thread(target=lambda: _safe_run(fn,u,c,*a,**k),daemon=True).start()
    return wrap
def _safe_run(fn,u,c,*a,**k):
    try: fn(u,c,*a,**k)
    except Exception as e: traceback.print_exc()

# === CORE AUTOZ ===
def download_random_video(username):
    cl = ig_login()
    if not cl:
        return False, "Login failed"
    try:
        uid = cl.user_id_from_username(username)
        medias = cl.user_medias(uid, 30)
        vids = [m for m in medias if getattr(m, "video_url", None)]
        if not vids:
            return False, "No videos found"

        # Random video select
        ch = random.choice(vids)

        # Ensure videos directory exists
        os.makedirs(VIDEO_DIR, exist_ok=True)

        # ✅ Correct call — give only folder name, not file path
        video_path = cl.video_download(ch.pk, folder=VIDEO_DIR)

        return True, video_path
    except Exception as e:
        traceback.print_exc()
        return False, str(e)
        
def post_to_ig(path):
    cl=ig_login()
    if not cl: return False,"Login fail"
    try:
        cl.video_upload(path,"")
        print("✅ Posted:",path)
        return True,"ok"
    except Exception as e:
        traceback.print_exc()
        return False,str(e)

def autoz_cycle():
    tgs=data["autoz"]["targets"]
    if not tgs: return False,"No targets"
    u=random.choice(tgs)
    ok,p=download_random_video(u)
    if not ok: return False,p
    ok2,m=post_to_ig(p)
    with data_lock:
        data["last_post"]={"video_code":f"autoz:{u}","time":datetime.now().isoformat()}
        data["autoz"]["last_run"]=datetime.now().isoformat()
        save_data(data)
    return ok2,f"Posted from @{u}"

# === WORKERS ===
def autoz_worker():
    print("🔄 Autoz Worker Started")
    while True:
        try:
            if data["autoz"]["enabled"]:
                mn=data["autoz"]["interval_min"]; mx=data["autoz"]["interval_max"]
                w=random.randint(mn,mx)
                print(f"Next autoz in {w}s")
                ok,m=autoz_cycle()
                print("Autoz Result:",ok,m)
                for _ in range(0,w,5):
                    if not data["autoz"]["enabled"]: break
                    time.sleep(5)
            else:
                time.sleep(5)
        except Exception as e:
            traceback.print_exc(); time.sleep(5)

def keep_alive():
    if not MY_RENDER_URL: return
    url=MY_RENDER_URL.rstrip("/")
    while True:
        try:
            r=requests.get(url,timeout=15)
            print(f"Ping {r.status_code} {url}")
        except Exception as e: print("Ping err",e)
        time.sleep(KEEP_ALIVE_INTERVAL)

# === FLASK ===
app=Flask(__name__)
dp=None; bot=None
@app.route("/")
def root(): return "OK Autoz Bot",200
@app.route("/status")
def status(): return json.dumps(data),200
@app.route("/"+BOT_TOKEN,methods=["POST"])
def wh(): 
    from telegram import Update
    upd=Update.de_json(request.get_json(force=True),bot)
    dp.process_update(upd); return "ok",200

def run_flask(): app.run(host="0.0.0.0",port=PORT,threaded=True)

# === TELEGRAM HANDLERS ===
def menu_btns():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶ Start",callback_data="start_auto"),
         InlineKeyboardButton("⏹ Stop",callback_data="stop_auto")],
        [InlineKeyboardButton("➕ Add Target",callback_data="add_target"),
         InlineKeyboardButton("➖ Remove Target",callback_data="rm_target")],
        [InlineKeyboardButton("📋 List",callback_data="list_target"),
         InlineKeyboardButton("⚙️ Set Interval",callback_data="interval_help")],
        [InlineKeyboardButton("🧾 Status",callback_data="status"),
         InlineKeyboardButton("🔁 Manual Run",callback_data="manual_run")],
        [InlineKeyboardButton("📡 Ping",callback_data="ping_now"),
         InlineKeyboardButton("❓ Help",callback_data="help")]
    ])

def start_cmd(u,c): u.message.reply_text("🚀 Autoz Bot Ready!",reply_markup=menu_btns())
def help_cmd(u,c): u.message.reply_text("Commands:\n/autoz_start\n/autoz_stop\n/autoz_addtarget <user>\n/autoz_rmtarget <user>\n/autoz_list\n/setinterval <min> <max>\n/autoz_manual\n/autoz_status\n/menu")

def menu_cmd(u,c): start_cmd(u,c)

@admin_only
def autoz_start(u,c):
    data["autoz"]["enabled"]=True; save_data(data)
    u.message.reply_text("✅ Autoz started")

@admin_only
def autoz_stop(u,c):
    data["autoz"]["enabled"]=False; save_data(data)
    u.message.reply_text("🛑 Autoz stopped")

@admin_only
def add_target(u,c):
    a=c.args
    if not a: u.message.reply_text("Usage: /autoz_addtarget <username>"); return
    t=a[0].lstrip("@")
    if t in data["autoz"]["targets"]: u.message.reply_text("Already added"); return
    data["autoz"]["targets"].append(t); save_data(data)
    u.message.reply_text(f"Added @{t}")

@admin_only
def rm_target(u,c):
    a=c.args
    if not a: u.message.reply_text("Usage: /autoz_rmtarget <username>"); return
    t=a[0].lstrip("@")
    if t not in data["autoz"]["targets"]: u.message.reply_text("Not found"); return
    data["autoz"]["targets"]=[x for x in data["autoz"]["targets"] if x!=t]; save_data(data)
    u.message.reply_text(f"Removed @{t}")

def list_targets(u,c):
    t=data["autoz"]["targets"]
    u.message.reply_text("Targets:\n"+("\n".join(["@"+x for x in t]) if t else "(none)"))

@safe_thread
def manual_run(u,c):
    chat=u.effective_chat.id
    Bot(BOT_TOKEN).send_message(chat_id=chat,text="Manual run started...")
    ok,m=autoz_cycle()
    Bot(BOT_TOKEN).send_message(chat_id=chat,text=f"Result: {ok} {m}")

def setinterval(u,c):
    a=c.args
    if len(a)!=2:
        u.message.reply_text("Usage: /setinterval <min_seconds> <max_seconds>"); return
    try:
        mn=int(a[0]); mx=int(a[1])
        if mn<10 or mx<mn: u.message.reply_text("Keep min>=10 and max>=min"); return
        data["autoz"]["interval_min"]=mn; data["autoz"]["interval_max"]=mx; save_data(data)
        u.message.reply_text(f"Interval set: {mn}-{mx}s ✅")
    except: u.message.reply_text("Invalid numbers")

def status_cmd(u,c):
    a=data["autoz"]; last=data["last_post"]
    txt=(f"📊 *AUTOZ STATUS*\n"
         f"Enabled: {a['enabled']}\n"
         f"Targets: {', '.join(a['targets']) or '(none)'}\n"
         f"Interval: {a['interval_min']}–{a['interval_max']}s\n"
         f"Last Run: {a['last_run']}\n"
         f"Last Post: {last}")
    u.message.reply_text(txt,parse_mode="Markdown")

def ping_now(u,c):
    if not MY_RENDER_URL: u.message.reply_text("MY_RENDER_URL not set"); return
    try:
        r=requests.get(MY_RENDER_URL.rstrip("/"),timeout=10)
        u.message.reply_text(f"Ping {r.status_code} OK")
    except Exception as e: u.message.reply_text(str(e))

def cb(u,c):
    q=u.callback_query; d=q.data
    if d=="start_auto": autoz_start(q,c)
    elif d=="stop_auto": autoz_stop(q,c)
    elif d=="list_target": list_targets(q,c)
    elif d=="status": status_cmd(q,c)
    elif d=="manual_run": manual_run(q,c)
    elif d=="ping_now": ping_now(q,c)
    elif d=="interval_help": q.message.reply_text("Use /setinterval <min> <max>")
    elif d=="add_target": q.message.reply_text("Use /autoz_addtarget <username>")
    elif d=="rm_target": q.message.reply_text("Use /autoz_rmtarget <username>")
    elif d=="help": help_cmd(q,c)
    q.answer()

# === MAIN ===
def main():
    global dp,bot
    if BOT_TOKEN.startswith("YOUR_"): print("⚠️ Set BOT_TOKEN"); return
    bot=Bot(BOT_TOKEN)
    up=Updater(BOT_TOKEN,use_context=True)
    dp=up.dispatcher

    dp.add_handler(CommandHandler("start",start_cmd))
    dp.add_handler(CommandHandler("help",help_cmd))
    dp.add_handler(CommandHandler("menu",menu_cmd))
    dp.add_handler(CommandHandler("autoz_start",autoz_start))
    dp.add_handler(CommandHandler("autoz_stop",autoz_stop))
    dp.add_handler(CommandHandler("autoz_addtarget",add_target,pass_args=True))
    dp.add_handler(CommandHandler("autoz_rmtarget",rm_target,pass_args=True))
    dp.add_handler(CommandHandler("autoz_list",list_targets))
    dp.add_handler(CommandHandler("autoz_manual",manual_run))
    dp.add_handler(CommandHandler("setinterval",setinterval,pass_args=True))
    dp.add_handler(CommandHandler("autoz_status",status_cmd))
    dp.add_handler(CommandHandler("ping_now",ping_now))
    dp.add_handler(CallbackQueryHandler(cb))

    threading.Thread(target=autoz_worker,daemon=True).start()
    threading.Thread(target=keep_alive,daemon=True).start()
    threading.Thread(target=run_flask,daemon=True).start()

    for i in range(3):
        try:
            bot.delete_webhook()
            up.start_polling(); print("Bot Running ✅"); up.idle(); break
        except TgConflict:
            print("Conflict, retrying..."); bot.delete_webhook(); time.sleep(2)
        except Exception as e:
            traceback.print_exc(); time.sleep(2)

if __name__=="__main__": main()





