#!/usr/bin/env python3
"""Create a BookStack user and API token over HTTPS, without SSH.

BookStack has no artisan command for a normal user or an API token, but its
admin web UI can do both. This script drives the same routes a browser uses,
so it needs only the site URL and the admin credentials. It is the default
behind deploy/create-user.sh. deploy/create-user-ssh.sh is the fallback that
runs PHP on the instance.

Usage:
  uv run deploy/create-user-http.py --name "Pi Agent" --email pi@example.com
  uv run deploy/create-user-http.py --name "Pi Agent" --email pi@example.com --role Agent
  uv run deploy/create-user-http.py --name "Pi Agent" --email pi@example.com --identity pi-agent
  uv run deploy/create-user-http.py --sync-role

The default role, Agent, is a copy of Editor plus the permissions the API
needs for normal research work. Credentials are printed once. Pass --identity
to store them in Infisical at dev /users/<folder>, the slug with each dot
replaced by a hyphen. A failed Infisical write leaves a file under deploy/users/.
See docs/provision.md.
"""

from __future__ import annotations

import argparse
import html
import http.cookiejar
import os
import re
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from infisical_api import USERS_PATH, WIKI_PATH, InfisicalClient, InfisicalError, folder_name, token_from_env

ROOT = Path(__file__).resolve().parent.parent
# ROLLOUT=<name> reads and writes rollouts/<name>/, matching deploy/lib.sh.
ROLLOUT = os.environ.get("ROLLOUT", "")
if ROLLOUT:
    ROLLOUT_DIR = ROOT / "rollouts" / ROLLOUT
    DEFAULT_ENV_FILE = ROLLOUT_DIR / ".env"
    DEFAULT_STATE_FILE = ROLLOUT_DIR / "state.env"
    DEFAULT_USERS_DIR = ROLLOUT_DIR / "users"
else:
    DEFAULT_ENV_FILE = ROOT / ".env"
    DEFAULT_STATE_FILE = ROOT / "deploy" / "state.env"
    DEFAULT_USERS_DIR = ROOT / "deploy" / "users"

AGENT_ROLE_DESCRIPTION = (
    "Fleet agent. Editor content permissions, plus API, export, attachment, image, and comment access."
)

# Permissions a research agent needs beyond a plain Editor role.
# A fresh install grants attachment and comment permissions to Admin only.
# Keep in sync with deploy/create-user.php.
AGENT_PERMISSIONS = [
    "access-api",
    "content-export",
    "attachment-create-all",
    "attachment-update-all",
    "attachment-delete-all",
    "image-create-all",
    "image-update-own",
    "image-delete-own",
    "comment-create-all",
    "comment-update-own",
    "comment-delete-own",
]

