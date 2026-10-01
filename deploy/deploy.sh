#!/bin/bash
# Wdrożenie na kontener (domyślnie 192.168.1.66): kod -> /opt/air-locker-map, venv, restart usługi.
# Pierwsze wdrożenie tworzy /etc/air-locker-map.env z losowym sekretem i hasłem admina
# (hasło wypisuje raz na ekran — zapisz je).
set -euo pipefail
HOST=${1:-claude@192.168.1.66}
cd "$(dirname "$0")/.."
rsync -a --delete --exclude __pycache__ app requirements.txt deploy "$HOST:/tmp/airmap-src/"
ssh "$HOST" 'set -e
sudo rsync -a --delete /tmp/airmap-src/app /tmp/airmap-src/requirements.txt /opt/air-locker-map/
sudo chown -R airmap:airmap /opt/air-locker-map
if [ ! -x /opt/air-locker-map/venv/bin/uvicorn ] || ! sudo cmp -s /tmp/airmap-src/requirements.txt /opt/air-locker-map/.req-installed; then
  sudo -u airmap python3 -m venv /opt/air-locker-map/venv
  sudo -u airmap /opt/air-locker-map/venv/bin/pip install -q --upgrade pip
  sudo -u airmap /opt/air-locker-map/venv/bin/pip install -q -r /opt/air-locker-map/requirements.txt
  sudo cp /tmp/airmap-src/requirements.txt /opt/air-locker-map/.req-installed
fi
if [ ! -f /etc/air-locker-map.env ]; then
  PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
  HASH=$(cd /opt/air-locker-map && sudo -u airmap venv/bin/python -c "import sys;from app.main import hash_password;print(hash_password(sys.argv[1]))" "$PW")
  SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
  printf "AIRMAP_SECRET=%s\nAIRMAP_ADMIN_HASH=%s\n" "$SECRET" "$HASH" | sudo tee /etc/air-locker-map.env >/dev/null
  sudo chown root:airmap /etc/air-locker-map.env && sudo chmod 640 /etc/air-locker-map.env
  echo "HASLO_ADMINA=$PW"
fi
sudo cp /tmp/airmap-src/deploy/air-locker-map.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q air-locker-map
sudo systemctl restart air-locker-map
sleep 3
systemctl is-active air-locker-map
curl -fsS -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8080/api/status'
