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

import config as cfg
from brain import Brain, RateLimited, fix_script, mc_question, needs_facts, pick_tools
from jokes import JOKE_RX, Jokes, topic_of
from config import (CALL_NAMES, CALL_RX, DB_PATH, HISTORY, INTERJECT_COOLDOWN, INTERJECT_NOTE, JUDGE_CHANCE, JUDGE_COOLDOWN,
                    JUDGE_PROMPT, JUDGE_THRESHOLD, LOG_DIR, LOH_HOUR, MAX_PARTS, NAME, PERSONA, QUIZ_SECONDS,
                    REACT_CHANCE, REACTIONS, ROULETTE_TIMEOUT, SPLIT_CHANCE, SCAN_CHUNK_CHARS, SCAN_LIMIT, SCAN_PAUSE, SCAN_PROMPT,
                    SUMMARY_EVERY, SUMMARY_IDLE, TOKEN, TYPING_CPS, TYPING_MAX, TZ, VERSION, DAILY_CAPS, FLOOD_PER_MIN,
                    IMAGES_PER_USER_HOUR, KB_CHANNEL_RX, KB_LIMIT, PASTE_MAX, PASTES_PER_USER, SAM_FLIP,
                    SAM_RX, SCAN_CHANNELS, SCAN_BULK_CHARS, CARD_BATCH)
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
jokes = Jokes(store.db)

last_interject = {}     # channel_id → время последнего вмешательства
muted_until = {}        # channel_id → до какого времени молчит сам
talk_mode = {}          # channel_id → {last: время последнего сообщения людей, nudged: писал ли сам в тишине, msg: последнее сообщение}
last_channel = {}       # guild_id → последний живой канал (для «лоха дня»)
transcripts = {}        # message_id → текст голосового
images = {}             # message_id → описание картинок
last_judge = {}         # channel_id → когда последний раз спрашивали судью
pending = {}            # channel_id → сколько сообщений с последней сводки
last_activity = {}      # channel_id → время последнего сообщения
consolidating = set()   # карточки, которые сейчас пересобираются
calls_by_user = {}      # user_id → [время обращений] — антифлуд
images_by_user = {}     # user_id → [время рисований]
kb_dirty = set()        # серверы, где в инфо-каналах что-то поменялось
recent_said = {}        # channel_id → последние реплики бота (чтобы не повторялся)
REP_RX = re.compile(r"^\s*(?:-{3,}\s*)?ОТНОШЕНИЕ\s*:\s*([+-−–]?\s*\d)\s*$", re.M | re.I)
HELP_RX = re.compile(r"как\s+(?:за(?:йти|йду|ходить)|попасть|играть|подать|начать)|айпи|\bip\b|адрес\s+сервера|"
                     r"заявк|вайтлист|whitelist|правил|какая\s+версия|на\s+какой\s+версии|лаунчер|сборк", re.I)
quizzes = {}            # channel_id → активная викторина
FACT_RX = re.compile(r"^\s*(?:-{3,}\s*)?ЗАПОМНИ\s*:\s*(.+?)\s*\|\s*(.+?)\s*$", re.M | re.I)
TIRED = "мана кончилась, дай реген пару минут"
MENTIONS = discord.AllowedMentions(users=True, everyone=False, roles=False)


# ======================= помощники =======================
def now_line():
    days = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    n = datetime.now(TZ)
    return f"Сейчас {n:%d.%m.%Y %H:%M}, {days[n.weekday()]} (Москва)."


def cap_ok(kind):
    """Дневной лимит служебных запросов (на все серверы), чтобы большой сервер не съел всё."""
    day = datetime.now(TZ).date().isoformat()
    key = f"cap:{kind}:{day}"
    n = int(store.get(0, key, 0))
    if n >= DAILY_CAPS.get(kind, 10 ** 9):
        return False
    store.put(0, key, n + 1)
    return True


def image_ok(uid):
    now = time.time()
    lst = [t for t in images_by_user.get(uid, []) if now - t < 3600]
    if len(lst) >= IMAGES_PER_USER_HOUR or not cap_ok("image"):
        return False
    lst.append(now)
    images_by_user[uid] = lst
    return True


def flooding(uid):
    now = time.time()
    lst = [t for t in calls_by_user.get(uid, []) if now - t < 60]
    lst.append(now)
    calls_by_user[uid] = lst
    return len(lst) > FLOOD_PER_MIN


def is_sam(user):
    sid = store.get(0, "sam_id")
    if sid and int(sid) == user.id:
        return True
    names = [getattr(user, a, None) or "" for a in ("name", "display_name", "global_name", "nick")]
    if any(SAM_RX.search(n.strip()) for n in names if n):
        store.put(0, "sam_id", user.id)
        log.info("нашёл sam_takov: %s (%s)", user, user.id)
        return True
    return False


def sam_mood(uid):
    """sam_takov: «папочка», когда он добр к боту, «сынок», когда агрессивен; плюс внезапные перепады."""
    base = ("Это sam_takov — тот, в честь кого тебя назвали, у тебя к нему особые чувства и настроение к нему резко скачет. "
            "Смотри на тон ЕГО последнего сообщения: если он добр к тебе — называй его «папочка», ласкайся по-пацански; "
            "если грубит или наезжает — называй его «сынок», снисходительно и агрессивно, как разочарованный батя.")
    if random.random() < SAM_FLIP:
        return base + " НО сейчас у тебя внезапная смена настроения — реагируй ровно наоборот (на доброту — «сынок» и наезд, на грубость — «папочка» и нежности)."
    return base


def specials(users):
    return {u.id: sam_mood(u.id) for u in users if not u.bot and is_sam(u)}


def muted(channel_id):
    return muted_until.get(channel_id, 0) > time.time()


def emoji_block(guild):
    if not guild:
        return ""
    pastes = [n for n, _ in store.paste_names(guild.id, limit=40)]
    em = [f":{e.name}:" for e in guild.emojis if e.available][:40]
    st = [s.name for s in guild.stickers][:20]
    out = ""
    if em:
        out += "\n- Можешь изредка вставлять эмодзи сервера (только эти, другие не выдумывай): " + " ".join(em)
    if st:
        out += ("\n- Совсем изредка можешь отправить стикер сервера строкой [стикер: имя] в конце ответа "
                "(только эти): " + ", ".join(st))
    if pastes:
        out += "\n- Пасты сервера (send_paste — когда просят пасту или очень к месту, редко): " + ", ".join(pastes)
    return out