CSRF_RE = re.compile(r'name="_token"\s+value="([^"]+)"')
ROLE_LINK_RE = re.compile(r'href="[^"]*/settings/roles/(\d+)"[^>]*>([^<]*)</a>')
USER_LINK_RE = re.compile(r'href="[^"]*/settings/users/(\d+)"[^>]*>([\s\S]*?)</a>')
PERMISSION_RE = re.compile(r'name="permissions\[([^\]]+)\]"[^>]*\schecked')
READONLY_VALUE_RE = re.compile(r'<input[^>]*readonly[^>]*value="([^"]+)"')
TAG_RE = re.compile(r"<[^>]+>")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def unquote_env(value: str) -> str:
    if value.startswith("$'") and value.endswith("'"):
        return bytes(value[2:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    # deploy/lib.sh writes values with printf %q, which escapes spaces.
    return value.replace("\\ ", " ")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = unquote_env(value.strip())
    return values


def shell_quote(value: str) -> str:
    """Quote a value for a KEY=value file that Python and bash both read."""
    if re.fullmatch(r"[A-Za-z0-9._:@/+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "$'" + escaped + "'"


def generate_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


class Web:
    """A cookie-backed session against the BookStack web UI."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(self, path: str) -> tuple[str, str]:
        response = self.opener.open(self.base_url + path)
        return response.geturl(), response.read().decode("utf-8")

    def post(self, path: str, fields: dict[str, str]) -> tuple[str, str]:
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method="POST")
        response = self.opener.open(request)
        return response.geturl(), response.read().decode("utf-8")

    def csrf_from(self, path: str) -> str:
        _, page = self.get(path)
        return csrf(page)


def csrf(page: str) -> str:
    match = CSRF_RE.search(page)
    if not match:
        die("Could not find a CSRF token on the page. Is the URL a BookStack instance?")
    return match.group(1)


def strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def login(web: Web, email: str, password: str) -> None:
    token = web.csrf_from("/login")
    web.post("/login", {"_token": token, "email": email, "password": password})
    final_url, _ = web.get("/settings/users")
    if "/login" in final_url:
        die("Admin login failed. Check ADMIN_EMAIL and ADMIN_PASSWORD in Infisical dev /wiki.")


def list_roles(web: Web) -> dict[str, int]:
    _, page = web.get("/settings/roles")
    return {html.unescape(name.strip()): int(role_id) for role_id, name in ROLE_LINK_RE.findall(page)}


def role_state(web: Web, role_id: int) -> tuple[set[str], str, str, bool]:
    """Return (permissions, display_name, description, mfa_enforced) for a role."""
    _, page = web.get(f"/settings/roles/{role_id}")
    permissions = set(PERMISSION_RE.findall(page))
    name_match = re.search(r'name="display_name"[^>]*value="([^"]*)"', page)
    description_match = re.search(r'name="description"[^>]*value="([^"]*)"', page)
    mfa = bool(re.search(r'name="mfa_enforced"[^>]*\schecked', page))
    name = html.unescape(name_match.group(1)) if name_match else ""
    description = html.unescape(description_match.group(1)) if description_match else ""
    return permissions, name, description, mfa


def create_role(web: Web, name: str, description: str, permissions: set[str]) -> None:
    token = web.csrf_from("/settings/roles/new")
    fields = {"_token": token, "display_name": name, "description": description}
    for permission in sorted(permissions):
        fields[f"permissions[{permission}]"] = "true"
    web.post("/settings/roles/new", fields)


def update_role(web: Web, role_id: int, name: str, description: str, mfa: bool, permissions: set[str]) -> None:
    token = web.csrf_from(f"/settings/roles/{role_id}")
    fields = {
        "_token": token,
        "_method": "PUT",
        "display_name": name,
        "description": description,
    }
    if mfa:
        fields["mfa_enforced"] = "true"
    for permission in sorted(permissions):
        fields[f"permissions[{permission}]"] = "true"
    web.post(f"/settings/roles/{role_id}", fields)


def ensure_agent_role(web: Web, role_name: str) -> int:
    roles = list_roles(web)
    if role_name in roles:
        role_id = roles[role_name]
        permissions, _, _, _ = role_state(web, role_id)
        if "access-api" not in permissions:
            die(
                f'Role "{role_name}" exists but cannot call the API (missing access-api). '
                f"Leave --role unset to create an Agent role, run --sync-role to add the "
                f"permissions, or add access-api in BookStack first."
            )
        return role_id

    editor_id = roles.get("Editor")
    if editor_id is None:
        known = ", ".join(roles) or "none"
        die(f"No Editor role to clone. Roles on this instance: {known}")
    editor_permissions, _, _, _ = role_state(web, editor_id)
    if not editor_permissions:
        die('The Editor role has no permissions to copy.')
    create_role(web, role_name, AGENT_ROLE_DESCRIPTION, editor_permissions | set(AGENT_PERMISSIONS))
    roles = list_roles(web)
    if role_name not in roles:
        die(f'Created the "{role_name}" role but could not find it afterwards.')
    return roles[role_name]


def find_user(web: Web, email: str) -> int | None:
    _, page = web.get("/settings/users")
    for user_id, fragment in USER_LINK_RE.findall(page):
        if email.lower() in strip_tags(fragment).lower():
            return int(user_id)
    return None


def create_user(web: Web, name: str, email: str, password: str, role_id: int) -> int:
    token = web.csrf_from("/settings/users/create")
    fields = {
        "_token": token,
        "name": name,
        "email": email,
        "external_auth_id": "",
        "roles[0]": "0",
        f"roles[{role_id}]": str(role_id),
        "send_invite": "false",
        "password": password,
        "password-confirm": password,
    }
    web.post("/settings/users/create", fields)
    user_id = find_user(web, email)
    if user_id is None:
        die(f"BookStack did not create the user {email}. Re-run with the same email to see the error in the UI.")
    return user_id


def api_ok(base_url: str, token: str) -> bool:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/system",
        headers={"Authorization": f"Token {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return False


def create_token(web: Web, base_url: str, user_id: int) -> tuple[str, str]:
    token = web.csrf_from(f"/api-tokens/{user_id}/create?context=settings")
    _, page = web.post(f"/api-tokens/{user_id}/create", {"_token": token, "name": "fleet", "expires_at": ""})
    values = READONLY_VALUE_RE.findall(page)
    if len(values) < 2:
        die("BookStack did not show the new API token. It may already have been displayed once.")
    token_id, token_secret = values[0], values[1]
    # The token page renders the id and secret; confirm which is which.
    if not api_ok(base_url, f"{token_id}:{token_secret}"):
        if api_ok(base_url, f"{token_secret}:{token_id}"):
            token_id, token_secret = token_secret, token_id
        else:
            die("Created an API token but it did not authenticate. Check the BookStack instance.")
    return token_id, token_secret


def write_user_file(users_dir: Path, email: str, values: dict[str, str]) -> Path:
    safe_email = re.sub(r"[^A-Za-z0-9._@-]", "_", email)
    users_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(users_dir, 0o700)
    except OSError:
        pass
    out_file = users_dir / f"{safe_email}.env"
    lines = [f"{key}={shell_quote(value)}" for key, value in values.items()]
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(out_file, 0o600)
    except OSError:
        pass
    return out_file


def sync_role(web: Web, role_name: str) -> None:
    roles = list_roles(web)
    if role_name not in roles:
        die(f'Role "{role_name}" does not exist.')
    role_id = roles[role_name]
    current, name, description, mfa = role_state(web, role_id)
    missing = [permission for permission in AGENT_PERMISSIONS if permission not in current]
    if missing:
        update_role(web, role_id, name, description, mfa, current | set(missing))
    print(f"Updated role {name} (id {role_id}).")
    print("Added permissions: " + (",".join(missing) if missing else "none"))


def validate_slug(identity: str) -> None:
    if not SLUG_RE.fullmatch(identity):
        die("--identity must be an agent slug: letters, digits, and . _ -")


USER_SECRET_KEYS = (
    "USER_ID",
    "EMAIL",
    "NAME",
    "ROLE",
    "ROLE_ID",
    "PASSWORD",
    "TOKEN_ID",
    "TOKEN_SECRET",
    "AUTHORIZATION",
)


def require_infisical(env_file: str) -> None:
    try:
        token_from_env(Path(env_file))
    except InfisicalError as exc:
        die(str(exc))


def push_infisical(identity: str, fields: dict[str, str], env_file: str, users_dir: Path, email: str) -> None:
    name = folder_name(identity)
    secret_path = f"{USERS_PATH}/{name}"
    pending = {key: fields[key] for key in USER_SECRET_KEYS}
    try:
        client = InfisicalClient.from_token(token_from_env(Path(env_file)))
        client.ensure_folder(secret_path)
        client.upsert(secret_path, pending)
        remote = client.secrets(secret_path)
    except InfisicalError as exc:
        saved = dict(pending)
        saved["IDENTITY"] = identity
        out_file = write_user_file(users_dir, email, saved)
        die(
            f"Infisical did not store the new user. Credentials were written to {out_file}. {exc} "
            f"Re-run: bash deploy/sync-infisical-users.sh --identity {identity} --file {out_file}"
        )
    mismatched = [key for key in USER_SECRET_KEYS if remote.get(key) != pending[key]]
    if mismatched:
        saved = dict(pending)
        saved["IDENTITY"] = identity
        out_file = write_user_file(users_dir, email, saved)
        die(
            f"Infisical read-back mismatch for {', '.join(mismatched)}. "
            f"Credentials were written to {out_file}. "
            f"Re-run: bash deploy/sync-infisical-users.sh --identity {identity} --file {out_file}"
        )
    print(f"Stored in Infisical at dev {secret_path}.")


def create_user_and_token(
    web: Web,
    base_url: str,
    users_dir: Path,
    name: str,
    email: str,
    role_name: str,
    identity: str | None,
    env_file: str,
) -> None:
    if find_user(web, email) is not None:
        die(f"A user with email {email} already exists. Delete it first, or use a different email.")

    role_id = ensure_agent_role(web, role_name)
    password = generate_password()
    user_id = create_user(web, name, email, password, role_id)
    token_id, token_secret = create_token(web, base_url, user_id)
    authorization = f"Token {token_id}:{token_secret}"
    fields = {
        "USER_ID": str(user_id),
        "EMAIL": email,
        "NAME": name,
        "ROLE": role_name,
        "ROLE_ID": str(role_id),
        "PASSWORD": password,
        "TOKEN_ID": token_id,
        "TOKEN_SECRET": token_secret,
        "AUTHORIZATION": authorization,
    }
    if identity:
        fields["IDENTITY"] = identity

    print(f"Created BookStack user {email} (id {user_id}) in role {role_name}.")
    print(f"Password: {password}")
    print(f"Authorization: {authorization}")
    if identity:
        push_infisical(identity, fields, env_file, users_dir, email)
    else:
        out_file = write_user_file(users_dir, email, fields)
        print(f"Saved to {out_file}")
        print(f"Sync to Infisical with: bash deploy/sync-infisical-users.sh --identity <identity> --file {out_file}")
        print(f"Do not commit {users_dir}.")
    print("Paste the Authorization line back.")


def validate_identity(name: str, email: str, role: str) -> None:
    if len(name) > 191 or len(email) > 191 or len(role) > 191:
        die("Name, email, and role must be 191 characters or fewer.")
    if any(ord(char) < 32 for char in name + email + role):
        die("Name, email, and role cannot contain control characters.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        die(f"Email looks invalid: {email}")


def resolve_config(args: argparse.Namespace) -> tuple[str, str, str]:
    try:
        client = InfisicalClient.from_token(token_from_env(Path(args.env_file)))
        values = client.secrets(WIKI_PATH)
    except InfisicalError as exc:
        die(str(exc))
    state = load_env_file(Path(args.state_file))

    url = args.url or values.get("BOOKSTACK_URL", "")
    host = state.get("HOST", "")
    if not url and host:
        url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    if not url:
        die("Set BOOKSTACK_URL in Infisical dev /wiki, or pass --url.")

    admin_email = args.admin_email or values.get("ADMIN_EMAIL", "")
    admin_password = args.admin_password or values.get("ADMIN_PASSWORD", "")
    if not admin_email:
        die("Set ADMIN_EMAIL in Infisical dev /wiki, or pass --admin-email.")
    if not admin_password:
        die("Set ADMIN_PASSWORD in Infisical dev /wiki, or pass --admin-password.")
    return url, admin_email, admin_password


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create-user-http.py",
        description="Create a BookStack user and API token over HTTPS, without SSH.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Creates a BookStack user through the admin web UI and prints an API token.\n"
            "The default role is Agent: a copy of Editor, plus access-api and the\n"
            "attachment, image, and comment permissions the API requires.\n\n"
            "--sync-role adds those permissions to an existing role. It does not\n"
            "create a user or print a new token.\n\n"
            "--identity stores the new credentials in Infisical at dev /users/<folder>.\n"
            "Each dot in the slug becomes a hyphen in that folder name. A failed\n"
            "write leaves the credentials in the users directory."
        ),
    )
    parser.add_argument("--name", help="display name for the new user")
    parser.add_argument("--email", help="email for the new user")
    parser.add_argument("--identity", help="agent slug; also sync the new user to Infisical. Dots in the slug become hyphens in the folder name")
    parser.add_argument("--role", default="Agent", help="role to assign (default: Agent)")
    parser.add_argument("--sync-role", action="store_true", help="add missing agent permissions to --role")
    parser.add_argument("--url", help="BookStack base URL, overriding BOOKSTACK_URL")
    parser.add_argument("--admin-email", help="admin email, overriding ADMIN_EMAIL")
    parser.add_argument("--admin-password", help="admin password, overriding ADMIN_PASSWORD")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="env file with INFISICAL_TOKEN (default: .env, or rollouts/$ROLLOUT/.env)")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE), help="deploy state file for HOST (default: deploy/state.env, or rollouts/$ROLLOUT/state.env)")
    parser.add_argument("--users-dir", default=str(DEFAULT_USERS_DIR), help="where to write the credentials file (default: deploy/users, or rollouts/$ROLLOUT/users)")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if not args.role:
        die("--role cannot be empty.")
    if not args.sync_role and (not args.name or not args.email):
        die("Provide --name and --email, or pass --sync-role.")
    if not args.sync_role:
        validate_identity(args.name, args.email, args.role)
    if args.identity and args.sync_role:
        die("--identity applies when creating a user, not with --sync-role.")
    if args.identity:
        validate_slug(args.identity)
        require_infisical(args.env_file)

    base_url, admin_email, admin_password = resolve_config(args)
    web = Web(base_url)
    login(web, admin_email, admin_password)

    if args.sync_role:
        sync_role(web, args.role)
        return
    create_user_and_token(
        web,
        base_url,
        Path(args.users_dir),
        args.name,
        args.email,
        args.role,
        args.identity,
        args.env_file,
    )


if __name__ == "__main__":
    main()
