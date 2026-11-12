#!/usr/bin/env python3
# Usage:
#   python3 smart_rank.py <export-dir> [--candidates 700] [--limit N] [--themes 10] [--group-by pairs|cosine] [--thr 0.65]
#                         [--about "торговая онлайн-платформа"] [--model qwen3-14b-local] [--emb bge-m3]
#                         [--budget 6000] [--workers 1] [--ollama http://127.0.0.1:11434] [--keep-alive 0] [--out-dir <folder>]
#   python3 smart_rank.py --regroup <smart-ranking-<date>.jsonl> [--cache-dir <export-dir>] [--out-dir <folder>]
#                         [--themes 10] [--group-by pairs|cosine] [--about ...] [--model ...] [--workers 1] [--ollama ...] [--keep-alive 0]
# Smart ranking of Slack discussions with local models only (Ollama on 127.0.0.1; nothing leaves the machine):
#  1. prefilter: keyword score from slack_units fused with bge-m3 similarity to incident descriptions (reciprocal rank fusion);
#  2. the local LLM reads each candidate and returns JSON: incident or not, who noticed, whether monitoring stayed green,
#     the user's role, cause, fix, lesson, story score. Cached in <export-dir>/smart_cache_v3.jsonl, so a rerun resumes;
#  3. groups (default --group-by pairs): a second, cheap LLM pass over the incidents only. The model gets the short fields
#     it wrote in stage 2 (theme, summary, root_cause), not the thread, and picks `system` and `cause` from closed lists
#     (enums in the JSON schema, enforced by Ollama). Cached in <export-dir>/group_cache_v1.jsonl. The incidents are then
#     grouped in plain code by the pair (system, cause), and the LLM names and summarises the top groups: groups-<date>.md.
#     --group-by cosine: the first attempt, greedy grouping of the summaries by bge-m3 cosine (--thr 0.65): themes-<date>.md.
#     On short summaries of one platform it glues most incidents into one big group.
# --limit N stops after N new LLM calls in stage 2 (a test run). --themes 0 skips stage 3.
# --regroup runs only stage 3 on an existing ranking file; --cache-dir is where group_cache_v1.jsonl lives (default: next to it).
# Results go to --out-dir (default: next to the export folder). Internal data: do not publish the results.
import hashlib, json, math, os, re, sys, threading, time, urllib.request, urllib.error
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

A = sys.argv
opt = lambda k, d: type(d)(A[A.index(k) + 1]) if k in A else d
K, LIMIT, NTHEMES, THR = opt('--candidates', 700), opt('--limit', 10 ** 9), opt('--themes', 10), opt('--thr', 0.65)
GROUP_BY = opt('--group-by', 'pairs')
ABOUT = opt('--about', 'торговая онлайн-платформа')
MODEL, EMB = opt('--model', 'qwen3-14b-local'), opt('--emb', 'bge-m3')
BUDGET, WORKERS = opt('--budget', 6000), opt('--workers', 1)  # chars of thread text per call; parallel calls (the server needs OLLAMA_NUM_PARALLEL >= workers)
OLLAMA = opt('--ollama', 'http://127.0.0.1:11434')
# Memory: the 14B Q5_K_M model takes about 10 GB once loaded. Between calls of one run Ollama keeps the models loaded
# for RUN_KEEP; when the run ends the script passes --keep-alive to Ollama as keep_alive (default 0: unload at once).
RUN_KEEP, KEEP_ALIVE = '5m', opt('--keep-alive', '0')
REGROUP = opt('--regroup', '')
if GROUP_BY not in ('pairs', 'cosine'): sys.exit('--group-by is pairs or cosine')
if REGROUP:
    REGROUP = os.path.abspath(os.path.expanduser(REGROUP))
    EXP = os.path.abspath(os.path.expanduser(opt('--cache-dir', os.path.dirname(REGROUP))))
    m = re.search(r'\d{4}-\d{2}-\d{2}', os.path.basename(REGROUP))
    DATE = m.group(0) if m else os.path.splitext(os.path.basename(REGROUP))[0]
    out_default = os.path.dirname(REGROUP)
