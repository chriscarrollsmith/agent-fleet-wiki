# Shared setup for the BookStack deploy stages.
# Sourced by the stage scripts. Not run directly.

# Git Bash (MSYS) rewrites arguments that look like POSIX paths, e.g. the
# /aws/service/... SSM parameter, into Windows paths. Disable that globally so
# callers never need to prefix scripts with MSYS_NO_PATHCONV=1.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="$ROOT/deploy/state.env"
DEFAULT_SECRET_FILE="$ROOT/deploy/bookstack-s3.env"
DEFAULT_KEY_FILE="$ROOT/deploy/bookstack.pem"

# Resolve a possibly-relative artifact path against the repo root. Handles both
# POSIX (/c/...) and native Windows (C:/...) absolute paths.
resolve_path() {
  case "$1" in
    /*|[A-Za-z]:[\\/]*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$ROOT" "$1" ;;
  esac
}

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
  # Ambient shell vars must not override .env / defaults for generated paths.
  unset KEY_FILE SECRET_FILE
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
  KEY_FILE="$(resolve_path "${KEY_FILE:-$DEFAULT_KEY_FILE}")"
  SECRET_FILE="$(resolve_path "${SECRET_FILE:-$DEFAULT_SECRET_FILE}")"
  export KEY_FILE SECRET_FILE
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

# Generate a URL/argv-safe admin password (no quoting surprises in .env).
generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-24
  else
    head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-24
  fi
}

# Set KEY=value in a .env file, replacing an existing line if present.
set_env_var() {
  local key="$1" value="$2" file="${3:-$ROOT/.env}"
  local tmp="${file}.tmp"
  umask 077
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ "^" k "=" { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

aws_text() {
  aws "$@" --output text | tr -d '\r'
}

persist_state() {
  local tmp="$STATE_FILE.tmp"
  local keys=(VPC SUBNET SG KEY_FILE SECRET_FILE INSTANCE_ID ALLOC EIP HOST)
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
