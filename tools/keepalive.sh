#!/data/data/com.termux/files/usr/bin/bash
# Сторож сессий на телефоне: раз в минуту проверяет tmux-сессии bot, memsync, monitor и поднимает пропавшие.
# Работает вне tmux (через nohup), поэтому переживает падение tmux-сервера. Запускается из ~/.termux/boot/start.sh.
# Второй экземпляр сам выходит (pid в ~/.keepalive.pid). Лог — ~/discord-bot/logs/keepalive.log.
B=~/discord-bot
PIDFILE=~/.keepalive.pid
LOG=$B/logs/keepalive.log
mkdir -p "$B/logs"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "сторож уже работает (pid $(cat "$PIDFILE"))"
    exit 0
fi
echo $$ > "$PIDFILE"

declare -A CMD=(
    [bot]="cd $B && while true; do python bot.py; sleep 15; done"
    [memsync]="$B/tools/push-memory.sh loop >> $B/logs/push-memory.log 2>&1"
    [monitor]="$B/tools/monitor.py loop >> $B/logs/monitor.log 2>&1"
)

while true; do
    for name in bot memsync monitor; do
        if ! tmux has-session -t "$name" 2>/dev/null; then
            tmux new -d -s "$name" "${CMD[$name]}"
            echo "$(date '+%F %T') сессии $name не было — поднял" >> "$LOG"
        fi
    done
    sleep 60
done
