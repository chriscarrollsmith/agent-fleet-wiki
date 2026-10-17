# agent-fleet-wiki

Private BookStack wiki (S3-backed attachments) on a single AWS EC2 instance.

## 1. Configure

```bash
cp .env.example .env
# edit .env: AWS_PROFILE, AWS_DEFAULT_REGION, BUCKET (globally unique), ADMIN_EMAIL
```

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

Stages are idempotent, so re-running is safe. Generated artifacts
(`deploy/bookstack.pem`, `deploy/bookstack-s3.env`) and the admin password
(added to `.env` as `ADMIN_PASSWORD`) are gitignored.

## 4. Create an agent user

BookStack does not ship a command for a normal user or an API token.
`deploy/create-user.sh` creates both on the instance. The default role,
Agent, is a copy of Editor plus the permissions the API needs for
normal research work: `access-api`, `content-export`, attachment upload,
gallery images, and comments. Editor does not have the attachment or
comment permissions. Registration stays closed.

```bash
bash deploy/create-user.sh \
  --name "Agent User" \
  --email "agent-user@example.com"
```

The password and `Authorization: Token <id>:<secret>` line are printed once
and written under `deploy/users/`, which is gitignored. Re-running the same
email fails, because the user already exists. An existing `--role` is reused
only when that role already has `access-api`.

An existing Agent role can be given those permissions without creating
another user:

```bash
bash deploy/create-user.sh --sync-role
```

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