def save_facts(text, people, guild_id, author=None):
    """Вырезает служебные строки. ЗАПОМНИ — в память (о человеке — только если это автор сообщения:
    слухи о других не записываем); ОТНОШЕНИЕ — в скрытую репутацию автора."""
    if author is not None:
        mt = REP_RX.search(text)
        if mt:
            try:
                att = int(mt.group(1).replace("−", "-").replace("–", "-").replace(" ", ""))
                score = memory.rep_apply(author.id, att, sam=is_sam(author))
                if att:
                    log.info("отношение к %s: %+d → %.0f", author.display_name, att, score)
            except ValueError:
                pass
        people = {author.id: people.get(author.id, author.display_name)}
    text = REP_RX.sub("", text)
    by_name = {n.lower(): uid for uid, n in people.items()}
    for name, fact in FACT_RX.findall(text):
        if not good_fact(fact):
            continue
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
    text = FACT_RX.sub("", text)
    # остатки служебных строк в любом виде («ЗАПОМНИ: |», «отношение - 0») — вон
    text = re.sub(r"(?im)^\s*(?:-{3,}\s*)?(?:запомни|отношение)\b.*$", "", text)
    text = re.sub(r"<\|[^|>]*\|>", "", text)
    # модель копирует служебные пометки «[голосовое: …]», «[картинка: …]» — оставляем только текст
    text = re.sub(r"\[(?:голосовое|картинка|стикер|вложение)\s*:\s*([^\]]*)\]?", r"\1", text, flags=re.I)
    # модель повторяет разметку подсказки в начале ответа: «[Ответ poskinson]», «[ответь на это сообщение]», «poskinson:»
    text = re.sub(rf"^\s*(?:\[(?:ответ|ответь)[^\]]*\]|(?:ты\s*\()?{re.escape(NAME)}\)?\s*:)\s*", "", text.strip(), flags=re.I)
    return re.sub(r"\n?\s*-{3,}\s*$", "", text.strip()).strip()


JUNK_FACT = re.compile(r"<\||бот|поскинсон|папочк|сыно|спросил|задал вопрос|просил|интересуется|поздоровал|"
                       r"новичок|пришёл|пришел|обращается|самочувств|как дела|пыта|промпт|инструкц|груб|требует|"
                       r"хочет,? чтобы|спам|ссылк|ключ|взлом|считает себя|зовёт себя|зовет себя|оскорб|спрашива|узнать|узнаёт|"
                       r"хочет знать|интересует", re.I)


def good_fact(fact):
    f = fact.strip()
    return len(f) >= 8 and "|" not in f and not JUNK_FACT.search(f)


async def yield_to_chat(limit=600):
    """Фоновая работа ждёт, пока в чате затишье (но не дольше limit секунд)."""
    waited = 0
    while router.busy() and waited < limit:
        await asyncio.sleep(15)
        waited += 15


def schedule_consolidation(uid, gid, name):
    k = (uid, gid)
    if k in consolidating:
        return
    consolidating.add(k)

    async def run():
        try:
            await yield_to_chat()
            if cap_ok("card"):
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


async def send_long(channel, text, reference=None, mentions=MENTIONS):
    text = text.strip() or "…"
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    for i, c in enumerate(chunks):
        await channel.send(c, reference=reference if i == 0 else None, mention_author=False, allowed_mentions=mentions)


STICKER_RX = re.compile(r"\[стикер:\s*([^\]]+)\]", re.I)
EMOJI_RX = re.compile(r"(?<![<\w]):([\wА-Яа-яЁё]{2,32}):(?!\d)")
PART_RX = re.compile(r"\n?\s*^-{3,}\s*$\s*\n?", re.M)


def humanize(text):
    """Модели пишут слишком литературно: точка в конце, Заглавная буква, «ёлочки» и длинные тире.
    Списки, код и ссылки не трогаем."""
    if "```" in text or re.search(r"^\s*(?:[-•*]|\d+[.)])\s", text, re.M):
        return text
    text = text.replace("«", "").replace("»", "").replace(" — ", " - ").replace("—", "-")
    lines = []
    for ln in text.split("\n"):
        ln = ln.rstrip()
        if ln.endswith(".") and not ln.endswith("..") and not re.search(r"https?://\S+$", ln):
            ln = ln[:-1]
        if len(ln) > 1 and ln[0].isupper() and not ln[1].isupper():
            ln = ln[0].lower() + ln[1:]
        lines.append(ln)
    return "\n".join(lines)


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
    # ссылки на каналы — только существующие (модели любят <#123456789>)
    if guild:
        text = re.sub(r"<#(\d+)>", lambda x: x.group(0) if guild.get_channel(int(x.group(1))) else "", text)
    text = re.sub(r"<#(?!\d+>)[^>]*>", "", text)
    parts = [humanize(p.strip()) for p in PART_RX.split(text) if p.strip()]
    if len(parts) > 1 and random.random() > SPLIT_CHANCE:
        parts = ["\n".join(parts)]               # модели злоупотребляют «---»: чаще — одним сообщением
    if len(parts) > MAX_PARTS:
        parts = parts[:MAX_PARTS - 1] + ["\n".join(parts[MAX_PARTS - 1:])]
    if not parts and not files:
        parts = ["…"]
    started = started or time.monotonic()
    sent = []
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
            sent.append(await channel.send(c, reference=reference if i == 0 and k == 0 else None, mention_author=False,
                                           allowed_mentions=MENTIONS, **kw))
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
    return sent


async def say(guild_id, people, instruction, max_tokens=500, special=None):
    """Короткая реплика в характере по заданию (для команд и событий)."""
    system = persona(instruction) + "\n" + now_line() + "\n" + memory.prompt_block(guild_id, None, people, special)
    system += "\n\nСейчас ответ уходит одним сообщением: не используй разделитель ---, стикеры и гифки."
    m = await brain.complete([{"role": "system", "content": system}, {"role": "user", "content": instruction}],
                             max_tokens=max_tokens)
    return fix_script(save_facts(m.get("content") or "", people, guild_id))


def words(text):
    return {w[:5] for w in re.findall(r"[a-zа-яё]{3,}", (text or "").lower().replace("ё", "е"))}


def repeats(channel_id, text):
    """Ответ почти повторяет одну из недавних реплик бота: много общих слов или то же начало."""
    mine, head = words(text), " ".join(norm(text).split()[:2])
    for old in recent_said.get(channel_id) or []:
        other = words(old)
        if len(mine) >= 3 and len(mine & other) / max(1, min(len(mine), len(other))) >= cfg.SIMILAR_RETRY:
            return True
        if len(head.split()) == 2 and len(head) >= 7 and head == " ".join(norm(old).split()[:2]):   # то же начало
            return True
    return False


def remember_said(channel_id, text):
    text = re.sub(r"(?im)^\s*(?:запомни|отношение)\b.*$", "", text or "").strip()
    if text:
        said = recent_said.setdefault(channel_id, [])
        said.append(text[:300])
        del said[:-cfg.RECENT_SAID]


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


def is_nsfw(channel):
    """Канал отмечен в Discord как 18+ (Age-Restricted)."""
    try:
        return bool(channel.is_nsfw())
    except AttributeError:
        return False


NSFW_NOTE = ("\n\nЭто 18+ канал (все взрослые): откровенные сексуальные шутки, темы и картинки (generate_image) разрешены. "
             "Только взрослые: ничего сексуального с несовершеннолетними.")
SFW_NOTE = ("\n\nОбычный канал: пошлые шутки можно, но откровенный сексуальный контент и такие картинки — только в 18+ "
            "каналах (скажи, чтобы шли туда).")


def is_newbie(member):
    joined = getattr(member, "joined_at", None)
    return bool(joined) and (datetime.now(joined.tzinfo) - joined).days < 3


