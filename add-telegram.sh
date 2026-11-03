#!/bin/bash
# Saves your Telegram bot token (from @BotFather) in .env and checks that it works.
# Run from this folder:   bash add-telegram.sh
# The token is hidden while you paste and only ever saved in .env on this computer.
cd "$(dirname "$0")"
if [ ! -f .env ] || [ ! -x .venv/bin/python ]; then
  echo "Run the one-time setup first:   bash setup.sh"
  exit 1
fi
read -r -s -p "Paste the bot token from @BotFather: " TG_TOKEN_IN; echo
export TG_TOKEN_IN
./.venv/bin/python - <<'EOF'
import os, re, sys, pathlib
import httpx

token = os.environ["TG_TOKEN_IN"].strip()
if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token):
    sys.exit("That doesn't look like a bot token (it should look like 123456789:AAH...). .env is unchanged.")
try:
    r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
except (httpx.HTTPError, ValueError) as e:
    sys.exit(f"Couldn't reach Telegram ({e.__class__.__name__}). Check your internet and try again. .env is unchanged.")
if not r.get("ok"):
    sys.exit("Telegram says that token is wrong: " + r.get("description", "unknown error")
             + ". Copy it again from @BotFather. .env is unchanged.")

env = pathlib.Path(".env")
text, n = re.subn(r"^TELEGRAM_BOT_TOKEN=.*$", lambda _m: "TELEGRAM_BOT_TOKEN=" + token, env.read_text(), flags=re.M)
if n == 0:
    text = text.rstrip("\n") + "\n\n# Telegram bot token from @BotFather\nTELEGRAM_BOT_TOKEN=" + token + "\n"
env.write_text(text)
env.chmod(0o600)
username = r["result"]["username"]
print(f"Saved. Your bot is @{username}  (https://t.me/{username})")
print("Now restart the bot: press Ctrl+C in its window, then run:   bash start.sh")
EOF
