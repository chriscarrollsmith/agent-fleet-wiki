# modal-cloud-agent-dispatch

Spin up an AI CLI agent in a fresh Modal sandbox with an ephemeral volume mounted at `/workspace`.

```sh
uv run dispatch/dispatch.py \
  --install "npm install -g @anthropic-ai/claude-code" \
  --secret ANTHROPIC_API_KEY \
  --command 'claude -p {prompt} --dangerously-skip-permissions' \
  --prompt "Write a haiku about containers to haiku.txt, then cat it"
```

| Flag | Meaning |
| --- | --- |
| `--install` | Shell command that installs the harness. It runs as an image layer, so Modal caches it across runs. |
| `--copy SRC[:DEST]` | Copy a local file or directory into the image. `DEST` defaults to `/opt/<name>`. Repeatable. |
| `--setup` | Shell command to run in the image after `--copy`, for example to `chmod` a copied script. |
| `--command` | Command that starts the agent. `{prompt}` is replaced with the shell-quoted prompt, and `$PROMPT` is also set. |
| `--prompt` | The prompt. Use `@file.md` to read it from a file. |
| `--secret KEY[=VALUE]` | Env var for the container. A bare `KEY` copies the value from your local environment. Repeatable. |
| `--bookstack-env PATH` | Read `BOOKSTACK_URL` and `BOOKSTACK_TOKEN` from a local env file. Repeatable; later files and `--secret` win. |
| `--modal-secret NAME` | Attach a named Modal secret. Repeatable. |
| `--timeout` | Sandbox timeout in seconds (default 3600). |

The base image is `node:22-bookworm` with Python 3.12. The agent's stdout and stderr are streamed back to you, and its exit code becomes the script's exit code. The volume is deleted when the run ends, and the sandbox is terminated when the agent command exits.

You can also call it from Python: `from dispatch import dispatch_agent`, passing `copies=[(local, remote)]` and `secrets={...}`.

## Dispatch a Pi agent that can edit the wiki

The wiki project ships the `bookstack` client (`client/bookstack.py`), an API client for an agent user. Install the Pi harness, put that client on `PATH`, and hand the agent the BookStack token and a provider key:

```sh
uv run dispatch/dispatch.py \
  --install "npm install -g --ignore-scripts @earendil-works/pi-coding-agent" \
  --copy client/bookstack.py:/usr/local/bin/bookstack \
  --setup 'chmod 0755 /usr/local/bin/bookstack' \
  --bookstack-env bookstack.env \
  --secret ANTHROPIC_API_KEY \
  --command 'pi --print {prompt}' \
  --prompt "Find the page about capital regions, summarize it, and add a 'Reviewed' tag."
```

What each piece does:

- **Agent utility** — `--copy` installs the client at `/usr/local/bin/bookstack`. The file's `#!/usr/bin/env python3` shebang runs it directly, so no wrapper is needed; the image already has `python3` on `PATH`. `--setup` marks it executable: Modal masks file modes on upload, and a Windows client uploads files as `0644` with no execute bit, so `chmod` is required. Adding `--setup` bakes the file into the image; without it the file is mounted at container startup. (The repo's `client/bookstack.sh` wrapper is redundant here.)
- **API token** — `--bookstack-env bookstack.env` reads `BOOKSTACK_URL` and `BOOKSTACK_TOKEN` and injects them as container secrets. Create `bookstack.env` from `bookstack.env.example`. The files written by `deploy/create-user.sh` also work: pass `--bookstack-env deploy/state.env --bookstack-env deploy/users/<email>.env`, and only the URL and the token are used (the password and other keys are ignored). `--secret` overrides values from these files.
- **Harness install** — `--install` installs Pi with npm. Pi needs Node 22.19 or newer, which the `node:22-bookworm` base image satisfies.
- **Provider key and start command** — `--secret ANTHROPIC_API_KEY` copies your provider key into the sandbox; use `OPENAI_API_KEY` or another provider variable to match your model. `pi --print` runs the prompt non-interactively and exits. Pi does not prompt for tool approval, so the agent can read and edit files and run `bookstack` on its own. Add `--model <pattern>` (for example `pi --print --model sonnet`) if you want a specific model.

Inside the sandbox the agent can call the wiki directly, because its environment already holds `BOOKSTACK_URL` and `BOOKSTACK_TOKEN`:

```sh
bookstack system
bookstack books list --format text
bookstack search "capital region" --book 1
bookstack pages get 3 --field markdown
bookstack pages create --book 1 --name "Finding" --markdown-file finding.md
bookstack attachments upload --page 3 --file notes.txt
```

Run `bookstack --help` (or `bookstack pages --help`) in the sandbox for the full command set.

### Tell the agent the client exists

The sandbox has `bookstack` on `PATH`, but the model still has to know to use it. Say so in the prompt, or ship a Pi skill that the agent loads when a wiki task comes up:

```sh
# skill/SKILL.md:
#   ---
#   name: bookstack
#   description: Read and edit the team wiki with the `bookstack` CLI. Use for wiki pages, books, attachments, and search.
#   ---
#   Run `bookstack --help`. `BOOKSTACK_URL` and `BOOKSTACK_TOKEN` are already set.
uv run dispatch/dispatch.py \
  ... \
  --copy skill:/root/.pi/agent/skills/bookstack \
  ...
```

A long-form prompt works well as a checked-in file, read with `--prompt @dispatch/task.md`.
