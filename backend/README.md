# Luma backend

Luma is a self-hosted personal assistant. This FastAPI service provides the
shared JSON API for the web, mobile, and desktop clients. PostgreSQL 16 stores
accounts and application data; Redis stores sessions, rate limits, and worker
coordination state.

## Development

Use Python 3.9 or later. Production images use Python 3.12. Create your own
virtual environment, install `requirements.txt`, and configure the environment
from the repository's `.env.example`. A supplied `.venv` can be a shared
environment; do not install packages into it.

Run from this directory:

```sh
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

`GET /health` is the public health check. `/docs` serves the OpenAPI reference.
The application serves `apps/web/dist` when it is present. Build the web client
before running the backend if you need the user interface.

Database migrations run before workers accept requests. Each production worker
shares PostgreSQL and Redis. Per-user business queries use the existing
`user_id` identity, so client-facing identifiers remain stable across account
schema upgrades.

## Accounts

The service owns usernames, optional email addresses, password hashes, and
roles. Before any users exist, API registration requires a `bootstrap_token`
request field matching the server's `AUTH_BOOTSTRAP_TOKEN`. Configure an
independent random token of at least 16 characters; an empty or shorter value
blocks first-account API registration. A successful bootstrap creates the
first administrator, even when later registration is invite-only or closed.
Bootstrap attempts use the registration rate limit, and the user check and
creation are serialized in PostgreSQL.

After the first account exists, the bootstrap token no longer grants access or
administrator privileges. All later registrations follow `AUTH_REGISTRATION`:
`open`, `invite` (the default), or `closed`. Invite registration requires the
server's `AUTH_INVITE_CODE`. Remove `AUTH_BOOTSTRAP_TOKEN` from the environment
after initialization and restart the service to apply the change. Bootstrap
tokens and passwords are never returned or logged.

Clients use these JSON endpoints:

- `GET /api/v1/auth/config` discovers registration availability.
- `POST /api/v1/auth/register` creates an account and a session.
- `POST /api/v1/auth/login` accepts a username or email in `login`.
- `GET /api/v1/auth/me` returns the authenticated account.
- `POST /api/v1/auth/logout` revokes the current session.
- `POST /api/v1/auth/logout-all` revokes all sessions for the account.
- `GET /api/v1/auth/sessions` and `DELETE /api/v1/auth/sessions/{id}` manage
  device sessions.
- `PATCH /api/v1/account/profile` updates the display name or email.
- `POST /api/v1/account/password` changes the password and revokes other
  sessions.
- `DELETE /api/v1/account` verifies the password and removes account data.

`GET /api/v1/auth/config` returns three boolean fields. An empty database
returns:

```json
{
  "registration_open": true,
  "requires_invite": false,
  "bootstrap_required": true
}
```

`bootstrap_required` becomes `false` after any user exists. Later registration
sets `registration_open` to `true` in open and invite modes, and sets
`requires_invite` to `true` only in invite mode. Closed mode sets both to
`false`. This endpoint never returns the bootstrap token or whether it has
been configured correctly.

The recommended initialization method is the administrator CLI from a shell
on the server. The current web, Flutter, and desktop clients have no
bootstrap-token input, so API initialization requires a separate API request.
From this directory, run:

```sh
python -m app.cli create-admin --username admin
```

Add `--email admin@example.com` and `--display-name "Administrator"` if needed.
By default, the command prompts for the password twice using hidden input;
the two entries must match. Only `--password-stdin` reads one line from stdin.
Passwords are not accepted as command-line arguments. The command requires
no bootstrap token because it runs with server shell access. It runs the same
database initialization and migrations as application startup before creating
the account. It refuses to create an account if any users already exist, unless
the operator explicitly adds `--force-additional-admin`. Existing users without
that flag produce exit code 2; a duplicate username or email produces exit code
3. Successful output contains only the username and role.

For Docker, start the service and then initialize the administrator:

```sh
docker compose up -d
docker compose exec luma python -m app.cli create-admin --username admin
```

To supply a password from a protected file through stdin, disable the Docker
TTY allocation:

```sh
docker compose exec -T luma python -m app.cli create-admin --username admin --password-stdin < /path/to/protected-password-file
```

Initialize before public access. Compose binds the service to `127.0.0.1` by
default; expose it through an HTTPS reverse proxy after initialization.

Login and registration set an HttpOnly cookie and return an `access_token` for
clients using bearer authentication. Sessions expire after seven days without
activity and have an absolute thirty-day lifetime by default. Administrators
manage `role` and `status` through `/api/admin/users`; disabled accounts cannot
keep active sessions. Authentication must remain enabled in deployed services.

## Provider and optional services

Configure an OpenAI-compatible model endpoint using `LLM_BASE_URL`,
`LLM_API_KEY`, `LLM_MODEL`, and `LLM_TIMEOUT_SECONDS`. Set the endpoint and model
for your chosen provider; `your-model-name` is a placeholder. Provider keys are
only read on the server. `LUMA_PROVIDER=local` explicitly selects deterministic
local behavior for development.

File storage supports the built-in backends `FILE_STORAGE=local` and `cos`.
The `fileservice` backend ships as the example plugin
`plugins_examples.fileservice` and is not enabled by default. See
`docs/extending.md`. Local
content lives under `ASSISTANT_FILE_ROOT`; remote backends store logical keys
and keep their credentials and URLs on the server. See `.env.example` for the
complete supported configuration.

Code execution and browser tools run in a Tencent Cloud Agent Runtime sandbox.
The host never executes model-supplied commands. Configure the runtime's E2B
domain, API key, and tool identifiers to enable these tools.

MCP connectors accept public HTTPS endpoints. Tokens are encrypted with
`LUMA_SECRETS_KEY` before storage. Tool results are untrusted data, never HTML
or executable instructions. Use `MCP_BLOCKED_HOSTS` and
`BROWSER_BLOCKED_HOSTS` to block your service's public hosts and addresses.
Voice and push integrations are optional and use separate server-side
credentials.

Request telemetry records timings and safe metadata only. It does not collect
request bodies, query parameters, passwords, or tokens.

## Verification

Run the backend suite as one process; tests use an isolated temporary schema
on the configured development PostgreSQL instance:

```sh
.venv/bin/python -m unittest discover -s tests
```

The root Dockerfile builds the web client and packages the backend as a
non-root service. See `deploy/README.md` for Compose setup and backup/restore
procedures. This repository does not configure or modify an existing hosted
service.