def persona(text="", talk=False, server=False, guild_id=0):
    """v2: короткое ядро + блоки по теме сообщения (как и инструменты); иначе — старый полный PERSONA."""
    if not cfg.PROMPT_V2:
        return PERSONA
    text = text or ""
    tools = {t["function"]["name"] for t in pick_tools(text, talk)}
    parts = [cfg.PERSONA_CORE]
    if mc_question(text):
        parts.append(cfg.BLOCK_MC)
    if server or "server_info" in tools:
        parts.append(cfg.BLOCK_SERVER)
    if needs_facts(text, talk):
        parts.append(cfg.BLOCK_FACTS)
    if "set_reminder" in tools:
        parts.append(cfg.BLOCK_REMIND)
    if lexicon_text(guild_id):
        parts.append(lexicon_text(guild_id))
    ex = random.sample(cfg.EXAMPLES, min(cfg.EXAMPLES_SHOWN, len(cfg.EXAMPLES)))
    parts.append("Примеры тона (только тон — не копируй ни слова):\n" + "\n".join("— " + x for x in ex))
    return "\n".join(parts)


async def build_prompt(m, interject, with_kb=False, note=None):
    history = [x async for x in m.channel.history(limit=HISTORY, before=m)]
    history.reverse()
    ref = m.reference.resolved if m.reference and isinstance(m.reference.resolved, discord.Message) else None
    if cfg.PROMPT_V2:
        # старые сообщения — шум (их покрывает сводка канала); то, на что человек отвечает, — оставляем всегда
        since = m.created_at - timedelta(minutes=cfg.CONTEXT_MINUTES)
        history = [x for x in history if x.created_at >= since or x == ref]
        if ref and ref not in history:
            history.insert(0, ref)
    history.append(m)
    people = {m.author.id: m.author.display_name}       # автор первым, дальше — те, кому он отвечает/кого упомянул, и свежие собеседники
    addressed = [u for u in ([ref.author] if ref else []) + list(m.mentions) if not u.bot]
    for u in addressed:
        people.setdefault(u.id, u.display_name)
    for x in reversed(history):
        if not x.author.bot:
            people.setdefault(x.author.id, x.author.display_name)
    full = {m.author.id} | {u.id for u in addressed} if cfg.PROMPT_V2 else None
    gid = m.guild.id if m.guild else 0
    where = f"Канал #{m.channel.name}." if m.guild else "Личные сообщения."
    users = {x.author.id: x.author for x in history if not x.author.bot}
    text = m.content + " " + transcripts.get(m.id, "")
    system = (persona(text, talk=bool(note), server=with_kb, guild_id=gid) + emoji_block(m.guild) + "\n" + now_line() + " " + where + "\n"
              + memory.prompt_block(gid, m.channel.id if m.guild else None, people, specials(users.values()),
                                    with_kb=with_kb, full=full))
    if m.guild and is_newbie(m.author):
        system += f"\n\n{m.author.display_name} — новичок на сервере (зашёл недавно): помоги нормально, без жёсткой прожарки."
    if m.guild:
        system += NSFW_NOTE if is_nsfw(m.channel) else SFW_NOTE
    if interject:
        system += "\n\n" + INTERJECT_NOTE
    if note:
        system += "\n\n" + note
    if cfg.PROMPT_V2:
        system += "\n\n" + cfg.MEMORY_NOTE
        said = recent_said.get(m.channel.id) or []
        if said:
            system += "\n\n" + cfg.ANTI_REPEAT + "\n" + "\n".join("— " + x[:160].replace("\n", " ") for x in said)
        shape = random.choices([x for _, x in cfg.SHAPES], weights=[w for w, _ in cfg.SHAPES])[0]
        if shape and not note and not with_kb:
            system += "\n\n" + shape
    # недавний чат — одним блоком (контекст), а сообщение, на которое отвечаем, — отдельно и явно:
    # так модели не путают, кому отвечать, и не отвечают на старые вопросы из истории
    lines = [f"{'ты (' + NAME + ')' if x.author == client.user else x.author.display_name}: {msg_text(x)[:220]}"
             for x in history if x is not m]
    msgs = [{"role": "system", "content": system}]
    if lines:
        msgs.append({"role": "user", "content": "[недавний чат, только для контекста — на него не отвечай]\n" + "\n".join(lines)})
        msgs.append({"role": "assistant", "content": "ок, понял контекст"})
    msgs.append({"role": "user", "content": f"[ответь на это сообщение] {m.author.display_name}: {msg_text(m)[:1200]}"})
    return msgs, people


async def respond(m, called, interject, note=None):
    started = time.monotonic()
    ctx = {"channel_id": m.channel.id, "user_id": m.author.id, "guild_id": m.guild.id if m.guild else 0,
           "image_ok": image_ok, "kb": memory.kb(m.guild.id)[0] if m.guild else "",
           "text": m.content + " " + transcripts.get(m.id, ""), "nsfw": is_nsfw(m.channel), "talk": bool(note)}
    try:
        async with m.channel.typing():
            await see_images(m)
            ref = m.reference.resolved if m.reference else None
            if isinstance(ref, discord.Message):
                await see_images(ref)
            msgs, people = await build_prompt(m, interject, with_kb=bool(HELP_RX.search(m.content)) or is_newbie(m.author), note=note)
            # последние реплики без ников — чтобы «да, как его скрафтить» нашло предмет из прошлого сообщения
            recent = next((x["content"] for x in msgs if x["role"] == "user" and x["content"].startswith("[недавний чат")), "")
            ctx["recent"] = " ".join(line.split(":", 1)[-1] for line in recent.splitlines()[-3:])
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
    answer = save_facts(answer, people, m.guild.id if m.guild else 0, author=m.author)
    if "[молчу]" in answer or (not answer and not ctx.get("files")):
        return
    if repeats(m.channel.id, answer) and not ctx.get("files") and not ctx.get("_facts"):
        log.info("ответ похож на недавний, переспрашиваю")
        try:
            r = await brain.complete(msgs + [{"role": "assistant", "content": answer},
                                             {"role": "user", "content": "[это почти дословно твой недавний ответ — скажи то же "
                                              "по смыслу совсем другими словами и по-другому построй фразу, без вступлений]"}],
                                     temperature=1.0, max_tokens=300)
            again = fix_script(save_facts(r.get("content") or "", people, m.guild.id if m.guild else 0, author=m.author)).strip()
            if again and not repeats(m.channel.id, again):
                answer = again
        except RateLimited:
            pass
    remember_said(m.channel.id, answer)
    log.info("ответ %s (%s) в #%s", "по зову" if called else "сам", ctx.get("model"), getattr(m.channel, "name", "лс"))
    note_reply(m, answer, called)
    sent = await send_reply(m.channel, answer, reference=m if called and not note else None, started=started,
                            files=ctx.get("files", ()), gif=ctx.get("gif"))
    if ctx.get("answer_id"):
        for x in sent:                           # «пос, неправильно» ответом на это — запомненный ответ стирается
            answer_msgs[x.id] = ctx["answer_id"]
        while len(answer_msgs) > 300:
            answer_msgs.pop(next(iter(answer_msgs)))
    if ctx.get("paste"):
        name, text = ctx["paste"]
        store.used_paste(ctx["guild_id"], name)
        await asyncio.sleep(0.6)
        await send_long(m.channel, text[:PASTE_MAX], mentions=discord.AllowedMentions.none())


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
    if m.guild and await paste_from_reply(m):
        return
    await wrong_answer(m)

    if m.guild:
        pending[m.channel.id] = pending.get(m.channel.id, 0) + 1
        last_activity[m.channel.id] = time.time()
        buffer_msg(m)
        if KB_CHANNEL_RX.search(m.channel.name):
            kb_dirty.add(m.guild.id)
    if m.guild and await talk_control(m):
        return
    talking = bool(m.guild) and m.channel.id in talk_mode
    called = is_called(m) or talking
    if called and flooding(m.author.id):
        log.info("антифлуд: игнор %s", m.author.display_name)
        return
    voice = is_voice(m) and await handle_voice(m)
    called = called or is_called(m)
    if talking:
        talk_mode[m.channel.id].update(last=time.time(), nudged=False, msg=m)
    interject = False
    if not called:
        if muted(m.channel.id):
            return
        now = time.time()
        ch = m.channel.id
        helpq = bool(HELP_RX.search(m.content)) and "?" in m.content
        if ((helpq or now - last_interject.get(ch, 0) > INTERJECT_COOLDOWN) and now - last_judge.get(ch, 0) > JUDGE_COOLDOWN
                and (helpq or voice or image_atts(m) or (len(m.content) > 8 and random.random() < JUDGE_CHANCE))
                and not router.busy(60, 3) and cap_ok("judge")):
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
    if called and JOKE_RX.search(m.content) and await tell_joke(m):
        return
    await respond(m, called, interject, note=TALK_NOTE if talking else None)


