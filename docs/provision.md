# Provision a BookStack and its orchestrator

Two directories.

This repo is the infrastructure checkout. It creates the EC2 instance, the private attachment bucket, and the admin account, and it keeps the SSH key. One working copy is bound to one wiki by `deploy/state.env`. Running `bash deploy/run.sh` again updates that wiki. To keep several wikis in one checkout instead, give each a `rollouts/<name>/` directory and set `ROLLOUT=<name>`. The README describes that layout.

The orchestration folder is a sibling directory. It creates BookStack users and dispatches agents. Its BookStack credential is the admin login. The instance key, the AWS profile, and the S3 attachment keys stay in the infrastructure checkout.

## 1. Deploy the wiki

Clone this repo into a new directory and deploy from that clone. The checkout that already has a `deploy/state.env` is bound to the wiki it created, including `deploy/bookstack.pem` and `deploy/bookstack-s3.env`.

```bash
cp .env.example .env
```

Set `AWS_PROFILE`, `AWS_DEFAULT_REGION`, `BUCKET`, and `ADMIN_EMAIL`. `BUCKET` is a globally unique S3 name.

In the same account, also set `KEY_NAME` and `IAM_USER` to values that are not already in use. `KEY_NAME` is the EC2 key pair and the security group (default `bookstack`). `IAM_USER` is the attachment user (default `bookstack-attachments`). Reusing that user replaces its policy with the new bucket, and the previous wiki loses attachment access. Both variables are commented in `.env.example`.

The EC2 console name is the literal `bookstack` in `deploy/05-instance.sh`. Change that tag in this clone before stage 05 when the new instance should be distinguishable in the console.

Authenticate, then deploy. Stages are idempotent. `--from` resumes at a stage.

```bash
set -a && source .env && set +a
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity --profile "$AWS_PROFILE"
bash deploy/run.sh
```

| Stage | What it does |
| --- | --- |
| `01-preflight.sh` | Checks the SSO session and records the default VPC and subnet. Creates nothing. |
| `02-bucket.sh` | Creates the private, versioned attachment bucket, or reapplies those settings. |
| `03-iam.sh` | Creates the attachment IAM user. Writes the access key once, to `deploy/bookstack-s3.env`. |
| `04-network.sh` | Creates the key pair and security group. SSH is limited to this machine's address. Ports 80 and 443 are public. |
| `05-instance.sh` | Launches Ubuntu 24.04 (`t3.small`, 30 GB), attaches an elastic IP, and sets `HOST` to `<eip>.sslip.io`. |
| `06-install-bookstack.sh` | Runs the upstream BookStack install. Several minutes. |
| `07-storage.sh` | Points attachments at the private bucket and raises the upload limit. |
| `08-tls-admin.sh` | Issues the sslip.io certificate and replaces the default admin account. |

`08-tls-admin.sh` generates `ADMIN_PASSWORD` and appends it to `.env`. The password is not printed. Sign in at `https://<HOST>` with `ADMIN_EMAIL`.

These files stay in the infrastructure checkout and are gitignored:

| File | Contents |
| --- | --- |
| `.env` | AWS profile, bucket, `ADMIN_EMAIL`, `ADMIN_PASSWORD` |
| `deploy/state.env` | `VPC`, `SUBNET`, `SG`, `KEY_FILE`, `SECRET_FILE`, `INSTANCE_ID`, `ALLOC`, `EIP`, `HOST` |
| `deploy/bookstack.pem` | SSH private key for `ubuntu@<EIP>` |
| `deploy/bookstack-s3.env` | Attachment bucket access key |

`deploy/create-user.sh` in this checkout reads `BOOKSTACK_URL` from `.env`, and falls back to `HOST` in `deploy/state.env`. Set `BOOKSTACK_URL=https://<HOST>` in `.env` after stage 08 so the value is explicit. User creation from this checkout still has the SSH fallback, `deploy/create-user-ssh.sh`, for an admin account with two-factor authentication. That fallback is not part of the orchestrator.

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
- `deploy/bookstack.pem` and `deploy/bookstack-s3.env`

In the copied `deploy/create-user.sh`, `deploy/create-user-http.py`, and `dispatch/dispatch.py`, delete the comments that name `deploy/create-user-ssh.sh`. Those files are not in this folder.

Set `name` and `description` in `pyproject.toml` to this project. Then:

```bash
uv sync
```

### Environment

`HOST` is the value in the infrastructure `deploy/state.env`. `ADMIN_EMAIL` and `ADMIN_PASSWORD` are the values in the infrastructure `.env`.

Write `.env`:

