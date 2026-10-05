from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

POLICY_VERSION = "ani-json-v2"
_STOPWORDS = {"metadata", "title", "description", "comment", "twitter", "reddit", "pinterest", "credits", "credit", "official", "art", "by", "the"}


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"[^a-z0-9а-яё]+", "", value.casefold())


def words(value: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9а-яё]+", str(value).casefold()) if word not in _STOPWORDS]


def has_meaningful_metadata(value: str) -> bool:
    return any(len(word) >= 3 and not word.isdigit() for word in words(value))


def metadata_identity_hints(metadata: str) -> list[str]:
    hints = []
    for line in str(metadata).splitlines():
        _, separator, value = line.partition(":")
        if not separator:
            value = line
        value = re.sub(r"https?://\S+", " ", value)
        value = re.sub(r"#[a-z0-9_-]+", lambda match: " " + match.group(0)[1:].replace("_", " ") + " ", value, flags=re.I)
        value = re.sub(r"[^a-z0-9а-яё' -]+", " ", value.casefold())
        value = re.sub(r"\s+", " ", value).strip()
        if value and has_meaningful_metadata(value):
            hints.append(value)
    return list(dict.fromkeys(hints))


def metadata_catalog_identity(metadata: str, catalog: "Catalog") -> tuple[list[str], list[str]]:
    titles: list[str] = []
    characters: list[str] = []
    raw = " ".join(metadata_identity_hints(metadata))
    for entry in catalog.entries:
        raw_normalized = normalize(raw)
        if normalize(entry.title) in raw_normalized or alias_match(raw, entry.title):
            titles.append(entry.title)
        if entry.character != "MIX" and (normalize(entry.character) in raw_normalized or alias_match(raw, entry.character)):
            characters.append(entry.character)
    for token in re.findall(r"#[A-Za-z][A-Za-z0-9_-]+", str(metadata)):
        tag = token[1:].casefold()
        for entry in catalog.entries:
            if tag in normalize(entry.title) or tag in normalize(entry.character):
                if tag in normalize(entry.title):
                    titles.append(entry.title)
                if tag in normalize(entry.character):
                    characters.append(entry.character)
    return list(dict.fromkeys(titles)), list(dict.fromkeys(characters))


def alias_match(value: str, canonical: str) -> bool:
    left = normalize(value)
    right = normalize(canonical)
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 3 and len(right) >= 4 and right.startswith(left):
        return True
    if len(left) >= 5 and len(right) >= 5 and difflib.SequenceMatcher(None, left, right).ratio() >= 0.82:
        return True
    return False


def metadata_supports(metadata: str, values: list[str]) -> tuple[bool, str]:
    source = normalize(metadata)
    source_words = set(words(metadata))
    for value in values:
        candidate = normalize(value)
        if not candidate or candidate in _STOPWORDS:
            continue
        if candidate in source:
            return True, value
        candidate_words = [word for word in words(value) if len(word) >= 4]
        if candidate_words and all(word in source_words for word in candidate_words):
            return True, value
        if len(candidate) >= 5:
            for token in source_words:
                if len(token) >= 5 and difflib.SequenceMatcher(None, candidate, token).ratio() >= 0.82:
                    return True, value
        if len(candidate) >= 4:
            for token in source_words:
                if len(token) >= 3 and (candidate.startswith(token) or token.startswith(candidate)):
                    return True, value
    return False, ""


@dataclass(frozen=True)
class Entry:
    title: str
    character: str
    title_rank: int
    character_rank: int


class Catalog:
    def __init__(self, entries: list[Entry], digest: str, source: Path | None = None):
        self.entries = entries
        self.digest = digest
        self.source = source

    def prompt_text(self) -> str:
        grouped: dict[str, list[Entry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.title, []).append(entry)
        lines = []
        for title, entries in grouped.items():
            lines.append(f"- {title}")
            for entry in entries:
                if entry.character:
                    lines.append(f"  - {entry.character}")
        return "\n".join(lines)

    def matching_titles(self, value: str) -> list[Entry]:
        matches = [entry for entry in self.entries if alias_match(value, entry.title)]
        unique: dict[str, Entry] = {}
        for entry in matches:
            unique.setdefault(entry.title, entry)
        return list(unique.values())

    def matching_characters(self, value: str) -> list[Entry]:
        return [entry for entry in self.entries if entry.character and alias_match(value, entry.character)]


def load_catalog(path: Path, title_limit: int = 30, character_limit: int = 15) -> Catalog:
    raw = path.read_bytes() if path.exists() else b""
    digest = hashlib.sha256(raw).hexdigest()
    entries: list[Entry] = []
    title = ""
    title_rank = 0
    character_rank = 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            character = stripped[1:].strip()
            if title and character and character_rank < character_limit:
                character_rank += 1
                entries.append(Entry(title, character, title_rank, character_rank))
            continue
        if title_rank >= title_limit:
            break
        title = stripped
        title_rank += 1
        character_rank = 0
    return Catalog(entries, digest, path)
