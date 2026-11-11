# ChatGPT Conversation API

An experimental, disabled-by-default native ChatGPT conversation management
feature. It manages ordinary ChatGPT conversations and native Projects through
the desktop's authenticated request service. Codex sessions and cloud Work tasks
are separate products and are excluded.

Enable `chatgpt-conversation-api` in an explicit feature configuration and
rebuild using the repository's signed Linux package process. Reads become
available when the feature is enabled. Backend mutations remain disabled unless
`settings.chatgpt-conversation-api.mutationsEnabled` is explicitly `true`.
Every mutation batch then needs an actual native desktop approval dialog.
The explicit Hydex native release profile includes this private read bridge;
both mutation settings remain false. Generic source builds still require
selection of the disabled-by-default feature, and Codex MCP registration is
always an explicit user step.
Shared or unknown-access Project moves additionally require
`sharedProjectMovesEnabled: true` and a distinct native access-authorization
dialog. No tool accepts a model's assertion that consent was granted.

```json
{
  "enabled": ["chatgpt-conversation-api"],
  "settings": {
    "chatgpt-conversation-api": {
      "mutationsEnabled": false,
      "sharedProjectMovesEnabled": false,
      "minDelayMs": 500,
      "maxOperations": 1000
    }
  }
}
```

The maximum batch size is 1000, minimum request spacing is 250 ms (500 ms by
default), and mutations execute serially. Rate limits can increase spacing.

After building and launching that application, register this MCP server in a
dedicated trusted project's `.codex/config.toml`. This keeps it undiscoverable
from other projects and excludes its schemas from the initial tool list:

```toml
[mcp_servers.chatgpt_conversations]
command = "node"
args = ["/opt/hydex-desktop/.codex-linux/features/chatgpt-conversation-api/mcp.js"]
omit_tools_from = ["direct"]
enabled_tools = [
  "chatgpt_get_status", "chatgpt_list_conversations",
  "chatgpt_search_conversations", "chatgpt_get_conversation",
  "chatgpt_get_conversation_metadata",
  "chatgpt_get_changes",
  "chatgpt_list_projects", "chatgpt_get_inventory_status",
  "chatgpt_get_job_status",
]
```

The installed Hydex runtime supports project configuration and deferred/Code
Mode discovery. With this read-only allow-list, mutation and preview tools are
absent from the project catalog. Models without tool search need Code Mode to
access tools omitted from `direct`. Do not put this registration in the global
config when project-only discovery is wanted. CLI chats can opt out with
`-c mcp_servers.chatgpt_conversations.enabled=false`; the current Desktop has no
separate per-chat switch for this custom stdio server.

For approved writes, explicitly enable `mutationsEnabled` in the local Desktop
build profile and add the preview/write/job tools to this project's allow-list
(or omit the allow-list to expose the current full catalog). The convenience
rename/move/archive/create tools only prepare plans. Execution and undo always
require fresh native approval for the exact stored batch. Do not replace that
approval with a secret argument or a model assertion. The release default stays
read-only; `sharedProjectMovesEnabled` is a separate opt-in.

For an unpackaged build, replace `/opt/hydex-desktop` with its app directory.
The MCP adapter requires Node.js 20+ on the Codex process's PATH. Native deb,
RPM and pacman packages declare `nodejs`; AppImage/Nix users must supply it.
This feature does not edit Codex configuration, install applications, start a
browser, copy authentication files, or register itself automatically.

The desktop creates a private Unix socket and capability discovery record in
`$XDG_RUNTIME_DIR/hydex-chatgpt-conversation-api/`. The directory is mode 0700,
the socket and discovery file are mode 0600, and the MCP adapter authenticates
with a random per-desktop capability. This is a local IPC capability, not a
ChatGPT session token. No HTTP or external network listener is created.
Processes already controlling the same Unix account are inside this local trust
boundary. Do not share the discovery record.

Plans and operation results persist under the desktop user-data directory's
`hydex-chatgpt-conversation-api/` subdirectory, with 0700/0600 permissions and
fsynced atomic updates. They contain titles, Project IDs, reasons and previous
states, but do not cache complete conversation bodies or session credentials.
Interrupted mutation intents are reconciled against backend state; uncertain
operations are never blindly repeated. Undo requires another native approval,
checks for concurrent changes, and restores only reversible verified changes.
Created Projects remain because this API has no permanent-deletion capability.

### Targeted metadata and optional shared raw cache

`chatgpt_get_conversation_metadata` reads one requested conversation through
the native read-only `POST /conversations/batch` contract with
`conversation_ids: [id]`. It does not scan history. The
result includes normalized metadata, unknown raw metadata fields, explicit
missing-record status and redaction provenance. Archived records remain
readable. Account/workspace identity is checked before and after the request.

