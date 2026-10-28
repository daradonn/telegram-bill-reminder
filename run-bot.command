#!/bin/bash
set -Eeuo pipefail

cd "$(dirname "$0")"

IMAGE_NAME="bill-reminder"
CONTAINER_NAME="bill-reminder-bot"
VOLUME_NAME="bill-reminder-data"
ENV_FILE=".env"

fail() {
  printf 'Error: %s\n' "$1" >&2
  read -r -p "Press Return to close this window..." _
  exit 1
}

if ! command -v podman >/dev/null 2>&1; then
  fail "Podman is not installed or is not available in PATH."
fi

if [[ ! -f "$ENV_FILE" ]]; then
  fail "Missing .env. Copy .env.example to .env and set TELEGRAM_BOT_TOKEN first."
fi

if ! grep -Eq '^TELEGRAM_BOT_TOKEN=.+$' "$ENV_FILE" ||
   grep -Eq '^TELEGRAM_BOT_TOKEN=(replace_|your_|YOUR_)' "$ENV_FILE"; then
  fail "Set a valid TELEGRAM_BOT_TOKEN in .env before starting the bot."
fi

if ! podman info >/dev/null 2>&1; then
  printf 'Starting the Podman machine...\n'
  podman machine start || fail "Could not start the Podman machine."
fi

podman info >/dev/null 2>&1 || fail "Podman is not responding. Check the Podman machine."

if [[ ! -f "Containerfile" || ! -f "bot.py" ]]; then
  fail "Place this launcher beside Containerfile and bot.py."
fi

if podman container exists "$CONTAINER_NAME"; then
  if [[ "$(podman inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" == "true" ]]; then
    printf 'Stopping the existing bot...\n'
    podman stop "$CONTAINER_NAME"
  fi
  printf 'Removing the old container...\n'
  podman rm "$CONTAINER_NAME"
fi

if ! podman volume exists "$VOLUME_NAME"; then
  printf 'Creating persistent data volume...\n'
  podman volume create "$VOLUME_NAME" >/dev/null
fi

printf 'Building the bot image...\n'
podman build -t "$IMAGE_NAME" -f Containerfile .

printf 'Starting the bot...\n'
podman run -d \
  --name "$CONTAINER_NAME" \
  --restart=unless-stopped \
  --env-file "$ENV_FILE" \
  -v "$VOLUME_NAME:/data" \
  "$IMAGE_NAME"

sleep 2
if [[ "$(podman inspect --format '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
  printf 'The bot stopped during startup. Recent logs:\n' >&2
  podman logs --tail 50 "$CONTAINER_NAME" >&2 || true
  fail "Bot startup failed."
fi

printf '\nBot is running. Recent logs:\n'
podman logs --tail 30 "$CONTAINER_NAME"
printf '\nDone. The data volume "%s" was preserved.\n' "$VOLUME_NAME"
printf 'To follow logs: podman logs -f %s\n' "$CONTAINER_NAME"
read -r -p "Press Return to close this window..." _
