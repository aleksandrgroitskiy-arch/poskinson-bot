"""SQLite: память о людях, напоминания, очки, настройки серверов, прогресс чтения истории."""
import sqlite3
import time

from config import MAX_FACTS, MAX_SERVER_FACTS


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        x = self.db.execute
        x("CREATE TABLE IF NOT EXISTS facts (user_id INTEGER, name TEXT, fact TEXT, ts REAL)")
        cols = [r[1] for r in x("PRAGMA table_info(facts)")]
        if "guild_id" not in cols:
            x("ALTER TABLE facts ADD COLUMN guild_id INTEGER DEFAULT 0")
        x("CREATE TABLE IF NOT EXISTS users (guild_id INTEGER, user_id INTEGER, name TEXT, last_seen REAL,"
          " PRIMARY KEY (guild_id, user_id))")
        x("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, channel_id INTEGER,"
          " user_id INTEGER, due REAL, text TEXT, done INTEGER DEFAULT 0)")
        x("CREATE TABLE IF NOT EXISTS scores (guild_id INTEGER, user_id INTEGER, kind TEXT, value INTEGER,"
          " PRIMARY KEY (guild_id, user_id, kind))")
        x("CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (guild_id, key))")
        x("CREATE TABLE IF NOT EXISTS scanned (channel_id INTEGER PRIMARY KEY, ts REAL)")
        self.db.commit()

    # ---- факты ----
    def facts(self, user_id, limit=MAX_FACTS):
        rows = self.db.execute("SELECT fact FROM facts WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                               (user_id, limit)).fetchall()
        return [r[0] for r in rows]

    def server_facts(self, guild_id, limit=MAX_SERVER_FACTS):
        rows = self.db.execute("SELECT fact FROM facts WHERE user_id=0 AND guild_id=? ORDER BY ts DESC LIMIT ?",
                               (guild_id, limit)).fetchall()
        return [r[0] for r in rows]

    def add_fact(self, user_id, name, fact, guild_id=0):
        fact = fact.strip().strip("-•* ")[:200]
        if len(fact) < 3:
            return False
        q = "SELECT fact FROM facts WHERE user_id=?" + (" AND guild_id=?" if user_id == 0 else "")
        args = (user_id, guild_id) if user_id == 0 else (user_id,)
        if fact.lower() in {r[0].lower() for r in self.db.execute(q, args)}:
            return False
        self.db.execute("INSERT INTO facts (user_id, name, fact, ts, guild_id) VALUES (?,?,?,?,?)",
                        (user_id, name, fact, time.time(), guild_id))
        self.db.commit()
        return True

    def forget(self, user_id):
        n = self.db.execute("DELETE FROM facts WHERE user_id=?", (user_id,)).rowcount
        self.db.commit()
        return n

    # ---- кто есть на сервере ----
    def seen(self, guild_id, user_id, name, ts=None):
        self.db.execute(
            "INSERT INTO users VALUES (?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE SET"
            " name=excluded.name, last_seen=MAX(last_seen, excluded.last_seen)",
            (guild_id, user_id, name, ts or time.time()))
        self.db.commit()

    def active_users(self, guild_id, days=30):
        rows = self.db.execute("SELECT user_id, name FROM users WHERE guild_id=? AND last_seen>?",
                               (guild_id, time.time() - days * 86400)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ---- напоминания ----
    def add_reminder(self, channel_id, user_id, due, text):
        cur = self.db.execute("INSERT INTO reminders (channel_id, user_id, due, text) VALUES (?,?,?,?)",
                              (channel_id, user_id, due, text))
        self.db.commit()
        return cur.lastrowid

    def due_reminders(self):
        return self.db.execute("SELECT * FROM reminders WHERE done=0 AND due<=?", (time.time(),)).fetchall()

    def user_reminders(self, user_id):
        return self.db.execute("SELECT * FROM reminders WHERE done=0 AND user_id=? ORDER BY due",
                               (user_id,)).fetchall()

    def close_reminder(self, rid, user_id=None):
        q, args = "UPDATE reminders SET done=1 WHERE id=?", [rid]
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        n = self.db.execute(q, args).rowcount
        self.db.commit()
        return n

    # ---- очки ----
    def add_score(self, guild_id, user_id, kind, delta=1):
        self.db.execute(
            "INSERT INTO scores VALUES (?,?,?,?) ON CONFLICT(guild_id, user_id, kind) DO UPDATE SET value=value+?",
            (guild_id, user_id, kind, delta, delta))
        self.db.commit()

    def set_score(self, guild_id, user_id, kind, value):
        self.db.execute("INSERT OR REPLACE INTO scores VALUES (?,?,?,?)", (guild_id, user_id, kind, value))
        self.db.commit()

    def score(self, guild_id, user_id, kind):
        r = self.db.execute("SELECT value FROM scores WHERE guild_id=? AND user_id=? AND kind=?",
                            (guild_id, user_id, kind)).fetchone()
        return r[0] if r else 0

    def top(self, guild_id, kind, limit=10):
        return self.db.execute("SELECT user_id, value FROM scores WHERE guild_id=? AND kind=? AND value>0"
                               " ORDER BY value DESC LIMIT ?", (guild_id, kind, limit)).fetchall()

    # ---- настройки сервера ----
    def get(self, guild_id, key, default=None):
        r = self.db.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key)).fetchone()
        return r[0] if r else default

    def put(self, guild_id, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (guild_id, key, str(value)))
        self.db.commit()

    # ---- чтение истории ----
    def is_scanned(self, channel_id):
        return self.db.execute("SELECT 1 FROM scanned WHERE channel_id=?", (channel_id,)).fetchone() is not None

    def mark_scanned(self, channel_id):
        self.db.execute("INSERT OR REPLACE INTO scanned VALUES (?,?)", (channel_id, time.time()))
        self.db.commit()
