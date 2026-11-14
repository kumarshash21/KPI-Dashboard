#!/bin/bash
# Double-click to start the Salesforce dashboard (OAuth client-credentials version).
cd "$(dirname "$0")" || exit 1
PORT=4173
echo "=== Salesforce Dashboard (API credentials) ==="

if [ ! -f .env ] || ! grep -Eq '^SF_CLIENT_ID=.+' .env || ! grep -Eq '^SF_CLIENT_SECRET=.+' .env; then
  echo "Missing credentials. Open the .env file in this folder and fill in SF_CLIENT_ID and SF_CLIENT_SECRET."
  open -e .env 2>/dev/null
  read -n 1 -s -r -p "Press any key to close..."; exit 1
fi

lsof -ti tcp:$PORT | xargs kill 2>/dev/null
( sleep 4; open "http://localhost:$PORT" ) &

echo "Starting dashboard on http://localhost:$PORT  (first load takes 20-40 s)"
echo "Keep this window open. Press Ctrl+C or close it to stop."
python3 -m uvicorn app:app --host 0.0.0.0 --port $PORT
