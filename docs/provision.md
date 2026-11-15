# Provision a BookStack and its orchestrator

Two directories.

This repo is the infrastructure checkout. It creates the EC2 instance, the private attachment bucket, and the admin account, and it keeps the SSH key. One working copy is bound to one wiki by `deploy/state.env`. Running `bash deploy/run.sh` again updates that wiki. To keep several wikis in one checkout instead, give each a `rollouts/<name>/` directory and set `ROLLOUT=<name>`. The README describes that layout.

The orchestration folder is a sibling directory. It creates BookStack users and dispatches agents. Its BookStack credential is the admin login in Infisical `dev /wiki`. The SSH key stays in the infrastructure checkout.

## 1. Deploy the wiki

Clone this repo into a new directory and deploy from that clone. The checkout that already has a `deploy/state.env` is bound to the wiki it created, including `deploy/bookstack.pem`.

```bash
cp .env.example .env
```

Set `INFISICAL_TOKEN` to a service token for this wiki's Infisical project. Create `dev /wiki` and put `AWS_PROFILE`, `AWS_DEFAULT_REGION`, `BUCKET`, and `ADMIN_EMAIL` there. `BUCKET` is a globally unique S3 name.

In the same account, also set `KEY_NAME` and `IAM_USER` in `dev /wiki` to values that are not already in use. `KEY_NAME` is the EC2 key pair and the security group (default `bookstack`). `IAM_USER` is the attachment user (default `bookstack-attachments`). Reusing that user replaces its policy with the new bucket, and the previous wiki loses attachment access. Both are described in `.env.example`.

The EC2 console name is the literal `bookstack` in `deploy/05-instance.sh`. Change that tag in this clone before stage 05 when the new instance should be distinguishable in the console.

Authenticate, then deploy. Stages are idempotent. `--from` resumes at a stage.

```bash
aws sso login --profile fleet-coordinator
aws sts get-caller-identity --profile fleet-coordinator
bash deploy/run.sh
```

`fleet-coordinator` is the profile when `dev /wiki` does not set `AWS_PROFILE`. Use the stored name when it does.

| Stage | What it does |
| --- | --- |
| `01-preflight.sh` | Checks the SSO session and records the default VPC and subnet. Creates nothing. |
| `02-bucket.sh` | Creates the private, versioned attachment bucket, or reapplies those settings. |
| `03-iam.sh` | Creates the attachment IAM user. Stores the access key once, in Infisical `dev /wiki`. |
| `04-network.sh` | Creates the key pair and security group. SSH is limited to this machine's address. Ports 80 and 443 are public. |
| `05-instance.sh` | Launches Ubuntu 24.04 (`t3.small`, 30 GB), attaches an elastic IP, and sets `HOST` to `<eip>.sslip.io`. |
| `06-install-bookstack.sh` | Runs the upstream BookStack install. Several minutes. |
| `07-storage.sh` | Points attachments at the private bucket and raises the upload limit. |
| `08-tls-admin.sh` | Issues the sslip.io certificate and replaces the default admin account. |

`08-tls-admin.sh` generates `ADMIN_PASSWORD` and stores it in Infisical `dev /wiki`. The password is not printed. Sign in at `https://<HOST>` with `ADMIN_EMAIL`.

These files stay in the infrastructure checkout and are gitignored:

| File | Contents |
| --- | --- |
| `.env` | `INFISICAL_TOKEN` |
| `deploy/state.env` | `VPC`, `SUBNET`, `SG`, `INSTANCE_ID`, `ALLOC`, `EIP`, `HOST` |
| `deploy/bookstack.pem` | SSH private key for `ubuntu@<EIP>` |
| Infisical `dev /wiki` | AWS profile, bucket, admin login, attachment key |

`deploy/create-user.sh` in this checkout reads `BOOKSTACK_URL` from Infisical `dev /wiki`, and falls back to `HOST` in `deploy/state.env`. Set `BOOKSTACK_URL=https://<HOST>` in `dev /wiki` after stage 08 so the value is explicit. User creation from this checkout still has the SSH fallback, `deploy/create-user-ssh.sh`, for an admin account with two-factor authentication. That fallback is not part of the orchestrator.

## 2. Provision the orchestration folder

Create a new directory and a new git repository beside the infrastructure clone. Copy these paths from that clone:

```
.gitattributes
.python-version
bookstack.env.example
client/bookstack.py
client/bookstack.sh
deploy/create-user.sh
deploy/create-user-http.py
deploy/infisical_api.py
deploy/sync-infisical-users.sh
deploy/sync_infisical_users.py
deploy/test_sync_infisical_users.py
dispatch/__init__.py
dispatch/dispatch.py
dispatch/README.md
pyproject.toml
uv.lock
```

