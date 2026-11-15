#!/usr/bin/env python3
"""Infisical access for this repo, using a service token.

The token in INFISICAL_TOKEN is sent as a bearer token. Its project id comes
from GET /api/v2/service-token. Wiki settings live at dev /wiki. BookStack
agent accounts live at dev /users/<folder>. Human passwords live at
dev /humans/<folder>.

Bash stages source the export-wiki command. Writers pipe KEY=value lines to
the upsert command. Secret values stay on stdin and stdout, not in argv.

Usage:
  python deploy/infisical_api.py export-wiki
  python deploy/infisical_api.py upsert /wiki <<'EOF'
  ADMIN_PASSWORD=value
  EOF
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

API_BASE = "https://app.infisical.com"
ENVIRONMENT = "dev"
WIKI_PATH = "/wiki"
USERS_PATH = "/users"
HUMANS_PATH = "/humans"

ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")


class InfisicalError(Exception):
    def __init__(self, status: int, message: str, path: str):
        self.status = status
        self.message = message
        self.path = path
        super().__init__(f"Infisical {path} failed ({status}): {message}" if status else message)


def service_token_project(payload: dict) -> str:
    project = payload.get("projectId") or payload.get("workspace") or ""
    if not isinstance(project, str) or not project:
        raise InfisicalError(0, "Infisical service token did not include a project id.", "GET /api/v2/service-token")
    return project


def folder_name(identity: str) -> str:
    """Infisical folder names reject dots, so each dot in a slug becomes a hyphen.

    ``deepseek-flash-4.1-pi-crime-agent`` is stored at
    ``/users/deepseek-flash-4-1-pi-crime-agent``.
    """
    return identity.replace(".", "-")


def human_folder(email: str) -> str:
    """Folder name for a person who is not an agent account.

    ``frank@example.com`` is stored at ``/humans/frank-example-com``.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", email.strip().lower()).strip("-")
    if not cleaned:
        raise InfisicalError(0, f"Cannot store a human password for {email}.", HUMANS_PATH)
    return cleaned


