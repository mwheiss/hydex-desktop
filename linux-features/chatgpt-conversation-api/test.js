"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { EventEmitter } = require("node:events");
const { spawn } = require("node:child_process");
const { ConversationService } = require("./runtime/service.js");
const { ApiError, atomicJson } = require("./runtime/common.js");
const { normalize, retrieve, conversationState, projectRecord } = require("./runtime/normalize.js");
const { tools, dispatch, validate } = require("./runtime/tools.js");
const { listen, connect } = require("./runtime/ipc.js");
const { jsonLines, send } = require("./runtime/wire.js");
const { PREFIX, RendererBackend, nativeApproval } = require("./runtime/desktop.js");
const { installRenderer, installPreload } = require("./renderer.js");
const patch = require("./patch.js");
const { CONTEXT, MockBackend, tree, message, clone } = require("./fixtures/backend.js");

function fixture(t, options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hydex-conversation-test-"));
  const backend = options.backend ?? new MockBackend();
  const approvals = [];
  const service = new ConversationService({ backend, directory: path.join(directory, "journal"),
    settings: { mutationsEnabled: true, minDelayMs: 0, ...options.settings },
    approve: options.approve ?? (async (p, meta) => { approvals.push({ plan: p, meta }); return true; }),
    sleep: async () => {},
  });
  t.after(async () => { await service.close(); fs.rmSync(directory, { recursive: true, force: true }); });
  return { directory, backend, service, approvals };
}
async function finish(service, job_id) {
  await service.running.get(job_id)?.completion;
  return service.jobStatus({ context: CONTEXT, job_id });
}
const op = (type, rest = {}) => ({ type, conversation_id: "c1", reason: "Organize the test fixture", ...rest });
async function preview(service, operations) { return service.preview({ context: CONTEXT, plan: { operations } }); }
async function execute(service, plan, extra = {}) {
  return service.execute({ context: CONTEXT, plan_id: plan.plan_id, approval: "request-native", ...extra });
}
const code = expected => e => e.code === expected;

