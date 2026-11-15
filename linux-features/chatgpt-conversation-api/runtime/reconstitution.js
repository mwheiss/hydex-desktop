"use strict";

// Experimental writes share the existing hash-bound plan, native approval and
// journal. They never use a read tool as a disguised mutation or replay an
// interrupted HTTP request. History imports are new chats, not checkpoints.
const crypto = require("node:crypto");
const { fail, identifier, textValue, digest, same, publicError } = require("./common.js");
const { normalize } = require("./normalize.js");

const TYPES = new Set(["create_chat", "send_message", "branch_chat", "import_chat"]);
const uuid = value => typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
function keys(value, allowed) {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(k => !allowed.includes(k))) fail("INVALID_ARGUMENT", "Unexpected experimental operation field.");
}
function ensure(service) {
  if (!service.settings.experimentalChatWrites) fail("EXPERIMENTS_DISABLED", "Enable experimentalChatWrites explicitly and rebuild the optional feature.");
}
function importPayload(value) {
  keys(value, ["uuid", "name", "created_at", "updated_at", "chat_messages"]);
  if (!uuid(value.uuid)) fail("INVALID_ARGUMENT", "History import requires a new UUID.");
  textValue(value.name); const seen = new Set();
  for (const date of [value.created_at, value.updated_at]) if (typeof date !== "string" || !Number.isFinite(Date.parse(date))) fail("INVALID_ARGUMENT", "Import timestamps must be ISO dates.");
  if (!Array.isArray(value.chat_messages) || !value.chat_messages.length || value.chat_messages.length > 2000) fail("INVALID_ARGUMENT", "Import requires 1 to 2000 text messages.");
  for (const m of value.chat_messages) {
    keys(m, ["uuid", "sender", "text", "content", "created_at", "updated_at", "attachments", "files"]);
    if (!uuid(m.uuid) || seen.has(m.uuid) || !["human", "assistant"].includes(m.sender)) fail("INVALID_ARGUMENT", "Invalid or duplicate imported message identity/role.");
    seen.add(m.uuid); textValue(m.text, 200000);
    if (![m.created_at, m.updated_at].every(t => typeof t === "string" && Number.isFinite(Date.parse(t)))) fail("INVALID_ARGUMENT", "Invalid import message timestamps.");
    if (m.content !== undefined && !same(m.content, [{ type: "text", text: m.text }])) fail("INVALID_ARGUMENT", "Only matching plain text import content is supported.");
    if ([m.attachments, m.files].some(x => x !== undefined && (!Array.isArray(x) || x.length))) fail("INVALID_ARGUMENT", "Attachments cannot be restored by history import.");
  }
  return JSON.parse(JSON.stringify(value));
}
function testFile(value) {
  keys(value, ["name", "text", "synthetic"]);
  if (value.synthetic !== true || typeof value.name !== "string" || !/^[A-Za-z0-9_-]{1,80}\.txt$/.test(value.name)) fail("INVALID_ARGUMENT", "Only an explicitly synthetic, simply named .txt canary file is supported.");
  textValue(value.text, 65536);
  if (Buffer.byteLength(value.text) > 65536) fail("INVALID_ARGUMENT", "Canary files are limited to 64 KiB.");
  return { ...value, sha256: crypto.createHash("sha256").update(value.text).digest("hex"), bytes: Buffer.byteLength(value.text) };
}
async function snapshot(service, id, context) {
  const result = await service.getConversation({ context, id, mode: "raw", branch: "all" });
  if (!result.complete || !result.pages?.length) fail("INCOMPLETE_CONVERSATION", "A complete source snapshot is required.");
  const raw = result.pages.at(-1), normalized = normalize(result.pages, { branch: "all", header: raw });
  if (normalized.project_id || raw.gizmo_id || raw.is_archived || !raw.current_node || !raw.mapping?.[raw.current_node]) fail("OPERATION_UNAVAILABLE", "Experimental continuation currently requires an unarchived ordinary chat outside a Project or GPT.");
  if (normalized.messages.some(m => ["in_progress", "incomplete"].includes(m.raw_message?.status))) fail("CONCURRENT_UPDATE", "Wait until the source turn has finished.");
  return { hash: digest(result.pages), current_node: raw.current_node, normalized, raw };
}
async function preview(service, input, context) {
  ensure(service);
  const allowed = ["type", "reason", "conversation_id", "model", "text", "test_file", "message_id", "history", "loss_report", "source_conversation_id", "source_snapshot_sha256"];
  keys(input, allowed);
  const op = { type: input.type, reason: textValue(input.reason, 2000), task_id: crypto.randomUUID() };
  const fields = { create_chat: ["model", "text", "test_file"], send_message: ["conversation_id", "model", "text"],
    branch_chat: ["conversation_id", "message_id"], import_chat: ["history", "loss_report", "source_conversation_id", "source_snapshot_sha256"] }[input.type];
  if (Object.keys(input).some(k => !["type", "reason", ...fields].includes(k))) fail("INVALID_ARGUMENT", "Field is not supported by this experimental operation.");
  if (["create_chat", "send_message"].includes(op.type)) {
    op.model = textValue(input.model, 100);
    if (!/^[A-Za-z0-9._:-]+$/.test(op.model)) fail("INVALID_ARGUMENT", "Choose an explicit native model slug.");
    op.text = textValue(input.text, 100000); op.user_message_id = crypto.randomUUID();
    if (input.test_file !== undefined) op.test_file = testFile(input.test_file);
    if (op.type === "create_chat") op.parent_message_id = crypto.randomUUID();
  }
  const sourceId = op.type === "import_chat" ? input.source_conversation_id : input.conversation_id;
  if (sourceId !== undefined) {
    op.source_conversation_id = identifier(sourceId);
    const source = await snapshot(service, sourceId, context);
    if (op.type === "import_chat" && input.source_snapshot_sha256 !== source.hash) fail("CONCURRENT_UPDATE", "The imported history must come from the current complete source snapshot; prepare it again.");
    op.expected_source = { hash: source.hash, current_node: source.current_node };
    if (op.type === "send_message") { op.conversation_id = sourceId; op.parent_message_id = source.current_node; }
    if (op.type === "branch_chat") {
      op.conversation_id = sourceId; op.message_id = identifier(input.message_id ?? source.current_node);
      if (!source.raw.mapping[op.message_id]?.message) fail("INVALID_ARGUMENT", "Branch point must be a source message node.");
    }
  } else if (["send_message", "branch_chat"].includes(op.type)) fail("INVALID_ARGUMENT", "A conversation ID is required.");
  if (op.type === "import_chat") {
    if (input.source_snapshot_sha256 !== undefined && !input.source_conversation_id) fail("INVALID_ARGUMENT", "A source snapshot digest requires its conversation ID.");
    op.history = importPayload(input.history);
    // Provenance reports are reference material, never interpreted as commands.
    op.loss_report = textValue(input.loss_report, 16000);
    op.fidelity = "text_history_only; original identity, hidden context, tools, files, memory and Project configuration are not restored";
  }
  return op;
}
async function preflight(service, op, context, entry = {}) {
  ensure(service);
  // Any issued experimental request requires reconciliation, never resubmission.
  if (["intent", "uncertain"].includes(entry.status)) return;
  if (op.source_conversation_id) {
    const current = await snapshot(service, op.source_conversation_id, context);
    if (!same(op.expected_source, { hash: current.hash, current_node: current.current_node })) fail("CONCURRENT_UPDATE", "The source chat changed after preview; preview a fresh plan.");
  }
}
async function verify(service, op, entry, context) {
  if (entry.result?.already_imported) fail("IMPORT_NOT_CREATED", "The importer returned a pre-existing chat; no new reconstruction was confirmed.");
  const id = entry.result?.conversation_id ?? op.conversation_id;
  if (!id) fail("UNCERTAIN_MUTATION", "No created chat ID was confirmed. Reconcile manually; this request will not be repeated.");
  identifier(id);
  if (op.type !== "send_message" && id === op.source_conversation_id) fail("VERIFY_FAILED", "Reconstruction must create a different chat identity.");
  const result = await service.getConversation({ context, id, mode: "raw", branch: "all" });
  if (!result.complete) fail("VERIFY_FAILED", "The resulting chat snapshot is incomplete.");
  const normalized = normalize(result.pages, { branch: "active", header: result.pages.at(-1) });
  if (["create_chat", "send_message"].includes(op.type)) {
    const message = normalized.messages.find(m => m.id === op.user_message_id);
    if (!message || message.role !== "user" || !message.content?.parts?.includes(op.text)) fail("VERIFY_FAILED", "The frozen outgoing message has not been confirmed in history.");
    if (entry.result?.stream_complete !== true) fail("UNCERTAIN_MUTATION", "The outgoing message exists, but stream completion was not confirmed. It will not be resent.");
  } else if (op.type === "import_chat") {
    // The backend may merge adjacent speakers. Preserve ordered text/roles,
    // and report a mismatch rather than treating HTTP success as a restore.
    const actual = normalized.messages.filter(m => m.visible && ["user", "assistant"].includes(m.role));
    const expected = op.history.chat_messages;
    if (actual.length !== expected.length || expected.some((m, i) => actual[i].role !== (m.sender === "human" ? "user" : "assistant") || actual[i].content?.parts?.join("") !== m.text)) fail("IMPORT_FIDELITY_MISMATCH", "The created chat exists, but imported text/role readback differs from the approved history.", { conversation_id: id });
  } else if (!normalized.messages.some(m => m.node_id === op.message_id || m.id === op.message_id)) fail("VERIFY_FAILED", "The branch point was not found in the new chat.");
  entry.after = { conversation_id: id, snapshot_sha256: digest(result.pages), fidelity: op.fidelity ?? "native_operation; context fidelity not measured" };
  service.recordChange(context, { kind: "chat_changed", id });
}
async function run(service, job, op, entry, signal) {
  ensure(service);
  const context = job.context;
  let recovered = null;
  if (["intent", "uncertain"].includes(entry.status)) {
    // A lost IPC response need not lose the renderer's task/result. This is a
    // local lookup, never a second HTTP dispatch. After renderer loss only the
    // retained backend identity can be reconciled.
    try { recovered = await service.backend.call("experiment_status", { task_id: op.task_id }, context); await service.identity(context); }
    catch (error) { if (error.code === "CONTEXT_CHANGED") throw error; }
    if (!recovered) { await verify(service, op, entry, context); entry.status = "verified"; return; }
  } else {
    await preflight(service, op, context, entry);
    entry.status = "intent"; entry.started_at = new Date().toISOString(); service.journal.saveJob(job);
  }
  let terminal = false;
  try {
    let task = recovered ?? await service.call("experiment_start", { task_id: op.task_id, operation: op }, context, { read: false, signal });
    const deadline = service.clock() + 15 * 60 * 1000;
    for (;;) {
      if (task.task_id !== op.task_id) fail("VERIFY_FAILED", "Renderer returned a different operation identity.");
      if (task.result) { entry.result = task.result; service.journal.saveJob(job); }
      if (task.status === "completed") { terminal = true; break; }
      if (task.status !== "running") fail("UNCERTAIN_MUTATION", "The renderer did not confirm completion; inspect the saved result before retrying.");
      signal.throwIfAborted();
      if (service.clock() > deadline) fail("UNCERTAIN_MUTATION", "Generation exceeded the operation time limit.");
      await service.sleep(1000);
      // This polls local renderer state; it never sends another backend write.
      task = await service.backend.call("experiment_status", { task_id: op.task_id }, context);
      await service.identity(context);
    }
    await verify(service, op, entry, context); entry.status = "verified";
  } catch (error) {
    entry.status = "uncertain"; entry.error = publicError(error); service.journal.saveJob(job); throw error;
  } finally {
    if (!terminal) await service.backend.call("experiment_cancel", { task_id: op.task_id }, context).catch(() => {});
  }
}
module.exports = { TYPES, ensure, importPayload, testFile, snapshot, preview, preflight, run };
