#!/usr/bin/env bash
# Raise the upload limit and point attachments at the private bucket.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
: "${HOST:?Run deploy/05-instance.sh first}"
: "${EIP:?Run deploy/05-instance.sh first}"

if [[ ! -f "$SECRET_FILE" ]]; then
  echo "Missing ${SECRET_FILE}. Run deploy/03-iam.sh first." >&2
  exit 1
fi

scp_to_ubuntu "$SECRET_FILE" /tmp/bookstack-s3.env
ssh_ubuntu sudo bash -s <<'REMOTE'
set -euo pipefail
cat > /etc/php/8.3/fpm/conf.d/99-bookstack-uploads.ini <<'EOF'
upload_max_filesize = 2048M
post_max_size = 2048M
max_execution_time = 600
max_input_time = 600
EOF
sed -i 's/^Timeout .*/Timeout 600/' /etc/apache2/apache2.conf

ENV_FILE=/var/www/bookstack/.env
if grep -q '^STORAGE_ATTACHMENT_TYPE=' "$ENV_FILE"; then
  echo "Storage settings are already present."
else
  cat /tmp/bookstack-s3.env >> "$ENV_FILE"
  cat >> "$ENV_FILE" <<'EOF'
STORAGE_IMAGE_TYPE=local_secure
STORAGE_ATTACHMENT_TYPE=s3
FILE_UPLOAD_SIZE_LIMIT=2048
EOF
fi
chown ubuntu:www-data "$ENV_FILE"
chmod 740 "$ENV_FILE"
rm -f /tmp/bookstack-s3.env
systemctl restart php8.3-fpm apache2
grep -E '^(STORAGE_IMAGE_TYPE|STORAGE_ATTACHMENT_TYPE|FILE_UPLOAD_SIZE_LIMIT|STORAGE_S3_BUCKET)=' "$ENV_FILE"
REMOTE

echo
echo "Attachments will be stored in S3. Page images stay on the instance."
