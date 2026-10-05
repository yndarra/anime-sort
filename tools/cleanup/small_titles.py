r"""Мелкие тайтлы — разобрать: тайтлы Waifu, где картинок не больше порога (configs\other\settings.json →
small_title_max_files, по умолчанию 15), перестают быть отдельными папками:
    - персонаж мелкого тайтла (полное имя из 2+ слов), который есть и в «большом» тайтле (больше порога картинок), — его картинки
      переносятся в папку этого персонажа в большом тайтле (если таких тайтлов несколько — в самый большой);
    - всё остальное (персонажи, которых нет в больших тайтлах, файлы прямо в папке тайтла, MIX) — в Waifu\Other
      россыпью: other.bat отправит их на второй круг конвейера.
Сравнение имён — без номеров папок («12. Rem») и без регистра. Перед переносом — итог и вопрос.
На время работы наблюдатели tools\watch замирают (Waifu массово меняется).

python tools\cleanup\small_titles.py            показать и спросить
python tools\cleanup\small_titles.py --yes      без вопроса
python tools\cleanup\small_titles.py --max 15   свой порог
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = PROJECT / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "sort"))

import console  # noqa: E402

NUMBER = re.compile(r"^\d+\.\s+")
MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
KEEP = {"Other", "MIX", "NPC", "Групповые фото"}


def clean(name: str) -> str:
    return NUMBER.sub("", name)


def key(name: str) -> str:
    return " ".join(clean(name).split()).casefold()


def media(folder: Path) -> list[Path]:
    return [f for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in MEDIA]


def plan(waifu: Path, limit: int) -> tuple[list, dict]:
    """→ (переносы [(файл, папка назначения или None — в Other)], сводка {тайтл: (в большие, в Other)})."""
    titles = [(t, len(media(t))) for t in waifu.iterdir()
              if t.is_dir() and not t.name.startswith(".") and clean(t.name) not in KEEP]
    big = [(t, n) for t, n in titles if n > limit]
    small = [(t, n) for t, n in titles if n <= limit]
    # Персонаж → папки этого персонажа в больших тайтлах (с числом картинок тайтла).
    index: dict[str, list[tuple[int, Path]]] = {}
    for title, count in big:
        for character in title.iterdir():
            if character.is_dir() and clean(character.name) not in KEEP:
                index.setdefault(key(character.name), []).append((count, character))
    moves, summary = [], {}
    for title, _ in small:
        to_big = to_other = 0
        for item in title.iterdir():
            files = media(item) if item.is_dir() else ([item] if item.suffix.lower() in MEDIA else [])
            target = None
            # Только полные имена (2+ слова): одиночные («Yuna», «Freia») слишком часто означают разных персонажей.
            if item.is_dir() and clean(item.name) not in KEEP and key(item.name) in index and len(key(item.name).split()) >= 2:
                target = max(index[key(item.name)], key=lambda pair: pair[0])[1]
            moves += [(file, target) for file in files]
            if target is not None:
                to_big += len(files)
            else:
                to_other += len(files)
        summary[title] = (to_big, to_other)
    return moves, summary


def unique(target: Path) -> Path:
    if not target.exists():
        return target
    for index in range(2, 100000):
        candidate = target.with_name(f"{target.stem}__{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"не подобрать имя для {target}")


def remove_empty(folder: Path) -> None:
    for path in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        folder.rmdir()
    except OSError:
        pass


def main() -> int:
    from waifu_common import WAIFU_ROOT, waifu_busy

    import modes

    default = int(modes.other_settings()["small_title_max_files"])   # configs\\other\\settings.json
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=default, help="тайтл с таким числом картинок и меньше — разобрать")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()

    console.title(f"Мелкие тайтлы (≤ {args.max} картинок): персонажи — в большие тайтлы, остальное — в Other")
    moves, summary = plan(WAIFU_ROOT, args.max)
    if not summary:
        console.ok("Мелких тайтлов нет.")
        return 0
    to_big = sum(1 for _, target in moves if target is not None)
    to_other = len(moves) - to_big
    for title, (b, o) in [item for item in summary.items() if item[1][0]][:25]:
        console.say(f"  {clean(title.name)}: в большие тайтлы {b}, в Other {o}", console.GREY)
    console.say(f"Мелких тайтлов {len(summary)}: картинок в большие тайтлы {to_big}, в Other {to_other}", console.WHITE)
    if not args.yes and console.ask("Перенести?", "дн") != "д":
        console.warn("Отменено.")
        return 0
    other = WAIFU_ROOT / "Other"
    other.mkdir(exist_ok=True)
    with waifu_busy():
        for file, target in moves:
            file.replace(unique((target if target is not None else other) / file.name))
        for title in summary:
            remove_empty(title)
    console.ok(f"Готово: {len(summary)} мелких тайтлов разобрано ({to_big} картинок в большие тайтлы, {to_other} в Other).")
    return 0


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
