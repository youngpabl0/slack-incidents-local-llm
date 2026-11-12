# slack-incidents-local-llm

Код к статье "Три года инцидентов в Slack и локальная нейросеть" (журнал "Системный администратор", рубрика "SRE и нейросети"): выгрузка Slack только на чтение и разбор обсуждений локальной моделью.

## Что нужно

- Node.js 22 или новее (нужен встроенный WebSocket).
- Python 3.9 или новее, сторонние пакеты не нужны.
- [Ollama](https://ollama.com) и модель эмбеддингов bge-m3: `ollama pull bge-m3`.
- GGUF модели Qwen3-14B в квантовании Q5_K_M. Положите файл рядом с `Modelfile.qwen3` под именем `Qwen3-14B-Q5_K_M.gguf` и соберите модель:

```
ollama create qwen3-14b-local -f Modelfile.qwen3
```

- Chrome с портом отладки и вкладкой Slack, в которой вы залогинены. Chrome не открывает порт отладки на основном профиле, поэтому профиль отдельный:

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.slack-export-chrome" \
  https://app.slack.com/client
```

## Выгрузка только на чтение

`slack_export.mjs` подключается к вкладке через Chrome DevTools Protocol и вызывает API Slack из неё, от вашего имени. Токен сессии берётся из localStorage вкладки и уходит только в API Slack. Скрипт может вызвать только методы из списка `READ_ONLY`, на любой другой он останавливается:

```
auth.test, search.messages, users.list, users.info, bots.info,
users.conversations, conversations.list, conversations.history, conversations.replies
```

## Команды

```
D=./slack-export/$(date +%F)

# 1. Выгрузка: все треды, где вы писали
node slack_export.mjs --out $D

# Всё, где вы участник (каналы, приватные каналы, личные сообщения), с указанной даты.
# Каналы со словами alert, incident, devops, prod, ops, support, release идут первыми, личные сообщения последними.
caffeinate -is node slack_export.mjs --all --since 2023-11-01 --out $D
# продолжить прерванный запуск без повторного поиска
caffeinate -is node slack_export.mjs --all --since 2023-11-01 --out $D --reuse-index
# без личных сообщений
node slack_export.mjs --all --no-dm --since 2023-11-01 --out $D

# 2. Быстрое ранжирование по словам, без модели
python3 rank_threads.py $D

# 3. Умное ранжирование: два фильтра, потом модель читает кандидатов, потом группы инцидентов
python3 smart_rank.py $D --candidates 600 --about "торговая онлайн-платформа"
# пробный прогон: модель читает 20 обсуждений, без групп
python3 smart_rank.py $D --limit 20 --themes 0

# 4. Только группировка, по готовому рейтингу, без повторного чтения
python3 smart_rank.py --regroup ./slack-export/smart-ranking-<дата>.jsonl --cache-dir $D --out-dir ./slack-export
# первый вариант, по косинусной близости описаний: на коротких описаниях склеивает почти всё в одну группу
python3 smart_rank.py --regroup ./slack-export/smart-ranking-<дата>.jsonl --cache-dir $D --group-by cosine --thr 0.65

# 5. Показать треды за строками рейтинга, чтобы проверить пересказ модели
python3 show_threads.py $D ./slack-export/smart-ranking-<дата>.jsonl 1 2 3

# Ночной прогон: ранжирует то, что уже выгружено, ждёт конца выгрузки (не дольше 07:40), ранжирует снимок
./run_overnight.sh $D ./slack-export 0740
```

## Скорость

Второй экземпляр Ollama с двумя параллельными запросами (две копии 14B в 24 ГБ не помещаются, выгрузите её из основного):

```
OLLAMA_HOST=127.0.0.1:11435 OLLAMA_NUM_PARALLEL=2 \
OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve

python3 smart_rank.py $D --ollama http://127.0.0.1:11435 --workers 2 --budget 6000
```

## Группы инцидентов

Третья стадия `smart_rank.py` идёт только по инцидентам. Модель получает не тред, а свои же короткие поля из второй стадии (`theme`, `summary`, `root_cause`) и выбирает `system` и `cause` из закрытых списков. Списки заданы как enum в JSON-схеме, за схемой следит Ollama. Сами списки лежат в начале третьей стадии (`SYSTEMS`, `CAUSES`), поправьте их под свою систему. Дальше обычный код: словарь по паре `(system, cause)`, группы сортируются по весу, модель называет и кратко описывает верхние.

## Что появится на диске

В папке выгрузки `$D`:

- `threads.jsonl`: треды, где вы писали;
- `channels/<канал>.jsonl`: сообщения канала с тредами, `.done` у выгруженных;
- `threads_outside.jsonl`: ваши треды в каналах, из которых вы вышли (с `--all`);
- `conversations.json`, `users.json`, `summary.json`: справочники;
- `semantic_cache.jsonl`, `smart_cache_v3.jsonl`, `group_cache_v1.jsonl`: кэши векторов и ответов модели, повторный запуск не спрашивает модель второй раз.

Рядом с выгрузкой (или в `--out-dir`):

- `ranking-<дата>.md` и `.jsonl` от `rank_threads.py`;
- `smart-ranking-<дата>.md` и `.jsonl` от `smart_rank.py`;
- `groups-<дата>.md` (или `themes-<дата>.md` с `--group-by cosine`).

## Важно

Выгрузка содержит внутренние данные компании: переписку, имена, адреса, куски логов. Перед выгрузкой убедитесь, что правила вашей компании это разрешают. Держите данные на своей машине и не коммитьте их (`.gitignore` уже исключает `slack-export/` и `*.jsonl`). Не отправляйте их в облачные модели, детекторы и переводчики: `smart_rank.py` ходит только к Ollama на 127.0.0.1. Пересказ модели это указатель, а не факт: прежде чем рассказывать историю, откройте тред по ссылке.
