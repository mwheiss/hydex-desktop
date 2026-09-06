# Persistent App-Server

A single companion feature for `remote-mobile-control`. It makes that feature's
existing local proxy mode the normal Desktop behavior and installs a persistent
systemd user server. It adds no Electron transport or ASAR patch beyond the
existing mobile feature. A packaged, bounded adapter lets the current VS Code
extension carry its JSONL stdio protocol over the service's Unix WebSocket.

```text
systemd --user
    +-- packaged codex app-server --remote-control --listen unix://
            +-- Desktop -> packaged JSONL/WebSocket Unix adapter
            +-- VS Code -> the same packaged adapter
            +-- terminal -> explicit attachment to the same socket
            +-- phone -> Remote Control
```

Required selection: `remote-mobile-control` and `persistent-app-server` enabled;
`shared-app-server-socket` disabled. Other unrelated features are preserved.

## Apply and install

This is one standalone patch against the normal checkout, not an incremental
patch on another proposed implementation. Use a checkout without earlier
experimental feature patches. Finish work and quit the old Desktop-owned server
before switching. Running tasks are not migrated between processes.

```fish
git apply --check ~/Downloads/persistent-app-server-clean.patch
git apply ~/Downloads/persistent-app-server-clean.patch
python3 linux-features/persistent-app-server/test.py
python3 linux-features/persistent-app-server/manage.py install
```

Run as your ordinary user, not with `sudo`. The final command:

1. Runs `check-mobile.cjs` against the actual source in this checkout. A failed
   source contract stops installation before feature selection or build changes.
2. Enables the two features above and disables the shared-server feature in
   `linux-features/features.json`, preserving other selections and settings.
   Backs up the first previous selection as `features.json.before-persistent-app-server`.
3. Runs the normal `make install-native`, inheriting build settings such as
   `HYDEX_CLI_BINARY`.
4. Verifies the packaged mobile launch-patch marker and CLI capabilities,
   installs the current user's service, enables lingering, and enables/starts
   `codex-remote-control.service`. It never restarts an already active service.

The feature and `remote-mobile-control` are tracked defaults for fresh native
builds. Creating a local `linux-features/features.json` makes the selection
explicit and can opt out. The source install command remains useful when you
want setup plus lingering in one step. Use `install --no-linger` for login-only
startup. Lingering enables boot startup and persistence after logout.

Native package installation never enrolls user accounts. The first Desktop,
packaged `codex`, or packaged adapter invocation performs idempotent setup for
that user before continuing. Automatic first-use setup is login-scoped and
never prompts for lingering; enable lingering separately when boot/logout
persistence is required:

```fish
loginctl enable-linger (id -un)
```

Existing user setup survives subsequent package upgrades. A later Desktop,
packaged `codex`, or adapter call starts an inactive service if needed, but
never restarts an active server.

On pacman, Debian, and RPM systems, selecting this feature also makes the
Desktop package the system Codex CLI provider. It owns `/usr/bin/codex` and
`/usr/bin/codex-code-mode-host` as links to its version-matched packaged
runtime, provides `codex` and `openai-codex`, and replaces/conflicts with the
native `hydex`/`hydex-bin` identities and supported `openai-codex` variants.
Installing the Desktop package therefore replaces the standalone Hydex CLI
package in the same transaction. It also conflicts with the virtual `codex`
provider and the explicit `codex-bin` package.

## VS Code attachment

The package installs a dedicated extension executable at:

```text
/opt/codex-desktop/.codex-linux/features/persistent-app-server/codex-vscode-proxy
```

The patched Hydex extension discovers this path automatically on Linux and
falls back to its bundled Hydex runtime when the Desktop package is absent. An
exact stale `hydex.cliExecutable` value for this adapter is treated the same as
unset. The corresponding official extension still requires an explicit
`chatgpt.cliExecutable` setting. The wrapper forwards normal CLI probes such as `--version`, converts
the current extension's exact stdio app-server launch into masked WebSocket text
frames on the private Unix socket, and fails closed if a future extension
changes that launch contract. Frames and handshake headers are hard-capped. It
never starts a second app-server.

