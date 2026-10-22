#!/usr/bin/env bash
# Push BookStack user credentials from deploy/users to Infisical.
#
# The local file is the source. Matching remote values are left unchanged.
# INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET, and INFISICAL_PROJECT_ID come
# from the process environment, or from .env when the environment does not set
# them. The client id and secret belong to a machine identity created under
# the project's Access Control.
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
