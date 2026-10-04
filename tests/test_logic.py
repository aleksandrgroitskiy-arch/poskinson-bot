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

print("запомненные ответы и «не знаю»")
from brain import qkey  # noqa: E402
B = bot.brain
a, b2 = qkey("поскинсон как сделать ферму железа в 1.21"), qkey("пос как сделать фермы железо 1.21")
check("похожие вопросы — один ключ", len(a & b2) / len(a | b2) >= config.ANSWER_MATCH, (a, b2))
calls = []
async def fake_search(q):
    calls.append(q)
    return fake_search.result
async def no_wiki(found, question=""):
    return None
async def no_find(q):
    return None
real_search, real_wiki, real_find = B.search, B.wiki_article, B.wiki_find
B.search, B.wiki_article, B.wiki_find = fake_search, no_wiki, no_find
fake_search.result = "ничего не нашлось"
ctx = {}
note = asyncio.run(B.prepare_facts("пос как сделать ферму фантомов", True, ctx))
check("не нашёл — велим сказать «не знаю»", "ничего не дал" in note and "_grounded" not in ctx)
async def some_wiki(found, question=""):
    return "[статья «Руководство:Ферма железа»] големы спавнятся у жителей"
B.wiki_article = some_wiki
fake_search.result = "Ферма железа\nhttps://example.org\nголемы спавнятся у жителей"
ctx = {}
note = asyncio.run(B.prepare_facts("поскинсон как сделать ферму железа в 1.21", True, ctx))
check("нашёл — найденное в подсказке", "големы спавнятся" in note and ctx.get("_grounded"))
for junk in ("хз, не нашёл толком", "[крестик] не смог сгенерировать ответ, попробуй ещё раз",
             "такого в ванили нет, это мод какой-то наверное"):
    B.remember_answer(junk, ctx)
check("«хз», сбои и «такого нет» не запоминаются", "answer_id" not in ctx)
B.remember_answer("ставишь жителей с кроватями, зомби рядом, големы падают в лаву над воронками", ctx)
check("проверенный ответ запомнен", ctx.get("answer_id"))
n = len(calls)
ctx2 = {}
note2 = asyncio.run(B.prepare_facts("пос как сделать фермы железо 1.21", True, ctx2))
check("похожий вопрос — из памяти, без поиска", len(calls) == n and "уже отвечал" in note2 and ctx2.get("answer_id") == ctx["answer_id"])
ctx3 = {}
asyncio.run(B.prepare_facts("пос какой сейчас курс доллара", False, ctx3))
check("свежее (курс, новости) не запоминается", "_grounded" not in ctx3)
ctx4 = {}
asyncio.run(B.prepare_facts("пос смотрел сагу о винланде?", False, ctx4))
check("не Майнкрафт (болтовня про аниме) не запоминается", "_grounded" not in ctx4)
check("в поиск уходят сущности, без «смотрел»", calls[-1] == "сагу винланде", calls[-1])
check("жалоба стирает ответ", bot.store.drop_answer(ctx["answer_id"]) == 1
      and bot.store.find_answer(a, config.ANSWER_MATCH, 10 ** 9)[0] is None)
check("«неправильно» распознаётся", bool(bot.WRONG_RX.search("пос это неправильно, так уже не работает")))
B.search, B.wiki_article, B.wiki_find = real_search, real_wiki, real_find
from brain import same_word, wiki_words  # noqa: E402
check("слова-сущности без «как скрафтить»", wiki_words("пос как скрафтить маяк в 1.21") == ["маяк"])
check("«да, как его скрафтить» — без сущностей (возьмём из чата)", wiki_words("да, как его скрафтить") == [])
check("окончания: лису/лиса, кирпичи/кирпич", same_word("лису", "лиса") and same_word("кирпичи", "кирпич")
      and not same_word("маяк", "магма"))
seen = {}
async def find_spy(q):
    seen["q"] = q
    return None
B.wiki_find, B.search = find_spy, fake_search
fake_search.result = "ничего не нашлось"
asyncio.run(B.prepare_facts("да, как его скрафтить", True, {"recent": "чё, снова маяк крутишь?"}))
B.wiki_find, B.search = real_find, real_search
check("предмет берётся из последних реплик", "маяк" in seen.get("q", ""), seen)
from brain import wiki_recipes  # noqa: E402
rec = wiki_recipes("=== Крафт ===\n{{Крафт\n|A1=Стекло |B1=Стекло |C1=Стекло\n|A2=Стекло |B2=Звезда Нижнего мира |C2=Стекло\n"
                   "|A3=Обсидиан |B3=Обсидиан |C3=Обсидиан\n|Выход=Маяк\n|тип=Остальное\n}}")
check("рецепт из шаблона вики читается", rec == ["Маяк — ряд 1: Стекло, Стекло, Стекло; ряд 2: Стекло, Звезда Нижнего мира, "
                                                 "Стекло; ряд 3: Обсидиан, Обсидиан, Обсидиан"], rec)
found = ("Железная руда\nhttps://ru.minecraft.wiki/w/Железная_руда\nруда\n\n"
         "Ферма железа\nhttps://ru.minecraft.wiki/w/Руководство:Ферма_железа\nферма")
picked = []
async def fake_get(url, params=None, headers=None):
    if params.get("titles"):
        picked.append(params["titles"])
    return SimpleNamespace(json=lambda: {"query": {"pages": {"1": {"extract": "текст статьи"}}}, "parse": {}})
real_get, B.web.get = B.web.get, fake_get
asyncio.run(B.wiki_article(found, "как сделать ферму железа"))
none = asyncio.run(B.wiki_article("Фантом\nhttps://ru.minecraft.wiki/w/Фантом\n", "как сделать ферму железа"))
B.web.get = real_get
check("статья вики — та, что про вопрос", picked[:1] == ["Руководство:Ферма железа"], picked)
check("статья не про то — не берём", none is None)
from brain import pick_tools  # noqa: E402
names = lambda q: {t["function"]["name"] for t in pick_tools(q)}
check("«как попасть в энд» — с поиском и без паст", "web_search" in names("как попасть в эндер мир?")
      and "send_paste" not in names("как попасть в эндер мир?"))
check("«кинь пасту» — пасты", "send_paste" in names("пос кинь пасту про крипера"))
check("«как попасть в эндер мир» → портал края", wiki_words("верно, молодец, как попасть в эндер мир?") == ["портал", "края"],
      wiki_words("верно, молодец, как попасть в эндер мир?"))
check("незер → нижний мир", wiki_words("что есть в незере") == ["нижний", "мир"], wiki_words("что есть в незере"))
check("правило «не выдумывай» в ядре", "ЗАПРЕТ НА ВЫДУМКУ" in config.PERSONA_CORE)

print(f"\nитого: {ok} ✓, {fail} ✕")
sys.exit(1 if fail else 0)
