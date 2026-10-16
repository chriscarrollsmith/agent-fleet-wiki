#!/usr/bin/env bash
# Issue the sslip.io certificate and replace the default admin account.
# The admin password is generated locally, written to .env, and uploaded once.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
require ADMIN_EMAIL
: "${HOST:?Run deploy/05-instance.sh first}"
: "${EIP:?Run deploy/05-instance.sh first}"

admin_created="$(ssh_ubuntu test -f /var/www/bookstack/storage/.fleet-admin-created && echo yes || echo no)"
if [[ "$admin_created" == yes ]]; then
  ADMIN_PASSWORD=""
  echo "Admin account already exists. Password remains in ${ROOT}/.env."
else
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    ADMIN_PASSWORD="$(generate_password)"
    set_env_var ADMIN_PASSWORD "$ADMIN_PASSWORD"
    echo "Generated an admin password and wrote it to ${ROOT}/.env."
  else
    echo "Using ADMIN_PASSWORD from ${ROOT}/.env."
  fi
  pw_file="$(mktemp)"
  printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" > "$pw_file"
  scp_to_ubuntu "$pw_file" /tmp/bookstack-admin.env
  rm -f "$pw_file"
fi

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
  echo "Admin account was already replaced."
else
  # Password was uploaded by the deploy script; never printed to the console.
  source /tmp/bookstack-admin.env
  sudo -u www-data php artisan bookstack:create-admin \
    --initial --email="$ADMIN_EMAIL" --name="Admin" --password="$ADMIN_PASSWORD"
  rm -f /tmp/bookstack-admin.env
  touch "$MARKER"
  chown ubuntu:www-data "$MARKER"
fi
REMOTE

echo
echo "Open https://${HOST}"
echo "Sign in with ${ADMIN_EMAIL}. The password is in ${ROOT}/.env (ADMIN_PASSWORD)."