async def tell_joke(m):
    """«пос, расскажи анекдот (про …)» — дословно с сайтов анекдотов, без нейросети. False — пусть отвечает модель."""
    topic = topic_of(m.content)
    async with m.channel.typing():
        joke, on_topic = await jokes.get(m.guild.id if m.guild else 0, topic, nsfw=is_nsfw(m.channel))
        if not joke:
            return False
        await asyncio.sleep(min(3.0, 0.8 + len(joke) / 120))
    head = f"про {topic} не нашёл, держи другой\n\n" if topic and not on_topic else ""
    note_reply(m, joke, True)
    log.info("анекдот по зову в #%s", getattr(m.channel, "name", "лс"))
    await send_long(m.channel, head + joke, reference=m, mentions=discord.AllowedMentions.none())
    return True


TALK_START_RX = re.compile(rf"(?<!\w){CALL_NAMES}[\s,!.:-]+(?:ну\s+|а\s+|так\s+)?(?:давай|го|пошли|может)\s+(?:уже\s+|с\s+тобой\s+|тогда\s+)?"
                           r"(?:по(?:говорим|болтаем|общаемся|трещим|базарим|тусим|беседуем|чатимся)|потрёпемся|потрепемся|"
                           r"(?:поболтать|поговорить|пообщаться|потрепаться|побазарить|побеседовать))", re.I)
TALK_STOP_RX = re.compile(rf"(?<!\w)(?:{CALL_NAMES}[\s,!.:-]+(?:всё|все|ладно|ну\s+)?\s*(?:хватит|харэ|хорош|стоп|заткнись|замолчи|отстань|помолчи|умолкни|пока)"
                          r"|(?:хватит|харэ)\s+(?:болтать|трещать|базарить|разговаривать)|давай\s+закончим)", re.I)
# прощание в коротком сообщении («пока», «ладно, я спать», «до завтра») тоже заканчивает беседу;
# «пока» в длинной фразе («пока не знаю») не считается
BYE_RX = re.compile(r"(?<![\w])(?:(?:пока|покеда|пока-пока|бб|bb|bye|чао|прощай|споки|удачи)(?:[\s,!.]+(?:всем|ребята|ребят|народ|"
                    r"поскинсон|поскинс|пос|бро|друг))*[\s,!.)]*$|до связи|до завтра|до встречи|до скорого|увидимся|"
                    r"спокойной ночи|доброй ночи|сладких снов|я спать|пошёл спать|пошла спать|я пошёл|я пошла|"
                    r"я ушёл|я ушла|я побежал|я побежала|я отчаливаю)(?![\w])", re.I)
TALK_IDLE = 7 * 60          # без ответов людей столько — режим сам выключается
TALK_NUDGE = 100            # в тишине бот пишет сам один раз через столько секунд

TALK_NOTE = ("РЕЖИМ БЕСЕДЫ: с тобой просто болтают по душам, и ты ведёшь разговор как живой человек, а не бот на подхвате. "
             "2–3 коротких фразы, по-простому как друг в переписке (без красивых оборотов и пересказов), добродушно, без оскорблений и наездов, если отношение не холодное; мат совсем редко и по делу. Ты правда интересуешься собеседником: отвечай по сути, делись "
             "своим мнением и историями, подмечай детали из его слов и памяти о нём, а в конце чаще всего задавай свой вопрос "
             "(не шаблонный «а ты как?», а по теме) или бросай новую тему, чтобы беседа не затухала. Не повторяй вопросы, "
             "которые уже задавал. Про фильмы, аниме, игры и т.п. говори только то, в чём уверен: не знаешь — переспроси или загугли, ничего не выдумывай. Если сейчас много людей — обращайся по именам и втягивай всех.")
TALK_START_NOTE = ("\n\nСобеседник предложил поговорить: согласись в своём стиле и сам открой беседу — спроси о чём-то "
                   "конкретном (о нём, его дне, играх, Майнкрафте, жизни), не «о чём хочешь поговорить».")
TALK_NUDGE_NOTE = ("\n\nВ чате тишина — собеседник притих. Сам напиши что-нибудь: зацепись за прошлую тему иначе или "
                   "брось новую, задай вопрос, можно подколоть, что заснул.")


async def talk_control(m):
    """Включает/выключает режим беседы в канале. True — сообщение уже обработано."""
    ch = m.channel.id
    text = m.content + " " + transcripts.get(m.id, "")
    bye = len(text.split()) <= 6 and BYE_RX.search(text)
    if ch in talk_mode and (TALK_STOP_RX.search(text) or bye):
        talk_mode.pop(ch, None)
        if bye:
            phrases = ["давай, пока", "до связи, заходи ещё", "бывай, было норм поболтать", "пока, не пропадай", "ну давай, до скорого"]
        else:
            phrases = ["ладно, молчу", "окей, пообщались, зови если чё", "всё, ушёл в тень", "понял, умолкаю"]
        await m.reply(random.choice(phrases), mention_author=False)
        return True
    if ch not in talk_mode and TALK_START_RX.search(text):
        if flooding(m.author.id):
            return True
        talk_mode[ch] = {"last": time.time(), "nudged": False, "msg": m}
        await respond(m, True, False, note=TALK_NOTE + TALK_START_NOTE)
        return True
    return False


PASTE_SAVE_RX = re.compile(r"(?:запомни|сохрани|добавь|запиши)\s+(?:это\s+)?(?:как\s+)?пасту\s*(.*)", re.I | re.S)


answer_msgs = {}        # id сообщения бота → id запомненного ответа, на котором оно основано
WRONG_RX = re.compile(r"неправильн|неверн|не так|не то|враньё|вранье|врёшь|врешь|бред|чушь|ошиб|устарел|не работает", re.I)


