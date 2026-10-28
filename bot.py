import html
import json
import logging
import os
import re
import shlex
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
LEGACY_CHAT_ID = os.getenv("LEGACY_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Phnom_Penh"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INVOICE_DIR = DATA_DIR / "invoices"
DB_PATH = DATA_DIR / "tasks.sqlite3"
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_FILE_SIZE = 20 * 1024 * 1024

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("bill-reminder-bot")


def connect_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def db_connection():
    connection = connect_db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INVOICE_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                due_date TEXT NOT NULL,
                recurring_day INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                last_reminded_date TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                task_id INTEGER REFERENCES tasks(id),
                uploaded_at TEXT NOT NULL
            )
            """
        )
        task_columns = {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}
        invoice_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(invoices)")
        }
        if "chat_id" not in task_columns:
            db.execute("ALTER TABLE tasks ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''")
        if "chat_id" not in invoice_columns:
            db.execute(
                "ALTER TABLE invoices ADD COLUMN chat_id TEXT NOT NULL DEFAULT ''"
            )
        if LEGACY_CHAT_ID.isdigit():
            db.execute(
                "UPDATE tasks SET chat_id = ? WHERE chat_id = ''", (LEGACY_CHAT_ID,)
            )
            db.execute(
                "UPDATE invoices SET chat_id = ? WHERE chat_id = ''",
                (LEGACY_CHAT_ID,),
            )


def telegram(method, payload=None):
    body = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{API}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        result = json.loads(response.read())
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {result}")
    return result["result"]


def reply_markup(rows):
    return {"inline_keyboard": rows}


def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram("sendMessage", payload)


def callback_button(label, data):
    return {"text": label, "callback_data": data}


def pending_tasks(chat_id):
    with db_connection() as db:
        return db.execute(
            "SELECT id, title, due_date FROM tasks "
            "WHERE chat_id = ? AND status = 'pending' ORDER BY due_date, id",
            (chat_id,),
        ).fetchall()


def parse_monthly_date(day, today=None):
    if not 1 <= day <= 31:
        raise ValueError("The monthly day must be between 1 and 31.")
    today = today or date.today()
    year, month = today.year, today.month
    while True:
        month_day = min(day, (date(year + (month == 12), month % 12 + 1, 1)
                              - date.resolution).day)
        candidate = date(year, month, month_day)
        if candidate >= today:
            return candidate.isoformat()
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def add_task(chat_id, title, due_date, recurring_day=None):
    with db_connection() as db:
        db.execute(
            "INSERT INTO tasks (chat_id, title, due_date, recurring_day) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, title, due_date, recurring_day),
        )


def complete_task(chat_id, task_id):
    today = datetime.now(TIMEZONE).date()
    with db_connection() as db:
        task = db.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ? "
            "AND status = 'pending'",
            (task_id, chat_id),
        ).fetchone()
        if task is None:
            return False
        db.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ? AND chat_id = ?",
            (task_id, chat_id),
        )
        if task["recurring_day"] is not None:
            due = date.fromisoformat(task["due_date"])
            year, month = due.year, due.month
            while True:
                if month == 12:
                    year, month = year + 1, 1
                else:
                    month += 1
                next_day = min(
                    task["recurring_day"],
                    (date(year + (month == 12), month % 12 + 1, 1)
                     - date.resolution).day,
                )
                next_due = date(year, month, next_day)
                if next_due > today:
                    break
            db.execute(
                "INSERT INTO tasks (chat_id, title, due_date, recurring_day) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, task["title"], next_due.isoformat(), task["recurring_day"]),
            )
        return True


def delete_task(chat_id, task_id):
    with db_connection() as db:
        cursor = db.execute(
            "DELETE FROM tasks WHERE id = ? AND chat_id = ? AND status = 'pending'",
            (task_id, chat_id),
        )
        return cursor.rowcount > 0


def edit_task(chat_id, task_id, title=None, due_date=None):
    with db_connection() as db:
        task = db.execute(
            "SELECT id FROM tasks WHERE id = ? AND chat_id = ? AND status = 'pending'",
            (task_id, chat_id),
        ).fetchone()
        if task is None:
            return False
        if title is not None:
            db.execute(
                "UPDATE tasks SET title = ? WHERE id = ? AND chat_id = ?",
                (title, task_id, chat_id),
            )
        if due_date is not None:
            db.execute(
                "UPDATE tasks SET due_date = ? WHERE id = ? AND chat_id = ?",
                (due_date, task_id, chat_id),
            )
        return True


def send_test_alert(chat_id):
    send_message(
        chat_id,
        "Test alert: this is a sample reminder. "
        "If you see this, alert delivery is working.",
    )
def show_tasks(chat_id):
    tasks = pending_tasks(chat_id)
    if not tasks:
        send_message(chat_id, "No pending tasks. Add one with /add.")
        return
    rows = []
    lines = ["Pending tasks:"]
    for task in tasks:
        lines.append(f"• {html.escape(task['title'])} — due {task['due_date']}")
        rows.append(
            [
                callback_button(f"Mark paid: {task['title'][:20]}", f"done:{task['id']}"),
                callback_button(f"Delete: {task['title'][:20]}", f"delete:{task['id']}"),
            ]
        )
    send_message(chat_id, "\n".join(lines), reply_markup(rows))


def help_message(chat_id):
    send_message(
        chat_id,
        "Bill reminder bot\n\n"
        "Add a one-time task:\n"
        "/add \"Pay electricity\" 2026-11-01\n\n"
        "Add a bill that repeats monthly:\n"
        "/add \"Pay electricity\" monthly 1\n\n"
        "/tasks — list pending tasks\n"
        "/edit (or /update) TASK_ID \"New title\" [YYYY-MM-DD] — edit a task\n"
        "/delete (or /remove) [TASK_ID] — delete a task\n"
        "/test (or /testalert) — send a test alert\n"
        "/help — show this help\n\n"
        "After the due date, pending tasks are reminded daily. "
        "Send an invoice photo or document here to attach it to a task. "
        "The bot will ask before marking that task paid."
    )


def handle_add(chat_id, text):
    try:
        parts = shlex.split(text)
        if len(parts) < 3 or not parts[1]:
            raise ValueError
        title = parts[1]
        if len(parts) == 4 and parts[2].lower() == "monthly":
            day = int(parts[3])
            due = parse_monthly_date(day, datetime.now(TIMEZONE).date())
            add_task(chat_id, title, due, day)
            send_message(
                chat_id,
                f"Added monthly task: {html.escape(title)} (due day {day})."
            )
            return
        if len(parts) == 3:
            due = date.fromisoformat(parts[2]).isoformat()
            add_task(chat_id, title, due)
            send_message(
                chat_id,
                f"Added task: {html.escape(title)} (due {due})."
            )
            return
        raise ValueError
    except (ValueError, IndexError):
        send_message(
            chat_id,
            'Usage: /add "Task title" YYYY-MM-DD\n'
            'or: /add "Task title" monthly DAY (1–31)'
        )


def handle_edit(chat_id, text):
    try:
        parts = shlex.split(text)
        if len(parts) < 3:
            raise ValueError
        task_id = int(parts[1])
        title = parts[2]
        due_date = None
        if len(parts) >= 4:
            due_date = date.fromisoformat(parts[3]).isoformat()
        if not edit_task(chat_id, task_id, title=title, due_date=due_date):
            send_message(chat_id, "This task is already done or no longer exists.")
            return
        message = f"Updated task {task_id}: {html.escape(title)}"
        if due_date:
            message += f" (due {due_date})"
        send_message(chat_id, message)
    except (ValueError, IndexError):
        send_message(
            chat_id,
            'Usage: /edit TASK_ID "New title" [YYYY-MM-DD]\\n'
            "Example: /edit 3 \"Pay electricity\" 2026-12-01",
        )


def handle_delete_command(chat_id, text):
    parts = text.split()
    if len(parts) >= 2:
        try:
            task_id = int(parts[1])
        except ValueError:
            send_message(chat_id, "Usage: /delete TASK_ID")
            return
        if delete_task(chat_id, task_id):
            send_message(chat_id, "Task deleted.")
        else:
            send_message(chat_id, "This task is already done or no longer exists.")
        return
    tasks = pending_tasks(chat_id)
    if not tasks:
        send_message(chat_id, "No pending tasks to delete.")
        return
    rows = [
        [callback_button(f"Delete: {task['title'][:35]}", f"delete:{task['id']}")]
        for task in tasks
    ]
    send_message(chat_id, "Select a task to delete:", reply_markup=rows)


def handle_test(chat_id):
    send_test_alert(chat_id)


def download_telegram_file(chat_id, file_id, original_name):
    metadata = telegram("getFile", {"file_id": file_id})
    remote_path = metadata["file_path"]
    url = f"https://api.telegram.org/file/bot{TOKEN}/{remote_path}"
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=60) as response:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_FILE_SIZE:
            raise ValueError("File is larger than the 20 MB bot download limit.")
        contents = response.read(MAX_FILE_SIZE + 1)
    if len(contents) > MAX_FILE_SIZE:
        raise ValueError("File is larger than the 20 MB bot download limit.")
    suffix = Path(original_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".jpg" if not suffix else ".bin"
    user_invoice_dir = INVOICE_DIR / str(chat_id)
    user_invoice_dir.mkdir(parents=True, exist_ok=True)
    destination = user_invoice_dir / f"{uuid.uuid4().hex}{suffix}"
    destination.write_bytes(contents)
    return destination


def receive_invoice(chat_id, message):
    document = message.get("document")
    if document:
        file_id = document["file_id"]
        filename = document.get("file_name", "invoice.bin")
    else:
        photo = message["photo"][-1]
        file_id = photo["file_id"]
        filename = "invoice.jpg"
    path = download_telegram_file(chat_id, file_id, filename)
    invoice_id = uuid.uuid4().hex[:12]
    with db_connection() as db:
        db.execute(
            "INSERT INTO invoices (id, chat_id, file_path, uploaded_at) "
            "VALUES (?, ?, ?, ?)",
            (invoice_id, chat_id, str(path), datetime.now(TIMEZONE).isoformat()),
        )
    tasks = pending_tasks(chat_id)
    if not tasks:
        send_message(
            chat_id,
            "Invoice saved, but there are no pending tasks to attach it to. "
            "Add a task with /add."
        )
    elif len(tasks) == 1:
        attach_invoice(chat_id, invoice_id, tasks[0]["id"])
    else:
        rows = [
            [callback_button(task["title"][:50], f"attach:{invoice_id}:{task['id']}")]
            for task in tasks
        ]
        send_message(
            chat_id,
            "Which pending task should this invoice be attached to?",
            reply_markup(rows),
        )


def attach_invoice(chat_id, invoice_id, task_id):
    with db_connection() as db:
        invoice = db.execute(
            "SELECT id FROM invoices WHERE id = ? AND chat_id = ?",
            (invoice_id, chat_id),
        ).fetchone()
        task = db.execute(
            "SELECT id, title FROM tasks WHERE id = ? AND chat_id = ? "
            "AND status = 'pending'",
            (task_id, chat_id),
        ).fetchone()
        if invoice is None or task is None:
            send_message(chat_id, "That invoice or task is no longer available.")
            return
        db.execute(
            "UPDATE invoices SET task_id = ? WHERE id = ? AND chat_id = ?",
            (task_id, invoice_id, chat_id),
        )
    send_message(
        chat_id,
        f"Invoice attached to <b>{html.escape(task['title'])}</b>. "
        "Has this bill been paid?",
        reply_markup=reply_markup(
            [[
                callback_button("Yes, mark paid", f"paid:{task_id}"),
                callback_button("No, keep pending", f"keep:{task_id}"),
            ]]
        ),
    )


def handle_callback(chat_id, callback):
    data = callback.get("data", "")
    telegram("answerCallbackQuery", {"callback_query_id": callback["id"]})
    try:
        parts = data.split(":")
        action = parts[0]
        if action == "attach" and len(parts) == 3:
            attach_invoice(chat_id, parts[1], int(parts[2]))
        elif action in ("done", "paid") and len(parts) == 2:
            if complete_task(chat_id, int(parts[1])):
                send_message(
                    chat_id, "Marked as paid. Reminders for this bill have stopped."
                )
            else:
                send_message(chat_id, "This task is already done or no longer exists.")
        elif action == "delete" and len(parts) == 2:
            if delete_task(chat_id, int(parts[1])):
                send_message(chat_id, "Task deleted.")
            else:
                send_message(chat_id, "This task is already done or no longer exists.")
        elif action == "keep" and len(parts) == 2:
            send_message(
                chat_id,
                "Kept pending. I’ll continue daily reminders after the due date.",
            )
    except (ValueError, IndexError):
        logger.warning("Ignoring invalid callback data")


def handle_message(message):
    chat = message.get("chat", {})
    if chat.get("type") != "private":
        logger.info("Ignoring a message outside a private chat")
        return
    chat_id = str(chat["id"])
    text = message.get("text", "")
    if text.startswith("/add"):
        handle_add(chat_id, text)
    elif text.startswith("/edit") or text.startswith("/update"):
        handle_edit(chat_id, text)
    elif text.startswith("/delete") or text.startswith("/remove"):
        handle_delete_command(chat_id, text)
    elif text.startswith("/test") or text.startswith("/testalert"):
        handle_test(chat_id)
    elif text.startswith("/tasks"):
        show_tasks(chat_id)
    elif text.startswith("/help") or text.startswith("/start"):
        help_message(chat_id)
    elif message.get("photo") or message.get("document"):
        try:
            receive_invoice(chat_id, message)
        except (urllib.error.URLError, ValueError, KeyError) as error:
            logger.error("Could not save invoice: %s", error)
            send_message(
                chat_id,
                f"Could not save that invoice: {html.escape(str(error))}",
            )


def handle_update(update):
    callback = update.get("callback_query")
    if callback:
        callback_message = callback.get("message", {})
        callback_chat = callback_message.get("chat", {})
        sender = callback.get("from", {})
        if (
            callback_chat.get("type") == "private"
            and str(callback_chat.get("id")) == str(sender.get("id"))
        ):
            handle_callback(str(callback_chat["id"]), callback)
        return
    message = update.get("message")
    if message:
        handle_message(message)


def send_due_reminders():
    now = datetime.now(TIMEZONE)
    if now.hour < REMINDER_HOUR:
        return
    today = now.date()
    with db_connection() as db:
        tasks = db.execute(
            "SELECT id, chat_id, title, due_date FROM tasks "
            "WHERE chat_id != '' AND status = 'pending' AND due_date <= ? "
            "AND (last_reminded_date IS NULL OR last_reminded_date != ?)",
            (today.isoformat(), today.isoformat()),
        ).fetchall()
    for task in tasks:
        text = f"Reminder: <b>{html.escape(task['title'])}</b> was due {task['due_date']}."
        markup = reply_markup(
            [[callback_button("Mark paid", f"done:{task['id']}")]]
        )
        try:
            telegram(
                "sendMessage",
                {
                    "chat_id": task["chat_id"],
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": markup,
                },
            )
        except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
            logger.error(
                "Could not send reminder to chat %s: %s", task["chat_id"], error
            )
            continue
        with db_connection() as db:
            db.execute(
                "UPDATE tasks SET last_reminded_date = ? "
                "WHERE id = ? AND chat_id = ? AND status = 'pending'",
                (today.isoformat(), task["id"], task["chat_id"]),
            )


def main():
    if not 0 <= REMINDER_HOUR <= 23:
        raise ValueError("REMINDER_HOUR must be between 0 and 23.")
    initialize_db()
    offset = None
    while True:
        try:
            send_due_reminders()
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset
            updates = telegram("getUpdates", params)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handle_update(update)
                except (urllib.error.URLError, RuntimeError, sqlite3.Error) as error:
                    logger.exception("Could not process Telegram update: %s", error)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as error:
            logger.error("Telegram connection failed: %s", error)
            time.sleep(5)


if __name__ == "__main__":
    main()
