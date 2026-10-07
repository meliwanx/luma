# On-demand MCP tools and skills

Luma uses MCP Streamable HTTP for tool transport and keeps credentials encrypted
in its existing owner-bound connector store. Search, skill manifests and active
schema budgets are Luma application behavior. MCP does not automatically choose
relevant tools or load skills for an assistant.

The default external-tool surface is `luma.capabilities.search`,
`luma.capabilities.load` and `luma.capabilities.release`. The existing small
built-in task, file, connector and memory tools remain available. A conversation
does not receive every connector's schemas or instructions at startup.

## Discovery and loading

1. Search the current intent. Results contain capability IDs, short summaries
   and revisions, without schemas, endpoints or skill instructions.
2. Load a small selection. A skill also loads its tool dependencies. The next
   model round receives the selected function schemas and bounded skill text.
3. Execute through the existing policy, encrypted credential binding and visible
   tool events. Remote results and connector instructions are untrusted data.
4. Release the selection after completing work. Pending confirmations retain
   only owner/session-bound IDs and revisions, allowing a continuation to reload
   the same process without storing its complete text or raw results.

Each selection is limited to eight external tools, two skills and 32,000
serialized characters. A trusted skill is limited to 8,000 characters. Loading
also applies `CAPABILITY_TOKEN_BUDGET` (default 6,000 estimated tokens). Remote
results passed to the model have a 12,000-character per-result and 24,000-character
per-turn budget; omitted content is explicitly marked, with complete JSON rows
preserved where possible. Superseded selection text is removed from subsequent
provider rounds. Discovery scans synchronized metadata, not live endpoints; the
synchronized catalog may
contain up to 2,048 tools across at most 32 pages per connector. A repeated cursor
or oversized catalog is rejected rather than silently truncating capabilities.

Loading and execution recheck connector ownership, enabled state and schema
revision. Revisions include server metadata, endpoint and credential rotation.
Live `tools/list` is checked before each invocation; drift
requires explicit connector synchronization and loading again. The system does
not automatically replay a write after a timeout or schema change. Arguments
are validated locally against the selected JSON Schema. External schema
references are rejected, so validation cannot fetch arbitrary URLs.

## Trusted deployment skills

Set `LUMA_SKILL_PATHS` to local JSON files or directories separated by the
platform path separator. These files are administrator-owned deployment inputs,
not files a model can choose. Keep business-specific manifests and all real
connection settings outside the public repository.

```json
{
  "id": "record-investigation",
  "title": "Investigate a record",
  "summary": "Read evidence and explain the applicable rule",
  "keywords": ["record", "diagnosis"],
  "instructions": "Read the relevant record and rule. Report evidence and unknowns separately. Do not invent a cause.",
  "tools": [{"connector_name": "Example", "tool": "query_record"}],
  "allowed_users": ["example-user"]
}
```

`allowed_users` is optional; when present, only those owners can discover the
skill. Every referenced tool must belong to an enabled connector owned by the
current user. A missing dependency or ambiguous connector name hides the skill.
An exact `capability_id` can replace the name/tool pair. User/tenant scopes and
backend actor credentials must additionally be enforced by enterprise services;
tool arguments must never be accepted as proof of identity or authorization.
Selected manifest revisions are checked on subsequent rounds. A changed rule
or audience requires release and explicit loading again; partial release removes
that skill's text while retaining the remaining selected processes.

## Business writes and evidence

Loaded MCP writes require the actual product confirmation flow, even if a
connector's old permission setting allowed automatic writes. Confirmation is
bound to capability revision and arguments. Business adapters should first
provide a server-owned preview, enforce idempotency, and return the actual
receipt/status from the target system. A model-supplied `confirmed: true` is
not an authorization credential. An approval broker may require a separate
SSO-authenticated user action before an adapter accepts submission.

Transport calls retain existing connection and tool timeouts (15 and 30 seconds),
bounded response bodies and the agent's call/round limits. Enterprise services
must enforce their own distributed actor/tenant rate limits. Connector event and
notification integration is documented in [connector-events.md](connector-events.md).

The capability state is a runtime execution hint, not long-term memory. Memory
implementations may consume only a deliberately selected, nonsensitive final
summary through their existing interface; credentials, raw tool output, preview
reasons and approval tokens do not belong in capability state.
Set `exclude_from_memory: true` on a sensitive workflow skill to suppress the
current assistant turn's automatic memory extraction, including after release.
The same marker excludes its sensitive conversation window from automatic
summary generation and blocks unrelated memory/notification writes in that turn.
An enterprise preview should show sensitive fields only to its authenticated
owner in the target workflow UI; tool results, audit and notifications should
contain opaque preview/process IDs and nonsensitive status summaries.

Protocol references: [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[MCP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
and the [official Python SDK](https://github.com/modelcontextprotocol/python-sdk).
