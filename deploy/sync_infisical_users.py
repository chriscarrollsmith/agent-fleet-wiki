#!/usr/bin/env python3
"""Push BookStack user credentials from deploy/users to Infisical.

The local credential file is the source. This creates /users/<identity> in the
dev environment when that folder is missing, then upserts the nine BookStack
fields. Matching remote values are left unchanged. IDENTITY is recorded on the
local file and is not uploaded.

INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET, and INFISICAL_PROJECT_ID come
from the process environment, or from .env when the environment does not set
them. The client id and secret belong to a machine identity created under the
project's Access Control.

Usage:
  uv run deploy/sync_infisical_users.py --identity pi-agent --file deploy/users/pi@example.com.env
  uv run deploy/sync_infisical_users.py
  uv run deploy/sync_infisical_users.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# ROLLOUT=<name> reads and writes rollouts/<name>/, matching deploy/create-user-http.py.
ROLLOUT = os.environ.get("ROLLOUT", "")
if ROLLOUT:
    ROLLOUT_DIR = ROOT / "rollouts" / ROLLOUT
    DEFAULT_ENV_FILE = ROLLOUT_DIR / ".env"
    DEFAULT_USERS_DIR = ROLLOUT_DIR / "users"
else:
    DEFAULT_ENV_FILE = ROOT / ".env"
    DEFAULT_USERS_DIR = ROOT / "deploy" / "users"

API_BASE = "https://app.infisical.com"
ENVIRONMENT = "dev"
USERS_PATH = "/users"

SECRET_KEYS = (
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
SECRET_KEY_SET = set(SECRET_KEYS)
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class InfisicalError(Exception):
    def __init__(self, status: int, message: str, path: str):
        self.status = status
        self.message = message
        self.path = path
        super().__init__(f"Infisical {path} failed ({status}): {message}")


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
    """Read a KEY=value file. Quoting matches deploy/create-user-http.py."""
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
    if re.fullmatch(r"[A-Za-z0-9._:@/+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "$'" + escaped + "'"


def validate_slug(identity: str) -> None:
    if not SLUG_RE.fullmatch(identity):
        die("--identity must be an agent slug: letters, digits, and . _ -")


def credentials_from(values: dict[str, str], path: Path | None = None) -> dict[str, str]:
    missing = [key for key in SECRET_KEYS if not values.get(key)]
    if missing:
        where = f"{path} is" if path else "Credential file is"
        die(f"{where} missing " + ", ".join(missing) + ".")
    return {key: values[key] for key in SECRET_KEYS}


def keys_to_write(local: dict[str, str], remote: dict[str, str | None]) -> dict[str, str]:
    """Return local secrets that are missing, hidden, or different on the remote."""
    return {key: local[key] for key in SECRET_KEYS if remote.get(key) != local[key]}


def upsert_body(project_id: str, secret_path: str, pending: dict[str, str]) -> dict[str, object]:
    return {
        "projectId": project_id,
        "environment": ENVIRONMENT,
        "secretPath": secret_path,
        "mode": "upsert",
        "secrets": [{"secretKey": key, "secretValue": pending[key]} for key in SECRET_KEYS if key in pending],
    }


def error_message(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    message: object = "request failed"
    if isinstance(parsed, dict):
        message = parsed.get("message", parsed.get("error", "request failed"))
    if not isinstance(message, str):
        message = json.dumps(message)
    return message[:500]


def parse_json(raw: str) -> dict:
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        die("Infisical returned a response that was not JSON.")
    if not isinstance(body, dict):
        die("Infisical returned a response that was not a JSON object.")
    return body


class Infisical:
    def __init__(self, base_url: str, client_id: str, client_secret: str, project_id: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.project_id = project_id
        self.token = ""

    def login(self) -> None:
        body = self._request(
            "POST",
            "/api/v1/auth/universal-auth/login",
            body={"clientId": self.client_id, "clientSecret": self.client_secret},
        )
        token = body.get("accessToken")
        if not isinstance(token, str) or not token:
            die("Infisical login did not return an access token.")
        self.token = token

    def folder_names(self, path: str) -> list[str]:
        try:
            payload = self._request(
                "GET",
                "/api/v2/folders",
                query={"projectId": self.project_id, "environment": ENVIRONMENT, "path": path},
            )
        except InfisicalError as exc:
            if exc.status == 404:
                die(f"Infisical path {path} does not exist in the {ENVIRONMENT} environment.")
            raise
        folders = payload.get("folders")
        if not isinstance(folders, list):
            die(f"Infisical folder list at {path} did not include a folders array.")
        names: list[str] = []
        for folder in folders:
            if not isinstance(folder, dict) or not isinstance(folder.get("name"), str):
                die(f"Infisical folder list at {path} included an entry without a name.")
            names.append(folder["name"])
        return names

    def create_folder(self, name: str, path: str) -> None:
        self._request(
            "POST",
            "/api/v2/folders",
            body={"projectId": self.project_id, "environment": ENVIRONMENT, "path": path, "name": name},
        )

    def secret_values(self, secret_path: str) -> dict[str, str | None]:
        try:
            payload = self._request(
                "GET",
                "/api/v4/secrets",
                query={
                    "projectId": self.project_id,
                    "environment": ENVIRONMENT,
                    "secretPath": secret_path,
                    "viewSecretValue": "true",
                    "expandSecretReferences": "false",
                    "includeImports": "false",
                    "recursive": "false",
                },
            )
        except InfisicalError as exc:
            if exc.status == 404:
                return {}
            raise
        secrets = payload.get("secrets")
        if secrets is None:
            return {}
        if not isinstance(secrets, list):
            die(f"Infisical secret list at {secret_path} did not include a secrets array.")
        found: dict[str, str | None] = {}
        for item in secrets:
            if not isinstance(item, dict) or not isinstance(item.get("secretKey"), str):
                die(f"Infisical secret list at {secret_path} included an entry without a key.")
            key = item["secretKey"]
            if key not in SECRET_KEY_SET:
                continue
            value = item.get("secretValue")
            if item.get("secretValueHidden") is True or not isinstance(value, str):
                found[key] = None
            else:
                found[key] = value
        return found

    def upsert(self, secret_path: str, pending: dict[str, str]) -> None:
        self._request("PATCH", "/api/v4/secrets/batch", body=upsert_body(self.project_id, secret_path, pending))

    def _request(self, method: str, path: str, query: dict[str, str] | None = None, body: dict | None = None) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise InfisicalError(exc.code, error_message(detail), f"{method} {path}") from None
        except urllib.error.URLError as exc:
            raise InfisicalError(0, f"could not reach {self.base_url}: {exc.reason}", f"{method} {path}") from None
        return parse_json(raw)


def sync_user(client: Infisical, identity: str, values: dict[str, str], dry_run: bool, path: Path | None = None) -> list[str]:
    validate_slug(identity)
    local = credentials_from(values, path)
    secret_path = f"{USERS_PATH}/{identity}"
    missing_folder = identity not in client.folder_names(USERS_PATH)
    remote = {} if missing_folder else client.secret_values(secret_path)
    pending = keys_to_write(local, remote)
    lines: list[str] = []
    if missing_folder:
        action = "would create" if dry_run else "created"
        lines.append(f"{identity}: {action} {secret_path}")
        if not dry_run:
            client.create_folder(identity, USERS_PATH)
    if not pending:
        lines.append(f"{identity}: already in sync")
        return lines
    key_list = ", ".join(key for key in SECRET_KEYS if key in pending)
    if dry_run:
        lines.append(f"{identity}: would write {key_list}")
        return lines
    client.upsert(secret_path, pending)
    lines.append(f"{identity}: wrote {key_list}")
    return lines


def identity_for(path: Path, values: dict[str, str], flag: str | None) -> str:
    recorded = values.get("IDENTITY", "")
    if flag and recorded and flag != recorded:
        die(f"{path} has IDENTITY={recorded}, which does not match --identity {flag}.")
    identity = flag or recorded
    if not identity:
        die(f"{path} has no IDENTITY. Pass --identity with --file.")
    validate_slug(identity)
    return identity


def record_identity(path: Path, identity: str, values: dict[str, str]) -> None:
    if values.get("IDENTITY") == identity:
        return
    if values.get("IDENTITY"):
        die(f"{path} has IDENTITY={values['IDENTITY']}, which does not match {identity}.")
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + f"IDENTITY={shell_quote(identity)}\n", encoding="utf-8")


def jobs_from_args(args: argparse.Namespace) -> list[tuple[str, Path]]:
    if args.identity and not args.file:
        die("Pass --file with --identity. A directory sync reads IDENTITY from each file.")
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            die(f"Credential file not found: {path}")
        return [(identity_for(path, load_env_file(path), args.identity), path)]
    users_dir = Path(args.users_dir)
    if not users_dir.is_dir():
        die(f"Credential directory not found: {users_dir}")
    files = sorted(path for path in users_dir.glob("*.env") if path.is_file())
    if not files:
        die(f"No credential files in {users_dir}.")
    return [(identity_for(path, load_env_file(path), None), path) for path in files]


def resolve_infisical(env_file: Path) -> tuple[str, str, str]:
    values = load_env_file(env_file)

    def pick(key: str) -> str:
        process = os.environ.get(key, "")
        if process:
            return process
        return values.get(key, "")

    client_id = pick("INFISICAL_CLIENT_ID")
    client_secret = pick("INFISICAL_CLIENT_SECRET")
    project_id = pick("INFISICAL_PROJECT_ID")
    if not client_id or not client_secret or not project_id:
        die(
            "Set INFISICAL_CLIENT_ID, INFISICAL_CLIENT_SECRET, and INFISICAL_PROJECT_ID "
            f"in the environment or {env_file}."
        )
    return client_id, client_secret, project_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync-infisical-users",
        description="Push local BookStack user credentials to Infisical.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The local file is the source. Matching secrets are left as they are.\n"
            "A different remote value is overwritten. IDENTITY stays on the local\n"
            "file and is not uploaded.\n\n"
            "Examples:\n"
            "  bash deploy/sync-infisical-users.sh --identity pi-agent --file deploy/users/pi@example.com.env\n"
            "  bash deploy/sync-infisical-users.sh\n"
            "  bash deploy/sync-infisical-users.sh --dry-run"
        ),
    )
    parser.add_argument("--identity", help="agent slug for /users/<identity>; required with --file when the file has no IDENTITY")
    parser.add_argument("--file", help="one deploy/users credential file to push")
    parser.add_argument("--users-dir", default=str(DEFAULT_USERS_DIR), help="directory of credential files (default: deploy/users, or rollouts/$ROLLOUT/users)")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="env file with Infisical credentials (default: .env, or rollouts/$ROLLOUT/.env)")
    parser.add_argument("--dry-run", action="store_true", help="show folders and secret names that would change")
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    jobs = jobs_from_args(args)
    client_id, client_secret, project_id = resolve_infisical(Path(args.env_file))
    client = Infisical(args.api_base, client_id, client_secret, project_id)
    try:
        client.login()
        for identity, path in jobs:
            values = load_env_file(path)
            for line in sync_user(client, identity, values, args.dry_run, path):
                print(line)
            if not args.dry_run:
                record_identity(path, identity, values)
    except InfisicalError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
