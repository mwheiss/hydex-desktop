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

After building and launching that application, register this MCP server manually
in the Codex instance that should use it:

```toml
[mcp_servers.chatgpt_conversations]
command = "node"
args = ["/opt/hydex-desktop/.codex-linux/features/chatgpt-conversation-api/mcp.js"]
```

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
node linux-features/chatgpt-conversation-api/fixtures/demo.js
node linux-features/chatgpt-conversation-api/fixtures/demo.js --codex /opt/hydex-desktop/resources/codex
node linux-features/chatgpt-conversation-api/fixtures/audit-bundle.js /path/to/verified/official/app.asar
```

The demo uses synthetic records, a simulated backend and a test-only human
approval callback. It cannot access an account. Linux amd64/arm64 share the
JavaScript implementation; actual runtime and live-account verification must
be reported separately from fixture tests.
