"""Анекдоты из интернета — дословно, без нейросети (не тратит лимиты и не выдумывает несмешное).
Источники вперемешку: anekdot.ru (там же поиск по теме), nekdo.ru, anekdotov.net (baneks.ru не годится —
подменяет русские буквы латинскими). Уже рассказанные на сервере
не повторяются (таблица jokes_seen); национальные/религиозные и (вне 18+) пошлые — пропускаются."""
import hashlib
import html
import logging
import random
import re
import time
from urllib.parse import quote

import httpx

log = logging.getLogger("poskinson.jokes")

# просьба рассказать: «пос, анекдот», «расскажи шутку», «знаешь анекдот про…», «пошути»;
# не «я пошутил», «пошути над ним» (это прожарка — пусть модель), «анекдоты тупые»
JOKE_RX = re.compile(r"(?:расскаж\w*|давай|кинь|дай|го|хочу|ещ[её]|травани|можешь|знаешь|нужен|нужна)\s+(?:\w+\s+){0,2}"
                     r"(?:анекдот|шутк|прикол)\w*|^\W*(?:\w+\W+)?(?:анекдот|анек)(?:а|ик|ы)?(?:\s+(?:про|о|об)\s.+)?[\s?!.)]*$|"
                     r"(?<![\w])пошути(?:\s+(?:что|чё|че|что-нибудь|что-то|чтонибудь))?[\s?!.)]*$", re.I)
TOPIC_RX = re.compile(r"(?:анекдот|шутк|прикол)\w*\s+(?:про|о|об|на тему)\s+(.{2,40}?)\s*[?!.)]*$", re.I)

# табу бота: по национальности, религии, ориентации, инвалидности — такие анекдоты не берём
TABOO_RX = re.compile(r"еврей|евре[ий]к|жид|чукч|грузин|армян|узбек|таджик|кавказ|негр|чёрн(?:ый|ые|ого) парень|хохл|москал|"
                      r"цыган|китаец|китайц|азиат|мусульм|ислам|аллах|раввин|синагог|мечет|верующ|церк|батюшк|священник|гей|пидор|педик|голуб(?:ой|ые)|"
                      r"инвалид|даун|дебил|аутист|глухонем|слеп(?:ой|ая)|чурк|хач|лезгин|чечен|негрит", re.I)
NSFW_RX = re.compile(r"секс|трах|минет|член|влагалищ|вагин|оргазм|кончил|сперм|сиськ|сись?к|жоп[уае]|презерватив|проститут|"
                     r"шлюх|порн|эрекц|голая|голый|в постел|изнасил|интим|эротич|сексшоп|потенц", re.I)
POLITICS_RX = re.compile(r"путин|зеленск|байден|трамп|навальн|депутат|госдум|кремл|украин|хохл|войн|сво(?![а-яё])|мобилиз", re.I)

MAX_LEN = 900       # длинные «истории» — не анекдоты
UA = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"}


def clean(raw):
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p>\s*<p[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(raw).replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", "\n".join(x.strip() for x in text.split("\n"))).strip()


def topic_of(text):
    m = TOPIC_RX.search(text or "")
    return m.group(1).strip() if m else ""


class Jokes:
    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS jokes_seen (guild_id INTEGER, hash TEXT, ts REAL, PRIMARY KEY (guild_id, hash))")
        db.commit()
        self.web = httpx.AsyncClient(timeout=10, follow_redirects=True, headers=UA)

    @staticmethod
    def key(text):
        return hashlib.sha1(re.sub(r"\W+", "", text.lower()).encode()).hexdigest()[:16]

    def seen(self, guild_id, text):
        return self.db.execute("SELECT 1 FROM jokes_seen WHERE guild_id=? AND hash=?",
                               (guild_id, self.key(text))).fetchone() is not None

    def mark(self, guild_id, text):
        self.db.execute("INSERT OR REPLACE INTO jokes_seen VALUES (?,?,?)", (guild_id, self.key(text), time.time()))
        self.db.commit()

    def ok(self, text, nsfw):
        if not text or len(text) < 25 or len(text) > MAX_LEN:
            return False
        if TABOO_RX.search(text) or POLITICS_RX.search(text):
            return False
        return nsfw or not NSFW_RX.search(text)

    # ---------- источники ----------
    async def page(self, url):
        r = await self.web.get(url)
        r.raise_for_status()
        return r.text

    async def anekdot_ru(self, topic=""):
        if topic:
            url = f"https://www.anekdot.ru/search/?query={quote(topic)}&ch%5Bj%5D=on&mode=any"
        else:
            url = "https://www.anekdot.ru/random/anekdot/"
        return [clean(x) for x in re.findall(r'<div class="text">(.*?)</div>', await self.page(url), re.S)]

    async def anekdotov(self, topic=""):
        if topic:
            return []
        r = await self.web.get("https://anekdotov.net/anekdot/")
        r.raise_for_status()
        body = r.content.decode("cp1251", errors="replace")
        return [clean(x) for x in re.findall(r"class=anekdot>(.*?)</div>", body, re.S)]

    async def nekdo(self, topic=""):
        if topic:
            return []
        body = await self.page("https://nekdo.ru/random/")
        return [clean(x) for x in re.findall(r'class="text"[^>]*>(.*?)</div>', body, re.S)]

    async def get(self, guild_id, topic="", nsfw=False):
        """→ (текст анекдота, нашёлся ли именно по теме) или (None, False), если все источники молчат."""
        topic = (topic or "").strip()[:40]
        sources = [self.anekdot_ru, self.nekdo, self.anekdotov]
        attempts = [(topic, [self.anekdot_ru])] if topic else []
        attempts.append(("", random.sample(sources, len(sources))))
        for t, srcs in attempts:
            for src in srcs:
                for _ in range(2 if not t else 1):          # случайные страницы — можно перекрутить разок
                    try:
                        found = await src(t)
                    except (httpx.HTTPError, ValueError) as e:
                        log.warning("анекдоты %s: %s", src.__name__, e)
                        break
                    good = [x for x in found if self.ok(x, nsfw) and not self.seen(guild_id, x)]
                    if good:
                        joke = random.choice(good)
                        self.mark(guild_id, joke)
                        log.info("анекдот (%s%s): %s", src.__name__, f", про {t}" if t else "", joke[:60].replace("\n", " "))
                        return joke, bool(t)
        return None, False
