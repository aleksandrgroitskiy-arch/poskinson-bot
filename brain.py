"""Groq: чат с инструментами (поиск, страницы, напоминания), распознавание голосовых."""
import asyncio
import html
import json
import logging
import re
from datetime import datetime

import httpx

from config import CHAT_MODELS, GROQ_KEY, TZ, VOICE_MODEL

log = logging.getLogger("poskinson.brain")

TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск в интернете (DuckDuckGo). Для свежих новостей, цен, событий, фактов, в которых не уверен.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Поисковый запрос"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_page",
        "description": "Открыть страницу по ссылке и прочитать её текст (начало).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "set_reminder",
        "description": "Поставить напоминание человеку, который просит. Бот напишет ему в этот канал в нужное время.",
        "parameters": {"type": "object", "properties": {
            "when": {"type": "string", "description": "Когда: дата-время ISO 8601 по Москве, например 2026-10-02T19:30:00+03:00"},
            "text": {"type": "string", "description": "О чём напомнить, коротко"}}, "required": ["when", "text"]}}},
    {"type": "function", "function": {
        "name": "list_reminders",
        "description": "Показать активные напоминания человека, который спрашивает.",
        "parameters": {"type": "object", "properties": {}}}},
]


class RateLimited(Exception):
    pass


class Brain:
    def __init__(self, store):
        self.store = store
        self.http = httpx.AsyncClient(base_url="https://api.groq.com/openai/v1",
                                      headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=90)
        self.web = httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) poskinson"})
        self.blocked = {}   # model → до какого времени упёрлась в лимит

    # ---------- один запрос с перебором моделей ----------
    async def complete(self, messages, models=CHAT_MODELS, tools=None, max_tokens=900, temperature=0.75, json_mode=False):
        last = None
        loop = asyncio.get_running_loop()
        for model in models:
            if self.blocked.get(model, 0) > loop.time():
                continue
            body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
            body["reasoning_effort"] = "none" if model.startswith("qwen/") else "low"
            if tools:
                body["tools"] = tools
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = await self.http.post("/chat/completions", json=body)
            except httpx.HTTPError as e:
                last = f"сеть: {e!r}"
                continue
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]
            last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            log.warning(last)
            if r.status_code == 429:
                wait = float(r.headers.get("retry-after") or 30)
                self.blocked[model] = loop.time() + min(wait, 3600)
        raise RateLimited(last or "все модели в лимите")

    # ---------- ответ в чат с инструментами ----------
    async def chat(self, messages, ctx):
        """ctx: dict(channel_id, user_id) — для напоминаний."""
        text = await self._chat(messages, ctx)
        if CJK.search(text):
            # Qwen иногда срывается в китайский: одна повторная попытка, потом вырезаем
            log.info("иероглифы в ответе, переспрашиваю")
            again = await self._chat(messages + [{"role": "system", "content": "Отвечай строго по-русски, без иероглифов."}], ctx)
            text = again if not CJK.search(again) else again
        return fix_script(text)

    async def _chat(self, messages, ctx):
        msgs = list(messages)
        for _ in range(4):
            m = await self.complete(msgs, tools=TOOLS)
            calls = m.get("tool_calls") or []
            if not calls:
                return clean_text(m.get("content") or "")
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = await self.run_tool(c["function"]["name"], args, ctx)
                log.info("инструмент %s %s → %s", c["function"]["name"], args, result[:120].replace("\n", " "))
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result[:2500]})
        m = await self.complete(msgs)
        return clean_text(m.get("content") or "")

    async def run_tool(self, name, args, ctx):
        try:
            if name == "web_search":
                return await self.search(args.get("query", ""))
            if name == "open_page":
                return await self.open_page(args.get("url", ""))
            if name == "set_reminder":
                return self.set_reminder(args, ctx)
            if name == "list_reminders":
                rows = self.store.user_reminders(ctx["user_id"])
                if not rows:
                    return "активных напоминаний нет"
                return "\n".join(f"#{r['id']} {datetime.fromtimestamp(r['due'], TZ):%d.%m %H:%M} — {r['text']}" for r in rows)
        except Exception as e:
            log.exception("инструмент %s", name)
            return f"ошибка: {e}"
        return "нет такого инструмента"

    def set_reminder(self, args, ctx):
        when = (args.get("when") or "").strip()
        try:
            dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return f"не понял время «{when}», нужен ISO вроде 2026-10-02T19:30:00+03:00"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        now = datetime.now(TZ)
        if dt <= now:
            return f"это время уже прошло (сейчас {now:%d.%m %H:%M})"
        rid = self.store.add_reminder(ctx["channel_id"], ctx["user_id"], dt.timestamp(), (args.get("text") or "").strip()[:300])
        return f"готово: напоминание #{rid} на {dt.astimezone(TZ):%d.%m.%Y %H:%M} (Москва)"

    # ---------- интернет ----------
    async def search(self, query):
        if not query:
            return "пустой запрос"

        def run():
            from ddgs import DDGS
            return DDGS().text(query, region="ru-ru", max_results=5)

        try:
            res = await asyncio.wait_for(asyncio.to_thread(run), 25)
        except Exception as e:
            return f"поиск не сработал: {e}"
        if not res:
            return "ничего не нашлось"
        return "\n\n".join(f"{r.get('title', '')}\n{r.get('href', '')}\n{(r.get('body') or '')[:250]}" for r in res)

    async def open_page(self, url):
        if not re.match(r"^https?://", url or ""):
            return "нужна ссылка http(s)"
        r = await self.web.get(url)
        text = r.text
        text = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        return text[:3000] or "страница пустая"

    # ---------- голосовые ----------
    async def transcribe(self, data, filename="voice.ogg"):
        r = await self.http.post("/audio/transcriptions",
                                 files={"file": (filename, data, "audio/ogg")},
                                 data={"model": VOICE_MODEL, "language": "ru", "response_format": "json"})
        if r.status_code != 200:
            raise RuntimeError(f"whisper: HTTP {r.status_code} {r.text[:200]}")
        return (r.json().get("text") or "").strip()


CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]+")
LAT2CYR = str.maketrans("AaBEeKMHOoPpCcTXxy", "АаВЕеКМНОоРрСсТХху")


def fix_script(text):
    """Срывы модели: иероглифы — вон; латинские буквы-двойники внутри русских слов — в кириллицу."""
    text = CJK.sub("", text)

    def word(m):
        w = m.group(0)
        cyr = sum("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in w)
        if cyr and cyr >= len(w) / 2:
            return w.translate(LAT2CYR)
        return w
    text = re.sub(r"[A-Za-zА-Яа-яЁё]+", word, text)
    # одиночная латинская буква-двойник перед русским словом («A теперь»)
    text = re.sub(r"\b([AaOoEeCcKkMmTtXxPpBbHy])(?=\s+[а-яё])", lambda m: m.group(1).translate(LAT2CYR), text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def clean_text(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
