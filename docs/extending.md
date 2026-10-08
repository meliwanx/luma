# Extending Luma

Private deployments can add storage backends, HTTP routes, MCP connector
templates and process hooks without editing the core tree. Keep that code in a
directory outside this repository and mount it at startup.

## Plugins

Two environment variables control loading:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LUMA_PLUGINS` | empty | Comma-separated Python module names, imported in order |
| `LUMA_PLUGIN_PATH` | empty | Comma-separated directories inserted at the front of `sys.path` |

An empty `LUMA_PLUGINS` loads nothing. If any named module fails to import, or
one of its hooks raises, the process logs the module name and the exception
type, then exits. The log line does not include the exception message. A
deployment that is missing a private feature stops instead of serving a
partial process.

Each module may define any of these callables. Missing ones are skipped.

| Hook | When it runs |
| --- | --- |
| `register_storage(registry)` | Import, before requests are served |
| `register_routes(app)` | Import, before requests are served |
| `register_mcp_presets(registry)` | Import, before requests are served |
| `on_startup()` | Application lifespan, after the database is ready. May be async |
| `on_shutdown()` | Lifespan shutdown, in reverse plugin order. May be async |

`register_routes` may only add routes under `/api/`. A route whose path and
HTTP method already exist is rejected and removed before startup fails. Hooks
must not replace a built-in storage name (`local` or `cos`).

`register_mcp_presets` accepts a name and an `https` URL with no user info, query
string or fragment. Presets are templates for operators. They do not carry
tokens. Read them from `app.plugins.mcp_presets()`.

### Private directory

```text
/opt/luma-private/
  private_ext/
    __init__.py
    deploy.py
```

```sh
LUMA_PLUGIN_PATH=/opt/luma-private
LUMA_PLUGINS=private_ext.deploy
```

`deploy.py` can register a storage backend, routes and presets. Do not copy
that package into this repository.

```python
def register_storage(registry):
    registry.register("archive", ArchiveStorage.from_env, config_keys=("ARCHIVE_URL",))

def register_routes(app):
    @app.get("/api/v1/private/status")
    def status():
        return {"ok": True}

def register_mcp_presets(registry):
    registry.register("docs", "https://mcp.example/mcp")

def on_startup():
    return None

def on_shutdown():
    return None
```

The example plugin `plugins_examples.fileservice` is in this repository so the
contract is executable. It is not enabled unless `LUMA_PLUGINS` names
`plugins_examples.fileservice`.

## Storage

`FILE_STORAGE` selects the backend for new uploads. The default is `local`.
Core registers only `local` and `cos`. A file row keeps the `storage` name it
was written with. Reading a row whose name is not registered raises an error
that includes that name.

| Backend | Where it comes from | Required settings |
| --- | --- | --- |
| `local` | built in | `ASSISTANT_FILE_ROOT` (optional; defaults to `backend/data/files`) |
| `cos` | built in | `COS_SECRET_ID`, `COS_SECRET_KEY`, `COS_BUCKET`, `COS_REGION` |
| `fileservice` | example plugin | `FILE_SERVICE_URL`, `FILE_SERVICE_APP_KEY`, `FILE_SERVICE_APP_SECRET` |

`COS_REGION` has no default. Selecting `cos` without it fails with the missing
variable name. `COS_PREFIX` remains optional.

To turn on the example file service:

```sh
LUMA_PLUGINS=plugins_examples.fileservice
FILE_STORAGE=fileservice
```

Register additional backends with `registry.register(name, factory, config_keys=...)`.
`factory` is called with no arguments and should read its own environment.
`config_keys` lists the variables that must invalidate the process cache when
they change. Names are lowercase letters, digits, `_` and `-`, up to 32
characters, and must start with a letter.

## Sandbox and speech defaults

Sandbox region, tool names and the data-plane host suffix are empty unless the
deployment sets them. Sandbox and browser tools are not registered until a tool
name, or `AGENT_RUNTIME_AIO_TOOL`, is set, so the model does not see them.
Status `reason` is `未配置` while the adapter is off, and `未配置：` plus the
variable names when it is on but incomplete. A call that still reaches the
adapter raises `AgentRuntimeUnavailable` and names the missing variable.

| Variable | Default | Used when |
| --- | --- | --- |
| `AGENT_RUNTIME_REGION` | empty | cloud API mode |
| `AGENT_RUNTIME_CODE_TOOL` | empty | code sandbox, unless `AGENT_RUNTIME_AIO_TOOL` is set |
| `AGENT_RUNTIME_BROWSER_TOOL` | empty | browser sandbox, unless `AGENT_RUNTIME_AIO_TOOL` is set |
| `SANDBOX_PREVIEW_HOST_SUFFIX` | empty | preview and browser data-plane checks; `E2B_DOMAIN` is used when this is empty |
| `BAILIAN_ASR_BASE_URL` | empty | speech recognition, required together with the model when an API key is set |
| `BAILIAN_ASR_MODEL` | empty | speech recognition model name |

`MCP_BLOCKED_HOSTS` and `BROWSER_BLOCKED_HOSTS` also default to empty. Set them
to this server's own public addresses so tools cannot call back into it.
Separate hosts with commas.

Chat completions use `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`. The built-in
placeholders are not a provider endpoint. `LUMA_PROVIDER=local` skips the
remote model. See `.env.example`.

## Brand

Product names are configuration, not string literals in private plugins. The
brand loader (implemented with the brand change) reads environment variables
first, then the JSON file named by `BRAND_CONFIG` when that variable is set.
Environment variables win over the file. Defaults:

| Variable | Default |
| --- | --- |
| `BRAND_PRODUCT_NAME` | `Luma` |
| `BRAND_ASSISTANT_NAME` | the product name |
| `BRAND_TAGLINE` | `你的个人 AI 助理` |
| `BRAND_COMPANY_NAME` | empty |
| `BRAND_SUPPORT_URL` | empty |
| `BRAND_LOGO_URL` | `/brand/logo.svg` |
| `BRAND_PRIMARY_COLOR` | `#2563EB` |
| `BRAND_CONFIG` | empty |
| `BRAND_ASSETS_DIR` | empty; logo and favicon fall back to the built-in assets |

