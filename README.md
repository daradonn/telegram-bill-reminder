# Telegram bill reminder bot

A small Telegram bot that supports multiple users, running in Podman using long
polling. It does not need a public IP address, inbound port, webhook, or
third-party application library. The container needs outbound internet access
and must stay running for reminders to arrive.

## What it does

- Add one-time or monthly recurring tasks.
- Start sending one reminder per day, from the configured local reminder hour,
  when a task is due and still pending.
- Receive invoice photos or documents and save them in the persistent data
  volume.
- Attach an invoice to a pending task and ask before marking it paid.
- Stop reminders when a task is marked paid. Monthly tasks get a new occurrence
  for the next upcoming billing date.
- Keep each user's tasks, invoices, and reminders private to their own Telegram
  private chat. Group chats are ignored.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`. In PowerShell,
   run `Copy-Item .env.example .env`, then edit `.env`. Keep it private.
3. Build and start:

   ```sh
   podman build -t bill-reminder -f Containerfile .
   podman volume create bill-reminder-data 
   podman run -d --name bill-reminder-bot --restart=unless-stopped --env-file .env -v bill-reminder-data:/data bill-reminder
   ```

   If your Podman installation uses the separate compose provider, the
   equivalent command is `podman-compose up -d --build`.
4. View logs with `podman compose logs -f`.

To share the bot, send people its Telegram link (for example,
`https://t.me/YourBotUsername`). Each person must open a **private chat** and
send `/start`. They can then create and manage their own tasks. The bot stores
chat IDs internally to separate users; you do not need to collect their IDs.
Only share the bot with people you trust: anyone who can start a private chat
can use it, and the host administrator can access the stored invoice files.

### macOS one-click rebuild and restart

Put `run-bot.command` beside `Containerfile`, `bot.py`, and `.env`. In Terminal,
run `chmod +x run-bot.command` once, then double-click `run-bot.command` in
Finder whenever you want to stop/remove the old container, rebuild the image,
and start the updated bot. It preserves the `bill-reminder-data` Podman volume.

The `./data` directory contains the SQLite database and uploaded invoices.
Back it up securely. The bot stores invoice files locally and does not encrypt
them; protect the host and backups, and remove files you no longer need.
On an SELinux-enabled Linux host, append `:Z` to the data volume in
`compose.yaml` if Podman reports a volume permission error.

## Chat commands

```text
/add "Pay electricity" monthly 1
/add "Pay water" 2026-11-05
/tasks
/edit 1 "Pay electricity" 2026-12-01
/delete 1
/test
/help
```

For monthly tasks, the day is 1–31; for months with fewer days, the due date is
the month's last day. After sending an invoice, select the task if prompted and
confirm whether the bill has actually been paid. Attaching an invoice alone
does not mark a bill paid.

`TIMEZONE` defaults to `Asia/Phnom_Penh`; `REMINDER_HOUR` defaults to `9` (24-hour
local time) for all users. Existing single-user database records can be
assigned to their original owner by setting `LEGACY_CHAT_ID` in `.env` to that
user's numeric Telegram chat ID for startup. The old `TELEGRAM_CHAT_ID` setting
is also accepted for this migration. Remove the migration setting after the
records are claimed.
