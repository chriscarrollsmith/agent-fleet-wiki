#!/usr/bin/env bash
# Raise the upload limit and point attachments at the private bucket.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
: "${HOST:?Run deploy/05-instance.sh first}"
: "${EIP:?Run deploy/05-instance.sh first}"

require STORAGE_S3_KEY STORAGE_S3_SECRET STORAGE_S3_REGION STORAGE_S3_BUCKET

s3_file="$(mktemp)"
chmod 600 "$s3_file"
trap 'rm -f "$s3_file"' EXIT
cat > "$s3_file" <<EOF
STORAGE_S3_KEY=${STORAGE_S3_KEY}
STORAGE_S3_SECRET=${STORAGE_S3_SECRET}
STORAGE_S3_REGION=${STORAGE_S3_REGION}
STORAGE_S3_BUCKET=${STORAGE_S3_BUCKET}
EOF
scp_to_ubuntu "$s3_file" /tmp/bookstack-s3.env
rm -f "$s3_file"
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
