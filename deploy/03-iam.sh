#!/usr/bin/env bash
# Create the attachment IAM user. Stores the access key once, in Infisical dev /wiki.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
require BUCKET

USER_NAME="${IAM_USER:-bookstack-attachments}"

if aws iam get-user --user-name "$USER_NAME" >/dev/null 2>&1; then
  echo "IAM user ${USER_NAME} already exists. Updating its policy."
else
  echo "Creating IAM user ${USER_NAME}."
  aws iam create-user --user-name "$USER_NAME" >/dev/null
fi

aws iam put-user-policy --user-name "$USER_NAME" --policy-name bookstack-attachments \
  --policy-document "$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::${BUCKET}"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/uploads/files/*"
    }
  ]
}
EOF
)"

if [[ -n "${STORAGE_S3_KEY:-}" ]]; then
  echo "Attachment key already recorded in Infisical. Not creating another key."
else
  read -r ACCESS_KEY_ID SECRET_ACCESS_KEY < <(
    aws iam create-access-key --user-name "$USER_NAME" \
      --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | tr -d '\r'
  )
  if ! printf 'STORAGE_S3_KEY=%s\nSTORAGE_S3_SECRET=%s\nSTORAGE_S3_REGION=%s\nSTORAGE_S3_BUCKET=%s\n' \
    "$ACCESS_KEY_ID" "$SECRET_ACCESS_KEY" "$AWS_DEFAULT_REGION" "$BUCKET" | infisical_py upsert /wiki; then
    aws iam delete-access-key --user-name "$USER_NAME" --access-key-id "$ACCESS_KEY_ID" >/dev/null
    echo "Infisical did not confirm the access key, so the new key was deleted." >&2
    echo "If dev /wiki has STORAGE_S3_KEY, delete that secret before running this stage again." >&2
    exit 1
  fi
  echo "Access key stored in Infisical dev /wiki. The secret is not printed."
fi

echo
echo "Access keys for ${USER_NAME}:"
aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text
echo "Policy bookstack-attachments is attached."