These stay in the infrastructure checkout:

- `.env`, `deploy/state.env`, and `deploy/users/`
- `deploy/01-preflight.sh` through `deploy/08-tls-admin.sh`, `deploy/run.sh`, and `deploy/lib.sh`
- `deploy/create-user-ssh.sh` and `deploy/create-user.php`
- `deploy/bookstack.pem`

In the copied `deploy/create-user.sh`, `deploy/create-user-http.py`, and `dispatch/dispatch.py`, delete the comments that name `deploy/create-user-ssh.sh`. Those files are not in this folder.

Set `name` and `description` in `pyproject.toml` to this project. Then:

```bash
uv sync
```

### Environment

`HOST` is the value in the infrastructure `deploy/state.env`. `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `BOOKSTACK_URL`, and `OPENROUTER_API_KEY` are values in Infisical `dev /wiki` for this wiki's project.

Write `.env`:

```
INFISICAL_TOKEN=
```

`INFISICAL_TOKEN` is a service token (`st.…`) created under the project's Access Control, with read and write on the `dev` environment. The same token can be the one in the infrastructure checkout. Its project id comes from `GET /api/v2/service-token`. The steps to create the token are in [Agent credentials](#agent-credentials).

`deploy/create-user.sh` reads `BOOKSTACK_URL`, `ADMIN_EMAIL`, and `ADMIN_PASSWORD` from Infisical `dev /wiki`. When `BOOKSTACK_URL` is absent it uses `HOST` from `deploy/state.env`. Write that file as one line, and do not copy the infrastructure state file:

```
HOST=<eip>.sslip.io
```

Write `.env.example` with `INFISICAL_TOKEN=` and nothing else. `.env`, `deploy/state.env`, and `deploy/users/` are gitignored.

Write `.gitignore`:

```
.env
deploy/state.env
bookstack.env
deploy/users/
logs/
__pycache__/
.venv/
```

Modal auth is `~/.modal.toml` (`modal token set`, `modal profile activate`). Set `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in Infisical `dev /wiki` only on a machine that has no Modal profile. `dispatch.py` exports them from there.

### README

```markdown
# <project>

Orchestration repo for BookStack wiki agents. It holds just enough to:

1. Create BookStack users and API tokens over HTTPS.
2. Dispatch AI CLI agents into Modal sandboxes that can read and edit the wiki.

The instance, the attachment bucket, and the SSH key live in the infrastructure checkout that deployed this wiki.

## Layout

.env                       # INFISICAL_TOKEN (gitignored)
.env.example
bookstack.env.example      # template for a direct client token
client/bookstack.py        # BookStack API CLI
client/bookstack.sh
deploy/create-user.sh           # create user + token over HTTPS
deploy/create-user-http.py
deploy/infisical_api.py         # service-token client
deploy/sync-infisical-users.sh   # push a local credential file to Infisical
deploy/sync_infisical_users.py
deploy/state.env                # HOST only (gitignored)
deploy/users/                   # credential file kept only when an Infisical write fails
dispatch/dispatch.py
dispatch/README.md
dispatch/task.md

## Credentials

`.env` holds `INFISICAL_TOKEN`. `deploy/create-user.sh`, `deploy/sync-infisical-users.sh`, and `dispatch/dispatch.py` read Infisical `dev /wiki` with that token.

| Variable | Where it lives | Used by |
| --- | --- | --- |
| `INFISICAL_TOKEN` | `.env` | every Infisical call |
| `BOOKSTACK_URL` | `dev /wiki` | create-user; passed to sandboxes through `--bookstack-env` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | `dev /wiki` | `deploy/create-user.sh` |
| `OPENROUTER_API_KEY` | `dev /wiki` | `dispatch/dispatch.py`, via a bare `--secret` |

## Create an agent user

    bash deploy/create-user.sh --name "Research Agent" --email "research-1@example.com" --identity research-agent

The password and `Authorization: Token <id>:<secret>` line are printed once. `--identity` stores them in Infisical. Infisical is the shared record. A failed write leaves `deploy/users/<email>.env`.

## BookStack users in Infisical

`deploy/sync-infisical-users.sh` uploads a local credential file to Infisical and then removes it. Matching secrets are left unchanged, and a different remote value is overwritten. Use it for a file the SSH fallback wrote, or for a file left behind when an Infisical write failed.

| | |
| --- | --- |
| Site | `https://app.infisical.com` |
| Project | `<project name>` (`<project id>`) |
| Environment | `dev` |
| Path | `/users/<folder>` |

