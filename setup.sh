#!/bin/bash
# One-time setup. In Terminal, from this folder, run:   bash setup.sh
# It installs the Python packages and writes .env. The bot token you paste is
# hidden while you type and only ever saved in .env on this computer.
set -e
cd "$(dirname "$0")"

echo "1/3  Installing Python packages (about a minute)..."
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

if [ -f .env ]; then
  echo "     .env already exists, so I'm leaving it alone."
  echo "     To add or change the Telegram bot token, run:   bash add-telegram.sh"
  exit 0
fi

echo "2/3  Your ngrok URL (for the connect page and xPay's payment alerts):"
read -r -p "     ngrok URL (looks like https://something.ngrok-free.dev): " NGROK_URL
export NGROK_URL

echo "3/3  Writing .env..."
./.venv/bin/python - <<'EOF'
import os, re, pathlib
from cryptography.fernet import Fernet

url = os.environ["NGROK_URL"].strip().rstrip("/")
if url and not url.startswith("http"):
    url = "https://" + url
values = {"PUBLIC_BASE_URL": url, "SECRET_KEY": Fernet.generate_key().decode()}
text = pathlib.Path(".env.example").read_text()
for key, value in values.items():
    text = re.sub(rf"^{key}=.*$", lambda _m: f"{key}={value}", text, flags=re.M)
env = pathlib.Path(".env")
env.write_text(text)
env.chmod(0o600)
print("     Saved to .env (only readable by you)." if url else
      "     Saved, but PUBLIC_BASE_URL is empty. Fill it in .env before starting.")
EOF
echo
echo "Next, add your Telegram bot token:   bash add-telegram.sh"
