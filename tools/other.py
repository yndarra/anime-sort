r"""other.bat — «Other» на второй круг: ВСЕ картинки Waifu\Other (и россыпью, и в подпапках) ПЕРЕНОСЯТСЯ
в новые папки коллекции dataN\data<N>-other по other_batch_size (configs\tools.json), номера — после последней dataN.
Совпавшие имена получают суффикс __2, __3 …; опустевшие подпапки Other удаляются.
Затем предлагается prepare.bat: под папки -other он берёт configs\template-other.json (шесть AI-этапов, запасные
модели, соседи по общему порядку всех dataN). Конвейер ПЕРЕНОСИТ файлы dataN-other в test-dataN-other\in и удаляет
пустую dataN-other; итог, как обычно, КОПИРУЕТСЯ в Waifu (finish.bat).

Порядок файлов — по времени изменения. Перед переносом — план и вопрос. Журнал (откуда → куда) —
anime-sort\logs\other_<время>.json.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "tools"))
sys.path.insert(1, str(PROJECT))

import console  # noqa: E402
from console import UserError  # noqa: E402

MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".avif"}
BATCH_RE = re.compile(r"^data(\d+)(?:-other)?$")


def unique(target: Path) -> Path:
    if not target.exists():
        return target
    for index in range(2, 100000):
        candidate = target.with_name(f"{target.stem}__{index}{target.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"не подобрать имя для {target}")


def main() -> int:
    from engine.config import Report, load_settings
    from waifu_common import waifu_busy

    settings = load_settings(Report(), check_run_files=False)
    if settings is None:
        raise UserError("configs\\config.json с ошибками", "проверьте: venv\\Scripts\\python engine\\main.py --check")
    try:
        size = int(json.loads((PROJECT / "configs" / "tools.json").read_text(encoding="utf-8")).get("other_batch_size", 500))
    except (OSError, ValueError):
        size = 500
    other = settings.waifu / "Other"
    console.title("«Other» на второй круг")
    files = sorted((p for p in other.rglob("*") if p.is_file() and p.suffix.lower() in MEDIA),
                   key=lambda p: (p.stat().st_mtime, p.name)) if other.is_dir() else []
    if not files:
        console.ok("В «Other» нет картинок — переносить нечего.")
        return 0
    numbers = [int(m.group(1)) for p in settings.source.iterdir() if p.is_dir() for m in [BATCH_RE.match(p.name)] if m]
    first = max(numbers, default=0) + 1
    groups = [files[i:i + size] for i in range(0, len(files), size)]
    for offset, group in enumerate(groups):
        console.say(f"  data{first + offset}-other: {len(group)} файлов")
    if console.ask(f"Перенести {len(files)} файлов из «Other» в dataN?", "дн") != "д":
        console.warn("Отменено.")
        return 0
    journal = {}
    with waifu_busy():
        for offset, group in enumerate(groups):
            folder = settings.source / f"data{first + offset}-other"
            folder.mkdir(parents=True)
            journal[folder.name] = moved = {}
            for path in group:
                target = unique(folder / path.name)
                shutil.move(str(path), str(target))
                moved[target.name] = str(path.relative_to(other))
            console.ok(f"  {folder.name}: {len(group)}")
        for path in sorted((p for p in other.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            (path / "desktop.ini").unlink(missing_ok=True)
            try:
                path.rmdir()
            except OSError:
                pass
    log = PROJECT / "logs" / f"other_{time.strftime('%Y-%m-%d_%H-%M-%S')}.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(journal, ensure_ascii=False, indent=1), encoding="utf-8")
    console.ok(f"Готово: {len(files)} файлов в {len(groups)} папках.")
    if console.ask("Запустить prepare (проверка API и сборка пачек под новые папки)?", "дн") == "д":
        subprocess.run([sys.executable, str(PROJECT / "tools" / "prepare.py")])
    return 0


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
