"use strict";

// Serialized alongside the renderer bridge. Reuse the official high-level
// transport (requirements, integrity, attestation, stream decoding and uploads).
// No tokens, caller URLs, arbitrary headers or generic HTTP cross this boundary.
function installExperiments(getClients, getRequest, getIdentity) {
  const tasks = new Map();
  const same = (a, b) => a && b && ["user_id", "account_id", "workspace_id"].every(k => a[k] === b[k]);
  const publicTask = t => ({ task_id: t.id, status: t.status, result: { ...t.result }, ...(t.error ? { error: t.error } : {}) });
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
      if (task.status === "running") task.status = "uncertain";
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
    const t = { id: args.task_id, fingerprint, context: before, controller, status: "running", result: {} };
    tasks.set(t.id, t); // Register before dispatch: duplicate IPC cannot issue twice.
    const assertCurrent = () => { controller.signal.throwIfAborted(); if (!same(before, getIdentity())) throw { code: "CONTEXT_CHANGED" }; assertRequestCurrent(); };
    const failed = e => { t.status = "uncertain"; t.error = { code: e?.code === "CONTEXT_CHANGED" ? "CONTEXT_CHANGED" : "BACKEND_ERROR" }; };
    const capture = event => {
      const u = event?.update ?? event;
      const id = u?.conversationId ?? u?.conversation_id;
      if (typeof id === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(id)) t.result.conversation_id = id;
    };
    // Defer all network work until the task ID has been returned to main.
    Promise.resolve().then(async () => {
      assertCurrent();
      if (op.type === "import_chat") {
        const response = await getRequest().safePost("/data-import/conversation", options({ signal: controller.signal,
          requestBody: { source: "claude", conversation: op.history } }));
        assertCurrent();
        if (response?.conversation_id) t.result = { conversation_id: response.conversation_id, import_status: response.status, already_imported: response.already_imported === true };
        if (response?.status !== "success" || !response.conversation_id || response.already_imported) throw Error("Import did not create a new chat");
        t.status = "completed";
      } else if (op.type === "branch_chat") {
        const response = await getRequest().safePost("/conversation/new_branch", options({ signal: controller.signal,
          requestBody: { conversation_id: op.conversation_id, message_id: op.message_id } }));
        assertCurrent();
        const id = response?.conversation?.conversation_id ?? response?.conversation?.id;
        if (!id || id === op.conversation_id) throw Error("Branch did not return a new chat");
        t.result = { conversation_id: id }; t.status = "completed";
      } else if (["create_chat", "send_message"].includes(op.type)) {
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
        const request = { action: "next", ...(op.conversation_id ? { conversation_id: op.conversation_id } : {}),
          parent_message_id: op.parent_message_id, model: op.model,
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone, timezone_offset_min: new Date().getTimezoneOffset(),
          // Disable personalization for controlled experiments. Existing chats
          // may still have context predating this flag; the evaluator says so.
          is_do_not_remember: true,
          messages: [{ id: op.user_message_id, author: { role: "user" },
            content: { content_type: "text", parts: [op.text] }, metadata: attachments.length ? { attachments } : {} }] };
        if (op.conversation_id) t.result.conversation_id = op.conversation_id;
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
      } else throw { code: "OPERATION_UNAVAILABLE" };
    }).catch(failed);
    return publicTask(t);
  };
}
module.exports = { installExperiments };
