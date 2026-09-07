# Remote Trust Race Workaround

Tracked-default Linux workaround for the Codex Remote exact-path trust race described in
[openai/codex#39678](https://github.com/openai/codex/issues/39678) and the related generated-worktree
failure in [openai/codex#43239](https://github.com/openai/codex/issues/43239).

Remote can create a generated target and perform an exact-path trust preflight before that target has
been recorded in `~/.codex/config.toml`. The feature runs a small per-user inotify watcher and records
an exact trust entry only for narrowly validated Codex-generated paths.

## Trust boundary

The watcher never treats an arbitrary parent as recursively trusted and never overrides an explicit
`untrusted` decision.

- **Projectless chats:** the default root is `~/Documents/Codex`. The root itself must already have an
  explicit `trust_level = "trusted"` entry. Only new, user-owned, real-directory direct children whose
  names begin with a valid `YYYY-MM-DD` date prefix are eligible.
- **Managed worktrees:** the default root is `$CODEX_HOME/worktrees`. Only user-owned directories at
  exactly `<worktrees-root>/<generated-id>/<repo-name>` are eligible, and `<repo-name>` must match the
  basename of an existing, user-owned Git checkout that is explicitly trusted in Codex config.
- `config.toml` replacements are watched. Previously approved generated directories are reconciled
  immediately if Codex atomically replaces the config and drops an injected exact-path entry. Pre-existing
  lookalike directories are not trusted on startup.

The service uses Linux inotify directly through libc; this is **not** an inotify-limit workaround and
requires no `inotify-tools` package.

The runtime supports Python 3.6 and newer. Python 3.11+ uses the standard-library `tomllib`; older
interpreters use the bundled MIT-licensed Tomli 1.2.3 parser.

## Feature flag

Feature id: `remote-trust-race-workaround`.

It is a tracked default and requires `remote-mobile-control`. For an explicit feature configuration,
include it in `linux-features/features.json` to enable it, or omit it to opt out. COPR/native Hydex
builds include it by default.

Optional runtime path overrides are intentionally environment-only advanced controls:

```text
HYDEX_REMOTE_PROJECTLESS_ROOT=/absolute/path
HYDEX_REMOTE_WORKTREES_ROOT=/absolute/path
```

## Runtime

On Hydex launch the feature ensures the user service:

```text
hydex-remote-trust-race-workaround.service
```

The service remains active independently of the Electron process so it also covers the persistent
Remote app-server. Inspect it with:

```bash
systemctl --user status hydex-remote-trust-race-workaround.service
journalctl --user -u hydex-remote-trust-race-workaround.service
```

Remove only this generated service with:

```bash
python3 /opt/hydex-desktop/.codex-linux/features/remote-trust-race-workaround/manage.py remove
```
