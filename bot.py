import asyncio
import os
import re
import sys
import time
import uuid
import shutil
import sqlite3
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, errors, types, utils
from telethon.sessions import StringSession

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()

raw_api_id = os.getenv("API_ID", "").strip()
API_ID = int(raw_api_id) if raw_api_id.isdigit() else 0
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
raw_admin_id = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(raw_admin_id) if raw_admin_id.isdigit() else 0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def cleanup_storage():
    if os.path.exists(DOWNLOAD_DIR):
        for item in os.listdir(DOWNLOAD_DIR):
            item_path = os.path.join(DOWNLOAD_DIR, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
            except Exception:
                pass

cleanup_storage()

if not API_ID or not API_HASH or not BOT_TOKEN:
    print("=" * 60)
    print("CONFIG ERROR: Please set your API_ID, API_HASH, and BOT_TOKEN in .env file!")
    print(f"File path: {os.path.join(BASE_DIR, '.env')}")
    print("=" * 60)
    sys.exit(1)

class Database:
    def __init__(self, path):
        self.path = path
        self._init_db()

    def get_conn(self):
        conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self.get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT,
                    username TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    phone TEXT,
                    first_name TEXT,
                    tg_user_id INTEGER,
                    session_str TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()

    def add_user(self, user_id, first_name, username):
        with self.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, first_name, username) VALUES (?, ?, ?);",
                (user_id, first_name or "", username or "")
            )
            conn.commit()

    def get_user_count(self):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM users;")
            return cur.fetchone()[0]

    def get_all_user_ids(self):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT user_id FROM users;")
            return [row[0] for row in cur.fetchall()]

    def add_account(self, user_id, phone, first_name, tg_user_id, session_str):
        with self.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (user_id, phone, first_name, tg_user_id, session_str) VALUES (?, ?, ?, ?, ?);",
                (user_id, phone, first_name or "Telegram User", tg_user_id, session_str)
            )
            conn.commit()
            return cur.lastrowid

    def get_user_accounts(self, user_id):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE user_id = ? ORDER BY id DESC;", (user_id,))
            return [dict(r) for r in cur.fetchall()]

    def get_account_by_id(self, acc_id, user_id):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT * FROM accounts WHERE id = ? AND user_id = ?;", (acc_id, user_id))
            row = cur.fetchone()
            return dict(row) if row else None

    def delete_account(self, acc_id, user_id):
        with self.get_conn() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ? AND user_id = ?;", (acc_id, user_id))
            conn.commit()

    def get_total_accounts_count(self):
        with self.get_conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM accounts;")
            return cur.fetchone()[0]

db = Database(DB_PATH)

active_tasks = {}
USER_STATES = {}

class TaskCancelledException(Exception):
    pass

def is_admin(user_id):
    if not ADMIN_ID:
        return False
    return int(user_id) == int(ADMIN_ID)

def progress_bar(current, total, length=12):
    if total <= 0:
        return "░" * length
    fraction = min(1.0, max(0.0, current / total))
    filled = int(fraction * length)
    return "█" * filled + "░" * (length - filled)

def format_speed(b):
    if b >= 1024 * 1024:
        return f"{b / (1024 * 1024):.2f} MB/s"
    if b >= 1024:
        return f"{b / 1024:.2f} KB/s"
    return f"{b:.2f} B/s"

def format_size(s):
    if s >= 1024 * 1024 * 1024:
        return f"{s / (1024 * 1024 * 1024):.2f} GB"
    if s >= 1024 * 1024:
        return f"{s / (1024 * 1024):.2f} MB"
    if s >= 1024:
        return f"{s / 1024:.2f} KB"
    return f"{s} B"

def format_time(s):
    s = max(0, int(s))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"

def parse_source_link(text):
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)|([a-zA-Z0-9_]+))(?:/(\d+))?(?:/(\d+))?/?$", text)
    if m:
        c_id, username, num1, num2 = m.groups()
        entity = int(f"-100{c_id}") if c_id else username
        msg_id = int(num2) if num2 else (int(num1) if num1 else None)
        return entity, msg_id
    parts = text.split()
    if len(parts) == 2 and parts[1].isdigit():
        ent = int(parts[0]) if (parts[0].startswith("-100") or parts[0].startswith("-")) else parts[0].lstrip("@")
        return ent, int(parts[1])
    return None, None

def parse_end_link(text):
    text = text.strip()
    if text.isdigit():
        return int(text)
    _, msg_id = parse_source_link(text)
    return msg_id

def parse_dest_link(text):
    text = text.strip()
    if text.lower() in ("me", "saved"):
        return "me"
    if text.startswith("-100") and text[4:].isdigit():
        return int(text)
    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    if text.isdigit():
        return int(text)
    if text.startswith("@"):
        return text[1:]
    m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:c/(\d+)|([a-zA-Z0-9_]+))(?:/(\d+))?/?$", text)
    if m:
        c_id, username, _ = m.groups()
        return int(f"-100{c_id}") if c_id else username
    return text

async def get_clean_entity(client, target):
    try:
        return await client.get_entity(target)
    except Exception:
        try:
            await client.get_dialogs(limit=100)
            return await client.get_entity(target)
        except Exception:
            return await client.get_input_entity(target)

