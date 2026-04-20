#!/usr/bin/env bash
# Pre-commit hook: extract inline <script> blocks from HTML files and syntax-check with Node.
set -euo pipefail

status=0
for file in "$@"; do
  js=$(python3 -c "
import re, sys
with open(sys.argv[1]) as f:
    html = f.read()
scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
print('\n'.join(scripts))
" "$file")

  if [ -z "$js" ]; then
    continue
  fi

  tmpfile=$(mktemp /tmp/check-js-XXXXXX.js)
  echo "$js" > "$tmpfile"
  if ! node -e "
const fs = require('fs');
const code = fs.readFileSync('$tmpfile', 'utf8');
try { new Function(code); } catch(e) {
  console.error('$file: JS syntax error:', e.message);
  process.exit(1);
}
" 2>&1; then
    status=1
  fi
  rm -f "$tmpfile"
done

exit $status
