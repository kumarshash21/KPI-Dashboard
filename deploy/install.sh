#!/usr/bin/env bash
# One-shot installer for Linux with systemd. Run from the project folder:  sudo bash deploy/install.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"

command -v python3 >/dev/null || { echo "python3 is required (3.9+)"; exit 1; }
python3 - <<'PY' || { echo "Python 3.9+ required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

echo "== Creating virtualenv and installing packages =="
sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

[ -f "$APP_DIR/.env" ] || { sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"; echo "Created .env from .env.example"; }
chmod 600 "$APP_DIR/.env"; chown "$RUN_USER" "$APP_DIR/.env"

echo "== Installing systemd service (always on, restarts on crash/reboot) =="
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__USER__|$RUN_USER|g" "$APP_DIR/deploy/sf-dashboard.service" > /etc/systemd/system/sf-dashboard.service
systemctl daemon-reload
systemctl enable sf-dashboard >/dev/null

if grep -Eq '^SF_CLIENT_ID=.+' "$APP_DIR/.env" && grep -Eq '^SF_CLIENT_SECRET=.+' "$APP_DIR/.env"; then
  systemctl restart sf-dashboard
  sleep 3
  systemctl --no-pager --lines=5 status sf-dashboard || true
  echo; echo "Done. Test:  curl http://localhost:4173/api/auth-check"
else
  echo; echo "Service installed but NOT started: fill SF_CLIENT_ID and SF_CLIENT_SECRET in $APP_DIR/.env, then run:"
  echo "  sudo systemctl start sf-dashboard"
fi
