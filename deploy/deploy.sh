#!/usr/bin/env bash
# First install and every update on the UAT VM. Run from the repo folder:  bash deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

git pull --ff-only || true

if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p data/searches

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> Created .env from .env.example. Fill in the API keys, then run this script again."
  exit 1
fi

if pm2 describe keyword-studio >/dev/null 2>&1; then
  pm2 reload ecosystem.config.js --update-env
else
  pm2 start ecosystem.config.js
fi
pm2 save

sleep 3
curl -fsS "http://127.0.0.1:${KEYWORD_STUDIO_PORT:-8000}/api/platforms" >/dev/null \
  && echo ">> keyword-studio is up on 127.0.0.1:${KEYWORD_STUDIO_PORT:-8000}" \
  || { echo ">> health check failed; see: pm2 logs keyword-studio"; exit 1; }
