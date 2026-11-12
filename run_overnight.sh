#!/usr/bin/env bash
# Rank a Slack export overnight with local models only.
# Usage: run_overnight.sh <export-dir> <out-dir> [HHMM deadline, default 0740]
# Model calls go to a second Ollama on 11435 started for the job (see README, "Скорость").
# 1) rank what is already exported, 2) wait for slack_export.mjs to finish, but not past the deadline,
# 3) rank the snapshot as it is then and group the incidents. Results are cached in the export folder, so a rerun resumes.
EXP=${1:?export dir}; OUTD=${2:?out dir}; DEADLINE=${3:-0740}
R="$(cd "$(dirname "$0")" && pwd)/smart_rank.py"
ARGS=(--ollama http://127.0.0.1:11435 --workers 2 --budget 6000 --candidates 600 --out-dir "$OUTD")
KEEP=(); command -v caffeinate >/dev/null && KEEP=(caffeinate -is)  # macOS: do not sleep while the model reads
echo "== pass 1 (export so far) $(date '+%F %T')"
"${KEEP[@]}" python3 "$R" "$EXP" "${ARGS[@]}" --themes 0
while pgrep -f "node .*slack_export.mjs" >/dev/null && [ "$(date +%H%M)" -lt "$DEADLINE" ]; do sleep 60; done
echo "== export $(pgrep -f 'node .*slack_export.mjs' >/dev/null && echo 'still running, deadline reached' || echo finished) $(date '+%F %T'): $(ls "$EXP"/channels/*.done | wc -l | tr -d ' ') of $(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "$EXP/conversations.json") conversations done"
echo "== pass 2 (snapshot) $(date '+%F %T')"
"${KEEP[@]}" python3 "$R" "$EXP" "${ARGS[@]}" --themes 10
echo "== done $(date '+%F %T')"