class ProgressTracker:
    def __init__(self, bot_client, chat_id, message_id, task_id, total_msgs, start_msg_id):
        self.bot = bot_client
        self.chat_id = chat_id
        self.message_id = message_id
        self.task_id = task_id
        self.total_msgs = max(1, total_msgs)
        self.processed_msgs = 0
        self.forwarded = 0
        self.skipped = 0
        self.current_msg_id = start_msg_id
        self.status = "Starting..."
        self.start_time = time.time()
        self.last_edit_time = 0.0
        self.current_bytes = 0
        self.total_bytes = 0
        self.current_speed = 0.0
        self.sub_mode = ""
        self.last_sub_bytes = 0
        self.last_sub_time = time.time()

    def make_progress_callback(self, mode):
        self.sub_mode = mode
        self.last_sub_bytes = 0
        self.last_sub_time = time.time()

        async def callback(current, total):
            if active_tasks.get(self.task_id, {}).get("cancelled"):
                raise TaskCancelledException("Stopped by user")
            now = time.time()
            time_diff = now - self.last_sub_time
            if time_diff >= 0.5:
                bytes_diff = current - self.last_sub_bytes
                self.current_speed = max(0.0, bytes_diff / time_diff) if time_diff > 0 else 0.0
                self.last_sub_bytes = current
                self.last_sub_time = now
            self.current_bytes = current
            self.total_bytes = total
            if now - self.last_edit_time >= 2.5:
                await self.update_display()

        return callback

    def render_text(self):
        percent = (self.processed_msgs / self.total_msgs) * 100.0
        bar = progress_bar(self.processed_msgs, self.total_msgs, 12)
        elapsed = format_time(time.time() - self.start_time)
        if self.processed_msgs > 0:
            avg_per_msg = (time.time() - self.start_time) / self.processed_msgs
            eta = format_time((self.total_msgs - self.processed_msgs) * avg_per_msg)
        else:
            eta = "Calculating..."
        speed_text = format_speed(self.current_speed)
        text = (
            f"⚡ **FORWARDING IN PROGRESS** ⚡\n\n"
            f"📊 **Progress:** `{bar}` `{percent:.1f}%`\n"
            f"🔢 **Messages:** `{self.processed_msgs}/{self.total_msgs}`\n"
            f"🎯 **Current Msg ID:** `#{self.current_msg_id}`\n"
            f"📡 **Status:** `{self.status}`\n"
        )
        if self.sub_mode and self.total_bytes > 0:
            sub_pct = (self.current_bytes / self.total_bytes) * 100.0
            sub_bar = progress_bar(self.current_bytes, self.total_bytes, 8)
            text += (
                f"📁 **Media:** `{sub_bar}` `{sub_pct:.1f}%`\n"
                f"📦 **Size:** `{format_size(self.current_bytes)} / {format_size(self.total_bytes)}`\n"
            )
        text += (
            f"🚀 **VPS Speed:** `{speed_text}`\n"
            f"⏳ **Elapsed:** `{elapsed}`\n"
            f"⏱️ **ETA:** `{eta}`\n\n"
            f"✅ **Forwarded:** `{self.forwarded}` | ⏩ **Skipped:** `{self.skipped}`\n\n"
            f"⚠️ *Click Stop below to cancel anytime.*"
        )
        return text

    async def update_display(self):
        now = time.time()
        self.last_edit_time = now
        text = self.render_text()
        buttons = [Button.inline("⏹ Stop Forwarding", f"stop_fwd_{self.task_id}".encode("utf-8"))]
        try:
            await self.bot.edit_message(self.chat_id, self.message_id, text, buttons=buttons)
        except errors.MessageNotModifiedError:
            pass
        except errors.FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            pass

def get_main_keyboard(is_admin_user=False):
    buttons = [
        [Button.inline("🚀 Start Forward", b"start_fwd")],
        [Button.inline("➕ Add Account", b"add_acc"), Button.inline("🗑️ Remove Account", b"rm_acc")],
        [Button.inline("📋 My Accounts", b"list_acc")]
    ]
    if is_admin_user:
        buttons.append([Button.inline("👑 Admin Panel", b"admin_panel")])
    return buttons

def get_cancel_keyboard():
    return [[Button.inline("❌ Cancel", b"cancel_flow")]]

