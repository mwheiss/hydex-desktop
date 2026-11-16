"use strict";

// Serialized alongside the renderer bridge. Reuse the official high-level
// transport (requirements, integrity, attestation, stream decoding and uploads).
// No tokens, caller URLs, arbitrary headers or generic HTTP cross this boundary.
function installExperiments(getClients, getRequest, getIdentity) {
  const tasks = new Map();
  const same = (a, b) => a && b && ["user_id", "account_id", "workspace_id"].every(k => a[k] === b[k]);
  const publicTask = t => {
    const result = { ...t.result };
    // Main journals the full response before allowing continuation. Do not
    // resend a potentially large graph on every subsequent stream poll.
    if (t.generationIssued) delete result.branch_response;
    return { task_id: t.id, status: t.status, result, ...(t.error ? { error: t.error } : {}) };
  };
  return async (kind, args, env) => {
    const { before, options, assertRequestCurrent } = env;
    const task = tasks.get(args.task_id);
    if (task && !same(task.context, before)) throw { code: "CONTEXT_CHANGED" };
    if (kind === "experiment_status") {
      if (!task) throw { code: "OPERATION_UNAVAILABLE" };
      return publicTask(task);
    }
    if (kind === "experiment_cancel") {
      if (!task) return { task_id: args.task_id, status: "unknown" };
      task.controller.abort();
      if (task.streamId) getClients().transport.cancelStream(task.streamId);
      if (["running", "prepared"].includes(task.status)) task.status = "uncertain";
      return publicTask(task);
    }
    if (kind === "experiment_continue") {
      if (!task || task.operationType !== "branch_chat") throw { code: "OPERATION_UNAVAILABLE" };
      if (task.status === "prepared" && !task.generationIssued && !task.controller.signal.aborted) {
        // Set this synchronously: duplicate IPC acknowledgements cannot issue
        // another completion. Only main can acknowledge the journaled draft.
        task.generationIssued = true; task.status = "running"; task.result.continuation_started = true;
        Promise.resolve().then(task.generate).catch(task.failed);
      }
      return publicTask(task);
    }
    if (kind !== "experiment_start") throw { code: "OPERATION_UNAVAILABLE" };
    const fingerprint = JSON.stringify(args.operation);
    if (task) {
      if (task.fingerprint !== fingerprint) throw { code: "OPERATION_UNAVAILABLE" };
      return publicTask(task);
    }
    if (tasks.size >= 128) throw { code: "OPERATION_UNAVAILABLE" };
    const op = args.operation, controller = new AbortController();
    const t = { id: args.task_id, fingerprint, operationType: op.type, context: before, controller, status: "running", result: {} };
    tasks.set(t.id, t); // Register before dispatch: duplicate IPC cannot issue twice.
    const assertCurrent = () => { controller.signal.throwIfAborted(); if (!same(before, getIdentity())) throw { code: "CONTEXT_CHANGED" }; assertRequestCurrent(); };
    const failed = e => {
      t.status = "uncertain";
      const status = [e?.status, e?.statusCode, e?.response?.status].find(value => Number.isInteger(value) && value >= 100 && value <= 599);
      t.error = { code: e?.code === "CONTEXT_CHANGED" ? "CONTEXT_CHANGED" : "BACKEND_ERROR", ...(status ? { status } : {}) };
    };
    const capture = event => {
      const u = event?.update ?? event;
      const id = u?.conversationId ?? u?.conversation_id;
      if (typeof id === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(id)) t.result.conversation_id = id;
    };
    t.failed = failed;
    t.generate = async () => {
      assertCurrent();
      const clients = getClients(), attachments = [];
      if (op.test_file) {
        const file = new File([op.test_file.text], op.test_file.name, { type: "text/plain" });
        const uploaded = await clients.upload(file, { beforeRequest: assertCurrent,
          expectedIdentity: { accountId: before.workspace_id, userId: before.user_id },
          model: op.model, storeInLibrary: false, signal: controller.signal });
        assertCurrent();
        if (!uploaded?.id) throw Error("No uploaded file identity");
        // Persist the file ID even if generation subsequently fails.
        t.result.file_id = uploaded.id;
        attachments.push({ id: uploaded.id, name: uploaded.name, size: uploaded.size, mime_type: uploaded.mimeType ?? "text/plain" });
      }
      const branch = op.type === "branch_chat";
      const request = { action: "next", ...(op.type === "send_message" ? { conversation_id: op.conversation_id } : {}),
        ...(branch ? { branching_from_conversation_id: op.conversation_id, branching_from_message_id: op.message_id } : {}),
        parent_message_id: branch ? t.result.branch_response.conversation.current_node : op.parent_message_id, model: op.model,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, timezone_offset_min: new Date().getTimezoneOffset(),
        // Disable personalization for controlled experiments. Existing chats
        // may still have context predating this flag; the evaluator says so.
        is_do_not_remember: true,
        messages: [{ id: op.user_message_id, author: { role: "user" },
          content: { content_type: "text", parts: [op.text] }, metadata: attachments.length ? { attachments } : {} }] };
      if (op.type === "send_message") t.result.conversation_id = op.conversation_id;
      const stream = await clients.transport.startCompletionStream({ request,
        expectedIdentity: { accountId: before.workspace_id, userId: before.user_id }, expectedAccountId: before.workspace_id,
        assertRequestCurrent: assertCurrent, startupSignal: controller.signal,
        additionalHeaders: { "ChatGPT-Account-ID": before.workspace_id },
        onDecodedEvent: capture, onUpdate: capture,
        onComplete: event => { capture(event); try { assertCurrent(); t.result.stream_complete = true; t.status = "completed"; } catch (e) { failed(e); } },
        onError: failed, onPrematureTermination: () => failed(),
        onTransportClose: () => { if (t.status === "running") failed(); },
      });
      t.streamId = stream.streamRequestId;
      // A cancellation can arrive while startCompletionStream is awaiting.
      if (controller.signal.aborted && t.streamId) clients.transport.cancelStream(t.streamId);
    };
    // Defer all network work until the task ID has been returned to main.
    Promise.resolve().then(async () => {
      assertCurrent();
      if (op.type === "import_chat") {
        const response = await getRequest().safePost("/data-import/conversation", options({ signal: controller.signal,
          requestBody: { source: "claude", conversation: op.history } }, { "X-OpenAI-Expected-Account-Id": before.workspace_id }));
        assertCurrent();
        t.result.import_response = response;
        t.result.import_status = response?.status;
        t.result.already_imported = response?.already_imported === true;
        if (response?.conversation_id) t.result.conversation_id = response.conversation_id;
        if (response?.status !== "success" || !response.conversation_id || (response.already_imported && op.reuse_existing !== true)) throw Error("Import did not create an approved chat");
        t.status = "completed";
      } else if (op.type === "branch_chat") {
        const response = await getRequest().safePost("/conversation/new_branch", options({ signal: controller.signal,
          requestBody: { conversation_id: op.conversation_id, message_id: op.message_id } }));
        assertCurrent();
        if (!response?.conversation?.mapping || !response.conversation.current_node) throw Error("No native branch draft");
        // This graph's ID is a preparation ID, not a persisted chat. The
        // official client submits branching_from_* on the first completion.
        t.result.branch_response = response; t.status = "prepared";
      } else if (["create_chat", "send_message"].includes(op.type)) await t.generate();
      else throw { code: "OPERATION_UNAVAILABLE" };
    }).catch(failed);
    return publicTask(t);
  };
}
module.exports = { installExperiments };