Live verification on 2026-11-11 found that the backend also returns a nonempty
message mapping on this route, despite the signed app using it for metadata.
The service rejects such a payload with `UNSUPPORTED_FORMAT` and safe shape
diagnostics. A genuinely metadata-only network lookup is not currently verified.

An optional Python 3.9+ cache adapter ships in the same tracked feature runtime:

```toml
[mcp_servers.chatgpt_conversations]
command = "python3"
args = ["/opt/hydex-desktop/.codex-linux/features/chatgpt-conversation-api/cache/mcp.py"]
omit_tools_from = ["direct"]
```

Use `--config /absolute/path/api-cache-config.json` for a project-specific cache
directory/upstream configuration. Otherwise the adapter connects to the adjacent
native Node bridge and stores snapshots under
`$XDG_DATA_HOME/hydex-desktop/conversation-cache` (or `~/.local/share`). It never
registers globally or bypasses native write approval.

The default `prefer_cache` read serves clean snapshots validated within 60 seconds
immediately. Older clean snapshots are explicitly stale and trigger a coalesced
per-chat background refresh. Missing snapshots and known changes block on a fresh
read of that chat. `refresh` waits for current server data; `cache_only` starts no
refreshes; `inventory_snapshot` supports bulk imports. Every read verifies identity.

`chatgpt_get_changes` exposes an account-scoped, bounded local feed of IDs only.
The renderer hooks the native account-guarded history/turn-completion handler and
its missed-message recovery callback. The trusted main-frame IPC accepts only
`chat_changed` IDs or `gap` hints. Restarts, retention overflow and notification
gaps conservatively invalidate cached freshness. The adapter persists its cursor;
changes arriving during a body fetch remain dirty. No event can grant approval.

One detached worker per read-job kind owns an inherited OS file lock. Durable
queues coalesce work across clients; each worker uses its own native connection.
A shared metadata inventory is eligible every 30 minutes while the API is active,
independently of chat responses. Failures preserve old snapshots, expose retry
information and respect native cooldowns. Previews identify mutation targets;
execution/undo still require the native exact-batch dialog, and only affected
chats are invalidated when known. Unknown targets invalidate conservatively.

`cache_info` exposes age, dirty/stale state, refresh progress/failures and change
notification capability. Old Desktop versions work using age-based freshness
until the event bridge is installed/restarted. Normal cached reads can briefly
lag server changes; explicit `refresh` performs a blocking current read.

Native raw responses are saved before normalization. Compressed, checksummed
snapshots retain every exposed page, available branch, tool record, unknown
field and opaque field; earlier versions are kept. Credential redactions and
completeness limitations remain intact. This does not add attachment downloads
or decrypt reasoning. Storage is private (0700/0600), account/workspace scoped,
and retained across Desktop refreshes.

Oversized normalized views return bounded `RESPONSE_TOO_LARGE` results referring
to the retained raw snapshot. Read-only clients can explicitly derive an indexing
projection locally. The full copy is not truncated, and one large frame cannot
poison later reads. Unicode stays UTF-8; lone surrogates remain JSON escapes.

The cache launcher and code are declared feature resources under `runtime/cache/`,
so refresh staging copies them automatically. Tests live in `cache/tests/` and use
synthetic records. No live chat data, credentials or machine configuration is tracked.

Disabling the feature takes effect after rebuild/restart and removes its staged
resources. The journal is intentionally retained for audit/recovery. After all
jobs are resolved and the application has exited, the user may remove that
specific user-data subdirectory. Normal shutdown removes owned runtime socket
and discovery files; stale process identities are checked before reclamation.

See [architecture and verified limitations](architecture.md) and the
[agent session example](example.md), plus [validation results](verification.md).
Schemas and tool definitions live in
[`runtime/tools.js`](runtime/tools.js); the stable normalized representation is
defined in [`runtime/normalize.js`](runtime/normalize.js).

Run the feature tests with:

```bash
node --test linux-features/chatgpt-conversation-api/test.js
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider linux-features/chatgpt-conversation-api/cache/tests
node linux-features/chatgpt-conversation-api/fixtures/demo.js
node linux-features/chatgpt-conversation-api/fixtures/demo.js --codex /opt/hydex-desktop/resources/codex
node linux-features/chatgpt-conversation-api/fixtures/audit-bundle.js /path/to/verified/official/app.asar
```

The demo uses synthetic records, a simulated backend and a test-only human
approval callback. It cannot access an account. Linux amd64/arm64 share the
JavaScript implementation; actual runtime and live-account verification must
be reported separately from fixture tests.
