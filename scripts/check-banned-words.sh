#!/usr/bin/env bash
# Fail if any tracked file contains a banned term.
#
# Patterns are loaded from `.banned-words` (committed) and `.banned-words.local`
# (gitignored, optional). Each line is an ERE pattern; '#' and blank lines are
# ignored. The script scans every git-tracked file except itself and the
# patterns files.

set -euo pipefail

PATTERN_FILES=(".banned-words" ".banned-words.local")

# Collect non-comment, non-blank lines from each existing pattern file.
PATTERNS=""
for f in "${PATTERN_FILES[@]}"; do
  if [[ -f "$f" ]]; then
    file_patterns="$(grep -vE '^(#|$)' "$f" || true)"
    if [[ -n "$file_patterns" ]]; then
      if [[ -n "$PATTERNS" ]]; then
        PATTERNS="$PATTERNS"$'\n'"$file_patterns"
      else
        PATTERNS="$file_patterns"
      fi
    fi
  fi
done

if [[ -z "$PATTERNS" ]]; then
  echo "check-banned-words: no patterns configured (skipping)" >&2
  exit 0
fi

# Combine patterns into a single ERE alternation.
COMBINED="$(echo "$PATTERNS" | paste -sd '|' -)"

# Files to scan: everything tracked except this script and the pattern files.
EXCLUDE_RE='^(scripts/check-banned-words\.sh|\.banned-words(\.local)?)$'
FILES="$(git ls-files | grep -vE "$EXCLUDE_RE" || true)"

if [[ -z "$FILES" ]]; then
  exit 0
fi

HITS="$(echo "$FILES" | tr '\n' '\0' | xargs -0 grep -ilE "$COMBINED" 2>/dev/null || true)"

if [[ -n "$HITS" ]]; then
  echo "check-banned-words: banned terms found in:" >&2
  echo "$HITS" >&2
  echo "" >&2
  echo "Patterns: $COMBINED" >&2
  exit 1
fi

exit 0
