"""Маршрутизатор нейросетей: перебирает модели всех провайдеров по ролям (chat/light/vision),
обходит лимиты (429 → модель «отдыхает»), считает расход по дням (таблица usage)."""
import json
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from config import BULK_TIMEOUT, MODELS, PROVIDERS, key

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
            if k and not p.get("disabled"):
                self.clients[name] = httpx.AsyncClient(
                    # человек ждёт ответа: подвисший провайдер бросаем через 30 с и идём к следующему
                    base_url=p["base"], timeout=httpx.Timeout(30, connect=8),
                    headers={"Authorization": f"Bearer {k}", **p.get("headers", {})})
        self.available = {}       # provider → set(model ids) из каталога; нет записи — каталог не прочитан
        self.resting = {}         # (provider, model) → monotonic до какого времени не трогать
        self.budget = {}          # (provider, model) → (осталось токенов в минуту, monotonic когда обновится)
        self.chat_times = []      # когда были запросы болтовни — фоновые задачи уступают
        log.info("провайдеры с ключами: %s", ", ".join(self.clients) or "нет")

    async def discover(self):
        """Прочитать каталоги моделей: чего нет — не предлагать."""
        for name, c in self.clients.items():
            if PROVIDERS[name].get("no_catalog"):
                continue                       # каталог по этому адресу не отдаётся — верим списку из config
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
    def busy(self, window=60, n=2):
        """Идёт живая болтовня — фоновым задачам (сводки, память, чтение истории) лучше подождать."""
        now = time.monotonic()
        self.chat_times = [t for t in self.chat_times if now - t < window]
        return len(self.chat_times) >= n

    def candidates(self, role, need=0):
        now = time.monotonic()
        out, tight = [], []
        for p, m in MODELS[role]:
            if not self.usable(p, m) or self.resting.get((p, m), 0) > now:
                continue
            left, reset = self.budget.get((p, m), (None, 0))
            if need and left is not None and reset > now and left < need:
                tight.append((p, m))           # по заголовкам не влезет — в конец очереди, а не мимо
                continue
            daily = PROVIDERS[p].get("daily")
            if daily and self.used_today(p) >= daily:
                continue
            out.append((p, m))
        return out + tight

    async def complete(self, messages, role="chat", tools=None, max_tokens=900, temperature=0.75, json_mode=False,
                       tool_choice=None):
        # крупные фоновые запросы (bulk) думают минуты — им своё время ожидания, болтовне — обычное
        wait = httpx.Timeout(BULK_TIMEOUT, connect=15) if role == "bulk" else httpx.USE_CLIENT_DEFAULT
        last = None
        if role == "chat":
            self.chat_times.append(time.monotonic())
        # грубая оценка размера: русский текст ~2.3 символа на токен, плюс ответ
        need = int(len(json.dumps(messages, ensure_ascii=False)) / 2.3 + (len(json.dumps(tools)) / 3 if tools else 0)
                   + max_tokens * 0.5)
        for p, m in self.candidates(role, need):
            body = {"model": m, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            if p.startswith("groq"):
                body["reasoning_effort"] = "none" if m.startswith("qwen/") else "low"
            elif "gpt-oss" in m:
                body["reasoning_effort"] = "low"
            if p == "openrouter":
                # иначе бесплатные модели тратят весь ответ на рассуждения (Nemotron — вслух, Qwen — пустой ответ)
                body["reasoning"] = {"enabled": False, "exclude": True}
            if tools:
                body["tools"] = tools
                if tool_choice:
                    body["tool_choice"] = tool_choice
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = await self.clients[p].post("/chat/completions", json=body, timeout=wait)
                if r.status_code == 400 and "tool_use_failed" in r.text and "tool_choice" in body:
                    # модель не захотела вызывать обязательный инструмент — та же модель, но без принуждения
                    body.pop("tool_choice")
                    r = await self.clients[p].post("/chat/completions", json=body, timeout=wait)
            except httpx.HTTPError as e:
                last = f"{p}:{m} сеть {e!r}"
                log.warning(last)
                self.resting[(p, m)] = time.monotonic() + 60
                self._count(p, m, False)
                continue
            self._remember_budget(p, m, r)
            if r.status_code == 200:
                data = r.json()
                self._count(p, m, True, data.get("usage"))
                msg = data["choices"][0]["message"]
                msg["_model"] = f"{p}:{m}"
                if tools is None and not (msg.get("content") or "").strip():
                    last = f"{p}:{m} пустой ответ"
                    continue
                if tools and not msg.get("tool_calls"):
                    parsed = _text_tool_calls(msg.get("content") or "", tools)
                    if parsed:
                        # модель написала вызов инструмента текстом (JSON) — превращаем в настоящий
                        msg["tool_calls"], msg["content"] = parsed, ""
                        log.info("%s:%s вызвал инструмент текстом — разобрал: %s", p, m,
                                 ", ".join(c["function"]["name"] for c in parsed))
                if re.search(r"<tool_call>|<function=|<\|tool", msg.get("content") or ""):
                    # провайдер сломал вызов инструментов и отдал его текстом — не позоримся, следующая модель
                    last = f"{p}:{m} инструмент текстом"
                    log.warning(last)
                    self.resting[(p, m)] = time.monotonic() + 600
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

    def _remember_budget(self, p, m, r):
        left = r.headers.get("x-ratelimit-remaining-tokens")
        if left is None:
            return
        reset = _seconds(r.headers.get("x-ratelimit-reset-tokens", "60s"))
        try:
            self.budget[(p, m)] = (int(float(left)), time.monotonic() + reset)
        except ValueError:
            pass

    @staticmethod
    def _rest(r):
        if r.status_code == 429:
            ra = r.headers.get("retry-after")
            try:
                wait = float(ra)
            except (TypeError, ValueError):
                wait = 60
            if re.search(r"per day|daily|RPD|TPD|requests per day|tokens per day", r.text, re.I):
                wait = max(wait, 3600)       # дневной лимит: Groq отдаёт точный retry-after, иначе — час
            return min(wait, 12 * 3600)
        if r.status_code == 400 and "tool_use_failed" in r.text:
            return 5                   # ошибка этого запроса, а не модели
        if r.status_code == 402:
            return 24 * 3600           # «нужна оплата» — на сутки
        if r.status_code in (401, 403):
            return 6 * 3600            # ключ не тот / регион — надолго
        if r.status_code == 404:
            return 24 * 3600           # модели нет
        return 120                     # 400/5xx — передохнуть

    async def raw(self, provider):
        """Клиент провайдера (для whisper и прочего нестандартного)."""
        return self.clients.get(provider)


def _seconds(v):
    """«2m59.5s», «7.66s», «120ms» → секунды."""
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", v or ""):
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total or 60.0


def _text_tool_calls(text, tools):
    """[{"name": "set_reminder", "arguments": {...}}] или {"name": …, "parameters": …} текстом → tool_calls."""
    names = {t["function"]["name"] for t in tools}
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    if not (t.startswith("{") or t.startswith("[")):
        m = re.search(r"(\[\s*\{\s*\"name\".*\}\s*\]|\{\s*\"name\".*\})", t, re.S)
        if not m:
            return None
        t = m.group(1)
    try:
        data = json.loads(t)
    except ValueError:
        return None
    items = data if isinstance(data, list) else [data]
    calls = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or it.get("name") not in names:
            return None
        args = it.get("arguments", it.get("parameters", {}))
        calls.append({"id": f"text_call_{i}", "type": "function",
                      "function": {"name": it["name"], "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)}})
    return calls or None
