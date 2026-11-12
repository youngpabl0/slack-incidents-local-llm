#!/usr/bin/env python3
# Print the discussions behind ranks of smart-ranking-<date>.jsonl: python3 show_threads.py <export-dir> <ranking.jsonl> 1 5 9 ...
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slack_units import load, ts2s
exp, rk, ranks = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]
rows = [json.loads(l) for l in open(rk)]
want = {}
for r in ranks:
    x = rows[r - 1]; p = x['link'].rsplit('/p', 1)[1]; want[p[:-6] + '.' + p[-6:]] = (r, x)
meta, units = load(exp)
for u in units:
    if u['ts'] in want:
        r, x = want[u['ts']]
        print(f"\n######## #{r} {x['date']} #{x['channel']} ({u['n']} msgs) {x['llm']['theme']}")
        for m in u["msgs"][:int(os.environ.get("MAXM", 40))]:
            t = (m.get('text') or '').replace('\n', ' ')
            print(f"[{ts2s(m['ts'], '%m-%d %H:%M')}] {m.get('user')}{' (bot)' if m.get('bot') else ''}: {t[:int(os.environ.get('MAXC', 260)) if not m.get('bot') else 120]}")
