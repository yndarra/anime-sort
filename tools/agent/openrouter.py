r"""Клиент агентов: только OpenRouter и только свой ключ (configs\agent.json → secrets\agent\openrouter.txt).
Конвейеры этот ключ не видят, агенты не трогают ключи конвейеров (secrets\providers). Ключ не печатается.

Agent(config).ask(system, user) → разобранный JSON из ответа модели; Agent.spent — потрачено долларов
(OpenRouter отдаёт стоимость запроса в usage.cost). credits(key) — остаток на счёте OpenRouter.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import console
from console import UserError

PROJECT = Path(__file__).resolve().parents[2]
CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def load_config() -> dict:
    try:
        return json.loads((PROJECT / "configs" / "agent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UserError(f"configs\\agent.json не прочитан: {exc}", "проверьте файл (образец — в README)")


def credits(key: str) -> float | None:
    """Остаток на счёте OpenRouter в долларах (None — не удалось узнать)."""
    request = urllib.request.Request(CREDITS_URL, headers={"Authorization": f"Bearer {key}", "User-Agent": "anime-sort"})
    try:
        data = json.loads(urllib.request.urlopen(request, timeout=30).read()).get("data") or {}
        return float(data.get("total_credits", 0)) - float(data.get("total_usage", 0))
    except Exception:
        return None


class Agent:
    def __init__(self, config: dict | None = None):
        config = config or load_config()
        self.endpoint = config["endpoint"]
        self.model = config["model"]
        key_file = PROJECT / config["key_file"]
        try:
            self.key = key_file.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            raise UserError(f"нет ключа агента: {key_file}",
                            "положите ключ OpenRouter (одной строкой) в этот файл — он только для агентов, конвейеры его не используют")
        self.spent = 0.0

    def balance(self) -> float | None:
        return credits(self.key)

    def ask(self, system: str, user: str, empty: dict | None = None, max_tokens: int = 8000) -> dict:
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                data = json.loads(urllib.request.urlopen(request, timeout=300).read())
                usage = data.get("usage") or {}
                self.spent += float(usage.get("cost") or 0)
                text = data["choices"][0]["message"]["content"] or ""
                match = re.search(r"\{.*\}", text, re.S)
                return json.loads(match.group(0)) if match else dict(empty or {})
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode("utf-8", errors="replace")
                if exc.code == 402 or "credit" in detail.casefold():
                    raise UserError("на ключе агента OpenRouter нет баланса (402)",
                                    "пополните баланс на openrouter.ai/settings/credits или пропустите этот шаг")
                if exc.code == 401:
                    raise UserError("ключ агента OpenRouter не принят (401)", "проверьте secrets\\agent\\openrouter.txt")
                console.warn(f"OpenRouter: HTTP {exc.code} {detail[:120]} — повтор через 10 с")
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                console.warn(f"OpenRouter: {type(exc).__name__}: {str(exc)[:120]} — повтор через 10 с")
            time.sleep(10)
        raise UserError("OpenRouter не ответил трижды подряд", "проверьте интернет и запустите снова")
