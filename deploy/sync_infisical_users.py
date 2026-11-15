#!/usr/bin/env python3
"""Push a local BookStack user file to Infisical.

Use this for a file written by the SSH fallback, or when an earlier Infisical
write failed and left the file on disk. The upload creates /users/<folder> in
the dev environment when that folder is missing, then upserts the nine
BookStack fields. Infisical folder names cannot contain dots, so each dot in
the agent slug becomes a hyphen. Matching remote values are left unchanged.
IDENTITY is not uploaded. After a successful upload the local file is removed.

INFISICAL_TOKEN comes from the process environment, or from .env when the
environment does not set it. The token is a service token created under the
project's Access Control.

Usage:
  uv run deploy/sync_infisical_users.py --identity pi-agent --file deploy/users/pi@example.com.env
  uv run deploy/sync_infisical_users.py
  uv run deploy/sync_infisical_users.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from infisical_api import API_BASE, USERS_PATH, InfisicalClient, InfisicalError, folder_name, token_from_env

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


def sync_user(client: InfisicalClient, identity: str, values: dict[str, str], dry_run: bool, path: Path | None = None) -> list[str]:
    validate_slug(identity)
    local = credentials_from(values, path)
    name = folder_name(identity)
    secret_path = f"{USERS_PATH}/{name}"
    missing_folder = name not in client.folder_names(USERS_PATH)
    remote = {} if missing_folder else {key: value for key, value in client.secret_values(secret_path).items() if key in SECRET_KEYS}
    pending = keys_to_write(local, remote)
    lines: list[str] = []
    if missing_folder:
        action = "would create" if dry_run else "created"
        lines.append(f"{identity}: {action} {secret_path}")
        if not dry_run:
            client.create_folder(name, USERS_PATH)
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync-infisical-users",
        description="Push local BookStack user credentials to Infisical.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Matching secrets are left as they are. A different remote value is\n"
            "overwritten. IDENTITY is not uploaded. A successful upload removes\n"
            "the local file.\n\n"
            "Examples:\n"
            "  bash deploy/sync-infisical-users.sh --identity pi-agent --file deploy/users/pi@example.com.env\n"
            "  bash deploy/sync-infisical-users.sh\n"
            "  bash deploy/sync-infisical-users.sh --dry-run"
        ),
    )
    parser.add_argument(
        "--identity",
        help="agent slug; the Infisical folder is that slug with each . replaced by -. Required with --file when the file has no IDENTITY",
    )
    parser.add_argument("--file", help="one deploy/users credential file to push")
    parser.add_argument("--users-dir", default=str(DEFAULT_USERS_DIR), help="directory of credential files (default: deploy/users, or rollouts/$ROLLOUT/users)")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="env file with INFISICAL_TOKEN (default: .env, or rollouts/$ROLLOUT/.env)")
    parser.add_argument("--dry-run", action="store_true", help="show folders and secret names that would change")
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    jobs = jobs_from_args(args)
    try:
        client = InfisicalClient.from_token(token_from_env(Path(args.env_file)), args.api_base)
        for identity, path in jobs:
            values = load_env_file(path)
            for line in sync_user(client, identity, values, args.dry_run, path):
                print(line)
            if not args.dry_run:
                path.unlink()
    except InfisicalError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