test("mapping normalization retains roles, tool activity, summaries, attachments, metadata and unknown fields", () => {
  const raw = tree(), copy = clone(raw);
  const active = normalize([raw]);
  assert.deepEqual(active.messages.map(m => m.kind), ["user_message", "reasoning_summary", "tool_call", "tool_result", "assistant_reply"]);
  assert.equal(active.messages[1].visible, false);
  assert.equal(active.messages[1].model, "fixture-model");
  assert.match(active.messages[1].reasoning.interpretation, /not complete private/);
  assert.deepEqual(active.messages.at(-1).opaque_reasoning_fields, ["encrypted_reasoning"]);
  assert.equal(active.messages.at(-1).attachments[0].id, "file-fixture");
  assert.equal(active.messages.at(-1).citations[0].url, "https://example.test/source");
  assert.equal(normalize([raw], { branch: "all" }).messages.at(-1).content.unknown.keep, true);
  assert.deepEqual(raw, copy);
});
test("alternate branch follows current_node instead of first child", () => {
  const raw = tree(); raw.current_node = "alternate";
  assert.deepEqual(normalize([raw]).messages.map(m => m.id), ["user", "alternate"]);
});
test("null root is retained and tree cycles, missing nodes and unknown schemas fail explicitly", () => {
  assert.equal(normalize([tree()]).nodes[0].id, "root");
  const missing = tree(); delete missing.mapping.summary;
  assert.throws(() => normalize([missing]), code("INCOMPLETE_CONVERSATION"));
  const cycle = tree(); cycle.mapping.alternate.parent = "alternate";
  assert.throws(() => normalize([cycle], { branch: "all" }), e => ["UNSUPPORTED_FORMAT", "INCOMPLETE_CONVERSATION"].includes(e.code));
  assert.throws(() => normalize([{ id: "c1", unknown: [] }]), code("UNSUPPORTED_FORMAT"));
});
test("raw retrieval preserves backend fields while redacting credentials and signed capabilities", async () => {
  const raw = tree(); raw.access_token = "secret"; raw.credentials = { refreshToken: "refresh" };
  raw.mapping.reply.message.metadata.url = "https://example.test/asset?token=private&name=plot";
  const result = await retrieve(async () => raw, "c1", { mode: "raw" });
  assert.deepEqual(result.pages[0].unknown_top_level, raw.unknown_top_level);
  assert.equal(result.pages[0].mapping.reply.message.metadata.encrypted_reasoning, "opaque-byte-string");
  assert.equal(result.pages[0].access_token, "[redacted]");
  assert.equal(result.pages[0].credentials.refreshToken, "[redacted]");
  assert.equal(JSON.stringify(result).includes("token=private"), false);
  assert.equal(result.redactions.length, 3);
  assert.equal(raw.access_token, "secret");
});
test("ordered pages retrieve older and newer boundaries, preserve raw pages and deduplicate overlaps", async () => {
  const m1 = message("a", "user", "text", { parts: ["a"] }), m2 = message("b", "assistant", "text", { parts: ["b"] }), m3 = message("c", "tool", "execution_output", { text: "c" });
  const first = { conversation_id: "c1", title: "Ordered", messages: [m2], page_info: { has_previous_page: true, has_next_page: true, start_cursor: "b", end_cursor: "b" } };
  const older = { messages: [m1, m2], page_info: { has_previous_page: false, has_next_page: true, end_cursor: "b" } };
  const newer = { messages: [m2, m3], page_info: { has_previous_page: true, has_next_page: false, start_cursor: "b" } };
  const calls = [];
  const result = await retrieve(async (_id, page) => { calls.push(page); return page ? page.direction === "older" ? older : newer : first; }, "c1");
  assert.equal(result.complete, true); assert.equal(result.page_count, 3);
  assert.equal(result.conversation.title, "Ordered");
  assert.deepEqual(result.conversation.messages.map(m => m.id), ["a", "b", "c"]);
  assert.deepEqual(calls.map(p => p?.direction ?? "initial"), ["initial", "older", "newer"]);
  assert.equal(result.conversation.nodes[1].parent, null);
  assert.equal(result.conversation.nodes[1].relationships_supplied, false);
  assert.throws(() => normalize([first], { branch: "all" }), code("BRANCHES_UNAVAILABLE"));
});
test("missing pagination boundaries, stalled cursors, unsupported directions and disagreeing pages never claim completion", async () => {
  const raw = { id: "c1", messages: [message("a", "user", "text", { parts: ["x"] })] };
  await assert.rejects(retrieve(async () => raw, "c1"), code("UNSUPPORTED_FORMAT"));
  assert.equal((await retrieve(async () => raw, "c1", { mode: "raw" })).complete, false);
  const paged = { ...raw, page_info: { has_previous_page: true, has_next_page: false, start_cursor: "a" } };
  await assert.rejects(retrieve(async () => paged, "c1"), code("INCOMPLETE_CONVERSATION"));
  const unsupported = async (_id, page) => { if (page) throw new ApiError("UNSUPPORTED_PAGINATION", "No verified page contract."); return paged; };
  await assert.rejects(retrieve(unsupported, "c1"), code("UNSUPPORTED_PAGINATION"));
  assert.equal((await retrieve(unsupported, "c1", { mode: "raw" })).complete, false);
  const other = clone(raw); other.messages[0].content.parts = ["changed"];
  assert.throws(() => normalize([raw, other]), code("CONCURRENT_UPDATE"));
});
test("mutation state rejects missing membership and conflicts rather than treating them as unassigned", () => {
  const raw = tree(); delete raw.gizmo_id;
  assert.throws(() => conversationState(raw), code("UNSUPPORTED_FORMAT"));
  raw.gizmo_id = null; raw.project_id = "g-p-one";
  assert.throws(() => conversationState(raw), code("UNSUPPORTED_FORMAT"));
});
test("inventory includes archives and every Project page beyond sidebar visibility; metadata scans do not fetch bodies", async t => {
  const { service, backend } = fixture(t);
  const first = await service.listConversations({ context: CONTEXT });
  assert.equal(first.status, "running");
  const done = await finish(service, first.inventory_job_id);
  assert.equal(done.status, "completed");
  const all = await service.listConversations({ context: CONTEXT });
  assert.equal(all.items.length, 12); assert.equal(all.items.find(i => i.id === "c3").archived, true);
  assert.equal(all.completeness, "unknown");
  assert.equal(backend.calls.some(c => c.operation === "conversation"), false);
  assert.equal(backend.calls.filter(c => c.operation === "project_conversations" && c.args.id === "g-p-one").length, 5);
  assert.equal((await service.inventoryStatus({ context: CONTEXT })).sources_exhausted, true);
  const projectList = await service.listProjects({ context: CONTEXT });
  assert.equal(projectList.items[0].membership.known_count, 9);
  assert.equal(projectList.items[0].membership.sources_exhausted, true);
});
test("inventory/search cursors bind account, filter and revision and incremental sync scans every source", async t => {
  const { service } = fixture(t);
  const first = await service.listConversations({ context: CONTEXT }); await finish(service, first.inventory_job_id);
  const a = await service.listConversations({ context: CONTEXT, limit: 3 });
  const b = await service.listConversations({ context: CONTEXT, limit: 3, cursor: a.next_cursor });
  assert.equal(new Set([...a.items, ...b.items].map(x => x.id)).size, 6);
  await assert.rejects(service.listConversations({ context: CONTEXT, cursor: a.next_cursor, query: "changed" }), code("INVALID_CURSOR"));
  await assert.rejects(service.listConversations({ context: CONTEXT, cursor: a.next_cursor.slice(0, -1) + "x" }), code("INVALID_CURSOR"));
  assert.throws(() => service.decodeCursor(a.next_cursor, { ...CONTEXT, user_id: "other" }, "inventory"), code("CONTEXT_CHANGED"));
  const filtered = await service.listConversations({ context: CONTEXT, query: "c12", updated_since: "2020-01-01T00:00:00Z" });
  assert.deepEqual(filtered.items.map(x => x.id), ["c12"]);
  assert.match(filtered.incremental, /full metadata scan/);
});
test("incomplete inventory and rate-limited scopes remain explicit, never empty success", async t => {
  const { service, backend } = fixture(t);
  backend.hook = async operation => operation === "project_conversations" ? { items: [], cursor: "stalled" } : undefined;
  const started = await service.listConversations({ context: CONTEXT });
  assert.equal((await finish(service, started.inventory_job_id)).status, "paused");
  assert.equal((await service.inventoryStatus({ context: CONTEXT })).completeness, "unknown");
});
test("Project pagination uses scoped cursors and treats unknown sharing as shared", async t => {
  const { service } = fixture(t);
  const first = await service.listProjects({ context: CONTEXT });
  assert.equal(first.items.length, 2);
  assert.equal((await service.listProjects({ context: CONTEXT, cursor: first.next_cursor })).items[0].shared, true);
  assert.equal(projectRecord({ gizmo: { id: "g-p-unknown", display: { name: "Unknown" } } }).shared, true);
});
test("reads work with mutations disabled and direct mutation tools only create previews", async t => {
  const { service, backend } = fixture(t, { settings: { mutationsEnabled: false } });
  const result = await dispatch(service, "chatgpt_rename_conversation", { context: CONTEXT, id: "c1", title: "New", reason: "Test" });
  assert.ok(result.plan_id); assert.equal(backend.mutations.length, 0);
  await assert.rejects(execute(service, result), code("MUTATIONS_DISABLED"));
  assert.equal((await service.getConversation({ context: CONTEXT, id: "c1" })).complete, true);
});
test("model assertions cannot grant approval; a rejected native prompt leaves the backend unchanged", async t => {
  const { service, backend } = fixture(t, { approve: async () => false });
  const plan = await preview(service, [op("rename", { title: "New" })]);
  await assert.rejects(execute(service, plan, { approval: true }), code("APPROVAL_REQUIRED"));
  await assert.rejects(execute(service, plan), code("APPROVAL_DENIED"));
  assert.equal(backend.mutations.length, 0);
});
test("batch approval binds exact multi-operation state; mutation verification and undo restore original metadata", async t => {
  const { service, backend, approvals } = fixture(t);
  const before = conversationState(backend.records.get("c1"));
  const plan = await preview(service, [op("rename", { title: "New" }), op("move", { project_id: "g-p-two" }), op("archive", { archived: true })]);
  assert.equal(plan.operations[1].expected_before.title, "New");
  const run = await execute(service, plan), job = await finish(service, run.job_id);
  assert.equal(job.status, "completed"); assert.equal(job.entries.length, 3);
  assert.equal(approvals.length, 1); assert.equal(approvals[0].plan.hash, plan.hash);
  assert.deepEqual(conversationState(backend.records.get("c1")), { title: "New", project_id: "g-p-two", archived: true });
  await assert.rejects(execute(service, plan), code("PLAN_USED"));
  const undo = await service.undo({ context: CONTEXT, job_id: job.id, approval: "request-native" });
  const undone = await finish(service, undo.job_id);
  assert.equal(undone.status, "completed"); assert.equal(undone.kind, "undo");
  assert.deepEqual(conversationState(backend.records.get("c1")), before);
  assert.equal(approvals.length, 2); assert.equal(approvals[1].meta.kind, "undo");
  assert.equal(service.journal.job(job.id).entries.every(e => e.undone_by === undone.id), true);
});
test("plan tampering, account changes and concurrent user edits are rejected before mutation", async t => {
  const { service, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" })]);
  backend.records.get("c1").title = "User edit";
  await assert.rejects(execute(service, plan), code("CONCURRENT_UPDATE"));
  backend.records.get("c1").title = "Conversation c1";
  const saved = service.journal.plan(plan.plan_id); saved.operations[0].title = "Tampered";
  atomicJson(service.journal.file("plan", plan.plan_id), saved);
  await assert.rejects(execute(service, plan), code("PLAN_INTEGRITY"));
  backend.context.workspace_id = "different";
  await assert.rejects(service.getConversation({ context: CONTEXT, id: "c1" }), code("CONTEXT_CHANGED"));
  assert.equal(backend.mutations.length, 0);
});
test("an account change during approval or during a response invalidates execution", async t => {
  const backend = new MockBackend();
  const { service } = fixture(t, { backend, approve: async () => { backend.context.user_id = "other"; return true; } });
  const plan = await preview(service, [op("rename", { title: "New" })]);
  await assert.rejects(execute(service, plan), code("CONTEXT_CHANGED"));
  assert.equal(backend.mutations.length, 0);
});
test("sharing changes invalidate a plan and shared moves require the distinct feature setting", async t => {
  const { service, backend } = fixture(t);
  const shared = await preview(service, [op("move", { project_id: "g-p-shared" })]);
  await assert.rejects(execute(service, shared), code("SHARED_PROJECT_DISABLED"));
  const privatePlan = await preview(service, [op("move", { project_id: "g-p-two" })]);
  backend.projectRecords.get("g-p-two").gizmo.share_recipient = "workspace";
  await assert.rejects(execute(service, privatePlan), code("PROJECT_CHANGED"));
  assert.equal(backend.mutations.length, 0);
});
test("read rate limits honor Retry-After while mutations stop and require a freshly approved resume", async t => {
  const { service, backend, approvals } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" })]);
  let limited = true;
  backend.hook = async operation => { if (operation === "rename" && limited) throw new ApiError("RATE_LIMITED", "Wait.", { retry_at_ms: Date.now() + 60000 }); };
  const run = await execute(service, plan), job = await finish(service, run.job_id);
  assert.equal(job.status, "paused"); assert.equal(job.entries[0].status, "failed");
  assert.equal(backend.mutations.length, 0);
  await assert.rejects(execute(service, plan, { resume_job_id: job.id }), code("RATE_LIMITED"));
  limited = false; service.nextRequestAt = 0;
  const resumed = await execute(service, plan, { resume_job_id: job.id });
  assert.equal((await finish(service, resumed.job_id)).status, "completed");
  assert.equal(backend.mutations.length, 1); assert.equal(approvals.length, 2);
});
test("a lost mutation response is journaled uncertain and reconciled without repeating the mutation", async t => {
  const { service, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" }), op("archive", { archived: true })]);
  let lost = true;
  backend.hook = async (operation, args) => { if (operation === "rename" && lost) { lost = false; backend.records.get(args.id).title = args.title; backend.mutations.push({ operation, args }); throw new ApiError("NATIVE_TIMEOUT", "Lost response."); } };
  const run = await execute(service, plan), job = await finish(service, run.job_id);
  assert.equal(job.entries[0].status, "uncertain"); assert.equal(job.entries[1].status, "pending");
  const resumed = await execute(service, plan, { resume_job_id: job.id });
  assert.equal((await finish(service, resumed.job_id)).status, "completed");
  assert.equal(backend.mutations.filter(m => m.operation === "rename").length, 1);
});
test("an unresolved uncertain request is never blindly repeated; partial completion remains durable", async t => {
  const { service, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" }), op("archive", { archived: true })]);
  backend.hook = async operation => { if (operation === "archive") throw new ApiError("BACKEND_ERROR", "Connection failed."); };
  const run = await execute(service, plan), job = await finish(service, run.job_id);
  assert.equal(job.entries[0].status, "verified"); assert.equal(job.entries[1].status, "uncertain");
  backend.hook = null;
  const resumed = await execute(service, plan, { resume_job_id: job.id });
  assert.equal((await finish(service, resumed.job_id)).error.code, "UNCERTAIN_MUTATION");
  assert.equal(backend.mutations.length, 1);
  assert.equal(fs.statSync(service.journal.file("job", job.id)).mode & 0o777, 0o600);
});
test("Project creation is verified, name conflicts stop execution and undo never deletes Projects", async t => {
  const { service, backend } = fixture(t);
  await assert.rejects(preview(service, [{ type: "create_project", name: "Research", reason: "Duplicate" }]), code("PROJECT_CONFLICT"));
  const plan = await preview(service, [{ type: "create_project", name: "New Project", reason: "New topic" }]);
  const run = await execute(service, plan), job = await finish(service, run.job_id);
  assert.equal(job.status, "completed"); assert.ok(job.entries[0].created_project_id);
  const undo = await service.undo({ context: CONTEXT, job_id: job.id, approval: "request-native" });
  assert.equal(undo.status, "nothing_to_undo"); assert.equal(undo.retained_projects.length, 1);
  assert.equal(backend.projectRecords.size, 4);
});
test("undo detects intervening edits and cannot overwrite them", async t => {
  const { service, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" })]);
  const run = await execute(service, plan); await finish(service, run.job_id);
  backend.records.get("c1").title = "Later user edit";
  await assert.rejects(service.undo({ context: CONTEXT, job_id: run.job_id, approval: "request-native" }), code("CONCURRENT_UPDATE"));
  assert.equal(backend.records.get("c1").title, "Later user edit");
});
test("interrupted intents recover from disk without losing previous state", async t => {
  const { service, directory, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" })]);
  const job = service.journal.newJob(CONTEXT, "execute", { plan_id: plan.plan_id, plan_hash: plan.hash,
    entries: [{ status: "intent", before: conversationState(backend.records.get("c1")) }] });
  const recovered = new ConversationService({ backend, directory: path.join(directory, "journal"), settings: { mutationsEnabled: true, minDelayMs: 0 }, approve: async () => true });
  assert.equal(recovered.journal.job(job.id).status, "interrupted");
  backend.records.get("c1").title = "New";
  const resumed = await execute(recovered, plan, { resume_job_id: job.id });
  assert.equal((await finish(recovered, resumed.job_id)).status, "completed");
  assert.equal(backend.mutations.length, 0); await recovered.close();
});
test("tool schemas reject arbitrary JavaScript, unscoped calls, unknown fields and deletion", async t => {
  const { service } = fixture(t);
  await assert.rejects(dispatch(service, "chatgpt_eval", { code: "1+1" }), code("TOOL_UNAVAILABLE"));
  await assert.rejects(dispatch(service, "chatgpt_get_conversation", { id: "c1" }), code("INVALID_ARGUMENT"));
  await assert.rejects(dispatch(service, "chatgpt_get_status", { url: "https://attacker.test" }), code("INVALID_ARGUMENT"));
  await assert.rejects(preview(service, [op("delete")]), code("OPERATION_UNAVAILABLE"));
  assert.equal(tools.length, 15); assert.equal(tools.some(t => /delete|eval|request$/.test(t.name)), false);
});
test("completeness is confirmed only when all backend source totals agree", async t => {
  const { service, backend } = fixture(t);
  backend.hook = async (operation, args) => {
    if (operation === "projects") return { items: [...backend.projectRecords.values()].map(p => ({ gizmo: clone(p), conversations: [] })), cursor: null, total: backend.projectRecords.size };
    if (operation === "project_conversations") {
      const members = [...backend.records.values()].filter(r => r.gizmo_id === args.id);
      return { items: members.map(r => ({ id: r.conversation_id, title: r.title, is_archived: r.is_archived, gizmo_id: r.gizmo_id, update_time: r.update_time })), cursor: null, total: members.length };
    }
  };
  const started = await service.listConversations({ context: CONTEXT }); await finish(service, started.inventory_job_id);
  assert.equal((await service.inventoryStatus({ context: CONTEXT })).completeness, "confirmed_complete");
});
test("cancellation stops between mutations while retaining a verified in-flight operation", async t => {
  const { service, backend } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" }), op("archive", { archived: true })]);
  let announce, release;
  const called = new Promise(r => { announce = r; }), gate = new Promise(r => { release = r; });
  backend.hook = async operation => { if (operation === "rename") { announce(); await gate; } };
  const job = await execute(service, plan); await called;
  await service.cancel({ context: CONTEXT, job_id: job.job_id }); release();
  const stopped = await finish(service, job.job_id);
  assert.equal(stopped.status, "cancelled"); assert.equal(stopped.entries[0].status, "verified");
  assert.equal(stopped.entries[1].status, "pending"); assert.equal(backend.records.get("c1").is_archived, false);
});
test("concurrent reads and inventory calls have bounded backend concurrency", async t => {
  const { service, backend } = fixture(t); let inFlight = 0, peak = 0;
  backend.hook = async () => { inFlight++; peak = Math.max(peak, inFlight); await new Promise(resolve => setImmediate(resolve)); inFlight--; };
  await Promise.all(["c1", "c2", "c3"].map(id => service.getConversation({ context: CONTEXT, id })));
  assert.equal(peak, 1);
});
test("a rate-limit read retries only a bounded number of times and preserves the cooldown", async t => {
  const { service, backend } = fixture(t); let calls = 0;
  backend.hook = async operation => { if (operation === "conversation") { calls++; throw new ApiError("RATE_LIMITED", "Wait.", { retry_at_ms: Date.now() + 1000 }); } };
  await assert.rejects(service.getConversation({ context: CONTEXT, id: "c1" }), code("RATE_LIMITED"));
  assert.equal(calls, 3); assert.ok(service.nextRequestAt > Date.now());
});
test("account changes during native reads discard the response and restricted records are explicit", async t => {
  const { service, backend } = fixture(t);
  await assert.rejects(service.getConversation({ context: CONTEXT, id: "missing" }), code("SESSION_EXPIRED"));
  backend.hook = async operation => { if (operation === "conversation") { backend.context.user_id = "other"; return tree(); } };
  await assert.rejects(service.getConversation({ context: CONTEXT, id: "c1" }), code("CONTEXT_CHANGED"));
});
test("durable journals reject symlinks and raw unknown records remain inspectable but incomplete", async t => {
  const { service, directory } = fixture(t);
  const plan = await preview(service, [op("rename", { title: "New" })]);
  const target = path.join(directory, "replacement"); fs.writeFileSync(target, JSON.stringify(service.journal.plan(plan.plan_id)), { mode: 0o600 });
  fs.unlinkSync(service.journal.file("plan", plan.plan_id)); fs.symlinkSync(target, service.journal.file("plan", plan.plan_id));
  await assert.rejects(execute(service, plan), code("UNSAFE_PATH"));
  const unknown = await retrieve(async () => ({ id: "c1", future_structure: { data: "preserve" } }), "c1", { mode: "raw" });
  assert.equal(unknown.complete, false); assert.equal(unknown.pages[0].future_structure.data, "preserve");
});
test("published input and output schemas validate the actual public results", async t => {
  const { service } = fixture(t);
  const status = await dispatch(service, "chatgpt_get_status", {});
  validate(tools.find(t => t.name === "chatgpt_get_status").outputSchema, status);
  const definition = tools.find(t => t.name === "chatgpt_get_conversation");
  for (const mode of ["normalized", "raw"]) {
    const result = await dispatch(service, definition.name, { context: CONTEXT, id: "c1", mode });
    validate(definition.outputSchema, result);
  }
});

function nativeHarness(t) {
  const backend = new MockBackend(), handlers = new Map();
  const sender = new EventEmitter(); Object.assign(sender, { id: 1, mainFrame: {}, isDestroyed: () => false });
  const event = { sender, senderFrame: sender.mainFrame, trusted: true };
  const electron = { ipcMain: { handle: (name, fn) => handlers.set(name, fn), removeHandler: name => handlers.delete(name) } };
  const native = new RendererBackend({ electron, isTrustedIpcEvent: e => e.trusted === true });
  t.after(() => native.close());
  const accountAtom = {}, auth = { userId: CONTEXT.user_id, accountId: CONTEXT.workspace_id, authenticatedUserId: CONTEXT.user_id,
    authenticatedAccountId: CONTEXT.account_id, hasChatGptToken: true, authMethod: "chatgpt", authLoading: false, accountLoading: false, requiresAuth: false };
  let callback;
  const bridge = { hello: async c => handlers.get(PREFIX + "hello")(event, c), respond: async (id, result) => handlers.get(PREFIX + "response")(event, id, result), onRequest: cb => { callback = cb; } };
  sender.send = (_name, request) => { Promise.resolve().then(() => callback(request)); };
  const request = Object.fromEntries(["safeGet", "safePost", "safePatch"].map(method => [method, async (route, options) => {
    options.assertRequestCurrent();
    assert.deepEqual(JSON.parse(JSON.stringify(options.expectedIdentity)), { accountId: CONTEXT.workspace_id, userId: CONTEXT.user_id });
    assert.equal(options.additionalHeaders["ChatGPT-Account-ID"], CONTEXT.workspace_id);
    assert.equal(options.retry, "never");
    const a = options.parameters?.path ?? {}, q = options.parameters?.query ?? {}, body = options.requestBody ?? {};
    const operation = route === "/conversations" ? "list" : route.endsWith("/sidebar") ? "projects"
      : route.endsWith("/rename") ? "rename" : route === "/projects" ? "create_project"
      : route.includes("gizmo_id_or_short_url") ? "project" : route.includes("/gizmos/") ? "project_conversations"
      : method === "safePatch" ? body.is_archived === undefined ? "move" : "archive" : "conversation";
    const result = await backend.call(operation, { ...body, id: a.conversation_id ?? a.gizmo_id ?? a.gizmo_id_or_short_url,
      offset: q.offset, limit: q.limit, archived: q.is_archived ?? body.is_archived, cursor: q.cursor, project_id: body.gizmo_id }, CONTEXT);
    options.assertRequestCurrent(); return result;
  }]));
  const scope = { get: atom => { assert.equal(atom, accountAtom); return auth; } };
  vm.runInNewContext(`(${installRenderer.toString()})(scope,accountAtom,()=>request)`, { scope, accountAtom, request, hydexConversationBridge: bridge, addEventListener: () => {} });
  return { backend, native, handlers, event, auth, sender };
}
test("authenticated renderer bridge uses native request identity and trusted main-frame IPC", async t => {
  const { native, handlers, event, auth } = nativeHarness(t);
  assert.deepEqual(clone(await native.identity()), CONTEXT);
  const raw = await native.call("conversation", { id: "c1" }, CONTEXT);
  assert.equal(raw.conversation_id, "c1");
  assert.equal(await handlers.get(PREFIX + "hello")({ ...event, trusted: false }, { ...CONTEXT, user_id: "attacker" }), false);
  assert.equal(await handlers.get(PREFIX + "hello")({ ...event, senderFrame: {} }, CONTEXT), false);
  auth.authenticatedUserId = "other";
  await assert.rejects(native.identity(), code("CONTEXT_CHANGED"));
});
test("preload exposes only typed bridge messaging and never generic IPC or JavaScript execution", () => {
  let exposed;
  vm.runInNewContext(`(${installPreload.toString()})();`, { require: name => {
    assert.equal(name, "electron"); return { contextBridge: { exposeInMainWorld: (name, api) => { assert.equal(name, "hydexConversationBridge"); exposed = api; } }, ipcRenderer: { invoke: () => {}, on: () => {} } };
  } });
  assert.deepEqual(Object.keys(exposed).sort(), ["hello", "onRequest", "respond"]);
});
test("native approval requires an affirmative click and separate shared-destination consent", async () => {
  const answers = [1, 0], dialogs = [];
  const approve = nativeApproval({ dialog: { showMessageBox: async options => { dialogs.push(options); return { response: answers.shift() }; } } });
  assert.equal(await approve({ id: "plan", hash: "sha", context: CONTEXT, operations: [op("move")] }, { kind: "execute", shared: [{ id: "g-p-shared" }] }), false);
  assert.equal(dialogs.length, 2); assert.equal(dialogs[0].defaultId, 0); assert.equal(dialogs[1].cancelId, 0);
  assert.match(dialogs[0].detail, /sha/);
});
test("IPC directory and files are private, duplicate owners are rejected, and the adapter can reconnect", async t => {
  const { service, directory } = fixture(t), bridgeDir = path.join(directory, "bridge");
  const server = await listen({ service, directory: bridgeDir });
  t.after(() => server.close());
  const client = await connect(bridgeDir);
  assert.equal((await client.call("chatgpt_get_status", {})).result.connected, true); client.close();
  const second = await connect(bridgeDir);
  assert.equal((await second.call("chatgpt_get_status", {})).result.context.user_id, CONTEXT.user_id); second.close();
  assert.equal(fs.statSync(bridgeDir).mode & 0o777, 0o700);
  for (const file of ["desktop.sock", "bridge.json", "owner.json"]) assert.equal(fs.statSync(path.join(bridgeDir, file)).mode & 0o777, 0o600);
  await assert.rejects(listen({ service, directory: bridgeDir }), code("ALREADY_RUNNING"));
});
test("unsafe directories and symlinked discovery records are rejected", async t => {
  const { service, directory } = fixture(t);
  const unsafe = path.join(directory, "unsafe"); fs.mkdirSync(unsafe, { mode: 0o755 }); fs.chmodSync(unsafe, 0o755);
  await assert.rejects(listen({ service, directory: unsafe }), code("UNSAFE_PATH"));
  const safe = path.join(directory, "safe"); fs.mkdirSync(safe, { mode: 0o700 });
  const target = path.join(directory, "target"); fs.writeFileSync(target, "{}", { mode: 0o600 });
  fs.symlinkSync(target, path.join(safe, "bridge.json"));
  await assert.rejects(connect(safe), code("UNSAFE_PATH"));
});
test("unauthenticated socket clients cannot dispatch tools and stale ownership can be reclaimed", async t => {
  const net = require("node:net");
  const { service, directory } = fixture(t), bridgeDir = path.join(directory, "bridge");
  fs.mkdirSync(bridgeDir, { mode: 0o700 });
  fs.writeFileSync(path.join(bridgeDir, "owner.json"), JSON.stringify({ pid: 2147483647, start: "stale" }), { mode: 0o600 });
  const server = await listen({ service, directory: bridgeDir });
  let calls = 0; const original = service.status.bind(service);
  service.status = async () => { calls++; return original(); };
  await new Promise((resolve, reject) => {
    const socket = net.createConnection(path.join(bridgeDir, "desktop.sock"));
    socket.on("error", reject); socket.once("close", resolve);
    socket.once("connect", () => send(socket, { version: 1, auth: "0".repeat(64), tool: "chatgpt_get_status" }));
  });
  assert.equal(calls, 0); await server.close();
  assert.equal(fs.existsSync(path.join(bridgeDir, "bridge.json")), false);
  const restarted = await listen({ service, directory: bridgeDir });
  const client = await connect(bridgeDir);
  assert.equal((await client.call("chatgpt_get_status", {})).result.connected, true);
  client.close(); await restarted.close();
});

test("stdio MCP crosses the authenticated Unix bridge and native renderer for inventory, retrieval, approval, execution and undo", async t => {
  const harness = nativeHarness(t);
  const allowed = new Set();
  const { service, directory } = fixture(t, { backend: harness.native, approve: async plan => allowed.has(plan.hash) });
  const bridgeDir = path.join(directory, "hydex-chatgpt-conversation-api");
  const server = await listen({ service, directory: bridgeDir }); t.after(() => server.close());
  const child = spawn(process.execPath, [path.join(__dirname, "runtime/mcp.js")], { env: { ...process.env, XDG_RUNTIME_DIR: directory }, stdio: ["pipe", "pipe", "pipe"] });
  t.after(() => child.kill());
  let seq = 0; const requests = new Map(); let stderr = "";
  child.stderr.on("data", bytes => { stderr += bytes; });
  jsonLines(child.stdout, response => { const resolve = requests.get(response.id); requests.delete(response.id); resolve?.(response); }, () => assert.fail("Invalid MCP framing"));
  const rpc = (method, params) => new Promise(resolve => { const id = ++seq; requests.set(id, resolve); send(child.stdin, { jsonrpc: "2.0", id, method, params }); });
  const call = async (name, args = {}) => (await rpc("tools/call", { name, arguments: args })).result;
  assert.equal((await rpc("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "fixture-codex-mcp-client", version: "1" } })).result.protocolVersion, "2025-06-18");
  send(child.stdin, { jsonrpc: "2.0", method: "notifications/initialized" });
  assert.equal((await rpc("tools/list", {})).result.tools.length, 15);
  const status = await call("chatgpt_get_status"); assert.equal(status.structuredContent.connected, true);
  const inventory = await call("chatgpt_list_conversations", { context: CONTEXT });
  assert.equal((await finish(service, inventory.structuredContent.inventory_job_id)).status, "completed");
  assert.equal((await call("chatgpt_list_conversations", { context: CONTEXT })).structuredContent.items.length, 12);
  const conversation = await call("chatgpt_get_conversation", { context: CONTEXT, id: "c1", mode: "normalized", branch: "all" });
  assert.equal(conversation.structuredContent.conversation.messages.length, 6);
  const projects = await call("chatgpt_list_projects", { context: CONTEXT }); assert.equal(projects.structuredContent.items.length, 2);
  const previewed = await call("chatgpt_preview_changes", { context: CONTEXT, plan: { operations: [op("rename", { title: "Classified" }), op("move", { project_id: "g-p-two" })] } });
  const proposed = previewed.structuredContent;
  const denied = await call("chatgpt_execute_approved_plan", { context: CONTEXT, plan_id: proposed.plan_id, approval: "request-native" });
  assert.equal(denied.isError, true); assert.equal(denied.structuredContent.error.code, "APPROVAL_DENIED");
  allowed.add(proposed.hash);
  const executed = await call("chatgpt_execute_approved_plan", { context: CONTEXT, plan_id: proposed.plan_id, approval: "request-native" });
  const done = await finish(service, executed.structuredContent.job_id); assert.equal(done.status, "completed");
  // The simulated user approves the inverse as it is presented; no tool can
  // set this callback or make an approval argument bypass it.
  service.approve = async plan => plan.original_job_id === done.id;
  const undo = await call("chatgpt_undo_job", { context: CONTEXT, job_id: done.id, approval: "request-native" });
  assert.equal((await finish(service, undo.structuredContent.job_id)).status, "completed");
  assert.equal(harness.backend.records.get("c1").title, "Conversation c1");
  assert.equal(harness.backend.records.get("c1").gizmo_id, null);
  assert.equal(stderr, ""); child.stdin.end();
});

const MAIN = 'let trust=event=>registry.isTrustedIpcSender(event.sender,event.senderFrame??null);run();';
const PRELOAD = 'const x=require("electron");x.contextBridge.exposeInMainWorld("electronBridge",{invoke:()=>x.ipcRenderer.invoke("native")});';
const REQUEST = 'Ghh3(e,t,n){n.d(t,{Request:()=>r,getChatGptRequestProfile:()=>p});const r={};function p(){}}';
const AUTH = 'cE1j(e,t,n){var g=n("AUTH");function sync(W,N,Z){let v=W.get(g.PO);(N.isLoading||!N.hasChatGptToken||v.authenticatedAccountId!==Z||v.authenticatedUserId!==N.userId)&&abort();W.set(j.$S,{accountId:N.accountId,accountInfoError:!1,accountInfoLoading:!1,authLoading:N.isLoading,authMethod:N.authMethod,plan:null,supportedSurface:!0,workCloudSupportedSurface:!0})}}';
const ROUTES = ['"/conversation/{conversation_id}"', '"/gizmos/{gizmo_id}/conversations"', '"/gizmos/snorlax/sidebar"',
  '"/conversation/id/{conversation_id}/rename"', '"/conversations/{conversation_id}/messages"', 'Request.safePost("/projects",{})', '"ChatGPT-Account-ID"'].join(";");
const CHUNK = `export const __webpack_modules__={${REQUEST},${AUTH},routes(e,t,n){${ROUTES}}};`;
test("current semantic patch anchors are idempotent and incomplete/ambiguous patches fail closed", () => {
  for (const [source, fn] of [[MAIN, patch.patchMain], [PRELOAD, patch.patchPreload], [CHUNK, patch.patchRenderer]]) {
    const patched = fn(source); assert.notEqual(patched, source); assert.equal(fn(patched), patched);
    assert.throws(() => fn(patched + patch.MARK));
  }
  assert.throws(() => patch.patchMain(MAIN + MAIN));
  assert.throws(() => patch.patchRenderer(CHUNK.replace("/gizmos/snorlax/sidebar", "/changed")));
  assert.throws(() => patch.patchRenderer(CHUNK.replace("v.authenticatedAccountId", "v.changed")));
  assert.throws(() => patch.patchMain(MAIN, { mutationsEnabled: "true" }));
  assert.throws(() => patch.patchMain(MAIN, { allowDelete: true }));
});
test("extracted-app patch preflights all anchors before writing and feature defaults stay opt-in", t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hydex-conversation-patch-fixture-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const build = path.join(directory, ".vite", "build"), assets = path.join(directory, "webview", "assets", "async");
  fs.mkdirSync(build, { recursive: true }); fs.mkdirSync(assets, { recursive: true });
  fs.writeFileSync(path.join(build, "opaque-main.js"), MAIN); fs.writeFileSync(path.join(build, "preload.js"), PRELOAD);
  fs.writeFileSync(path.join(assets, "opaque-chunk.js"), CHUNK.replace("/gizmos/snorlax/sidebar", "/changed"));
  assert.throws(() => patch.patchExtracted(directory));
  assert.equal(fs.readFileSync(path.join(build, "opaque-main.js"), "utf8"), MAIN);
  fs.writeFileSync(path.join(assets, "opaque-chunk.js"), CHUNK);
  assert.equal(patch.patchExtracted(directory).changed, true); assert.equal(patch.patchExtracted(directory).changed, false);
  const manifest = require("./feature.json"); assert.equal(manifest.defaultEnabled, false);
  assert.equal(patch.settingsOf().mutationsEnabled, false); assert.equal(patch.settingsOf().sharedProjectMovesEnabled, false);
});
test("feature resources stage with traversable directories, exact file modes and disabled cleanup", t => {
  const { stageEnabledLinuxFeatureInstall, enabledLinuxFeaturePackageDependencies } = require("../../scripts/lib/linux-features.js");
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hydex-conversation-resource-fixture-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const featuresRoot = path.resolve(__dirname, ".."), config = path.join(directory, "features.json"), app = path.join(directory, "minimal-app-fixture");
  fs.mkdirSync(app); fs.writeFileSync(config, JSON.stringify({ enabled: ["chatgpt-conversation-api"] }));
  const options = { featuresRoot, featuresConfigPath: config };
  stageEnabledLinuxFeatureInstall(app, options);
  const installed = path.join(app, ".codex-linux/features/chatgpt-conversation-api");
  assert.equal(fs.statSync(installed).mode & 0o777, 0o755);
  assert.equal(fs.statSync(path.join(installed, "mcp.js")).mode & 0o777, 0o644);
  assert.equal(require(path.join(installed, "tools.js")).tools.length, 15);
  assert.equal(fs.existsSync(path.join(installed, "fixtures")), false);
  for (const packageFormat of ["deb", "rpm", "pacman"]) assert.ok(enabledLinuxFeaturePackageDependencies({ ...options, packageFormat }).includes("nodejs"));
  fs.writeFileSync(config, JSON.stringify({ enabled: [] }));
  stageEnabledLinuxFeatureInstall(app, options); assert.equal(fs.existsSync(installed), false);
});
