r"""Запросы к моделям через провайдера из providers\<имя>\provider.json.

Один клиент на этап: модель, порог и список API (провайдер/ключ, по порядку) — из описания этапа (StageSpec).
Работает первый доступный API списка; исчерпан баланс, N сбоев подряд или сбои дольше M минут — API «засыпает»,
файл уходит в следующий; проснувшийся API проверяется одним запросом. Все спят — этап ждёт (см. configs\README.txt).
Ответы кэшируются в cache\ набора по модели (не по API), поэтому перезапуск и переключение не тратят запросы.
Ключ читается из файла и никуда не пишется (в текст ошибок он не попадает: заменяется на [redacted]).
"""
from __future__ import annotations

import base64
import io
import collections
import json
import mimetypes
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from engine.ani_catalog import (POLICY_VERSION, Catalog, has_meaningful_metadata, metadata_catalog_identity,
                                metadata_supports)
from engine.config import ApiRoute, StageSpec
from engine.core import (QuotaExhaustedError, check_requests, clear_skip, read_json, atomic_json, clean_series, is_multi_title, is_unknown_title,
                         normalized_confidence)

RATE_LIMIT_RETRIES = 15
NETWORK_RETRIES = 6
PARSE_RETRIES = 2

# Что отправлять модели как есть. Всё остальное (GIF, HEIC, AVIF, слишком большие файлы)
# перекодируется в JPEG: мост обрывал соединение на огромных запросах (GIF 26 МБ -> "SSL EOF"),
# а часть форматов API не принимает вовсе.
SEND_AS_IS = {"image/jpeg", "image/png", "image/webp"}
MAX_SEND_BYTES = 4 * 1024 * 1024
MAX_SEND_SIDE = 2048

IMAGE_PROMPT = (
    "You are an anime image cataloger. Identify the most likely visible canonical anime/game "
    "character(s) and title. Inspect the image first and use embedded metadata as a clue, not proof. "
    "Return ONLY JSON: {\"known\":true/false,\"confidence\":0.0,"
    "\"characters\":[\"canonical name\"],\"series\":\"canonical title\","
    "\"reason\":\"short explanation\"}. Confidence must be 0 to 1."
)
METADATA_PROMPT = (
    "You are an anime metadata cataloger. Infer the most likely title or character from the supplied "
    "embedded metadata. Return ONLY JSON: "
    '{"known":true/false,"confidence":0.0,"characters":["name"],"series":"title",'
    '"reason":"short explanation"}. It is enough to identify either a title or a character; '
    "do not require both. Prefer a canonical title/character from the ranked catalog below. "
    "Do not treat usernames, credits, emojis, numbers, or generic promotional words as identity."
)


# ---------------------------------------------------------------- картинка и metadata

def image_payload(path: Path) -> tuple[str, bytes]:
    """(mime, байты) для отправки модели; при необходимости — первый кадр, уменьшенный JPEG."""
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = path.read_bytes()
    try:
        from PIL import Image

        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass
        with Image.open(io.BytesIO(data)) as image:
            too_big = len(data) > MAX_SEND_BYTES or max(image.size) > MAX_SEND_SIDE
            if mime in SEND_AS_IS and not too_big and not getattr(image, "is_animated", False):
                return mime, data
            image.seek(0)
            frame = image.convert("RGB")
            frame.thumbnail((MAX_SEND_SIDE, MAX_SEND_SIDE))
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=90)
            return "image/jpeg", buffer.getvalue()
    except Exception:
        return mime, data


EXIF_FIELDS = {40091: "XPTitle", 40092: "XPComment", 270: "ImageDescription"}


