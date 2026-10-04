"""Чат с инструментами: поиск, страницы, напоминания, картинки, гифки. Модели — через llm.Router."""
import asyncio
import html
import importlib.util
import json
import logging
import re
from datetime import datetime
from urllib.parse import unquote

import httpx

import config as cfg
from config import ANSWER_MATCH, ANSWER_TTL_DAYS, ANSWERS_MAX, CALL_RX, REMINDERS_PER_USER, TZ, WIKI_CHARS
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
        "description": "Нарисовать картинку, когда просят нарисовать/сгенерировать/показать картинку. Картинка приложится к твоему ответу. Откровенное 18+ — только если в подсказке сказано, что это 18+ канал (и только взрослые персонажи); иначе откажи в своём стиле. Жесть, кровь, хоррор, мемы — можно везде.",
        "parameters": {"type": "object", "properties": {
            "prompt_en": {"type": "string", "description": "Подробное описание картинки НА АНГЛИЙСКОМ: объект, стиль, детали"}},
            "required": ["prompt_en"]}}},
    {"type": "function", "function": {
        "name": "server_info",
        "description": "База знаний ЭТОГО Майнкрафт-сервера: как зайти, IP, версия, правила, заявки, донат, каналы, анонсы. Вызывай на любой вопрос про сам сервер.",
        "parameters": {"type": "object", "properties": {}}}},
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


# Какие инструменты дать модели — по смыслу сообщения. Описания инструментов стоят ~700 токенов,
# а на «как дела» они не нужны: так влезает в 2 раза больше ответов в минуту.
INTENTS = [
    ({"web_search", "open_page"}, re.compile(
        r"как (?:с?делать|построить|скрафтить|получить|найти|работает|настроить|установить|поставить|зайти|убить|добыть|"
        r"приручить|вырастить|развести|починить|включить|скачать|обновить|исправить)|сколько|какой|какая|какие|какое|"
        r"что такое|что нового|кто такой|кто такая|кто сейчас|зачем|почему|когда|где |крафт|рецепт|ферм|редстоун|"
        r"механик|зачар|верси|мод|плагин|лаунчер|ошибк|краш|новост|курс|цен|стоит|вышел|выйдет|погод|найди|загугли|гугл|"
        r"посмотри|ссылк|http", re.I)),
    ({"server_info"}, re.compile(r"сервер|айпи|\bip\b|зайти|заявк|вайтлист|правил|донат|канал|версия|админ|модер|ивент", re.I)),
    ({"set_reminder", "list_reminders"}, re.compile(r"напомн|напоминан|будильник|через \d+ ?(?:мин|час|сек|день|дн)|в \d{1,2}:\d{2}", re.I)),
    ({"generate_image"}, re.compile(r"нарису|рисуй|сгенер|картинк|арт|изобрази|покажи как выглядит", re.I)),
    ({"send_paste"}, re.compile(r"(?<![а-яё])(?:копи)?паст(?:а|у|ы|е|ой|ами|ах)?(?![а-яё])", re.I)),   # не «попасть»
    ({"send_gif"}, re.compile(r"гиф|gif", re.I)),
]


# вопросы по механикам Майнкрафта: память моделей врёт (маяк, лисы…) — сначала обязательно поиск по вики
MC_HOWTO = re.compile(r"(?<![а-яё]{2})крафт|рецепт|ферм|редстоун|механик|зачар|приручи|развести|разводить|вырастить|спавн(?:ятся|ится)|"
                      r"где найти|как получить|как добыть|как сделать|как построить|сколько (?:блоков|нужно|стоит|хп|урон)|"
                      r"дроп|лут|биом|данж|крепост|бастион|энд|незер|элитр|маяк|зелье|варить|"
                      r"житель|торгов|голем|иссушител|дракон|вард(?:ен)?|шалкер|трезубец|"
                      r"портал|нижн(?:ий|ем|его|ему) мир|визер|(?<![а-яё])(?:в|из|до) (?:ад|аду|аде|край|крае|края)(?![а-яё])",
                      re.I)                     # «ад», «край» — так игроки зовут Незер и Энд