async def wrong_answer(m):
    """Ответом на сообщение бота «неправильно / врёшь / устарело» — запомненный ответ стирается (найдётся заново)."""
    ref = m.reference
    rid = answer_msgs.get(ref.message_id) if ref else None
    if not rid or not WRONG_RX.search(m.content) or len(m.content) > 120:
        return
    if store.drop_answer(rid):
        log.info("стёр запомненный ответ #%s по жалобе %s: %s", rid, m.author.display_name, m.content[:80])
        try:
            await m.add_reaction("📝")
        except discord.HTTPException:
            pass


async def paste_from_reply(m):
    """Ответом на сообщение: «поскинсон, запомни пасту <название>» — текст того сообщения становится пастой."""
    mt = PASTE_SAVE_RX.search(m.content)
    if not mt or not m.reference or not is_called(m):
        return False
    ref = m.reference.resolved
    if not isinstance(ref, discord.Message):
        try:
            ref = await m.channel.fetch_message(m.reference.message_id)
        except discord.HTTPException:
            return False
    text = ref.content.strip()[:PASTE_MAX]
    if paste_count(m.guild.id, m.author.id) >= PASTES_PER_USER:
        await m.reply(f"у тебя уже {PASTES_PER_USER} паст, хватит засирать базу", mention_author=False)
        return True
    if not text:
        await m.reply("там нет текста, чё мне сохранять, воздух?", mention_author=False)
        return True
    name = mt.group(1).strip().strip("«»\"'.,:").strip() or " ".join(text.split()[:4])
    name, existed = store.add_paste(m.guild.id, name, text, m.author.id)
    await m.reply(f"📋 паста **{name}** {'обновлена' if existed else 'сохранена'}. вызывать: `/паста {name}`", mention_author=False)
    return True


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
        "**Беседа:** «поскинсон давай поговорим» — болтаю сам и задаю вопросы, «поскинсон хватит» — стоп\n"
        "**Пасты:** /паста, /паста_добавить, /пасты, /паста_удалить — или ответь на сообщение «поскинсон, запомни пасту <название>»\n"
        "**Служебное:** /заткнись, /говори, /напоминания, /отменить, /чтознаешь, /забудь, /лохдня_тут, /статистика\n"
        f"версия {VERSION}",
        ephemeral=True)


@tree.command(name="анекдот", description="Рассказать анекдот")
@app_commands.describe(тема="О чём (необязательно)")
async def c_joke(inter: discord.Interaction, тема: str = ""):
    async def run():
        joke, on_topic = await jokes.get(gid(inter), тема, nsfw=is_nsfw(inter.channel))
        if joke:                                 # дословно с сайтов; все лежат — сочиняет модель
            return (f"про {тема} не нашёл, держи другой\n\n" if тема and not on_topic else "") + joke
        return await say(gid(inter), {inter.user.id: inter.user.display_name},
                         f"{inter.user.display_name} просит анекдот" + (f" про: {тема}" if тема else " на любую тему")
                         + ". Расскажи один смешной анекдот, без вступлений.")
    await guarded(inter, run())


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
                             f"Обращайся к нему как <@{кого.id}>.", special=specials([кого])))


@tree.command(name="нарисуй", description="Нарисовать картинку")
@app_commands.describe(что="Что нарисовать (можно по-русски)")
async def c_draw(inter: discord.Interaction, что: str):
    if not image_ok(inter.user.id):
        await inter.response.send_message(f"не больше {IMAGES_PER_USER_HOUR} картинок в час, художник хуев", ephemeral=True)
        return
    await inter.response.defer(thinking=True)
    try:
        m = await router.complete([{"role": "user", "content": "Переведи на английский и подробно опиши для генератора "
                                    f"картинок (объект, стиль, детали, 1–2 предложения), только описание: {что}"}],
                                  role="light", max_tokens=200, temperature=0.4)
        prompt = (m.get("content") or что).strip().strip('"')
        try:
            data, name, src = await media.generate(prompt, nsfw=is_nsfw(inter.channel))
        except ValueError:
            await inter.followup.send("такое рисую только в 18+ канале и только со взрослыми" if not is_nsfw(inter.channel)
                                      else "с несовершеннолетними — никогда. иди нахуй")
            return
        comment = await say(gid(inter), {inter.user.id: inter.user.display_name},
                            f"{inter.user.display_name} попросил нарисовать: «{что}». Картинка готова. "
                            "Одной короткой едкой фразой прокомментируй его запрос.", max_tokens=120)
        await inter.followup.send(comment[:1900] or "на, любуйся", file=discord.File(io.BytesIO(data), filename=name))
    except Exception:
        log.exception("нарисуй")
        await inter.followup.send("кисточка сломалась, попробуй позже")


@tree.command(name="статистика", description="Расход нейросетей за сегодня (для админов)")
@app_commands.default_permissions(manage_guild=True)
async def c_stats(inter: discord.Interaction):
    rows = router.report()
    lines = [f"`{p}:{m}` — {req} запр., ошибок {err}, токенов {ti}+{to}" for p, m, req, err, ti, to in rows]
    roles = {r: len(router.candidates(r)) for r in ("chat", "light", "vision")}
    await inter.response.send_message(
        f"**poskinson {VERSION}**, провайдеры: {', '.join(router.clients) or 'нет'}\n"
        f"доступно моделей сейчас — болтовня: {roles['chat']}, служебных: {roles['light']}, зрение: {roles['vision']}\n"
        + ("\n".join(lines) or "сегодня ещё ничего не тратил"), ephemeral=True)


async def paste_autocomplete(inter: discord.Interaction, current: str):
    return [app_commands.Choice(name=f"{n} ({u})"[:100], value=n) for n, u in store.paste_names(gid(inter), current)]


