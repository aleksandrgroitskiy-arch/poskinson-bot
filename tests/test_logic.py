"""Логика без нейросетей и Discord: `.venv/bin/python tests/test_logic.py`. Живую базу не трогает."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

config.DB_PATH = ":memory:"
import bot  # noqa: E402
from brain import public_url  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✕ {name} {extra}")


def user(uid, name, display=None):
    return SimpleNamespace(id=uid, name=name, display_name=display or name, global_name=None, nick=None, bot=False)


print("служебные строки и факты")
vasya, petya = user(1, "vasya"), user(2, "petya")
txt = "ну ты и лох\nЗАПОМНИ: vasya | ник в майне Vasyan228\nЗАПОМНИ: petya | ворует у всех\nзапомни: сервер | мем про крипера\nОТНОШЕНИЕ: -2\n---"
out = bot.save_facts(txt, {1: "vasya", 2: "petya"}, 5, author=vasya)
check("служебное вырезано", out == "ну ты и лох", repr(out))
check("факт об авторе записан", "ник в майне Vasyan228" in bot.store.facts(1))
check("слух о другом НЕ записан", not bot.store.facts(2))
check("факт о сервере записан", any("крипера" in f for f in bot.memory.fresh_facts(0, 5)))
check("отношение учтено (отрицательное)", bot.memory.rep(1) < 0, bot.memory.rep(1))

print("репутация")
for _ in range(20):
    bot.memory.rep_apply(10, 3)
check("лесть за час упирается в потолок", bot.memory.rep(10) <= config.REP_HOURLY_CAP + 0.01, bot.memory.rep(10))
check("новый человек — нейтрально", bot.memory.tier(bot.memory.rep(999)) == "нейтральное")
check("шкала: -50 враждебное", bot.memory.tier(-50) == "враждебное")
check("шкала: 80 любишь", bot.memory.tier(80) == "любишь его")

print("sam_takov")
for n in ("sam_takov", "Sam Takoy", "СамТаков", "сам такой", "poskinson"):
    bot.store.put(0, "sam_id", "")
    check(f"узнаёт «{n}»", bot.is_sam(user(50, n)))
check("не путает с обычным ником", not bot.is_sam(user(51, "samurai_tank")))
bot.store.put(0, "sam_id", 50)
flips = sum("внезапная смена" in bot.sam_mood(50) for _ in range(200))
check("в настроении есть и «папочка», и «сынок»", "папочка" in bot.sam_mood(50) and "сынок" in bot.sam_mood(50))
check("внезапные перепады бывают, но не всегда", 20 < flips < 120, flips)

print("очеловечивание текста")
check("точка и заглавная", bot.humanize("Ну и Лох.") == "ну и Лох")
check("ёлочки и тире", bot.humanize("это «круто» — да") == "это круто - да")
check("код не трогает", bot.humanize("```py\nprint(1).\n```") == "```py\nprint(1).\n```")

print("защита ссылок (SSRF)")


async def ssrf():
    for u in ("http://localhost:8080", "http://127.0.0.1", "http://192.168.0.1/admin", "http://169.254.169.254/latest",
              "http://10.0.0.5", "file:///etc/passwd", "http://[::1]/", "https://example.com:22"):
        check(f"блок {u}", not await public_url(u))
    check("пускает https://ru.minecraft.wiki", await public_url("https://ru.minecraft.wiki/w/Крафт"))
asyncio.run(ssrf())

print("антифлуд и лимиты")
res = [bot.flooding(77) for _ in range(config.FLOOD_PER_MIN + 2)]
check(f"после {config.FLOOD_PER_MIN} обращений в минуту — игнор", res[:config.FLOOD_PER_MIN] == [False] * config.FLOOD_PER_MIN and res[-1])
imgs = [bot.image_ok(88) for _ in range(config.IMAGES_PER_USER_HOUR + 1)]
check(f"картинок не больше {config.IMAGES_PER_USER_HOUR} в час", imgs[-1] is False and all(imgs[:-1]))
check("помощь новичку распознаётся", bool(bot.HELP_RX.search("а как зайти к вам на сервер?")))
check("айпи распознаётся", bool(bot.HELP_RX.search("скиньте айпи плз")))

print("18+ картинки")
from media import forbidden  # noqa: E402
check("откровенное в обычном канале — нельзя", forbidden("naked woman", False))
check("откровенное в 18+ канале — можно", not forbidden("naked woman", True))
check("с несовершеннолетними — нельзя даже в 18+", forbidden("nude teen", True) and forbidden("sexy schoolgirl", True))
check("жесть и «ребёнок в майне» — можно везде", not forbidden("bloody zombie", False) and not forbidden("a kid playing minecraft", False))

for pre in ("[Ответ poskinson] ", "[ответь на это сообщение] ", "poskinson: ", "ты (poskinson): "):
    out = bot.save_facts(pre + "ну привет", {}, 5)
    check(f"префикс «{pre.strip()}» вырезан", out == "ну привет", repr(out))
check("обычное «[» в ответе не трогается", bot.save_facts("[мем] смешно", {}, 5) == "[мем] смешно")

print("промпт v2: ядро + блоки")
config.PROMPT_V2 = True
hi = bot.persona("пос привет")
check("на «привет» — только ядро", hi == config.PERSONA_CORE)
check("ядро заметно короче старого", len(config.PERSONA_CORE) < len(config.PERSONA) * 0.65,
      f"{len(config.PERSONA_CORE)} vs {len(config.PERSONA)}")
check("крафт — блок Майнкрафта", config.BLOCK_MC in bot.persona("пос как скрафтить маяк"))
check("айпи — блок сервера", config.BLOCK_SERVER in bot.persona("скиньте айпи сервера"))
check("аниме — блок фактов", config.BLOCK_FACTS in bot.persona("пос смотрел аниме сага о винланде?"))
check("напомни — блок напоминаний", config.BLOCK_REMIND in bot.persona("пос напомни через 10 минут"))
check("в ядре нет служебных строк", "ЗАПОМНИ" not in config.PERSONA_CORE)
config.PROMPT_V2 = False
check("флаг выключен — старый PERSONA", bot.persona("пос привет") == config.PERSONA)
config.PROMPT_V2 = True

print("карточки: полные только у нужных")
bot.memory.add_fact(31, "kolya", "строит замок из кварца", 5)
bot.memory.add_fact(32, "tolya", "держит ферму свиней", 5)
blk = bot.memory.prompt_block(5, None, {31: "kolya", 32: "tolya"}, full={31})
check("автору — полная", "замок из кварца" in blk)
check("остальным — только отношение", "ферму свиней" not in blk and "[tolya] Твоё отношение" in blk)

print("пакетный разбор памяти")
class FakeRouter:
    def __init__(self, content):
        self.content = content
    async def complete(self, *a, **kw):
        self.kw = kw
        return {"content": self.content}
fake = FakeRouter('{"facts": [{"who": "sasha", "fact": "учится на программиста в колледже"},'
                  ' {"who": "masha", "fact": "ворует алмазы у всех подряд"},'
                  ' {"who": "сервер", "fact": "каждую пятницу строят общую стену"}],'
                  ' "attitude": [{"who": "sasha", "score": 2}, {"who": "dima", "score": -3}]}')
real_router, bot.router = bot.router, fake
buf = {"guild": 5, "first_call": 1, "items": [
    {"id": 1, "uid": 41, "name": "sasha", "text": "пос, я учусь на программиста", "to_bot": True, "sam": False},
    {"id": 0, "uid": 0, "name": None, "text": "о, и как оно", "to_bot": False, "sam": False},
    {"id": 2, "uid": 42, "name": "dima", "text": "лол", "to_bot": False, "sam": False}]}
asyncio.run(bot.extract(77, buf))
bot.router = real_router
check("разбор — роль extract, temperature 0", fake.kw.get("role") == "extract" and fake.kw.get("temperature") == 0)
check("факт о том, кто писал, записан", any("программиста" in f for f in bot.store.facts(41)))
check("о том, кого не было в чате, — нет", not bot.store.facts(43) and not any("алмазы" in f for f in bot.memory.fresh_facts(0, 5)))
check("факт о сервере записан", any("стену" in f for f in bot.memory.fresh_facts(0, 5)))
check("отношение — тому, кто писал боту", bot.memory.rep(41) > 0, bot.memory.rep(41))
check("не писавшему боту — не меняется", bot.memory.rep(42) == 0, bot.memory.rep(42))
check("буфер очищен", buf["items"] == [] and buf["first_call"] == 0)

print(f"\nитого: {ok} ✓, {fail} ✕")
sys.exit(1 if fail else 0)
