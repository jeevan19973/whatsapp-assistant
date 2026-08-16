#!/usr/bin/env bash
# Push local code changes to the Oracle VM and rebuild the stack, in one command.
# Run this ON YOUR MAC from anywhere:  ./deploy/push.sh
#
# One-time setup: fill in the two values below (or export them in your shell).
set -euo pipefail

# --- Fill these in once ---
KEY="${ORACLE_KEY:-$HOME/Downloads/ssh-key.key}"   # path to your private key
HOST="${ORACLE_HOST:-ubuntu@<PUBLIC_IP>}"          # ubuntu@<your VM public IP>
# --------------------------

# Resolve the project root (the folder that contains this script's parent).
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_DIR="whatsapp-assistant"

echo "==> Copying code to $HOST ..."
rsync -av --progress \
  -e "ssh -i $KEY" \
  --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
  --exclude '*.db' --exclude '.pytest_cache' \
  "$PROJECT_DIR/" \
  "$HOST:~/$REMOTE_DIR/"

echo "==> Rebuilding and restarting on the VM ..."
ssh -i "$KEY" "$HOST" "cd ~/$REMOTE_DIR && docker compose up -d --build"

echo "==> Done. Recent app logs:"
ssh -i "$KEY" "$HOST" "cd ~/$REMOTE_DIR && docker compose logs --tail 20 app"