async def run_forwarding_task(task_id, user_id, chat_id, status_msg_id, account, src_entity_input, start_id, end_id, dest_entity_input):
    bot_client = active_tasks[task_id]["bot"]
    session_str = account["session_str"]
    user_client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    active_tasks[task_id]["client"] = user_client
    task_temp_dir = os.path.join(DOWNLOAD_DIR, task_id)
    os.makedirs(task_temp_dir, exist_ok=True)

    min_id = min(start_id, end_id)
    max_id = max(start_id, end_id)
    ids_to_process = list(range(min_id, max_id + 1))
    total_count = len(ids_to_process)

    tracker = ProgressTracker(bot_client, chat_id, status_msg_id, task_id, total_count, min_id)

    try:
        await user_client.connect()
        if not await user_client.is_user_authorized():
            await bot_client.edit_message(
                chat_id,
                status_msg_id,
                "❌ **Account Authorization Failed!**\nThe session string has expired or was revoked. Please add the account again.",
                buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]]
            )
            return

        src_entity = await get_clean_entity(user_client, src_entity_input)
        dest_entity = await get_clean_entity(user_client, dest_entity_input)

        src_noforwards = False
        try:
            full_src = await user_client.get_entity(src_entity)
            src_noforwards = getattr(full_src, "noforwards", False)
        except Exception:
            pass

        tracker.status = "Fetching messages..."
        await tracker.update_display()

        async for msg in user_client.iter_messages(src_entity, ids=ids_to_process):
            if active_tasks.get(task_id, {}).get("cancelled"):
                break

            if msg is None:
                tracker.processed_msgs += 1
                tracker.skipped += 1
                if time.time() - tracker.last_edit_time >= 2.5:
                    await tracker.update_display()
                continue

            tracker.current_msg_id = msg.id

            if isinstance(msg, types.MessageService) or (not msg.text and not msg.media):
                tracker.processed_msgs += 1
                tracker.skipped += 1
                if time.time() - tracker.last_edit_time >= 2.5:
                    await tracker.update_display()
                continue

            if not msg.media:
                tracker.status = "⏩ Copying text message..."
                sent_ok = False
                for attempt in range(3):
                    try:
                        await user_client.send_message(
                            dest_entity,
                            message=msg.text or "",
                            formatting_entities=msg.entities,
                            link_preview=bool(msg.web_preview)
                        )
                        sent_ok = True
                        break
                    except errors.FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 1)
                    except Exception:
                        break
                if sent_ok:
                    tracker.forwarded += 1
                else:
                    tracker.skipped += 1
                tracker.processed_msgs += 1
                if time.time() - tracker.last_edit_time >= 2.5:
                    await tracker.update_display()
                await asyncio.sleep(0.4)
                continue

            is_restricted = src_noforwards or getattr(msg, "noforwards", False)
            copied_direct = False

            if not is_restricted:
                tracker.status = "⏩ Sending media directly..."
                try:
                    await user_client.send_file(
                        dest_entity,
                        file=msg.media,
                        caption=msg.text or "",
                        formatting_entities=msg.entities
                    )
                    copied_direct = True
                    tracker.forwarded += 1
                except (errors.ChatForwardsRestrictedError, Exception):
                    is_restricted = True

            if is_restricted and not copied_direct:
                temp_file = None
                thumb_file = None
                try:
                    tracker.status = "📥 Downloading restricted media..."
                    await tracker.update_display()
                    down_cb = tracker.make_progress_callback("📥 Downloading")
                    temp_file = await user_client.download_media(msg, file=task_temp_dir, progress_callback=down_cb)

                    if not temp_file or not os.path.exists(temp_file):
                        tracker.skipped += 1
                        tracker.processed_msgs += 1
                        continue

                    try:
                        thumb_file = await user_client.download_media(msg, file=task_temp_dir, thumb=-1)
                    except Exception:
                        thumb_file = None

                    tracker.status = "📤 Uploading restricted media..."
                    await tracker.update_display()
                    up_cb = tracker.make_progress_callback("📤 Uploading")
                    uploaded_file = await user_client.upload_file(temp_file, part_size_kb=512, progress_callback=up_cb)

                    attrs = list(getattr(msg.document, "attributes", [])) if msg.document else None
                    supports_streaming = bool(msg.video)
                    voice_note = bool(msg.voice)
                    video_note = bool(msg.video_note)
                    force_doc = bool(msg.document and not (msg.video or msg.audio or msg.voice or msg.video_note))

                    for attempt in range(3):
                        try:
                            await user_client.send_file(
                                dest_entity,
                                file=uploaded_file,
                                caption=msg.text or "",
                                formatting_entities=msg.entities,
                                thumb=thumb_file if (thumb_file and os.path.exists(thumb_file)) else None,
                                attributes=attrs,
                                supports_streaming=supports_streaming,
                                voice_note=voice_note,
                                video_note=video_note,
                                force_document=force_doc
                            )
                            tracker.forwarded += 1
                            break
                        except errors.FloodWaitError as e:
                            await asyncio.sleep(e.seconds + 1)
                        except Exception:
                            tracker.skipped += 1
                            break
                except TaskCancelledException:
                    break
                except errors.FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                    tracker.skipped += 1
                except Exception:
                    tracker.skipped += 1
                finally:
                    if temp_file and os.path.exists(temp_file):
                        try:
                            os.remove(temp_file)
                        except Exception:
                            pass
                    if thumb_file and os.path.exists(thumb_file):
                        try:
                            os.remove(thumb_file)
                        except Exception:
                            pass
                    if os.path.exists(task_temp_dir):
                        for item in os.listdir(task_temp_dir):
                            item_path = os.path.join(task_temp_dir, item)
                            try:
                                if os.path.isfile(item_path) or os.path.islink(item_path):
                                    os.unlink(item_path)
                                elif os.path.isdir(item_path):
                                    shutil.rmtree(item_path, ignore_errors=True)
                            except Exception:
                                pass

            tracker.processed_msgs += 1
            tracker.sub_mode = ""
            tracker.current_speed = 0.0
            tracker.current_bytes = 0
            tracker.total_bytes = 0
            if time.time() - tracker.last_edit_time >= 2.5:
                await tracker.update_display()
            await asyncio.sleep(0.4)

        elapsed = format_time(time.time() - tracker.start_time)
        if active_tasks.get(task_id, {}).get("cancelled"):
            text = (
                f"🛑 **FORWARDING STOPPED BY USER** 🛑\n\n"
                f"📊 **Processed:** `{tracker.processed_msgs}/{tracker.total_msgs}`\n"
                f"✅ **Sent:** `{tracker.forwarded}`\n"
                f"⏩ **Skipped / Empty:** `{tracker.skipped}`\n"
                f"⏳ **Time Elapsed:** `{elapsed}`\n"
            )
            await bot_client.edit_message(
                chat_id,
                status_msg_id,
                text,
                buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]]
            )
        else:
            text = (
                f"🎉 **FORWARDING COMPLETED!** 🎉\n\n"
                f"📊 **Total Processed:** `{tracker.total_msgs}` messages\n"
                f"✅ **Successfully Forwarded:** `{tracker.forwarded}`\n"
                f"⏩ **Skipped / Empty:** `{tracker.skipped}`\n"
                f"⏱️ **Total Time Taken:** `{elapsed}`\n"
            )
            await bot_client.edit_message(
                chat_id,
                status_msg_id,
                text,
                buttons=[
                    [Button.inline("🚀 Forward Another", b"start_fwd")],
                    [Button.inline("🔙 Main Menu", b"back_to_menu")]
                ]
            )
    except Exception as e:
        await bot_client.edit_message(
            chat_id,
            status_msg_id,
            f"❌ **An error occurred during forwarding:**\n`{str(e)}`",
            buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]]
        )
    finally:
        try:
            if user_client.is_connected():
                await user_client.disconnect()
        except Exception:
            pass
        if os.path.exists(task_temp_dir):
            try:
                shutil.rmtree(task_temp_dir, ignore_errors=True)
            except Exception:
                pass
        active_tasks.pop(task_id, None)

