"""poskinson — грубый Discord-бот для развлечения (бесплатные нейросети нескольких провайдеров).

Отвечает, когда зовут (упоминание, ответ ему, имя, личка); сам влезает, когда нейросеть-судья
видит повод; слушает голосовые и видит картинки; рисует; кидает гифки, эмодзи и стикеры сервера;
пишет с паузами «на печать» и иногда несколькими сообщениями; ищет в интернете; ставит напоминания.
Память — слоями (memory.py): карточки людей и лор сервера, свежие факты, сводки каналов, хронология.
Slash-команды: /лохдня /дуэль /рулетка /кости /шар /викторина /очки /прожарка /анекдот /совет /нарисуй
/заткнись /говори /напоминания /отменить /чтознаешь /забудь /статистика /помощь /лохдня_тут.
Файлы: config.py (характер, модели, настройки), llm.py (маршрутизатор моделей), media.py, memory.py,
store.py (SQLite), brain.py (инструменты). Ключи — .env. Логи — logs/, копии памяти — backups/.
"""
import asyncio
import io
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from brain import Brain, RateLimited, fix_script
from config import (CALL_RX, DB_PATH, HISTORY, INTERJECT_COOLDOWN, INTERJECT_NOTE, JUDGE_CHANCE, JUDGE_COOLDOWN,
                    JUDGE_PROMPT, JUDGE_THRESHOLD, LOG_DIR, LOH_HOUR, MAX_PARTS, NAME, PERSONA, QUIZ_SECONDS,
                    REACT_CHANCE, REACTIONS, ROULETTE_TIMEOUT, SPLIT_CHANCE, SCAN_CHUNK_CHARS, SCAN_LIMIT, SCAN_PAUSE, SCAN_PROMPT,
                    SUMMARY_EVERY, SUMMARY_IDLE, TOKEN, TYPING_CPS, TYPING_MAX, TZ, VERSION)
from llm import Router
from media import Media
from memory import Memory
from store import Store

log = logging.getLogger("poskinson")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
store = Store(DB_PATH)
router = Router(store.db)
media = Media(router)
memory = Memory(store, router)
brain = Brain(store, router, media)

last_interject = {}     # channel_id → время последнего вмешательства
muted_until = {}        # channel_id → до какого времени молчит сам
last_channel = {}       # guild_id → последний живой канал (для «лоха дня»)
transcripts = {}        # message_id → текст голосового
images = {}             # message_id → описание картинок
last_judge = {}         # channel_id → когда последний раз спрашивали судью
pending = {}            # channel_id → сколько сообщений с последней сводки
last_activity = {}      # channel_id → время последнего сообщения
consolidating = set()   # карточки, которые сейчас пересобираются
quizzes = {}            # channel_id → активная викторина
FACT_RX = re.compile(r"^\s*(?:-{3,}\s*)?ЗАПОМНИ\s*:\s*(.+?)\s*\|\s*(.+?)\s*$", re.M | re.I)
TIRED = "мана кончилась, дай реген пару минут"
MENTIONS = discord.AllowedMentions(users=True, everyone=False, roles=False)


# ======================= помощники =======================
def now_line():
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    n = datetime.now(TZ)
    return f"Сейчас {n:%d.%m.%Y %H:%M}, {days[n.weekday()]} (Москва)."


def muted(channel_id):
    return muted_until.get(channel_id, 0) > time.time()


def emoji_block(guild):
    if not guild:
        return ""
    em = [f":{e.name}:" for e in guild.emojis if e.available][:40]
    st = [s.name for s in guild.stickers][:20]
    out = ""
    if em:
        out += "\n- Можешь изредка вставлять эмодзи сервера (только эти, другие не выдумывай): " + " ".join(em)
    if st:
        out += ("\n- Совсем изредка можешь отправить стикер сервера строкой [стикер: имя] в конце ответа "
                "(только эти): " + ", ".join(st))
    return out


def save_facts(text, people, guild_id):
    """Вырезает строки «ЗАПОМНИ: …», кладёт факты в память, при надобности пересобирает карточки."""
    by_name = {n.lower(): uid for uid, n in people.items()}
    for name, fact in FACT_RX.findall(text):
        key = name.strip().lstrip("@").lower()
        if key == "сервер" and guild_id:
            new, due = memory.add_fact(0, "сервер", fact, guild_id)
            if new:
                log.info("запомнил о сервере: %s", fact)
            if due:
                schedule_consolidation(0, guild_id, "сервер")
        elif key in by_name:
            new, due = memory.add_fact(by_name[key], name.strip(), fact, guild_id or 0)
            if new:
                log.info("запомнил: %s | %s", name, fact)
            if due:
                schedule_consolidation(by_name[key], 0, name.strip())
    return FACT_RX.sub("", text).strip()


def schedule_consolidation(uid, gid, name):
    k = (uid, gid)
    if k in consolidating:
        return
    consolidating.add(k)

    async def run():
        try:
            await memory.consolidate(uid, gid, name)
        finally:
            consolidating.discard(k)
    asyncio.create_task(run())


def image_atts(m):
    return [a for a in m.attachments if (a.content_type or "").startswith("image/")][:2]


