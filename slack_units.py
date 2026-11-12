# Shared loader for slack_export.mjs folders. A "unit" is one discussion: a thread (root + replies) or a burst of
# top-level chat in one conversation (messages without threads, less than 30 minutes apart). Text is cleaned of Slack
# markup, the user's own messages are counted, and keyword signals are attached. Used by rank_threads.py and smart_rank.py.
import json, os, re, glob, datetime, math

OUTAGE = re.compile(r'(?i)(упал|упало|упали|лежит|лёг|легл|не работа|недоступ|outage|\bdown\b|\b50[234]\b|5xx|timeout|таймаут|деград|инцидент|incident|авари|сломал|broke|не открыва|не грузит|зависл|freez|замёрз|stuck|hang)')
INVISIBLE = re.compile(r'(?i)(алерт\w*\s+(не|нет)|не было алерт|без алерт|не сработал|мониторинг\w*\s+(не|молч)|молчал|не заметил|заметили (только|поздно)|узнали (от|из)|пишут (клиент|пользовател|юзер)|клиенты пишут|пользователи пишут|жалоб|саппорт|support|тикет от|user report|customers? (report|complain)|from (users|customers)|nobody noticed|no alert|alert didn.?t|didn.?t fire|silent)')
FIX = re.compile(r'(?i)(причин|root cause|\brca\b|постмортем|postmortem|post-mortem|откат|откатил|rollback|roll back|hotfix|хотфикс|починил|fixed|поправил)')


def jsonl(path):
    out = []
    for l in open(path, encoding='utf-8'):
        try: out.append(json.loads(l))
        except ValueError: pass  # a line still being written by a running export
    return out


def ts2s(ts, fmt='%Y-%m-%d'):
    try: return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime(fmt)
    except (TypeError, ValueError): return '?'


def cleaner(users):
    def clean(t):
        t = re.sub(r'<@(U\w+)(\|[^>]*)?>', lambda m: '@' + users.get(m.group(1), m.group(1)), t or '')
        t = re.sub(r'<#C\w+\|([^>]*)>', r'#\1', t)
        t = re.sub(r'<(https?://[^|>]+)\|([^>]+)>', r'\2', t)
        t = re.sub(r'<(https?://[^>]+)>', r'\1', t)
        t = re.sub(r'<!(here|channel)[^>]*>', r'@\1', t)
        return t.replace('&gt;', '>').replace('&lt;', '<').replace('&amp;', '&')
    return clean


def load(exp, gap_min=30):
    """Returns (meta, units). meta: team, my_name. Each unit: key, kind, channel, channel_name, ts, link, msgs, my + signals."""
    summ = json.load(open(os.path.join(exp, 'summary.json'))) if os.path.exists(os.path.join(exp, 'summary.json')) else {}
    users = json.load(open(os.path.join(exp, 'users.json'))) if os.path.exists(os.path.join(exp, 'users.json')) else {}
    team, my_name = summ.get('team', ''), summ.get('user_name') or users.get(summ.get('user_id', ''), '')
    clean = cleaner(users)
    mine = {}
    idx_path = os.path.join(exp, 'threads.jsonl')
    index = jsonl(idx_path) if os.path.exists(idx_path) else []
    for t in index: mine[(t['channel'], t['thread_ts'])] = t.get('my_count', 0)
    raw = []  # (kind, channel, channel_name, msgs)
    for f in sorted(glob.glob(os.path.join(exp, 'channels', '*.jsonl'))):
        top = sorted(jsonl(f), key=lambda m: float(m['ts']))
        burst = []
        def flush():
            if burst: raw.append(('burst', burst[0].get('channel'), burst[0].get('channel_name'), list(burst)))
            burst.clear()
        for m in top:
            if m.get('thread'):
                raw.append(('thread', m.get('channel'), m.get('channel_name'), [m] + [x for x in m['thread'][1:] if 'error' not in x]))
                continue
            if burst and float(m['ts']) - float(burst[-1]['ts']) > gap_min * 60: flush()
            burst.append(m)
        flush()
    extra = [t for t in index if 'messages' in t]
    op = os.path.join(exp, 'threads_outside.jsonl')
    if os.path.exists(op): extra += jsonl(op)
    seen = {(c, ms[0]['ts']) for k, c, n, ms in raw}
    for t in extra:
        if (t['channel'], t['thread_ts']) in seen: continue
        seen.add((t['channel'], t['thread_ts']))
        raw.append(('thread', t['channel'], t.get('channel_name'), [x for x in t['messages'] if 'error' not in x]))
    units = []
    for kind, ch, name, msgs in raw:
        if not msgs: continue
        for x in msgs: x['text'] = clean(x.get('text'))
        humans = [x for x in msgs if not x.get('bot')]
        if not humans: continue
        ts = msgs[0]['ts']
        my = mine.get((ch, ts), 0) if kind == 'thread' else 0
        if my == 0 and my_name: my = sum(1 for x in humans if x.get('user') == my_name)
        text = '\n'.join(x.get('text') or '' for x in msgs)
        try: dur_h = (float(msgs[-1]['ts']) - float(msgs[0]['ts'])) / 3600
        except (TypeError, ValueError): dur_h = 0
        u = {'key': f'{ch}:{ts}', 'kind': kind, 'channel': ch, 'channel_name': name, 'ts': ts, 'date': ts2s(ts),
             'link': f'https://{team}.slack.com/archives/{ch}/p{str(ts).replace(".", "")}' if team and ch else '',
             'msgs': msgs, 'n': len(msgs), 'humans': len(humans), 'people': len({x.get('user') for x in humans}), 'my': my,
             'hours': round(dur_h, 1), 'outage_hits': len(OUTAGE.findall(text)), 'invisible_hits': len(INVISIBLE.findall(text)),
             'fix_hits': len(FIX.findall(text))}
        s = min(u['invisible_hits'], 6) * 5 + min(u['outage_hits'], 10) * 2 + min(u['fix_hits'], 6) + 2 * math.log2(1 + u['n']) \
            + 3 * min(my, 5) + min(u['people'], 8) + min(dur_h, 48) / 8
        if u['outage_hits'] == 0 and u['invisible_hits'] == 0: s *= 0.3
        u['score'] = round(s, 1)
        units.append(u)
    return {'team': team, 'my_name': my_name}, units
