r"""Общий порядок файлов для наборов «второго круга» (test-dataN-other).

Файлы такого набора когда-то лежали в обычных наборах test-dataN среди соседей, а теперь собраны вместе из
Waifu\Other — порядок внутри набора-other соседей не отражает. Поэтому этапы соседей (N, N-L&R, N-LR …) для
-other наборов смотрят на ОБЩИЙ список: все обычные наборы test-dataN по номеру (части test-dataN_K — по K),
внутри набора — порядок manifest.json (порядок сохранения). Файл набора-other находится в общем списке по
sha256 (содержимое не менялось: Waifu получает копии), а определения соседей берутся из итоговой раскладки
out\0. ALL\Тайтл[\Персонаж]\файл каждого набора.

Файлы самого набора-other в общем списке считаются НЕ определёнными (они ведь в Other), пока их не определит
этап этого же набора — тогда используется новое определение. Файл, которого нет ни в одном manifest
(например, добавленный вручную), соседей не получает.
"""
from __future__ import annotations

import re
from pathlib import Path

from engine.core import Dataset, OTHER, file_digest, read_json

DATASET_RE = re.compile(r"^test-data(\d+)(?:_(\d+))?$")
ALL_DIR = "0. ALL"
UNKNOWN = {OTHER, "Другое"}


def is_other(ds: Dataset) -> bool:
    return ds.root.name.casefold().endswith("-other")


def ordinary_datasets(parent: Path) -> list[Path]:
    found = []
    for path in parent.iterdir():
        match = DATASET_RE.match(path.name)
        if match and path.is_dir():
            found.append(((int(match.group(1)), int(match.group(2) or 0)), path))
    return [path for _, path in sorted(found)]


def placements(root: Path, names: set[str]) -> dict[str, dict]:
    """Имя исходного файла → определение по раскладке out\\0. ALL (Тайтл[\\Персонаж]\\«Модель имя»)."""
    result: dict[str, dict] = {}
    base = root / "out" / ALL_DIR
    if not base.is_dir():
        return result
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(base).parts
        if len(parts) < 2 or parts[0] in UNKNOWN:
            continue
        name = path.name if path.name in names else path.name.split(" ", 1)[-1]
        if name not in names:
            continue
        character = parts[1] if len(parts) >= 3 else ""
        result[name] = {"kind": "title_only" if not character else "character", "series": parts[0],
                        "character": character, "tags": {}, "model": "global"}
    return result


class GlobalOrder:
    """position(key) — место файла набора в общем списке; known — {место: определение} на начало этапа;
    name_at(место) — «набор/файл» для деталей."""

    def __init__(self, ds: Dataset, known_here: dict[int, dict]):
        self.names: list[str] = []
        self.known: dict[int, dict] = {}
        by_digest: dict[str, int] = {}
        for root in ordinary_datasets(ds.root.parent):
            manifest = read_json(root / "manifest.json", {})
            files, digests = manifest.get("files") or [], manifest.get("sha256") or []
            if len(files) != len(digests):
                continue
            found = placements(root, set(files))
            for name, digest in zip(files, digests):
                index = len(self.names)
                self.names.append(f"{root.name}/{name}")
                by_digest.setdefault(digest, index)
                if name in found:
                    self.known[index] = found[name]
        self.positions: dict[str, int] = {}
        for local, (_, path, key) in enumerate(ds.entries):
            index = by_digest.get(file_digest(path))
            if index is None:
                continue
            self.positions[key] = index
            # Файл набора-other: старое определение (если было — например, из разобранного мелкого тайтла)
            # не считается, считается только определение этапов этого набора.
            self.known.pop(index, None)
            if local in known_here:
                self.known[index] = known_here[local]
        self.order = sorted(self.known)

    def position(self, key: str) -> int | None:
        return self.positions.get(key)

    def name_at(self, index: int | None) -> str | None:
        return self.names[index] if index is not None and 0 <= index < len(self.names) else None