`<identity>` is the agent slug, such as `pi-agent` or `deepseek-flash-4.1-pi-crime-agent`. Infisical folder names cannot contain `.`, so the folder is that slug with each `.` replaced by `-`: `deepseek-flash-4.1-pi-crime-agent` is stored at `/users/deepseek-flash-4-1-pi-crime-agent`. Each folder holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`. `AUTHORIZATION` is the header value `Token <id>:<secret>`. `IDENTITY` in the local file is the slug, including any dots. It is not uploaded.

    bash deploy/sync-infisical-users.sh --identity research-agent --file deploy/users/research-1@example.com.env
    bash deploy/sync-infisical-users.sh
    bash deploy/sync-infisical-users.sh --dry-run

The utility creates `/users/<folder>` when it is missing, then upserts the nine fields. The parent `/users` folder must already exist. `--dry-run` reports the folder and secret names that would change.

To read an existing user, send `Authorization: Bearer <INFISICAL_TOKEN>`. The project id is `projectId` (or `workspace`) from `GET /api/v2/service-token`. List folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets?projectId=<project id>&environment=dev&secretPath=/users/<folder>&viewSecretValue=true&expandSecretReferences=false&includeImports=false`. Read wiki settings with `secretPath=/wiki`. Use the folder name, not the raw slug, when the slug contains a dot.

## Dispatch an agent

Take the user's `TOKEN_ID` and `TOKEN_SECRET` from Infisical `dev /users/<folder>`. `dispatch.py` reads `INFISICAL_TOKEN` from `.env` and loads `dev /wiki`, so a bare `--secret OPENROUTER_API_KEY` resolves from there. Pass the BookStack token with `--secret BOOKSTACK_URL` and `--secret BOOKSTACK_TOKEN`, or with `--bookstack-env` when a local file still exists.

    uv run dispatch/dispatch.py \
      --install "npm install -g --ignore-scripts @earendil-works/pi-coding-agent" \
      --copy client/bookstack.py:/usr/local/bin/bookstack \
      --setup 'chmod 0755 /usr/local/bin/bookstack' \
      --bookstack-env deploy/state.env \
      --bookstack-env deploy/users/research-1@example.com.env \
      --secret OPENROUTER_API_KEY \
      --command 'pi --print {prompt}' \
      --prompt @dispatch/task.md
```

### AGENTS.md

Write this file for the orchestrator role on this wiki. Fill in the project name, the project id, the naming rule, and the dispatch rules.

```markdown
# Orchestrator

You create BookStack users and dispatch agents against the wiki in `BOOKSTACK_URL`.

## Users

Name each identity so the model, harness, and topic are visible in the slug. A Deepseek Flash 4.1 agent in the Pi harness researching crime might be `deepseek-flash-4.1-pi-crime-agent`.

BookStack user credentials are stored in Infisical. Read them from there when you select an existing user. When you create a user, pass `--identity` so the credentials are stored in Infisical before you dispatch them. A file under `deploy/users/` exists only when that write failed, or when the user was created over SSH. It is gitignored.

`deploy/create-user.sh --identity <identity>` stores the new user in Infisical. To upload a file that already exists:

    bash deploy/sync-infisical-users.sh --identity <identity> --file deploy/users/<email>.env

`bash deploy/sync-infisical-users.sh` uploads every `deploy/users/*.env` that already contains `IDENTITY`, then removes each file it uploaded. The utility creates `dev /users/<folder>` when it is missing and upserts the nine credential fields. It does not upload `IDENTITY`.

Infisical folder names cannot contain `.`. The folder is the agent slug with each `.` replaced by `-`. `deepseek-flash-4.1-pi-crime-agent` is stored at `/users/deepseek-flash-4-1-pi-crime-agent`. Use the folder name, not the raw slug, when you read a user.

Authenticate to `https://app.infisical.com` with the service token in `INFISICAL_TOKEN`: `Authorization: Bearer <INFISICAL_TOKEN>`. The project id is `projectId` (or `workspace`) from `GET /api/v2/service-token`. The project is <project name> (`<project id>`). Wiki settings live in the `dev` environment at `/wiki`. Agent credentials live at `/users/<folder>`. List those folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets` using the same `projectId`, `environment=dev`, `secretPath=/users/<folder>`, `viewSecretValue=true`, `expandSecretReferences=false`, and `includeImports=false`. Read wiki settings with `secretPath=/wiki`.

Each folder holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`.

## Dispatch

Keep prompts to a general directive. Let the agent decide the page structure.
Run one agent on a book at a time.
`dispatch/task.md` is the prompt file. `dispatch/README.md` is the flag reference.

