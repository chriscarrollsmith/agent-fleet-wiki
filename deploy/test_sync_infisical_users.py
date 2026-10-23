"""Tests for deploy/sync_infisical_users.py. No live Infisical calls."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_infisical_users as sync


SAMPLE = {key: f"value-{key}" for key in sync.SECRET_KEYS}
SAMPLE["AUTHORIZATION"] = "Token abc:def"


def credential_text(values: dict[str, str], identity: str | None = None) -> str:
    lines = [f"{key}={values[key]}" for key in sync.SECRET_KEYS]
    if identity:
        lines.append(f"IDENTITY={identity}")
    return "\n".join(lines) + "\n"


class EnvGuard:
    def __init__(self, **values: str):
        self.values = values
        self.previous: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.values.items():
            self.previous[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *_) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class InfisicalHandler(BaseHTTPRequestHandler):
    folders: set[str] = set()
    secrets: dict[str, dict[str, str]] = {}
    calls: list[tuple[str, str, dict]] = []
    token = "test-token"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def _handle(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        body = json.loads(raw) if raw else {}
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query).items()}
        self.calls.append((self.command, self.path, body))
        if parsed.path == "/api/v1/auth/universal-auth/login":
            if self.headers.get("Authorization"):
                self._json(400, {"message": "login must be anonymous"})
                return
            self._json(200, {"accessToken": self.token})
            return
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self._json(401, {"message": "unauthorized"})
            return
        if parsed.path == "/api/v2/folders" and self.command == "GET":
            self._json(200, {"folders": [{"name": name} for name in sorted(self.folders)]})
            return
        if parsed.path == "/api/v2/folders" and self.command == "POST":
            self.folders.add(body["name"])
            self._json(200, {"folder": {"name": body["name"]}})
            return
        if parsed.path == "/api/v4/secrets" and self.command == "GET":
            secret_path = query.get("secretPath", "")
            identity = secret_path.removeprefix("/users/")
            stored = self.secrets.get(identity, {})
            self._json(
                200,
                {
                    "secrets": [
                        {"secretKey": key, "secretValue": value, "secretValueHidden": False}
                        for key, value in stored.items()
                    ]
                },
            )
            return
        if parsed.path == "/api/v4/secrets/batch" and self.command == "PATCH":
            identity = body["secretPath"].removeprefix("/users/")
            current = self.secrets.setdefault(identity, {})
            for item in body["secrets"]:
                current[item["secretKey"]] = item["secretValue"]
            self._json(200, {"secrets": []})
            return
        self._json(404, {"message": "not found"})

    def _json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        InfisicalHandler.folders = set()
        InfisicalHandler.secrets = {}
        InfisicalHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), InfisicalHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.api = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_first_push_creates_folder_and_upserts_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE), encoding="utf-8")
            output = self.run_main(path, identity="pi-agent")
            recorded = path.read_text(encoding="utf-8")
        self.assertIn("pi-agent: created /users/pi-agent", output)
        self.assertIn("wrote USER_ID", output)
        self.assertEqual(InfisicalHandler.secrets["pi-agent"], SAMPLE)
        methods = [call[0] for call in InfisicalHandler.calls]
        self.assertEqual(methods, ["POST", "GET", "POST", "PATCH"])
        login = InfisicalHandler.calls[0][2]
        self.assertEqual(login["clientId"], "client")
        folder_list = InfisicalHandler.calls[1][1]
        self.assertIn("projectId=project-id", folder_list)
        self.assertIn("environment=dev", folder_list)
        self.assertIn("path=%2Fusers", folder_list)
        upsert = InfisicalHandler.calls[-1][2]
        self.assertEqual(upsert["projectId"], "project-id")
        self.assertEqual(upsert["mode"], "upsert")
        self.assertEqual(upsert["secretPath"], "/users/pi-agent")
        self.assertNotIn("IDENTITY", {item["secretKey"] for item in upsert["secrets"]})
        self.assertIn("IDENTITY=pi-agent", recorded)

    def test_second_push_leaves_matching_secrets(self) -> None:
        InfisicalHandler.folders.add("pi-agent")
        InfisicalHandler.secrets["pi-agent"] = dict(SAMPLE)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE, "pi-agent"), encoding="utf-8")
            output = self.run_main(path)
        self.assertIn("pi-agent: already in sync", output)
        self.assertNotIn("PATCH", [call[0] for call in InfisicalHandler.calls])

    def test_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE), encoding="utf-8")
            output = self.run_main(path, identity="pi-agent", dry_run=True)
            recorded = path.read_text(encoding="utf-8")
        self.assertIn("would create /users/pi-agent", output)
        self.assertIn("would write", output)
        self.assertEqual(InfisicalHandler.folders, set())
        self.assertEqual(InfisicalHandler.secrets, {})
        self.assertNotIn("IDENTITY=", recorded)

    def test_changed_secret_is_the_only_upsert(self) -> None:
        remote = dict(SAMPLE)
        remote["ROLE"] = "Editor"
        InfisicalHandler.folders.add("pi-agent")
        InfisicalHandler.secrets["pi-agent"] = remote
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE, "pi-agent"), encoding="utf-8")
            output = self.run_main(path)
        self.assertIn("wrote ROLE", output)
        self.assertNotIn("USER_ID", output)
        upsert = InfisicalHandler.calls[-1][2]
        self.assertEqual([item["secretKey"] for item in upsert["secrets"]], ["ROLE"])
        self.assertEqual(InfisicalHandler.secrets["pi-agent"]["ROLE"], SAMPLE["ROLE"])

    def test_identity_conflict_refuses_before_login(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE, "other-agent"), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_main(path, identity="pi-agent")
        self.assertEqual(InfisicalHandler.calls, [])

    def test_missing_field_refuses(self) -> None:
        values = dict(SAMPLE)
        del values["PASSWORD"]
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                sync.credentials_from(values)

    def test_hidden_remote_value_is_rewritten(self) -> None:
        remote = {key: SAMPLE[key] for key in sync.SECRET_KEYS if key != "PASSWORD"}
        remote["PASSWORD"] = None
        pending = sync.keys_to_write(SAMPLE, remote)
        self.assertEqual(list(pending), ["PASSWORD"])

    def test_slug_rejects_path_characters(self) -> None:
        for slug in ("", ".hidden", "a/b", "../x", "has space"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    sync.validate_slug(slug)
        sync.validate_slug("deepseek-flash-4.1-pi-crime-agent")
        self.assertEqual(sync.folder_name("deepseek-flash-4.1-pi-crime-agent"), "deepseek-flash-4-1-pi-crime-agent")

    def test_dotted_slug_uses_the_hyphenated_folder(self) -> None:
        identity = "deepseek-flash-4.1-pi-crime-agent"
        folder = "deepseek-flash-4-1-pi-crime-agent"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent@example.com.env"
            path.write_text(credential_text(SAMPLE), encoding="utf-8")
            first = self.run_main(path, identity=identity)
            second = self.run_main(path)
            recorded = path.read_text(encoding="utf-8")
        self.assertIn(f"{identity}: created /users/{folder}", first)
        self.assertIn(f"{identity}: already in sync", second)
        self.assertEqual(InfisicalHandler.secrets[folder], SAMPLE)
        self.assertNotIn(identity, InfisicalHandler.folders)
        self.assertIn(f"IDENTITY={identity}", recorded)

    def test_missing_project_id_refuses_before_login(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "pi@example.com.env"
            path.write_text(credential_text(SAMPLE), encoding="utf-8")
            argv = [
                "--file",
                str(path),
                "--identity",
                "pi-agent",
                "--api-base",
                self.api,
                "--env-file",
                str(path.with_name("missing.env")),
            ]
            with EnvGuard(INFISICAL_CLIENT_ID="client", INFISICAL_CLIENT_SECRET="secret", INFISICAL_PROJECT_ID=""):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        sync.main(argv)
        self.assertEqual(InfisicalHandler.calls, [])

    def test_project_id_falls_back_to_env_file(self) -> None:
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "INFISICAL_CLIENT_ID=from-file\nINFISICAL_CLIENT_SECRET=from-file-secret\nINFISICAL_PROJECT_ID=from-file-project\n",
                encoding="utf-8",
            )
            with EnvGuard(INFISICAL_CLIENT_ID="", INFISICAL_CLIENT_SECRET="", INFISICAL_PROJECT_ID=""):
                client_id, client_secret, project_id = sync.resolve_infisical(env_file)
        self.assertEqual((client_id, client_secret, project_id), ("from-file", "from-file-secret", "from-file-project"))

    def run_main(self, path: Path, identity: str | None = None, dry_run: bool = False) -> str:
        argv = ["--file", str(path), "--api-base", self.api, "--env-file", str(path.with_name("missing.env"))]
        if identity:
            argv.extend(["--identity", identity])
        if dry_run:
            argv.append("--dry-run")
        buffer = io.StringIO()
        with EnvGuard(
            INFISICAL_CLIENT_ID="client",
            INFISICAL_CLIENT_SECRET="secret",
            INFISICAL_PROJECT_ID="project-id",
        ):
            with contextlib.redirect_stdout(buffer):
                sync.main(argv)
        return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
