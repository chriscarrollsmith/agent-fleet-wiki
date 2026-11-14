#!/usr/bin/env bash
# Rotate one BookStack rollout's credentials. See docs/rotate-credentials.md.
#
# Usage:
#   ROLLOUT=name bash deploy/rotate-credentials.sh --dry-run
#   ROLLOUT=name bash deploy/rotate-credentials.sh ssh-key
#   ROLLOUT=name bash deploy/rotate-credentials.sh s3
#   ROLLOUT=name bash deploy/rotate-credentials.sh admin
#   ROLLOUT=name bash deploy/rotate-credentials.sh infisical
#   ROLLOUT=name bash deploy/rotate-credentials.sh humans --human person@example.com
#   ROLLOUT=name bash deploy/rotate-credentials.sh all --human person@example.com
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/rotate_credentials.py"

if command -v uv >/dev/null 2>&1 && [[ -f "$HERE/../pyproject.toml" ]]; then
  exec uv run --quiet --project "$HERE/.." python "$SCRIPT" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT" "$@"
fi
echo "python3 (or uv) is required to run deploy/rotate-credentials.sh" >&2
exit 1
