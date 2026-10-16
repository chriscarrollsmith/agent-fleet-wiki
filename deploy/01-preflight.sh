#!/usr/bin/env bash
# Check the SSO session, region, and default VPC. Creates nothing.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env

echo "Profile:  ${AWS_PROFILE}"
echo "Region:   ${AWS_DEFAULT_REGION}"
echo
aws sts get-caller-identity
echo
require BUCKET ADMIN_EMAIL
echo "Bucket:   ${BUCKET}"
echo "Email:    ${ADMIN_EMAIL}"

VPC="$(aws_text ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId')"
if [[ -z "$VPC" || "$VPC" == None ]]; then
  echo "No default VPC in ${AWS_DEFAULT_REGION}." >&2
  exit 1
fi
SUBNET="$(aws_text ec2 describe-subnets \
  --filters "Name=vpc-id,Values=${VPC}" Name=default-for-az,Values=true \
  --query 'Subnets[0].SubnetId')"
if [[ -z "$SUBNET" || "$SUBNET" == None ]]; then
  echo "No default subnet in ${VPC}." >&2
  exit 1
fi

persist_state
echo
echo "Default VPC:    ${VPC}"
echo "Default subnet: ${SUBNET}"
echo "Preflight ok. No resources were created."
