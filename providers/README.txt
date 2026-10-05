providers — провайдеры API
==========================

Папка = провайдер, в ней provider.json (адрес и модели). КЛЮЧИ лежат отдельно и в git не попадают:
    secrets\providers\<провайдер>\<ключ>.txt     одна строка — ключ
В конфиге этап ссылается на ключ так: "api": "<провайдер>/<ключ>"  (например "openrouter/key").
Агенты (tools\agent) ходят только в OpenRouter со своим ключом secrets\agent\openrouter.txt.

В репозитории только openrouter\ и example\ (образец любого OpenAI-совместимого API); остальные провайдеры —
частные шлюзы, они лежат только локально (.gitignore).

provider.json
    {
      "endpoint": "https://.../v1/chat/completions",   адрес (формат OpenAI chat/completions)
      "auth": "Bearer",                                 как передаётся ключ
      "reasoning": "none",        бюджет рассуждений Claude-моделей: anthropic (thinking), openrouter (reasoning) или none
      "interval": 1.0,            секунд между запросами (лимит провайдера)
      "models": {"kimi-k3": "moonshotai/kimi-k3"},   имя в конфиге: имя модели у провайдера
      "images": false,            (необязательно) провайдер не принимает картинки — только для json_meta
      "temperature": false        (необязательно) не отправлять temperature
    }
Новая модель — строка в "models": модели, которых там нет, проверка конфигов не пропустит.
Проверить, что ключи живы и модели видят картинку: prepare.bat (или venv\Scripts\python tools\prepare.py).
