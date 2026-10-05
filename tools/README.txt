tools — инструменты anime-sort
==============================

Запуск — .bat в корне проекта (prepare.bat, finish.bat, other.bat) или напрямую: venv\Scripts\python tools\<…>.py.
Окна инструментов: цветной журнал, ошибка — красным с подсказкой «Что сделать», окно не закрывается само.
Коллекция (Waifu, dataN, test-dataN) — из "collection" в configs\config.json → <коллекция>\anime-paths.json.

Запуск конвейеров
-----------------
prepare.py      проверка API (probe.py) → поиск необработанных dataN → configN.json из configs\main|other\template.json → запуск (--mode main|other)
modes.py        режимы (main / other): где шаблон и настройки режима, перенос старой раскладки configs
probe.py        маленький запрос на каждую пару «ключ + модель»: работает / нет баланса / теряет картинку / нет ключа
other.py        всё из Waifu\Other → новые dataN-other (по other_batch_size из configs\other\settings.json) → prepare

После конвейеров
----------------
finish.py       шаги: add → мелкие тайтлы → нумерация (агент имён — только вручную); перед шагом «выполнить/пропустить»,
                при ошибке «повторить/пропустить/выйти»
add\            add.py — слияние готовых наборов в Waifu; сам находит готовые и ведёт журнал folders.json
agent\          names_agent.py — агент имён, запуск вручную (только OpenRouter, ключ secrets\agent\openrouter.txt): дубли тайтлов
                и персонажей → правила names.json (после проверки и вопроса) → fix_name
cleanup\        small_titles.py — тайтлы с ≤ small_title_max_files картинок → подпапками в «Other»
fix_name\       fix_name.py — переименования по names.json; regroup.py — порядок в names.json; honkai.py, dublicates.py
sort\           нумерация папок по количеству картинок (sort.py) и снятие нумерации (sort_cancel.py)
watch\          наблюдатели: пустые папки, нумерация на ходу; на время массовых правок замирают (tools\.busy)
ani\            каталог ani.txt — частые тайтлы/персонажи Waifu (подсказка этапу json_meta)
mark_broken\    ручные починки наборов: mark_broken.py (застрявшие файлы → «Other»), reset_no_image.py

Общее
-----
console.py        окна инструментов: цвета, ошибки «что сделать», вопросы д/н, ожидание Enter
                  (ANIME_SORT_NO_PAUSE=1 — без ожидания, так шаги идут внутри finish.bat)
waifu_common.py   старые общие функции инструментов Waifu: путь к Waifu, запуск дочерних скриптов, блокировка .busy
merge_journal.jsonl  журнал копирования add.py (возобновление после сбоя) — не в git
