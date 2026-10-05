r"""finish.bat — доделать работу после конвейеров, по шагам:
    1. add          влить готовые наборы test-dataN в Waifu (tools\add\add.py; folders.json ведётся сам)
    2. мелкие тайтлы → «Other» (tools\cleanup\small_titles.py)
    3. нумерация    пронумеровать папки Waifu по числу картинок (tools\sort\sort.py)
Агент имён (tools\agent\names_agent.py) сюда не входит — он тратит баланс OpenRouter, запускается только вручную.
Перед каждым шагом — «выполнить / пропустить». Шаг упал (нет баланса у агента и т. п.) — окно не закрывается:
«повторить / пропустить / выйти». В конце — итог по шагам.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
TOOLS = PROJECT / "tools"
sys.path.insert(0, str(TOOLS))

import console  # noqa: E402

STEPS = [
    ("Слияние готовых наборов в Waifu", [TOOLS / "add" / "add.py", "--no-pause"]),
    ("Мелкие тайтлы → «Other»", [TOOLS / "cleanup" / "small_titles.py"]),
    ("Нумерация папок Waifu", [TOOLS / "sort" / "sort.py", "--no-pause"]),
]


def run_step(command: list) -> int:
    env = dict(os.environ, ANIME_SORT_NO_PAUSE="1", PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, *map(str, command)], env=env).returncode


def main() -> int:
    console.title("Доделать после конвейеров")
    results = []
    for index, (name, command) in enumerate(STEPS, 1):
        console.title(f"Шаг {index} из {len(STEPS)}: {name}")
        if console.ask("д — выполнить, п — пропустить", "дп") == "п":
            results.append((name, "пропущен"))
            continue
        while True:
            code = run_step(command)
            if code == 0:
                results.append((name, "готово"))
                break
            console.error(f"шаг «{name}» завершился с ошибкой (код {code}, текст выше)",
                          "исправьте причину и выберите «повторить», или пропустите шаг")
            answer = console.ask("р — повторить, п — пропустить шаг, в — выйти", "рпв")
            if answer == "р":
                continue
            if answer == "п":
                results.append((name, "пропущен после ошибки"))
                break
            results.append((name, "остановлено"))
            return summary(results, 1)
    return summary(results, 0)


def summary(results: list, code: int) -> int:
    console.title("Итог")
    for name, state in results:
        color = console.GREEN if state == "готово" else console.YELLOW
        console.say(f"  {name}: {state}", color)
    return code


if __name__ == "__main__":
    raise SystemExit(console.guarded(main))