# вопросы про вещи из мира (кино, аниме, игры, музыка, люди, места): память модели врёт — сначала поиск в сети
FACT_RX = re.compile(r"аниме|манг[аиу]|сериал|фильм|кино|мульт|игр[аыуе]|игру|книг|песн|трек|альбом|групп[аыу]|исполнител|"
                     r"режисс|актёр|актер|персонаж|герой|сезон|серия|серии|эпизод|студи[яи]|разработчик|"
                     r"знаешь|слышал|смотрел|играл|читал|что за|кто так(?:ой|ая|ие)|что так(?:ое|ой)|про что|о чём|о чем|"
                     r"расскажи|объясни|правда что|правда ли|скольк|в каком году|когда вышел|откуда|кто создал|кто сделал", re.I)
NAMED_RX = re.compile(r"[«\"“][^»\"”]{2,60}[»\"”]|(?<=[a-zа-яё,] )[A-ZА-ЯЁ][\w'-]{2,}|\b[A-Za-z][A-Za-z0-9'-]{3,}\b")


def needs_facts(text, talk=False):
    """Нужно ли проверить факты в сети до ответа (есть вопрос про конкретную вещь, а не просто болтовня)."""
    text = text or ""
    if FACT_RX.search(text):
        return True
    return talk and bool(NAMED_RX.search(text))


def pick_tools(text, talk=False):
    names = set()
    if needs_facts(text, talk):
        names |= {"web_search", "open_page"}
    for group, rx in INTENTS:
        if rx.search(text or ""):
            names |= group
    if MC_HOWTO.search(text or ""):
        names |= {"web_search", "open_page"}     # вопрос по Майнкрафту — всегда с поиском
    return [t for t in TOOLS if t["function"]["name"] in names]


