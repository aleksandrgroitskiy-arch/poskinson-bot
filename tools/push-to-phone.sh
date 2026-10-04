#!/usr/bin/env bash
# Заливает код бота на телефон и перезапускает его в tmux. Память и ключи не трогает.
set -euo pipefail
cd "$(dirname "$0")/.."

rsync -av --delete \
    --exclude '.env' --exclude 'memory.db' --exclude 'backups/' --exclude 'logs/' \
    --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' --exclude '*.bak*' \
    ./ phone:~/discord-bot/

# respawn-pane перезапускает ту же команду в той же сессии — сторож keepalive.sh не успеет поднять «пропавшую» вторую копию
ssh phone 'tmux respawn-pane -k -t bot 2>/dev/null || tmux new -d -s bot "cd ~/discord-bot && while true; do python bot.py; sleep 15; done"'
echo "✓ код залит, бот перезапущен"
