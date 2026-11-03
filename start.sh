#!/bin/bash
# Starts the bot on port 8000. Keep this Terminal window open while you test.
# ngrok must be running in another window:   ngrok http 8000
cd "$(dirname "$0")"
if [ ! -x .venv/bin/uvicorn ]; then
  echo "Run   bash setup.sh   first."
  exit 1
fi
exec ./.venv/bin/uvicorn app.main:create_app --factory --port 8000
