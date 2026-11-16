#!/usr/bin/env bash
# scripts/lib/land_main.py runs this hook after each push to main, from the
# checkout root, with LANDED_SHA. It publishes the AWS prices page again when
# the page code changed since its last publish, so the page shows merged code.
# The promotion step moves ~/.claude to the landed commit before it pushes.
# A checkout at another commit publishes nothing; the next promotion or the
# hourly buildlog run publishes. A publish failure does not fail the push.
set -u

page_paths=(scripts/aws_target scripts/review_links config/aws_page.conf config/aws_target.conf)
head=$(git rev-parse --verify -q HEAD) || head=""
if [ "$head" != "${LANDED_SHA:-}" ]; then
  echo "post-push: $PWD is at ${head:-no commit}, not ${LANDED_SHA:-no commit}; the AWS prices page publishes on the next promotion or the hourly run"
  exit 0
fi
record="$(git rev-parse --path-format=absolute --git-common-dir)/aws-page-published"
published=$(cat "$record" 2>/dev/null) || published=""
if [ -n "$published" ] && git merge-base --is-ancestor "$published" "$LANDED_SHA" 2>/dev/null \
    && git diff --quiet "$published" "$LANDED_SHA" -- "${page_paths[@]}"; then
  printf '%s\n' "$LANDED_SHA" > "$record.tmp" && mv "$record.tmp" "$record"
  exit 0
fi
if timeout 120 scripts/lib/py scripts/aws_target/aws_page.py publish; then
  printf '%s\n' "$LANDED_SHA" > "$record.tmp" && mv "$record.tmp" "$record"
  echo "post-push: the AWS prices page is published from $LANDED_SHA"
else
  echo "post-push: the AWS prices page publish failed (exit $?); the next promotion or the hourly run publishes it"
fi
exit 0
