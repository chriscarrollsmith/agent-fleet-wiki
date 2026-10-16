#!/usr/bin/env bash
# Run the BookStack API client. Requires python3.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/bookstack.py" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$ROOT/bookstack.py" "$@"
fi
echo "python3 is required to run bookstack/bookstack.sh" >&2
exit 1
