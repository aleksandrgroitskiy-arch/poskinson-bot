"""Маршрутизатор нейросетей: перебирает модели всех провайдеров по ролям (chat/light/vision),
обходит лимиты (429 → модель «отдыхает»), считает расход по дням (таблица usage)."""
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from config import MODELS, PROVIDERS, key

log = logging.getLogger("poskinson.llm")


class RateLimited(Exception):
    pass


class Router:
    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT, provider TEXT, model TEXT, requests INTEGER,"
                   " errors INTEGER, tok_in INTEGER, tok_out INTEGER, PRIMARY KEY (day, provider, model))")
        db.commit()
        self.clients = {}
        for name, p in PROVIDERS.items():
            k = key(p["key"])
            if k:
                self.clients[name] = httpx.AsyncClient(
                    base_url=p["base"], timeout=90,
                    headers={"Authorization": f"Bearer {k}", **p.get("headers", {})})
        self.available = {}       # provider → set(model ids) из каталога; нет записи — каталог не прочитан
        self.resting = {}         # (provider, model) → monotonic до какого времени не трогать
        log.info("провайдеры с ключами: %s", ", ".join(self.clients) or "нет")

    async def discover(self):
        """Прочитать каталоги моделей: чего нет — не предлагать."""
        for name, c in self.clients.items():
            try:
                r = await c.get("/models")
                if r.status_code == 200:
                    data = r.json()
                    items = data.get("data", data) if isinstance(data, dict) else data
                    self.available[name] = {m.get("id") for m in items if isinstance(m, dict)}
                else:
                    log.warning("%s: каталог HTTP %s %s", name, r.status_code, r.text[:150])
            except Exception as e:
                log.warning("%s: каталог не прочитан: %r", name, e)
        for role, lst in MODELS.items():
            ok = [f"{p}:{m}" for p, m in lst if self.usable(p, m)]
            log.info("роль %s: %s", role, ", ".join(ok) or "НИЧЕГО")

    def usable(self, provider, model):
        if provider not in self.clients:
            return False
        cat = self.available.get(provider)
        return cat is None or model in cat

    # ---------- учёт ----------
    def _day(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _count(self, provider, model, ok, usage=None):
        u = usage or {}
        self.db.execute(
            "INSERT INTO usage VALUES (?,?,?,1,?,?,?) ON CONFLICT(day, provider, model) DO UPDATE SET "
            "requests=requests+1, errors=errors+excluded.errors, tok_in=tok_in+excluded.tok_in, tok_out=tok_out+excluded.tok_out",
            (self._day(), provider, model, 0 if ok else 1, u.get("prompt_tokens", 0), u.get("completion_tokens", 0)))
        self.db.commit()

    def used_today(self, provider):
        r = self.db.execute("SELECT SUM(requests) FROM usage WHERE day=? AND provider=?", (self._day(), provider)).fetchone()
        return r[0] or 0

    def report(self):
        rows = self.db.execute("SELECT provider, model, requests, errors, tok_in, tok_out FROM usage WHERE day=?"
                               " ORDER BY requests DESC", (self._day(),)).fetchall()
        return [tuple(r) for r in rows]

    # ---------- запрос ----------
    def candidates(self, role):
        now = time.monotonic()
        out = []
        for p, m in MODELS[role]:
            if not self.usable(p, m) or self.resting.get((p, m), 0) > now:
                continue
            daily = PROVIDERS[p].get("daily")
            if daily and self.used_today(p) >= daily:
                continue
            out.append((p, m))
        return out

    async def complete(self, messages, role="chat", tools=None, max_tokens=900, temperature=0.75, json_mode=False):
        last = None
        for p, m in self.candidates(role):
            body = {"model": m, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            if p == "groq":
                body["reasoning_effort"] = "none" if m.startswith("qwen/") else "low"
            elif "gpt-oss" in m:
                body["reasoning_effort"] = "low"
            if tools:
                body["tools"] = tools
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = await self.clients[p].post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                last = f"{p}:{m} сеть {e!r}"
                self.resting[(p, m)] = time.monotonic() + 60
                self._count(p, m, False)
                continue
            if r.status_code == 200:
                data = r.json()
                self._count(p, m, True, data.get("usage"))
                msg = data["choices"][0]["message"]
                msg["_model"] = f"{p}:{m}"
                if tools is None and not (msg.get("content") or "").strip():
                    last = f"{p}:{m} пустой ответ"
                    continue
                return msg
            self._count(p, m, False)
            last = f"{p}:{m} HTTP {r.status_code} {r.text[:200]}"
            log.warning(last)
            self.resting[(p, m)] = time.monotonic() + self._rest(r)
            if json_mode and r.status_code == 400:
                # модель не умеет json-режим: без него, следующая попытка
                continue
        raise RateLimited(last or f"нет доступных моделей для роли {role}")

    @staticmethod
    def _rest(r):
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            try:
                wait = float(ra)
            except (TypeError, ValueError):
                wait = 60
            if re.search(r"per day|daily|RPD|TPD|requests per day|tokens per day", r.text, re.I):
                wait = max(wait, 3 * 3600)
            return min(wait, 12 * 3600)
        if r.status_code in (401, 403):
            return 6 * 3600            # ключ не тот / регион — надолго
        if r.status_code == 404:
            return 24 * 3600           # модели нет
        return 120                     # 400/5xx — передохнуть

    async def raw(self, provider):
        """Клиент провайдера (для whisper и прочего нестандартного)."""
        return self.clients.get(provider)
