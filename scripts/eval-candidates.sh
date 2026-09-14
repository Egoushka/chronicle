#!/usr/bin/env bash
# Find candidate segments for eval/questions.json and print them with their
# evidence ids already formatted.
#
#   ./scripts/eval-candidates.sh 'квартир'
#   ./scripts/eval-candidates.sh 'AcmeBE|acme' 20
#
# WHY THIS EXISTS, AND WHY IT DOES NOT WRITE THE QUESTIONS
#
# The questions have to come from memory, not from browsing. A question you
# write after reading the segment is a question you already know is findable —
# selecting for findability is exactly the bias that makes a self-built
# benchmark flatter its own system. The realistic query is "what did we decide
# about the flat", asked cold, with no idea whether the answer survives in the
# archive at all.
#
# So: you supply the question. This supplies the boring half — locating the
# segment and transcribing its `source_event_ids` without typos, which is the
# part that actually eats the hour.
#
# Workflow: remember something -> grep for a word you are confident appears ->
# confirm the segment really answers it -> paste the emitted block into
# eval/questions.json and replace QUESTION/KIND.
#
# Searches raw_text with SQL only. It deliberately does NOT use /recall, so the
# benchmark is not selected by the retrieval path it is meant to measure.
set -euo pipefail

PATTERN="${1:?usage: eval-candidates.sh <regex> [limit]}"
LIMIT="${2:-10}"
HOST="${CHRONICLE_DOCTOR_HOST:-homelab}"

ssh "$HOST" "cd /srv/stacks/chronicle && docker compose exec -T chronicle-db \
  psql -U chronicle -d chronicle -tAF'|' -c \"
    SELECT segment_id,
           started_at::date,
           event_count,
           array_to_string(source_event_ids, ','),
           left(replace(replace(raw_text, chr(10), ' / '), '\\\"', ''), 240)
    FROM segment
    WHERE is_substantive
      AND raw_text ~* '${PATTERN//\'/\'\'}'
    ORDER BY started_at
    LIMIT ${LIMIT}\"" |
while IFS='|' read -r id date n evidence preview; do
  [ -z "${id:-}" ] && continue
  printf '\n\033[36m── segment %s · %s · %s events\033[0m\n' "$id" "$date" "$n"
  printf '   %s\n' "$preview"
  printf '\n  {\n    "question": "REPLACE ME",\n    "kind": "lookup",\n    "evidence": ['
  printf '"%s"' "$(echo "$evidence" | sed 's/,/", "/g')"
  printf '],\n    "keywords": ["%s"],\n    "note": null\n  },\n' "$PATTERN"
done

cat <<'EOF'

  Paste the blocks you want into eval/questions.json, replace "REPLACE ME"
  with the question as you would actually ask it, and set "kind":

    lookup         "what did we decide about X"
    first_mention  "when did I first talk about X"      argmin over ts
    evolution      "how did my view of X change"        needs several segments
    aggregate      "how many times did I X"             counting, not ranking

  Then:  make eval     # chronicle vs ripgrep, evidence recall on both
EOF