def msg_text(m):
    text = m.clean_content
    if m.id in transcripts:
        text = (text + " " if text else "") + f"[голосовое: {transcripts[m.id]}]"
    imgs = image_atts(m)
    if imgs:
        text += f" [картинка: {images[m.id]}]" if m.id in images else " [картинка]"
    other = [a.filename for a in m.attachments if a not in imgs and m.id not in transcripts]
    if other:
        text += " [вложение: " + ", ".join(other) + "]"
    if m.stickers:
        text += " [стикер: " + ", ".join(s.name for s in m.stickers) + "]"
    return text.strip()


async def see_images(m):
    """Описать картинки сообщения (один раз, с кешем)."""
    if m.id in images or not image_atts(m):
        return
    notes = []
    for a in image_atts(m):
        url = a.proxy_url + ("&" if "?" in a.proxy_url else "?") + "width=1024&height=1024"
        d = await media.describe(url, m.clean_content[:200])
        if d:
            notes.append(d)
    if notes:
        images[m.id] = " / ".join(notes)
        log.info("картинка от %s: %s", m.author.display_name, images[m.id][:100])
        if len(images) > 300:
            for k in list(images)[:60]:
                images.pop(k, None)


def is_voice(m):
    if getattr(m.flags, "voice", False):
        return True
    return any(getattr(a, "is_voice_message", lambda: False)() for a in m.attachments)


async def send_long(channel, text, reference=None):
    text = text.strip() or "…"
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    for i, c in enumerate(chunks):
        await channel.send(c, reference=reference if i == 0 else None, mention_author=False, allowed_mentions=MENTIONS)


STICKER_RX = re.compile(r"\[стикер:\s*([^\]]+)\]", re.I)
EMOJI_RX = re.compile(r"(?<![<\w]):([\wА-Яа-яЁё]{2,32}):(?!\d)")
PART_RX = re.compile(r"\n?\s*^-{3,}\s*$\s*\n?", re.M)


async def send_reply(channel, text, reference=None, started=None, files=(), gif=None):
    """Как человек: пауза «на печать», ответ может быть несколькими сообщениями,
    эмодзи :имя: → эмодзи сервера, [стикер: имя] → стикер, картинки и гифка — следом."""
    guild = getattr(channel, "guild", None)
    sticker = None
    m = STICKER_RX.search(text)
    if m:
        text = STICKER_RX.sub("", text).strip()
        if guild:
            name = m.group(1).strip().lower()
            sticker = next((s for s in guild.stickers if s.name.lower() == name), None)
    if guild:
        emap = {e.name: str(e) for e in guild.emojis if e.available}
    else:
        emap = {}
    # эмодзи сервера → настоящие, выдуманные (:смех:) → вон
    text = EMOJI_RX.sub(lambda x: emap.get(x.group(1), ""), text).strip()
    parts = [p.strip() for p in PART_RX.split(text) if p.strip()]
    if len(parts) > 1 and random.random() > SPLIT_CHANCE:
        parts = ["\n".join(parts)]               # модели злоупотребляют «---»: чаще — одним сообщением
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS - 1] + ["\n".join(parts[MAX_PARTS - 1:])]
    if not parts and not files:
        parts = ["…"]
    started = started or time.monotonic()
    for i, part in enumerate(parts):
        delay = min(TYPING_MAX, 0.6 + len(part) / TYPING_CPS)
        if i == 0:
            delay -= time.monotonic() - started      # пока думал — уже «печатал»
        if delay > 0.3:
            async with channel.typing():
                await asyncio.sleep(delay)
        last = i == len(parts) - 1
        chunks = [part[k:k + 1900] for k in range(0, len(part), 1900)]
        for k, c in enumerate(chunks):
            kw = {}
            if files and last and k == len(chunks) - 1:
                kw["files"] = [discord.File(io.BytesIO(d), filename=n) for d, n in files]
            await channel.send(c, reference=reference if i == 0 and k == 0 else None, mention_author=False,
                               allowed_mentions=MENTIONS, **kw)
    if not parts and files:
        await channel.send(files=[discord.File(io.BytesIO(d), filename=n) for d, n in files], reference=reference,
                           mention_author=False)
    if gif:
        await asyncio.sleep(0.8)
        await channel.send(gif)
    if sticker:
        try:
            await channel.send(stickers=[sticker])
        except discord.HTTPException:
            pass


async def say(guild_id, people, instruction, max_tokens=500):
    """Короткая реплика в характере по заданию (для команд и событий)."""
    system = PERSONA + "\n" + now_line() + "\n" + memory.prompt_block(guild_id, None, people)
    system += "\n\nСейчас ответ уходит одним сообщением: не используй разделитель ---, стикеры и гифки."
    m = await brain.complete([{"role": "system", "content": system}, {"role": "user", "content": instruction}],
                             max_tokens=max_tokens)
    return fix_script(save_facts(m.get("content") or "", people, guild_id))


def norm(s):
    s = s.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


# ======================= ответы в чате =======================
def is_called(m):
    if m.guild is None:
        return True
    if client.user in m.mentions:
        return True
    ref = m.reference.resolved if m.reference else None
    if isinstance(ref, discord.Message) and ref.author == client.user:
        return True
    return bool(CALL_RX.search(m.content) or CALL_RX.search(transcripts.get(m.id, "")))


