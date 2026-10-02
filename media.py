"""Медиа: голосовые → текст, картинка → описание, текст → картинка, поиск гифок."""
import base64
import logging
import random
import urllib.parse

import httpx

from config import CF_ACCOUNT, CF_IMAGE_MODEL, CF_TOKEN, KLIPY_KEY, VOICE_MODEL
from llm import RateLimited

log = logging.getLogger("poskinson.media")

DESCRIBE_PROMPT = ("Опиши картинку по-русски для участника чата, который её не видит: что на ней, кто/что изображено, "
                   "настроение, если это мем — в чём шутка, весь текст на картинке дословно. 2–5 предложений, без вступлений.")


class Media:
    def __init__(self, router):
        self.router = router
        self.web = httpx.AsyncClient(timeout=60, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) poskinson"})

    # ---------- голосовые ----------
    async def transcribe(self, data, filename="voice.ogg"):
        provider, model = VOICE_MODEL
        c = await self.router.raw(provider)
        if not c:
            raise RuntimeError("нет ключа для распознавания речи")
        r = await c.post("/audio/transcriptions", files={"file": (filename, data, "audio/ogg")},
                         data={"model": model, "language": "ru", "response_format": "json"})
        if r.status_code != 200:
            raise RuntimeError(f"whisper: HTTP {r.status_code} {r.text[:200]}")
        return (r.json().get("text") or "").strip()

    # ---------- картинка → описание ----------
    async def describe(self, url, hint=""):
        """url — картинка (лучше уменьшенная через media.discordapp.net). Возвращает описание или None."""
        try:
            r = await self.web.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("картинка не скачалась: %r", e)
            return None
        mime = r.headers.get("content-type", "image/png").split(";")[0]
        if not mime.startswith("image/") or len(r.content) > 5 * 1024 * 1024:
            return None
        data_url = f"data:{mime};base64," + base64.b64encode(r.content).decode()
        text = DESCRIBE_PROMPT + (f" Подпись к картинке в чате: «{hint}»." if hint else "")
        try:
            m = await self.router.complete(
                [{"role": "user", "content": [{"type": "text", "text": text},
                                              {"type": "image_url", "image_url": {"url": data_url}}]}],
                role="vision", max_tokens=400, temperature=0.3)
        except RateLimited as e:
            log.warning("никто не смог описать картинку: %s", e)
            return None
        return (m.get("content") or "").strip() or None

    # ---------- текст → картинка ----------
    async def generate(self, prompt):
        """prompt по-английски. Возвращает (bytes, имя файла, источник)."""
        if CF_ACCOUNT and CF_TOKEN:
            try:
                r = await self.web.post(
                    f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/ai/run/{CF_IMAGE_MODEL}",
                    headers={"Authorization": f"Bearer {CF_TOKEN}"}, json={"prompt": prompt, "steps": 6})
                if r.status_code == 200:
                    img = r.json().get("result", {}).get("image")
                    if img:
                        return base64.b64decode(img), "poskinson.jpg", "cloudflare"
                log.warning("cloudflare: HTTP %s %s", r.status_code, r.text[:200])
            except httpx.HTTPError as e:
                log.warning("cloudflare: %r", e)
        seed = random.randint(1, 10 ** 9)
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
               + f"?width=1024&height=1024&nologo=true&seed={seed}")
        r = await self.web.get(url, timeout=120)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content, "poskinson.jpg", "pollinations"
        raise RuntimeError(f"картинка не нарисовалась: HTTP {r.status_code}")

    # ---------- гифки ----------
    async def gif(self, query):
        if not KLIPY_KEY:
            return None
        url = f"https://api.klipy.com/api/v1/{KLIPY_KEY}/gifs/search"
        try:
            r = await self.web.get(url, params={"q": query, "per_page": 12, "page": 1, "locale": "ru",
                                                "customer_id": "poskinson", "content_filter": "off"})
            if r.status_code != 200:
                log.warning("klipy: HTTP %s %s", r.status_code, r.text[:200])
                return None
            urls = []
            _collect_urls(r.json(), urls)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("klipy: %r", e)
            return None
        gifs = [u for u in urls if ".gif" in u.lower()] or [u for u in urls if ".mp4" in u.lower()]
        return random.choice(gifs[:8]) if gifs else None


def _collect_urls(node, out):
    """Klipy отдаёт много размеров; собираем все ссылки на медиа, порядок — как в выдаче."""
    if isinstance(node, dict):
        hd = node.get("file", {}).get("hd") if isinstance(node.get("file"), dict) else None
        if isinstance(hd, dict) and isinstance(hd.get("gif"), dict) and hd["gif"].get("url"):
            out.append(hd["gif"]["url"])
            return
        for v in node.values():
            _collect_urls(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_urls(v, out)
    elif isinstance(node, str) and node.startswith("http") and any(x in node.lower() for x in (".gif", ".mp4")):
        out.append(node)
