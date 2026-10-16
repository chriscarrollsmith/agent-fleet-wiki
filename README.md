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
