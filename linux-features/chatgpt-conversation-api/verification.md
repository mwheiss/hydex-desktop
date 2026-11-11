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

The 2026-11-08 session repair adds regression coverage for distinct membership
and authenticated-request user IDs, in-page navigation preserving registration,
document navigation retiring it, and repeated reloads retaining a single set of
lifecycle observers. Signed-in OpenAI providers retain `requiresAuth: true`;
this provider requirement is not an unauthenticated-session flag. Regression
tests keep authentication loading, token availability and native identity guards
independent from that requirement. A patched-factory execution regression also
covers the native request loader being shadowed by a local account snapshot,
plus partial-capture and binding-collision rejection. The adjacent suite passes
all 51 tests, including strict native page-limit and Work-origin regressions,
foreign-resource rejection in raw/normalized reads, and short-page history
offset handling. This source result does not by itself establish installed
live-account access.

## Live native read validation on 2026-11-09

An installed amd64 build from source
`6c2e370bea2a329de231a0227ad91f2da53c0168`, using signed official package
`26.1103.60933` and the matching Hydex CLI `0.167.0-alpha.3.2`, passed real
account reads through the Codex MCP client. Verification covered authenticated
context, Project listing, history/archive/Project inventory, and complete raw
and normalized retrieval of an actual conversation. The verifier started zero
model turns, executed no conversation mutations, and saved counts and hashes
rather than transcript bodies.

The scan exhausted its page chains. Account-wide completeness stayed `unknown`
because backend totals changed during scanning and the Project directory has
no authoritative total. This result is not a claim of immutable full-account
coverage.

The MCP catalog was available only from the dedicated trusted project's config;
unrelated workspaces did not discover it. The explicit local build enabled
mutations behind native approval for each exact execution/undo batch. Shared
Project moves remained disabled. Native approval enforcement is covered by the
synthetic tests; no live backend write was exercised. Arm64 live-account
validation also remains unperformed. Earlier read-not-performed notes above
describe the initial validation and are superseded by this read result.
# 2026-11-11 targeted metadata and durable cache replay

The tracked feature now exposes `chatgpt_get_conversation_metadata` through the
native read-only `POST /conversations/batch` contract with exactly one ID.
This route was identified in the signed application's own metadata-query and
thread-catalog callers. The operation is account/workspace bound and does not
perform an inventory or body read.

| Check | Result | Scope |
| --- | --- | --- |
| Adjacent native API fixtures | 56 passed | Includes one-ID renderer POST, missing/archived records, credential redaction, malformed/unrelated responses, foreign-origin exclusion and unchanged native approval |
| Optional cache regression suite | 31 passed | Persistent raw versions, targeted validation/coalescing, safe old-bridge fallback, bounded frames, Unicode and explicit indexing projection |
| Feature resource staging | Passed | Native tools and optional Python cache launcher stage together with 0755 directories/0644 files; tests are not installed; disabling removes managed resources |
| Official package verification | Passed | Signed stable `chatgpt` `26.1106.51428` amd64, SHA-256 `8356a7821930b135b741347faccbc43e1692b4424c3eab710a9af56c0bebb135`; no verification skip |
| Official-bundle build, this feature alone | Passed | Native semantic anchors, including the batch metadata contract, applied to the verified stable bundle; existing installed Desktop was not replaced |

The new metadata operation requires a Desktop built with this feature revision
and a restart. A live targeted read against the user's running older Desktop
has not been claimed: that runtime does not expose this operation. The cache
adapter detects the missing capability and refreshes only the requested body,
never the entire account. Native get/preview/execute/undo authorization remains
inside the original service and approval executor.

The cache code and launcher live under `runtime/cache/`, inside the existing
declarative resource root. Future refresh builds therefore carry the code
without editing installed/generated files. Machine configuration and retained
chat data are not part of the source tree or validation fixtures.
