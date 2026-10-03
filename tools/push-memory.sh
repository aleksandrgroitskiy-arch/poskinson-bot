#!/data/data/com.termux/files/usr/bin/bash
# Работает на ТЕЛЕФОНЕ. Снимок memory.db → приватный GitHub-репозиторий poskinson-memory.
# Без аргументов — один раз; с `loop` — каждый день в 05:00 (после ночной пересборки памяти в 4:00).
set -euo pipefail
REPO="$HOME/memory-repo"
BOT="$HOME/discord-bot"

push_once() {
    cd "$BOT"
    git -C "$REPO" pull -q --ff-only origin main
    python - <<PY
import sqlite3, sys
src = sqlite3.connect("memory.db")
dst = sqlite3.connect("$REPO/memory.db.new")
src.backup(dst); dst.close(); src.close()
r = sqlite3.connect("$REPO/memory.db.new").execute("pragma integrity_check").fetchone()[0]
sys.exit(0 if r == "ok" else "integrity_check: " + r)
PY
    mv "$REPO/memory.db.new" "$REPO/memory.db"
    cp "$BOT/backups/memory-latest.md" "$REPO/" 2>/dev/null || true
    cd "$REPO"
    git add -A
    if git diff --cached --quiet; then echo "$(date '+%F %T') без изменений"; return; fi
    git commit -q -m "Память бота $(date '+%F %H:%M') (телефон)"
    git push -q origin main
    echo "$(date '+%F %T') ✓ отправлено на GitHub"
}

if [ "${1:-}" = loop ]; then
    while true; do
        sleep $(( $(date -d 'tomorrow 05:00' +%s) - $(date +%s) ))
        push_once || echo "$(date '+%F %T') ошибка, повтор завтра"
    done
else
    push_once
fi