Desktop supplies a per-launch `mcp_servers.codex_app` stdio definition for its
app tools and browser pipe. The wrapper parses that exact launch override,
materializes only the environment variables explicitly named by it, and merges
the complete transport into `thread/start`, `thread/resume`, and `thread/fork`
configuration. This preserves Desktop's dynamic MCP transport without adding it
to the global service configuration or exposing it to VS Code-only clients.

## Dependency audit: why shared-app-server-socket is not needed

Reviewed the public `hydex/main` sources on 2026-09-06, including implementation,
staging, startup ownership and existing tests, not just the feature description.
The conclusion is scoped to the local-proxy/Remote-Control server arrangement;
it does not claim that graphical computer-use tools work without a GUI session.

- `remote-mobile-control/feature.json` has its own `patch.js` and `stage.sh`
  entrypoints and no `requires` entry.
- `patch.js` defines `codexLinuxRemoteMobileLocalAppServerArgs` itself. With
  `CODEX_REMOTE_CONTROL_APP_SERVER_MODE=proxy`, it generates `app-server proxy`
  with the configured `--sock`; it does not instantiate the shared transport.
  `applyLinuxRemoteMobileAppServerRemoteControlPatch` rewrites an ordinary local
  stdio launch. Its descriptor runs in `extracted-app:post-webview`.
- `stage.sh` installs its own cold-start hook and checks its own launch-patch
  marker. It does not need shared-server staging resources.
- `cold-start-hook.sh` chooses an active/configured systemd unit before the
  Desktop/bundled fallback. Explicit autostart disablement also prevents fallback.
- The existing mobile `test.js` has a test named "Linux remote mobile app-server
  launch proxies Desktop RPCs to the declarative owner". It applies only the
  mobile transform to a normal launch fixture and checks the proxy command.
- By contrast, `shared-app-server-socket/patch.js` inserts
  `CodexLinuxSharedAppServerSocketTransport` into the transport factory when its
  bridge variable is set. That is a different, Desktop-owned transport path,
  not a prerequisite for mobile's stdio proxy path.

Source links:

- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/remote-mobile-control/feature.json
- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/remote-mobile-control/patch.js#L189-L294
- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/remote-mobile-control/stage.sh
- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/remote-mobile-control/cold-start-hook.sh#L54-L99
- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/remote-mobile-control/test.js#L1074-L1100
- https://github.com/mwheiss/codex-desktop-linux/blob/hydex/main/linux-features/shared-app-server-socket/patch.js#L53-L99

`check-mobile.cjs` makes this reproducible on the real checkout. It copies only
that checkout's mobile feature into a temporary directory, loads its actual
module, transforms an unpatched launch fixture, executes the registered launch
descriptor, runs its stage hook, and exercises its cold-start hook with stubbed
systemctl/Codex executables. No shared-feature directory is present. Nine checks
cover dependency declaration, proxy arguments, idempotence, platform handling,
staging and refusal to launch a competing fallback.

Run it independently:

```fish
node linux-features/persistent-app-server/check-mobile.cjs
```

This is a source contract test, not an Electron integration test or a sandbox
for untrusted source. It executes the checkout's trusted build scripts in a
throwaway tree, with service-manager operations stubbed.

## Runtime configuration and ownership

Initial setup saves the installation path, absolute `CODEX_HOME`, home directory
and PATH in `~/.config/codex-desktop/persistent-app-server.json` (0600).
`XDG_CONFIG_HOME` is respected. Reinstalling preserves the established settings.
The user unit is `~/.config/systemd/user/codex-remote-control.service`.

The launcher emits settings from that configuration:

```text
CODEX_HOME=<saved Codex home>
CODEX_CLI_PATH=<installation>/.codex-linux/features/persistent-app-server/codex-vscode-proxy
CODEX_REMOTE_CONTROL_APP_SERVER_MODE=proxy
CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET=<saved Codex home>/app-server-control/app-server-control.sock
CODEX_REMOTE_CONTROL_DAEMON_AUTOSTART_DISABLED=1
```

