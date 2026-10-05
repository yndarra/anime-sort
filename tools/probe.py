r"""Проверка API перед запуском: какие ключи живы, видят ли модели картинку, есть ли баланс.

Один маленький запрос на каждую пару «провайдер/ключ + модель»:
    картинка (синий фон, красный круг) для этапов с картинками — модель должна её описать;
    короткий текст для текстовых этапов (json_meta).
Итог по каждой паре — Probe.status:
    ok          работает
    no_balance  баланс ключа исчерпан (HTTP 402 или текст про баланс/квоту)
    no_image    модель отвечает, что картинки нет (шлюз её теряет) — для картинок не годится
    no_key      нет файла ключа (secrets\providers\<провайдер>\<ключ>.txt) или он пустой
    error       другое (сеть, 5xx, модель недоступна) — текст в Probe.detail
Ключи не печатаются и не пишутся в логи.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(1, str(PROJECT))

from engine.client import NO_IMAGE_RE, QUOTA_RE  # noqa: E402
from engine.config import KEYS, Report, load_provider  # noqa: E402

TIMEOUT = 120


@dataclass
class Probe:
    route: str           # «beniclo/key1»
    model: str           # имя модели в конфиге
    images: bool         # проверялась картинкой
    status: str = "error"
    detail: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def test_image() -> str:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (256, 256), (20, 60, 200))
    ImageDraw.Draw(image).ellipse((60, 60, 196, 196), fill=(220, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


_IMAGE = None


def read_key(route: str) -> str:
    provider, key = route.split("/", 1)
    path = KEYS / provider / f"{key}.txt"
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[0] if lines else ""


def probe(route: str, model: str, images: bool) -> Probe:
    global _IMAGE
    result = Probe(route, model, images)
    provider = load_provider(route.split("/", 1)[0], Report(), "probe")
    if provider is None:
        result.detail = "нет provider.json"
        return result
    if model not in provider.models:
        result.detail = f"модели {model} нет в providers\\{provider.name}\\provider.json"
        return result
    key = read_key(route)
    if not key:
        result.status, result.detail = "no_key", "нет файла ключа или он пустой"
        return result
    if images and not provider.images:
        result.detail = "провайдер не принимает картинки"
        return result
    if images:
        _IMAGE = _IMAGE or test_image()
        content = [{"type": "text", "text": "Describe this image in one short line: background color, shape and its color."},
                   {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_IMAGE}"}}]
    else:
        content = [{"type": "text", "text": "Reply with the single word OK."}]
    body = {"model": provider.models[model], "max_tokens": 1500, "messages": [{"role": "user", "content": content}]}
    request = urllib.request.Request(provider.endpoint, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                              "User-Agent": "anime-sort"})
    start = time.time()
    try:
        data = json.loads(urllib.request.urlopen(request, timeout=TIMEOUT).read())
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(str(block.get("text", "")) for block in text if isinstance(block, dict))
        result.seconds = time.time() - start
        if images and NO_IMAGE_RE.search(text):
            result.status, result.detail = "no_image", text.strip()[:100]
        elif not str(text).strip():
            result.detail = "пустой ответ"
        else:
            result.status, result.detail = "ok", str(text).strip()[:80]
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", errors="replace").replace(key, "[ключ]")
        result.seconds = time.time() - start
        if exc.code == 402 or QUOTA_RE.search(detail):
            result.status = "no_balance"
        result.detail = f"HTTP {exc.code}: {detail[:160]}"
    except Exception as exc:
        result.seconds = time.time() - start
        result.detail = f"{type(exc).__name__}: {str(exc).replace(key, '[ключ]')[:160]}"
    return result


def probe_all(pairs: list[tuple[str, str, bool]], workers: int = 8) -> dict[tuple[str, str, bool], Probe]:
    """Проверить пары (route, модель, картинкой ли) параллельно."""
    unique = list(dict.fromkeys(pairs))
    with ThreadPoolExecutor(workers) as pool:
        return dict(zip(unique, pool.map(lambda pair: probe(*pair), unique)))


def openrouter_credits(route: str) -> float | None:
    """Остаток кредитов OpenRouter в долларах (None — не узнать)."""
    key = read_key(route)
    if not key:
        return None
    try:
        request = urllib.request.Request("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
        data = json.loads(urllib.request.urlopen(request, timeout=20).read()).get("data", {})
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception:
        return None