`GET /api/v1/brand` returns only those public fields. Private plugins should
read the same values instead of embedding a product name.

Web builds use `VITE_BRAND_PRODUCT_NAME`, `VITE_BRAND_TAGLINE` and
`VITE_BRAND_PRIMARY_COLOR`. Flutter builds use `BRAND_PRODUCT_NAME`,
`BRAND_TAGLINE` and `BRAND_PRIMARY_COLOR`. iOS display name and bundle id use
`BRAND_DISPLAY_NAME` (default `Luma`) and the bundle id configured for that
build. Desktop packaging reads `BRAND_FILE` (default `brand.json`).

## Accounts

`AUTH_PROVIDERS` chooses how people sign in. It is a comma-separated list.
The default is `password`. `sso` is optional and can be combined with
`password`, for example `sso,password`. Session cookies and bearer tokens stay
in the shared account layer.

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_PROVIDERS` | `password` | `password`, `sso`, or both |
| `AUTH_SSO_LABEL` | `单点登录` | Button label on the login screen |
| `AUTH_ACCOUNT_LABEL` | `账号` | Label for the account field |
| `AUTH_PASSWORD_LOGIN_ENABLED` | `true` | Account-password channel on the SSO provider |
| `SSO_BASE_URL` | empty | Identity service origin. Required when `sso` is enabled |
| `SSO_VERIFY_URL` | `{SSO_BASE_URL}/api/sso/verify` | Ticket verification URL |
| `SSO_FEDERATED_LOGIN_URL` | `{SSO_BASE_URL}/api/sso/federated-login` | Browser redirect URL |
| `SSO_APP_BASE_URL` | empty | Public origin of this app, used to build the callback |
| `SSO_CALLBACK_URL` | empty | Override the callback. Otherwise derived from `SSO_APP_BASE_URL` |
| `SSO_SYSTEM_CODE` | `assistant` | Sent as `X-System-Code` |
| `SSO_API_SECRET` | empty | HMAC secret. Required when `sso` is enabled. Not returned by the API |
| `SSO_SYSTEM_SECRET` | empty | Alias of `SSO_API_SECRET` |
| `SSO_VERIFY_TIMEOUT_SECONDS` | `8` | Verify timeout, clamped to 1–30 seconds |
| `SSO_TIMEOUT_SECONDS` | empty | Alias used only when `SSO_VERIFY_TIMEOUT_SECONDS` is empty |

`GET /api/v1/auth/providers` describes the enabled providers for the login
screen. It does not return secrets. Password registration continues to use
`AUTH_REGISTRATION`, `AUTH_INVITE_CODE` and `AUTH_BOOTSTRAP_TOKEN`.

## Client config

`GET /api/v1/client-config` is public. It returns `browser_live_host_suffixes`,
the host suffixes the web and Flutter clients may open for a browser live view.
`BROWSER_LIVE_HOST_SUFFIXES` is a comma-separated list. Leave it empty to keep
the default `[".tencentags.com"]`, which is Tencent Cloud's public data-plane
suffix. A failed request on the client restores that same default. The value
is not a credential. Set it to the same suffix as `SANDBOX_PREVIEW_HOST_SUFFIX`
when the deployment uses another data-plane host.
