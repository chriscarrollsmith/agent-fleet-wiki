#!/usr/bin/env bash
# Install BookStack with the upstream Ubuntu 24.04 script. Safe to re-run after it has succeeded.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
: "${HOST:?Run deploy/05-instance.sh first}"
: "${EIP:?Run deploy/05-instance.sh first}"

echo "Waiting for status checks on the instance."
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID"

installed="$(ssh_ubuntu test -f /var/www/bookstack/.env && echo yes || echo no)"
if [[ "$installed" == yes ]]; then
  echo "BookStack is already installed at /var/www/bookstack."
else
  echo "Installing BookStack. This takes several minutes."
  ssh_ubuntu sudo bash -s -- "$HOST" <<'REMOTE'
set -euo pipefail
HOST=$1
wget -q https://raw.githubusercontent.com/BookStackApp/devops/main/scripts/installation-ubuntu-24.04.sh
chmod a+x installation-ubuntu-24.04.sh
./installation-ubuntu-24.04.sh "$HOST"
REMOTE
fi

echo
echo "Login page:"
curl -fsS -o /dev/null -w "HTTP %{http_code}\n" "http://${HOST}/login"
echo "Default login is still admin@admin.com / password. The next stages replace it."
