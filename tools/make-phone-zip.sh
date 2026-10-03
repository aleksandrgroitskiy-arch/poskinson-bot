#!/usr/bin/env bash
# Собирает zip для переноса бота на телефон (Termux): код + .env + свежая копия памяти.
# Без .venv, .git, логов и *.bak. Результат: ~/poskinson-phone.zip (внутри ключи — не пересылай никому!).
set -euo pipefail
cd "$(dirname "$0")/.."
out=~/poskinson-phone.zip
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
dst="$tmp/discord-bot"
mkdir -p "$dst"
cp *.py CHANGELOG.md README.md "$dst/" 2>/dev/null || cp *.py CHANGELOG.md "$dst/"
cp -r tools tests "$dst/"
cp .env "$dst/.env"
sqlite3 memory.db ".backup '$dst/memory.db'"
find "$dst" -name '__pycache__' -prune -exec rm -rf {} +
rm -f "$out"
python -m zipfile -c "$out" "$dst"  # корень архива = discord-bot/
chmod 600 "$out"
echo "✓ $out ($(du -h "$out" | cut -f1))"
echo "  Дальше: скинь на телефон, потом README.md → раздел «Перенос на телефон (Termux)»."
echo "  ВАЖНО: перед запуском на телефоне останови бота тут: systemctl --user stop poskinson"
