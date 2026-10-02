"""Проверка всех моделей и медиа: `.venv/bin/python tests/smoke.py [--fun]`.

Для каждой модели из config.MODELS: отвечает ли, вызывает ли инструменты, держит ли русский
(иероглифы/латиница), время ответа. --fun — ещё одинаковые вопросы на юмор для сравнения.
Зрение — описание тестовой картинки с текстом; рисование; гифки Klipy; распознавание речи.
Живую базу не трогает (память в RAM). Отчёт — в tests/report-<дата>.md.
"""
import asyncio
import io
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from brain import CJK, TOOLS  # noqa: E402
from llm import Router  # noqa: E402
from media import Media  # noqa: E402

FUN = "--fun" in sys.argv
PERSONA = config.PERSONA + "\nСейчас 02.10.2026 16:30 (Москва).\nПамять: (пусто)"
FUN_Q = ["k3tchup_zz: поскинсон как дела", "k3tchup_zz: расскажи анекдот",
         "k3tchup_zz: я опять слил три катки подряд, утешь меня"]
out = []


def say(line=""):
    print(line)
    out.append(line)


def latin_inside(text):
    import re
    return [w for w in re.findall(r"[A-Za-zА-Яа-яЁё]+", text)
            if re.search(r"[А-Яа-яЁё]", w) and re.search(r"[A-Za-z]", w)]


async def one(router, provider, model, messages, **kw):
    t = time.monotonic()
    saved = config.MODELS["chat"][:]
    config.MODELS["chat"][:] = [(provider, model)]
    router.resting.clear()
    try:
        m = await router.complete(messages, role="chat", **kw)
        return m, time.monotonic() - t, None
    except Exception as e:
        return None, time.monotonic() - t, str(e)[:160]
    finally:
        config.MODELS["chat"][:] = saved


async def main():
    router = Router(sqlite3.connect(":memory:"))
    await router.discover()
    media = Media(router)
    say(f"# Проверка poskinson {config.VERSION} — {datetime.now():%d.%m.%Y %H:%M}")
    say(f"Провайдеры с ключами: {', '.join(router.clients)}")
    seen = []
    for role, lst in config.MODELS.items():
        for p, m in lst:
            if (p, m) not in seen and role != "vision":
                seen.append((p, m))

    say("\n## Модели: отвечает / инструменты / русский / время")
    say("| модель | ответ | инструмент | русский | сек |\n|---|---|---|---|---|")
    alive = []
    for p, m in seen:
        if not router.usable(p, m):
            say(f"| {p}:{m} | нет ключа или модели | | | |")
            continue
        msg, dt, err = await one(router, p, m, [{"role": "system", "content": PERSONA},
                                                 {"role": "user", "content": "k3tchup_zz: поскинсон, кто сейчас президент Франции?"}],
                                 tools=TOOLS, max_tokens=300)
        if err:
            say(f"| {p}:{m} | ✕ {err} | | | {dt:.1f} |")
            continue
        calls = [c["function"]["name"] for c in (msg.get("tool_calls") or [])]
        text = msg.get("content") or ""
        bad = ("иероглифы " if CJK.search(text) else "") + (f"смесь: {latin_inside(text)[:3]}" if latin_inside(text) else "")
        say(f"| {p}:{m} | ✓ | {', '.join(calls) or '— (ответил сам)'} | {bad or 'ок'} | {dt:.1f} |")
        alive.append((p, m))

    if FUN:
        say("\n## Юмор: одинаковые вопросы")
        for p, m in [x for x in alive if x in config.MODELS["chat"]]:
            say(f"\n### {p}:{m}")
            for q in FUN_Q:
                msg, dt, err = await one(router, p, m, [{"role": "system", "content": PERSONA}, {"role": "user", "content": q}],
                                         max_tokens=400)
                ans = err or (msg.get("content") or "").strip().replace("\n", " ")
                say(f"- **{q.split(': ', 1)[1]}** ({dt:.1f}с) → {ans[:400]}")

    say("\n## Зрение")
    img = await media.generate("a cat holding a sign with the text HELLO 2026, photo")
    open("/tmp/poskinson-test.jpg", "wb").write(img[0])
    import base64
    data_url = "data:image/jpeg;base64," + base64.b64encode(img[0]).decode()
    for p, m in config.MODELS["vision"]:
        if not router.usable(p, m):
            say(f"- {p}:{m}: нет ключа или модели")
            continue
        saved = config.MODELS["vision"][:]
        config.MODELS["vision"][:] = [(p, m)]
        t = time.monotonic()
        try:
            r = await router.complete([{"role": "user", "content": [
                {"type": "text", "text": "Опиши картинку по-русски в 1–2 предложениях, процитируй текст на ней."},
                {"type": "image_url", "image_url": {"url": data_url}}]}], role="vision", max_tokens=200, temperature=0.2)
            say(f"- {p}:{m} ({time.monotonic() - t:.1f}с): {(r.get('content') or '').strip()[:250]}")
        except Exception as e:
            say(f"- {p}:{m}: ✕ {str(e)[:160]}")
        finally:
            config.MODELS["vision"][:] = saved

    say("\n## Рисование")
    t = time.monotonic()
    try:
        data, name, src = await media.generate("a grumpy goblin gamer at a computer, cartoon")
        say(f"- ✓ {src}, {len(data) // 1024} КБ, {time.monotonic() - t:.1f}с")
    except Exception as e:
        say(f"- ✕ {e}")

    say("\n## Гифки (Klipy)")
    for q in ("facepalm", "crying laughing"):
        url = await media.gif(q)
        say(f"- {q}: {url or '✕ не нашлось / нет ключа'}")

    say("\n## Голосовые (Whisper)")
    p = Path("/tmp/claude-1000/-home-lll/5803cb06-a508-463b-bcfb-ef20301ad261/scratchpad/voice-message.ogg")
    if p.exists():
        try:
            say(f"- ✓ «{await media.transcribe(p.read_bytes(), p.name)}»")
        except Exception as e:
            say(f"- ✕ {e}")
    else:
        say("- пропущено: нет тестовой записи")

    rep = Path(__file__).parent / f"report-{datetime.now():%Y%m%d-%H%M}.md"
    rep.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\nотчёт: {rep}")


asyncio.run(main())
