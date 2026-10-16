#!/usr/bin/env bash
# Create the attachment IAM user. Writes the access key once, to deploy/bookstack-s3.env.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
require BUCKET

USER_NAME=bookstack-attachments

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

if [[ -f "$SECRET_FILE" ]]; then
  echo "Access key file already exists at ${SECRET_FILE}. Not creating another key."
else
  read -r ACCESS_KEY_ID SECRET_ACCESS_KEY < <(
    aws iam create-access-key --user-name "$USER_NAME" \
      --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text | tr -d '\r'
  )
  umask 077
  cat > "$SECRET_FILE" <<EOF
STORAGE_S3_KEY=${ACCESS_KEY_ID}
STORAGE_S3_SECRET=${SECRET_ACCESS_KEY}
STORAGE_S3_REGION=${AWS_DEFAULT_REGION}
STORAGE_S3_BUCKET=${BUCKET}
EOF
  echo "Access key written to ${SECRET_FILE}. The secret is not printed."
fi

echo
echo "Access keys for ${USER_NAME}:"
aws iam list-access-keys --user-name "$USER_NAME" --query 'AccessKeyMetadata[].AccessKeyId' --output text
echo "Policy bookstack-attachments is attached."
