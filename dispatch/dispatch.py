"""Dispatch an AI CLI agent into a Modal sandbox with an ephemeral volume.

Example:
    uv run dispatch/dispatch.py \\
        --install "npm install -g --ignore-scripts @earendil-works/pi-coding-agent" \\
        --copy client/bookstack.py:/usr/local/bin/bookstack \\
        --setup 'chmod 0755 /usr/local/bin/bookstack' \\
        --bookstack-env bookstack.env \\
        --secret ANTHROPIC_API_KEY \\
        --command 'pi --print {prompt}' \\
        --prompt @task.md
"""

import argparse
import os
import shlex
import sys

import modal

APP_NAME = "cloud-agent-dispatch"
WORKDIR = "/workspace"


def build_image(
    install_command: str | None,
    copies: list[tuple[str, str]] | None = None,
    setup_command: str | None = None,
) -> modal.Image:
    # Node + git + curl out of the box, which covers most agent harnesses.
    image = modal.Image.from_registry("node:22-bookworm", add_python="3.12")
    if install_command:
        image = image.run_commands(install_command)
    for local, remote in copies or []:
        # A setup command is a build step, so any copies it needs must be baked in.
        bake = setup_command is not None
        if os.path.isdir(local):
            image = image.add_local_dir(local, remote_path=remote, copy=bake)
        else:
            image = image.add_local_file(local, remote_path=remote, copy=bake)
    if setup_command:
        image = image.run_commands(setup_command)
    return image


def render_command(command: str, prompt: str) -> str:
    """Substitute {prompt} with the shell-quoted prompt.

    The prompt is also exposed as $PROMPT inside the container, so
    commands like `agent -p "$PROMPT"` work too.
    """
    return command.replace("{prompt}", shlex.quote(prompt))


def dispatch_agent(
    install_command: str | None,
    command: str,
    prompt: str,
    secrets: dict[str, str] | None = None,
    named_secrets: list[str] | None = None,
    copies: list[tuple[str, str]] | None = None,
    setup_command: str | None = None,
    timeout: int = 3600,
) -> int:
    """Run an agent in a fresh sandbox; stream its output and return the exit code."""
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    secret_objs = [modal.Secret.from_dict({**(secrets or {}), "PROMPT": prompt})]
    secret_objs += [modal.Secret.from_name(name) for name in named_secrets or []]
    shell_command = render_command(command, prompt)

    with modal.Volume.ephemeral() as volume:
        sb = modal.Sandbox.create(
            "bash",
            "-lc",
            shell_command,
            app=app,
            image=build_image(install_command, copies, setup_command),
            secrets=secret_objs,
            volumes={WORKDIR: volume},
            workdir=WORKDIR,
            timeout=timeout,
        )
        print(f"[dispatch] sandbox {sb.object_id} running: {shell_command}", file=sys.stderr)
        try:
            for line in sb.stdout:
                print(line, end="", flush=True)
            for line in sb.stderr:
                print(line, end="", file=sys.stderr, flush=True)
            sb.wait()
        finally:
            sb.terminate()
        return sb.returncode


def parse_secret(spec: str) -> tuple[str, str]:
    """KEY=VALUE sets a literal; bare KEY copies it from the local environment."""
    if "=" in spec:
        key, value = spec.split("=", 1)
        return key, value
    if spec not in os.environ:
        raise SystemExit(f"--secret {spec}: not set in local environment")
    return spec, os.environ[spec]


def unquote_env(value: str) -> str:
    if value.startswith("$'") and value.endswith("'"):
        return bytes(value[2:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    # deploy/create-user.sh writes values with printf %q, which escapes spaces.
    return value.replace("\\ ", " ")


def load_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
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


def load_bookstack_env(path: str) -> dict[str, str]:
    """Read BookStack credentials from any of the repo's env files.

    Understands BOOKSTACK_URL / BOOKSTACK_TOKEN directly, plus the files
    written by deploy/create-user.sh (TOKEN_ID + TOKEN_SECRET, or
    AUTHORIZATION) and deploy/state.env (HOST).
    """
    if not os.path.isfile(path):
        raise SystemExit(f"--bookstack-env {path}: file not found")
    values = load_env_file(path)
    url = values.get("BOOKSTACK_URL", "") or values.get("URL", "")
    host = values.get("HOST", "")
    if not url and host:
        url = host if host.startswith(("http://", "https://")) else f"https://{host}"
    token = values.get("BOOKSTACK_TOKEN", "")
    if not token and values.get("TOKEN_ID") and values.get("TOKEN_SECRET"):
        token = f"{values['TOKEN_ID']}:{values['TOKEN_SECRET']}"
    if not token and values.get("AUTHORIZATION"):
        token = values["AUTHORIZATION"].removeprefix("Token ").strip()
    resolved = {key: value for key, value in (("BOOKSTACK_URL", url), ("BOOKSTACK_TOKEN", token)) if value}
    if not resolved:
        raise SystemExit(f"--bookstack-env {path}: no BookStack URL or token found")
    return resolved


def parse_copy(spec: str) -> tuple[str, str]:
    """Parse a --copy argument as SRC or SRC:DEST.

    DEST must be an absolute POSIX path; when omitted it defaults to
    /opt/<basename>. Windows drive prefixes such as C:\\path are not split.
    """
    local, remote = spec, None
    head, _, tail = spec.rpartition(":")
    if tail.startswith("/") and head and not (len(head) == 1 and head.isalpha()):
        local, remote = head, tail
    if remote is None:
        remote = "/opt/" + os.path.basename(local.rstrip("/\\"))
    return local, remote


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", help="Shell command to install the agent harness (cached as an image layer)")
    parser.add_argument("--command", required=True, help="Command to start the agent; {prompt} is replaced with the quoted prompt")
    parser.add_argument("--prompt", required=True, help="Prompt to give the agent (use @path to read from a file)")
    parser.add_argument("--secret", action="append", default=[], metavar="KEY[=VALUE]", help="Env var to set in the container (repeatable)")
    parser.add_argument("--modal-secret", action="append", default=[], metavar="NAME", help="Named Modal secret to attach (repeatable)")
    parser.add_argument("--copy", action="append", default=[], metavar="SRC[:DEST]", help="Copy a local file or directory into the image at DEST, default /opt/<name> (repeatable)")
    parser.add_argument("--setup", help="Shell command to run in the image after --copy (for example, chmod on a copied script)")
    parser.add_argument("--bookstack-env", action="append", default=[], metavar="PATH", help="Read BOOKSTACK_URL and BOOKSTACK_TOKEN from a local env file (repeatable)")
    parser.add_argument("--timeout", type=int, default=3600, help="Sandbox timeout in seconds (default 3600)")
    args = parser.parse_args()

    prompt = args.prompt
    if prompt.startswith("@"):
        with open(prompt[1:], encoding="utf-8") as f:
            prompt = f.read()

    copies = []
    for spec in args.copy:
        local, remote = parse_copy(spec)
        if not os.path.exists(local):
            raise SystemExit(f"--copy {spec}: {local} not found")
        copies.append((local, remote))

    secrets: dict[str, str] = {}
    for path in args.bookstack_env:
        secrets.update(load_bookstack_env(path))
    secrets.update(dict(parse_secret(s) for s in args.secret))

    sys.exit(
        dispatch_agent(
            install_command=args.install,
            command=args.command,
            prompt=prompt,
            secrets=secrets,
            named_secrets=args.modal_secret,
            copies=copies,
            setup_command=args.setup,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    main()