```
BOOKSTACK_URL=https://<HOST>
ADMIN_EMAIL=<infrastructure ADMIN_EMAIL>
ADMIN_PASSWORD=<infrastructure ADMIN_PASSWORD>
OPENROUTER_API_KEY=
INFISICAL_PROJECT_ID=
INFISICAL_CLIENT_ID=
INFISICAL_CLIENT_SECRET=
```

`INFISICAL_CLIENT_ID` and `INFISICAL_CLIENT_SECRET` are the universal-auth client id and secret of a machine identity created under the project's Access Control. `INFISICAL_PROJECT_ID` is that project's id. Cloud agents often receive the client id and secret in the environment. Put the project id in this file as well, and write the same id into `AGENTS.md`. The steps to create the identity are in [Agent credentials](#agent-credentials).

`deploy/create-user.sh` reads `BOOKSTACK_URL`, `ADMIN_EMAIL`, and `ADMIN_PASSWORD` from this file. When `BOOKSTACK_URL` is absent it uses `HOST` from `deploy/state.env`. Write that file as one line, and do not copy the infrastructure state file:

```
HOST=<eip>.sslip.io
```

Write `.env.example` with the same keys and empty values. `.env`, `deploy/state.env`, and `deploy/users/` are gitignored.

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

Modal auth is `~/.modal.toml` (`modal token set`, `modal profile activate`). Set `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` in `.env` only on a machine that has no Modal profile.

### README

```markdown
# <project>

Orchestration repo for BookStack wiki agents. It holds just enough to:

1. Create BookStack users and API tokens over HTTPS.
2. Dispatch AI CLI agents into Modal sandboxes that can read and edit the wiki.

The instance, the attachment bucket, and the SSH key live in the infrastructure checkout that deployed this wiki.

## Layout

.env                       # local secrets (gitignored)
.env.example
bookstack.env.example      # template for a direct client token
client/bookstack.py        # BookStack API CLI
client/bookstack.sh
deploy/create-user.sh           # create user + token over HTTPS
deploy/create-user-http.py
deploy/sync-infisical-users.sh   # push deploy/users credentials to Infisical
deploy/sync_infisical_users.py
deploy/state.env                # HOST only (gitignored)
deploy/users/                   # generated credentials, one file per agent (gitignored)
dispatch/dispatch.py
dispatch/README.md
dispatch/task.md

## Credentials

`.env` is loaded by `deploy/create-user.sh`, `deploy/sync-infisical-users.sh`, and `dispatch/dispatch.py`.

| Variable | Used by |
| --- | --- |
| `BOOKSTACK_URL` | create-user; passed to sandboxes through `--bookstack-env` |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | `deploy/create-user.sh` |
| `OPENROUTER_API_KEY` | `dispatch/dispatch.py`, via a bare `--secret` |
| `INFISICAL_PROJECT_ID` | `deploy/sync-infisical-users.sh` |
| `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET` | `deploy/sync-infisical-users.sh` |

The client id and secret belong to a machine identity created under the project's Access Control. Cloud agents receive those two from the environment. `INFISICAL_PROJECT_ID` is the Infisical project id.

## Create an agent user

    bash deploy/create-user.sh --name "Research Agent" --email "research-1@example.com" --identity research-agent

The password and `Authorization: Token <id>:<secret>` line are printed once and written to `deploy/users/<email>.env`. `--identity` also syncs that file to Infisical. The local file is a copy. The shared record is Infisical.

## BookStack users in Infisical

`deploy/sync-infisical-users.sh` pushes local credential files to Infisical. The local file is the source: matching secrets are left unchanged, and a different remote value is overwritten.

| | |
| --- | --- |
| Site | `https://app.infisical.com` |
| Project | `<project name>` (`<project id>`) |
| Environment | `dev` |
| Path | `/users/<identity>` |

`<identity>` is the agent slug. Each folder holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`. `AUTHORIZATION` is the header value `Token <id>:<secret>`. `IDENTITY` in the local file is the slug. It is not uploaded.

    bash deploy/sync-infisical-users.sh --identity research-agent --file deploy/users/research-1@example.com.env
    bash deploy/sync-infisical-users.sh
    bash deploy/sync-infisical-users.sh --dry-run

The utility creates `/users/<identity>` when it is missing, then upserts the nine fields. The parent `/users` folder must already exist. `--dry-run` reports the folder and secret names that would change.

To read an existing user, log in with `POST /api/v1/auth/universal-auth/login`. List folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets?projectId=<project id>&environment=dev&secretPath=/users/<identity>&viewSecretValue=true&expandSecretReferences=false&includeImports=false`.

## Dispatch an agent

Take the user's `TOKEN_ID` and `TOKEN_SECRET` from Infisical, or from the local `deploy/users/<email>.env` copy. `dispatch.py` loads `.env`, so a bare `--secret OPENROUTER_API_KEY` resolves from it.

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

Name each identity so the model, harness, and topic are visible in the slug.

BookStack user credentials are stored in Infisical. Read them from there when you select an existing user. When you create a user, sync that user's local credential file to Infisical before dispatching them. The file under `deploy/users/` is a local copy and is gitignored.

`deploy/create-user.sh --identity <identity>` writes the local file and syncs it. To sync a file that already exists:

    bash deploy/sync-infisical-users.sh --identity <identity> --file deploy/users/<email>.env

`bash deploy/sync-infisical-users.sh` pushes every `deploy/users/*.env` that already contains `IDENTITY`. The local file is the source. The utility creates `dev /users/<identity>` when it is missing and upserts the nine credential fields. It does not upload `IDENTITY`.

Authenticate to `https://app.infisical.com` with universal auth (`INFISICAL_CLIENT_ID` and `INFISICAL_CLIENT_SECRET`): `POST /api/v1/auth/universal-auth/login`. Those credentials belong to a machine identity created under this project's Access Control. The project is <project name> (`<project id>`). Credentials live in the `dev` environment at `/users/<identity>`, where `<identity>` is the agent slug above. List those folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets` using the same `projectId`, `environment=dev`, `secretPath=/users/<identity>`, `viewSecretValue=true`, `expandSecretReferences=false`, and `includeImports=false`.

Each folder holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`.

## Dispatch

Keep prompts to a general directive. Let the agent decide the page structure.
Run one agent on a book at a time.
`dispatch/task.md` is the prompt file. `dispatch/README.md` is the flag reference.
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

`bash deploy/create-user.sh --name "..." --email "..." --identity <identity>` drives the admin web UI over HTTPS, writes `deploy/users/<email>.env`, and syncs that file to Infisical. The first run creates the Agent role: Editor, plus `access-api`, `content-export`, attachment upload, gallery images, and comments.

The local file holds `USER_ID`, `EMAIL`, `NAME`, `ROLE`, `ROLE_ID`, `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, `AUTHORIZATION`, and `IDENTITY`. `AUTHORIZATION` is the header value `Token <id>:<secret>`. `IDENTITY` is the agent slug. It stays in the local file and is not uploaded.

Create the Infisical machine identity under the project's Access Control. Open the project, go to Access Control, then Machine Identities, and add an identity on the Create New tab. Give it a project role that can list and create secret folders, and can read and write secret values, in the `dev` environment. Universal Auth is enabled on a new identity. Copy the client id, create a client secret, and store both with the project id in `.env`.

Create a `/users` folder in the `dev` environment before the first sync. The utility creates `/users/<identity>` under that folder. It does not create `/users` itself.

`bash deploy/sync-infisical-users.sh` pushes every `deploy/users/*.env` that already contains `IDENTITY`. Pass `--identity` and `--file` for one file. `--dry-run` reports the folder and secret names that would change. The local file is the source: matching secrets are left unchanged, and a different remote value is overwritten.

```bash
bash deploy/sync-infisical-users.sh --identity research-agent --file deploy/users/research-1@example.com.env
bash deploy/sync-infisical-users.sh
bash deploy/sync-infisical-users.sh --dry-run
uv run python deploy/test_sync_infisical_users.py
```

The unit test does not call Infisical.

To read an existing user, authenticate with `POST /api/v1/auth/universal-auth/login` and the same client id and secret. List folders with `GET /api/v2/folders?projectId=<project id>&environment=dev&path=/users`. Read one user with `GET /api/v4/secrets?projectId=<project id>&environment=dev&secretPath=/users/<identity>&viewSecretValue=true&expandSecretReferences=false&includeImports=false`.

Put that project id and these routes in `AGENTS.md`. The orchestrator reads credentials from Infisical when it selects an existing user, and syncs a new user's local file before dispatching them.

### Check the boundary

From the orchestration folder:

```bash
test ! -e deploy/bookstack.pem \
  && test ! -e deploy/lib.sh \
  && test ! -e deploy/create-user-ssh.sh \
  && test ! -e deploy/bookstack-s3.env
bash deploy/create-user.sh --name "Example Agent" --email "agent@example.com"
set -a && source .env && set +a
bash client/bookstack.sh --env-file deploy/users/agent@example.com.env books list --format text
```

`create-user.sh` logs in as the admin user in `.env`. The client call uses that same `BOOKSTACK_URL` and the new user's `TOKEN_ID` and `TOKEN_SECRET`.

An admin account with two-factor authentication cannot complete the scripted HTTPS login. Create the user from the infrastructure checkout with `deploy/create-user-ssh.sh`. The private key stays there. That fallback does not call Infisical. From the checkout that holds the Infisical variables, run `bash deploy/sync-infisical-users.sh --identity <identity> --file <users-dir>/<email>.env`.
