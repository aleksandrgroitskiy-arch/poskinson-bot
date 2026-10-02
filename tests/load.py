"""Нагрузка: обращение к боту каждые N секунд (по умолчанию 10), как в живом чате.
`.venv/bin/python tests/load.py [интервал] [сколько]`. Живую базу не трогает.
Считает: сколько ответов, задержку, какие модели, сколько токенов, отказы."""
import asyncio
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

config.DB_PATH = ":memory:"
import bot  # noqa: E402

INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 10
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 18
G = 777
QUESTIONS = [
    "поскинсон как дела", "поскинсон ты где пропадал", "поскинсон какой крафт у маяка",
    "поскинсон а как зайти к вам на сервер?", "поскинсон скажи анекдот", "поскинсон ты кто вообще",
    "поскинсон как сделать ферму железа", "поскинсон я опять умер в лаве со всем лутом",
    "поскинсон напомни через 2 часа зайти на ивент", "поскинсон какая версия на сервере?",
    "поскинсон чё лучше кирка на удачу или шёлковое касание", "поскинсон го в майн",
    "поскинсон меня зовут Лёха, я строю замок на спавне", "поскинсон ты бот или человек",
    "поскинсон как приручить лису", "поскинсон скинь правила", "поскинсон спасибо бро", "поскинсон ты душнила",
]
HISTORY = [("Вася", "кто-нибудь видел где деревня с библиотекарем рядом со спавном"),
           ("Петя", "да у реки была, но там криперы всё разнесли"),
           ("Лёха", "я вчера весь вечер копал незерит и нашёл 2 обломка, это норм вообще?"),
           ("Вася", "норм, у меня за неделю 5"),
           ("Петя", "го сегодня в энд, надо элитры"),
           ("poskinson", "го, только не сдохните опять как в прошлый раз"),
           ("Лёха", "кто админ на сервере? у меня приват слетел"),
           ("Вася", "пиши в тикеты"),
           ("Петя", "ахах опять приват слетел"),
           ("Лёха", "ну блин")]


async def one(i, q, res):
    uid = 100 + i % 5
    name = ["Лёха", "Вася", "Петя", "Миша", "Даня"][i % 5]
    u = SimpleNamespace(id=uid, name=name, display_name=name, global_name=None, nick=None, bot=False)
    people = {uid: name, 201: "Вася", 202: "Петя"}
    with_kb = bool(bot.HELP_RX.search(q))
    system = (config.PERSONA + "\n" + bot.now_line() + " Канал #общий.\n"
              + bot.memory.prompt_block(G, 1, people, None, with_kb=with_kb))
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": "[недавний чат, только для контекста — на него не отвечай]\n"
             + "\n".join(f"{'ты (poskinson)' if w == 'poskinson' else w}: {t}" for w, t in HISTORY)},
            {"role": "assistant", "content": "ок, понял контекст"},
            {"role": "user", "content": f"[ответь на это сообщение] {name}: {q}"}]
    ctx = {"channel_id": 1, "user_id": uid, "guild_id": G, "kb": bot.memory.kb(G)[0], "text": q, "image_ok": lambda _: True}
    t = time.monotonic()
    try:
        raw = await bot.brain.chat(msgs, ctx)
        ans = bot.humanize(bot.save_facts(raw, people, G, author=u))
        ok = bool(ans)
    except Exception as e:
        ans, ok = f"✕ {type(e).__name__}: {str(e)[:80]}", False
    dt = time.monotonic() - t
    res.append((i, ok, dt, ctx.get("model"), q, ans))
    print(f"[{i:2}] {'✓' if ok else '✕'} {dt:5.1f}с {str(ctx.get('model')):32} {q[10:45]:35} → {ans.replace(chr(10), ' / ')[:90]}")


async def main():
    await bot.router.discover()
    # база знаний и карточки, как на живом сервере
    bot.memory.db.execute("INSERT OR REPLACE INTO kb VALUES (?,?,?)", (G, (
        "Сервер: ПоскинКрафт, выживание с приватами, Java 1.21.4, любой лаунчер.\nКак зайти: заявка в <#104> по шаблону → "
        "одобрение → IP в личку.\nПравила: без читов/x-ray, не грифери, без оскорблений родных, без спама, без лаг-машин. "
        "Полные — <#103>.\nКаналы: <#102> инфо, <#103> правила, <#104> заявки, <#105> анонсы."), time.time()))
    for uid, card in ((100, "Кто: Лёха\nИгры: копает незерит, строит замок"), (201, "Кто: Вася\nИгры: ищет деревни"),
                      (202, "Кто: Петя\nКосяки: вечно слетает приват")):
        bot.memory.db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?)", (uid, 0, "", card, time.time()))
    bot.memory.db.commit()
    print(f"нагрузка: {COUNT} обращений, каждые {INTERVAL:.0f} с\n")
    res, tasks = [], []
    start = time.monotonic()
    for i in range(COUNT):
        tasks.append(asyncio.create_task(one(i, QUESTIONS[i % len(QUESTIONS)], res)))
        await asyncio.sleep(INTERVAL)
    await asyncio.gather(*tasks)
    ok = [r for r in res if r[1]]
    lat = sorted(r[2] for r in ok)
    models = {}
    for r in ok:
        models[r[3]] = models.get(r[3], 0) + 1
    print(f"\nитого за {time.monotonic() - start:.0f} с: ответов {len(ok)}/{COUNT}")
    if lat:
        print(f"задержка: медиана {lat[len(lat) // 2]:.1f} с, худшая {lat[-1]:.1f} с")
    print("модели:", ", ".join(f"{m} ×{n}" for m, n in sorted(models.items(), key=lambda x: -x[1])))
    for p, m, req, err, ti, to in bot.router.report():
        print(f"  {p}:{m}: запросов {req}, отказов {err}, токенов вход {ti} (≈{ti // max(1, req - err)} на запрос), выход {to}")


random.seed(1)
asyncio.run(main())
