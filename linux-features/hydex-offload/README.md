# Hydex Offload Control

This opt-in feature adds a compact `Hydex auto`, `Hydex on`, and `Hydex off`
selector next to the local model picker. It applies the selection to Hydex's
`modelOffloadOverride` app-server setting.

The control is attached only to the local app-server composer used by Codex
and local ChatGPT Work tasks. It is not added to Chat or cloud Work, which use
the separate ChatGPT model picker and transport.

## Requirements

This feature changes the desktop UI and app-server requests and replaces the
official Codex executable staged at `resources/codex` with a corresponding
Hydex release binary. This is the same integration shape used by the Hydex VS
Code patch: the host continues launching its ordinary bundled `codex` path,
but that path contains Hydex.

Set `HYDEX_CLI_BINARY` while building. The binary must be executable, statically
linked for the target architecture, expose `--offload` and `--no-offload`, and
report the exact same `codex-cli` version as the official desktop bundle. For
example:

```bash
HYDEX_CLI_BINARY=/absolute/path/to/hydex/codex make install-native
```

The latest official desktop and Hydex versions can advance independently. A
version mismatch rejects the candidate without replacing the installed app.

## Enable

Add the feature id to the ignored `linux-features/features.json` file:

```json
{
  "enabled": [
    "hydex-offload"
  ]
}
```

Then rebuild and install the app with the matching Hydex binary:

```bash
HYDEX_CLI_BINARY=/absolute/path/to/hydex/codex make install-native
```

## Behavior

The selector stores its desktop-wide preference in
`localStorage["hydex.offloadOverride"]`:

| Selection | App-server value | Behavior |
|---|---|---|
| `Hydex auto` | `null` | Clear the runtime override and follow Hydex config. |
| `Hydex on` | `"force_on"` | Force eligible inference through the local offload provider. |
| `Hydex off` | `"force_off"` | Force eligible inference through the primary provider. |

Auto removes the local-storage key but deliberately sends a JSON `null`.
Omitting the field would retain a thread's previous sticky override instead of
returning to configured behavior.

The request bridge injects the selection into `turn/start` so a new task's
first turn is routed correctly. It also injects it into
`thread/settings/update`. On an existing task, changing the selector calls the
desktop's normal next-turn settings path immediately.

The desktop-wide selector is authoritative for subsequent local app-server
requests. It does not attempt to infer or display which provider handled a
previous turn.

The selector opens an app-rendered popover using the same elevated surface,
selection-row, hover, and selected-state tokens as the model picker. Do not
replace it with a native HTML `select`: Chromium's native X11 popup can ignore
the webview's dark theme and render unreadable light-background options.

## Failure behavior

Hydex rejects `force_on` when no valid local offload provider is configured.
The desktop preserves that error; it does not silently fall back to the primary
provider.

A vanilla Codex app-server does not implement this contract. The build hook
therefore validates and injects Hydex before the candidate can be installed.

Native packages retain the validated Hydex executable in their update-builder
payload. Automatic rebuilds can reuse it while the official package still
bundles the same Codex version. If the official version advances first, the
update fails closed until the desktop package is rebuilt with a corresponding
Hydex binary.

Both the request bridge and composer control are enforced when this feature is
enabled. If either minified upstream contract drifts, candidate acceptance
rejects the build rather than installing a partial integration.

## Test

Run the focused tests with:

```bash
node --test linux-features/hydex-offload/test.js
```
