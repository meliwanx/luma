<p align="center">
  <img src="docs/assets/luma-logo.svg" width="200" height="200" alt="Luma logo" />
</p>

<h1 align="center">Luma</h1>

[简体中文](README.zh-CN.md)

**License: free personal noncommercial use; commercial use requires prior written authorization from [meliwanx](https://github.com/meliwanx).** This is a source-available project. See [License](#license).

Luma is a self-hosted AI assistant with several clients. You talk to it from the web, a desktop app (macOS / Windows) or an iPhone. It plans with an LLM, calls tools, and runs code or drives a browser inside a per-user cloud sandbox, then returns only the final result.

All reasoning and execution happen on your server and in the sandbox. Every client shows the same account, so switching devices or closing a client does not interrupt a running task.

> Status: early source-available release, published as source code only. There are no prebuilt Docker images, desktop installers or app-store builds; you build everything from this repository. The server and the web and desktop clients are usable. The iOS client builds from source.

## Looking for a self-hosted Muse alternative?

If you are exploring **Meta Muse alternatives**, Luma offers a **self-hosted personal AI agent** with persistent conversations, memory, goals and background tasks. Run it on your own server, choose an OpenAI-compatible model provider, and access the same assistant from the web, macOS / Windows or iOS.

Connect your tools through **MCP**, and optionally enable a per-user **cloud sandbox** for **browser automation**, Python / shell **code execution**, files and long-running jobs. Start with [Quick start](#quick-start-docker) and [Cloud sandbox](#cloud-sandbox).

Luma is an independent project with no affiliation to Meta Muse. In this release, desktop apps provide access to the server; code and browser actions run in the configured cloud sandbox. Control of your own Mac or Windows desktop is outside the current release. Luma is **source-available**: personal noncommercial use is free, and commercial use requires [prior written authorization](#license).

## What it does

- **One main chat plus topic side chats.** A long-lived main conversation sits alongside optional side chats, one per topic, with full-text search across all of them. Replies keep generating when you switch chats or disconnect, and the event stream resumes when you return.
- **Agent loop with tools.** Each request has a cap on rounds and tool calls. Tools run in parallel, and large results are truncated without breaking their structure. When the budget runs out, the assistant is forced to give a final answer. Tool rounds stay silent, so you see one clean answer.
- **Cloud sandbox (optional).** Integrates Tencent Cloud Agent Runtime through its E2B-compatible API:
  - per-user code execution (Python and shell);
  - long-running background jobs;
  - file import and export;
  - port previews;
  - a Chromium browser controlled over CDP, with a live view.

  Idle sandboxes are paused instead of destroyed, and workspaces are backed up and restored.
- **MCP connectors.** Add remote MCP servers from settings or by pasting a config in chat. Only HTTPS servers are allowed, and requests are guarded against SSRF. Tokens are stored encrypted (Fernet) and never shown to the model. Usage instructions provided by a server are fetched only when needed.
- **Permissions.**
  - Read-only and sandbox actions run directly.
  - External writes ask for confirmation unless you have allowed them.
  - Delete, bulk and payment-like actions always ask.
- **Memory, tasks, goals, routines.** Editable long-term memory, task tracking and scheduled routines.
- **Ideas, feed and proactive messages.**
  - Daily personalised suggestions of things the assistant can do for you.
  - A topic feed whose source links are checked against pages the assistant actually opened.
  - A small number of proactive check-ins per day, sent only inside a time window you control.
- **Library.** Files you upload and files the assistant produces (exports, screenshots), with previews and links back to the chat they came from.
- **Voice input (optional).** Speech is transcribed to text, then an LLM cleans it up: it removes filler words and restores lists and punctuation.
- **Usage analytics.** Token counts, time to first token and latency for every model call. Each user sees their own figures; admins see aggregates.
- **Accounts.** Built-in registration, login, password change, session management and account deletion. See [Account security](#account-security).

## Architecture

```
 Web (React) ─┐
 Desktop      ├──►  FastAPI  ──►  LLM (OpenAI-compatible)
 (Electron)   │      │  │   ──►  Tencent Agent Runtime sandbox (code, browser)
 iOS (Flutter)┘      │  │   ──►  MCP servers (HTTPS)
                     │  └──►  PostgreSQL (data, migrations)
                     └─────►  Redis (sessions, event streams, rate limits)
```

| Path | Contents |
| --- | --- |
| `backend/` | FastAPI app, agent loop, tools, Alembic migrations, tests |
| `apps/web/` | React + Vite web client (also served by the backend) |
| `apps/desktop/` | Electron shell around the web client, with tray and global shortcut |
| `apps/flutter/` | Flutter client (iOS-focused) |
| `docs/` | Architecture notes |
| `scripts/` | Backup, restore and verification helpers |

## Quick start (Docker)

You need Docker Engine with the Compose plugin.

```bash
git clone https://github.com/meliwanx/luma.git
cd luma
cp .env.example .env
chmod 600 .env
# Edit .env: set DB_PASSWORD, REDIS_PASSWORD, AUTH_SESSION_SECRET,
# LUMA_SECRETS_KEY and the LLM_* values. The file includes commands to generate the secrets.

docker compose up -d --build --wait   # builds the image from this source tree
docker compose exec luma python -m app.cli create-admin --username admin
```

Then open <http://127.0.0.1:8000> and sign in as `admin`.

By default the service listens on `127.0.0.1` only. Create the first administrator **before** exposing the port. To serve Luma publicly, put a TLS reverse proxy in front of it and set `LUMA_BIND_HOST`, `AUTH_COOKIE_SECURE=true` and `CORS_ORIGINS` to match.

To try Luma without a model provider, set `LUMA_PROVIDER=local`. Replies will be canned, but the rest of the interface works.

## Configuration

All configuration is done through environment variables in `.env`. The full annotated list is in [`.env.example`](.env.example). These are the ones you will set first:

| Variable | Purpose |
| --- | --- |
| `DB_PASSWORD`, `REDIS_PASSWORD` | Credentials for the bundled PostgreSQL and Redis |
| `AUTH_SESSION_SECRET` | Secret used to sign sessions |
| `LUMA_SECRETS_KEY` | Fernet key that encrypts connector tokens. Back it up: if you lose it, stored tokens cannot be recovered |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | Any OpenAI-compatible chat completions endpoint |
| `AUTH_REGISTRATION` | Sign-up mode for accounts after the first: `invite` (default), `open` or `closed` |
| `AUTH_INVITE_CODE` | Code users must enter to sign up in `invite` mode |
| `AUTH_BOOTSTRAP_TOKEN` | Optional. Lets you create the first admin through the API instead of the CLI |
| `LUMA_BIND_HOST`, `LUMA_PORT` | Address and port the service is published on (default `127.0.0.1:8000`) |
| `AGENT_RUNTIME_ENABLED`, `E2B_DOMAIN`, `E2B_API_KEY`, `AGENT_RUNTIME_*_TOOL` | Tencent Cloud Agent Runtime sandbox (optional) |
| `FILE_STORAGE` | `local` (default, stored in a Docker volume) or `cos`. `fileservice` is an optional plugin; see [docs/extending.md](docs/extending.md) |
| `MCP_BLOCKED_HOSTS`, `BROWSER_BLOCKED_HOSTS` | Put your server's own public address here so tools cannot call back into it |

### Cloud sandbox

Code execution and browsing stay disabled until you configure a sandbox. To use Tencent Cloud Agent Runtime:

1. In the console, create sandbox tools: either a Code Interpreter and a Browser, or a single All-In-One tool.
2. Create an API key.
3. Set these variables:

```
AGENT_RUNTIME_ENABLED=true
AGENT_RUNTIME_API_MODE=e2b
E2B_DOMAIN=your-region.sandbox.example   # data-plane domain from your sandbox provider
E2B_API_KEY=...
AGENT_RUNTIME_AIO_TOOL=...              # or AGENT_RUNTIME_CODE_TOOL / AGENT_RUNTIME_BROWSER_TOOL
```

Region, tool names and `SANDBOX_PREVIEW_HOST_SUFFIX` have no built-in default.
Leave them empty while the sandbox is disabled. When it is enabled, a missing
value is reported by name.

Choose a region whose network can reach the sites your users need. Commands from users or the model never run on the Luma host itself. Without a sandbox, the code and browser tools are simply unavailable.

## Account security

- Passwords are hashed with Argon2id, with scrypt as a fallback. They must be 8–128 characters, and common passwords are rejected.
- Login errors do not reveal whether an account exists. Failed logins are rate-limited per account and per IP.
- Sessions are stored server-side in Redis. Browsers get HttpOnly cookies; apps get bearer tokens. Sessions expire after 7 days without use and after 30 days at most. Changing your password signs out your other devices.
- **First administrator.** Create it from a shell on the server with `python -m app.cli create-admin`. Alternatively, set `AUTH_BOOTSTRAP_TOKEN` (at least 16 characters) and pass it as `bootstrap_token` to `POST /api/v1/auth/register` while the database is still empty. The bundled clients do not have a field for this token yet. Without the CLI or a valid token, nobody can claim the first account. Remove the token once setup is done.
- Every query is scoped to the signed-in user. Admins can promote, demote and disable users, but cannot demote or disable themselves.

## Operations

- **Migrations** run automatically at startup, under a PostgreSQL advisory lock.
- **Health:** `GET /health` reports whether the database and Redis are reachable. The container also has a health check.
- **Backup and restore:** `scripts/backup.sh` writes a timestamped `pg_dump` and an archive of the file volume. `scripts/restore.sh` restores both.
- **Images:** no prebuilt image is published. `docker compose build` builds a local image named `luma:local` from the `Dockerfile`. To run it elsewhere, tag and push it to a registry you control. `.github/workflows/ci.yml` only runs the backend tests and the web build.

## Clients

- **Web:** served by the backend at `/app`. For development, run `cd apps/web && npm install && npm run dev`.
- **Desktop:** run `cd apps/desktop && npm install && npm start`. Point it at your server with `LUMA_SERVER_URL` or `config.local.json`. Packaging and code signing are covered in [apps/desktop/README.md](apps/desktop/README.md).
- **iOS:** see [apps/flutter/README.md](apps/flutter/README.md). Set `API_BASE_URL` at build time, and use your own bundle identifier and team.

## Limitations

- The sandbox integration targets Tencent Cloud Agent Runtime's E2B-compatible API. Other providers have not been tested.
- Runtime snapshots and persistent volumes are not used, because the provider did not support them in testing. Persistence relies on pausing and resuming sandboxes, plus workspace backups.
- Push notifications require your own APNs or FCM credentials.
- The interface text is mostly in Chinese.
- Luma cannot operate the user's own computer. All actions run in the cloud sandbox.
- The model client speaks the OpenAI chat completions protocol, including streaming and tool calls, and is covered by tests against a mock server. A smoke test of a Docker deployment built from source confirmed that the `LLM_*` settings are picked up and that `LUMA_PROVIDER=local` works without a provider. It did not run real inference against any particular vendor, so verify your provider's compatibility yourself.

## Development

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# Tests need PostgreSQL and Redis; see .github/workflows/ci.yml for how the CI services are set up.
.venv/bin/python -m unittest discover -s tests
```

## Contributing

Issues and pull requests are welcome. Please include tests for behaviour changes and keep secrets out of commits. Before submitting, run the backend tests and `npm run build` in `apps/web`.

Contributors retain their copyrights. Before merging an external contribution, the maintainer must obtain explicit permission to distribute it under the project license and, where needed, grant separate commercial licenses. Submission alone is not a copyright transfer or an automatic commercial-relicensing grant.

## License

[Luma Personal Noncommercial License 1.0](LICENSE) · [中文协议](LICENSE.zh-CN.md)

- **Free:** an individual's own personal noncommercial self-hosting, learning, modification and sharing without charge, with the license and copyright notices retained.
- **Prior written authorization required:** company/internal business use, work for an employer or client, paid professional work, commercial product integration, hosted/SaaS/API services for a commercial purpose, and paid implementation or support. Not charging end users does not by itself make a use noncommercial.
- **Commercial requests:** contact [meliwanx](https://github.com/meliwanx) through [a licensing issue](https://github.com/meliwanx/luma/issues/new). A request or silence is not authorization; the written agreement sets scope, fees and responsibility terms.

This commercial-use restriction means the project is **source-available**, not [OSI open source](https://opensource.org/osd). Third-party dependencies keep their own licenses.

Earlier MIT-licensed releases remain usable under their original terms; this change does not retroactively revoke those permissions. The historical license and applicable commits are preserved in [licenses/LEGACY-MIT.txt](licenses/LEGACY-MIT.txt). The new license applies to versions and contributions released with it, subject to independently granted rights.
