#!/usr/bin/env bash
# Issue the sslip.io certificate and replace the default admin account.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
require ADMIN_EMAIL
: "${HOST:?Run deploy/05-instance.sh first}"
: "${EIP:?Run deploy/05-instance.sh first}"

ssh_ubuntu sudo bash -s -- "$HOST" "$ADMIN_EMAIL" <<'REMOTE'
set -euo pipefail
HOST=$1
ADMIN_EMAIL=$2
cd /var/www/bookstack

if ! certbot certificates 2>/dev/null | grep -q "$HOST"; then
  apt-get install -y certbot python3-certbot-apache
  certbot --apache -d "$HOST" --non-interactive --agree-tos -m "$ADMIN_EMAIL" --redirect
fi

if grep -q '^APP_URL=http://' .env; then
  sed -i "s|^APP_URL=.*|APP_URL=https://${HOST}|" .env
  sudo -u www-data php artisan bookstack:update-url --force "http://${HOST}" "https://${HOST}"
fi

MARKER=storage/.fleet-admin-created
if [[ -f "$MARKER" ]]; then
  echo "Admin account was already replaced. Password was shown the first time this ran."
else
  sudo -u www-data php artisan bookstack:create-admin \
    --initial --email="$ADMIN_EMAIL" --name="Admin" --generate-password
  touch "$MARKER"
  chown ubuntu:www-data "$MARKER"
fi
REMOTE

echo
echo "Open https://${HOST}"
echo "Sign in with ${ADMIN_EMAIL} and the password printed above."