@tree.command(name="паста", description="Кинуть пасту: по названию или случайную")
@app_commands.describe(название="Название или слово из пасты; пусто — случайная")
@app_commands.autocomplete(название=paste_autocomplete)
async def c_paste(inter: discord.Interaction, название: str = ""):
    g = gid(inter)
    row = store.find_paste(g, название) if название.strip() else store.random_paste(g)
    if not row:
        names = [n for n, _ in store.paste_names(g, limit=15)]
        await inter.response.send_message(
            ("нет такой пасты. есть: " + ", ".join(names)) if names else "паст ещё нет, добавь: /паста_добавить", ephemeral=True)
        return
    store.used_paste(g, row["name"])
    text = row["text"]
    await inter.response.send_message(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    for k in range(2000, len(text), 2000):
        await inter.followup.send(text[k:k + 2000], allowed_mentions=discord.AllowedMentions.none())


def paste_count(guild_id, user_id):
    store._pastes_table()
    return store.db.execute("SELECT COUNT(*) FROM pastes WHERE guild_id=? AND author_id=?", (guild_id, user_id)).fetchone()[0]


class PasteModal(discord.ui.Modal, title="Новая паста"):
    name = discord.ui.TextInput(label="Название", max_length=60, placeholder="например: батя в здании")
    text = discord.ui.TextInput(label="Текст пасты", style=discord.TextStyle.paragraph, max_length=4000)

    async def on_submit(self, inter: discord.Interaction):
        if paste_count(gid(inter), inter.user.id) >= PASTES_PER_USER:
            await inter.response.send_message(f"у тебя уже {PASTES_PER_USER} паст, удали старые", ephemeral=True)
            return
        name, existed = store.add_paste(gid(inter), str(self.name), str(self.text), inter.user.id)
        await inter.response.send_message(f"📋 паста **{name}** {'обновлена' if existed else 'сохранена'}. вызывать: `/паста {name}`")


@tree.command(name="паста_добавить", description="Добавить пасту (откроется окно для текста)")
async def c_paste_add(inter: discord.Interaction):
    await inter.response.send_modal(PasteModal())


@tree.command(name="пасты", description="Список паст сервера")
async def c_pastes(inter: discord.Interaction):
    rows = store.paste_names(gid(inter), limit=100)
    if not rows:
        await inter.response.send_message("паст нет. добавь: /паста_добавить или ответь на сообщение «поскинсон, запомни пасту <название>»",
                                          ephemeral=True)
        return
    text = "📋 **пасты** (название — сколько раз кидали):\n" + "\n".join(f"• {n} — {u}" for n, u in rows)
    await inter.response.send_message(text[:2000], ephemeral=True)


@tree.command(name="паста_удалить", description="Удалить пасту")
@app_commands.describe(название="Какую")
@app_commands.autocomplete(название=paste_autocomplete)
async def c_paste_del(inter: discord.Interaction, название: str):
    g = gid(inter)
    row = store.get_paste(g, название)
    if not row:
        await inter.response.send_message("нет такой пасты", ephemeral=True)
        return
    perms = getattr(inter.user, "guild_permissions", None)
    if row["author_id"] != inter.user.id and not (perms and perms.manage_messages):
        await inter.response.send_message("удалять может автор пасты или модеры. а ты ни то ни другое", ephemeral=True)
        return
    store.delete_paste(g, название)
    await inter.response.send_message(f"🗑️ паста **{row['name']}** удалена")


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
                      "унизительно. Используй то, что о них знаешь. Без заголовка, без итоговой строки.", max_tokens=400,
                      special=specials([x for x in (a, b) if isinstance(x, discord.Member)]))
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


