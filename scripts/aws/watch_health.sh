#!/usr/bin/env bash
# Watch the auto-heal loop; print every batch milestone + outcome each poll.
# Exits when a batch MERGES (lands in the umbrella PR) / a PR url appears, or
# after MAX_POLLS. Pure observation. Overlapping windows may repeat a line or
# two between polls — that's fine.
source "$(dirname "$0")/lib.sh"
require_var AWS_REGION
R="$AWS_REGION"; MAX_POLLS="${1:-45}"
for i in $(seq 1 "$MAX_POLLS"); do
  sleep 80
  NOW=$(date +%s)
  RAW=$(aws logs filter-log-events --region "$R" --log-group-name /ecs/autoagent \
    --start-time $(( (NOW - 130) * 1000 )) --filter-pattern 'health_loop' \
    --query 'events[].message' --output text 2>/dev/null)
  PARSED=$(printf '%s' "$RAW" | tr '\t' '\n' | python3 -c "
import sys,json
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    try:
        d=json.loads(line)
        ev=d.get('event','')
        if not ev: continue
        extra=d.get('status','') or d.get('reason','') or d.get('error','') or d.get('detail','') or d.get('pr_url','') or d.get('fix_pr_url','')
        print(' '.join([d.get('timestamp','')[11:19], ev, 't'+str(d.get('task_id','')), str(extra)]))
    except: pass")
  [ -n "$PARSED" ] && printf 'poll %d @ %s UTC\n%s\n' "$i" "$(date -u +%H:%M:%S)" "$PARSED"
  printf '%s' "$PARSED" | grep -qiE 'merged|pull/|fix_pr_url|pr_opened' && { echo ">>> MERGE/PR DETECTED — stopping watch"; break; }
done
echo "=== watch ended $(date -u +%H:%M:%S) ==="
