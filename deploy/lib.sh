# Shared setup for the BookStack deploy stages.
# Sourced by the stage scripts. Not run directly.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="$ROOT/deploy/state.env"
SECRET_FILE="$ROOT/deploy/bookstack-s3.env"
KEY_FILE="${KEY_FILE:-$ROOT/deploy/bookstack.pem}"

source_file() {
  local file="$1"
  # Git Bash on Windows often saves files with CRLF. Strip CR before sourcing.
  # shellcheck disable=SC1090
  source <(tr -d '\r' < "$file")
}

load_env() {
  if [[ ! -f "$ROOT/.env" ]]; then
    echo "Missing ${ROOT}/.env" >&2
    echo "Copy .env.example to .env and fill in the values." >&2
    exit 1
  fi
  set -a
  source_file "$ROOT/.env"
  set +a
  if [[ -f "$STATE_FILE" ]]; then
    set -a
    source_file "$STATE_FILE"
    set +a
  fi
  export AWS_PROFILE="${AWS_PROFILE:-fleet-coordinator}"
  : "${AWS_DEFAULT_REGION:?Set AWS_DEFAULT_REGION in .env}"
  export AWS_DEFAULT_REGION
  export KEY_FILE
}

require() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      echo "Set ${name} in .env" >&2
      exit 1
    fi
  done
}

aws_text() {
  aws "$@" --output text | tr -d '\r'
}

persist_state() {
  local tmp="$STATE_FILE.tmp"
  local keys=(VPC SUBNET SG KEY_FILE INSTANCE_ID ALLOC EIP HOST)
  local key
  umask 077
  : > "$tmp"
  for key in "${keys[@]}"; do
    if [[ -n "${!key:-}" ]]; then
      printf '%s=%q\n' "$key" "${!key}" >> "$tmp"
    fi
  done
  mv "$tmp" "$STATE_FILE"
}

ssh_ubuntu() {
  : "${KEY_FILE:?Run deploy/04-network.sh first}"
  : "${EIP:?Run deploy/05-instance.sh first}"
  ssh -i "$KEY_FILE" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "ubuntu@${EIP}" "$@"
}

scp_to_ubuntu() {
  local src="$1"
  local dest="$2"
  : "${KEY_FILE:?Run deploy/04-network.sh first}"
  : "${EIP:?Run deploy/05-instance.sh first}"
  scp -i "$KEY_FILE" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "$src" "ubuntu@${EIP}:${dest}"
}
