ANIME-SORT — сортировка аниме-картинок по тайтлам и персонажам
==============================================================
(английская версия для GitHub — README.md, устройство подробно — docs\architecture.md)

Что делает
----------
Берёт папки dataN коллекции (Pictures\Anime\dataN, по 500 картинок — их готовит проект anime-vault),
прогоняет каждую картинку через цепочку этапов-«определителей» и раскладывает по папкам «Тайтл\Персонаж».
Этапы, их порядок, модели, пороги и какие файлы достаются каждому этапу — в configs\template.json, без правки кода.
Результат каждого набора — в test-dataN\ (in / work / out / logs / cache), готовое вливается в Waifu.

Порядок работы
--------------
0. Новые картинки: anime-vault → download.bat (Pinterest), distribute.bat (раскладка в dataN).
1. prepare.bat  — (по желанию) агент конфигов пересобирает шаблоны под живые API; проверка ключей и моделей,
                  поиск необработанных папок, сборка пачек configN.json (у каждой пачки свой первый ключ и свой
                  порядок AI-этапов), запуск.
2. Главное окно (start.vbs) — журнал всех пачек, статистика, кнопки окон; окна наборов — лог и таблица этапов
   и кнопки набора: ← назад к прошлому этапу, → пропустить этап, ↻ проверить API сейчас, ↔ сменить провайдера.
3. finish.bat   — слияние в Waifu → мелкие тайтлы (≤15 картинок: персонажей — в большие тайтлы, остальное
                  в «Other») → нумерация (шаги можно пропускать).
   Агент имён (дубли тайтлов/персонажей → names.json) — вручную: venv\Scripts\python tools\agent\names_agent.py
4. other.bat    — всё из «Other» на второй круг (dataN-other, configs\template-other.json: 6 AI-этапов,
                  запасные модели, соседи по общему порядку всех наборов) и снова prepare.

Где что
-------
  prepare.bat, finish.bat, other.bat, start.vbs   запуск (двойной щелчок)
  configs\     template.json, template-other.json (шаблоны пачек), config.json (коллекция, подписи этапов, окно), live.json (на ходу),
               agent.json (агенты, только OpenRouter), tools.json (параметры инструментов); configN.json пишет prepare
  providers\   провайдеры API: адрес и модели (provider.json); в git только openrouter и example
  secrets\     КЛЮЧИ — не в git: secrets\providers\<провайдер>\<ключ>.txt, secrets\agent\openrouter.txt (только агенты)
  engine\      движок: проверка конфигов, пачка, набор, конвейер, клиент API с переключением, окна, значки папок
  stages\      этапы — по файлу на тип этапа
  gui\         главное окно (журнал + статистика + кнопки) и окно набора (лог + таблица этапов)
  tools\       инструменты: add (слияние), fix_name (+regroup, names.json), sort, watch,
               agent (агент имён и агент конфигов), cleanup, mark_broken,
               prepare / finish / other, probe (проверка API), console (окна инструментов)
  assets\      значок «готово» для папок
  tests\       тесты (venv\Scripts\python -m pytest)
  docs\        документация и скриншоты
  logs\        журналы, в том числе other_*.json (не в git)
  venv\        окружение Python

В каждой папке свой README.txt с подробностями.