def unquote_env(value: str) -> str:
    if value.startswith("$'") and value.endswith("'"):
        return bytes(value[2:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value.replace("\\ ", " ")


def read_env_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
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


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return read_env_text(path.read_text(encoding="utf-8"))


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:@/+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "$'" + escaped + "'"


def format_env(values: dict[str, str]) -> str:
    lines: list[str] = []
    for key in sorted(values):
        if not ENV_KEY_RE.fullmatch(key):
            raise InfisicalError(0, f"Infisical secret {key} is not an environment variable name.", WIKI_PATH)
        value = values[key]
        if "\n" in value or "\r" in value or "\x00" in value:
            raise InfisicalError(0, f"Infisical secret {key} contains a newline.", WIKI_PATH)
        lines.append(f"{key}={shell_quote(value)}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def token_from_env(env_file: Path | None = None) -> str:
    process = os.environ.get("INFISICAL_TOKEN", "")
    if process:
        return process
    if env_file is not None:
        file_token = read_env_file(env_file).get("INFISICAL_TOKEN", "")
        if file_token:
            return file_token
    where = f" in {env_file}" if env_file else ""
    raise InfisicalError(0, f"Set INFISICAL_TOKEN{where}.", "token")


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
    except json.JSONDecodeError as exc:
        raise InfisicalError(0, "Infisical returned a response that was not JSON.", "response") from exc
    if not isinstance(body, dict):
        raise InfisicalError(0, "Infisical returned a response that was not a JSON object.", "response")
    return body


class InfisicalClient:
    def __init__(self, token: str, project_id: str, base_url: str = API_BASE):
        self.token = token
        self.project_id = project_id
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_token(cls, token: str, base_url: str = API_BASE) -> InfisicalClient:
        client = cls(token, "", base_url)
        payload = client.request("GET", "/api/v2/service-token")
        client.project_id = service_token_project(payload)
        return client

    def request(self, method: str, path: str, query: dict[str, str] | None = None, body: dict | None = None) -> dict:
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
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise InfisicalError(exc.code, error_message(detail), f"{method} {path}") from None
        except urllib.error.URLError as exc:
            raise InfisicalError(0, f"could not reach {self.base_url}: {exc.reason}", f"{method} {path}") from None
        return parse_json(raw)

    def folder_names(self, path: str) -> list[str]:
        try:
            payload = self.request(
                "GET",
                "/api/v2/folders",
                query={"projectId": self.project_id, "environment": ENVIRONMENT, "path": path},
            )
        except InfisicalError as exc:
            if exc.status == 404:
                raise InfisicalError(
                    404,
                    f"Infisical path {path} does not exist in the {ENVIRONMENT} environment.",
                    "GET /api/v2/folders",
                ) from None
            raise
        folders = payload.get("folders")
        if not isinstance(folders, list):
            raise InfisicalError(0, f"Infisical folder list at {path} did not include a folders array.", "GET /api/v2/folders")
        names: list[str] = []
        for folder in folders:
            if not isinstance(folder, dict) or not isinstance(folder.get("name"), str):
                raise InfisicalError(0, f"Infisical folder list at {path} included an entry without a name.", "GET /api/v2/folders")
            names.append(folder["name"])
        return names

    def create_folder(self, name: str, path: str) -> None:
        self.request(
            "POST",
            "/api/v2/folders",
            body={"projectId": self.project_id, "environment": ENVIRONMENT, "path": path, "name": name},
        )

    def ensure_folder(self, secret_path: str) -> None:
        """Create each missing segment of secret_path. /users itself is never created."""
        parts = [part for part in secret_path.split("/") if part]
        if not parts:
            raise InfisicalError(0, "Infisical secret path is empty.", "folder")
        parent = "/"
        for index, name in enumerate(parts):
            existing = self.folder_names(parent)
            if name not in existing:
                if index == 0 and name == "users":
                    raise InfisicalError(
                        404,
                        f"Infisical path /users does not exist in the {ENVIRONMENT} environment.",
                        "GET /api/v2/folders",
                    )
                self.create_folder(name, parent)
            parent = f"/{name}" if parent == "/" else f"{parent}/{name}"

    def secret_values(self, secret_path: str) -> dict[str, str | None]:
        """Return secret values. A hidden value is None. A missing path raises."""
        try:
            payload = self.request(
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
                raise InfisicalError(
                    404,
                    f"Infisical path {secret_path} does not exist in the {ENVIRONMENT} environment.",
                    "GET /api/v4/secrets",
                ) from None
            raise
        secrets = payload.get("secrets")
        if secrets is None:
            return {}
        if not isinstance(secrets, list):
            raise InfisicalError(0, f"Infisical secret list at {secret_path} did not include a secrets array.", "GET /api/v4/secrets")
        found: dict[str, str | None] = {}
        for item in secrets:
            if not isinstance(item, dict) or not isinstance(item.get("secretKey"), str):
                raise InfisicalError(0, f"Infisical secret list at {secret_path} included an entry without a key.", "GET /api/v4/secrets")
            key = item["secretKey"]
            value = item.get("secretValue")
            if item.get("secretValueHidden") is True or not isinstance(value, str):
                found[key] = None
            else:
                found[key] = value
        return found

    def secrets(self, secret_path: str) -> dict[str, str]:
        raw = self.secret_values(secret_path)
        hidden = [key for key, value in raw.items() if value is None]
        if hidden:
            raise InfisicalError(
                0,
                f"Infisical hid values at {secret_path}: {', '.join(hidden)}.",
                "GET /api/v4/secrets",
            )
        return {key: value for key, value in raw.items() if isinstance(value, str)}

    def upsert(self, secret_path: str, pending: dict[str, str]) -> None:
        if not pending:
            return
        self.request(
            "PATCH",
            "/api/v4/secrets/batch",
            body={
                "projectId": self.project_id,
                "environment": ENVIRONMENT,
                "secretPath": secret_path,
                "mode": "upsert",
                "secrets": [{"secretKey": key, "secretValue": value} for key, value in pending.items()],
            },
        )


def export_wiki(client: InfisicalClient) -> str:
    return format_env(client.secrets(WIKI_PATH))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read and write Infisical with the service token in INFISICAL_TOKEN.")
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export-wiki", help="print dev /wiki as KEY=value lines")
    upsert = sub.add_parser("upsert", help="upsert KEY=value lines from stdin")
    upsert.add_argument("path", help="secret path, for example /wiki")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        client = InfisicalClient.from_token(token_from_env(), args.api_base)
        if args.command == "export-wiki":
            sys.stdout.write(export_wiki(client))
            return
        pending = read_env_text(sys.stdin.read())
        if not pending:
            raise InfisicalError(0, "upsert received no KEY=value lines.", args.path)
        client.ensure_folder(args.path)
        client.upsert(args.path, pending)
        remote = client.secrets(args.path)
        mismatched = [key for key, value in pending.items() if remote.get(key) != value]
        if mismatched:
            raise InfisicalError(0, f"Infisical read-back mismatch at {args.path} for {', '.join(mismatched)}.", args.path)
    except InfisicalError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
