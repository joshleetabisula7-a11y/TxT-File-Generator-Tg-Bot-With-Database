# src.py
import os
import random
import threading
import tempfile
import hashlib
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
        InlineKeyboardButton("🔄 Refresh Logs", callback_data="refresh_logs")
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
        f"👋 <b>Hello, {name} {username}</b>\n\n"
        f"{status_line}\n\n"
        "Welcome to <b>PaFreeTxtNiJosh</b> — search large logs quickly and safely.\n"
        "Use the buttons below to start searching, redeem a key, or see help.\n\n"
        "<i>Tip:</i> If results are too long we send only the first 200 lines per search."
    )

    bot.send_message(message.chat.id, welcome, parse_mode="HTML", reply_markup=make_main_keyboard(is_admin=is_admin))

# ================= SEARCH FLOW =================
@bot.callback_query_handler(func=lambda c: c.data == "search")
def ask_search(call):
    if not has_active_key(call.from_user.id):
        bot.answer_callback_query(call.id, "You need an active key to search (use Redeem).", show_alert=True)
        return
    msg = bot.send_message(call.message.chat.id, "🔎 Please send the keyword to search for:")
    bot.register_next_step_handler(msg, do_search)

def safe_filename_for_kw(kw):
    h = hashlib.sha1(kw.encode("utf-8")).hexdigest()[:16]
    return f"results_{h}.txt"

def do_search(message):
    try:
        uid = message.from_user.id
        if not has_active_key(uid):
            bot.send_message(message.chat.id, "❌ You need an active key to search.")
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

        tmp_path = None
        try:
            tmp = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, prefix="results_", suffix=".txt")
            tmp_path = tmp.name
            tmp.write("\n".join(results_to_send))
            tmp.close()

            caption = f"✅ Found {len(results)} lines"
            if truncated:
                caption += f" — showing first {SEARCH_LINE_LIMIT} lines"
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

# ================= REDEEM VIA BUTTON FLOW =================
@bot.callback_query_handler(func=lambda c: c.data == "redeem_prompt")
def redeem_prompt(call):
    msg = bot.send_message(call.message.chat.id, "🔑 Please send your key (format: KEY-XXXXXX):")
    bot.register_next_step_handler(msg, redeem_via_prompt)

def redeem_via_prompt(message):
    try:
        key = message.text.strip()
        uid = message.from_user.id
        ok, msg, _ = process_redeem_for_user(uid, key)
        bot.send_message(message.chat.id, msg)
    except Exception:
        bot.send_message(message.chat.id, "Usage: send KEY-XXXXXX or use /redeem KEY-XXXXXX")

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
