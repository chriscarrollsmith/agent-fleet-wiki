#!/usr/bin/env python3
"""Rotate one BookStack rollout's credentials.

The stages match the capital-region rotation: SSH key, attachment key, admin
password, every Infisical user under dev /users, then named human accounts.
New secrets are written to Infisical. A failed Infisical write leaves a local
file so the new value is not only in the process. Values are not printed.

Usage:
  ROLLOUT=name bash deploy/rotate-credentials.sh --dry-run
  ROLLOUT=name bash deploy/rotate-credentials.sh ssh-key
  ROLLOUT=name bash deploy/rotate-credentials.sh s3
  ROLLOUT=name bash deploy/rotate-credentials.sh admin
  ROLLOUT=name bash deploy/rotate-credentials.sh infisical
  ROLLOUT=name bash deploy/rotate-credentials.sh humans --human person@example.com
  ROLLOUT=name bash deploy/rotate-credentials.sh all --human person@example.com
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import stat
import string
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from infisical_api import HUMANS_PATH, USERS_PATH, WIKI_PATH, InfisicalClient, InfisicalError, human_folder
from infisical_api import service_token_project as infisical_project

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
REMOTE_PHP = HERE / "rotate-bookstack.php"
BOOKSTACK_DIR = "/var/www/bookstack"
CHUNK = 10

ENV_BASE = os.environ.copy()
ENV_BASE["MSYS_NO_PATHCONV"] = "1"
ENV_BASE["MSYS2_ARG_CONV_EXCL"] = "*"


class RotationError(Exception):
    pass


def die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def unquote_env(value: str) -> str:
    if value.startswith("$'") and value.endswith("'"):
        return bytes(value[2:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value.replace("\\ ", " ")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        values[key.strip()] = unquote_env(value.strip())
    return values


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:@/+-]*", value):
        return value
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return "$'" + escaped + "'"


def replace_env_keys(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if "=" in line and not line.strip().startswith("#"):
            key = line.split("=", 1)[0]
            if key in updates:
                out.append(f"{key}={shell_quote(updates[key])}")
                seen.add(key)
                continue
        out.append(line)
    missing = set(updates) - seen
    if missing:
        raise RotationError(f"{path.name} is missing {', '.join(sorted(missing))}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def generate_secret(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def wiki_url(values: dict[str, str], state: dict[str, str]) -> str:
    url = values.get("BOOKSTACK_URL", "")
    if not url and state.get("HOST"):
        url = "https://" + state["HOST"]
    if not url:
        raise RotationError("Set BOOKSTACK_URL, or HOST in the state file.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def assert_single_instance(instance_ids: list[str], expected: str) -> None:
    if instance_ids != [expected]:
        found = ", ".join(instance_ids) or "none"
        raise RotationError(
            f"Refusing to replace key pair. Expected only {expected}; found {found}."
        )


def service_token_project(payload: dict) -> str:
    try:
        return infisical_project(payload)
    except InfisicalError as exc:
        raise RotationError(str(exc)) from None


def access_key_still_propagating(stderr: str) -> bool:
    return "InvalidAccessKeyId" in stderr


def signoff(resend_from: str) -> str:
    match = re.match(r"\s*([^<]+)<", resend_from)
    name = match.group(1).strip() if match else resend_from.split("@", 1)[0]
    return name.split()[0] if name else "Admin"


def email_text(first: str, url: str, email: str, password: str, sender: str) -> str:
    return (
        f"Hi {first},\n\n"
        "I reset your password on this wiki because Cursor had a data breach. "
        "I don't know that these credentials were exposed. This is a precaution.\n\n"
        "This email was written by an agent. Agents say dumb things. "
        "The password below is the part to rely on.\n\n"
        f"Sign in: {url}/login\n"
        f"Email: {email}\n"
        f"Password: {password}\n\n"
        "After you sign in, you can change it under your account settings.\n\n"
        f"{sender}\n"
    )


def authorized_key_lines(existing: list[str], old: str, new: str, mode: str) -> list[str]:
    lines = [line.strip() for line in existing if line.strip()]
    if mode == "add":
        if new not in lines:
            lines.append(new)
        return lines
    if mode == "replace":
        if new.strip() not in lines:
            raise RotationError("The new key is not authorized, so the old key was left in place.")
        return [new.strip()]
    raise RotationError(f"Unknown authorized_keys mode {mode}.")


def chunks(items: list, size: int) -> list[list]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def json_lines(stdout: str) -> list[dict]:
    rows = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            rows.append(json.loads(line))
    return rows


class Wiki:
    def __init__(self, env_file: Path):
        self.env_file = env_file
        # A rollout keeps every file beside .env. A single-wiki checkout keeps
        # .env at the repo root and the other files under deploy/.
        base = ROOT / "deploy" if env_file.parent == ROOT else env_file.parent
        self.base = base
        self.state_file = base / "state.env"
        self.users_dir = base / "users"
        self.key_file = base / "bookstack.pem"
        file_values = load_env_file(env_file)
        if file_values.get("KEY_FILE"):
            self.key_file = Path(file_values["KEY_FILE"])
        token = file_values.get("INFISICAL_TOKEN", "") or os.environ.get("INFISICAL_TOKEN", "")
        if not token:
            raise RotationError(f"Set INFISICAL_TOKEN in {env_file}.")
        try:
            self.client = InfisicalClient.from_token(token)
            self.values = self.client.secrets(WIKI_PATH)
        except InfisicalError as exc:
            raise RotationError(str(exc)) from None
        self.values["INFISICAL_TOKEN"] = token
        self.state = load_env_file(self.state_file)

    def require(self, *names: str, state: bool = False) -> None:
        source = self.state if state else self.values
        label = str(self.state_file) if state else f"Infisical dev {WIKI_PATH}"
        missing = [name for name in names if not source.get(name)]
        if missing:
            raise RotationError(f"Set {', '.join(missing)} in {label}.")


def run(cmd: list[str], *, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=ROOT, env=env or ENV_BASE, capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "SecretAccessKey" in detail or "ADMIN_PASSWORD" in detail:
            detail = "command failed"
        raise RotationError(detail or f"{cmd[0]} failed ({result.returncode})")
    return result


def aws(wiki: Wiki, args: list[str], *, check: bool = True, access_key: tuple[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = ENV_BASE.copy()
    env["AWS_DEFAULT_REGION"] = wiki.values.get("AWS_DEFAULT_REGION", "us-east-1")
    if access_key:
        env.pop("AWS_PROFILE", None)
        env.pop("AWS_SESSION_TOKEN", None)
        env["AWS_ACCESS_KEY_ID"] = access_key[0]
        env["AWS_SECRET_ACCESS_KEY"] = access_key[1]
    else:
        env["AWS_PROFILE"] = wiki.values.get("AWS_PROFILE", "fleet-coordinator")
    return run(["aws", *args], env=env, check=check)


def ssh(wiki: Wiki, remote: str, *, key: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    identity = key or wiki.key_file
    return run(
        [
            "ssh",
            "-i",
            str(identity),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "BatchMode=yes",
            f"ubuntu@{wiki.state['EIP']}",
            remote,
        ],
        check=check,
    )


def scp_to(wiki: Wiki, local: Path, remote: str) -> None:
    run(
        [
            "scp",
            "-i",
            str(wiki.key_file),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            str(local),
            f"ubuntu@{wiki.state['EIP']}:{remote}",
        ]
    )


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_pending(path: Path, updates: dict[str, str]) -> None:
    lines = [f"{key}={shell_quote(value)}" for key, value in updates.items()]
    write_private(path, "\n".join(lines) + "\n")


def commit_secrets(client: InfisicalClient, path: str, updates: dict[str, str], pending: Path) -> None:
    """Upsert secrets and read them back. On failure, leave them in pending."""
    try:
        client.ensure_folder(path)
        client.upsert(path, updates)
        remote = client.secrets(path)
    except InfisicalError as exc:
        write_pending(pending, updates)
        raise RotationError(f"Infisical did not store the new values at {path}. They were written to {pending}. {exc}") from None
    mismatched = [key for key, value in updates.items() if remote.get(key) != value]
    if mismatched:
        write_pending(pending, updates)
        raise RotationError(
            f"Infisical read-back mismatch at {path} for {', '.join(mismatched)}. Values were written to {pending}."
        )


def public_key(private_key: Path) -> str:
    result = run(["ssh-keygen", "-y", "-f", str(private_key)])
    return result.stdout.strip()


def edit_remote_authorized_keys(wiki: Wiki, old: str, new: str, mode: str, key: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="crr-ssh-") as tmp:
        directory = Path(tmp)
        script = directory / "edit_keys.py"
        script.write_text(
            "import os\nfrom pathlib import Path\n"
            "path = Path.home() / '.ssh' / 'authorized_keys'\n"
            "existing = path.read_text().splitlines() if path.exists() else []\n"
            "old = os.environ['OLD_PUB']\n"
            "new = os.environ['NEW_PUB']\n"
            "mode = os.environ['MODE']\n"
            "lines = [line.strip() for line in existing if line.strip()]\n"
            "if mode == 'add':\n"
            "    if new not in lines:\n"
            "        lines.append(new)\n"
            "elif mode == 'replace':\n"
            "    if new not in lines:\n"
            "        raise SystemExit('new key is not authorized')\n"
            "    lines = [new]\n"
            "else:\n"
            "    raise SystemExit('bad mode')\n"
            "path.write_text('\\n'.join(lines) + '\\n')\n"
            "print('keys', len(lines))\n",
            encoding="utf-8",
        )
        scp_to(wiki, script, "/tmp/edit_keys.py")
        remote = (
            f"OLD_PUB={shlex.quote(old)} NEW_PUB={shlex.quote(new)} MODE={shlex.quote(mode)} "
            "python3 /tmp/edit_keys.py; status=$?; rm -f /tmp/edit_keys.py; exit $status"
        )
        ssh(wiki, remote, key=key)


def instance_ids_for_key(wiki: Wiki) -> list[str]:
    key_name = wiki.values.get("KEY_NAME", "bookstack")
    result = aws(
        wiki,
        [
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=key-name,Values={key_name}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--output",
            "json",
        ],
    )
    payload = json.loads(result.stdout)
    ids = []
    for reservation in payload.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            ids.append(instance["InstanceId"])
    return ids


def rotate_ssh(wiki: Wiki) -> None:
    wiki.require("EIP", "INSTANCE_ID", "HOST", state=True)
    key_name = wiki.values.get("KEY_NAME", "bookstack")
    if not wiki.key_file.is_file():
        raise RotationError(f"SSH key not found: {wiki.key_file}")
    ids = instance_ids_for_key(wiki)
    assert_single_instance(ids, wiki.state["INSTANCE_ID"])
    incoming = wiki.key_file.with_name(wiki.key_file.name + ".incoming")
    incoming_pub = Path(str(incoming) + ".pub")
    if incoming.exists():
        incoming.unlink()
    incoming_pub.unlink(missing_ok=True)
    run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-f",
            str(incoming),
            "-N",
            "",
            "-C",
            wiki.state["HOST"],
            "-q",
        ]
    )
    # Once the old public key is removed, incoming is the only key that can log in.
    remote_switched = False
    try:
        new_pub = incoming_pub.read_text(encoding="utf-8").strip() or public_key(incoming)
        old_pub = public_key(wiki.key_file)
        edit_remote_authorized_keys(wiki, old_pub, new_pub, "add", wiki.key_file)
        probe = ssh(wiki, "echo new-key-ok", key=incoming)
        if "new-key-ok" not in probe.stdout:
            raise RotationError("The new SSH key did not log in. The old key is still authorized.")
        edit_remote_authorized_keys(wiki, old_pub, new_pub, "replace", incoming)
        rejected = ssh(wiki, "echo old-key-ok", key=wiki.key_file, check=False)
        if "Permission denied" not in (rejected.stderr + rejected.stdout):
            raise RotationError("The old SSH key was not rejected. Both keys may still be authorized.")
        confirmed = ssh(wiki, "echo new-key-ok", key=incoming)
        if "new-key-ok" not in confirmed.stdout:
            raise RotationError("The new SSH key stopped working after the old key was removed.")
        remote_switched = True
        os.replace(incoming, wiki.key_file)
        os.chmod(wiki.key_file, stat.S_IRUSR | stat.S_IWUSR)
        pub = Path(str(wiki.key_file) + ".pub")
        pub.write_text(public_key(wiki.key_file) + "\n", encoding="utf-8")
        incoming_pub.unlink(missing_ok=True)
        replace_key_pair(wiki, key_name, pub)
    except Exception:
        if not remote_switched:
            incoming.unlink(missing_ok=True)
            incoming_pub.unlink(missing_ok=True)
        raise
    print(f"ssh key replaced for ubuntu@{wiki.state['EIP']}")


def replace_key_pair(wiki: Wiki, key_name: str, public: Path) -> None:
    temporary = f"{key_name}-incoming"
    material = f"fileb://{public.as_posix()}"
    aws(wiki, ["ec2", "import-key-pair", "--key-name", temporary, "--public-key-material", material])
    aws(wiki, ["ec2", "delete-key-pair", "--key-name", key_name])
    aws(wiki, ["ec2", "import-key-pair", "--key-name", key_name, "--public-key-material", material])
    aws(wiki, ["ec2", "delete-key-pair", "--key-name", temporary])
    print(f"aws key pair {key_name} replaced")


def list_bucket_status(wiki: Wiki, key_id: str, secret: str) -> subprocess.CompletedProcess[str]:
    bucket = wiki.values["BUCKET"]
    last = None
    for attempt in range(8):
        last = aws(
            wiki,
            [
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--max-keys",
                "1",
                "--query",
                "KeyCount",
                "--output",
                "text",
            ],
            check=False,
            access_key=(key_id, secret),
        )
        if last.returncode == 0:
            return last
        if not access_key_still_propagating(last.stderr) or attempt == 7:
            return last
        time.sleep(3)
    return last


def push_s3_env(wiki: Wiki, key_id: str, secret: str) -> None:
    with tempfile.TemporaryDirectory(prefix="crr-s3-") as tmp:
        directory = Path(tmp)
        payload = directory / "s3.env"
        write_private(payload, f"STORAGE_S3_KEY={key_id}\nSTORAGE_S3_SECRET={secret}\n")
        scp_to(wiki, payload, "/tmp/crr-s3.env")
        scp_to(wiki, REMOTE_PHP, "/tmp/rotate-bookstack.php")
        result = ssh(
            wiki,
            "php /tmp/rotate-bookstack.php s3-env /tmp/crr-s3.env; status=$?; rm -f /tmp/crr-s3.env; exit $status",
        )
    if f"server_key_id {key_id}" not in result.stdout:
        raise RotationError("The instance env was not updated to the new attachment key.")


def rotate_s3(wiki: Wiki) -> None:
    wiki.require("BUCKET", "IAM_USER", "STORAGE_S3_KEY", "STORAGE_S3_SECRET")
    wiki.require("EIP", state=True)
    previous = {"STORAGE_S3_KEY": wiki.values["STORAGE_S3_KEY"], "STORAGE_S3_SECRET": wiki.values["STORAGE_S3_SECRET"]}
    user = wiki.values.get("IAM_USER", "bookstack-attachments")
    created = aws(wiki, ["iam", "create-access-key", "--user-name", user, "--output", "json"])
    data = json.loads(created.stdout)["AccessKey"]
    key_id = data["AccessKeyId"]
    secret = data["SecretAccessKey"]
    if "\n" in secret or not key_id.startswith("AKIA"):
        raise RotationError("The new access key looked wrong. It was created and not installed.")
    print(f"created access key ending {key_id[-4:]}")
    listed = list_bucket_status(wiki, key_id, secret)
    if listed.returncode != 0:
        aws(wiki, ["iam", "delete-access-key", "--user-name", user, "--access-key-id", key_id])
        raise RotationError("The new access key cannot list the bucket. It was deleted. The previous key is still in use.")
    print("new access key can list the bucket")
    try:
        push_s3_env(wiki, key_id, secret)
        scp_to(wiki, REMOTE_PHP, "/tmp/rotate-bookstack.php")
        checked = ssh(wiki, f"sudo -u www-data php /tmp/rotate-bookstack.php s3-check {shlex.quote(key_id)}; rm -f /tmp/rotate-bookstack.php")
        if "s3_app_ok" not in checked.stdout:
            raise RotationError("BookStack did not accept the new attachment key.")
    except Exception as exc:
        print("restoring the previous attachment key on the instance")
        push_s3_env(wiki, previous["STORAGE_S3_KEY"], previous["STORAGE_S3_SECRET"])
        aws(wiki, ["iam", "delete-access-key", "--user-name", user, "--access-key-id", key_id], check=False)
        raise RotationError(str(exc)) from exc
    unit = ssh(wiki, "systemctl list-units --type=service --no-legend --plain 'php*-fpm.service' | awk 'NR==1 {print $1}'")
    service = unit.stdout.strip()
    if not service:
        raise RotationError("php-fpm is not running. The instance is on the new key and Infisical was not updated.")
    restarted = ssh(wiki, f"sudo systemctl restart {shlex.quote(service)} && systemctl is-active {shlex.quote(service)}")
    if "active" not in restarted.stdout:
        raise RotationError("php-fpm did not return. The instance is on the new key and Infisical was not updated.")
    commit_secrets(
        wiki.client,
        WIKI_PATH,
        {"STORAGE_S3_KEY": key_id, "STORAGE_S3_SECRET": secret},
        wiki.base / "pending-s3.env",
    )
    aws(wiki, ["iam", "delete-access-key", "--user-name", user, "--access-key-id", previous["STORAGE_S3_KEY"]])
    remaining = aws(wiki, ["iam", "list-access-keys", "--user-name", user, "--output", "json"])
    ids = [item["AccessKeyId"] for item in json.loads(remaining.stdout)["AccessKeyMetadata"]]
    if ids != [key_id]:
        raise RotationError("The attachment user does not have exactly the new access key. Infisical dev /wiki has the new secret.")
    print(f"attachment key replaced; live key ends {key_id[-4:]}")


def run_user_php(wiki: Wiki, users: list[dict]) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="crr-users-") as tmp:
        directory = Path(tmp)
        payload = directory / "users.json"
        write_private(payload, json.dumps({"users": users}))
        scp_to(wiki, payload, "/tmp/crr-users.json")
        scp_to(wiki, REMOTE_PHP, "/tmp/rotate-bookstack.php")
        remote = (
            "sudo chown www-data:www-data /tmp/crr-users.json && sudo chmod 600 /tmp/crr-users.json && "
            "sudo -u www-data php /tmp/rotate-bookstack.php users /tmp/crr-users.json; "
            "status=$?; sudo rm -f /tmp/crr-users.json /tmp/rotate-bookstack.php; exit $status"
        )
        result = ssh(wiki, remote, check=False)
    rows = json_lines(result.stdout)
    if result.returncode != 0 and not rows:
        raise RotationError((result.stderr or "BookStack user rotation failed").strip())
    return rows


def rotate_admin(wiki: Wiki) -> None:
    wiki.require("ADMIN_EMAIL")
    wiki.require("EIP", state=True)
    password = generate_secret(24)
    email = wiki.values["ADMIN_EMAIL"]
    rows = run_user_php(wiki, [{"email": email, "password": password, "rotate_token": False}])
    if len(rows) != 1 or not rows[0].get("ok"):
        raise RotationError(f"Admin password was not changed: {rows[0].get('error', 'no result') if rows else 'no result'}")
    commit_secrets(wiki.client, WIKI_PATH, {"ADMIN_PASSWORD": password}, wiki.base / "pending-admin.env")
    confirm_admin_login(wiki, email, password)
    print(f"admin password replaced for {email}")


def confirm_admin_login(wiki: Wiki, email: str, password: str) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("create_user_http", HERE / "create-user-http.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    web = module.Web(wiki_url(wiki.values, wiki.state))
    module.login(web, email, password)


def local_files_by_email(users_dir: Path) -> dict[str, Path]:
    found = {}
    if not users_dir.is_dir():
        return found
    for path in users_dir.glob("*.env"):
        email = load_env_file(path).get("EMAIL", "")
        if email:
            found[email.lower()] = path
    return found


def update_local_user(path: Path, password: str, token_id: str = "", token_secret: str = "") -> None:
    updates = {"PASSWORD": password}
    if token_id and token_secret:
        updates["TOKEN_ID"] = token_id
        updates["TOKEN_SECRET"] = token_secret
        updates["AUTHORIZATION"] = f"Token {token_id}:{token_secret}"
    if path.is_file():
        replace_env_keys(path, updates)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={shell_quote(value)}" for key, value in updates.items()]
    write_private(path, "\n".join(lines) + "\n")


def prepare_user(record: dict) -> dict:
    item = {
        "email": record["email"],
        "user_id": int(record.get("user_id") or 0),
        "password": generate_secret(24),
        "rotate_token": bool(record.get("rotate_token")),
        "folder": record.get("folder", ""),
    }
    if item["rotate_token"]:
        item["token_id"] = generate_secret(32)
        item["token_secret"] = generate_secret(32)
    return item


def sync_rotated_user(client: InfisicalClient | None, item: dict, users_dir: Path, files: dict[str, Path]) -> None:
    email = item["email"]
    token_id = item.get("token_id", "")
    token_secret = item.get("token_secret", "")
    path = files.get(email.lower())
    if not (item.get("folder") and client is not None):
        return
    pending = {"PASSWORD": item["password"]}
    if token_id and token_secret:
        pending["TOKEN_ID"] = token_id
        pending["TOKEN_SECRET"] = token_secret
        pending["AUTHORIZATION"] = f"Token {token_id}:{token_secret}"
    secret_path = f"{USERS_PATH}/{item['folder']}"
    fallback = path or users_dir / f"{email}.env"
    commit_secrets(client, secret_path, pending, fallback)
    if path is not None and path.is_file():
        path.unlink()


def rotate_user_records(wiki: Wiki, client: InfisicalClient | None, records: list[dict]) -> tuple[list[str], list[dict]]:
    """Rotate records. Returns (failure messages, prepared items that were emailed-ready)."""
    if not records:
        return [], []
    prepared = [prepare_user(record) for record in records]
    by_email = {item["email"].lower(): item for item in prepared}
    rows = run_user_php(wiki, prepared)
    files = local_files_by_email(wiki.users_dir)
    done: list[dict] = []
    failures: list[str] = []
    seen = set()
    for row in rows:
        email = row.get("email", "")
        item = by_email.get(email.lower())
        if not row.get("ok") or item is None:
            failures.append(f"{email or 'unknown'}: {row.get('error', 'no matching request')}")
            continue
        seen.add(email.lower())
        try:
            if item.get("rotate_token"):
                if not token_works(wiki, item["token_id"], item["token_secret"]):
                    raise RotationError("new token was rejected")
            sync_rotated_user(client, item, wiki.users_dir, files)
        except RotationError as exc:
            update_local_user(files.get(email.lower()) or wiki.users_dir / f"{email}.env", item["password"], item.get("token_id", ""), item.get("token_secret", ""))
            failures.append(f"{email}: {exc}. Credentials were left in the local user file.")
            continue
        print(f"rotated {email} tokens_removed={row.get('removed_tokens', 0)} sessions={row.get('sessions', 0)}")
        done.append({**item, "name": row.get("name") or email})
    for item in prepared:
        if item["email"].lower() not in seen:
            failures.append(f"{item['email']}: BookStack did not report a result")
    return failures, done


def token_works(wiki: Wiki, token_id: str, token_secret: str) -> bool:
    request = urllib.request.Request(
        wiki_url(wiki.values, wiki.state) + "/api/system",
        headers={"Authorization": f"Token {token_id}:{token_secret}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False


def infisical_records(client: InfisicalClient, pilot: str) -> list[dict]:
    records = []
    try:
        folders = client.folder_names(USERS_PATH)
    except InfisicalError as exc:
        raise RotationError(str(exc)) from None
    for folder in folders:
        try:
            values = client.secrets(f"{USERS_PATH}/{folder}")
        except InfisicalError as exc:
            raise RotationError(str(exc)) from None
        email = values.get("EMAIL", "")
        user_id = values.get("USER_ID", "")
        if not email or not str(user_id).isdigit():
            raise RotationError(f"{folder} is missing EMAIL or a numeric USER_ID.")
        records.append(
            {
                "folder": folder,
                "email": email,
                "user_id": user_id,
                "rotate_token": bool(values.get("TOKEN_ID") and values.get("TOKEN_SECRET")),
            }
        )
    records.sort(key=lambda item: item["email"].lower())
    if pilot:
        chosen = [item for item in records if item["folder"] == pilot]
        if len(chosen) != 1:
            raise RotationError(f"Infisical folder {pilot} was not found under /users.")
        rest = [item for item in records if item["folder"] != pilot]
        return chosen + rest
    return records


def rotate_infisical(wiki: Wiki, pilot: str) -> None:
    client = wiki.client
    records = infisical_records(client, pilot)
    if not records:
        raise RotationError("Infisical /users has no folders.")
    print(f"infisical users {len(records)}; pilot {records[0]['folder']}")
    failures, _ = rotate_user_records(wiki, client, records[:1])
    if failures:
        raise RotationError("Pilot failed. Stopped before the remaining users.\n" + "\n".join(failures))
    print("pilot ok")
    for group in chunks(records[1:], CHUNK):
        group_failures, _ = rotate_user_records(wiki, client, group)
        failures.extend(group_failures)
    if failures:
        raise RotationError("Some Infisical users were not fully rotated:\n" + "\n".join(failures))
    print(f"rotated {len(records)} infisical users")


def send_email(api_key: str, sender: str, to_email: str, text: str, subject: str) -> str:
    with tempfile.TemporaryDirectory(prefix="crr-mail-") as tmp:
        directory = Path(tmp)
        header = directory / "header.txt"
        body = directory / "body.json"
        output = directory / "out.json"
        write_private(header, f"Authorization: Bearer {api_key}\nContent-Type: application/json\nAccept: application/json\n")
        body.write_text(
            json.dumps(
                {
                    "from": sender,
                    "to": [to_email],
                    "reply_to": re.search(r"<([^>]+)>", sender).group(1) if "<" in sender else sender,
                    "subject": subject,
                    "text": text,
                }
            ),
            encoding="utf-8",
        )
        # curl, not urllib. Cloudflare in front of Resend answers urllib with error 1010.
        result = run(
            ["curl", "-sS", "-o", str(output), "-w", "%{http_code}", "https://api.resend.com/emails", "-H", f"@{header}", "-d", f"@{body}"],
            check=False,
        )
        raw = output.read_text(encoding="utf-8") if output.is_file() else ""
    if result.returncode != 0:
        raise RotationError(f"curl could not reach Resend for {to_email}.")
    if result.stdout.strip() not in {"200", "201"}:
        try:
            detail = json.loads(raw).get("message") or json.loads(raw).get("name")
        except json.JSONDecodeError:
            detail = "non-json error"
        raise RotationError(f"Resend rejected the message to {to_email} ({result.stdout.strip()}): {detail}")
    email_id = json.loads(raw).get("id", "")
    if not email_id:
        raise RotationError(f"Resend accepted a message to {to_email} without an id.")
    return str(email_id)


def rotate_humans(wiki: Wiki, emails: list[str]) -> None:
    if not emails:
        raise RotationError("Pass --human for each person who should receive a password email.")
    wiki.require("RESEND_API_KEY", "RESEND_FROM")
    wiki.require("EIP", state=True)
    url = wiki_url(wiki.values, wiki.state)
    sender = wiki.values["RESEND_FROM"]
    records = [{"email": email, "rotate_token": False, "folder": ""} for email in emails]
    failures, done = rotate_user_records(wiki, None, records)
    if failures or len(done) != len(emails):
        raise RotationError("A human password was not changed, so no email was sent.\n" + "\n".join(failures))
    host = wiki.state.get("HOST") or urllib.parse.urlparse(url).netloc
    files = local_files_by_email(wiki.users_dir)
    for person in done:
        # Store the password before sending. A mail failure then still leaves a copy.
        folder = human_folder(person["email"])
        pending = files.get(person["email"].lower()) or wiki.users_dir / f"{person['email']}.env"
        fields = {"EMAIL": person["email"], "PASSWORD": person["password"]}
        if person.get("name"):
            fields["NAME"] = person["name"]
        commit_secrets(wiki.client, f"{HUMANS_PATH}/{folder}", fields, pending)
        if pending.is_file():
            pending.unlink()
        first = (person.get("name") or person["email"]).split()[0]
        text = email_text(first, url, person["email"], person["password"], signoff(sender))
        email_id = send_email(
            wiki.values["RESEND_API_KEY"],
            sender,
            person["email"],
            text,
            f"Your password for {host} was reset",
        )
        print(f"emailed {person['email']} resend={email_id}")


def dry_run(wiki: Wiki, humans: list[str], pilot: str) -> None:
    url = wiki_url(wiki.values, wiki.state) if wiki.values.get("BOOKSTACK_URL") or wiki.state.get("HOST") else "(no url yet)"
    print(f"env {wiki.env_file}")
    print(f"url {url}")
    print(f"key {wiki.values.get('KEY_NAME', 'bookstack')} iam {wiki.values.get('IAM_USER', 'bookstack-attachments')} bucket {wiki.values.get('BUCKET', '')}")
    print(f"admin {wiki.values.get('ADMIN_EMAIL', '')}")
    if wiki.state.get("INSTANCE_ID") and wiki.values.get("AWS_PROFILE"):
        ids = instance_ids_for_key(wiki)
        print("instances " + (", ".join(ids) or "none"))
    records = infisical_records(wiki.client, pilot)
    print(f"infisical users {len(records)} project {wiki.client.project_id}")
    for record in records:
        token = "token" if record["rotate_token"] else "password-only"
        print(f"  {record['folder']} {record['email']} {token}")
    if humans:
        print("humans " + ", ".join(humans))
    else:
        print("humans none")
    print("dry-run; nothing was changed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rotate credentials for one BookStack rollout.")
    parser.add_argument("stage", choices=("ssh-key", "s3", "admin", "infisical", "humans", "all"))
    parser.add_argument("--env-file", default="", help="rollout .env (default: rollouts/$ROLLOUT/.env, or ./.env)")
    parser.add_argument("--human", action="append", default=[], help="email of a person who is not an Infisical agent; repeat for each")
    parser.add_argument("--pilot", default="", help="Infisical folder to rotate first (default: first email in alphabetical order)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and read Infisical and AWS; change nothing")
    return parser


def resolve_env_file(arg: str) -> Path:
    if arg:
        return Path(arg)
    rollout = os.environ.get("ROLLOUT", "")
    if rollout:
        return ROOT / "rollouts" / rollout / ".env"
    return ROOT / ".env"


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    env_file = resolve_env_file(args.env_file)
    if not env_file.is_file():
        die(f"Env file not found: {env_file}")
    try:
        wiki = Wiki(env_file)
        if args.dry_run:
            dry_run(wiki, args.human, args.pilot)
            return
        stages = ["ssh-key", "s3", "admin", "infisical", "humans"] if args.stage == "all" else [args.stage]
        if "humans" in stages and not args.human and args.stage == "humans":
            die("Pass --human for each person who should receive a password email.")
        for stage in stages:
            if stage == "ssh-key":
                rotate_ssh(wiki)
            elif stage == "s3":
                rotate_s3(wiki)
            elif stage == "admin":
                rotate_admin(wiki)
            elif stage == "infisical":
                rotate_infisical(wiki, args.pilot)
            elif stage == "humans":
                if args.human:
                    rotate_humans(wiki, args.human)
                else:
                    print("no --human addresses; skipped human emails")
    except RotationError as exc:
        die(str(exc))


if __name__ == "__main__":
    main()
