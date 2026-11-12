#!/usr/bin/env python3
# Usage: python3 rank_threads.py <export-dir> [out.md] [--top 80]
# Quick keyword ranking of Slack discussions from a slack_export.mjs folder: incidents first, especially outages that
# monitoring did not catch (found by people, support or customers), then size, the user's participation and duration.
# For reading the threads with a local model use smart_rank.py. Writes Markdown + JSONL.
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slack_units import load

EXP = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else os.path.join(os.path.dirname(EXP.rstrip('/')), f'ranking-{os.path.basename(EXP.rstrip("/"))}.md')
TOP = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 80
meta, units = load(EXP)
units.sort(key=lambda u: -u['score'])
cols = ('score', 'date', 'kind', 'channel_name', 'n', 'my', 'people', 'outage_hits', 'invisible_hits', 'fix_hits', 'hours', 'link')
with open(OUT.replace('.md', '.jsonl'), 'w', encoding='utf-8') as fo:
    for u in units: fo.write(json.dumps({**{k: u[k] for k in cols}, 'first': (u['msgs'][0].get('text') or '')[:220]}, ensure_ascii=False) + '\n')
with open(OUT, 'w', encoding='utf-8') as fo:
    fo.write(f'# Slack ranking from {os.path.basename(EXP.rstrip("/"))}: incidents, outages that alerts missed, the user\'s threads\n\n')
    fo.write(f'{len(units)} discussions scored, top {TOP}. Score = 5 x "not seen by monitoring" signals + 2 x outage signals + fix/RCA signals + size + 3 x the user\'s messages + participants + duration. Internal data: do not publish.\n\n')
    fo.write('| # | score | date | channel | msgs | mine | people | missed-by-alerts | outage | first message |\n|---|---|---|---|---|---|---|---|---|---|\n')
    for i, u in enumerate(units[:TOP], 1):
        first = (u['msgs'][0].get('text') or '').replace('\n', ' ').replace('|', '/')[:220]
        fo.write(f"| {i} | {u['score']} | [{u['date']}]({u['link']}) | {u['channel_name']} | {u['n']} | {u['my']} | {u['people']} | {u['invisible_hits']} | {u['outage_hits']} | {first} |\n")
print('ranked', len(units), '->', OUT)
