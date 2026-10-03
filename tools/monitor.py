#!/data/data/com.termux/files/usr/bin/python
"""Работает на ТЕЛЕФОНЕ-хосте. Раз в 5 минут собирает состояние (батарея, память, бот, лимиты, копия памяти)
и шлёт в ntfy.sh: сводка — в топик <T>-status (для виджета на айфоне), тревоги — в <T>-alert (пуши).
Секретный топик <T> лежит в ~/.monitor-topic (не в гите). Запуск: `monitor.py loop` (tmux-сессия monitor) или без аргументов — один раз."""
import json, os, shutil, subprocess, sys, time, urllib.request

HOME = os.path.expanduser("~")
BOT = f"{HOME}/discord-bot"
STATE_FILE = f"{HOME}/.monitor-state.json"
TOPIC = open(f"{HOME}/.monitor-topic").read().strip()
INTERVAL = 300
COOLDOWN = 3 * 3600          # повтор той же тревоги не чаще раза в 3 часа
CHARGER_GRACE = 20 * 60      # «не на зарядке» — только если так дольше 20 минут


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=BOT).stdout
    except Exception:
        return ""


def collect():
    s = {"ts": int(time.time())}
    try:
        b = json.loads(run(["fastfetch", "--logo", "none", "--format", "json", "-s", "Battery"]))[0]["result"][0]
        s["battery"] = int(b["capacity"])
        s["charging"] = "Discharging" not in b["status"]
        s["battery_status"] = ", ".join(b["status"])
    except Exception:
        s["battery"] = None
    mem = run(["free", "-m"]).splitlines()
    try:
        p = mem[1].split()
        s["ram_total_mb"], s["ram_avail_mb"] = int(p[1]), int(p[6])
    except Exception:
        pass
    up = run(["uptime"])
    s["load"] = up.split("load average:")[-1].strip().split(",")[0] if "load average" in up else None
    s["uptime"] = run(["uptime", "-p"]).strip()
    d = shutil.disk_usage(HOME)
    s["disk_free_gb"] = round(d.free / 1e9, 1)
    s["bot_online"] = bool(run(["pgrep", "-f", "python bot.py"]).strip())
    try:
        lim = json.loads(run(["python", "tools/limits.py"]))
        s["limit_pct"] = round(lim["main_pct"] * 100)
        s["replies_today"] = lim["replies"]
        s["last_reply"] = lim.get("last_reply")
        s["errors_today"] = lim["errors"]
        s["version"] = lim["version"]
    except Exception:
        pass
    try:
        ct = int(run(["git", "-C", f"{HOME}/memory-repo", "log", "-1", "--format=%ct"]).strip())
        s["memory_backup_age_h"] = round((time.time() - ct) / 3600, 1)
    except Exception:
        pass
    return s


def publish(topic, message, title=None, priority=3, tags=None):
    body = {"topic": topic, "message": message, "priority": priority}
    if title:
        body["title"] = title
    if tags:
        body["tags"] = tags
    req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def problems(s, state):
    """Список (ключ, заголовок, текст, приоритет, теги) того, что сейчас не так."""
    out = []
    if not s["bot_online"]:
        out.append(("bot", "Бот упал", "Процесс poskinson не запущен. Цикл в tmux должен поднять его сам, если нет — проверь хост.", 5, ["rotating_light"]))
    b = s.get("battery")
    if b is not None:
        if not s["charging"]:
            since = state.setdefault("discharging_since", s["ts"])
            if s["ts"] - since > CHARGER_GRACE:
                out.append(("charger", "Хост не на зарядке", f"Батарея {b}%, не заряжается уже {(s['ts'] - since) // 60} мин. Проверь зарядку.", 4 if b > 30 else 5, ["electric_plug"]))
        else:
            state.pop("discharging_since", None)
        if b <= 20 and not s["charging"]:
            out.append(("battery", "Батарея хоста садится", f"{b}%, не на зарядке.", 5 if b <= 10 else 4, ["battery"]))
    if s.get("ram_avail_mb") is not None and s["ram_avail_mb"] < 600:
        out.append(("ram", "Мало памяти на хосте", f"Свободно {s['ram_avail_mb']} МБ из {s['ram_total_mb']}.", 3, ["warning"]))
    if s.get("disk_free_gb") is not None and s["disk_free_gb"] < 5:
        out.append(("disk", "Мало места на хосте", f"Свободно {s['disk_free_gb']} ГБ.", 3, ["warning"]))
    if s.get("limit_pct") is not None and s["limit_pct"] >= 85:
        out.append(("limit", "Лимит нейросети почти выбран", f"Основная модель: {s['limit_pct']}% дневного лимита.", 4, ["fuelpump"]))
    if s.get("memory_backup_age_h") is not None and s["memory_backup_age_h"] > 30:
        out.append(("backup", "Копия памяти устарела", f"На GitHub последняя копия {s['memory_backup_age_h']} ч назад.", 3, ["floppy_disk"]))
    return out


def tick():
    state = {}
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        pass
    s = collect()
    try:
        publish(f"{TOPIC}-status", json.dumps(s, ensure_ascii=False), priority=1)
    except Exception as e:
        print(time.strftime("%F %T"), "статус не отправился:", e, flush=True)
    active = problems(s, state)
    sent = state.setdefault("sent", {})
    now = s["ts"]
    for key, title, text, prio, tags in active:
        if now - sent.get(key, 0) >= COOLDOWN:
            try:
                publish(f"{TOPIC}-alert", text, title, prio, tags)
                sent[key] = now
            except Exception as e:
                print(time.strftime("%F %T"), "тревога не отправилась:", e, flush=True)
    for key in list(sent):
        if key not in {a[0] for a in active}:      # проблема ушла — один раз сообщаем, что всё ок
            try:
                publish(f"{TOPIC}-alert", f"Снова в порядке: {key}", "✅ Всё нормально", 2, ["white_check_mark"])
                del sent[key]
            except Exception:
                pass
    json.dump(state, open(STATE_FILE, "w"))
    return s, active


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        while True:
            try:
                tick()
            except Exception as e:
                print(time.strftime("%F %T"), "сбой цикла:", e, flush=True)
            time.sleep(INTERVAL)
    else:
        s, a = tick()
        print(json.dumps(s, ensure_ascii=False, indent=1))
        print("тревоги:", [x[0] for x in a] or "нет")