bot = TelegramClient(os.path.join(BASE_DIR, "bot_session"), API_ID, API_HASH)

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    user = await event.get_sender()
    db.add_user(user.id, user.first_name, user.username)
    USER_STATES.pop(user.id, None)
    admin_flag = is_admin(user.id)
    text = (
        f"👋 **Welcome to Ultra Forwarder Bot, {user.first_name}!**\n\n"
        f"⚡ **High-Speed Message & Media Forwarder**\n"
        f"• Preserves captions, thumbnails, duration & media quality.\n"
        f"• Direct instant copy without forward tag where permitted.\n"
        f"• Fast VPS download & upload for restricted channels.\n"
        f"• Live progress with VPS speed in MB/s, ETA, and Stop button.\n"
        f"• Multi-account management with full 2FA support.\n\n"
        f"👇 **Select an option below to proceed:**"
    )
    await event.respond(text, buttons=get_main_keyboard(admin_flag))

@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel_command_handler(event):
    user_id = event.sender_id
    state = USER_STATES.pop(user_id, None)
    if state and "client" in state:
        try:
            if state["client"].is_connected():
                await state["client"].disconnect()
        except Exception:
            pass
    await event.respond("❌ Action cancelled.", buttons=get_main_keyboard(is_admin(user_id)))

@bot.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_command_handler(event):
    user_id = event.sender_id
    if not is_admin(user_id):
        await event.respond("⛔ You are not authorized to use the admin panel.")
        return
    USER_STATES.pop(user_id, None)
    total_users = db.get_user_count()
    total_accs = db.get_total_accounts_count()
    active_cnt = len(active_tasks)
    text = (
        f"👑 **ADMIN CONTROL PANEL** 👑\n\n"
        f"👥 **Total Registered Users:** `{total_users}`\n"
        f"📱 **Total Connected Accounts:** `{total_accs}`\n"
        f"🔄 **Active Forwarding Tasks:** `{active_cnt}`\n"
    )
    buttons = [
        [Button.inline("📢 Broadcast Message", b"admin_broadcast")],
        [Button.inline("📊 Refresh Stats", b"admin_stats")],
        [Button.inline("🔙 Back to Menu", b"back_to_menu")]
    ]
    await event.respond(text, buttons=buttons)