@tree.command(name="лохдня_тут", description="Объявлять лоха дня в этом канале (для админов)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def c_loh_here(inter: discord.Interaction):
    store.put(inter.guild.id, "loh_channel", inter.channel.id)
    await inter.response.send_message(f"ок, лоха дня объявляю здесь, каждый день в {LOH_HOUR}:00")


@tree.command(name="обновить_инфо", description="Перечитать инфо-каналы и пересобрать базу знаний сервера (для админов)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def c_kb(inter: discord.Interaction):
    await inter.response.defer(thinking=True, ephemeral=True)
    ok = await build_guild_kb(inter.guild)
    kb, _ = memory.kb(inter.guild.id)
    await inter.followup.send(("✅ база знаний собрана:\n" if ok else "✕ не получилось (лимит или нет инфо-каналов), текущая:\n")
                              + (kb or "(пусто)")[:1800], ephemeral=True)


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


@tasks.loop(seconds=20)
async def talk_loop():
    now = time.time()
    for ch, st in list(talk_mode.items()):
        idle = now - st["last"]
        if idle >= TALK_IDLE:
            talk_mode.pop(ch, None)
            log.info("режим беседы в %s закончился по тишине", ch)
        elif idle >= TALK_NUDGE and not st["nudged"] and not router.busy(60, 3):
            st["nudged"] = True
            try:
                await respond(st["msg"], False, False, note=TALK_NOTE + TALK_NUDGE_NOTE)
            except Exception:
                log.exception("реплика в режиме беседы")


# ======================= напоминания =======================
@tasks.loop(seconds=15)
async def reminder_loop():
    for r in store.due_reminders():
        store.close_reminder(r["id"])
        text = f"⏰ <@{r['user_id']}> напоминаю: **{r['text']}**. " + random.choice(
            ["давай, шевели булками", "не благодари", "опять бы забыл, склеротик", "я тебе не секретарь, но ладно"])
        try:
            ch = client.get_channel(r["channel_id"]) or await client.fetch_channel(r["channel_id"])
            # пингуем только хозяина напоминания, что бы он туда ни написал
            await send_long(ch, text, mentions=discord.AllowedMentions(users=[discord.Object(id=r["user_id"])],
                                                                        everyone=False, roles=False))
        except Exception:
            try:
                user = await client.fetch_user(r["user_id"])
                await user.send(text)
            except Exception:
                log.exception("напоминание #%s не доставлено", r["id"])


# ======================= чтение истории для памяти =======================
scan_lock = asyncio.Lock()


def readable(ch):
    p = ch.permissions_for(ch.guild.me)
    return p.view_channel and p.read_message_history


def full_text(m):
    """Текст сообщения вместе с embed-блоками (правила и инфо часто постят ботом в embed)."""
    parts = [m.content]
    for e in m.embeds:
        parts += [e.title or "", e.description or ""]
        parts += [f"{f.name}: {f.value}" for f in e.fields]
    return "\n".join(x for x in parts if x).strip()


async def build_guild_kb(guild):
    chans = [c for c in guild.text_channels if KB_CHANNEL_RX.search(c.name) and readable(c)]
    lines = ["[все каналы сервера: " + ", ".join(f"#{c.name} id={c.id}" for c in guild.text_channels if readable(c))[:3000] + "]"]
    for c in chans:
        msgs = [m async for m in c.history(limit=KB_LIMIT)]
        msgs.reverse()
        body = "\n".join(f"{m.author.display_name}: {full_text(m)[:1500]}" for m in msgs if full_text(m))
        if body:
            lines.append(f"[канал #{c.name} id={c.id}]\n{body[:6000]}")
    log.info("база знаний %s: инфо-каналы %s", guild.name, ", ".join("#" + c.name for c in chans) or "не найдены")
    kb_dirty.discard(guild.id)
    return await memory.build_kb(guild.id, lines)


async def scan_guild(guild):
    async with scan_lock:
        if not memory.kb(guild.id)[0]:
            try:
                await build_guild_kb(guild)
            except Exception:
                log.exception("база знаний %s", guild.name)
        # самые живые каналы, кроме инфо (их уже прочитали для базы)
        chans = [c for c in guild.text_channels if readable(c) and not KB_CHANNEL_RX.search(c.name)]
        chans.sort(key=lambda c: c.last_message_id or 0, reverse=True)
        added = 0
        for ch in chans[:SCAN_CHANNELS]:
            if store.is_scanned(ch.id):
                continue
            try:
                added += await scan_channel(guild, ch) or 0
            except Exception:
                log.exception("чтение #%s", ch.name)
        if added:                                # карточки — один раз после всех каналов (пачками), а не после каждого
            await consolidate_all()


def scan_line(m):
    return (f"[бот {m.author.display_name}]: {full_text(m)[:300]}" if m.author.bot
            else f"{m.author.display_name}: {m.clean_content[:400]}")


def scan_names(msgs):
    names = {}
    for m in msgs:
        if not m.author.bot:
            names[m.author.display_name.lower()] = m.author.id
            names.setdefault(m.author.name.lower(), m.author.id)
    return names


def save_scan_facts(content, names, gid):
    """Строки «ник | факт» → память. Возвращает, сколько новых."""
    added = 0
    for line in (content or "").splitlines():
        if "|" not in line:
            continue
        who, fact = (s.strip() for s in line.split("|", 1))
        who = who.strip("-•* @").lower()
        if who == "сервер":
            added += memory.add_fact(0, "сервер", fact, gid)[0]
        elif who in names:
            added += memory.add_fact(names[who], who, fact, gid)[0]
    return added


async def scan_bulk(guild, ch, msgs):
    """Вся история канала одним запросом через OpenRouter. None — не вышло (тогда по кускам через Groq)."""
    if not msgs or not router.candidates("bulk") or not cap_ok("bulk"):
        return None
    text = "\n".join(scan_line(m) for m in msgs)[-SCAN_BULK_CHARS:]
    prompt = SCAN_PROMPT.replace("Не больше 25 строк", "Не больше 40 строк")
    try:
        r = await router.complete([{"role": "system", "content": prompt}, {"role": "user", "content": text}],
                                  role="bulk", max_tokens=2500, temperature=0)
    except RateLimited as e:
        log.warning("#%s: целиком не прочитан (%s) — читаю по кускам", ch.name, str(e)[:120])
        return None
    added = save_scan_facts(r.get("content"), scan_names(msgs), guild.id)
    log.info("#%s: прочитан целиком (%d символов) моделью %s, новых фактов: %d", ch.name, len(text), r.get("_model"), added)
    return added


async def scan_channel(guild, ch):
    log.info("читаю историю #%s (%s)", ch.name, guild.name)
    # сообщения ботов — только как контекст (иначе их сценки и дуэли разбирались как факты о людях)
    msgs = [m async for m in ch.history(limit=SCAN_LIMIT) if (full_text(m) if m.author.bot else m.content.strip())]
    msgs.reverse()
    for m in msgs:
        if not m.author.bot:
            store.seen(guild.id, m.author.id, m.author.display_name, m.created_at.timestamp())
    added = await scan_bulk(guild, ch, msgs)
    chunks, cur, size = [], [], 0
    for m in msgs if added is None else []:      # OpenRouter не справился — по кускам через Groq, как раньше
        line = scan_line(m)
        if size + len(line) > SCAN_CHUNK_CHARS and cur:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((m, line))
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    added = added or 0
    for i, chunk in enumerate(chunks):
        names = scan_names(m for m, _ in chunk)
        text = "\n".join(line for _, line in chunk)
        r = None
        await yield_to_chat()
        if not cap_ok("scan"):
            log.warning("дневной лимит чтения истории — #%s дочитаю завтра", ch.name)
            return
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
        added += save_scan_facts(r.get("content"), names, guild.id)
        log.info("#%s: кусок %d/%d, новых фактов: %d", ch.name, i + 1, len(chunks), added)
        if i + 1 < len(chunks):
            await asyncio.sleep(SCAN_PAUSE)
    # сводка канала по последним сообщениям — чтобы сразу знать, о чём тут говорят
    tail = msgs[-120:]
    if tail:
        await memory.summarize(ch.id, guild.id, ch.name, [f"{'[бот] ' if m.author.bot else ''}{m.author.display_name}: {(full_text(m) if m.author.bot else m.clean_content)[:300]}"
                                                    for m in tail],
                               tail[-1].id)
    store.mark_scanned(ch.id)
    log.info("#%s прочитан: %d сообщений, %d новых фактов", ch.name, len(msgs), added)
    return added


async def consolidate_all():
    """Пересобрать карточки всех, у кого есть несжатые факты: сначала пачками через OpenRouter,
    что осталось — по одной через Groq, с паузами (лимиты)."""
    todo = memory.needing_consolidation()
    people = [(uid, name) for uid, gid, name in todo if uid and (uid, 0) not in consolidating]
    done = set()
    for i in range(0, len(people), CARD_BATCH):
        part = people[i:i + CARD_BATCH]
        if not router.candidates("bulk") or not cap_ok("bulk"):
            break
        consolidating.update((uid, 0) for uid, _ in part)
        try:
            done |= await memory.consolidate_batch(part)
        except RateLimited as e:
            log.warning("карточки пачкой не вышли: %s", str(e)[:120])
            break
        finally:
            consolidating.difference_update((uid, 0) for uid, _ in part)
    for uid, gid, name in todo:
        if uid in done:
            continue
        k = (uid, gid) if not uid else (uid, 0)
        if k in consolidating:
            continue
        consolidating.add(k)
        try:
            await yield_to_chat()
            if not cap_ok("card"):
                return
            await memory.consolidate(uid, gid, name)
        finally:
            consolidating.discard(k)
        await asyncio.sleep(5)


@tasks.loop(minutes=2)
async def summary_loop():
    """Сводки каналов: каждые SUMMARY_EVERY сообщений или после паузы, если накопилось ≥ 5."""
    if router.busy():
        return                                   # сводки подождут затишья
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
        if not cap_ok("summary"):
            return
        if await memory.summarize(cid, ch.guild.id, ch.name, lines, msgs[-1].id):
            pending[cid] = 0


# ---- v2: факты и отношение — не из каждого ответа, а пакетом по накопленному чату (роль memory) ----
extract_buf = {}        # channel_id → {"guild": id, "items": [...], "first_call": время первого обращения к боту}


def buffer_msg(m):
    if not cfg.PROMPT_V2:
        return
    text = msg_text(m)
    if not text:
        return
    b = extract_buf.setdefault(m.channel.id, {"guild": m.guild.id, "items": [], "first_call": 0})
    b["items"].append({"id": m.id, "uid": m.author.id, "name": m.author.display_name, "text": text[:400],
                       "to_bot": False, "sam": is_sam(m.author)})
    del b["items"][:-120]                        # на случай, если разбор долго не случается


def note_reply(m, answer, called):
    b = extract_buf.get(m.channel.id) if cfg.PROMPT_V2 and m.guild else None
    if not b:
        return
    for it in b["items"]:
        if it["id"] == m.id and called:
            it["to_bot"] = True
            b["first_call"] = b["first_call"] or time.time()
    b["items"].append({"id": 0, "uid": 0, "name": None, "text": answer[:300], "to_bot": False, "sam": False})


async def extract(cid, b):
    items, b["items"], b["first_call"] = b["items"], [], 0
    speakers = {it["name"].lower(): it for it in items if it["uid"]}
    callers = {it["name"].lower() for it in items if it["to_bot"]}
    lines = [f"ты ({NAME}): {it['text']}" if not it["uid"] else
             f"{it['name']}{' (боту)' if it['to_bot'] else ''}: {it['text']}" for it in items]
    try:
        r = await router.complete([{"role": "system", "content": cfg.EXTRACT_PROMPT},
                                   {"role": "user", "content": "\n".join(lines)}],
                                  role="extract", max_tokens=500, temperature=0, json_mode=True)
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (r.get("content") or "").strip()))
    except (RateLimited, ValueError) as e:
        log.warning("разбор чата не удался: %s", e)
        return
    gid = b["guild"]
    for f in data.get("facts") or []:
        who, fact = str(f.get("who", "")).strip().lstrip("@"), str(f.get("fact", "")).strip()
        if not good_fact(fact):
            continue
        if who.lower() == "сервер":
            new, due = memory.add_fact(0, "сервер", fact, gid)
            if new:
                log.info("запомнил о сервере: %s", fact)
            if due:
                schedule_consolidation(0, gid, "сервер")
        elif who.lower() in speakers:            # только о тех, кто сам писал в этом куске (не слухи)
            it = speakers[who.lower()]
            new, due = memory.add_fact(it["uid"], it["name"], fact, gid)
            if new:
                log.info("запомнил: %s | %s", it["name"], fact)
            if due:
                schedule_consolidation(it["uid"], 0, it["name"])
    for a in data.get("attitude") or []:
        who = str(a.get("who", "")).strip().lstrip("@").lower()
        if who not in callers:
            continue
        try:
            att = max(-3, min(3, int(a.get("score", 0))))
        except (TypeError, ValueError):
            continue
        if att:
            it = speakers[who]
            score = memory.rep_apply(it["uid"], att, sam=it["sam"])
            log.info("отношение к %s: %+d → %.0f", it["name"], att, score)


