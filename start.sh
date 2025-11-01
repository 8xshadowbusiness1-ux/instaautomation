#!/bin/bash
while true; do
  echo "🚀 Starting InstaAutomation at $(date)"
  python3 instaautomation_full.py
  echo "⚠️ Bot crashed — restarting in 5 seconds..."
  sleep 5
done
