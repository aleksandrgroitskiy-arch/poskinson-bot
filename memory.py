"""Память в слоях.

  1. facts      — сырые факты (из чата «ЗАПОМНИ: …» и из чтения истории), только дописываются
  2. profiles   — карточка человека / сервера (user_id=0): факты, сжатые нейросетью по разделам;
                  пересобирается, когда накопилось CONSOLIDATE_AFTER новых фактов, и каждую ночь
  3. channels   — сводка «что сейчас происходит» по каналу, обновляется каждые SUMMARY_EVERY
                  сообщений или после паузы
  4. episodes   — хронология сервера: по строке на отрезок разговора («02.10 вечер: …»)

В подсказку идут: карточки + свежие факты сверх карточки + сводка канала + последние эпизоды.
Раз в день — копия базы и читаемый дамп в backups/ (для разбора ошибок и отката).
"""
import json
import logging
import re
import sqlite3
import time
from datetime import datetime

from config import (BACKUP_DIR, BACKUP_KEEP, CONSOLIDATE_AFTER, EPISODES_IN_PROMPT, MAX_FACTS, MAX_SERVER_FACTS, REP_DECAY_PER_DAY,
                    REP_HOURLY_CAP, REP_STEP, REP_TIERS, SAM_REP_MULT, TZ)
from llm import RateLimited

log = logging.getLogger("poskinson.memory")

CARD_PROMPT = """Ты ведёшь досье на участника Discord-сервера друзей для бота-тролля, который общается с ним и подкалывает его.
Собери из старой карточки и новых фактов НОВУЮ карточку. Правила:
- СТРОГО только то, что прямо написано в фактах или старой карточке. Ничего не додумывай, не обобщай сверх сказанного, не придумывай прозвищ, мемов и отношений, которых нет в фактах. Мало фактов — короткая карточка (2 факта → 1–2 строки).
- Если факты противоречат — верь более новым (они ниже в списке): старое отметь как бывшее («раньше …, теперь …»).
- Объединяй повторы, выкидывай мусор и одноразовые мелочи, сохраняй смешные детали и прозвища.
- Разделы (пустые пропускай), каждый — одна строка:
Кто: …
Прозвища: …
Игры: …
Учёба/работа: …
Вкусы и хобби: …
Отношения: (с кем дружит/спорит/кого подкалывает)
Косяки и мемы: …
- Всего не больше 700 символов. Только карточка, без вступлений."""

BATCH_SUFFIX = """

Сейчас людей несколько: на каждый блок «### id=…» — своя карточка по тем же правилам; факты одного человека другому не переносить.
Ответ — только JSON без пояснений: {"cards": {"<id>": "<карточка>", …}}"""

SERVER_CARD_PROMPT = """Ты ведёшь «лор» Discord-сервера друзей для бота-тролля.
Собери из старого лора и новых фактов НОВЫЙ лор: внутренние мемы и шутки, традиции, прозвища, кто есть кто, общие истории.
СТРОГО только то, что прямо есть в фактах или старом лоре — ничего не придумывай и не приукрашивай; по строке на факт, мало фактов — мало строк.
Если факты противоречат — верь более новым. Объединяй повторы, выкидывай мусор. Строки вида «- …», не больше 900 символов.
Только лор, без вступлений."""

SUMMARY_PROMPT = """Ниже прошлая сводка Discord-канала и новые сообщения после неё.
Ответь JSON: {"summary": "...", "episode": "..."}
summary — обновлённая сводка «что сейчас и недавно происходит в канале»: темы, кто что говорил, договорённости, незакрытые вопросы, споры. До 600 символов, по-русски.
episode — одна строка для хронологии сервера о самом заметном в НОВЫХ сообщениях (до 140 символов), или "" если ничего заметного."""


KB_PROMPT = """Ниже сообщения из информационных каналов Discord-сервера Майнкрафт-проекта (правила, инфо, гайды, анонсы, заявки).
Собери по ним «Базу знаний сервера» для бота, который отвечает новичкам. Строго только то, что прямо написано — ничего не додумывай
(особенно IP, версии, цены, правила). Разделы (пустые пропускай), коротко:
Сервер: название, режим/тип, версия игры, Java/Bedrock, лаунчер/сборка
Как зайти: IP/адрес и шаги (вайтлист, заявка, регистрация, моды)
Заявки: где и как подать, что указать
Правила: самое главное кратко (5–10 пунктов) + где полные
Донат/магазин: если есть
Каналы: по строке «<#id> — для чего» (id бери из пометок [канал #имя id=…])
Ссылки: сайт, карта, соцсети, если есть
Важные анонсы: последние 2–3, с датой
Не больше 1800 символов. Только база, без вступлений."""


