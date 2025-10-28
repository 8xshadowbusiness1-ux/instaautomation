#!/bin/bash
python3 -m pip install --upgrade pip
pip install -r requirements.txt

# Run the bot (auto restart if it crashes)
while true; do
  echo "🚀 Starting bot at $(date)"
  python3 bot.py
  echo "⚠️ Bot crashed — restarting in 10 seconds..."
  sleep 10
done
