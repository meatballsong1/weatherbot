#!/bin/bash
# Start both the bot and the web panel
cd "$(dirname "$0")"
source .env 2>/dev/null || true

echo "Starting WeatherWatch..."
python3 weather_bot.py &
BOT_PID=$!
echo "Bot PID: $BOT_PID"

python3 panel.py &
PANEL_PID=$!
echo "Panel PID: $PANEL_PID"

echo "Bot running. Panel at http://localhost:5000"
wait