def tidy_card(card, limit=900):
    """Пустые разделы («Прозвища: —») долой; длинную карточку режем по целой строке."""
    lines = [x.rstrip() for x in card.strip().splitlines()
             if x.strip() and not re.fullmatch(r"[^:\n]{2,30}:\s*[—–-]*\s*(?:нет|не указано)?\.?\s*", x.strip(), re.I)]
    out = ""
    for x in lines:
        if out and len(out) + len(x) + 1 > limit:
            break
        out += ("\n" if out else "") + x
    return out[:limit]


class Memory:
    def __init__(self, store, router):
        self.store = store
        self.router = router
        db = self.db = store.db
        db.execute("CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER, guild_id INTEGER, name TEXT, card TEXT,"
                   " updated REAL, PRIMARY KEY (user_id, guild_id))")
        db.execute("CREATE TABLE IF NOT EXISTS channels (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT,"
                   " summary TEXT, last_msg_id INTEGER, updated REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS rep (user_id INTEGER PRIMARY KEY, score REAL, updated REAL,"
                   " hour_start REAL, hour_delta REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS kb (guild_id INTEGER PRIMARY KEY, text TEXT, updated REAL)")
        db.execute("CREATE TABLE IF NOT EXISTS episodes (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,"
                   " channel_id INTEGER, ts REAL, text TEXT)")
        db.commit()

    # ---------- ключ карточки: человек (guild 0) или сервер (user 0) ----------
    @staticmethod
    def _key(user_id, guild_id):
        return (user_id, 0) if user_id else (0, guild_id)

    def card(self, user_id, guild_id=0):
        r = self.db.execute("SELECT card, updated FROM profiles WHERE user_id=? AND guild_id=?",
                            self._key(user_id, guild_id)).fetchone()
        return (r[0], r[1]) if r else ("", 0)

    def fresh_facts(self, user_id, guild_id=0, limit=None):
        """Факты, которых ещё нет в карточке."""
        _, since = self.card(user_id, guild_id)
        q = "SELECT fact FROM facts WHERE user_id=? AND ts>?" + (" AND guild_id=?" if not user_id else "")
        args = [user_id, since] + ([guild_id] if not user_id else [])
        rows = self.db.execute(q + " ORDER BY ts DESC" + (f" LIMIT {int(limit)}" if limit else ""), args).fetchall()
        return [r[0] for r in rows]

    def add_fact(self, user_id, name, fact, guild_id=0):
        """True, если факт новый. Возвращает (новый?, пора_пересобрать?)."""
        new = self.store.add_fact(user_id, name, fact, guild_id)
        due = new and len(self.fresh_facts(user_id, guild_id)) >= CONSOLIDATE_AFTER
        return new, due

    # ---------- пересборка карточек ----------
    async def consolidate(self, user_id, guild_id=0, name=""):
        old, _ = self.card(user_id, guild_id)
        q = "SELECT fact FROM facts WHERE user_id=?" + (" AND guild_id=?" if not user_id else "") + " ORDER BY ts"
        facts = [r[0] for r in self.db.execute(q, [user_id] + ([guild_id] if not user_id else []))][-120:]
        if not facts:
            return False
        started = time.time()
        user_part = (f"Участник: {name}\n" if name else "") + "Старая карточка:\n" + (old or "(нет)") + \
                    "\n\nФакты (старые сверху, новые снизу):\n" + "\n".join("- " + f for f in facts)
        try:
            m = await self.router.complete([{"role": "system", "content": CARD_PROMPT if user_id else SERVER_CARD_PROMPT},
                                            {"role": "user", "content": user_part}],
                                           role="memory", max_tokens=700, temperature=0)
        except RateLimited as e:
            log.warning("карточка %s не пересобрана: %s", name or user_id, e)
            return False
        card = (m.get("content") or "").strip()[:1200]
        if len(card) < 10:
            return False
        uid, gid = self._key(user_id, guild_id)
        self.db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)", (uid, gid, name, card, started))
        self.db.commit()
        log.info("карточка %s пересобрана (%d фактов) моделью %s", name or ("сервер" if not user_id else user_id),
                 len(facts), m.get("_model"))
        return True

    async def consolidate_batch(self, people):
        """Карточки нескольких людей одним запросом (роль bulk). people: [(user_id, ник)] → id, кому собрали.
        RateLimited пробрасывается — тогда вызывающий собирает по одному, как раньше."""
        names, blocks, started = dict(people), [], time.time()
        for uid, name in people:
            old, _ = self.card(uid)
            facts = [r[0] for r in self.db.execute("SELECT fact FROM facts WHERE user_id=? ORDER BY ts", (uid,))][-120:]
            if facts:
                blocks.append(f"### id={uid} ник={name}\nСтарая карточка:\n{old or '(нет)'}\n"
                              "Факты (старые сверху, новые снизу):\n" + "\n".join("- " + f for f in facts))
        if not blocks:
            return set()
        m = await self.router.complete([{"role": "system", "content": CARD_PROMPT + BATCH_SUFFIX},
                                        {"role": "user", "content": "\n\n".join(blocks)}],
                                       role="bulk", max_tokens=400 * len(blocks) + 300, temperature=0)
        raw = m.get("content") or ""
        try:
            cards = json.loads(raw[raw.index("{"):raw.rindex("}") + 1]).get("cards") or {}
        except ValueError:
            log.warning("карточки пачкой: ответ не JSON (%s)", m.get("_model"))
            return set()
        done = set()
        for k, card in cards.items():
            uid = int(k) if str(k).strip().isdigit() else None
            if uid not in names or not isinstance(card, str) or len(card.strip()) < 10:
                continue
            self.db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)",
                            (uid, 0, names[uid], tidy_card(card), started))
            done.add(uid)
        self.db.commit()
        log.info("карточки пачкой: %d из %d моделью %s", len(done), len(blocks), m.get("_model"))
        return done

    def needing_consolidation(self, at_least=1):
        """Все, у кого есть факты сверх карточки: [(user_id, guild_id, name)]."""
        rows = self.db.execute("SELECT DISTINCT user_id, guild_id, name FROM facts").fetchall()
        seen, out = set(), []
        for uid, gid, name in rows:
            k = self._key(uid, gid)
            if k in seen:
                continue
            seen.add(k)
            if len(self.fresh_facts(uid, gid)) >= at_least:
                out.append((uid, gid, name))
        return out

    def forget(self, user_id):
        n = self.store.forget(user_id)
        self.db.execute("DELETE FROM profiles WHERE user_id=? AND guild_id=0", (user_id,))
        self.db.commit()
        return n

    def about(self, user_id):
        card, _ = self.card(user_id)
        fresh = self.fresh_facts(user_id, limit=15)
        parts = []
        if card:
            parts.append(card)
        if fresh:
            parts.append("свежее:\n- " + "\n- ".join(fresh))
        return "\n\n".join(parts)

    # ---------- каналы и хронология ----------
    def channel(self, channel_id):
        r = self.db.execute("SELECT summary, last_msg_id, updated FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
        return (r[0] or "", r[1] or 0, r[2] or 0) if r else ("", 0, 0)

    async def summarize(self, channel_id, guild_id, name, lines, last_msg_id):
        old, _, _ = self.channel(channel_id)
        try:
            m = await self.router.complete(
                [{"role": "system", "content": SUMMARY_PROMPT},
                 {"role": "user", "content": f"Канал #{name}. Прошлая сводка:\n{old or '(нет)'}\n\nНовые сообщения:\n" + "\n".join(lines)}],
                role="memory", max_tokens=600, temperature=0, json_mode=True)
            data = json.loads(m.get("content") or "{}")
        except (RateLimited, ValueError) as e:
            log.warning("сводка #%s не обновлена: %s", name, e)
            return False
        summary = str(data.get("summary") or "").strip()[:900]
        episode = str(data.get("episode") or "").strip()[:200]
        if summary:
            self.db.execute("INSERT OR REPLACE INTO channels VALUES (?,?,?,?,?,?)",
                            (channel_id, guild_id, name, summary, last_msg_id, time.time()))
        if episode and guild_id:
            self.db.execute("INSERT INTO episodes (guild_id, channel_id, ts, text) VALUES (?,?,?,?)",
                            (guild_id, channel_id, time.time(), episode))
        self.db.commit()
        log.info("сводка #%s обновлена%s", name, f", эпизод: {episode}" if episode else "")
        return True

    def episodes(self, guild_id, limit=EPISODES_IN_PROMPT):
        rows = self.db.execute("SELECT ts, text FROM episodes WHERE guild_id=? ORDER BY ts DESC LIMIT ?",
                               (guild_id, limit)).fetchall()
        return [f"{datetime.fromtimestamp(ts, TZ):%d.%m %H:%M} — {t}" for ts, t in reversed(rows)]

    # ---------- скрытая репутация: как человек обращается с ботом ----------
    def rep(self, user_id):
        r = self.db.execute("SELECT score, updated FROM rep WHERE user_id=?", (user_id,)).fetchone()
        if not r:
            return 0.0
        days = max(0.0, (time.time() - r[1]) / 86400)
        return r[0] * (1 - REP_DECAY_PER_DAY) ** days

    def rep_apply(self, user_id, attitude, sam=False):
        """attitude −3…3 из строки ОТНОШЕНИЕ. Обычным — не больше REP_HOURLY_CAP очков в час."""
        attitude = max(-3, min(3, int(attitude)))
        if attitude == 0:
            return self.rep(user_id)
        now = time.time()
        r = self.db.execute("SELECT hour_start, hour_delta FROM rep WHERE user_id=?", (user_id,)).fetchone()
        hour_start, hour_delta = (r if r else (now, 0.0))
        if now - hour_start > 3600:
            hour_start, hour_delta = now, 0.0
        delta = attitude * REP_STEP * (SAM_REP_MULT if sam else 1)
        if not sam:
            room = REP_HOURLY_CAP - abs(hour_delta) if (hour_delta >= 0) == (delta >= 0) else REP_HOURLY_CAP
            delta = max(-room, min(room, delta)) if room > 0 else 0
        score = max(-100.0, min(100.0, self.rep(user_id) + delta))
        self.db.execute("INSERT OR REPLACE INTO rep VALUES (?,?,?,?,?)", (user_id, score, now, hour_start, hour_delta + delta))
        self.db.commit()
        return score

    @staticmethod
    def tier(score):
        for edge, name in REP_TIERS:
            if score < edge:
                return name
        return REP_TIERS[-1][1]

    # ---------- база знаний сервера ----------
    def kb(self, guild_id):
        r = self.db.execute("SELECT text, updated FROM kb WHERE guild_id=?", (guild_id,)).fetchone()
        return (r[0], r[1]) if r else ("", 0)

    async def build_kb(self, guild_id, lines):
        """lines — сообщения инфо-каналов с пометками [канал #имя id=…]."""
        text = "\n".join(lines)[-24000:]
        try:
            try:                               # сначала OpenRouter (Groq — под болтовню), не вышло — как раньше
                m = await self.router.complete([{"role": "system", "content": KB_PROMPT}, {"role": "user", "content": text}],
                                               role="bulk", max_tokens=1200, temperature=0)
            except RateLimited:
                m = await self.router.complete([{"role": "system", "content": KB_PROMPT}, {"role": "user", "content": text}],
                                               role="memory", max_tokens=1200, temperature=0)
        except RateLimited as e:
            log.warning("база знаний не собрана: %s", e)
            return False
        kb = (m.get("content") or "").strip()[:2500]
        if len(kb) < 20:
            return False
        self.db.execute("INSERT OR REPLACE INTO kb VALUES (?,?,?)", (guild_id, kb, time.time()))
        self.db.commit()
        log.info("база знаний сервера %s собрана (%d символов) моделью %s", guild_id, len(kb), m.get("_model"))
        return True

    # ---------- что идёт в подсказку ----------
    def prompt_block(self, guild_id, channel_id, people, special=None, with_kb=True, full=None):
        """full — кому давать полную карточку (остальным одна строка: ник + отношение); None — первым четырём."""
        out = []
        for uid, name in list(people.items())[:6 if full is not None else 4]:
            mood = (special or {}).get(uid) or f"Твоё отношение: {self.tier(self.rep(uid))}"
            if full is not None and uid not in full:
                out.append(f"[{name}] {mood}")
                continue
            card, _ = self.card(uid)
            card = card[:450]
            fresh = self.fresh_facts(uid, limit=MAX_FACTS)
            s = f"[{name}] {mood}\n" + (card + "\n" if card else "")
            if fresh:
                s += "Свежее: " + "; ".join(fresh)
            out.append(s.strip())
        mem = "Память о людях в чате:\n" + ("\n\n".join(out) or "(пока ничего)")
        if guild_id and with_kb:
            kb, _ = self.kb(guild_id)
            mem = ("База знаний сервера:\n" + (kb or "(пока не собрана — про сервер отправляй в инфо-каналы и к админам)")
                   + "\n\n" + mem)
            lore, _ = self.card(0, guild_id)
            fresh = self.fresh_facts(0, guild_id, limit=MAX_SERVER_FACTS)
            lore = lore[:450]
            if lore or fresh:
                mem += "\n\nЛор сервера:\n" + (lore + "\n" if lore else "") + ("Свежее: " + "; ".join(fresh) if fresh else "")
            eps = self.episodes(guild_id)
            if eps:
                mem += "\n\nНедавние события сервера:\n" + "\n".join(eps)
        if channel_id:
            summary, _, upd = self.channel(channel_id)
            summary = summary[:450]
            if summary:
                mem += f"\n\nСводка этого канала (на {datetime.fromtimestamp(upd, TZ):%d.%m %H:%M}):\n{summary}"
        return mem

    # ---------- копии и дамп ----------
    def backup(self):
        BACKUP_DIR.mkdir(exist_ok=True)
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        dst = sqlite3.connect(BACKUP_DIR / f"memory-{day}.db")
        self.db.backup(dst)
        dst.close()
        for old in sorted(BACKUP_DIR.glob("memory-*.db"))[:-BACKUP_KEEP]:
            old.unlink()
        (BACKUP_DIR / "memory-latest.md").write_text(self.dump(), encoding="utf-8")
        log.info("копия памяти: backups/memory-%s.db", day)

    def dump(self):
        lines = [f"# Память poskinson — {datetime.now(TZ):%d.%m.%Y %H:%M}", ""]
        for uid, gid, name, card, upd in self.db.execute("SELECT user_id, guild_id, name, card, updated FROM profiles"):
            title = "Лор сервера" if not uid else f"{name or uid}"
            lines += [f"## {title}", f"_обновлено {datetime.fromtimestamp(upd, TZ):%d.%m %H:%M}_", "", card, ""]
        lines.append("## Свежие факты (ещё не в карточках)")
        for uid, gid, name in self.needing_consolidation():
            lines.append(f"- **{'сервер' if not uid else name}**: " + "; ".join(self.fresh_facts(uid, gid)))
        lines += ["", "## Сводки каналов"]
        for name, summary, upd in self.db.execute("SELECT name, summary, updated FROM channels"):
            lines += [f"### #{name} ({datetime.fromtimestamp(upd, TZ):%d.%m %H:%M})", summary, ""]
        lines.append("## База знаний серверов")
        for gid, kb, upd in self.db.execute("SELECT guild_id, text, updated FROM kb"):
            lines += [f"### {gid} ({datetime.fromtimestamp(upd, TZ):%d.%m %H:%M})", kb, ""]
        lines.append("## Отношение (скрытая репутация)")
        for uid, score in self.db.execute("SELECT user_id, score FROM rep ORDER BY score"):
            name = self.db.execute("SELECT name FROM users WHERE user_id=? LIMIT 1", (uid,)).fetchone()
            lines.append(f"- {name[0] if name else uid}: {self.rep(uid):+.0f} ({self.tier(self.rep(uid))})")
        lines.append("")
        lines.append("## Хронология")
        for ts, t in self.db.execute("SELECT ts, text FROM episodes ORDER BY ts"):
            lines.append(f"- {datetime.fromtimestamp(ts, TZ):%d.%m %H:%M} — {t}")
        return "\n".join(lines) + "\n"
