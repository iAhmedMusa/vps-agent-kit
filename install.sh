#!/usr/bin/env bash
# VPS Keeper agent installer.
# Run from the unzipped kit folder, after: cp .env.example .env && nano .env
set -euo pipefail

[ "$EUID" -eq 0 ] || { echo "Run as root: sudo bash install.sh"; exit 1; }
cd "$(dirname "$0")"

for f in agent.py vps-agent.service; do
  [ -f "$f" ] || { echo "Missing $f. Run from the kit folder."; exit 1; }
done
[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env && nano .env"; exit 1; }
grep -q '^TELEGRAM_TOKEN=.\+' .env || { echo "TELEGRAM_TOKEN is empty in .env"; exit 1; }
grep -Eq '^(GEMINI_API_KEY|GROQ_API_KEY)=.+' .env || { echo "Set GEMINI_API_KEY and/or GROQ_API_KEY in .env"; exit 1; }

DIR=/opt/vps-agent

echo "==> python3-venv"
if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  apt-get update
  apt-get install -y python3-venv python3-pip
fi

echo "==> files to $DIR"
mkdir -p "$DIR" /var/lib/vps-agent
cp agent.py "$DIR/"
if [ -f "$DIR/.env" ] && ! cmp -s .env "$DIR/.env"; then
  cp "$DIR/.env" "$DIR/.env.bak.$(date +%s)"
  echo "    existing $DIR/.env backed up"
fi
cp .env "$DIR/.env"
chmod 600 "$DIR/.env"

echo "==> python venv + packages"
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q openai python-dotenv "python-telegram-bot[job-queue]"

echo "==> systemd service"
cp vps-agent.service /etc/systemd/system/vps-agent.service
systemctl daemon-reload
systemctl enable vps-agent
systemctl restart vps-agent

sleep 3
systemctl --no-pager --lines=0 status vps-agent || true
echo
echo "Installed. Logs: journalctl -u vps-agent -f"
echo "Edit settings later in $DIR/.env, then: systemctl restart vps-agent"