Missing configuration aborts launch rather than spawning a private server. The
last flag disables fallback daemon startup, not phone access. Local attachment
uses the Unix socket, not the internet relay.

Desktop still constructs its normal `app-server proxy --sock` command in proxy
mode, but `CODEX_CLI_PATH` routes that stdio child through the packaged adapter.
The adapter validates the exact configured socket and permits only configuration
overrides around that command before translating JSONL messages into Unix
WebSocket frames. Desktop's dynamic `codex_app` transport is carried as a
thread-scoped override; unrelated request configuration remains unchanged. It
never invokes the raw byte tunnel for a stdio client.

The service uses `Type=simple`; its Python helper execs the packaged CLI:

```text
/opt/codex-desktop/resources/codex -c features.code_mode_host=true app-server --remote-control --listen unix://
```

Systemd tracks the foreground server, not a detached starter. The packaged
resources directory is prepended to the saved PATH. When the existing
`hydex-offload` feature replaces the packaged CLI, this uses that executable.
No additional CLI is downloaded. No credentials are copied into this config.

Setup refuses an unrecognized existing unit, competing socket owner, or live
recorded Codex daemon/updater. It does not kill processes or delete sockets.
Resolve those conflicts explicitly after active work has finished. The current
mobile stage marker still says `owner=desktop`; it records the presence of the
launch transform, not the selected runtime ownership mode. Setup checks this
existing marker without changing its producer.

## Verification, upgrades and limits

```fish
systemctl --user status codex-remote-control.service
journalctl --user -u codex-remote-control.service -n 60 --no-pager
loginctl show-user (id -un) --property=Linger
```

Authentication and phone pairing are still required. For the default Codex home,
the compatible packaged CLI provides:

```fish
/opt/codex-desktop/resources/codex remote-control pair
/opt/codex-desktop/resources/codex --remote unix://
```

For a custom profile, supply `env CODEX_HOME=/your/path`. Manage this server via
`systemctl --user`, not Codex's separate managed-daemon start/stop commands.

Check a harmless running task from the phone after fully quitting Desktop,
recording the service MainPID before and after. Reopen Desktop and check the same
live thread and approvals. Then verify startup after reboot before opening the
UI. A reboot starts a new process; it does not checkpoint an executing task.

Package upgrades deliberately do not restart the running service. After active
tasks finish, load the upgraded binary with:

```fish
systemctl --user restart codex-remote-control.service
```

The patched Hydex VSIX discovers the packaged proxy as described above. This
feature does not change thread writer/approval rules, explicit cancellation,
account eligibility, suspend, server crashes, or GUI/keyring requirements.
Desktop-injected process-start settings cannot retroactively configure an
already running server. Native systemd installations are supported; AppImage
mounts and independently managed Nix services are not adopted by this helper.
The AppImage builder refuses an app containing this default feature; select the
explicit feature-free configuration before building AppImage.

## Remove

Native package removal stops active copies and scans normal local home
directories. It removes only marker-validated service/configuration files and
only an exact packaged-adapter value from strict-JSON Code, Code - OSS, or
VSCodium user/profile settings. JSON-with-comments files and foreign or unsafe
state are preserved with a warning. Codex data, credentials, unrelated editor
settings, and lingering are preserved.

For a source/manual installation, remove the current user's service explicitly:

```fish
python3 linux-features/persistent-app-server/manage.py remove
```

Then create an explicit local feature selection without
`persistent-app-server` and rebuild normally.

## Validation boundary

The accompanying report separates unit tests, source-excerpt checks and untested
integration. The generation runtime could inspect public source through web
retrieval, but could not clone/download the full repository. The nine contract
checks were executed here against manually transferred launch-function excerpts
and the retrieved hooks. On install they run against the actual complete mobile
feature from your checkout. Do not mistake the excerpt execution for a full
repository test. No native build, live user systemd service, real Codex/Electron
connection, reboot or authenticated phone pairing was exercised by the original
attached validation. Perform those checks on the target system before relying on it.
