#!/usr/bin/env bash
# Push BookStack user credentials from deploy/users to Infisical.
#
# Matching remote values are left unchanged. A successful upload removes the
# local file. INFISICAL_TOKEN comes from the process environment, or from .env
# when the environment does not set it. The token is a service token.
#
# Usage:
#   bash deploy/sync-infisical-users.sh --identity pi-agent --file deploy/users/pi@example.com.env
#   bash deploy/sync-infisical-users.sh
#   bash deploy/sync-infisical-users.sh --dry-run
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/sync_infisical_users.py"

if command -v uv >/dev/null 2>&1 && [[ -f "$HERE/../pyproject.toml" ]]; then
  exec uv run --quiet --project "$HERE/.." python "$SCRIPT" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT" "$@"
fi
echo "python3 (or uv) is required to run deploy/sync-infisical-users.sh" >&2
exit 1
