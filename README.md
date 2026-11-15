# agent-fleet-wiki

Private BookStack wiki (S3-backed attachments) on a single AWS EC2 instance.

This checkout deploys one wiki and keeps the SSH key. To stand up another wiki, and an orchestration folder that administers it with the admin login and no SSH, follow [docs/provision.md](docs/provision.md).

## 1. Configure

```bash
cp .env.example .env
# edit .env: INFISICAL_TOKEN
```

`.env` holds the Infisical service token. Wiki settings (`AWS_PROFILE`, `AWS_DEFAULT_REGION`, `BUCKET`, `ADMIN_EMAIL`, and the values the deploy writes later) live in that token's project at `dev /wiki`.

### Several wikis from one checkout

Each wiki can keep its own credentials in `rollouts/<name>/`, which is
gitignored:

```
rollouts/<name>/
  .env               # INFISICAL_TOKEN only
  state.env          # VPC, SUBNET, SG, INSTANCE_ID, ALLOC, EIP, HOST
  bookstack.pem      # SSH key for ubuntu@<EIP>
  users/             # a credential file left behind when an Infisical write fails
```

Set `ROLLOUT` to choose one. Every deploy and user script, and
`dispatch/dispatch.py`, then reads that directory's token instead of `.env`.
The token's project holds that wiki's settings at `dev /wiki`.

```bash
ROLLOUT=knowledge-workers bash deploy/run.sh --from 06
ROLLOUT=knowledge-workers bash deploy/create-user.sh --name "..." --email "..."
```

Without `ROLLOUT`, the single-wiki layout below applies.

## 2. Authenticate with AWS SSO

```bash
aws configure sso                                # first time only
aws sso login --profile fleet-coordinator
aws sts get-caller-identity --profile fleet-coordinator
```

## 3. Deploy

```bash
bash deploy/run.sh            # full run; output is teed to logs/deploy-*.log
bash deploy/run.sh --from 05  # resume from a stage
```

Stages are idempotent, so re-running is safe. The SSH key
(`deploy/bookstack.pem`) is gitignored. The admin password and the attachment
key are written to Infisical `dev /wiki`.

## 4. Create an agent user

BookStack has no API endpoint for API tokens, so a token has to be created
through the admin UI. `deploy/create-user.sh` does this over HTTPS, driving
the same routes a browser uses. It needs only the site URL and the admin
credentials, which are in Infisical `dev /wiki` (`BOOKSTACK_URL`, `ADMIN_EMAIL`,
`ADMIN_PASSWORD`). No SSH, AWS, or instance access is required, so this
works even when the AWS SSO session has expired.

The default role, Agent, is a copy of Editor plus the permissions the API
needs for normal research work: `access-api`, `content-export`, attachment
upload, gallery images, and comments. Editor does not have the attachment or
comment permissions. Registration stays closed.

```bash
bash deploy/create-user.sh \
  --name "Agent User" \
  --email "agent-user@example.com"
```

The password and `Authorization: Token <id>:<secret>` line are printed once.
Re-running the same email fails, because the user already exists. An existing
`--role` is reused only when that role already has `access-api`.

`--identity` stores those credentials in Infisical. The service token in
`INFISICAL_TOKEN` is what the utility uses. It writes `dev /users/<folder>`
and does not upload `IDENTITY`. Infisical folder names cannot contain `.`, so
each `.` in the slug becomes `-`: `deepseek-flash-4.1-pi-crime-agent` is stored
at `/users/deepseek-flash-4-1-pi-crime-agent`. A failed write leaves the
credentials in `deploy/users/`.

```bash
bash deploy/create-user.sh \
  --name "Agent User" \
  --email "agent-user@example.com" \
  --identity agent-user

bash deploy/sync-infisical-users.sh --identity agent-user --file deploy/users/agent-user@example.com.env
bash deploy/sync-infisical-users.sh --dry-run
```

`deploy/sync-infisical-users.sh` with no arguments pushes every
`deploy/users/*.env` that already contains `IDENTITY`, then removes each file
it uploaded. The orchestrator prompt that reads these credentials back is in
[docs/provision.md](docs/provision.md).

An existing Agent role can be given those permissions without creating
another user:

```bash
bash deploy/create-user.sh --sync-role
```

If the admin account has two-factor authentication enabled, the scripted
login cannot complete. Use the SSH/PHP fallback instead, which runs on the
instance and does not need the admin password:

```bash
bash deploy/create-user-ssh.sh \
  --name "Agent User" \
  --email "agent-user@example.com"
```

That fallback writes the same local file and does not call Infisical. Sync it afterwards with `deploy/sync-infisical-users.sh`. A successful sync removes the file.

The new user can see books the Editor role can see. A book with its own
permission list must include the Agent role or the API will not see it.

## 5. Use the API

`client/bookstack.sh` is the client for an agent user. It needs Python 3.
Copy `bookstack.env.example` to `bookstack.env` and set the URL and the
token from `deploy/create-user.sh`. The token file is gitignored.

```bash
bash client/bookstack.sh --env-file bookstack.env system
bash client/bookstack.sh --env-file bookstack.env books list --format text
bash client/bookstack.sh --env-file bookstack.env pages get 3 --field markdown
bash client/bookstack.sh --env-file bookstack.env pages create \
  --book 1 --name "Finding" --markdown-file finding.md
bash client/bookstack.sh --env-file bookstack.env attachments upload \
  --page 3 --file notes.txt
bash client/bookstack.sh --env-file bookstack.env attachments get 1 --output notes.txt
bash client/bookstack.sh --env-file bookstack.env search "capital region" --book 1
bash client/bookstack.sh --env-file bookstack.env export page 3 --format markdown
```

`BOOKSTACK_URL` and `BOOKSTACK_TOKEN` in the environment override the env
file. Run `bash client/bookstack.sh --help` and
`bash client/bookstack.sh pages --help` for the rest of the commands:
shelves, books, chapters, pages, attachments, images, comments, tags, and
export. Listing accepts `--filter field=value`, `--sort`, `--count`, and
`--offset`.

Comments need `comment-create-all`. A role created before that permission
was included can gain it, along with image upload, with:

```bash
bash deploy/create-user.sh --sync-role
```

## 6. Rotate credentials

`deploy/rotate-credentials.sh` replaces one wiki's SSH key, attachment key, admin password, and Infisical `dev /users` accounts. People who are not in Infisical are named with `--human` and get an email. The order, the checks, and the email text are in [docs/rotate-credentials.md](docs/rotate-credentials.md).

```bash
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh all --dry-run
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh all --human person@example.com
```