async def build_prompt(m, interject):
    history = [x async for x in m.channel.history(limit=HISTORY, before=m)]
    history.reverse()
    history.append(m)
    people = {}
    for x in history:
        if not x.author.bot:
            people[x.author.id] = x.author.display_name
    gid = m.guild.id if m.guild else 0
    where = f"Канал #{m.channel.name}." if m.guild else "Личные сообщения."
    system = (PERSONA + emoji_block(m.guild) + "\n" + now_line() + " " + where + "\n"
              + memory.prompt_block(gid, m.channel.id if m.guild else None, people))
    if interject:
        system += "\n\n" + INTERJECT_NOTE
    msgs = [{"role": "system", "content": system}]
    for x in history:
        cap = 1500 if x is m else 500
        if x.author == client.user:
            msgs.append({"role": "assistant", "content": x.clean_content[:cap]})
        else:
            msgs.append({"role": "user", "content": f"{x.author.display_name}: {msg_text(x)[:cap]}"})
    return msgs, people


async def respond(m, called, interject):
    started = time.monotonic()
    ctx = {"channel_id": m.channel.id, "user_id": m.author.id}
    try:
        async with m.channel.typing():
            await see_images(m)
            ref = m.reference.resolved if m.reference else None
            if isinstance(ref, discord.Message):
                await see_images(ref)
            msgs, people = await build_prompt(m, interject)
            answer = await brain.chat(msgs, ctx)
    except RateLimited:
        log.warning("лимит Groq")
        if called:
            await m.reply(TIRED, mention_author=False)
        return
    except Exception:
        log.exception("ошибка ответа")
        if called:
            await m.reply("чёт я завис, повтори", mention_author=False)
        return
    answer = save_facts(answer, people, m.guild.id if m.guild else 0)
    if "[молчу]" in answer or (not answer and not ctx.get("files")):
        return
    log.info("ответ %s (%s) в #%s", "по зову" if called else "сам", ctx.get("model"), getattr(m.channel, "name", "лс"))
    await send_reply(m.channel, answer, reference=m if called else None, started=started,
                     files=ctx.get("files", ()), gif=ctx.get("gif"))


async def handle_voice(m):
    att = m.attachments[0] if m.attachments else None
    if not att or att.size > 24 * 1024 * 1024:
        return False
    try:
        data = await att.read()
        text = await media.transcribe(data, att.filename or "voice.ogg")
    except Exception:
        log.exception("голосовое не распозналось")
        return False
    if not text:
        return False
    transcripts[m.id] = text
    if len(transcripts) > 500:
        for k in list(transcripts)[:100]:
            transcripts.pop(k, None)
    log.info("голосовое от %s: %s", m.author.display_name, text[:100])
    return True


async def check_quiz(m):
    q = quizzes.get(m.channel.id)
    if not q:
        return False
    text = norm(m.content)
    if not text:
        return False
    for ans in q["answers"]:
        a = norm(ans)
        if a and re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text):
            quizzes.pop(m.channel.id, None)
            q["timer"].cancel()
            store.add_score(m.guild.id, m.author.id, "quiz")
            pts = store.score(m.guild.id, m.author.id, "quiz")
            jab = random.choice(["даже слепая курица иногда находит зерно", "ну надо же, мозг всё-таки есть",
                                 "повезло, не зазнавайся", "читерил небось, гугл открыт", "окей, сегодня ты не самый тупой"])
            await m.reply(f"✅ правильно, ответ — **{q['answers'][0]}**. {jab}. очков: {pts}", mention_author=False)
            return True
    return False


@client.event
async def on_message(m):
    if m.author.bot:
        return
    if m.guild:
        store.seen(m.guild.id, m.author.id, m.author.display_name)
        last_channel[m.guild.id] = m.channel.id
    t = m.content.strip().lower()
    if t in ("!чтознаешь", "!что знаешь"):
        await m.reply(what_i_know(m.author.id), mention_author=False)
        return
    if t == "!забудь":
        await m.reply(forget_text(m.author.id), mention_author=False)
        return
    if m.guild and await check_quiz(m):
        return

    if m.guild:
        pending[m.channel.id] = pending.get(m.channel.id, 0) + 1
        last_activity[m.channel.id] = time.time()
    voice = is_voice(m) and await handle_voice(m)
    called = is_called(m)
    interject = False
    if not called:
        if muted(m.channel.id):
            return
        now = time.time()
        ch = m.channel.id
        if (now - last_interject.get(ch, 0) > INTERJECT_COOLDOWN and now - last_judge.get(ch, 0) > JUDGE_COOLDOWN
                and (voice or image_atts(m) or (len(m.content) > 8 and random.random() < JUDGE_CHANCE))):
            last_judge[ch] = now
            if await worth_it(m):
                interject = True
                last_interject[ch] = now
        if not interject:
            if random.random() < REACT_CHANCE:
                try:
                    await m.add_reaction(random.choice(REACTIONS))
                except discord.HTTPException:
                    pass
            return
    await respond(m, called, interject)


