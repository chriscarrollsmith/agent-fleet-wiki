#!/usr/bin/env bash
# Create the SSH key and the security group. SSH is limited to this machine's address.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
: "${VPC:?Run deploy/01-preflight.sh first}"

KEY_NAME="${KEY_NAME:-bookstack}"
MY_IP="$(curl -fsS https://checkip.amazonaws.com | tr -d '\r')/32"

if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
  if [[ ! -s "$KEY_FILE" ]]; then
    echo "Key pair ${KEY_NAME} exists in AWS, but ${KEY_FILE} is missing or empty." >&2
    echo "AWS cannot show the private key again. Delete the key pair, or put the pem file back, then re-run." >&2
    exit 1
  fi
  echo "Reusing key pair ${KEY_NAME}."
else
  # describe-key-pairs also fails when the SSO session has expired. Never
  # overwrite a key that may still be the only copy.
  if [[ -s "$KEY_FILE" ]]; then
    echo "${KEY_FILE} exists, but key pair ${KEY_NAME} was not found (is the SSO session expired?). Not overwriting it." >&2
    exit 1
  fi
  echo "Creating key pair ${KEY_NAME}."
  umask 077
  key_material="$(aws_text ec2 create-key-pair --key-name "$KEY_NAME" --query KeyMaterial)"
  printf '%s\n' "$key_material" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
fi

SG="$(aws_text ec2 describe-security-groups \
  --filters "Name=group-name,Values=${KEY_NAME}" "Name=vpc-id,Values=${VPC}" \
  --query 'SecurityGroups[0].GroupId')"
if [[ -z "$SG" || "$SG" == None ]]; then
  echo "Creating security group."
  SG="$(aws_text ec2 create-security-group \
    --group-name "$KEY_NAME" --description "BookStack" --vpc-id "$VPC" \
    --query GroupId)"
else
  echo "Reusing security group ${SG}."
fi

allow_port() {
  local port="$1"
  local cidr="$2"
  local found
  found="$(aws ec2 describe-security-groups --group-ids "$SG" \
    --filters "Name=ip-permission.from-port,Values=${port}" "Name=ip-permission.to-port,Values=${port}" "Name=ip-permission.cidr,Values=${cidr}" \
    --query 'SecurityGroups[0].GroupId' --output text | tr -d '\r')"
  if [[ "$found" == "$SG" ]]; then
    echo "Port ${port} from ${cidr} is already open."
    return
  fi
  aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$port" --cidr "$cidr" >/dev/null
  echo "Opened port ${port} from ${cidr}."
}

allow_port 22 "$MY_IP"
allow_port 80 "0.0.0.0/0"
allow_port 443 "0.0.0.0/0"

persist_state
echo
echo "Key file:       ${KEY_FILE}"
echo "Security group: ${SG}"
echo "SSH allowed from ${MY_IP} only."
