"""Чат с инструментами: поиск, страницы, напоминания, картинки, гифки. Модели — через llm.Router."""
import asyncio
import html
import json
import logging
import re
from datetime import datetime

import httpx

from config import TZ
from llm import RateLimited  # noqa: F401 — реэкспорт для bot.py

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
    {"type": "function", "function": {
        "name": "generate_image",
        "description": "Нарисовать картинку, когда просят нарисовать/сгенерировать/показать картинку. Картинка приложится к твоему ответу.",
        "parameters": {"type": "object", "properties": {
            "prompt_en": {"type": "string", "description": "Подробное описание картинки НА АНГЛИЙСКОМ: объект, стиль, детали"}},
            "required": ["prompt_en"]}}},
    {"type": "function", "function": {
        "name": "send_paste",
        "description": "Кинуть пасту (копипасту) сервера: когда просят пасту или она идеально к месту. Паста отправится целиком после твоего короткого комментария.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Название пасты или о чём она; пусто — случайная"}}}}},
    {"type": "function", "function": {
        "name": "send_gif",
        "description": "Добавить гифку-реакцию ПОСЛЕ твоего текстового ответа (не вместо него — текст пиши всё равно полностью). Изредка, не чаще раза в 10 ответов, и никогда, если просят анекдот, совет или информацию.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Что искать, 1–3 слова по-английски (например: facepalm, crying laughing)"}},
            "required": ["query"]}}},
]


class Brain:
    def __init__(self, store, router, media):
        self.store = store
        self.router = router
        self.media = media
        self.web = httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) poskinson"})

    async def complete(self, messages, role="chat", **kw):
        return await self.router.complete(messages, role=role, **kw)

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
            ctx["model"] = m.get("_model")
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
            if name == "generate_image":
                prompt = (args.get("prompt_en") or "").strip()
                if not prompt:
                    return "нужно описание"
                data, fname, src = await self.media.generate(prompt)
                ctx.setdefault("files", []).append((data, fname))
                return f"картинка готова ({src}) и будет приложена к ответу, просто прокомментируй её"
            if name == "send_gif":
                url = await self.media.gif(args.get("query") or "")
                if not url:
                    return "гифки сейчас недоступны, обойдись словами"
                ctx["gif"] = url
                return "гифка будет отправлена после твоего ответа"
            if name == "send_paste":
                gid = ctx.get("guild_id", 0)
                q = (args.get("query") or "").strip()
                row = self.store.find_paste(gid, q) if q else self.store.random_paste(gid)
                if not row:
                    names = [n for n, _ in self.store.paste_names(gid, limit=40)]
                    return ("такой пасты нет. есть: " + ", ".join(names)) if names else "паст пока нет вообще"
                ctx["paste"] = (row["name"], row["text"])
                return f"паста «{row['name']}» будет отправлена после твоего ответа; сам её текст не повторяй"
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
