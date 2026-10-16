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
Agent, is a copy of Editor plus `access-api` and the attachment
permissions the API requires (`attachment-create-all`,
`attachment-update-all`, `attachment-delete-all`). Editor does not have
those. Registration stays closed.

```bash
bash deploy/create-user.sh \
  --name "Cursor Agent" \
  --email "cursor-agent@promptlytechnologies.com"
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