`dispatch.py` closes the sandbox stdin after start. `pi --print` waits for that EOF before it calls the model. `--detach` prints the sandbox id and returns while the sandbox keeps running until the command exits or `--timeout`. Scratch files then live on the container filesystem, because an ephemeral volume stops when the dispatcher exits. The secrets check on a detached sandbox is `/proc/<pid>/environ` for the entrypoint, the `bash -lc` Modal started or the `pi` child it spawned. Running `bookstack` from `Sandbox.exec` is not that check.
```

### dispatch/task.md

```markdown
# Research task

You are a research agent working inside a Modal sandbox. The team wiki is
BookStack, reachable with the `bookstack` CLI already on `PATH`.
`BOOKSTACK_URL` and `BOOKSTACK_TOKEN` are set in your environment.

Start by orienting yourself:

    bookstack system
    bookstack books list --format text

Then complete the task below. Prefer creating new pages under the right book
over overwriting existing work, and report the page IDs and URLs you touched.

## Task

Describe the research question here.

## Rules

- Use `bookstack --help` and `bookstack <command> --help` to learn the API.
- Write long content with `--markdown-file` rather than quoting it inline.
- Cite the page IDs you read.
- Do not edit users, roles, or settings.
```

### Agent credentials

`bash deploy/create-user.sh --name "..." --email "..." --identity <identity>` drives the admin web UI over HTTPS and stores the new user in Infisical. The first run creates the Agent role: Editor, plus `access-api`, `content-export`, attachment upload, gallery images, and comments.

Each agent folder holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`. `AUTHORIZATION` is the header value `Token <id>:<secret>`. `IDENTITY` is the agent slug, including any dots. It is not uploaded. The Infisical folder is that slug with each `.` replaced by `-`. A failed write leaves those fields, plus `IDENTITY`, in `deploy/users/<email>.env`.

Create a service token under the project's Access Control. Give it read and write on the `dev` environment. Copy the token into `.env` as `INFISICAL_TOKEN`.

Create `/wiki` and `/users` in the `dev` environment before the first deploy. `/wiki` holds the wiki settings, including `BOOKSTACK_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, and `OPENROUTER_API_KEY`. The utilities create `/users/<folder>`. They do not create `/users` itself. The first human password rotation creates `/humans` and `/humans/<folder>`.

`bash deploy/sync-infisical-users.sh` uploads every `deploy/users/*.env` that already contains `IDENTITY`, then removes each file it uploaded. Pass `--identity` and `--file` for one file. `--dry-run` reports the folder and secret names that would change and leaves the file in place. Matching secrets are left unchanged, and a different remote value is overwritten.

```bash
bash deploy/sync-infisical-users.sh --identity research-agent --file deploy/users/research-1@example.com.env
bash deploy/sync-infisical-users.sh
bash deploy/sync-infisical-users.sh --dry-run
uv run python deploy/test_sync_infisical_users.py
```

The unit test does not call Infisical.

To read an existing user, send `Authorization: Bearer <INFISICAL_TOKEN>`. The project id comes from `GET /api/v2/service-token`. List folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets?projectId=<project id>&environment=dev&secretPath=/users/<folder>&viewSecretValue=true&expandSecretReferences=false&includeImports=false`. Use the folder name, not the raw slug, when the slug contains a dot.

Put that project id and these routes in `AGENTS.md`. The orchestrator reads credentials from Infisical when it selects an existing user. `create-user.sh --identity` stores a new user there before they are dispatched.

To rotate a wiki that is already running, use [Rotate a BookStack rollout](rotate-credentials.md). Rotation and sync both use the service token in `INFISICAL_TOKEN`. Sync does not rotate passwords.

### Check the boundary

From the orchestration folder:

```bash
test ! -e deploy/bookstack.pem \
  && test ! -e deploy/lib.sh \
  && test ! -e deploy/create-user-ssh.sh
bash deploy/create-user.sh --name "Example Agent" --email "agent@example.com"
BOOKSTACK_URL=https://<HOST> bash client/bookstack.sh \
  --env-file deploy/users/agent@example.com.env books list --format text
```

`create-user.sh` logs in as the admin user in Infisical `dev /wiki`. Without `--identity`, the new user's file stays in `deploy/users/`. `BOOKSTACK_URL` is the value in `dev /wiki`. The client call uses that URL and the new user's `TOKEN_ID` and `TOKEN_SECRET`.

An admin account with two-factor authentication cannot complete the scripted HTTPS login. Create the user from the infrastructure checkout with `deploy/create-user-ssh.sh`. The private key stays there. That fallback does not call Infisical. From the checkout that holds `INFISICAL_TOKEN`, run `bash deploy/sync-infisical-users.sh --identity <identity> --file <users-dir>/<email>.env`.
