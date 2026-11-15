# Shared setup for the BookStack deploy stages.
# Sourced by the stage scripts. Not run directly.

# Git Bash (MSYS) rewrites arguments that look like POSIX paths, e.g. the
# /aws/service/... SSM parameter, into Windows paths. Disable that globally so
# callers never need to prefix scripts with MSYS_NO_PATHCONV=1.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ROLLOUT=<name> keeps one wiki's files in rollouts/<name>/ (gitignored).
# .env holds INFISICAL_TOKEN. Wiki settings are read from Infisical dev /wiki.
# Without ROLLOUT, a checkout is bound to one wiki through .env and deploy/.
if [[ -n "${ROLLOUT:-}" ]]; then
  ROLLOUT_DIR="$ROOT/rollouts/$ROLLOUT"
  ENV_FILE="$ROLLOUT_DIR/.env"
  STATE_FILE="$ROLLOUT_DIR/state.env"
  DEFAULT_KEY_FILE="$ROLLOUT_DIR/bookstack.pem"
  USERS_DIR="$ROLLOUT_DIR/users"
else
  ENV_FILE="$ROOT/.env"
  STATE_FILE="$ROOT/deploy/state.env"
  DEFAULT_KEY_FILE="$ROOT/deploy/bookstack.pem"
  USERS_DIR="$ROOT/deploy/users"
fi

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

infisical_py() {
  if command -v python3 >/dev/null 2>&1; then
    python3 "$ROOT/deploy/infisical_api.py" "$@"
  elif command -v python >/dev/null 2>&1; then
    python "$ROOT/deploy/infisical_api.py" "$@"
  else
    echo "python3 is required to read Infisical." >&2
    exit 1
  fi
}

# Read one KEY from an env file. The value may be bare, double-quoted, or single-quoted.
env_file_value() {
  local key="$1" file="$2" line value
  line="$(tr -d '\r' < "$file" | sed -n "s/^[[:space:]]*${key}=//p" | tail -n 1)"
  value="${line#"${line%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "$value" == \"*\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

load_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing ${ENV_FILE}" >&2
    echo "Copy .env.example to ${ENV_FILE} and set INFISICAL_TOKEN." >&2
    exit 1
  fi
  # The env file contributes the service token only. Wiki settings come from Infisical.
  INFISICAL_TOKEN="$(env_file_value INFISICAL_TOKEN "$ENV_FILE")"
  export INFISICAL_TOKEN
  if [[ -z "${INFISICAL_TOKEN}" ]]; then
    echo "Set INFISICAL_TOKEN in ${ENV_FILE}." >&2
    exit 1
  fi
  local env_key_file
  env_key_file="$(env_file_value KEY_FILE "$ENV_FILE")"
  local wiki
  wiki="$(infisical_py export-wiki)"
  set -a
  # shellcheck disable=SC1090
  source <(printf '%s\n' "$wiki")
  set +a
  if [[ -f "$STATE_FILE" ]]; then
    set -a
    source_file "$STATE_FILE"
    set +a
  fi
  # Older state files recorded an absolute KEY_FILE. The env file is the only override.
  unset KEY_FILE
  export AWS_PROFILE="${AWS_PROFILE:-fleet-coordinator}"
  : "${AWS_DEFAULT_REGION:?Set AWS_DEFAULT_REGION in Infisical dev /wiki}"
  export AWS_DEFAULT_REGION
  KEY_FILE="$(resolve_path "${env_key_file:-$DEFAULT_KEY_FILE}")"
  export KEY_FILE
}

require() {
  local name
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      echo "Set ${name} in Infisical dev /wiki." >&2
      exit 1
    fi
  done
}

# Generate a URL/argv-safe admin password.
generate_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-24
  else
    head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-24
  fi
}

aws_text() {
  aws "$@" --output text | tr -d '\r'
}

persist_state() {
  local tmp="$STATE_FILE.tmp"
  # KEY_FILE is derived on load, so an absolute path never goes stale.
  local keys=(VPC SUBNET SG INSTANCE_ID ALLOC EIP HOST)
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
