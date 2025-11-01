#!/bin/bash
while true; do
  echo "🚀 Starting InstaAutomation at $(date)"
  python3 instaautomation.py
  echo "⚠️ Bot crashed — restarting in 5 seconds..."
  sleep 5
done
