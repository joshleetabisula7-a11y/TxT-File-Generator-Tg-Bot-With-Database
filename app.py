# src.py
import os
import random
import threading
import tempfile
import hashlib
import uuid
import html
from datetime import datetime, timedelta

import psycopg2
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask

# ================= ENV =================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7011151235"))

LOG_FILE = "logs.txt"
SEARCH_LINE_LIMIT = 200  # <-- per-search limit
SEARCH_COOLDOWN_MINUTES = 5  # per-user cooldown in minutes

if not TOKEN or not DATABASE_URL:
    raise Exception("Missing TELEGRAM_TOKEN or DATABASE_URL")

# ================= BOT =================
bot = telebot.TeleBot(TOKEN)

# ================= DATABASE =================
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS keys (
    key TEXT PRIMARY KEY,
    expires TIMESTAMP,
    redeemed_by BIGINT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    expires TIMESTAMP
)
""")
conn.commit()

# ================= LOAD LOGS =================
def load_logs():
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w").close()
    with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
        return [line.strip() for line in f if line.strip()]

logs = load_logs()
sent = {}  # mapping: keyword -> set(lines already sent for this kw)

# ================= COOLDOWN (in-memory) =================
# mapping: user_id -> datetime of last search
last_search = {}

# ================= FEEDBACK STORAGE (in-memory) =================
# feedback_id -> {user_id, user_name, file_id, caption, status, created_at, admin_msg_chat, admin_msg_id}
feedbacks = {}

# ================= UTIL: KEY CHECK =================
def get_user_expiry(user_id):
    cursor.execute("SELECT expires FROM users WHERE user_id=%s", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def has_active_key(user_id):
    exp = get_user_expiry(user_id)
    if not exp:
        return False
    if datetime.now() <= exp:
        return True
    # expired: remove record
    cursor.execute("DELETE FROM users WHERE user_id=%s", (user_id,))
    conn.commit()
    return False

# ================= UTIL: COOLDOWN HELPERS =================
def is_on_cooldown(user_id):
    """Return (on_cooldown:bool, remaining_timedelta:timedelta)"""
    if user_id == ADMIN_ID:
        return False, timedelta(0)  # admin bypass
    last = last_search.get(user_id)
    if not last:
        return False, timedelta(0)
    expire_time = last + timedelta(minutes=SEARCH_COOLDOWN_MINUTES)
    now = datetime.now()
    if now < expire_time:
        return True, (expire_time - now)
    return False, timedelta(0)

def set_search_timestamp(user_id):
    last_search[user_id] = datetime.now()

def fmt_timedelta(td):
    total = int(td.total_seconds())
    mins, secs = divmod(total, 60)
    return f"{mins}m {secs}s" if mins else f"{secs}s"

# ================= UTIL: PROCESS REDEEM =================
def process_redeem_for_user(uid, key):
    """Return (success:bool, message:str, expires:datetime|None)"""
    cursor.execute(
        "SELECT expires FROM keys WHERE key=%s AND redeemed_by IS NULL",
        (key,)
    )
    row = cursor.fetchone()
    if not row:
        return False, "❌ Invalid or already redeemed key", None
    expires = row[0]
    cursor.execute(
        "INSERT INTO users (user_id, expires) VALUES (%s,%s) "
        "ON CONFLICT (user_id) DO UPDATE SET expires=%s",
        (uid, expires, expires)
    )
    cursor.execute(
        "UPDATE keys SET redeemed_by=%s WHERE key=%s",
        (uid, key)
    )
    conn.commit()
    return True, f"✅ Access granted until {expires}", expires

# ================= COMMANDS =================
@bot.message_handler(commands=["help"])
def help_cmd(message):
    help_text = (
        "<b>Available commands</b>\n"
        "/start - Open main menu\n"
        "/redeem &lt;KEY&gt; - Redeem a key (e.g. /redeem KEY-123456)\n"
        "/createkey &lt;days&gt; &lt;count&gt; - (admin) create keys\n"
        "/refreshlogs - (admin) reload log file from disk\n\n"
        "Use the buttons in the menu for quick actions."
    )
    bot.send_message(message.chat.id, help_text, parse_mode="HTML")

@bot.message_handler(commands=["createkey"])
def create_key_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Not authorized")
        return
    try:
        parts = message.text.split()
        if len(parts) < 3:
            raise ValueError
        _, days_s, count_s = parts[:3]
        days, count = int(days_s), int(count_s)
        if count <= 0 or days <= 0:
            bot.reply_to(message, "❌ days and count must be positive integers")
            return
        keys = []
        for _ in range(count):
            key = f"KEY-{random.randint(100000,999999)}"
            expires = datetime.now() + timedelta(days=days)
            try:
                cursor.execute(
                    "INSERT INTO keys (key, expires, redeemed_by) VALUES (%s,%s,NULL)",
                    (key, expires)
                )
                keys.append(key)
            except psycopg2.IntegrityError:
                conn.rollback()
        conn.commit()
        if keys:
            bot.reply_to(message, "✅ Keys generated:\n" + "\n".join(keys))
        else:
            bot.reply_to(message, "⚠️ No new keys were generated (try again).")
    except Exception:
        bot.reply_to(message, "Usage: /createkey <days> <count>")

@bot.message_handler(commands=["redeem"])
def redeem_cmd(message):
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        _, key = parts[:2]
        uid = message.from_user.id
        ok, msg, _ = process_redeem_for_user(uid, key)
        bot.reply_to(message, msg)
    except Exception:
        bot.reply_to(message, "Usage: /redeem KEY-XXXXXX")

@bot.message_handler(commands=["refreshlogs"])
def refresh_logs_cmd(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ Not authorized")
        return
    global logs
    logs = load_logs()
    bot.reply_to(message, f"✅ Logs reloaded. {len(logs)} lines loaded.")

# ================= START / WELCOME =================
def make_main_keyboard(is_admin=False):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔍 Search Logs", callback_data="search"),
        InlineKeyboardButton("🔑 Redeem Key", callback_data="redeem_prompt"),
        InlineKeyboardButton("📊 Account Status", callback_data="check_access"),
        InlineKeyboardButton("❓ Help", callback_data="help_cb"),
        InlineKeyboardButton("📞 Owner", url="https://t.me/OnlyJosh4"),
        InlineKeyboardButton("🔄 Refresh Logs", callback_data="refresh_logs"),
        InlineKeyboardButton("📝 Feedback", callback_data="feedback_prompt")
    )
    if is_admin:
        kb.add(InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel"))
    return kb

@bot.message_handler(commands=["start"])
def start(message):
    uid = message.from_user.id
    name = message.from_user.first_name or ""
    username = ("@" + message.from_user.username) if message.from_user.username else "NoUsername"
    is_admin = (uid == ADMIN_ID)

    expiry = get_user_expiry(uid)
    if expiry and datetime.now() <= expiry:
        status_line = f"✅ <b>Access active</b>\nExpires: <code>{expiry}</code>"
    else:
        status_line = "❌ <b>No active key</b>\nUse the Redeem Key button or /redeem <KEY>"

    welcome = (
        f"👋 <b>Hello, {html.escape(name)} {html.escape(username)}</b>\n\n"
        f"{status_line}\n\n"
        "Welcome to <b>PaFreeTxtNiJosh</b> — search large logs quickly and safely.\n"
        "Use the buttons below to start searching, redeem a key, or see help.\n\n"
        f"<i>Tip:</i> If results are too long we send only the first {SEARCH_LINE_LIMIT} lines per search.\n"
        f"<i>Anti-spam:</i> There is a {SEARCH_COOLDOWN_MINUTES}-minute cooldown between searches per user."
    )

    bot.send_message(message.chat.id, welcome, parse_mode="HTML", reply_markup=make_main_keyboard(is_admin=is_admin))

# ================= SEARCH FLOW =================
@bot.callback_query_handler(func=lambda c: c.data == "search")
def ask_search(call):
    # check active key
    if not has_active_key(call.from_user.id):
        bot.answer_callback_query(call.id, "You need an active key to search (use Redeem).", show_alert=True)
        return

    # cooldown check
    on_cd, rem = is_on_cooldown(call.from_user.id)
    if on_cd:
        bot.answer_callback_query(call.id, f"Please wait {fmt_timedelta(rem)} before your next search.", show_alert=True)
        return

    msg = bot.send_message(call.message.chat.id, "🔎 Please send the keyword to search for:")
    bot.register_next_step_handler(msg, do_search)

def safe_filename_for_kw(kw):
    h = hashlib.sha1(kw.encode("utf-8")).hexdigest()[:16]
    return f"results_{h}.txt"

def do_search(message):
    try:
        uid = message.from_user.id

        # check active key before performing search
        if not has_active_key(uid):
            bot.send_message(message.chat.id, "❌ You need an active key to search.")
            return

        # cooldown check again (in-case time passed between pressing button and sending message)
        on_cd, rem = is_on_cooldown(uid)
        if on_cd:
            bot.send_message(message.chat.id, f"⏳ Cooldown active. Please wait {fmt_timedelta(rem)} before your next search.")
            return

        kw = message.text.strip().lower()
        if not kw:
            bot.send_message(message.chat.id, "❌ Empty keyword.")
            return

        results = []
        seen = sent.get(kw, set())
        for line in logs:
            if kw in line.lower() and line not in seen:
                results.append(line)
                # optional safety cap (very large)
                if len(results) >= 10000:
                    break

        if not results:
            bot.send_message(message.chat.id, "❌ No results found.")
            return

        # update sent-tracking and apply per-search line limit
        sent.setdefault(kw, set()).update(results)
        truncated = False
        if len(results) > SEARCH_LINE_LIMIT:
            truncated = True
            results_to_send = results[:SEARCH_LINE_LIMIT]
        else:
            results_to_send = results

        # mark last search timestamp (start of sending)
        set_search_timestamp(uid)

        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, prefix="results_", suffix=".txt")
            tmp_path = tmp.name
            tmp.write("\n".join(results_to_send))
            tmp.close()

            caption = f"✅ Found {len(results)} lines"
            if truncated:
                caption += f" — showing first {SEARCH_LINE_LIMIT} lines"
            caption += f"\n⏱️ Next search available in {SEARCH_COOLDOWN_MINUTES} minutes."
            with open(tmp_path, "rb") as f:
                bot.send_document(
                    message.chat.id,
                    f,
                    caption=caption
                )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Error during search.")
        try:
            bot.send_message(ADMIN_ID, f"Search error for user {message.from_user.id}: {e}")
        except Exception:
            pass

# ================= FEEDBACK FLOW =================
@bot.callback_query_handler(func=lambda c: c.data == "feedback_prompt")
def feedback_prompt(call):
    msg = bot.send_message(call.message.chat.id, "📝 Please send a photo for feedback. Add a caption describing the feedback (optional).")
    bot.register_next_step_handler(msg, feedback_receive_photo)

def feedback_receive_photo(message):
    """
    Expecting a photo message (with optional caption).
    We'll forward the photo+caption to admin with approve/reject buttons.
    """
    try:
        if not message.photo:
            bot.send_message(message.chat.id, "❌ No photo detected. Please press Feedback again and send a photo.")
            return

        # take the highest-resolution photo (last in list)
        file_id = message.photo[-1].file_id
        caption = message.caption or ""
        uid = message.from_user.id
        name = message.from_user.first_name or ""
        username = ("@" + message.from_user.username) if message.from_user.username else "NoUsername"

        # create feedback entry
        fid = uuid.uuid4().hex[:10]
        feedbacks[fid] = {
            "user_id": uid,
            "user_name": f"{name} {username}",
            "file_id": file_id,
            "caption": caption,
            "status": "pending",
            "created_at": datetime.now(),
            "admin_msg_chat": None,
            "admin_msg_id": None
        }

        # send to admin with approve/reject buttons
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"fb_approve:{fid}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"fb_reject:{fid}")
        )

        admin_caption = (
            f"📥 New feedback (ID: {fid})\n"
            f"From: <b>{html.escape(feedbacks[fid]['user_name'])}</b>\n\n"
            f"{html.escape(caption) if caption else '<i>(no caption)</i>'}\n\n"
            f"Sent: {feedbacks[fid]['created_at']}"
        )

        # send photo to admin; capture returned message id and chat id
        sent_msg = bot.send_photo(ADMIN_ID, file_id, caption=admin_caption, parse_mode="HTML", reply_markup=kb)

        # store admin message reference
        feedbacks[fid]["admin_msg_chat"] = sent_msg.chat.id
        feedbacks[fid]["admin_msg_id"] = sent_msg.message_id

        bot.send_message(message.chat.id, "✅ Feedback sent to admin for review. You'll be notified when approved or rejected.")
    except Exception as e:
        bot.send_message(message.chat.id, "⚠️ Error sending feedback. Try again.")
        try:
            bot.send_message(ADMIN_ID, f"Feedback send error: {e}")
        except Exception:
            pass

# ================= FEEDBACK APPROVAL CALLBACKS =================
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("fb_approve:"))
def feedback_approve_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    try:
        fid = call.data.split(":", 1)[1]
        fb = feedbacks.get(fid)
        if not fb:
            bot.answer_callback_query(call.id, "Feedback not found or expired.", show_alert=True)
            return
        if fb["status"] != "pending":
            bot.answer_callback_query(call.id, f"Already {fb['status']}.", show_alert=True)
            return

        fb["status"] = "approved"
        fb["admin_decision_at"] = datetime.now()
        fb["admin_decision_by"] = call.from_user.id

        # edit admin message caption to show approved status
        try:
            new_caption = f"{call.message.caption}\n\n✅ <b>APPROVED</b> by admin ({call.from_user.id}) at {fb['admin_decision_at']}"
            bot.edit_message_caption(chat_id=fb["admin_msg_chat"], message_id=fb["admin_msg_id"], caption=new_caption, parse_mode="HTML", reply_markup=None)
        except Exception:
            # fallback: edit reply markup only
            try:
                bot.edit_message_reply_markup(chat_id=fb["admin_msg_chat"], message_id=fb["admin_msg_id"], reply_markup=None)
                bot.send_message(ADMIN_ID, f"✅ Feedback {fid} approved.")
            except Exception:
                pass

        # notify the original user
        try:
            bot.send_message(fb["user_id"], f"✅ Your feedback (ID: {fid}) was approved by admin. Thank you!")
        except Exception:
            pass

        bot.answer_callback_query(call.id, "Feedback approved.", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, "Error processing approval.", show_alert=True)
        try:
            bot.send_message(ADMIN_ID, f"Error approving feedback {call.data}: {e}")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("fb_reject:"))
def feedback_reject_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    try:
        fid = call.data.split(":", 1)[1]
        fb = feedbacks.get(fid)
        if not fb:
            bot.answer_callback_query(call.id, "Feedback not found or expired.", show_alert=True)
            return
        if fb["status"] != "pending":
            bot.answer_callback_query(call.id, f"Already {fb['status']}.", show_alert=True)
            return

        fb["status"] = "rejected"
        fb["admin_decision_at"] = datetime.now()
        fb["admin_decision_by"] = call.from_user.id

        # edit admin message caption to show rejected status
        try:
            new_caption = f"{call.message.caption}\n\n❌ <b>REJECTED</b> by admin ({call.from_user.id}) at {fb['admin_decision_at']}"
            bot.edit_message_caption(chat_id=fb["admin_msg_chat"], message_id=fb["admin_msg_id"], caption=new_caption, parse_mode="HTML", reply_markup=None)
        except Exception:
            # fallback: edit reply markup only
            try:
                bot.edit_message_reply_markup(chat_id=fb["admin_msg_chat"], message_id=fb["admin_msg_id"], reply_markup=None)
                bot.send_message(ADMIN_ID, f"❌ Feedback {fid} rejected.")
            except Exception:
                pass

        # notify the original user
        try:
            bot.send_message(fb["user_id"], f"❌ Your feedback (ID: {fid}) was rejected by admin.")
        except Exception:
            pass

        bot.answer_callback_query(call.id, "Feedback rejected.", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(call.id, "Error processing rejection.", show_alert=True)
        try:
            bot.send_message(ADMIN_ID, f"Error rejecting feedback {call.data}: {e}")
        except Exception:
            pass

# ================= CHECK ACCESS CALLBACK =================
@bot.callback_query_handler(func=lambda c: c.data == "check_access")
def check_access(call):
    expiry = get_user_expiry(call.from_user.id)
    if expiry and datetime.now() <= expiry:
        bot.answer_callback_query(call.id, f"✅ Active until {expiry}", show_alert=True)
    else:
        bot.answer_callback_query(call.id, "❌ No active key", show_alert=True)

# ================= HELP CALLBACK =================
@bot.callback_query_handler(func=lambda c: c.data == "help_cb")
def help_callback(call):
    help_cmd(call.message)

# ================= REFRESH LOGS CALLBACK (admin only) =================
@bot.callback_query_handler(func=lambda c: c.data == "refresh_logs")
def refresh_logs_cb(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    global logs
    logs = load_logs()
    bot.answer_callback_query(call.id, f"✅ Logs reloaded ({len(logs)} lines).", show_alert=True)

# ================= ADMIN PANEL =================
@bot.callback_query_handler(func=lambda c: c.data == "admin_panel")
def admin_panel(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🆕 Create Keys", callback_data="admin_createkeys"),
        InlineKeyboardButton("👥 List Users", callback_data="admin_listusers"),
        InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("⬅️ Back", callback_data="admin_back")
    )
    bot.send_message(call.message.chat.id, "Admin Panel — choose an action:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "admin_back")
def admin_back(call):
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=make_main_keyboard(is_admin=True))

@bot.callback_query_handler(func=lambda c: c.data == "admin_createkeys")
def admin_createkeys(call):
    msg = bot.send_message(call.message.chat.id, "Send: <days> <count>  (e.g. `7 10` to create 10 keys for 7 days)")
    bot.register_next_step_handler(msg, admin_createkeys_step)

def admin_createkeys_step(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Not authorized")
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            raise ValueError
        days, count = int(parts[0]), int(parts[1])
        keys = []
        for _ in range(count):
            key = f"KEY-{random.randint(100000,999999)}"
            expires = datetime.now() + timedelta(days=days)
            try:
                cursor.execute(
                    "INSERT INTO keys (key, expires, redeemed_by) VALUES (%s,%s,NULL)",
                    (key, expires)
                )
                keys.append(key)
            except psycopg2.IntegrityError:
                conn.rollback()
        conn.commit()
        bot.reply_to(message, "✅ Keys generated:\n" + "\n".join(keys))
    except Exception:
        bot.reply_to(message, "Usage: <days> <count>")

@bot.callback_query_handler(func=lambda c: c.data == "admin_listusers")
def admin_listusers(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    cursor.execute("SELECT user_id, expires FROM users ORDER BY expires DESC LIMIT 200")
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(call.message.chat.id, "No users with active access.")
        return
    lines = [f"{r[0]} — {r[1]}" for r in rows]
    # send as file if too long
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, prefix="users_", suffix=".txt")
        tmp_path = tmp.name
        tmp.write("\n".join(lines))
        tmp.close()
        with open(tmp_path, "rb") as f:
            bot.send_document(call.message.chat.id, f, caption=f"Users ({len(lines)})")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

@bot.callback_query_handler(func=lambda c: c.data == "admin_broadcast")
def admin_broadcast(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized", show_alert=True)
        return
    msg = bot.send_message(call.message.chat.id, "Send the broadcast message to send to all users with active access:")
    bot.register_next_step_handler(msg, admin_broadcast_send)

def admin_broadcast_send(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Not authorized")
        return
    cursor.execute("SELECT user_id FROM users WHERE expires > %s", (datetime.now(),))
    rows = cursor.fetchall()
    count = 0
    for (uid,) in rows:
        try:
            bot.send_message(uid, f"📣 Broadcast from admin:\n\n{message.text}")
            count += 1
        except Exception:
            pass
    bot.reply_to(message, f"Broadcast sent to {count} users (attempted).")

# ================= WEB SERVER (so Render sees an open port) =================
app = Flask(__name__)

@app.route("/")
def index():
    return "OK"

@app.route("/health")
def health():
    return "OK"

def run_web():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

# ================= RUN =================
if __name__ == "__main__":
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    print("🤖 Bot running (polling) — web health listener started")
    bot.polling(none_stop=True)