class Brain:
    def __init__(self, store, router, media):
        self.store = store
        self.router = router
        self.media = media
        self.web = httpx.AsyncClient(timeout=15, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) poskinson"})
        self.wiki_cache = {}

    async def complete(self, messages, role="chat", **kw):
        return await self.router.complete(messages, role=role, **kw)

    # ---------- ответ в чат с инструментами ----------
    async def chat(self, messages, ctx):
        """ctx: dict(channel_id, user_id) — для напоминаний."""
        try:
            text = await self._chat(messages, ctx)
        except RateLimited:
            # вся болтовня в лимите — резерв без инструментов, лишь бы не молчать
            m = await self.complete(messages, role="fallback", temperature=temp(ctx))
            ctx["model"] = m.get("_model")
            text = clean_text(m.get("content") or "")
        if CJK.search(text):
            # Qwen иногда срывается в китайский: одна повторная попытка, потом вырезаем
            # без инструментов и без повторного поиска: те же сообщения (с уже найденным), что видела модель
            log.info("иероглифы в ответе, переспрашиваю")
            try:
                m = await self.complete(ctx.get("_base", messages) + [{"role": "system", "content": "Отвечай строго по-русски, без иероглифов."}],
                                        temperature=temp(ctx))
                again = clean_text(m.get("content") or "")
                if again and not CJK.search(again):
                    text = again
            except RateLimited:
                pass
        text = fix_script(text)
        if self.store is not None:
            self.remember_answer(text, ctx)
        return text

    async def _chat(self, messages, ctx):
        msgs = list(messages)
        tools = pick_tools(ctx.get("text", ""), ctx.get("talk")) if "text" in ctx else TOOLS
        force = False
        q = ctx.get("text", "")
        can_search = any(t["function"]["name"] == "web_search" for t in tools or [])
        mc = bool(MC_HOWTO.search(q)) and can_search
        rounds = 4
        if mc or (needs_facts(q, ctx.get("talk")) and can_search):
            note = await self.prepare_facts(q, mc, ctx)
            if ctx.get("_article"):
                tools = [t for t in tools if t["function"]["name"] not in ("web_search", "open_page")]
            rounds = 2                          # уже искали: ещё один поиск максимум, дальше — ответ или «не знаю»
            ctx["_facts"] = True                # фактический вопрос: температура ниже, чтобы не присочинял мимоходом
            if note:
                # в системное сообщение, а не отдельным system посреди переписки (Cloudflare такое не принимает)
                msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n\n" + note}
        ctx["_base"] = list(msgs)               # для переспроса без инструментов
        said = []                              # текст, который модель написала вместе с вызовом инструмента
        for _ in range(rounds):
            choice = {"type": "function", "function": {"name": "web_search"}} if force else None
            force = False                      # только на первом шаге
            m = await self.complete(msgs, tools=tools or None, max_tokens=600, tool_choice=choice, temperature=temp(ctx))
            ctx["model"] = m.get("_model")
            calls = m.get("tool_calls") or []
            if not calls:
                final = clean_text(m.get("content") or "")
                visible = re.sub(r"(?im)^\s*(?:запомни|отношение)\b.*$", "", final).strip()
                if not visible and not said:
                    # модель ответила одними служебными строками — переспросить без инструментов
                    m2 = await self.complete(msgs + [{"role": "system", "content": "Ответь человеку текстом, коротко."}],
                                             temperature=temp(ctx))
                    final = clean_text(m2.get("content") or "") + ("\n" + final if final else "")
                return "\n".join(x for x in said + [final] if x and x not in final) if said else final
            if (m.get("content") or "").strip():
                said.append(clean_text(m["content"]))
            msgs.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = await self.run_tool(c["function"]["name"], args, ctx)
                log.info("инструмент %s %s → %s", c["function"]["name"], args, result[:120].replace("\n", " "))
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result[:2500]})
        m = await self.complete(msgs, temperature=temp(ctx))
        return clean_text(m.get("content") or "")

    async def prepare_facts(self, q, mc, ctx):
        """До ответа: запомненный проверенный ответ → иначе поиск (для Майнкрафта — сама статья вики) →
        ничего не нашлось — велим честно сказать «не знаю». Возвращает заметку для подсказки."""
        clean_q = re.sub(CALL_RX.pattern + r"[,!]?", "", q, flags=re.I).strip()[:200]
        key = qkey(clean_q)
        cacheable = (self.store is not None and not FRESH_RX.search(clean_q) and not PERSONAL_RX.search(POLITE_RX.sub(" ", clean_q))
                     and len(key) >= 2)
        if cacheable:
            row, score = self.store.find_answer(key, ANSWER_MATCH, ANSWER_TTL_DAYS * 86400)
            if row:
                self.store.hit_answer(row["id"])
                ctx["answer_id"] = row["id"]
                log.info("запомненный ответ #%s (%.2f): %s", row["id"], score, row["question"][:60])
                return (f"Ты уже отвечал на похожий вопрос («{row['question']}»), ответ проверен по {row['source']}:\n"
                        f"{row['answer']}\nЕсли спрашивают то же — ответь так же по сути, своими словами. "
                        "Если вопрос про другое — поищи (web_search) или честно скажи, что не знаешь.")
        if mc:
            # вопрос без предмета («да, как его скрафтить») — предмет берём из последних реплик
            topic = clean_q if wiki_words(clean_q) else (ctx.get("recent", "") + " " + clean_q)
            title = await self.wiki_find(topic)
            article = await self.wiki_text(title, topic) if title else None
            found = article or ""
            if not article:
                found = await self.search(clean_q + " майнкрафт site:ru.minecraft.wiki")
                if found.startswith(("ничего", "поиск не")):
                    found = await self.search(clean_q + " minecraft wiki")
                article = await self.wiki_article(found, clean_q)
            log.info("поиск по вики заранее: %s → %s", clean_q[:60], (article or found)[:80].replace("\n", " "))
            source, head = "вики", ("Найдено в вики по этому вопросу. Отвечай ТОЛЬКО тем, что написано ниже, своими словами, "
                                    "коротко. Ничего не добавляй от себя: как добыть ингредиенты, советы, цифры — только если "
                                    "это есть в тексте; чего нет — не упоминай вовсе:\n")
            text = article or found[:2000]
        else:
            found = await self.search(" ".join(wiki_words(clean_q)) or clean_q)
            log.info("проверка фактов заранее: %s → %s", clean_q[:60], found[:80].replace("\n", " "))
            source, head = "сети", ("Найдено в сети по теме сообщения (опирайся строго на это; названия и типы вещей "
                                    "бери отсюда, ничего не выдумывай; если тут нет ответа или найденное про другое — "
                                    "честно скажи, что не знаешь, или поищи точнее через web_search):\n")
            text = found[:2000]
        if found.startswith(("ничего", "поиск не", "пустой")):
            return ("Поиск по этому вопросу ничего не дал. Не придумывай ответ: честно скажи, что не знаешь/не нашёл "
                    "(можно посоветовать глянуть вики или спросить на сервере).")
        ctx["_article"] = bool(mc and article)
        if cacheable and mc and article:         # запоминаем только проверенное по вики (частые вопросы сервера)
            ctx["_grounded"] = (key, clean_q, source)
        return head + text

    def remember_answer(self, text, ctx):
        """Проверенный поиском ответ на частый вопрос — запомнить (кроме «не знаю» и пустого)."""
        g = ctx.get("_grounded")
        answer = re.sub(r"(?im)^\s*(?:запомни|отношение)\b.*$", "", text or "").strip()
        if not g or len(answer) < 20 or UNSURE_RX.search(answer):
            return
        key, question, source = g
        ctx["answer_id"] = self.store.save_answer(key, question, answer[:1500], source, ANSWER_MATCH, ANSWERS_MAX)
        log.info("запомнил ответ #%s: %s", ctx["answer_id"], question[:60])

    async def wiki_article(self, found, question=""):
        """Статья ru.minecraft.wiki из результатов поиска, чьё название ближе всего к вопросу → текст для модели.
        Ни одно название не совпало с вопросом — None (лучше сниппеты, чем статья не про то)."""
        titles = [unquote(t).replace("_", " ") for t in re.findall(r"https://ru\.minecraft\.wiki/w/([^\s#?]+)", found or "")]
        want = qkey(question)
        scored = [(len(qkey(t.split(":")[-1]) & want), -i, t) for i, t in enumerate(titles)]
        scored = [x for x in scored if x[0] > 0 or not want]
        if not scored:
            return None
        return await self.wiki_text(max(scored)[2], question)

    async def wiki_api(self, base, **params):
        r = await self.web.get(base, headers={"User-Agent": "poskinson-bot (Discord bot)"},
                               params={"format": "json", **params})
        return r.json()

    async def wiki_find(self, question):
        """Статья ru.minecraft.wiki по вопросу — через API самой вики (без поисковика): точные названия
        («кирпичи» → «Кирпич») + поиск по названиям, включая «Руководство:». None — ничего похожего."""
        words = wiki_words(question)
        if not words:
            return None
        api = "https://ru.minecraft.wiki/api.php"
        cands, exact = [], set()
        try:
            variants = {v.capitalize() for w in words for v in (w, w[:-1], w[:-2]) if len(v) >= 3}
            pair = " ".join(words[:2]).capitalize()
            variants |= {pair, "Руководство:" + pair} if len(words) > 1 else set()
            data = await self.wiki_api(api, action="query", titles="|".join(sorted(variants)[:50]), redirects=1)
            exact = {p["title"] for p in data["query"].get("pages", {}).values() if "missing" not in p}
            cands += sorted(exact)
            stem = lambda w: w[:3] if len(w) <= 4 else w[:4] if len(w) <= 6 else w[:5]
            tries = [words[:3], words[1:3], words[:1]] if len(words) > 2 else [words[:2], words[:1]]
            for ws in (x for x in tries if x):
                q = " ".join(f"intitle:{stem(w)}*" for w in ws)
                data = await self.wiki_api(api, action="query", list="search", srsearch=q, srlimit=8,
                                           srnamespace="0|10014", srprop="")
                found = [x["title"] for x in data["query"]["search"]]
                cands += found
                if found:
                    break
        except Exception as e:
            log.warning("поиск по вики: %r", e)
            return None
        def hits(title):                         # сколько слов вопроса есть в названии (с учётом окончаний)
            tw = re.findall(r"[а-яёa-z]+", title.split(":")[-1].lower())
            return sum(any(same_word(w, x) for x in tw) for w in words)
        scored = [(hits(t), t in exact, -len(t), t) for t in dict.fromkeys(cands)]
        scored = [x for x in scored if x[0] > 0]
        return max(scored)[3] if scored else None

    async def wiki_search(self, query):
        """Запасной поиск без ddgs (на телефоне его нет): русская Википедия — названия, сниппеты и начало лучшей статьи."""
        api = "https://ru.wikipedia.org/w/api.php"
        try:
            data = await self.wiki_api(api, action="query", list="search", srsearch=query, srlimit=4)
            hits = data["query"]["search"]
            if not hits:
                return "ничего не нашлось"
            data = await self.wiki_api(api, action="query", prop="extracts", exintro=1, explaintext=1,
                                       titles=hits[0]["title"], redirects=1)
            intro = next(iter(data["query"]["pages"].values())).get("extract", "")[:1200]
        except Exception as e:
            return f"поиск не сработал: {e}"
        snip = lambda x: html.unescape(re.sub(r"<[^>]+>", "", x.get("snippet", "")))
        out = [f"{hits[0]['title']} (Википедия)\n{intro}"]
        out += [f"{h['title']} (Википедия)\n{snip(h)}" for h in hits[1:]]
        return "\n\n".join(out)

    async def wiki_text(self, title, question=""):
        """Статья вики: рецепты крафта (из шаблонов — в чистом тексте их нет) + вступление + разделы,
        ближайшие к вопросу, в пределах WIKI_CHARS."""
        if title not in self.wiki_cache:
            # «браузерный» User-Agent вики встречает капчей, честный бот — пускает
            api, ua = "https://ru.minecraft.wiki/api.php", {"User-Agent": "poskinson-bot (Discord bot)"}
            try:
                r = await self.web.get(api, headers=ua, params={
                    "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "titles": title, "format": "json"})
                page = next(iter(r.json()["query"]["pages"].values()))
                text = page.get("extract") or ""
                title = page.get("title") or title
                r = await self.web.get(api, headers=ua, params={
                    "action": "parse", "page": title, "prop": "wikitext", "redirects": 1, "format": "json"})
                recipes = wiki_recipes(r.json().get("parse", {}).get("wikitext", {}).get("*", ""))
            except Exception as e:
                log.warning("статья вики %s: %r", title, e)
                return None
            self.wiki_cache[title] = (text, recipes)
        text, recipes = self.wiki_cache[title]
        if not text and not recipes:
            return None
        parts = re.split(r"\n(?===+ )", text)
        intro, sections = parts[0].strip(), [x.strip() for x in parts[1:] if len(x.strip().splitlines()) > 1]
        want = qkey(question)
        sections.sort(key=lambda x: -len(qkey(x.splitlines()[0]) & want) * 3 - len(qkey(x[:600]) & want))
        out = f"[статья «{title}»]\n" + ("Рецепты крафта:\n" + "\n".join(recipes) + "\n\n" if recipes else "") + intro
        for sec in sections:
            if len(out) + len(sec) > WIKI_CHARS:
                break
            out += "\n\n" + re.sub(r"\n{3,}", "\n\n", sec)
        return out[:WIKI_CHARS + 500]

    async def run_tool(self, name, args, ctx):
        try:
            if name == "web_search":
                return await self.search(args.get("query", ""))
            if name == "open_page":
                return await self.open_page(args.get("url", ""))
            if name == "set_reminder":
                return self.set_reminder(args, ctx)
            if name == "generate_image":
                if ctx.get("image_ok") and not ctx["image_ok"](ctx["user_id"]):
                    return "лимит картинок для этого человека на час исчерпан — скажи ему подождать"
                prompt = (args.get("prompt_en") or "").strip()
                if not prompt:
                    return "нужно описание"
                try:
                    data, fname, src = await self.media.generate(prompt, nsfw=ctx.get("nsfw", False))
                except ValueError:
                    return ("такое не рисую (только в 18+ канале и только взрослые) — откажи в своём стиле"
                            if not ctx.get("nsfw") else "с несовершеннолетними — никогда, откажи жёстко")
                ctx.setdefault("files", []).append((data, fname))
                return f"картинка готова ({src}) и будет приложена к ответу, просто прокомментируй её"
            if name == "send_gif":
                url = await self.media.gif(args.get("query") or "")
                if not url:
                    return "гифки сейчас недоступны, обойдись словами"
                ctx["gif"] = url
                return "гифка будет отправлена после твоего ответа"
            if name == "server_info":
                kb = ctx.get("kb") or ""
                return kb or "база знаний сервера пока пустая — отправь человека в инфо-каналы и к админам, ничего не выдумывай"
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
        if len(self.store.user_reminders(ctx["user_id"])) >= REMINDERS_PER_USER:
            return f"у него уже {REMINDERS_PER_USER} напоминаний — больше нельзя"
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

        if importlib.util.find_spec("ddgs") is None:
            # на телефоне ddgs не ставится — ищем в Википедии (без site:, он там не работает)
            plain = re.sub(r"\bsite:\S+", "", query).strip()
            if re.search(r"minecraft|майн", query, re.I):
                title = await self.wiki_find(plain)
                text = await self.wiki_text(title, plain) if title else None
                if text:
                    return text
            return await self.wiki_search(plain)

        try:
            res = await asyncio.wait_for(asyncio.to_thread(run), 25)
        except Exception as e:
            return f"поиск не сработал: {e}"
        if not res:
            return "ничего не нашлось"
        return "\n\n".join(f"{r.get('title', '')}\n{r.get('href', '')}\n{(r.get('body') or '')[:250]}" for r in res)

    async def open_page(self, url):
        m = re.match(r"https?://ru\.minecraft\.wiki/w/([^\s#?]+)", url or "")
        if m:                                   # вики — через её API (сама страница отдаёт капчу)
            text = await self.wiki_text(unquote(m.group(1)).replace("_", " "))
            if text:
                return text
        if not re.match(r"^https?://", url or ""):
            return "нужна ссылка http(s)"
        if not await public_url(url):
            return "эту ссылку открыть нельзя"
        r = await self.web.get(url, follow_redirects=False)
        for _ in range(3):                      # редиректы — тоже только наружу
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = str(r.next_request.url) if r.next_request else r.headers["location"]
                if not await public_url(nxt):
                    return "эту ссылку открыть нельзя"
                r = await self.web.get(nxt, follow_redirects=False)
        text = r.text
        text = re.sub(r"(?is)<(script|style|noscript|svg|head).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = html.unescape(re.sub(r"\s+", " ", text)).strip()
        return text[:3000] or "страница пустая"


async def public_url(url):
    """Только внешние адреса: никаких localhost, домашней сети и служебных IP (защита от SSRF)."""
    import ipaddress
    import socket
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname or (u.port and u.port not in (80, 443)):
            return False
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return bool(infos)
    except Exception:
        return False


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


def wiki_recipes(wikitext):
    """{{Крафт |A1=Стекло |B2=Звезда Нижнего мира … |Выход=Маяк}} → «Маяк: ряд 1: Стекло, Стекло, Стекло; …»."""
    out = []
    for body in re.findall(r"\{\{Крафт\s*\|(.*?)\}\}", wikitext or "", re.S)[:4]:
        args = {}
        for part in body.split("|"):
            k, _, v = part.partition("=")
            args[k.strip()] = re.sub(r"\s+", " ", v).strip()
        grid = [[args.get(f"{c}{r}") or "пусто" for c in "ABC"] for r in "123"]
        grid = [row for row in grid if any(x != "пусто" for x in row)]
        if not grid:
            continue
        shape = " (в любом порядке)" if args.get("бесформенный") else ""
        rows = "; ".join(f"ряд {i + 1}: " + ", ".join(row) for i, row in enumerate(grid))
        out.append(f"{args.get('Выход', '?')}{shape} — {rows}")
    return out


# свежее (новости, цены, «сейчас») не запоминаем — устаревает
# вопрос к самому боту или про чьи-то планы — ответ личный, не «частый вопрос»
PERSONAL_RX = re.compile(r"(?<![а-яё])(?:ты|тебя|тебе|тобой|твой|твоя|твоё|твое|твои|мы|нас|вы|вас)(?![а-яё])|"
                         r"[а-яё]{2,}(?:ешь|ёшь|ишь)(?:ся)?(?![а-яё])", re.I)
# «ты знаешь / можешь подсказать, как…» — просто вежливый зачин вопроса
POLITE_RX = re.compile(r"(?<![а-яё])(?:(?:ты|вы)\s+)?(?:не\s+)?(?:знаешь|знаете|можешь|можете|подскажешь|подскажете|"
                       r"скажешь|скажете|объяснишь|объясните)(?![а-яё])", re.I)
FRESH_RX = re.compile(r"новост|цен[аыуе]?\b|курс|погод|сейчас|сегодня|вчера|завтра|счёт|счет|матч|выиграл|выйдет|скоро", re.I)
# «не знаю», отказы и сбои модели — не запоминаем (иначе мусор будет повторяться месяц)
UNSURE_RX = re.compile(r"(?<![\w])хз(?![\w])|не знаю|не нашёл|не нашел|не нашлось|без понятия|не уверен|не помню|"
                       r"не смог(?!л)|не могу(?!т)|попробуй ещё|попробуй еще|повтори|ошибк|такого[^.!?\n]{0,25}нет|нет такого|не существует|"
                       r"^\s*\[", re.I)
STOP = {"как", "что", "это", "где", "для", "или", "мне", "тебе", "тебя", "его", "так", "там", "тут", "вот", "уже", "еще",
        "надо", "нужно", "можно", "есть", "был", "была", "было", "быть", "про", "при", "без", "над", "под", "чем",
        "чтобы", "если", "когда", "какой", "какая", "какие", "какое", "знаешь", "скажи", "подскажи", "расскажи",
        "плз", "пожалуйста", "ребят", "народ", "кто", "нибудь", "вообще", "короче", "слушай", "блин", "ваще"}


HOWTO_WORDS = re.compile(r"^(?:с?крафт\w*|рецепт\w*|с?дела\w*|построи\w*|постро\w*|получи\w*|добы\w*|найти|найд\w*|"
                         r"работа\w*|нужн\w*|можн\w*|майн\w*|minecraft|вики|wiki|его|её|ее|их|него|нее|неё|давай|"
                         r"скажи|объясни|покажи|подскажи|расскажи|лучш\w*|быстр\w*|прост\w*|версии|версия|"
                         r"смотрел\w*|играл\w*|читал\w*|слышал\w*|видел\w*|знаешь|знает\w*|такое|такой|такая|"
                         r"попа\w*|зайти|залезть|дроп\w*|выпада\w*|падает)$", re.I)


# сленг игроков → как называется в русской вики
WIKI_SLANG = [(re.compile(r"^(?:эндермен\w*|эндерман\w*)$"), ["странник", "края"]),     # раньше «эндер…»
              (re.compile(r"^(?:эндер\w*|энд|энда|энде|енд\w*|end)$"), ["края"]),
              (re.compile(r"^(?:незер\w*|nether|ад|ада)$"), ["нижний", "мир"]),
              (re.compile(r"^(?:визер\w*|wither)$"), ["иссушитель"]),
              (re.compile(r"^(?:крип\w*)$"), ["крипер"])]
CHATTER = {"верно", "молодец", "спасибо", "спс", "круто", "понял", "ладно", "кстати", "окей", "ага", "снова", "опять",
           "крутишь", "давай", "теперь", "потом", "сначала", "вообще", "реально", "просто", "мир", "измерение"}


def wiki_words(text):
    """Слова-сущности вопроса (без «как», «скрафтить», «майн», версий, болтовни), сленг → названия вики:
    «как скрафтить маяк в 1.21» → [маяк]; «как попасть в эндер мир» → [портал, края]."""
    raw = re.findall(r"[а-яёa-z]+", re.sub(CALL_RX.pattern, " ", (text or "").lower(), flags=re.I))
    words = []
    for w in raw:
        if len(w) < 2 or w in STOP or w in CHATTER or HOWTO_WORDS.match(w):
            continue
        sub = next((rep for rx, rep in WIKI_SLANG if rx.match(w)), None)
        if sub or len(w) >= 3:
            words += sub or [w]
    if re.search(r"попа(?:сть|сти|ду)|зайти|залезть|телепорт", (text or "").lower()) and \
            any(w in ("края", "край", "нижний") for w in words):
        words.insert(0, "портал")              # «как попасть в энд» — это про портал
    return list(dict.fromkeys(words))


def same_word(a, b):
    """Одно слово с разными окончаниями: лису/лиса, кирпичи/кирпич, ферму/ферма."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 3 and n >= min(len(a), len(b)) - 2


def qkey(text):
    """Вопрос → набор основ слов и чисел-версий: «как сделать ферму железа» ≈ «как сделать фермы железо»."""
    text = re.sub(CALL_RX.pattern, " ", (text or "").lower().replace("ё", "е"), flags=re.I)
    words = re.findall(r"[a-zа-я]+|\d+(?:\.\d+)*", text)
    return {w if w[0].isdigit() else w[:4] if len(w) <= 6 else w[:5]
            for w in words if w[0].isdigit() or (len(w) >= 3 and w not in STOP)}


def temp(ctx=None):
    if not cfg.PROMPT_V2:
        return 0.75
    return cfg.FACT_TEMPERATURE if ctx and ctx.get("_facts") else cfg.CHAT_TEMPERATURE


def clean_text(text):
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