async def worth_it(m):
    """Нейросеть-судья: есть ли повод влезть. Дешёвая модель, ответ — оценка 0–10."""
    try:
        hist = [x async for x in m.channel.history(limit=8, before=m)]
        hist.reverse()
        hist.append(m)
        lines = "\n".join(f"{'(бот) ' if x.author == client.user else ''}{x.author.display_name}: {msg_text(x)[:300]}" for x in hist)
        r = await router.complete([{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": lines}],
                                  role="light", max_tokens=120, temperature=0, json_mode=True)
        data = json.loads(r.get("content") or "{}")
        score = float(data.get("score", 0))
    except Exception as e:
        log.debug("судья молчит: %r", e)
        return False
    log.info("судья: %.0f/10 в #%s — %s", score, m.channel.name, str(data.get("why", ""))[:80])
    return score >= JUDGE_THRESHOLD


def what_i_know(uid):
    t = memory.about(uid)
    return ("вот что я про тебя знаю:\n" + t) if t else "про тебя я ничего не помню, ты пустое место"


def forget_text(uid):
    return f"стёр {memory.forget(uid)} фактов о тебе. ты снова никто"


# ======================= slash-команды =======================
async def guarded(inter, coro):
    """Отложенный ответ: нейросеть думает дольше трёх секунд."""
    await inter.response.defer(thinking=True)
    try:
        text = await coro
    except RateLimited:
        text = TIRED
    except Exception:
        log.exception("команда")
        text = "чёт сломалось, попробуй позже"
    await inter.followup.send((text or "…")[:1990], allowed_mentions=MENTIONS)


def gid(inter):
    return inter.guild.id if inter.guild else 0


@tree.command(name="помощь", description="Что умеет бот")
async def c_help(inter: discord.Interaction):
    await inter.response.send_message(
        f"**{NAME}** — зови по имени, упоминанием или ответом на моё сообщение, в личке отвечаю всегда. "
        "Сам влезаю, когда есть повод, слушаю голосовые, вижу картинки, рисую, гуглю и ставлю напоминания "
        "(«поскинсон, напомни через 2 часа…»).\n"
        "**Игры:** /лохдня, /дуэль, /рулетка, /кости, /шар, /викторина, /очки\n"
        "**Болтовня:** /прожарка, /анекдот, /совет, /нарисуй\n"
        "**Служебное:** /заткнись, /говори, /напоминания, /отменить, /чтознаешь, /забудь, /лохдня_тут, /статистика\n"
        f"версия {VERSION}",
        ephemeral=True)


@tree.command(name="анекдот", description="Рассказать анекдот")
@app_commands.describe(тема="О чём (необязательно)")
async def c_joke(inter: discord.Interaction, тема: str = ""):
    await guarded(inter, say(gid(inter), {inter.user.id: inter.user.display_name},
                             f"{inter.user.display_name} просит анекдот" + (f" про: {тема}" if тема else " на любую тему")
                             + ". Расскажи один смешной анекдот, без вступлений."))


@tree.command(name="совет", description="Совет от бота (полезный, но с наездом)")
@app_commands.describe(тема="О чём совет")
async def c_advice(inter: discord.Interaction, тема: str = ""):
    await guarded(inter, say(gid(inter), {inter.user.id: inter.user.display_name},
                             f"{inter.user.display_name} просит жизненный совет" + (f" про: {тема}" if тема else "")
                             + ". Дай один реально полезный совет в своём стиле."))


@tree.command(name="прожарка", description="Прожарить человека")
@app_commands.describe(кого="Кого жарим")
async def c_roast(inter: discord.Interaction, кого: discord.Member):
    if кого.id == client.user.id:
        await inter.response.send_message("себя я не жарю, я и так идеален. а вот ты — нет")
        return
    people = {кого.id: кого.display_name, inter.user.id: inter.user.display_name}
    await guarded(inter, say(gid(inter), people,
                             f"{inter.user.display_name} просит прожарить {кого.display_name}. Жёстко прожарь "
                             f"{кого.display_name} в 2–4 предложениях, используя то, что о нём знаешь. "
                             f"Обращайся к нему как <@{кого.id}>."))


@tree.command(name="нарисуй", description="Нарисовать картинку")
@app_commands.describe(что="Что нарисовать (можно по-русски)")
async def c_draw(inter: discord.Interaction, что: str):
    await inter.response.defer(thinking=True)
    try:
        m = await router.complete([{"role": "user", "content": "Переведи на английский и подробно опиши для генератора "
                                    f"картинок (объект, стиль, детали, 1–2 предложения), только описание: {что}"}],
                                  role="light", max_tokens=200, temperature=0.4)
        prompt = (m.get("content") or что).strip().strip('"')
        data, name, src = await media.generate(prompt)
        comment = await say(gid(inter), {inter.user.id: inter.user.display_name},
                            f"{inter.user.display_name} попросил нарисовать: «{что}». Картинка готова. "
                            "Одной короткой едкой фразой прокомментируй его запрос.", max_tokens=120)
        await inter.followup.send(comment[:1900] or "на, любуйся", file=discord.File(io.BytesIO(data), filename=name))
    except Exception:
        log.exception("нарисуй")
        await inter.followup.send("кисточка сломалась, попробуй позже")


@tree.command(name="статистика", description="Расход нейросетей за сегодня (видишь только ты)")
async def c_stats(inter: discord.Interaction):
    rows = router.report()
    lines = [f"`{p}:{m}` — {req} запр., ошибок {err}, токенов {ti}+{to}" for p, m, req, err, ti, to in rows]
    roles = {r: len(router.candidates(r)) for r in ("chat", "light", "vision")}
    await inter.response.send_message(
        f"**poskinson {VERSION}**, провайдеры: {', '.join(router.clients) or 'нет'}\n"
        f"доступно моделей сейчас — болтовня: {roles['chat']}, служебных: {roles['light']}, зрение: {roles['vision']}\n"
        + ("\n".join(lines) or "сегодня ещё ничего не тратил"), ephemeral=True)


@tree.command(name="кости", description="Бросить кости, например 2d6 или 1d20+3")
@app_commands.describe(бросок="Формат NdM+K, по умолчанию 1d6")
async def c_dice(inter: discord.Interaction, бросок: str = "1d6"):
    m = re.fullmatch(r"\s*(\d{0,2})\s*[dдк]\s*(\d{1,4})\s*([+-]\s*\d{1,4})?\s*", бросок.lower())
    if not m:
        await inter.response.send_message("пиши нормально, типа `2d6` или `1d20+3`, гений", ephemeral=True)
        return
    n, sides = int(m.group(1) or 1), int(m.group(2))
    mod = int(m.group(3).replace(" ", "")) if m.group(3) else 0
    if not (1 <= n <= 50 and 2 <= sides <= 1000):
        await inter.response.send_message("до 50 кубиков и от 2 до 1000 граней, не наглей", ephemeral=True)
        return
    rolls = [random.randint(1, sides) for _ in range(n)]
    total = sum(rolls) + mod
    jab = ""
    if n == 1 and sides == 20:
        jab = " — крит, офигеть 🔥" if rolls[0] == 20 else " — крит провал, как и вся твоя жизнь 💀" if rolls[0] == 1 else ""
    detail = f" ({' + '.join(map(str, rolls))}{f' {mod:+d}' if mod else ''})" if n > 1 or mod else ""
    await inter.response.send_message(f"🎲 {inter.user.mention} выкинул **{total}**{detail}{jab}")


BALL = ["бесспорно", "предрешено", "никаких сомнений", "определённо да", "можешь быть уверен в этом",
        "мне кажется — да", "вероятнее всего", "хорошие перспективы", "знаки говорят — да", "да",
        "пока не ясно, попробуй снова", "спроси позже", "лучше не рассказывать", "сейчас нельзя предсказать",
        "сконцентрируйся и спроси опять", "даже не думай", "мой ответ — нет", "по моим данным — нет",
        "перспективы не очень хорошие", "весьма сомнительно"]


@tree.command(name="шар", description="Магический шар ответит на вопрос")
@app_commands.describe(вопрос="Вопрос да/нет")
async def c_ball(inter: discord.Interaction, вопрос: str):
    await guarded(inter, _ball(inter, вопрос, random.choice(BALL)))


async def _ball(inter, q, base):
    comment = await say(gid(inter), {inter.user.id: inter.user.display_name},
                        f"{inter.user.display_name} спросил магический шар: «{q}». Шар ответил: «{base}». "
                        "Добавь одну короткую едкую фразу-комментарий (не меняя ответ шара).", max_tokens=150)
    return f"🎱 **{q}**\nшар: **{base}**\n{comment}"


@tree.command(name="рулетка", description="Русская рулетка: 1 из 6")
@app_commands.guild_only()
async def c_roulette(inter: discord.Interaction):
    g, u = inter.guild.id, inter.user.id
    if random.randint(1, 6) == 1:
        streak = store.score(g, u, "roulette_streak")
        store.set_score(g, u, "roulette_streak", 0)
        store.add_score(g, u, "roulette_dead")
        text = random.choice(["💥 БАХ. мозги на стене, хотя их там было немного",
                              "💥 БАХ. земля тебе стекловатой", "💥 БАХ. ну и кто теперь лох",
                              "💥 БАХ. респаун через минуту, нуб"])
        if streak:
            text += f" (продержался {streak} раз подряд)"
        if ROULETTE_TIMEOUT and isinstance(inter.user, discord.Member):
            try:
                await inter.user.timeout(timedelta(seconds=ROULETTE_TIMEOUT), reason="русская рулетка")
                text += f"\n🔇 мут на {ROULETTE_TIMEOUT} сек, полежи"
            except (discord.Forbidden, discord.HTTPException):
                pass
        await inter.response.send_message(f"{inter.user.mention} {text}")
    else:
        store.add_score(g, u, "roulette_streak")
        streak = store.score(g, u, "roulette_streak")
        if streak > store.score(g, u, "roulette_best"):
            store.set_score(g, u, "roulette_best", streak)
        await inter.response.send_message(
            f"🔫 *щёлк*… {inter.user.mention} живой, серия: **{streak}**. " +
            random.choice(["везёт дуракам", "в следующий раз точно", "барабан тебя пожалел", "крути ещё, ссыкло?"]))


@tree.command(name="дуэль", description="Вызвать человека на дуэль")
@app_commands.describe(соперник="С кем драться")
@app_commands.guild_only()
async def c_duel(inter: discord.Interaction, соперник: discord.Member):
    a, b = inter.user, соперник
    if a.id == b.id:
        await inter.response.send_message("сам с собой? иди лучше подрочи, тоже соло-активность")
        return
    winner = client.user if b.id == client.user.id else random.choice([a, b])
    loser = a if winner.id != a.id else b
    people = {a.id: a.display_name}
    if b.id != client.user.id:
        people[b.id] = b.display_name
    await guarded(inter, _duel(inter, a, b, winner, loser, people))


async def _duel(inter, a, b, winner, loser, people):
    g = inter.guild.id
    weapons = ["тапки", "мемы", "клавиатуры", "сковородки", "подушки", "аргументы из твиттера", "пиво", "кринж", "швабры"]
    story = await say(g, people,
                      f"Опиши дуэль {a.display_name} против {b.display_name} в 3–5 коротких строках, жёстко и смешно, "
                      f"оружие: {random.choice(weapons)}. Победит {winner.display_name}, проиграет {loser.display_name} — "
                      "унизительно. Используй то, что о них знаешь. Без заголовка, без итоговой строки.", max_tokens=400)
    if winner.id != client.user.id:
        store.add_score(g, winner.id, "duel_win")
    if loser.id != client.user.id:
        store.add_score(g, loser.id, "duel_loss")
    return f"⚔️ **{a.display_name}** vs **{b.display_name}**\n{story}\n🏆 победитель: {winner.mention}"


@tree.command(name="викторина", description="Вопрос викторины: кто первый ответит в чат")
@app_commands.describe(тема="Тема (необязательно)")
@app_commands.guild_only()
async def c_quiz(inter: discord.Interaction, тема: str = ""):
    if inter.channel.id in quizzes:
        await inter.response.send_message("викторина уже идёт, глаза разуй", ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    themes = "игры, кино, сериалы, музыка, наука, история, география, мемы, интернет, еда, спорт, техника"
    try:
        m = await brain.complete([
            {"role": "system", "content": "Ты ведущий викторины. Отвечай только JSON."},
            {"role": "user", "content": f"Придумай один вопрос викторины средней сложности на тему: {тема or 'любая из: ' + themes}. "
             "Ответ — 1–3 слова, однозначный, проверяемый. JSON: {\"question\": \"...\", \"answers\": [\"основной ответ\", "
             "\"другие допустимые написания\"]}. Вопрос по-русски."}],
            json_mode=True, temperature=1.0, max_tokens=300)
        data = json.loads(m.get("content") or "{}")
        question, answers = data["question"], [str(a) for a in data["answers"] if str(a).strip()]
        assert answers
    except RateLimited:
        await inter.followup.send(TIRED)
        return
    except Exception:
        log.exception("викторина")
        await inter.followup.send("вопрос не придумался, мозг перегрелся. ещё раз")
        return
    ch = inter.channel
    quizzes[ch.id] = {"answers": answers, "question": question, "timer": asyncio.create_task(_quiz_timeout(ch, question))}
    await inter.followup.send(f"❓ **викторина** ({QUIZ_SECONDS} сек, пишите ответ в чат)\n{question}")


async def _quiz_timeout(ch, question):
    await asyncio.sleep(QUIZ_SECONDS)
    q = quizzes.get(ch.id)
    if q and q["question"] == question:
        quizzes.pop(ch.id, None)
        await ch.send(f"⌛ время вышло. ответ — **{q['answers'][0]}**. "
                      + random.choice(["стадо баранов", "позорище", "ни одного мозга на сервер", "я в вас разочарован"]))


@tree.command(name="очки", description="Таблица лидеров")
@app_commands.guild_only()
async def c_scores(inter: discord.Interaction):
    g = inter.guild.id

    def block(title, kind, unit=""):
        rows = store.top(g, kind, 5)
        if not rows:
            return f"**{title}:** пусто"
        return f"**{title}:**\n" + "\n".join(f"{i + 1}. <@{r[0]}> — {r[1]}{unit}" for i, r in enumerate(rows))

    text = "\n\n".join([block("🧠 викторина", "quiz"), block("⚔️ победы в дуэлях", "duel_win"),
                        block("🔫 рекорд рулетки", "roulette_best", " подряд"), block("🏆 лох дня", "loh", " раз")])
    await inter.response.send_message(text, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="лохдня", description="Кто сегодня лох дня")
@app_commands.guild_only()
async def c_loh(inter: discord.Interaction):
    g = inter.guild.id
    if store.get(g, "loh_date") == datetime.now(TZ).date().isoformat():
        uid = int(store.get(g, "loh_user"))
        await inter.response.send_message(
            f"сегодняшний лох уже выбран — <@{uid}>. всего лохом дня был {store.score(g, uid, 'loh')} раз",
            allowed_mentions=discord.AllowedMentions.none())
        return
    await inter.response.defer(thinking=True)
    text = await do_loh(inter.guild)
    await inter.followup.send(text or "некого выбирать, вы все молчите", allowed_mentions=MENTIONS)


@tree.command(name="лохдня_тут", description="Объявлять лоха дня в этом канале")
@app_commands.guild_only()
async def c_loh_here(inter: discord.Interaction):
    store.put(inter.guild.id, "loh_channel", inter.channel.id)
    await inter.response.send_message(f"ок, лоха дня объявляю здесь, каждый день в {LOH_HOUR}:00")


@tree.command(name="заткнись", description="Бот перестанет сам влезать в этот канал")
@app_commands.describe(минуты="На сколько минут (по умолчанию 60)")
async def c_shut(inter: discord.Interaction, минуты: int = 60):
    минуты = max(1, min(минуты, 24 * 60))
    muted_until[inter.channel.id] = time.time() + минуты * 60
    await inter.response.send_message(f"ладно, молчу {минуты} мин. но если позовёшь — отвечу, я ж не гордый 🙄")


@tree.command(name="говори", description="Снова можно влезать в разговор")
async def c_talk(inter: discord.Interaction):
    muted_until.pop(inter.channel.id, None)
    await inter.response.send_message("я вернулся, лохи 😈")


@tree.command(name="напоминания", description="Мои напоминания")
async def c_rem(inter: discord.Interaction):
    rows = store.user_reminders(inter.user.id)
    if not rows:
        await inter.response.send_message("напоминаний нет. попроси: «поскинсон, напомни через час…»", ephemeral=True)
        return
    await inter.response.send_message(
        "\n".join(f"#{r['id']} — {datetime.fromtimestamp(r['due'], TZ):%d.%m %H:%M}: {r['text']}" for r in rows), ephemeral=True)


@tree.command(name="отменить", description="Отменить напоминание по номеру")
@app_commands.describe(номер="Номер из /напоминания")
async def c_cancel(inter: discord.Interaction, номер: int):
    ok = store.close_reminder(номер, inter.user.id)
    await inter.response.send_message("отменил" if ok else "нет у тебя такого напоминания", ephemeral=True)


@tree.command(name="чтознаешь", description="Что бот помнит о тебе")
async def c_know(inter: discord.Interaction):
    await inter.response.send_message(what_i_know(inter.user.id)[:1990], ephemeral=True)


@tree.command(name="забудь", description="Стереть, что бот о тебе помнит")
async def c_forget(inter: discord.Interaction):
    await inter.response.send_message(forget_text(inter.user.id), ephemeral=True)


# ======================= лох дня =======================
def loh_channel(guild):
    for cid in (store.get(guild.id, "loh_channel"), last_channel.get(guild.id)):
        ch = guild.get_channel(int(cid)) if cid else None
        if ch and ch.permissions_for(guild.me).send_messages:
            return ch
    ch = guild.system_channel
    if ch and ch.permissions_for(guild.me).send_messages:
        return ch
    return next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)


async def do_loh(guild):
    g = guild.id
    cands = [(u, n) for u, n in store.active_users(g) if u != client.user.id]
    prev = store.get(g, "loh_user")
    if len(cands) > 1 and prev:
        cands = [c for c in cands if str(c[0]) != prev] or cands
    if not cands:
        return None
    uid, name = random.choice(cands)
    store.put(g, "loh_date", datetime.now(TZ).date().isoformat())
    store.put(g, "loh_user", uid)
    store.add_score(g, uid, "loh")
    try:
        roast = await say(g, {uid: name}, f"Сегодня лохом дня выбран {name}. Объясни в 2–4 предложениях, почему именно он — "
                                          "жёстко и смешно, используя то, что о нём знаешь (если ничего — придумай повод "
                                          "из его молчания или ника).", max_tokens=300)
    except RateLimited:
        roast = "почему — сам знает"
    n = store.score(g, uid, "loh")
    return f"🏆 **лох дня** — <@{uid}>" + (f" (уже {n}-й раз)" if n > 1 else "") + f"\n{roast}"


@tasks.loop(minutes=1)
async def loh_loop():
    n = datetime.now(TZ)
    if n.hour < LOH_HOUR:
        return
    for guild in client.guilds:
        if store.get(guild.id, "loh_date") == n.date().isoformat():
            continue
        ch = loh_channel(guild)
        if not ch:
            continue
        try:
            text = await do_loh(guild)
            if text:
                await send_long(ch, text)
        except Exception:
            log.exception("лох дня")


# ======================= напоминания =======================
@tasks.loop(seconds=15)
async def reminder_loop():
    for r in store.due_reminders():
        store.close_reminder(r["id"])
        text = f"⏰ <@{r['user_id']}> напоминаю: **{r['text']}**. " + random.choice(
            ["давай, шевели булками", "не благодари", "опять бы забыл, склеротик", "я тебе не секретарь, но ладно"])
        try:
            ch = client.get_channel(r["channel_id"]) or await client.fetch_channel(r["channel_id"])
            await send_long(ch, text)
        except Exception:
            try:
                user = await client.fetch_user(r["user_id"])
                await user.send(text)
            except Exception:
                log.exception("напоминание #%s не доставлено", r["id"])


# ======================= чтение истории для памяти =======================
scan_lock = asyncio.Lock()


async def scan_guild(guild):
    async with scan_lock:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history) or store.is_scanned(ch.id):
                continue
            try:
                await scan_channel(guild, ch)
            except Exception:
                log.exception("чтение #%s", ch.name)


async def scan_channel(guild, ch):
    log.info("читаю историю #%s (%s)", ch.name, guild.name)
    msgs = [m async for m in ch.history(limit=SCAN_LIMIT) if not m.author.bot and m.content.strip()]
    msgs.reverse()
    for m in msgs:
        store.seen(guild.id, m.author.id, m.author.display_name, m.created_at.timestamp())
    chunks, cur, size = [], [], 0
    for m in msgs:
        line = f"{m.author.display_name}: {m.clean_content[:400]}"
        if size + len(line) > SCAN_CHUNK_CHARS and cur:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((m, line))
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    added = 0
    for i, chunk in enumerate(chunks):
        names = {}
        for m, _ in chunk:
            names[m.author.display_name.lower()] = m.author.id
            names.setdefault(m.author.name.lower(), m.author.id)
        text = "\n".join(line for _, line in chunk)
        r = None
        for _ in range(4):
            try:
                r = await router.complete([{"role": "system", "content": SCAN_PROMPT}, {"role": "user", "content": text}],
                                          role="memory", max_tokens=1200, temperature=0)
                break
            except RateLimited:
                await asyncio.sleep(90)
        if r is None:
            log.warning("#%s: кусок %d пропущен — лимит", ch.name, i)
            continue
        for line in (r.get("content") or "").splitlines():
            if "|" not in line:
                continue
            who, fact = (s.strip() for s in line.split("|", 1))
            who = who.strip("-•* @").lower()
            if who == "сервер":
                added += memory.add_fact(0, "сервер", fact, guild.id)[0]
            elif who in names:
                added += memory.add_fact(names[who], who, fact, guild.id)[0]
        log.info("#%s: кусок %d/%d, новых фактов: %d", ch.name, i + 1, len(chunks), added)
        if i + 1 < len(chunks):
            await asyncio.sleep(SCAN_PAUSE)
    # сводка канала по последним сообщениям — чтобы сразу знать, о чём тут говорят
    tail = msgs[-120:]
    if tail:
        await memory.summarize(ch.id, guild.id, ch.name, [f"{m.author.display_name}: {m.clean_content[:300]}" for m in tail],
                               tail[-1].id)
    store.mark_scanned(ch.id)
    log.info("#%s прочитан: %d сообщений, %d новых фактов", ch.name, len(msgs), added)
    if added:
        await consolidate_all()


async def consolidate_all():
    """Пересобрать карточки всех, у кого есть несжатые факты (по одной, с паузами — лимиты)."""
    for uid, gid, name in memory.needing_consolidation():
        k = (uid, gid) if not uid else (uid, 0)
        if k in consolidating:
            continue
        consolidating.add(k)
        try:
            await memory.consolidate(uid, gid, name)
        finally:
            consolidating.discard(k)
        await asyncio.sleep(5)


@tasks.loop(minutes=2)
async def summary_loop():
    """Сводки каналов: каждые SUMMARY_EVERY сообщений или после паузы, если накопилось ≥ 5."""
    now = time.time()
    for cid, n in list(pending.items()):
        idle = now - last_activity.get(cid, now)
        if not (n >= SUMMARY_EVERY or (n >= 5 and idle >= SUMMARY_IDLE)):
            continue
        ch = client.get_channel(cid)
        if not ch or not getattr(ch, "guild", None):
            pending.pop(cid, None)
            continue
        _, last_id, _ = memory.channel(cid)
        after = discord.Object(id=last_id) if last_id else None
        msgs = [m async for m in ch.history(limit=150, after=after, oldest_first=True) if m.content or m.attachments]
        if not msgs:
            pending[cid] = 0
            continue
        lines = [f"{'(бот) ' if m.author == client.user else ''}{m.author.display_name}: {msg_text(m)[:300]}" for m in msgs]
        if await memory.summarize(cid, ch.guild.id, ch.name, lines, msgs[-1].id):
            pending[cid] = 0


@tasks.loop(minutes=10)
async def maintenance_loop():
    """Ночью (4–5 утра по Москве): каталоги моделей, пересборка карточек, копия памяти."""
    n = datetime.now(TZ)
    if n.hour != 4 or store.get(0, "maintenance") == n.date().isoformat():
        return
    store.put(0, "maintenance", n.date().isoformat())
    log.info("ночное обслуживание")
    await router.discover()
    await consolidate_all()
    memory.backup()


@tasks.loop(hours=1)
async def scan_loop():
    for guild in client.guilds:
        await scan_guild(guild)


# ======================= запуск =======================
async def sync_commands(guild):
    tree.copy_global_to(guild=guild)
    try:
        cmds = await tree.sync(guild=guild)
        log.info("команды на %s: %d", guild.name, len(cmds))
    except discord.HTTPException:
        log.exception("не удалось зарегистрировать команды на %s", guild.name)


@client.event
async def on_ready():
    log.info("вошёл как %s, серверов: %d", client.user, len(client.guilds))
    for guild in client.guilds:
        await sync_commands(guild)
    if not getattr(client, "_booted", False):
        client._booted = True
        log.info("poskinson %s", VERSION)
        await router.discover()
        if not (memory.db.execute("SELECT 1 FROM profiles LIMIT 1").fetchone()):
            asyncio.create_task(consolidate_all())       # первая сборка карточек из старых фактов
        try:
            memory.backup()
        except Exception:
            log.exception("копия памяти")
    for loop in (loh_loop, reminder_loop, scan_loop, summary_loop, maintenance_loop):
        if not loop.is_running():
            loop.start()


@client.event
async def on_guild_join(guild):
    log.info("добавили на сервер %s", guild.name)
    await sync_commands(guild)
    asyncio.create_task(scan_guild(guild))


if __name__ == "__main__":
    from logging.handlers import RotatingFileHandler
    LOG_DIR.mkdir(exist_ok=True)
    fh = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), fh])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("primp").setLevel(logging.WARNING)
    client.run(TOKEN, log_handler=None)