else:
    if len(A) < 2 or A[1].startswith('--'): sys.exit('usage: python3 smart_rank.py <export-dir> [options] | --regroup <ranking.jsonl> [options]')
    EXP = os.path.abspath(os.path.expanduser(A[1].rstrip('/')))
    DATE = os.path.basename(EXP)
    out_default = os.path.dirname(os.path.dirname(EXP)) if os.path.basename(os.path.dirname(EXP)) == 'raw' else os.path.dirname(EXP)
BASE = os.path.abspath(os.path.expanduser(opt('--out-dir', out_default)))
os.makedirs(BASE, exist_ok=True)
STATS = {'prompt_tok': 0, 'prompt_s': 0.0, 'gen_tok': 0, 'gen_s': 0.0}


def post(path, body, timeout=900):
    req = urllib.request.Request(OLLAMA + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r: return json.load(r)


def unit_vec(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


dot = lambda a, b: sum(x * y for x, y in zip(a, b))


def embed(texts):
    out = []
    for i in range(0, len(texts), 32):
        out += post('/api/embed', {'model': EMB, 'input': texts[i:i + 32], 'keep_alive': RUN_KEEP})['embeddings']
    return [unit_vec(v) for v in out]


def chat(system, user, schema, num_predict=600):
    err = None
    for _ in range(3):
        try:
            r = post('/api/chat', {'model': MODEL, 'stream': False, 'format': schema, 'keep_alive': RUN_KEEP,
                                   'options': {'num_ctx': 8192, 'temperature': 0.1, 'num_predict': num_predict},
                                   'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]})
            STATS['prompt_tok'] += r.get('prompt_eval_count', 0); STATS['prompt_s'] += r.get('prompt_eval_duration', 0) / 1e9
            STATS['gen_tok'] += r.get('eval_count', 0); STATS['gen_s'] += r.get('eval_duration', 0) / 1e9
            return json.loads(r['message']['content'])
        except (ValueError, KeyError, urllib.error.URLError, TimeoutError) as e: err = e
    return {'error': str(err)[:200]}


def cache(path):
    d = {}
    if os.path.exists(path):
        for l in open(path, encoding='utf-8'):
            try: x = json.loads(l); d[x['k']] = x['v']
            except ValueError: pass
    return d, open(path, 'a', encoding='utf-8')


def speed():
    return f"prompt {STATS['prompt_tok'] / max(STATS['prompt_s'], 1e-9):.0f} tok/s, gen {STATS['gen_tok'] / max(STATS['gen_s'], 1e-9):.0f} tok/s"


cell = lambda s: str(s).replace('|', '/').replace('\n', ' ')
ck = lambda u: f"{u['key']}:{u['n']}"


def smart(r):
    if 'error' in r: return -1
    if r['detected_by'] == 'alert': r['monitoring_was_green'] = False  # the model sometimes says both
    s = r['story_score'] * 3 + r['severity'] * 3 + (6 if r['monitoring_was_green'] else 0) \
        + {'customer_or_support': 4, 'engineer': 3}.get(r['detected_by'], 0) + {'led_fix': 6, 'helped': 3}.get(r.get('my_role'), 0) \
        + {'trading_or_money': 4, 'many_users': 3, 'some_users': 2}.get(r['user_impact'], 0)
    return round(s if r['is_prod_incident'] else s * 0.3, 1)


# 1-2. prefilter and LLM reading
PROTOS = [
    'На проде авария: пользователи не могут торговать или не видят данные, а алерты молчали',
    'Клиенты пишут в поддержку, что не работает, хотя мониторинг зелёный и дашборды в порядке',
    'Инцидент на продакшене: нашли причину, откатили релиз, сделали хотфикс и разбор',
    'Котировки или графики перестали обновляться, стрим пустой, данные от биржи не приходят',
    'Сертификат, DNS, балансировщик или деплой сломали сервис, заметили не сразу',
    'Production outage that customers noticed before our alerts did, root cause and fix',
]
SCHEMA = {'type': 'object', 'properties': {
    'is_prod_incident': {'type': 'boolean'},
    'user_impact': {'type': 'string', 'enum': ['none', 'internal', 'some_users', 'many_users', 'trading_or_money']},
    'detected_by': {'type': 'string', 'enum': ['alert', 'engineer', 'customer_or_support', 'unknown']},
    'monitoring_was_green': {'type': 'boolean'},
    'severity': {'type': 'integer', 'minimum': 0, 'maximum': 3},
    'my_role': {'type': 'string', 'enum': ['led_fix', 'helped', 'observer', 'absent']},
    'theme': {'type': 'string'}, 'summary': {'type': 'string'}, 'root_cause': {'type': 'string'}, 'lesson': {'type': 'string'},
    'story_score': {'type': 'integer', 'minimum': 0, 'maximum': 10}}}
SCHEMA['required'] = list(SCHEMA['properties'])


def rank():
    from slack_units import load, ts2s
    meta, units = load(EXP)
    my = meta['my_name']
    units = [u for u in units if u['humans'] >= 2 or u['my'] >= 1]
    print(f'{len(units)} discussions with people in them', flush=True)

    sims, fsim = cache(os.path.join(EXP, 'semantic_cache.jsonl'))
    todo = [u for u in units if ck(u) not in sims]
    if todo:
        pv = embed(PROTOS)
        t0 = time.time()
        for i in range(0, len(todo), 256):
            part = todo[i:i + 256]
            texts = [f"#{u['channel_name']}\n" + '\n'.join((x.get('text') or '')[:300] for x in u['msgs'] if not x.get('bot'))[:1200] for u in part]
            for u, v in zip(part, embed(texts)):
                sims[ck(u)] = round(max(dot(v, p) for p in pv), 4)
                fsim.write(json.dumps({'k': ck(u), 'v': sims[ck(u)]}) + '\n')
            fsim.flush()
            print(f'embedded {i + len(part)}/{len(todo)} ({time.time() - t0:.0f}s)', flush=True)
    fsim.close()
    by_kw = sorted(units, key=lambda u: -u['score'])
    by_sem = sorted(units, key=lambda u: -sims[ck(u)])
    rrf = {}
    for ranked in (by_kw, by_sem):
        for r, u in enumerate(ranked): rrf[u['key']] = rrf.get(u['key'], 0) + 1 / (60 + r)
    cands = sorted(units, key=lambda u: -rrf[u['key']])[:K]
    print(f'candidates for the LLM: {len(cands)} of {len(units)} (dropped {len(units) - len(cands)} with the lowest fused rank)', flush=True)

    system = f"""Ты помогаешь SRE-инженеру (в Slack: {my}, его сообщения помечены [я]) выбрать истории для рассказа о надёжности и о мониторинге, который показывал "всё хорошо", когда что-то было сломано. Компания: {ABOUT}. Тебе дают одно обсуждение из рабочего Slack. Заполни JSON строго по тексту обсуждения, ничего не выдумывай.
Поля:
- is_prod_incident: true, только если на продакшене что-то реально сломалось или деградировало (не тест, не стейдж, не плановая задача, не вопрос).
- user_impact: none | internal (задело только команду) | some_users | many_users | trading_or_money (торговля, ордера, деньги клиентов).
- detected_by: alert (сработал алерт) | engineer (инженер заметил сам) | customer_or_support (клиенты, поддержка, менеджеры) | unknown.
- monitoring_was_green: true только если мониторинг не помог увидеть проблему: алерта не было (или он не дошёл до людей), проверки и дашборды показывали норму, а заметили люди (инженеры, клиенты, поддержка). Если проблему обнаружил сработавший алерт, ставь false.
- severity: 0 нет проблемы, 1 мелочь, 2 заметный инцидент, 3 крупная авария.
- my_role: роль инженера, чьи сообщения помечены [я]: led_fix (нашёл причину или починил) | helped | observer | absent.
- theme: техническая область в 2-4 словах по-русски, о чём именно поломка (например "таймауты базы данных", "TLS-сертификаты", "деплой и откат", "поток котировок", "service discovery", "лимиты ресурсов"). Не пиши общих слов вроде "мониторинг", "алерты", "инцидент".
- summary: 1-3 предложения по-русски: что сломалось, как заметили, причина, как починили.
- root_cause: причина коротко или "неизвестно".
- lesson: что изменили после (алерт, проверка, процесс) или "нет".
- story_score: 0-10, насколько это хорошая история для рассказа: неочевидная причина, мониторинг врал или молчал, понятный урок, инженер с пометкой [я] участвовал. Рутина и мелочи 0-2."""

    def render(u, budget=BUDGET):
        lines = []
        for x in u['msgs']:
            t = (x.get('text') or '').strip()
            if not t: continue
            who = (x.get('user') or '?') + (' (бот)' if x.get('bot') else '') + (' [я]' if x.get('user') == my else '')
            lines.append(f"[{ts2s(x['ts'], '%Y-%m-%d %H:%M')}] {who}: {t[:200 if x.get('bot') else 700]}")
        if sum(map(len, lines)) > budget and len(lines) > 35:
            mid = lines[25:-10]
            keep = [l for l in mid if '[я]' in l]
            lines = lines[:25] + [f'[... пропущено {len(mid) - len(keep)} сообщений ...]'] + keep + lines[-10:]
        text = '\n'.join(lines)
        return text if len(text) <= budget else text[:budget * 2 // 3] + '\n[...]\n' + text[-budget // 3:]

    # the version is part of the cache file name: change it when the prompt or the schema changes
    llm, fllm = cache(os.path.join(EXP, 'smart_cache_v3.jsonl'))
    todo = [u for u in cands if ck(u) not in llm][:LIMIT]
    t0 = time.time(); lock = threading.Lock(); done = [0]

    def read_one(u):
        r = chat(system, f"Канал: #{u['channel_name']} ({'тред' if u['kind'] == 'thread' else 'переписка'}, {u['date']}, сообщений: {u['n']})\n\n{render(u)}", SCHEMA)
        with lock:
            llm[ck(u)] = r; done[0] += 1; i = done[0]
            fllm.write(json.dumps({'k': ck(u), 'v': r}, ensure_ascii=False) + '\n'); fllm.flush()
            if i % 10 == 0 or i == len(todo):
                el = time.time() - t0
                print(f'llm {i}/{len(todo)}, {el / i:.1f}s each, eta {el / i * (len(todo) - i) / 60:.0f} min ({speed()})', flush=True)
    with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(read_one, todo))
    fllm.close()
    rows = []
    for u in units:  # every discussion the model has read, also from earlier runs whose candidates were different
        r = llm.get(ck(u))
        if r is None: continue
        rows.append({'smart': smart(r), 'key': u['key'], 'date': u['date'], 'channel': u['channel_name'], 'kind': u['kind'], 'n': u['n'],
                     'my': u['my'], 'kw_score': u['score'], 'sem': sims[ck(u)], 'link': u['link'], 'llm': r})
    rows.sort(key=lambda x: -x['smart'])
    with open(os.path.join(BASE, f'smart-ranking-{DATE}.jsonl'), 'w', encoding='utf-8') as fo:
        for x in rows: fo.write(json.dumps(x, ensure_ascii=False) + '\n')
    with open(os.path.join(BASE, f'smart-ranking-{DATE}.md'), 'w', encoding='utf-8') as fo:
        fo.write(f'# Slack, умное ранжирование {DATE} (локально, {MODEL})\n\nПрочитано моделью: {len(rows)} обсуждений (текущие {len(cands)} кандидатов плюс прочитанные в прошлых прогонах) из {len(units)}. Внутренние данные, не публиковать.\n\n')
        fo.write('| # | балл | дата | канал | роль | кто заметил | мониторинг молчал | тема | что было |\n|---|---|---|---|---|---|---|---|---|\n')
        for i, x in enumerate(rows[:150], 1):
            r = x['llm']
            if 'error' in r: continue
            fo.write(f"| {i} | {x['smart']} | [{x['date']}]({x['link']}) | {cell(x['channel'])} | {r.get('my_role')} | {r['detected_by']} | {'да' if r['monitoring_was_green'] else ''} | {cell(r['theme'])} | {cell(r['summary'])} |\n")
    print('ranked', len(rows), '->', os.path.join(BASE, f'smart-ranking-{DATE}.md'), flush=True)
    left = sum(1 for u in cands if ck(u) not in llm)
    if left: print(f'{left} candidates not read yet: rerun to continue', flush=True)
    return rows


# 3. groups
# Closed lists for the second pass. Generic names: edit them to fit your own system.
SYSTEMS = {
    'database': 'база данных или хранилище: SQL, NoSQL, time series, поиск, кэш',
    'service_discovery': 'service discovery и реестр сервисов, ACL и конфигурация в нём',
    'orchestrator': 'оркестратор и планировщик: размещение задач, рестарты, ноды кластера',
    'deploy_release': 'деплой, релиз, CI/CD, откат',
    'market_data': 'поток рыночных данных: котировки, свечи, сделки, данные бирж и поставщиков данных',
    'broker_integration': 'интеграции с брокерами и торговые операции: ордера, счета, подключение брокера',
    'app_backend': 'код самого продукта: бэкенд, API, фронтенд, расчёты',
    'auth': 'вход и доступ пользователей: аутентификация, токены, сессии',
    'network': 'сеть: DNS, TLS, VPN, связность между серверами',
    'proxy_gateway': 'балансировщик, прокси, API-шлюз, CDN',
    'queue_streaming': 'очереди и стриминг событий: Kafka, шина событий, очереди задач',
    'monitoring': 'мониторинг, алерты, логи и метрики',
    'cloud_provider': 'облачный провайдер и его сервисы',
    'other': 'ничего из перечисленного',
}
CAUSES = {
    'config_change': 'изменили конфигурацию, настройки, права или лимиты',
    'release_bug': 'ошибка в коде или в новом релизе',
    'resource_exhaustion': 'кончились ресурсы: память, диск, CPU, соединения, OOM, утечка',
    'load_capacity': 'выросла нагрузка или не хватило мощности',
    'dependency_failure': 'отказала внешняя система или соседний сервис, от которого мы зависим',
    'cert_or_secret_expired': 'истёк сертификат, токен, ключ или секрет',
    'network': 'сетевой сбой: связность, DNS, сетевые таймауты',
    'node_failure': 'отказ ноды, диска, железа или виртуальной машины',
    'human_error': 'ручная ошибка при операции',
    'unknown': 'причина в описании не названа',
}
GSYSTEM = ('Тебе дают краткое описание одного инцидента: тему, что случилось и причину. Описание уже составлено по обсуждению, '
           'само обсуждение ты не видишь. Выбери два значения строго из списков, ничего не выдумывай.\n'
           'system: какая часть инфраструктуры сломалась или деградировала.\n'
           + '\n'.join(f'- {k}: {v}' for k, v in SYSTEMS.items()) +
           '\ncause: класс причины.\n'
           + '\n'.join(f'- {k}: {v}' for k, v in CAUSES.items()) +
           '\nЕсли отказал внешний поставщик, system это наша часть, которая от него зависит, а cause = dependency_failure. '
           'Если причина "неизвестно" или не названа, cause = unknown.')
GSCHEMA = {'type': 'object', 'properties': {'system': {'type': 'string', 'enum': list(SYSTEMS)}, 'cause': {'type': 'string', 'enum': list(CAUSES)}},
           'required': ['system', 'cause']}
TSCHEMA = {'type': 'object', 'properties': {'title': {'type': 'string'}, 'summary': {'type': 'string'},
                                            'best_examples': {'type': 'array', 'items': {'type': 'integer'}}},
           'required': ['title', 'summary', 'best_examples']}
TSYSTEM = f'Ты описываешь группы инцидентов ({ABOUT}) для рассказа о надёжности и о мониторинге, который показывал "всё хорошо", когда было сломано. Пиши только по данным, ничего не выдумывай.'
weight = lambda c: sum(sorted((x['smart'] for x in c['items']), reverse=True)[:3]) + 2 * len(c['items'])


def row_key(x):
    """Cache key of an incident: discussion key, message count and a hash of the fields the second pass reads."""
    base = x.get('key')
    if not base:  # ranking files without a key field: take channel and ts from the link
        m = re.search(r'/archives/([^/]+)/p(\d+)(\d{6})$', x.get('link') or '')
        base = f'{m.group(1)}:{m.group(2)}.{m.group(3)}' if m else x.get('link', '')
    h = hashlib.sha1('\n'.join(str(x['llm'].get(f, '')) for f in ('theme', 'summary', 'root_cause')).encode()).hexdigest()[:10]
    return f"{base}:{x['n']}:{h}"


def classify(inc):
    got, fo = cache(os.path.join(EXP, 'group_cache_v1.jsonl'))
    todo = [x for x in inc if row_key(x) not in got]
    for k in STATS: STATS[k] = type(STATS[k])()
    t0 = time.time(); lock = threading.Lock(); done = [0]

    def one(x):
        r = x['llm']
        a = chat(GSYSTEM, f"Тема: {r.get('theme', '')}\nЧто было: {r.get('summary', '')}\nПричина: {r.get('root_cause', '')}", GSCHEMA, 40)
        with lock:
            done[0] += 1; i = done[0]
            if 'error' not in a:  # errors are not cached, a rerun asks again
                got[row_key(x)] = a; fo.write(json.dumps({'k': row_key(x), 'v': a}, ensure_ascii=False) + '\n'); fo.flush()
            if i % 25 == 0 or i == len(todo):
                el = time.time() - t0
                print(f'group pass {i}/{len(todo)}, {el / i:.1f}s each ({speed()})', flush=True)
    with ThreadPoolExecutor(WORKERS) as ex: list(ex.map(one, todo))
    fo.close()
    if todo:
        el = time.time() - t0
        print(f'group pass done: {len(todo)} new answers in {el / 60:.1f} min, {el / len(todo):.1f}s per incident', flush=True)
    return got


def describe(items):
    listing = '\n'.join(f"{i}. [{x['date']}, #{x['channel']}, моя роль: {x['llm'].get('my_role', '?')}] {x['llm']['summary']} Причина: {x['llm']['root_cause'].rstrip('.')}. После: {x['llm']['lesson'].rstrip('.')}." for i, x in enumerate(items, 1))
    t = chat(TSYSTEM, f'Инциденты одной группы:\n{listing}\n\nДай: title (3-6 слов по-русски), summary (3-5 предложений по-русски: общий паттерн, что ломалось, как замечали, что поменяли), best_examples (номера 1-3 лучших историй).', TSCHEMA, 700)
    return t if 'error' not in t else {'title': items[0]['llm']['theme'], 'summary': '', 'best_examples': [1]}


def write_group(fo, n, c, head):
    items = sorted(c['items'], key=lambda x: -x['smart'])[:12]
    t = describe(items)
    fo.write(f"## {n}. {t['title']} ({head}{len(c['items'])} инцид., вес {weight(c):.0f})\n\n{t['summary']}\n\nЛучшие: {', '.join(map(str, t['best_examples']))}\n\n")
    for i, x in enumerate(items[:8], 1):
        r = x['llm']
        fo.write(f"{i}. [{x['date']}]({x['link']}) #{x['channel']}, балл {x['smart']}, роль {r.get('my_role', '?')}, заметил: {r['detected_by']}{', мониторинг молчал' if r['monitoring_was_green'] else ''}. {r['summary']} Причина: {r['root_cause'].rstrip('.')}. После: {r['lesson'].rstrip('.')}.\n")
    fo.write('\n')
    print(f'group {n}: {t["title"]}', flush=True)


def group(rows):
    inc = [x for x in rows if 'error' not in x['llm'] and x['llm'].get('is_prod_incident') and x['smart'] > 0]
    if not inc: return
    themes = Counter(x['llm'].get('theme', '').strip() for x in inc)
    print(f'incidents: {len(inc)}, distinct theme strings: {len(themes)}, incidents with a theme nobody else has: {sum(1 for x in inc if themes[x["llm"].get("theme", "").strip()] == 1)}', flush=True)
    if GROUP_BY == 'cosine':  # the first attempt, kept to reproduce it: the top 250 incidents, greedy grouping by cosine
        inc = inc[:250]
        vecs = embed([f"{x['llm']['theme']}: {x['llm']['summary']}" for x in inc])
        clusters = []
        for x, v in zip(inc, vecs):
            best = max(clusters, key=lambda c: dot(unit_vec(c['v']), v), default=None)
            if best and dot(unit_vec(best['v']), v) >= THR:
                best['items'].append(x); best['v'] = [a + b for a, b in zip(best['v'], v)]
            else: clusters.append({'v': list(v), 'items': [x]})
        clusters.sort(key=lambda c: -weight(c))
        path = os.path.join(BASE, f'themes-{DATE}.md')
        with open(path, 'w', encoding='utf-8') as fo:
            fo.write(f'# Темы из Slack {DATE}: топ-{NTHEMES} (локально, {MODEL})\n\nИнцидентов в группировке: {len(inc)}, групп: {len(clusters)}, порог близости {THR}. Внутренние данные, не публиковать.\n\n')
            for n, c in enumerate(clusters[:NTHEMES], 1): write_group(fo, n, c, '')
        print('themes ->', path, flush=True)
        return
    got = classify(inc)
    groups = defaultdict(list)
    for x in inc:
        a = got.get(row_key(x))
        if a: groups[(a['system'], a['cause'])].append(x)
    clusters = sorted(({'key': k, 'items': v} for k, v in groups.items()), key=lambda c: -weight(c))
    grouped = sum(len(c['items']) for c in clusters)
    sys_n = Counter(); cause_n = Counter()
    for c in clusters: sys_n[c['key'][0]] += len(c['items']); cause_n[c['key'][1]] += len(c['items'])
    path = os.path.join(BASE, f'groups-{DATE}.md')
    with open(path, 'w', encoding='utf-8') as fo:
        fo.write(f'# Группы инцидентов из Slack {DATE} по паре (system, cause) (локально, {MODEL})\n\n'
                 f'Инцидентов: {len(inc)}, разложено по полкам: {grouped}, без ответа модели: {len(inc) - grouped}. '
                 f'Разных строк theme среди них: {len(themes)}. Непустых групп: {len(clusters)}. Внутренние данные, не публиковать.\n\n')
        fo.write('| # | system | cause | инцидентов | вес |\n|---|---|---|---|---|\n')
        for n, c in enumerate(clusters, 1): fo.write(f"| {n} | {c['key'][0]} | {c['key'][1]} | {len(c['items'])} | {weight(c):.0f} |\n")
        fo.write('\nПо system: ' + ', '.join(f'{k} {v}' for k, v in sys_n.most_common()) + '\n\nПо cause: ' + ', '.join(f'{k} {v}' for k, v in cause_n.most_common()) + '\n\n')
        for n, c in enumerate(clusters[:NTHEMES], 1): write_group(fo, n, c, f'{c["key"][0]} / {c["key"][1]}, ')
    print(f'groups: {len(clusters)} from {grouped} incidents -> {path}', flush=True)


def release():
    """Pass --keep-alive to Ollama for both models: with the default 0 they leave memory as soon as the run is over."""
    for path, body in (('/api/generate', {'model': MODEL}), ('/api/embed', {'model': EMB, 'input': []})):
        try: post(path, {**body, 'keep_alive': KEEP_ALIVE}, timeout=60)
        except (urllib.error.URLError, TimeoutError, ValueError): pass


try:
    if REGROUP:
        rows = [json.loads(l) for l in open(REGROUP, encoding='utf-8') if l.strip()]
        print(f'{len(rows)} rows from {REGROUP}', flush=True)
    else:
        rows = rank()
    if NTHEMES > 0: group(rows)
finally:
    release()
