#!/usr/bin/env bash
# Create the private attachment bucket, or reapply its settings if it already exists.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
require BUCKET

if [[ "$BUCKET" == *change-me* || "$BUCKET" == *your-unique-bucket-name* ]]; then
  echo "Set BUCKET in .env to a real, globally unique name." >&2
  exit 1
fi
if [[ ! "$BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]; then
  echo "BUCKET must be a lowercase S3 name: ${BUCKET}" >&2
  exit 1
fi

create_bucket() {
  local err
  err="$(mktemp)"
  set +e
  if [[ "$AWS_DEFAULT_REGION" == us-east-1 ]]; then
    aws s3api create-bucket --bucket "$BUCKET" 2>"$err"
  else
    aws s3api create-bucket --bucket "$BUCKET" \
      --create-bucket-configuration "LocationConstraint=${AWS_DEFAULT_REGION}" 2>"$err"
  fi
  local status=$?
  set -e
  if [[ "$status" -eq 0 ]]; then
    rm -f "$err"
    return 0
  fi
  if grep -q BucketAlreadyOwnedByYou "$err"; then
    echo "Bucket ${BUCKET} is already in this account. Reapplying settings."
    rm -f "$err"
    return 0
  fi
  if grep -q BucketAlreadyExists "$err"; then
    echo "The name ${BUCKET} is already taken by another AWS account." >&2
    echo "S3 bucket names are global. Change BUCKET in .env and re-run this stage." >&2
    rm -f "$err"
    exit 1
  fi
  cat "$err" >&2
  rm -f "$err"
  exit "$status"
}

if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  echo "Bucket ${BUCKET} already exists and is reachable. Reapplying settings."
else
  echo "Creating bucket ${BUCKET}."
  create_bucket
fi

aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

echo
echo "Public access block:"
aws s3api get-public-access-block --bucket "$BUCKET"
echo "Bucket ${BUCKET} is private."
