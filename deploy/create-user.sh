#!/usr/bin/env bash
# Create a BookStack user and API token over HTTPS (default flow).
#
# Drives the same admin web routes a browser uses, so it needs only the site
# URL and the admin credentials from Infisical dev /wiki. No SSH, AWS, or instance access.
# The SSH/PHP fallback is deploy/create-user-ssh.sh.
#
# Usage:
#   bash deploy/create-user.sh --name "Pi Agent" --email "pi-agent@example.com"
#   bash deploy/create-user.sh --name "Pi Agent" --email "pi-agent@example.com" --role Agent
#   bash deploy/create-user.sh --name "Pi Agent" --email "pi-agent@example.com" --identity pi-agent
#   bash deploy/create-user.sh --sync-role
#
# Credentials are printed once. --identity stores them in Infisical at
# dev /users/<folder>. Each dot in the slug becomes a hyphen in that folder
# name. A failed Infisical write leaves a file under deploy/users/.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/create-user-http.py"

if command -v uv >/dev/null 2>&1 && [[ -f "$HERE/../pyproject.toml" ]]; then
  exec uv run --quiet --project "$HERE/.." python "$SCRIPT" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$SCRIPT" "$@"
fi
if command -v python >/dev/null 2>&1; then
  exec python "$SCRIPT" "$@"
fi
echo "python3 (or uv) is required to run deploy/create-user.sh" >&2
exit 1
