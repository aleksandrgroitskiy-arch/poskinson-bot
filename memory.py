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
import sqlite3
import time
from datetime import datetime

from config import (BACKUP_DIR, BACKUP_KEEP, CONSOLIDATE_AFTER, EPISODES_IN_PROMPT, MAX_FACTS, MAX_SERVER_FACTS, TZ)
from llm import RateLimited

log = logging.getLogger("poskinson.memory")

CARD_PROMPT = """Ты ведёшь досье на участника Discord-сервера друзей для бота-тролля, который общается с ним и подкалывает его.
Собери из старой карточки и новых фактов НОВУЮ карточку. Правила:
- Только то, что есть в фактах; если факты противоречат — верь более новым (они ниже в списке).
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

SERVER_CARD_PROMPT = """Ты ведёшь «лор» Discord-сервера друзей для бота-тролля.
Собери из старого лора и новых фактов НОВЫЙ лор: внутренние мемы и шутки, традиции, прозвища, кто есть кто, общие истории.
Если факты противоречат — верь более новым. Объединяй повторы, выкидывай мусор. Строки вида «- …», не больше 900 символов.
Только лор, без вступлений."""

SUMMARY_PROMPT = """Ниже прошлая сводка Discord-канала и новые сообщения после неё.
Ответь JSON: {"summary": "...", "episode": "..."}
summary — обновлённая сводка «что сейчас и недавно происходит в канале»: темы, кто что говорил, договорённости, незакрытые вопросы, споры. До 600 символов, по-русски.
episode — одна строка для хронологии сервера о самом заметном в НОВЫХ сообщениях (до 140 символов), или "" если ничего заметного."""


class Memory:
    def __init__(self, store, router):
        self.store = store
        self.router = router
        db = self.db = store.db
        db.execute("CREATE TABLE IF NOT EXISTS profiles (user_id INTEGER, guild_id INTEGER, name TEXT, card TEXT,"
                   " updated REAL, PRIMARY KEY (user_id, guild_id))")
        db.execute("CREATE TABLE IF NOT EXISTS channels (channel_id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT,"
                   " summary TEXT, last_msg_id INTEGER, updated REAL)")
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
                                           role="light", max_tokens=700, temperature=0.2)
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
                role="light", max_tokens=600, temperature=0.2, json_mode=True)
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

    # ---------- что идёт в подсказку ----------
    def prompt_block(self, guild_id, channel_id, people):
        out = []
        for uid, name in people.items():
            card, _ = self.card(uid)
            fresh = self.fresh_facts(uid, limit=MAX_FACTS)
            if card or fresh:
                s = f"[{name}]\n" + (card + "\n" if card else "")
                if fresh:
                    s += "Свежее: " + "; ".join(fresh)
                out.append(s.strip())
        mem = "Память о людях в чате:\n" + ("\n\n".join(out) or "(пока ничего)")
        if guild_id:
            lore, _ = self.card(0, guild_id)
            fresh = self.fresh_facts(0, guild_id, limit=MAX_SERVER_FACTS)
            if lore or fresh:
                mem += "\n\nЛор сервера:\n" + (lore + "\n" if lore else "") + ("Свежее: " + "; ".join(fresh) if fresh else "")
            eps = self.episodes(guild_id)
            if eps:
                mem += "\n\nНедавние события сервера:\n" + "\n".join(eps)
        if channel_id:
            summary, _, upd = self.channel(channel_id)
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
        lines.append("## Хронология")
        for ts, t in self.db.execute("SELECT ts, text FROM episodes ORDER BY ts"):
            lines.append(f"- {datetime.fromtimestamp(ts, TZ):%d.%m %H:%M} — {t}")
        return "\n".join(lines) + "\n"
