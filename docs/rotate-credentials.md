# Rotate a BookStack rollout

Use this after a credential may have left the machine that holds `rollouts/<name>/`. The command rotates one wiki. Other rollouts stay as they are, because each rollout has its own key pair, attachment user, and Infisical project.

`deploy/rotate-credentials.sh` is the entry point. `deploy/rotate_credentials.py` is the implementation. `deploy/rotate-bookstack.php` is copied to the instance for the BookStack steps. The unit test does not call AWS, SSH, BookStack, or Infisical:

```bash
python deploy/test_rotate_credentials.py
```

## What is rotated

Run the stages in this order. Each stage leaves the previous credential in place until the replacement has worked.

| Stage | What it replaces | Where the new value is kept |
| --- | --- | --- |
| `ssh-key` | The EC2 SSH key and the AWS key pair named `KEY_NAME` | `bookstack.pem` and `bookstack.pem.pub` |
| `s3` | The attachment user's access key | `bookstack-s3.env` and `/var/www/bookstack/.env` on the instance |
| `admin` | The BookStack admin password | `ADMIN_PASSWORD` in `.env` |
| `infisical` | Every account under Infisical `dev /users` | That folder, and a matching file in `users/` when one already exists |
| `humans` | Named people who are not in `/users` | The wiki, `users/<email>.env`, and an email |

The instance database password, `APP_KEY`, and mail password are not in the rollout directory. This command does not rotate them.

Infisical here is read and written with the service token in `INFISICAL_TOKEN`. `deploy/sync-infisical-users.sh` is a different path: it pushes local files with the universal-auth client id and secret, and it does not rotate anything.

## Before you start

The rollout `.env` needs `AWS_PROFILE`, `AWS_DEFAULT_REGION`, `BUCKET`, `IAM_USER`, `KEY_NAME`, `ADMIN_EMAIL`, and `BOOKSTACK_URL`. `state.env` needs `EIP`, `HOST`, and `INSTANCE_ID`. The SSH private key has to log in as `ubuntu@<EIP>`.

For the Infisical stage, `INFISICAL_TOKEN` is a service token (`st.…`) created under the project's Access Control, with read and write on the `dev` environment. The token's project id comes from `GET /api/v2/service-token`. If `INFISICAL_PROJECT_ID` is also set, it must be that same id.

For the human stage, set `RESEND_API_KEY` and `RESEND_FROM`. `RESEND_FROM` looks like `Your Name <you@example.com>`. The domain has to be verified in Resend. The message is sent with curl. Python's HTTP client is blocked by Cloudflare in front of Resend with error 1010.

```bash
aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity --profile "$AWS_PROFILE"
```

SSH in the security group has to allow this machine. The account running the command needs permission to replace the key pair and the attachment user's access keys.

Look at the plan before changing anything. `--dry-run` reads AWS and Infisical and writes nothing:

```bash
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh all --dry-run --human person@example.com
```

## Stages

```bash
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh ssh-key
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh s3
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh admin
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh infisical
ROLLOUT=knowledge-workers bash deploy/rotate-credentials.sh humans --human person@example.com
```

`all` runs those stages in that order. Without `--human`, `all` skips the email stage and says so.

### SSH key

The AWS key pair name is only applied when an instance is launched. The running instance trusts `~/.ssh/authorized_keys`.

The command refuses to start unless exactly one instance uses `KEY_NAME` and that instance is `INSTANCE_ID`. A shared key name would replace the key pair other wikis launch with.

It generates an ed25519 key, appends the public key, and logs in with it while the old key still works. It then leaves only the new public key in `authorized_keys` and checks that the old private key is rejected. After that check, it replaces `bookstack.pem` and imports the public key as `KEY_NAME`. Import goes through a temporary key pair name, `<KEY_NAME>-incoming`, so a failed import does not destroy the name before the new key is stored.

A failure before the old key is removed deletes the unused new private key. The old key still logs in. A failure after that cutover keeps the new private key, because it is the one the instance accepts.

### Attachment key

An IAM user can hold two access keys. The command creates a second key and lists the bucket with it. A new key often answers `InvalidAccessKeyId` for a few seconds, so the list is retried. If the list still fails, the new key is deleted and the old key stays in place.

The key is then written into `/var/www/bookstack/.env`. BookStack has to answer a HeadObject on `uploads/files/` with that key. If it does not, the previous key is written back and the new key is deleted. On success, php-fpm is restarted, `bookstack-s3.env` is updated, and the previous access key is deleted. The user is left with one key.

### Admin password

`deploy/rotate-bookstack.php` sets the password on the instance with BookStack's user repository, checks the stored hash, and clears that user's server session. The new password is written to `ADMIN_PASSWORD` before the web login is tried, so a login problem does not leave the new password only in memory. The web login then has to open `/settings/users`.

### Infisical users

Folders live at `dev /users/<folder>`. A folder name cannot contain `.`; the agent slug `deepseek-flash-4.1-pi-crime-agent` is stored as `deepseek-flash-4-1-pi-crime-agent`. Each folder's `EMAIL` and numeric `USER_ID` must match the BookStack user. A folder with `TOKEN_ID` and `TOKEN_SECRET` gets a new password and a new API token. The previous tokens for that user are deleted. A folder without a token gets a new password only.

The first account is rotated and read back from Infisical before any other account is touched. `--pilot <folder>` chooses that account. The default is the first email in alphabetical order. The rest run in groups of ten. For each account the command checks:

- the BookStack hash matches the new password
- the new token calls `GET /api/system` when the account has a token
- Infisical returns the same `PASSWORD`, `TOKEN_ID`, `TOKEN_SECRET`, and `AUTHORIZATION`

`AUTHORIZATION` is `Token <id>:<secret>`. An existing `users/<email>.env` is updated. A new local file is written only when BookStack changed and the Infisical write did not succeed, so the new password is not left only in the process. That failure stops the command after the accounts already reported.

### People who are not agents

Frank and Sravan, on the wiki this procedure was first run against, were not in `/users`. Pass `--human` for each such person. The command looks them up by email, rotates the password only, writes `users/<email>.env`, and then sends the notice below. The file is written before the send, so a mail failure does not leave the new password only in memory. It does not send mail if the password change did not succeed.

Resend is curl against `POST https://api.resend.com/emails`. The From address is `RESEND_FROM`. The sign-off is the first word of that display name. The subject is `Your password for <host> was reset`.

```text
Hi <first name>,

I reset your password on this wiki because Cursor had a data breach. I don't know that these credentials were exposed. This is a precaution.

This email was written by an agent. Agents say dumb things. The password below is the part to rely on.

Sign in: <BOOKSTACK_URL>/login
Email: <their email>
Password: <the new password>

After you sign in, you can change it under your account settings.

<first name of RESEND_FROM>
```

Say that the exposure is unknown. The first draft of this notice stated it as a fact, and that was wrong. Say that an agent wrote the mail.

## One wiki from a checkout

Without `ROLLOUT`, the same command reads `./.env`, `deploy/state.env`, `deploy/bookstack.pem`, `deploy/bookstack-s3.env`, and `deploy/users/`.

```bash
bash deploy/rotate-credentials.sh --dry-run
bash deploy/rotate-credentials.sh all --human person@example.com
```
