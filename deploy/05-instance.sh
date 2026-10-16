#!/usr/bin/env bash
# Launch the Ubuntu instance and attach an elastic IP. Prints the sslip.io hostname.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env
: "${VPC:?Run deploy/01-preflight.sh first}"
: "${SUBNET:?Run deploy/01-preflight.sh first}"
: "${SG:?Run deploy/04-network.sh first}"
: "${KEY_FILE:?Run deploy/04-network.sh first}"

KEY_NAME=bookstack
AMI_PARAM=/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id
AMI_PARAM="${AMI_PARAM//$'\r'/}"
echo "Resolving AMI ${AMI_PARAM}"

if [[ -n "${INSTANCE_ID:-}" ]]; then
  state="$(aws_text ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' || true)"
  if [[ "$state" == running || "$state" == pending ]]; then
    echo "Reusing instance ${INSTANCE_ID} (${state})."
  elif [[ "$state" == stopped || "$state" == stopping ]]; then
    echo "Starting stopped instance ${INSTANCE_ID}."
    aws ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
    aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
  else
    echo "Instance ${INSTANCE_ID} is ${state:-missing}. Clear INSTANCE_ID from deploy/state.env to launch another." >&2
    exit 1
  fi
else
  AMI="$(aws_text ssm get-parameter --name "$AMI_PARAM" --query Parameter.Value)"
  ROOT="$(aws_text ec2 describe-images --image-ids "$AMI" --query 'Images[0].RootDeviceName')"
  echo "AMI ${AMI}, root device ${ROOT}."
  INSTANCE_ID="$(aws_text ec2 run-instances \
    --image-id "$AMI" \
    --instance-type t3.small \
    --key-name "$KEY_NAME" \
    --security-group-ids "$SG" \
    --subnet-id "$SUBNET" \
    --associate-public-ip-address \
    --block-device-mappings "[{\"DeviceName\":\"${ROOT}\",\"Ebs\":{\"VolumeSize\":30,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=bookstack}]" \
    --query 'Instances[0].InstanceId')"
  persist_state
  echo "Launched ${INSTANCE_ID}. Waiting until it is running."
  aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
fi

EXISTING_EIP="$(aws ec2 describe-addresses --filters "Name=instance-id,Values=${INSTANCE_ID}" \
  --query 'Addresses[0].PublicIp' --output text | tr -d '\r')"
if [[ -n "$EXISTING_EIP" && "$EXISTING_EIP" != None ]]; then
  EIP="$EXISTING_EIP"
  echo "Reusing elastic IP ${EIP}."
else
  echo "Allocating an elastic IP."
  ALLOC="$(aws_text ec2 allocate-address --domain vpc --query AllocationId)"
  aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC" >/dev/null
  EIP="$(aws_text ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp')"
fi

HOST="${EIP}.sslip.io"
persist_state
echo
echo "Instance: ${INSTANCE_ID}"
echo "Address:  https://${HOST}"
echo "The instance is up. BookStack is not installed yet."