@bot.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    data = event.data.decode("utf-8")
    await event.answer()

    if data == "back_to_menu":
        state = USER_STATES.pop(user_id, None)
        if state and "client" in state:
            try:
                if state["client"].is_connected():
                    await state["client"].disconnect()
            except Exception:
                pass
        text = (
            f"⚡ **Ultra Forwarder Bot - Main Menu**\n\n"
            f"Select an option below to proceed:"
        )
        await event.edit(text, buttons=get_main_keyboard(is_admin(user_id)))
        return

    if data == "cancel_flow":
        state = USER_STATES.pop(user_id, None)
        if state and "client" in state:
            try:
                if state["client"].is_connected():
                    await state["client"].disconnect()
            except Exception:
                pass
        await event.edit("❌ Process cancelled.", buttons=get_main_keyboard(is_admin(user_id)))
        return

    if data.startswith("stop_fwd_"):
        task_id = data.replace("stop_fwd_", "")
        if task_id in active_tasks:
            active_tasks[task_id]["cancelled"] = True
            await event.edit("⏳ Stopping forwarding task, please wait...")
        else:
            await event.edit("ℹ️ Task is no longer active.", buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]])
        return

    if data == "admin_panel":
        if not is_admin(user_id):
            return
        total_users = db.get_user_count()
        total_accs = db.get_total_accounts_count()
        active_cnt = len(active_tasks)
        text = (
            f"👑 **ADMIN CONTROL PANEL** 👑\n\n"
            f"👥 **Total Registered Users:** `{total_users}`\n"
            f"📱 **Total Connected Accounts:** `{total_accs}`\n"
            f"🔄 **Active Forwarding Tasks:** `{active_cnt}`\n"
        )
        buttons = [
            [Button.inline("📢 Broadcast Message", b"admin_broadcast")],
            [Button.inline("📊 Refresh Stats", b"admin_stats")],
            [Button.inline("🔙 Back to Menu", b"back_to_menu")]
        ]
        await event.edit(text, buttons=buttons)
        return

    if data == "admin_stats":
        if not is_admin(user_id):
            return
        total_users = db.get_user_count()
        total_accs = db.get_total_accounts_count()
        active_cnt = len(active_tasks)
        text = (
            f"👑 **ADMIN CONTROL PANEL** 👑\n\n"
            f"👥 **Total Registered Users:** `{total_users}`\n"
            f"📱 **Total Connected Accounts:** `{total_accs}`\n"
            f"🔄 **Active Forwarding Tasks:** `{active_cnt}`\n\n"
            f"🕒 *Updated at:* `{time.strftime('%Y-%m-%d %H:%M:%S')}`"
        )
        buttons = [
            [Button.inline("📢 Broadcast Message", b"admin_broadcast")],
            [Button.inline("📊 Refresh Stats", b"admin_stats")],
            [Button.inline("🔙 Back to Menu", b"back_to_menu")]
        ]
        await event.edit(text, buttons=buttons)
        return

    if data == "admin_broadcast":
        if not is_admin(user_id):
            return
        USER_STATES[user_id] = {"action": "broadcast", "step": "message"}
        await event.edit(
            "📢 **Admin Broadcast**\n\nPlease send the message you want to broadcast to all bot users.\n(You can send text, photo, video, document, audio, or forward any message):",
            buttons=[[Button.inline("❌ Cancel Broadcast", b"cancel_flow")]]
        )
        return

    if data == "confirm_broadcast":
        if not is_admin(user_id):
            return
        broadcast_state = USER_STATES.get(user_id, {})
        if broadcast_state.get("action") != "broadcast_confirm":
            await event.edit("❌ Broadcast session expired.", buttons=[[Button.inline("🔙 Admin Panel", b"admin_panel")]])
            return
        b_msg = broadcast_state["msg"]
        user_ids = db.get_all_user_ids()
        total_u = len(user_ids)
        sent = 0
        failed = 0
        status_msg = await event.edit(f"📢 **Broadcasting to {total_u} users...**\nSent: 0 | Failed: 0")
        for idx, u_id in enumerate(user_ids):
            try:
                if b_msg.media:
                    await bot.send_file(u_id, file=b_msg.media, caption=b_msg.text or "", formatting_entities=b_msg.entities)
                else:
                    await bot.send_message(u_id, message=b_msg.text or "", formatting_entities=b_msg.entities)
                sent += 1
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                try:
                    if b_msg.media:
                        await bot.send_file(u_id, file=b_msg.media, caption=b_msg.text or "", formatting_entities=b_msg.entities)
                    else:
                        await bot.send_message(u_id, message=b_msg.text or "", formatting_entities=b_msg.entities)
                    sent += 1
                except Exception:
                    failed += 1
            except Exception:
                failed += 1
            if (idx + 1) % 10 == 0 or idx == total_u - 1:
                try:
                    await status_msg.edit(f"📢 **Broadcasting to {total_u} users...**\nSent: `{sent}` | Failed: `{failed}`")
                except Exception:
                    pass
            await asyncio.sleep(0.05)
        USER_STATES.pop(user_id, None)
        await status_msg.edit(
            f"✅ **Broadcast Completed!**\n\n"
            f"👥 **Total Target Users:** `{total_u}`\n"
            f"✅ **Successfully Sent:** `{sent}`\n"
            f"❌ **Failed / Blocked:** `{failed}`",
            buttons=[[Button.inline("🔙 Admin Panel", b"admin_panel")]]
        )
        return

    if data == "list_acc":
        accounts = db.get_user_accounts(user_id)
        if not accounts:
            text = (
                f"📋 **Your Connected Accounts**\n\n"
                f"ℹ️ You have not added any Telegram accounts yet.\n"
                f"Click 'Add Account' to connect one."
            )
            buttons = [
                [Button.inline("➕ Add Account", b"add_acc")],
                [Button.inline("🔙 Main Menu", b"back_to_menu")]
            ]
            await event.edit(text, buttons=buttons)
            return
        text = f"📋 **Your Connected Accounts ({len(accounts)}):**\n\n"
        for i, acc in enumerate(accounts, 1):
            text += (
                f"{i}. 👤 **Name:** {acc['first_name']}\n"
                f"   📱 **Phone:** `{acc['phone']}`\n"
                f"   🆔 **TG ID:** `{acc['tg_user_id']}`\n"
                f"   📅 **Added:** `{acc['created_at']}`\n\n"
            )
        buttons = [
            [Button.inline("➕ Add Another Account", b"add_acc"), Button.inline("🗑️ Remove Account", b"rm_acc")],
            [Button.inline("🚀 Start Forward", b"start_fwd")],
            [Button.inline("🔙 Main Menu", b"back_to_menu")]
        ]
        await event.edit(text, buttons=buttons)
        return

    if data == "rm_acc":
        accounts = db.get_user_accounts(user_id)
        if not accounts:
            await event.edit(
                "ℹ️ You have no connected accounts to remove.",
                buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]]
            )
            return
        text = "🗑️ **Select an account to remove:**\n\nClick on any account below to disconnect and delete it:"
        buttons = []
        for acc in accounts:
            buttons.append([Button.inline(f"❌ {acc['phone']} ({acc['first_name']})", f"del_acc_{acc['id']}".encode("utf-8"))])
        buttons.append([Button.inline("🔙 Main Menu", b"back_to_menu")])
        await event.edit(text, buttons=buttons)
        return

    if data.startswith("del_acc_"):
        acc_id = int(data.replace("del_acc_", ""))
        db.delete_account(acc_id, user_id)
        await event.edit(
            "✅ **Account removed successfully!**",
            buttons=[
                [Button.inline("📋 View Accounts", b"list_acc")],
                [Button.inline("🔙 Main Menu", b"back_to_menu")]
            ]
        )
        return

    if data == "add_acc":
        USER_STATES[user_id] = {"action": "add_account", "step": "phone"}
        await event.edit(
            "📱 **Add Telegram Account (2FA Supported)**\n\n"
            "Please send your phone number with international country code:\n"
            "Example: `+919876543210` or `+12025550123`\n\n"
            "Send `/cancel` at any time to abort.",
            buttons=get_cancel_keyboard()
        )
        return

    if data == "start_fwd":
        user_tasks = [t for t in active_tasks.values() if t.get("user_id") == user_id]
        if user_tasks:
            await event.edit(
                "⚠️ **You already have an active forwarding task!**\n"
                "Please wait for it to complete or click Stop on its progress message.",
                buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]]
            )
            return
        accounts = db.get_user_accounts(user_id)
        if not accounts:
            await event.edit(
                "⚠️ **No Telegram Account Connected!**\n\n"
                "You need to add at least one Telegram account so the bot can access and forward messages from your channels.\n"
                "Click below to connect your account:",
                buttons=[
                    [Button.inline("➕ Add Account", b"add_acc")],
                    [Button.inline("🔙 Main Menu", b"back_to_menu")]
                ]
            )
            return
        if len(accounts) == 1:
            USER_STATES[user_id] = {
                "action": "forward",
                "step": "source",
                "account": accounts[0]
            }
            await event.edit(
                f"📥 **Step 1/3: Source Channel / Message**\n\n"
                f"Using Account: `{accounts[0]['phone']} ({accounts[0]['first_name']})`\n\n"
                f"Please send the source message link or start message link:\n"
                f"Examples:\n"
                f"• `https://t.me/c/1234567890/50`\n"
                f"• `https://t.me/channel_username/50`",
                buttons=get_cancel_keyboard()
            )
            return
        text = "📱 **Select Account for Forwarding:**\n\nChoose which connected account to use:"
        buttons = []
        for acc in accounts:
            buttons.append([Button.inline(f"📱 {acc['phone']} ({acc['first_name']})", f"use_acc_{acc['id']}".encode("utf-8"))])
        buttons.append([Button.inline("❌ Cancel", b"cancel_flow")])
        await event.edit(text, buttons=buttons)
        return

    if data.startswith("use_acc_"):
        acc_id = int(data.replace("use_acc_", ""))
        account = db.get_account_by_id(acc_id, user_id)
        if not account:
            await event.edit("❌ Account not found.", buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]])
            return
        USER_STATES[user_id] = {
            "action": "forward",
            "step": "source",
            "account": account
        }
        await event.edit(
            f"📥 **Step 1/3: Source Channel / Message**\n\n"
            f"Using Account: `{account['phone']} ({account['first_name']})`\n\n"
            f"Please send the source message link or start message link:\n"
            f"Examples:\n"
            f"• `https://t.me/c/1234567890/50`\n"
            f"• `https://t.me/channel_username/50`",
            buttons=get_cancel_keyboard()
        )
        return

    if data == "confirm_fwd":
        state = USER_STATES.get(user_id, {})
        if state.get("action") != "forward_confirm":
            await event.edit("❌ Session expired. Please start again.", buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]])
            return
        account = state["account"]
        src_entity = state["src_entity"]
        start_id = state["start_id"]
        end_id = state["end_id"]
        dest_entity = state["dest_entity"]
        USER_STATES.pop(user_id, None)

        task_id = str(uuid.uuid4())[:8]
        active_tasks[task_id] = {
            "task_id": task_id,
            "user_id": user_id,
            "cancelled": False,
            "bot": bot,
            "client": None
        }

        min_id = min(start_id, end_id)
        max_id = max(start_id, end_id)
        total_range = max_id - min_id + 1

        init_text = (
            f"⚡ **FORWARDING IN PROGRESS** ⚡\n\n"
            f"📊 **Progress:** `{progress_bar(0, total_range, 12)}` `0.0%`\n"
            f"🔢 **Messages:** `0/{total_range}`\n"
            f"🎯 **Starting from ID:** `#{min_id}`\n"
            f"📡 **Status:** `Connecting account...`\n"
            f"🚀 **VPS Speed:** `0.00 B/s`\n"
            f"⏳ **Elapsed:** `00m 00s`\n"
            f"⏱️ **ETA:** `Calculating...`\n\n"
            f"✅ **Forwarded:** `0` | ⏩ **Skipped:** `0`\n\n"
            f"⚠️ *Click Stop below to cancel anytime.*"
        )
        status_msg = await event.edit(
            init_text,
            buttons=[[Button.inline("⏹ Stop Forwarding", f"stop_fwd_{task_id}".encode("utf-8"))]]
        )

        asyncio.create_task(
            run_forwarding_task(
                task_id=task_id,
                user_id=user_id,
                chat_id=event.chat_id,
                status_msg_id=status_msg.id,
                account=account,
                src_entity_input=src_entity,
                start_id=start_id,
                end_id=end_id,
                dest_entity_input=dest_entity
            )
        )
        return