def metadata_identity_key(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
    return key or str(value).casefold().strip()


def embedded_metadata_values(path: Path) -> list[dict[str, str]]:
    """Текст пина, встроенный в сам файл (PNG-поля Title/Description/Comment, EXIF)."""
    values: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        from PIL import Image

        with Image.open(path) as image:
            raw = [(key, image.info.get(key)) for key in ("Title", "Description", "Comment")]
            exif = image.getexif()
            raw += [(field, exif.get(tag)) for tag, field in EXIF_FIELDS.items()]
            for field, value in raw:
                if isinstance(value, bytes):
                    value = value.decode("utf-16le", errors="ignore").replace("\x00", "")
                if isinstance(value, str) and value.strip():
                    value = value.strip()
                    identity = metadata_identity_key(value)
                    if identity not in seen:
                        values.append({"field": field, "value": value})
                        seen.add(identity)
    except Exception:
        pass
    return values


def metadata_info(path: Path) -> tuple[str, int, int]:
    values = embedded_metadata_values(path)
    metadata = "\n".join(f"{item['field']}: {item['value']}" for item in values)
    return metadata, len(values), sum(len(item["value"]) for item in values)


# ---------------------------------------------------------------- разбор ответа

def parse_json_response(raw) -> dict:
    if isinstance(raw, list):
        raw = "".join(str(block.get("text", "")) for block in raw if isinstance(block, dict))
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def drop_mapping_answer(obj: dict) -> dict:
    """Модель иногда отвечает словарём {"персонаж": "тайтл", ...} вместо списка — это не определение."""
    if not isinstance(obj, dict):
        raise ValueError("model answer is not a JSON object")
    fields = (obj.get("characters"), obj.get("series"), obj.get("title"))
    if any(isinstance(value, dict) for value in fields):
        return {**obj, "known": False, "characters": [], "series": "", "title": "",
                "reason": "model returned a character->title mapping instead of an answer"}
    return obj


def make_envelope(obj: dict, metadata: str, result: dict | None, resolved: bool, metadata_count: int, metadata_chars: int) -> dict:
    chars = [str(value) for value in (obj.get("characters") or []) if str(value).strip()]
    series = clean_series(obj.get("series") or obj.get("title") or "")
    return {
        "confidence": normalized_confidence(obj.get("confidence", 0.0)),
        "known": bool(obj.get("known")) and bool(chars or series),
        "characters": chars,
        "series": series,
        "reason": str(obj.get("reason") or ""),
        "metadata": metadata,
        "metadata_sent": bool(metadata),
        "metadata_count": metadata_count,
        "metadata_chars": metadata_chars,
        "resolved": resolved,
        "result": result,
    }


def raw_image_result(envelope: dict, model: str) -> dict | None:
    """Кандидат в определение по ответу на картинку (без порога): нужен и тайтл, и персонаж."""
    chars = [str(value) for value in (envelope.get("characters") or []) if str(value).strip()]
    series = clean_series(envelope.get("series") or "")
    if not (envelope.get("known") and chars and series) or is_unknown_title(series) or is_multi_title(series):
        return None
    return {
        "kind": "character" if len(chars) == 1 else "series_group",
        "series": series,
        "character": chars[0] if len(chars) == 1 else " / ".join(chars),
        "tags": {},
        "model": model,
        "confidence": normalized_confidence(envelope.get("confidence", 0.0)),
        "reason": str(envelope.get("reason") or ""),
    }


def base_character_name(tag: str) -> str:
    match = re.match(r"^(?P<name>.+)_\((?P<series>[^)]+)\)$", tag)
    return match.group("name") if match else tag


def resolve_ai_name(character: str, series: str, by_tag: dict):
    """Имя персонажа от модели -> канонический тег из индекса персонажей (если совпадение единственное)."""
    norm = re.sub(r"[^a-z0-9]", "", character.casefold())
    series_norm = re.sub(r"[^a-z0-9]", "", series.casefold())
    candidates = []
    for tag, item in by_tag.items():
        raw_name = base_character_name(tag)
        raw_series = str(item.get("copyright") or "")
        if re.sub(r"[^a-z0-9]", "", raw_name.casefold()) == norm:
            if not series_norm or series_norm in re.sub(r"[^a-z0-9]", "", raw_series.casefold()):
                candidates.append((tag, raw_series or series))
    if len(candidates) != 1:
        return None
    tag, canonical_series = candidates[0]
    return {"kind": "character", "series": canonical_series, "character": base_character_name(tag), "tags": {tag: 1.0}}


def json_evidence(metadata: str, envelope: dict, catalog: Catalog | None) -> tuple[dict | None, dict]:
    """JSON-этап: определение принимается, только если его подтверждает сам текст metadata (и каталог ani.txt)."""
    confidence = normalized_confidence(envelope.get("confidence", 0.0))
    model_series = str(envelope.get("series") or "").strip()
    model_characters = [str(value).strip() for value in (envelope.get("characters") or []) if str(value).strip()]
    evidence_hint = {}
    if catalog and not (model_series or model_characters):
        metadata_titles, metadata_characters = metadata_catalog_identity(metadata, catalog)
        if len(metadata_titles) == 1:
            model_series = metadata_titles[0]
        if metadata_characters:
            model_characters = metadata_characters[:3]
        evidence_hint = {"metadata_titles": metadata_titles, "metadata_characters": metadata_characters}
    evidence = {"policy": POLICY_VERSION, "confidence": confidence, "match_type": "none", "metadata_match": "",
                "candidate_count": 0, "ambiguous": False, "reason": "", **evidence_hint}
    base = dict(envelope.get("result") or {})
    if not (model_series or model_characters) or not catalog:
        evidence["reason"] = "no identity or catalog unavailable"
        return None, evidence
    if is_unknown_title(model_series):
        evidence["reason"] = "unknown title is unresolved"
        return None, evidence
    if model_series and model_characters:
        title_candidates = catalog.matching_titles(model_series)
        character_candidates = [entry for character in model_characters for entry in catalog.matching_characters(character)]
        compatible = [entry for entry in character_candidates if any(entry.title == title.title for title in title_candidates)] if title_candidates else []
        if len(compatible) == 1:
            supported, matched = metadata_supports(metadata, [model_series, *model_characters, compatible[0].title, compatible[0].character])
            evidence.update({"match_type": "full", "candidate_count": len(compatible), "metadata_match": matched})
            if supported:
                return {**base, "series": compatible[0].title, "character": compatible[0].character, "kind": "character"}, \
                    {**evidence, "reason": "full catalog match with metadata evidence"}
        supported, matched = metadata_supports(metadata, [model_series, *model_characters])
        evidence.update({"metadata_match": matched, "candidate_count": len(compatible)})
        if supported:
            canonical_title = title_candidates[0].title if len(title_candidates) == 1 else model_series
            return {**base, "series": canonical_title, "character": model_characters[0], "kind": "character"}, \
                {**evidence, "match_type": "full", "reason": "explicit model title and character supported by metadata"}
        evidence["reason"] = "title and character have no metadata evidence"
        return None, evidence
    if model_characters:
        candidates = [entry for character in model_characters for entry in catalog.matching_characters(character)]
        evidence.update({"match_type": "character_only", "candidate_count": len(candidates)})
        supported, matched = metadata_supports(metadata, model_characters)
        evidence["metadata_match"] = matched
        by_title = {}
        for entry in candidates:
            by_title.setdefault(entry.title, entry)
        leaders = sorted(by_title.values(), key=lambda entry: (entry.title_rank, entry.title.casefold()))
        if supported and leaders:
            best = [entry for entry in leaders if entry.title_rank == leaders[0].title_rank]
            if len(best) == 1:
                entry = best[0]
                evidence["selected_title_rank"] = entry.title_rank
                return {**base, "kind": "character", "series": entry.title, "character": entry.character}, \
                    {**evidence, "reason": "unique highest-ranked character match with metadata evidence"}
        evidence["ambiguous"] = len(leaders) > 1
        evidence["reason"] = "character match is missing, ambiguous, or lacks metadata evidence"
        return None, evidence
    candidates = catalog.matching_titles(model_series)
    supported, matched = metadata_supports(metadata, [model_series] + [entry.title for entry in candidates])
    evidence.update({"match_type": "title_only", "candidate_count": len(candidates), "metadata_match": matched})
    if supported and has_meaningful_metadata(metadata) and len({entry.title for entry in candidates}) == 1:
        return {**base, "kind": "title_only", "series": candidates[0].title, "character": ""}, \
            {**evidence, "reason": "unique title match with meaningful metadata evidence"}
    if not candidates and has_meaningful_metadata(metadata):
        evidence.update({"match_type": "title_only_uncataloged", "reason": "confident model title supported by meaningful metadata"})
        return {**base, "kind": "title_only", "series": model_series, "character": ""}, evidence
    evidence["ambiguous"] = len(candidates) > 1
    evidence["reason"] = "title match is missing, ambiguous, or lacks meaningful metadata evidence"
    return None, evidence


# ---------------------------------------------------------------- клиент с несколькими API

# Модель «тарахтит»: из последних GARBAGE_WINDOW ответов GARBAGE_LIMIT пустых или неразборчивых — API засыпает.
GARBAGE_WINDOW = 10
GARBAGE_LIMIT = 5
QUOTA_RE = re.compile(r"insufficient|quota|balance|credit|недостаточно|баланс|исчерпан|no remaining|out of tokens", re.I)
# Шлюз потерял картинку: модель отвечает «картинки нет» (01.10.2026 так делал gemini-3.8-flash у beniclo,
# 28.09 — claude-opus-5 там же). Это не «не определил», а сбой API: ответ не кэшируется, API засыпает.
NO_IMAGE_RE = re.compile(
    r"\bno (?:image|picture|photo|attachment)s?\b"
    r"|\bimage (?:was|is|has) not\b|\bimage wasn't\b|\bno image\b"
    r"|\b(?:see|find|receive|access|view)\s+(?:any|an|the)?\s*(?:image|picture|photo|attachment)s?\b.{0,40}\b(?:attached|provided|included|message)\b"
    r"|\b(?:don't|do not|didn't|did not|can't|cannot|unable to)\s+(?:see|find|access|view)\s+(?:any|an|the)\s+(?:image|picture|attachment)",
    re.I)


class NoImageError(RuntimeError):
    """Модель ответила, что картинки в запросе нет, — шлюз её не передал."""


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


class RouteState:
    """Состояние одного API этапа: спит ли (до какого времени и почему), сколько сбоев подряд."""

    def __init__(self, route: ApiRoute):
        self.route = route
        self.sleep_until = 0.0
        self.reason = ""
        self.errors = 0
        self.failing_since: float | None = None
        self.probation = False          # только что проснулся — один неудачный запрос, и снова спать
        self.last_request = 0.0
        self.key = ""
        self.key_stamp: float | None = None
        # Исходы последних запросов: True — нормальный ответ, False — пустой/неразборчивый ответ модели.
        # Модель «тарахтит» (таких ответов много) — API уступает место следующему (см. ApiClient.failed).
        self.recent: collections.deque = collections.deque(maxlen=GARBAGE_WINDOW)

    def load_key(self) -> bool:
        """Ключ из файла (перечитывается, если файл изменился — можно вписать ключ на ходу)."""
        try:
            stamp = self.route.key_path.stat().st_mtime
        except OSError:
            self.key = ""
            return False
        if stamp != self.key_stamp:
            self.key_stamp = stamp
            try:
                lines = [line.strip() for line in self.route.key_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
            except (OSError, UnicodeDecodeError):
                lines = []
            self.key = lines[0] if lines else ""
        return bool(self.key)


class ApiClient:
    def __init__(self, stage: StageSpec, cache_dir: Path, note: Callable[..., None], metadata_only: bool = False,
                 catalog: Catalog | None = None, character_index: Callable[[], dict] | None = None,
                 console: Callable[[str], None] | None = None):
        self.stage = stage
        self.states = [RouteState(route) for route in stage.apis]
        self.model = stage.model or ""
        self.threshold = stage.accept or 0.0
        self.metadata_only = metadata_only
        self.catalog = catalog
        self.character_index = character_index or (lambda: {})
        self.note_event = note
        self.console = console or (lambda text: None)
        self.current_file = ""
        self.current_label: str | None = None
        self.cache_dir = cache_dir
        self.caches: dict[str, tuple[Path, dict]] = {}
        self.lock = threading.Lock()
        self.waiting_logged = False
        self.all_down_since: float | None = None   # с какого момента спят ВСЕ API этапа (для запасной модели)
        self.fallback_used = False

    # ---------- параметры переключения (live.json + переопределение у этапа)

    def failover(self):
        from engine import live

        settings = live.current()
        for key, value in self.stage.failover.items():
            setattr(settings, key, value)
        return settings

    # ---------- кэш: по модели, а не по API — переключение ключа не заставляет переспрашивать

    def cache(self, model: str) -> tuple[Path, dict]:
        if model not in self.caches:
            path = self.cache_dir / f"{safe_name(model)}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, ValueError):
                data = {}
            self.caches[model] = (path, data)
        return self.caches[model]

    def cache_key(self, model: str, identity: str) -> str:
        catalog_digest = self.catalog.digest if self.catalog else "none"
        mode = "metadata" if self.metadata_only else "image"
        return f"{model}:{identity}:{mode}:{POLICY_VERSION}:{catalog_digest}"

    def models(self) -> list[str]:
        return list(dict.fromkeys(state.route.model for state in self.states))

    # ---------- журнал

    def _note(self, reason: str, detail: str) -> None:
        """Повтор внутри одной попытки — красная строка «ошибка ... - повтор» в логе."""
        try:
            self.note_event(self.stage.id, "повтор", file=self.current_file, retry=True, error=True,
                            reason=reason, error_text=detail, model=self.model)
        except Exception:
            pass

    def announce(self, state: RouteState, reason: str) -> None:
        label = state.route.label
        if label == self.current_label:
            return
        previous, self.current_label = self.current_label, label
        try:
            if previous is None:
                self.note_event(self.stage.id, "API", api=label, model=state.route.model)
            else:
                self.note_event(self.stage.id, "переключение API", api=label, previous=previous, reason=reason,
                                model=state.route.model)
                self.console(f"{self.stage.label}: API {previous} → {label} ({reason})")
        except Exception:
            pass

    def sleep(self, state: RouteState, minutes: int, reason: str) -> None:
        state.sleep_until = time.time() + minutes * 60
        state.reason = reason
        state.errors = 0
        state.failing_since = None
        state.probation = False
        wake = time.strftime("%H:%M", time.localtime(state.sleep_until))
        try:
            self.note_event(self.stage.id, "API недоступен", api=state.route.label, reason=f"{reason}; проверю снова в {wake}",
                            error=True)
        except Exception:
            pass

    # ---------- выбор API

    def pick(self) -> RouteState:
        """Первый по списку API, который не спит и у которого есть ключ. Все спят — ждать ближайшего пробуждения."""
        while True:
            self.button_requests()
            now = time.time()
            for state in self.states:
                if state.sleep_until > now or not state.load_key():
                    continue
                if state.sleep_until:
                    # Сон истёк — проверить одним запросом: неудача сразу отправит обратно спать.
                    state.sleep_until = 0.0
                    state.probation = True
                    reason = f"проверка после паузы ({state.reason})" if state.reason else "проверка после паузы"
                else:
                    reason = self.states_reason()
                # Проснувшийся API на пробной проверке — ещё не «API снова работает»: иначе таймер запасной модели
                # и откладывания этапа обнулялся бы при каждой проверке (раз в failover_recheck_minutes) и не
                # срабатывал никогда. Сброс — когда выбран не спавший API или пришёл успешный ответ (succeeded).
                if not state.probation:
                    self.all_down_since = None
                if self.waiting_logged:
                    self.waiting_logged = False
                    self.console(f"{self.stage.label}: API снова доступен — {state.route.label}")
                self.announce(state, reason)
                return state
            if self.all_down_since is None:
                self.all_down_since = now
            fallback = self.stage.fallback
            if fallback and not self.fallback_used and now - self.all_down_since >= fallback.after * 60:
                self.use_fallback(now - self.all_down_since)
                continue
            sleeping = [state.sleep_until for state in self.states if state.sleep_until > now]
            keyless = any(not state.sleep_until and not state.load_key() for state in self.states)
            wake = min(sleeping) if sleeping else now + 60
            if keyless:
                wake = min(wake, now + 60)   # ключ могут вписать в любой момент
            if not self.waiting_logged:
                self.waiting_logged = True
                when = time.strftime("%H:%M", time.localtime(wake))
                text = f"все API этапа {self.stage.label} недоступны, повтор в {when}"
                try:
                    self.note_event("PIPELINE", text, error=True)
                except Exception:
                    pass
                self.console(text)
            # Ждём ближайшего пробуждения кусками по 2 с: кнопки «→»/«←» не должны ждать полминуты.
            until = time.time() + max(1.0, min(30.0, wake - now))
            while time.time() < until:
                check_requests(self.cache_dir.parent / "logs")
                self.button_requests()
                if any(state.sleep_until <= time.time() and state.load_key() for state in self.states):
                    break   # «↻» разбудил API (или вписан ключ) — выбирать сразу, не дожидаясь конца паузы
                time.sleep(min(2.0, max(0.1, until - time.time())))

    def button_requests(self) -> None:
        """Кнопки окна набора (logs\\control.json):
        «↻» (retry)  — разбудить все уснувшие API этапа: следующий файл сразу проверяет их, не дожидаясь конца паузы
                       (например, баланс пополнили);
        «↔» (switch) — принудительно уйти с текущего провайдера: все его API этапа засыпают, этап сразу (даже если
                       тот спал) пробует API следующего провайдера из своего списка."""
        logs = self.cache_dir.parent / "logs"
        control = read_json(logs / "control.json")
        if control.get("retry"):
            clear_skip(logs, "retry")
            now = time.time()
            woke = [state for state in self.states if state.sleep_until > now]
            for state in woke:
                state.sleep_until = now - 1     # сон «истёк» — pick() проверит API одним запросом
            self.waiting_logged = False
            text = f"проверить API сейчас (кнопка «↻»): разбужено {len(woke)}"
            self._event("PIPELINE", text)
            self.console(f"{self.stage.label}: {text}")
        if control.get("switch"):
            clear_skip(logs, "switch")
            current = next((state for state in self.states if state.route.label == self.current_label), None)
            if current is not None:
                provider = current.route.provider.name
                for state in self.states:
                    if state.route.provider.name == provider:
                        self.sleep(state, self.failover().failover_recheck_minutes, "провайдер сменён кнопкой «↔»")
                    elif state.sleep_until > time.time():
                        # Следующий провайдер пробуется СРАЗУ, даже если он спал: сон «истёк» — пробная проверка.
                        state.sleep_until = time.time() - 1
                self.waiting_logged = False
                self.console(f"{self.stage.label}: провайдер {provider} сменён кнопкой «↔» — сразу пробую следующего")

    def _event(self, stage: str, message: str, **data) -> None:
        try:
            self.note_event(stage, message, **data)
        except Exception:
            pass

    def use_fallback(self, down: float) -> None:
        """Все API этапа спят дольше fallback.after минут — модель «зависла» у всех провайдеров: этап до конца
        запуска работает на запасной модели (своим списком API). Событие «запасная модель» меняет название
        этапа в таблице окна на fallback.table."""
        fallback = self.stage.fallback
        previous = self.model
        self.fallback_used = True
        self.states = [RouteState(route) for route in fallback.apis]
        self.model = fallback.model
        if fallback.accept is not None:
            self.threshold = fallback.accept
        self.current_label = None
        self.waiting_logged = False
        self.all_down_since = None
        reason = f"все API {previous} спят {down / 60:.0f} мин"
        try:
            self.note_event(self.stage.id, "запасная модель", model=fallback.model, previous=previous,
                            table=fallback.table, reason=reason)
        except Exception:
            pass
        self.console(f"{self.stage.label}: модель {previous} → {fallback.model} ({reason})")

    def states_reason(self) -> str:
        """Причина ухода с API, который был текущим (а не первого уснувшего по списку этапа)."""
        now = time.time()
        for state in self.states:
            if state.route.label == self.current_label and state.sleep_until > now and state.reason:
                return state.reason
        return "предыдущий API недоступен"

    # ---------- исход запроса

    def succeeded(self, state: RouteState) -> None:
        self.all_down_since = None
        state.recent.append(True)
        state.errors = 0
        state.failing_since = None
        state.probation = False
        state.reason = ""

    def failed(self, state: RouteState, exc: BaseException) -> bool:
        """Сбой запроса (уже после внутренних повторов). True — API уснул, файл отправляется в следующий API."""
        text = f"{type(exc).__name__}: {str(exc)[:160]}"
        # Пустой или неразборчивый ответ модели (рассуждения съели лимит, сломанный JSON) — беда конкретного
        # файла, а не API: другие файлы через этот API проходят. API не усыпляется, файл получает ошибку
        # (и в конце набора уходит в «Other»).
        if isinstance(exc, json.JSONDecodeError) or (isinstance(exc, ValueError) and "empty model response" in str(exc)):
            state.recent.append(False)
            bad = state.recent.count(False)
            if len(state.recent) >= GARBAGE_WINDOW // 2 and bad >= GARBAGE_LIMIT:
                # Не отдельный трудный файл, а модель на этом API «тарахтит» — переключиться на запасной API
                # (в том числе с другой моделью). Через failover_recheck_minutes API проверится снова.
                state.recent.clear()
                self.sleep(state, self.failover().failover_recheck_minutes,
                           f"модель тарахтит: {bad} пустых/неразборчивых ответов из последних {GARBAGE_WINDOW}: {text}")
                return True
            return False
        settings = self.failover()
        now = time.time()
        if state.probation:
            self.sleep(state, settings.failover_recheck_minutes, f"проверка после паузы не прошла: {text}")
            return True
        state.errors += 1
        state.failing_since = state.failing_since or now
        if state.errors >= settings.failover_errors:
            word = "сбой" if state.errors % 10 == 1 and state.errors % 100 != 11 else                 "сбоя" if state.errors % 10 in (2, 3, 4) and state.errors % 100 not in (12, 13, 14) else "сбоев"
            self.sleep(state, settings.failover_recheck_minutes, f"{state.errors} {word} подряд: {text}")
            return True
        if now - state.failing_since >= settings.failover_minutes * 60:
            minutes = int((now - state.failing_since) // 60)
            self.sleep(state, settings.failover_recheck_minutes, f"сбои {minutes} мин без успеха: {text}")
            return True
        return False

    def run_with_failover(self, content_builder) -> tuple[dict, str]:
        """content_builder(state) делает запрос(ы) через этот API и возвращает ответ.
        Исчерпан баланс — API спит failover_quota_recheck_minutes, тот же файл идёт в следующий API."""
        while True:
            state = self.pick()
            try:
                result = content_builder(state)
            except QuotaExhaustedError as exc:
                self.sleep(state, self.failover().failover_quota_recheck_minutes, f"баланс исчерпан: {str(exc)[:160]}")
                continue
            except NoImageError as exc:
                # Систематический сбой шлюза — сразу спать, тот же файл — в следующий API.
                self.sleep(state, self.failover().failover_recheck_minutes, f"модель не получает картинку: {str(exc)[:160]}")
                continue
            except Exception as exc:
                if self.failed(state, exc):
                    continue
                raise
            self.succeeded(state)
            return result, state.route.model

    # ---------- сеть

    def _throttle(self, state: RouteState) -> None:
        with self.lock:
            wait = state.route.provider.interval - (time.monotonic() - state.last_request)
            if wait > 0:
                time.sleep(wait)
            state.last_request = time.monotonic()

    def _give_up_early(self, state: RouteState, detail: str) -> None:
        """Внутренние повторы (сеть, 5xx, 429) не тянутся дольше failover_minutes, если есть куда переключиться."""
        state.failing_since = state.failing_since or time.time()
        if len(self.states) > 1 and time.time() - state.failing_since >= self.failover().failover_minutes * 60:
            raise RuntimeError(f"сбой дольше {self.failover().failover_minutes} мин: {detail}")

    def _post(self, content: list[dict], state: RouteState) -> str:
        """Один логический запрос через API state с внутренними повторами (rate limit, сеть, 5xx).
        Исчерпанный баланс сразу поднимает QuotaExhaustedError."""
        route = state.route
        provider = route.provider
        key = state.key
        body = {"model": route.provider_model, "messages": [{"role": "user", "content": content}]}
        if provider.temperature:
            body["temperature"] = 0.1
        is_claude = "claude" in route.model.casefold() or "claude" in route.provider_model.casefold()
        dialect = provider.reasoning
        if is_claude:
            # Claude сначала «думает»; если рассуждения съели весь лимит (пустой ответ, finish=length),
            # тот же запрос сразу повторяется без рассуждений.
            body["max_tokens"] = 8000
            if dialect == "anthropic":
                body["thinking"] = {"type": "enabled", "budget_tokens": 2000}
            elif dialect == "openrouter":
                body["reasoning"] = {"max_tokens": 2000}
        data = json.dumps(body).encode()
        network_limit = NETWORK_RETRIES if len(self.states) == 1 else 2
        rate_attempts = network_attempts = 0
        thinking_fallback = False
        while True:
            self._throttle(state)
            request = urllib.request.Request(provider.endpoint, data=data, method="POST",
                                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    text = response.read().decode("utf-8", errors="replace")
                if text.lstrip().startswith("data:"):
                    raise ValueError("ответ потоком (SSE) на обычный запрос")
                payload = json.loads(text)
                choices = payload.get("choices") if isinstance(payload, dict) else None
                if not choices:
                    raise ValueError("в ответе нет choices")
                choice = choices[0]
                raw = (choice.get("message") or {}).get("content", "")
                if isinstance(raw, list):
                    raw = "".join(str(block.get("text", "")) for block in raw if isinstance(block, dict))
                finish = choice.get("finish_reason")
                if not str(raw).strip() and is_claude and finish == "length" and not thinking_fallback and dialect != "none":
                    thinking_fallback = True
                    self._note("рассуждения не уложились в лимит", "ответ без рассуждений")
                    body["max_tokens"] = 2000
                    if dialect == "anthropic":
                        body["thinking"] = {"type": "disabled"}
                    else:
                        body["reasoning"] = {"enabled": False}
                    data = json.dumps(body).encode()
                    continue
                if not str(raw).strip():
                    raise ValueError(f"empty model response (finish={finish})")
                return str(raw)
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", errors="replace").strip()
                except OSError:
                    detail = ""
                detail = detail.replace(key, "[redacted]")[:500]
                if exc.code == 402 or (exc.code in (400, 401, 403) and QUOTA_RE.search(detail)):
                    raise QuotaExhaustedError(f"{route.label}: HTTP {exc.code}: {detail}") from exc
                retry_after = 2.0
                try:
                    retry_after = float(json.loads(detail).get("error", {}).get("retry_after") or retry_after)
                except (ValueError, AttributeError, TypeError):
                    pass
                if exc.code == 429 and rate_attempts < RATE_LIMIT_RETRIES:
                    self._give_up_early(state, f"HTTP 429: {detail[:120]}")
                    rate_attempts += 1
                    wait = min(90.0, max(retry_after, 2.0) + 2.0 * rate_attempts)
                    self._note(f"rate limit, пауза {wait:.0f}с ({rate_attempts}/{RATE_LIMIT_RETRIES})", f"{route.label}: HTTP 429")
                    time.sleep(wait)
                    continue
                if exc.code >= 500 and network_attempts < network_limit:
                    self._give_up_early(state, f"HTTP {exc.code}")
                    network_attempts += 1
                    wait = min(90.0, 5.0 * 2 ** (network_attempts - 1))
                    self._note(f"сбой сервера, пауза {wait:.0f}с ({network_attempts}/{network_limit})", f"{route.label}: HTTP {exc.code}")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                detail = f"{type(exc).__name__}: {str(exc).replace(key, '[redacted]')[:150]}"
                if network_attempts < network_limit:
                    self._give_up_early(state, detail)
                    network_attempts += 1
                    wait = min(90.0, 5.0 * 2 ** (network_attempts - 1))
                    self._note(f"сбой сети, пауза {wait:.0f}с ({network_attempts}/{network_limit})", f"{route.label}: {detail}")
                    time.sleep(wait)
                    continue
                raise

    def _request_obj(self, content: list[dict], state: RouteState) -> dict:
        """Запрос + разбор JSON-ответа; пустой или битый ответ переспрашивается."""
        for attempt in range(PARSE_RETRIES + 1):
            try:
                return drop_mapping_answer(parse_json_response(self._post(content, state)))
            except (json.JSONDecodeError, ValueError, KeyError, TypeError, IndexError) as exc:
                if attempt >= PARSE_RETRIES:
                    raise
                self._note("неразборчивый ответ", f"{state.route.label}: {type(exc).__name__}: {str(exc)[:100]}")
        raise RuntimeError("unreachable")

    # ---------- классификация

    def classify(self, path: Path, key: str, identity: str) -> dict:
        """Ответ модели по файлу (конверт с confidence/series/characters/result).

        Сначала кэш (любой модели этапа — ответ уже оплачен), потом запрос через первый доступный API.
        result — определение с учётом порога ЭТОГО этапа.
        """
        self.current_file = key
        for model in self.models():
            path_, data = self.cache(model)
            with self.lock:
                cached = data.get(self.cache_key(model, identity))
            if cached and cached.get("state") in {"ok", "unknown"} and isinstance(cached.get("result"), dict):
                return self.apply_threshold(dict(cached["result"]))
        envelope, model = self.run_with_failover(lambda state: self.request(path, state))
        cache_path, data = self.cache(model)
        with self.lock:
            data[self.cache_key(model, identity)] = {"state": "ok" if envelope.get("known") else "unknown", "result": envelope}
            atomic_json(cache_path, data)
        return self.apply_threshold(envelope)

    def apply_threshold(self, envelope: dict) -> dict:
        envelope = dict(envelope)
        envelope["series"] = clean_series(envelope.get("series"))
        confidence = normalized_confidence(envelope.get("confidence", 0.0))
        if self.metadata_only:
            result = envelope.get("result")
            if isinstance(result, dict):
                result = {**result, "series": clean_series(result.get("series"))}
                if is_multi_title(result.get("series")) or confidence < self.threshold:
                    result = None
            envelope["result"] = result if isinstance(result, dict) else None
            return envelope
        candidate = envelope.get("candidate")
        if not isinstance(candidate, dict):
            # Старые записи кэша: определение сохранялось, только если уверенность прошла порог того запуска.
            candidate = envelope.get("result") if isinstance(envelope.get("result"), dict) else raw_image_result(envelope, self.model)
        if isinstance(candidate, dict):
            candidate = {**candidate, "series": clean_series(candidate.get("series")), "model": self.model}
            if is_multi_title(candidate.get("series")) or is_unknown_title(candidate.get("series")):
                candidate = None
        envelope["result"] = candidate if candidate and confidence >= self.threshold else None
        return envelope

    def request(self, path: Path, state: RouteState) -> dict:
        metadata, metadata_count, metadata_chars = metadata_info(path)
        prompt = METADATA_PROMPT if self.metadata_only else IMAGE_PROMPT
        if self.metadata_only and self.catalog:
            prompt += "\nRanked catalog (prior, not proof):\n" + self.catalog.prompt_text()
        if metadata:
            prompt += "\nEmbedded metadata:\n" + metadata
        if self.metadata_only:
            content = [{"type": "text", "text": prompt}]
        else:
            mime, raw_bytes = image_payload(path)
            encoded = base64.b64encode(raw_bytes).decode("ascii")
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}]
        obj = self._request_obj(content, state)
        if not self.metadata_only:
            answer = " ".join(str(obj.get(field) or "") for field in ("reason", "series", "title"))
            if not obj.get("characters") and NO_IMAGE_RE.search(answer):
                raise NoImageError(f"{state.route.label}: {answer.strip()[:120]}")
        if self.metadata_only and self.catalog:
            titles, characters = metadata_catalog_identity(metadata, self.catalog)
            if not obj.get("series") and not obj.get("characters") and (titles or characters or has_meaningful_metadata(metadata)):
                obj = self._request_obj([{"type": "text", "text": (
                    "The first answer missed explicit metadata identity. Resolve these metadata hints using the ranked catalog. "
                    "Return ONLY JSON with known, confidence, characters, series, reason. "
                    "Hints: titles=" + ", ".join(titles) + "; characters=" + ", ".join(characters) +
                    "\nRanked catalog:\n" + self.catalog.prompt_text())}], state)
            if not obj.get("series") and obj.get("characters"):
                obj = self._request_obj([{"type": "text", "text": (
                    "Resolve this character using the ranked catalog. Return ONLY JSON with known, confidence, "
                    "characters, series, reason. Prefer the highest-ranked unambiguous title; if no exact match "
                    "exists, choose a plausible popular title only when the character identity is meaningful. "
                    "Character: " + ", ".join(str(value) for value in obj.get("characters", [])) +
                    "\nRanked catalog:\n" + self.catalog.prompt_text())}], state)
        chars = [str(value) for value in (obj.get("characters") or []) if str(value).strip()]
        series = clean_series(obj.get("series") or obj.get("title") or "")
        model_known = bool(chars or series) and not is_unknown_title(series)
        if self.metadata_only:
            raw = None
            if model_known:
                raw = {"kind": "character" if len(chars) == 1 else "series_group", "series": series,
                       "character": chars[0] if len(chars) == 1 else " / ".join(chars), "tags": {}, "model": self.model,
                       "confidence": normalized_confidence(obj.get("confidence", 0.0)), "reason": str(obj.get("reason") or "")}
            envelope = make_envelope(obj, metadata, raw, False, metadata_count, metadata_chars)
            if self.catalog is None:
                supported, _ = metadata_supports(metadata, [series, *chars])
                accepted, evidence = (raw if supported and raw else None), {"reason": "no catalog"}
            else:
                accepted, evidence = json_evidence(metadata, envelope, self.catalog)
            envelope["evidence"] = evidence
            envelope["result"] = accepted
            envelope["api"] = state.route.label
            return envelope
        resolved_items = [resolve_ai_name(character, series, self.character_index()) for character in chars] if series else []
        resolved_items = [item for item in resolved_items if item]
        envelope = make_envelope(obj, metadata, None, False, metadata_count, metadata_chars)
        candidate = raw_image_result(envelope, self.model)
        if candidate and resolved_items and len({item["series"] for item in resolved_items}) == 1:
            # Имя совпало с каноническим тегом индекса персонажей — берём каноническое написание.
            candidate = {**candidate, "series": resolved_items[0]["series"],
                         "character": resolved_items[0]["character"] if len(resolved_items) == 1 else " / ".join(chars),
                         "tags": {k: v for item in resolved_items for k, v in item["tags"].items()}}
            envelope["resolved"] = True
        envelope["candidate"] = candidate
        envelope["result"] = candidate if candidate and envelope["confidence"] >= self.threshold else None
        envelope["api"] = state.route.label
        return envelope
