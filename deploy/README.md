# Docker deployment

The repository's `Dockerfile` builds the React web application and packages it
with the FastAPI backend. The runtime uses Python 3.12 and a non-root user.
Docker Compose starts PostgreSQL 16 and Redis 7 with persistent volumes, waits
for their health checks, then starts two API workers. Only the API's port is
published, bound to `127.0.0.1` by default. Database migrations run automatically
during application startup; a PostgreSQL advisory lock serializes migration
attempts from multiple workers.

## Configure and start

1. Install Docker Engine and the Docker Compose plugin.
2. Copy `.env.example` to `.env` and replace the required placeholders. Generate
   independent `DB_PASSWORD`, `REDIS_PASSWORD`, `AUTH_SESSION_SECRET`,
   `AUTH_INVITE_CODE` and `LUMA_SECRETS_KEY` values. The example includes key
   generation commands. Set `.env` permissions to `0600`.
3. Set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` for your compatible provider.
   To exercise the application without a model endpoint, set `LUMA_PROVIDER=local`.
4. Build the image from this source tree and start the service with
   `docker compose up -d --build --wait`. The project publishes source code
   only; no prebuilt image is provided.
5. Initialize the first administrator from the server shell:

   ```sh
   docker compose exec luma python -m app.cli create-admin --username admin
   ```

   Add `--email admin@example.com` and `--display-name "Administrator"` if
   needed. The command runs the same database initialization and migrations as
   application startup, prompts twice for a password using hidden input, and
   needs no bootstrap token. The two password entries must match. It refuses
   to run with exit code 2 if any users already exist;
   `--force-additional-admin` explicitly permits another admin. A duplicate
   username or email produces exit code 3. Successful output contains only
   the username and role.
   To read one password line from redirected stdin, add `--password-stdin`
   and disable the Docker TTY:

   ```sh
   docker compose exec -T luma python -m app.cli create-admin --username admin --password-stdin < /path/to/protected-password-file
   ```

   API initialization is also available: set `AUTH_BOOTSTRAP_TOKEN` to an
   independent random value of at least 16 characters before starting the
   service, then include it in the `bootstrap_token` field of
   `POST /api/v1/auth/register`. An empty or shorter server token blocks
   first-account API registration. An invalid token returns 403. The first
   account becomes an administrator, including in invite or closed mode.
   The current web, Flutter, and desktop clients have no token input, so this
   method requires a separate API request. Use the server-shell command above
   for the recommended setup.
6. After initialization, remove `AUTH_BOOTSTRAP_TOKEN` from `.env` and recreate
   Luma with `docker compose up -d --wait luma`. The token already stops granting
   administrator privileges as soon as a user exists. Later accounts follow
   `AUTH_REGISTRATION=open|invite|closed`; the default is `invite`.

Complete initialization before making the service public. The Compose port
mapping is `"${LUMA_BIND_HOST:-127.0.0.1}:${LUMA_PORT:-8000}:8000"`; keep the
default loopback binding when the reverse proxy runs on the host. If your
deployment needs a different bind address, set `LUMA_BIND_HOST` explicitly
after initialization. For a public deployment, configure HTTPS at a reverse
proxy, set `AUTH_COOKIE_SECURE=true`, and set `CORS_ORIGINS` to the exact web
origin.
Compose always sets `AUTH_REQUIRED=true`. Put your server's public addresses in
`MCP_BLOCKED_HOSTS` and `BROWSER_BLOCKED_HOSTS` to prevent tools from accessing
the gateway itself. Configure optional integrations only on the server.

Check liveness with `curl http://localhost:8000/health` and container status with
`docker compose ps`. The image health check requires both PostgreSQL and Redis
to be reachable. The deployment follows Docker's
[healthy dependency startup behavior](https://docs.docker.com/compose/how-tos/startup-order/).
Do not paste rendered Compose configuration or environment files into public logs.

## Persistent data

| Volume | Content |
| --- | --- |
| `pgdata` | PostgreSQL database |
| `redisdata` | Redis AOF, sessions, rate-limit counters and generation events |
| `filesdata` | Local uploads and sandbox backup objects under `backend/data` |

`FILE_STORAGE` accepts the built-in values `local` and `cos`. The `fileservice`
backend is the example plugin `plugins_examples.fileservice` and stays unloaded
until `LUMA_PLUGINS` names it. See `docs/extending.md`. This release has no S3
adapter. COS and file-service credentials remain server-side. Keep every storage
backend used by existing file rows configured until those files are migrated or
deleted. The local volume is still needed for staging and sandbox backups when
remote file storage is enabled. Provision remote-storage backups independently.

Agent code and browser tools require Tencent Cloud Agent Runtime. Set
`AGENT_RUNTIME_ENABLED=true`, `AGENT_RUNTIME_API_MODE=e2b`, your `E2B_DOMAIN`,
`E2B_API_KEY`, and provisioned tool names. Region and tool names have no
default; set `AGENT_RUNTIME_REGION` for cloud API mode and
`AGENT_RUNTIME_CODE_TOOL` / `AGENT_RUNTIME_BROWSER_TOOL` (or
`AGENT_RUNTIME_AIO_TOOL`) before enabling the sandbox. An unavailable sandbox
prevents execution; the API host does not execute
user or model commands.

## Upgrading an existing database

Startup migration `0018_accounts` follows `0017_merge_features` and preserves
every existing `user_id`, so conversations, files, memories and other owned
records remain attached to the same account. Each legacy profile receives a
username of `legacy_` followed by the first 24 hexadecimal characters of its
user-ID MD5 digest. Its display name defaults to the previous nickname.
Emails are trimmed and lowercased; when normalized emails collide, the row
with the lexicographically smallest `user_id` retains the email and the other
rows receive an empty email field. These rows keep their existing identities
and can set a new unique email after enrollment.

Legacy profiles have no independent password and cannot log in until a server
operator enrolls them. They also count as existing users: registering a new
account does not grant administrator privileges on an upgraded database.
Back up the database and local data before upgrading. After migration, identify
the existing `user_id` from your records or the `users` table. With the default
database schema, this read-only command lists profiles awaiting enrollment:

```bash
docker compose exec postgres sh -c 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT user_id, username, display_name, email FROM users WHERE password_hash IS NULL ORDER BY user_id;"'
```

Enroll the intended initial administrator, replacing `USER_ID` and `NAME` with
the preserved identifier and a valid username:

```bash
docker compose exec luma python scripts/enroll_account.py USER_ID --username NAME --admin
```

The script asks for the new password interactively and does not put it in shell
arguments or logs. It only enrolls a profile whose password hash is still empty,
preserves its `user_id`, and invalidates existing sessions. Repeat without
`--admin` for other legacy profiles. If a custom `DB_SCHEMA` is configured,
inspect its `users` table instead of the default schema. Fresh installations
use the local `create-admin` command or token-protected API initialization
described above.

## Backup and restore

Run `./scripts/backup.sh [backup-directory]`. It briefly stops a running Luma
service to align the database and local files, writes a timestamped directory
with `database.dump`, `files.tar.gz` and `SHA256SUMS`, then restarts the service.
PostgreSQL and Redis remain running. Keep the backup and the current secret
configuration in protected, separate storage; `.env` is intentionally excluded.
Encrypted connector credentials require the original `LUMA_SECRETS_KEY`.

Run `./scripts/restore.sh /path/to/backup --yes` only when you intend to replace
the current data. It verifies checksums and rejects unsafe archive paths before
stopping Luma, restores PostgreSQL and local files, resets Redis caches and
sessions, and restarts Luma if it was previously running. A failed restore keeps
Luma stopped so it cannot write into partially restored data. Users must log in
again. Redis sessions are deliberately not recovered from backups.

`docker compose down` preserves volumes; `docker compose down -v` deletes them.
Take a verified backup before removing volumes or upgrading database major versions.

## Automation

`ci.yml` runs one backend test process against PostgreSQL/Redis service
containers and builds the web app. No workflow builds or publishes container
images or installers. Compose tags the locally built image as `luma:local`; if
you want to distribute your own image, change `image:` in `docker-compose.yml`
and push it to a registry you control.
