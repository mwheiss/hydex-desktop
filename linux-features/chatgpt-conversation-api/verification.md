# Validation on 2026-11-06

| Check | Result | Scope |
|---|---|---|
| Adjacent feature tests and feature framework | 82 passed, 0 failed, 0 skipped | Synthetic backend, native renderer/main IPC functions, authenticated Unix socket, actual stdio MCP process, approval enforcement, journaling, recovery and undo |
| All Linux feature and patch-runner Node tests, `umask 022` | 1625 passed, 1 failed, 62 skipped | The failure is the existing read-aloud settings-path fixture at `linux-features/read-aloud/test.js:150`; its expected `codex-desktop` path conflicts with the current `hydex-desktop` default |
| Baseline reproduction in original unchanged checkout | Same six failures under inherited `umask 077` | Five broader-suite permission fixtures assume `umask 022`; the read-aloud path failure persists independently |
| Real Codex MCP client acceptance | Passed with Hydex CLI `0.166.0-alpha.6` | Ephemeral Codex config/thread, discovery of all 15 tools, complete synthetic inventory and retrieval, approval denial, approved moves/renames, backend verification and undo |
| Official ASAR compatibility | Passed | Freshly verified package `26.1103.31037`, both read-only and mutation-enabled settings; main/preload/renderer anchors, idempotence and syntax |
| Initial normal candidate build with this feature alone | Rejected by an existing required core patch | `quit-confirmation-focus`: `Expected exactly one official main-process Quit module`; this feature's descriptor applied successfully |
| Live account reads/mutations | Not performed | Requires a running feature-enabled accepted application; a live mutation additionally requires explicit separate authorization |

The ASAR compatibility audit does not disable core patches or claim that the
normal candidate build passed. The installer retained its signature, patch and
candidate-promotion gates and did not replace an installed application.

The real Codex acceptance run started **zero model turns**, made **zero real
account requests**, and used synthetic records plus a simulated human authority
outside the MCP tool surface. Production approval is supplied by actual native
desktop dialogs. Fixtures and the demo/audit helpers are excluded from the
native runtime resource payload and the minimal update-builder test payload.

Arm64, actual desktop launch with an authenticated account, and a single
explicitly authorized reversible live mutation remain unverified. No user's
existing Codex configuration, authentication files, installed application or
ChatGPT conversations were changed.

The subsequent full Hydex refresh replays the Desktop onto upstream
`5e0a33793a3924067953f9a0fee691a47a26a60c`, including the Quit repair. The
signed `26.1103.31037` ASAR inspection passes all five selected features:
29 descriptors applied and one was already applied. A narrowly matched
anonymous model-picker export repair preserves a stable React control identity
and passes 84 offload-feature tests. The release profile includes the API read
bridge with both mutation settings false. This inspection resolves the original
core-patch blocker; final native build/publication remains governed by the
separate release reports.

The refresh additionally validates native lifecycle interaction: cancelling
upstream's Quit prompt keeps the API socket and renderer bridge active; accepted
Quit drains the service in `will-quit` and removes its owned runtime records.
