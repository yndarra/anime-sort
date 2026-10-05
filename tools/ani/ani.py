from __future__ import annotations

import argparse
import hashlib
import os
import time
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV_DIR / "sort"))
sys.path.insert(0, str(DEV_DIR))

from sort import ranked, remove_empty_dirs
from waifu_common import WAIFU_ROOT, configure_console, run_guarded

configure_console()

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "ani.txt"


def write_catalog(root: Path, output: Path, title_limit: int, character_limit: int) -> None:
    remove_empty_dirs(root)
    rows = ranked(root)[:title_limit]
    lines = ["# ani-format=1", f"# J={title_limit} K={character_limit}", ""]
    for row in rows:
        lines.append(row["name"])
        for character in row["characters"][:character_limit]:
            lines.append(f" - {character['name']}")
        lines.append("")
    content = "\n".join(lines).rstrip() + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Две пачки могут запустить ani.py одновременно: у каждого процесса свой временный файл,
    # а занятый ani.txt (WinError 32) пробуем заменить ещё несколько раз.
    temporary = output.with_name(f"{output.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(temporary, output)
            break
        except PermissionError:
            if attempt == 9:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.5 + attempt * 0.5)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    print(f"written={output} titles={len(rows)} sha256={digest}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=WAIFU_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--J", type=int, default=30)
    parser.add_argument("--K", type=int, default=15)
    parser.add_argument("--no-pause", action="store_true")
    args = parser.parse_args()
    if args.J < 1 or args.K < 1:
        raise SystemExit("J and K must be positive")
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"Waifu root does not exist: {root}")
    write_catalog(root, args.output.resolve(), args.J, args.K)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_guarded(main))