@tasks.loop(minutes=1)
async def extract_loop():
    now = time.time()
    for cid, b in list(extract_buf.items()):
        n = len(b["items"])
        due = (b["first_call"] and (now - b["first_call"] >= cfg.EXTRACT_WAIT or n >= cfg.EXTRACT_MAX)) \
            or n >= cfg.EXTRACT_IDLE_MAX
        if due and cap_ok("extract"):
            await extract(cid, b)


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
    await lexicon_all(force=True)
    memory.backup()


@tasks.loop(hours=1)
async def scan_loop():
    for guild in client.guilds:
        if guild.id in kb_dirty:
            try:
                await build_guild_kb(guild)
            except Exception:
                log.exception("база знаний %s", guild.name)
        await scan_guild(guild)
    await lexicon_all()                          # первый раз — не дожидаясь ночи


# ======================= словарь сервера =======================
lexicon_cache = {}      # guild_id → данные словаря (из settings)


def lexicon(guild_id):
    if guild_id not in lexicon_cache:
        try:
            lexicon_cache[guild_id] = json.loads(store.get(guild_id, "lexicon") or "{}")
        except ValueError:
            lexicon_cache[guild_id] = {}
    return lexicon_cache[guild_id]


def lexicon_text(guild_id):
    """Блок для подсказки: местный сленг, мемы, манера и пара случайных живых фраз людей."""
    lx = lexicon(guild_id) if guild_id else {}
    if not lx:
        return ""
    out = ["КАК ОБЩАЮТСЯ НА ЭТОМ СЕРВЕРЕ (подстраивайся, словечки — к месту, не в каждом ответе):"]
    if lx.get("style"):
        out.append("манера: " + str(lx["style"])[:250])
    if lx.get("slang"):
        out.append("сленг: " + "; ".join(map(str, lx["slang"][:20]))[:450])
    if lx.get("memes"):
        out.append("мемы: " + "; ".join(map(str, lx["memes"][:8]))[:300])
    phrases = [str(x) for x in lx.get("phrases") or [] if str(x).strip()]
    if phrases:
        out.append("так пишут люди (для тона, не цитируй): " + " / ".join(random.sample(phrases, min(cfg.LEXICON_SHOWN, len(phrases)))))
    return "\n".join(out)


async def build_lexicon(guild):
    since = datetime.now(TZ) - timedelta(hours=cfg.LEXICON_HOURS)
    chans = [c for c in guild.text_channels if readable(c) and not is_nsfw(c) and not KB_CHANNEL_RX.search(c.name)]
    chans.sort(key=lambda c: c.last_message_id or 0, reverse=True)
    lines, size = [], 0
    for ch in chans[:8]:
        try:
            async for x in ch.history(limit=400, after=since, oldest_first=False):
                t = x.clean_content.strip()
                if x.author.bot or not t or t.startswith(("/", "!", "http")) or CALL_RX.match(t):
                    continue
                lines.append(t[:200].replace("\n", " "))
                size += len(lines[-1])
                if size > cfg.LEXICON_CHARS:
                    break
        except discord.HTTPException:
            continue
        if size > cfg.LEXICON_CHARS:
            break
    if len(lines) < 30:
        log.info("словарь %s: мало сообщений (%d), пропускаю", guild.name, len(lines))
        return
    random.shuffle(lines)
    old = json.dumps(lexicon(guild.id), ensure_ascii=False)
    r = await router.complete([{"role": "system", "content": cfg.LEXICON_PROMPT},
                               {"role": "user", "content": f"Прошлый словарь: {old}\n\nСообщения:\n" + "\n".join(lines)}],
                              role="extract", max_tokens=900, temperature=0.3, json_mode=True)
    data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", (r.get("content") or "").strip()))
    lx = {k: data.get(k) for k in ("slang", "memes", "style", "phrases") if data.get(k)}
    if not lx:
        return
    store.put(guild.id, "lexicon", json.dumps(lx, ensure_ascii=False))
    lexicon_cache[guild.id] = lx
    log.info("словарь %s обновлён: %d словечек, %d мемов, %d фраз (из %d сообщений)", guild.name,
             len(lx.get("slang") or []), len(lx.get("memes") or []), len(lx.get("phrases") or []), len(lines))


async def lexicon_all(force=False):
    for guild in client.guilds:
        if not force and lexicon(guild.id):
            continue
        if not cap_ok("lexicon"):
            return
        try:
            await build_lexicon(guild)
        except (RateLimited, ValueError, discord.HTTPException) as e:
            log.warning("словарь %s не собран: %s", guild.name, e)


# ======================= запуск =======================
def commands_hash():
    import hashlib
    sig = json.dumps([c.to_dict(tree) for c in tree.get_commands()], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(sig.encode()).hexdigest()


async def sync_commands(guild, force=False):
    """Регистрировать команды, только если их набор поменялся: у Discord строгий лимит на это."""
    h = commands_hash()
    if not force and store.get(guild.id, "commands_hash") == h:
        return
    tree.copy_global_to(guild=guild)
    try:
        cmds = await tree.sync(guild=guild)
        store.put(guild.id, "commands_hash", h)
        log.info("команды на %s: %d", guild.name, len(cmds))
    except discord.HTTPException:
        log.exception("не удалось зарегистрировать команды на %s", guild.name)


@client.event
async def on_ready():
    log.info("вошёл как %s, серверов: %d", client.user, len(client.guilds))
    for guild in client.guilds:
        asyncio.create_task(sync_commands(guild))     # в фоне: если Discord притормозит, остальное не ждёт
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
    for loop in (talk_loop, loh_loop, reminder_loop, scan_loop, summary_loop, maintenance_loop, extract_loop):
        if not loop.is_running():
            loop.start()


@client.event
async def on_guild_join(guild):
    log.info("добавили на сервер %s", guild.name)
    await sync_commands(guild)
    asyncio.create_task(scan_guild(guild))


if __name__ == "__main__":
    import os
    os.umask(0o077)                     # база, логи, копии — только владельцу
    from logging.handlers import RotatingFileHandler
    LOG_DIR.mkdir(exist_ok=True)
    fh = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), fh])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("primp").setLevel(logging.WARNING)
    client.run(TOKEN, log_handler=None)
