#!/data/data/com.termux/files/usr/bin/bash
# Работает на ТЕЛЕФОНЕ: батарея, нагрузка и состояние бота одним экраном. Команда `status`.
fastfetch --logo none --pipe -s Battery:CPU:Memory:Uptime 2>/dev/null
echo "Load: $(uptime | sed "s/.*load average: //")"
for s in bot memsync monitor; do tmux has-session -t $s 2>/dev/null && echo "tmux $s: работает" || echo "tmux $s: НЕТ"; done
pgrep -f "python bot.py" >/dev/null && echo "бот: онлайн, последний вход: $(grep 'вошёл как' ~/discord-bot/logs/bot.log | tail -1 | cut -c1-19)" || echo "бот: ОФЛАЙН"
echo "память на GitHub: $(git -C ~/memory-repo log -1 --format='%cd' --date=format:'%F %H:%M')"
