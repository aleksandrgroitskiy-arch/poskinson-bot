#!/usr/bin/env python3
"""Сводка лимитов бота для виджета angelOS (плагин bot-monitor): JSON в stdout.

  tools/limits.py          — из базы бота (memory.db, таблица usage) и лога, к нейросетям не ходит
  tools/limits.py --live   — плюс по 1 крошечному запросу к моделям Groq: сколько запросов на сутки осталось
"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

SHORT = {"qwen/qwen3.8-27b": "Qwen", "openai/gpt-oss-120b": "gpt-oss 120b", "openai/gpt-oss-20b": "gpt-oss 20b",
         "@cf/qwen/qwen3.8-27b": "Qwen", "@cf/mistralai/mistral-small-3.1-24b-instruct": "Mistral",
         "@cf/openai/gpt-oss-120b": "gpt-oss 120b", "@cf/openai/gpt-oss-20b": "gpt-oss 20b"}
PROV = {"groq": "Groq", "cloudflare": "Cloudflare", "openrouter": "OpenRouter", "zai": "Z.ai", "mistral": "Mistral"}


def short(p, m):
    name = SHORT.get(m) or m.split("/")[-1].replace(":free", "")
    return f"{PROV.get(p, p)} · {name}"


def main():
    out = {"updated": time.strftime("%H:%M"), "version": config.VERSION}
    if shutil.which("systemctl"):
        out["active"] = subprocess.run(["systemctl", "--user", "is-active", "--quiet", "poskinson"]).returncode == 0
    else:  # Termux на телефоне: бот крутится в tmux-сессии bot
        out["active"] = subprocess.run(["pgrep", "-f", "python bot.py"], capture_output=True).returncode == 0
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = sqlite3.connect(f"file:{HERE / 'memory.db'}?mode=ro", uri=True)
    rows = {}
    for p, m, req, err, ti, to in db.execute("SELECT provider, model, requests, errors, tok_in, tok_out FROM usage WHERE day=?", (day,)):
        p = "groq" if p in config.GROQ_EXTRA else p   # все ключи Groq — в виджете одна строка на модель
        a = rows.get((p, m), (0, 0, 0))
        rows[(p, m)] = (a[0] + req, a[1] + err, a[2] + ti + to)
    models, seen = [], set()
    # сначала модели болтовни по порядку, потом всё, что сегодня тратилось
    order = [pm for pm in config.MODELS["chat"] if pm[0] == "groq"] + list(rows)
    for p, m in order:
        if (p, m) in seen or (p != "groq" and (p, m) not in rows):
            continue
        seen.add((p, m))
        req, err, tok = rows.get((p, m), (0, 0, 0))
        lim = config.DAILY_TOKEN_LIMITS.get((p, m))
        rlim = config.DAILY_REQUEST_LIMITS.get(p)
        if lim:
            pct, label = tok / lim, f"{tok / 1000:.1f}k / {lim // 1000}k ток."
        elif rlim:
            pct, label = req / rlim, f"{req} / {rlim} запр."
        else:
            pct, label = None, f"{tok / 1000:.1f}k ток."
        models.append({"name": short(p, m), "pct": pct, "label": label, "requests": req, "errors": err})
    out["models"] = models[:7]
    main_ = next((x for x in models if x["pct"] is not None), None)
    out["main_pct"] = main_["pct"] if main_ else 0
    out["tokens"] = sum(v[2] for v in rows.values())
    out["requests"] = sum(v[0] for v in rows.values())
    out["errors"] = sum(v[1] for v in rows.values())
    today = datetime.now(config.TZ).date().isoformat()
    out["caps"] = {k.split(":")[1]: [int(v), config.DAILY_CAPS.get(k.split(":")[1], 0)] for k, v in
                   db.execute("SELECT key, value FROM settings WHERE guild_id=0 AND key LIKE ?", (f"cap:%:{today}",))}
    replies = last = 0
    log = HERE / "logs" / "bot.log"
    if log.exists():
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]:
            if line.startswith(today) and "ответ " in line and "(" in line:
                replies += 1
                last = line[11:16]
            elif line.startswith(today) and ("лимит Groq" in line or "мана" in line):
                out["limited_at"] = line[11:16]
    out["replies"] = replies
    out["last_reply"] = last or "—"
    if "--live" in sys.argv:
        out["live"] = live()
    print(json.dumps(out, ensure_ascii=False))


def live():
    import urllib.request
    res = []
    for n, key in enumerate((config.key(config.PROVIDERS[g]["key"]) for g in ("groq", *config.GROQ_EXTRA)), 1):
      if not key:
        continue
      for m in ("qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        body = json.dumps({"model": m, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", body,
                                     {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                      "User-Agent": "poskinson-monitor"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                h = r.headers
                res.append({"name": SHORT[m], "ok": True,
                            "left": f"{h.get('x-ratelimit-remaining-requests')}/{h.get('x-ratelimit-limit-requests')} запр."})
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")
            mm = re.search(r"on (tokens per day|requests per day|tokens per minute)[^:]*: Limit (\d+), Used (\d+)", msg)
            res.append({"name": SHORT[m], "ok": False,
                        "left": f"лимит {mm.group(1)}: {mm.group(3)}/{mm.group(2)}" if mm else f"HTTP {e.code}"})
        except Exception as e:
            res.append({"name": SHORT[m], "ok": False, "left": type(e).__name__})
    return res


if __name__ == "__main__":
    main()