@bot.on(events.NewMessage)
async def message_handler(event):
    if event.raw_text.startswith("/"):
        return
    user_id = event.sender_id
    text = event.raw_text.strip()
    state = USER_STATES.get(user_id)
    if not state:
        return

    action = state.get("action")
    step = state.get("step")

    if action == "add_account":
        if step == "phone":
            phone = text.replace(" ", "").replace("-", "")
            if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
                await event.respond("❌ **Invalid Phone Number!** Please include country code, e.g. `+919876543210`:", buttons=get_cancel_keyboard())
                return
            temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
            try:
                await temp_client.connect()
                send_code = await temp_client.send_code_request(phone)
                state["step"] = "code"
                state["client"] = temp_client
                state["phone"] = phone
                state["phone_code_hash"] = send_code.phone_code_hash
                await event.respond(
                    "📩 **OTP Code Sent!**\n\n"
                    "Please check your Telegram app or SMS and enter the login code.\n\n"
                    "💡 *Tip: If Telegram blocks the code in bot chats, enter it with spaces like:* `1 2 3 4 5`",
                    buttons=get_cancel_keyboard()
                )
            except errors.PhoneNumberInvalidError:
                await event.respond("❌ **Invalid Phone Number!** Please check and try again:", buttons=get_cancel_keyboard())
            except errors.FloodWaitError as e:
                await event.respond(f"❌ **Flood Wait:** Telegram requires waiting `{e.seconds}` seconds before trying again.", buttons=get_cancel_keyboard())
            except Exception as e:
                await event.respond(f"❌ **Error:** `{str(e)}`", buttons=get_cancel_keyboard())
            return

        if step == "code":
            code = text.replace(" ", "").replace("-", "")
            temp_client = state["client"]
            phone = state["phone"]
            phone_code_hash = state["phone_code_hash"]
            try:
                await temp_client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                me = await temp_client.get_me()
                session_str = temp_client.session.save()
                await temp_client.disconnect()
                db.add_account(user_id, phone, me.first_name, me.id, session_str)
                USER_STATES.pop(user_id, None)
                await event.respond(
                    f"✅ **Account Successfully Added!**\n\n"
                    f"👤 **Name:** {me.first_name}\n"
                    f"🆔 **Telegram ID:** `{me.id}`\n"
                    f"📱 **Phone:** `{phone}`",
                    buttons=[
                        [Button.inline("🚀 Start Forward", b"start_fwd")],
                        [Button.inline("🔙 Main Menu", b"back_to_menu")]
                    ]
                )
            except errors.SessionPasswordNeededError:
                state["step"] = "password"
                await event.respond(
                    "🔐 **Two-Step Verification (2FA) Required!**\n\n"
                    "This account has a 2FA cloud password enabled.\n"
                    "Please enter your 2FA password to complete login:",
                    buttons=get_cancel_keyboard()
                )
            except errors.PhoneCodeInvalidError:
                await event.respond("❌ **Incorrect OTP Code!** Please check and enter the correct code:", buttons=get_cancel_keyboard())
            except errors.PhoneCodeExpiredError:
                USER_STATES.pop(user_id, None)
                await event.respond("❌ **OTP Expired!** Please restart the process.", buttons=[[Button.inline("🔙 Main Menu", b"back_to_menu")]])
            except errors.FloodWaitError as e:
                await event.respond(f"❌ **Flood Wait:** Please wait `{e.seconds}` seconds.", buttons=get_cancel_keyboard())
            except Exception as e:
                await event.respond(f"❌ **Error:** `{str(e)}`", buttons=get_cancel_keyboard())
            return

        if step == "password":
            password = text
            temp_client = state["client"]
            phone = state["phone"]
            try:
                await temp_client.sign_in(password=password)
                me = await temp_client.get_me()
                session_str = temp_client.session.save()
                await temp_client.disconnect()
                db.add_account(user_id, phone, me.first_name, me.id, session_str)
                USER_STATES.pop(user_id, None)
                await event.respond(
                    f"✅ **Account Added Successfully (2FA Verified)!**\n\n"
                    f"👤 **Name:** {me.first_name}\n"
                    f"🆔 **Telegram ID:** `{me.id}`\n"
                    f"📱 **Phone:** `{phone}`",
                    buttons=[
                        [Button.inline("🚀 Start Forward", b"start_fwd")],
                        [Button.inline("🔙 Main Menu", b"back_to_menu")]
                    ]
                )
            except errors.PasswordHashInvalidError:
                await event.respond("❌ **Incorrect 2FA Password!** Please enter the correct password:", buttons=get_cancel_keyboard())
            except errors.FloodWaitError as e:
                await event.respond(f"❌ **Flood Wait:** Please wait `{e.seconds}` seconds.", buttons=get_cancel_keyboard())
            except Exception as e:
                await event.respond(f"❌ **Error:** `{str(e)}`", buttons=get_cancel_keyboard())
            return

    if action == "forward":
        if step == "source":
            src_entity, start_id = parse_source_link(text)
            if not src_entity or not start_id:
                await event.respond(
                    "❌ **Could not parse source link / message ID!**\n\n"
                    "Please provide a link that includes the message ID, e.g.:\n"
                    "• `https://t.me/c/1234567890/50`\n"
                    "• `https://t.me/channel_username/50`",
                    buttons=get_cancel_keyboard()
                )
                return
            state["src_entity"] = src_entity
            state["start_id"] = start_id
            state["step"] = "end"
            await event.respond(
                f"✅ **Source Registered:**\n"
                f"• Target: `{src_entity}`\n"
                f"• Start ID: `#{start_id}`\n\n"
                f"📤 **Step 2/3: End Message**\n\n"
                f"Please send the ending message link or ID:\n"
                f"Examples:\n"
                f"• `https://t.me/c/1234567890/100`\n"
                f"• `https://t.me/channel_username/100`\n"
                f"• `100`",
                buttons=get_cancel_keyboard()
            )
            return

        if step == "end":
            end_id = parse_end_link(text)
            if not end_id or end_id < 1:
                await event.respond("❌ **Invalid End Message ID!** Please enter a valid number or link:", buttons=get_cancel_keyboard())
                return
            state["end_id"] = end_id
            state["step"] = "dest"
            await event.respond(
                f"✅ **End Message Registered:** `#{end_id}`\n\n"
                f"🎯 **Step 3/3: Destination**\n\n"
                f"Please send the destination channel, group, or chat link / ID / username:\n"
                f"Examples:\n"
                f"• `https://t.me/c/1234567890`\n"
                f"• `https://t.me/destination_channel`\n"
                f"• `@destination_channel`\n"
                f"• `-1001234567890`\n"
                f"• `me` (for your Saved Messages)",
                buttons=get_cancel_keyboard()
            )
            return

        if step == "dest":
            dest_entity = parse_dest_link(text)
            state["dest_entity"] = dest_entity
            state["action"] = "forward_confirm"

            account = state["account"]
            src_entity = state["src_entity"]
            start_id = state["start_id"]
            end_id = state["end_id"]
            min_id = min(start_id, end_id)
            max_id = max(start_id, end_id)
            total = max_id - min_id + 1

            confirm_text = (
                f"📋 **Confirm Forwarding Task:**\n\n"
                f"📱 **Using Account:** `{account['phone']} ({account['first_name']})`\n"
                f"📥 **Source:** `{src_entity}` (Start ID: `#{min_id}`)\n"
                f"📤 **Ending at ID:** `#{max_id}`\n"
                f"🔢 **Total Messages in Range:** `{total}`\n"
                f"🎯 **Destination:** `{dest_entity}`\n\n"
                f"⚡ *Direct forward without forward tag where permitted.*\n"
                f"⚡ *Automatic fast download & upload where restricted.*\n\n"
                f"Click **Start Forwarding** to begin!"
            )
            buttons = [
                [Button.inline("▶️ Start Forwarding", b"confirm_fwd")],
                [Button.inline("❌ Cancel", b"cancel_flow")]
            ]
            await event.respond(confirm_text, buttons=buttons)
            return

    if action == "broadcast":
        if step == "message":
            total_u = db.get_user_count()
            USER_STATES[user_id] = {
                "action": "broadcast_confirm",
                "msg": event.message
            }
            confirm_text = (
                f"⚠️ **Confirm Broadcast**\n\n"
                f"You are about to broadcast this message to **{total_u}** users.\n\n"
                f"Are you sure you want to proceed?"
            )
            buttons = [
                [Button.inline("✅ Yes, Broadcast Now", b"confirm_broadcast")],
                [Button.inline("❌ Cancel", b"cancel_flow")]
            ]
            await event.respond(confirm_text, buttons=buttons)
            return

def main():
    if not API_ID or not API_HASH or not BOT_TOKEN:
        print("=" * 60)
        print("ERROR: API_ID, API_HASH, and BOT_TOKEN must be configured in .env!")
        print(f"Please create or edit '{os.path.join(BASE_DIR, '.env')}' with your credentials.")
        print("=" * 60)
        return
    print("=" * 60)
    print("Starting Ultra Forwarder Telegram Bot...")
    print(f"Database: {DB_PATH}")
    print(f"Admin ID: {ADMIN_ID if ADMIN_ID else 'Not set'}")
    print("=" * 60)
    bot.start(bot_token=BOT_TOKEN)
    print("Bot is online and polling for updates!")
    bot.run_until_disconnected()

if __name__ == "__main__":
    main()
