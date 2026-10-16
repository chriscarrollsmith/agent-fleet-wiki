#!/usr/bin/env bash
# Create a BookStack user and print a one-time API token.
#
# BookStack has no artisan command for a normal user or an API token.
# This uploads a small PHP script and runs it as www-data on the instance.
#
# Usage:
#   bash deploy/create-user.sh --name "Cursor Agent" --email "cursor-agent@example.com"
#   bash deploy/create-user.sh --name "Cursor Agent" --email "cursor-agent@example.com" --role Agent
#   bash deploy/create-user.sh --sync-role
#
# Credentials are printed once and written to deploy/users/ (gitignored).
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_env

NAME=""
EMAIL=""
ROLE="Agent"
SYNC=0

usage() {
  cat <<'EOF'
Usage: bash deploy/create-user.sh --name NAME --email EMAIL [--role ROLE]
       bash deploy/create-user.sh --sync-role [--role ROLE]

Creates a BookStack user on the deployed instance and an API token.
The default role is Agent: a copy of Editor, plus access-api and the
attachment upload permissions the API requires.

--sync-role adds those permissions to an existing role. It does not
create a user or print a new token.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:-}"
      shift 2
      ;;
    --email)
      EMAIL="${2:-}"
      shift 2
      ;;
    --role)
      ROLE="${2:-}"
      shift 2
      ;;
    --sync-role)
      SYNC=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$ROLE" ]]; then
  usage >&2
  exit 1
fi

if [[ "$SYNC" -eq 0 && ( -z "$NAME" || -z "$EMAIL" ) ]]; then
  usage >&2
  exit 1
fi

if [[ ${#NAME} -gt 191 || ${#EMAIL} -gt 191 || ${#ROLE} -gt 191 ]]; then
  echo "Name, email, and role must be 191 characters or fewer." >&2
  exit 1
fi

if [[ "$NAME" =~ [[:cntrl:]] || "$EMAIL" =~ [[:cntrl:]] || "$ROLE" =~ [[:cntrl:]] ]]; then
  echo "Name, email, and role cannot contain control characters." >&2
  exit 1
fi

if [[ "$SYNC" -eq 0 && ! "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "Email looks invalid: ${EMAIL}" >&2
  exit 1
fi

: "${EIP:?Run deploy/05-instance.sh first}"
: "${KEY_FILE:?Run deploy/04-network.sh first}"

json_escape() {
  local s=$1 i c out=
  for ((i = 0; i < ${#s}; i++)); do
    c=${s:i:1}
    case "$c" in
      \\) out+='\\' ;;
      \") out+='\"' ;;
      *) out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

PASSWORD=""
if [[ "$SYNC" -eq 0 ]]; then
  PASSWORD="$(generate_password)"
  if [[ ${#PASSWORD} -lt 16 ]]; then
    echo "Could not generate a password." >&2
    exit 1
  fi
fi

payload="$(mktemp)"
chmod 600 "$payload"
cleanup_local() {
  rm -f "$payload"
}
trap cleanup_local EXIT

if [[ "$SYNC" -eq 1 ]]; then
  printf '{"mode":"sync-role","role":"%s"}\n' "$(json_escape "$ROLE")" > "$payload"
else
  printf '{"mode":"create","name":"%s","email":"%s","password":"%s","role":"%s"}\n' \
    "$(json_escape "$NAME")" \
    "$(json_escape "$EMAIL")" \
    "$(json_escape "$PASSWORD")" \
    "$(json_escape "$ROLE")" > "$payload"
fi

remote_id="$(generate_password | cut -c1-8)"
remote_php="/tmp/create-bookstack-user-${remote_id}.php"
remote_json="/tmp/create-bookstack-user-${remote_id}.json"

cleanup_remote() {
  ssh_ubuntu sudo rm -f "$remote_php" "$remote_json" >/dev/null 2>&1 || true
}
trap 'cleanup_local; cleanup_remote' EXIT

scp_to_ubuntu "$ROOT/deploy/create-user.php" "$remote_php"
scp_to_ubuntu "$payload" "$remote_json"

output="$(ssh_ubuntu sudo bash -s -- "$remote_php" "$remote_json" <<'REMOTE'
set -euo pipefail
php_file=$1
json_file=$2
cleanup() { rm -f "$php_file" "$json_file"; }
trap cleanup EXIT
chown root:www-data "$json_file" "$php_file"
chmod 640 "$json_file"
chmod 644 "$php_file"
sudo -u www-data php "$php_file" /var/www/bookstack "$json_file"
REMOTE
)"

if [[ "$SYNC" -eq 1 ]]; then
  role="" role_id="" added=""
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    case "$line" in
      role=*|role_id=*|added=*)
        key=${line%%=*}
        val=${line#*=}
        printf -v "$key" '%s' "$val"
        ;;
      *)
        echo "Unexpected response from BookStack: ${line}" >&2
        exit 1
        ;;
    esac
  done <<< "$output"

  for key in role role_id added; do
    if [[ -z "${!key}" ]]; then
      echo "BookStack did not return ${key}." >&2
      exit 1
    fi
  done

  echo "Updated role ${role} (id ${role_id})."
  echo "Added permissions: ${added}"
  exit 0
fi

user_id="" email="" name="" role="" role_id="" password="" token_id="" token_secret="" authorization=""
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  case "$line" in
    user_id=*|email=*|name=*|role=*|role_id=*|password=*|token_id=*|token_secret=*|authorization=*)
      key=${line%%=*}
      val=${line#*=}
      printf -v "$key" '%s' "$val"
      ;;
    *)
      echo "Unexpected response from BookStack: ${line}" >&2
      exit 1
      ;;
  esac
done <<< "$output"

for key in user_id email name role role_id password token_id token_secret authorization; do
  if [[ -z "${!key}" ]]; then
    echo "BookStack did not return ${key}." >&2
    exit 1
  fi
done

users_dir="$ROOT/deploy/users"
mkdir -p "$users_dir"
chmod 700 "$users_dir"
safe_email="$(printf '%s' "$EMAIL" | tr -c 'A-Za-z0-9._@-' '_')"
out_file="$users_dir/${safe_email}.env"

umask 077
{
  printf 'USER_ID=%q\n' "$user_id"
  printf 'EMAIL=%q\n' "$email"
  printf 'NAME=%q\n' "$name"
  printf 'ROLE=%q\n' "$role"
  printf 'ROLE_ID=%q\n' "$role_id"
  printf 'PASSWORD=%q\n' "$password"
  printf 'TOKEN_ID=%q\n' "$token_id"
  printf 'TOKEN_SECRET=%q\n' "$token_secret"
  printf 'AUTHORIZATION=%q\n' "$authorization"
} > "$out_file"

echo "Created BookStack user ${email} (id ${user_id}) in role ${role}."
echo "Password: ${password}"
echo "Authorization: ${authorization}"
echo "Saved to ${out_file}"
echo "Paste the Authorization line back. Do not commit deploy/users/."
