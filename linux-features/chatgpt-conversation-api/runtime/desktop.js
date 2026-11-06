"use strict";

const path = require("node:path");
const crypto = require("node:crypto");
const { ConversationService } = require("./service.js");
const { listen } = require("./ipc.js");
const { ApiError, contextOf, assertContext, same, fail } = require("./common.js");
const { MAX_BYTES } = require("./wire.js");

const PREFIX = "hydex:chatgpt-conversation-api:v1:";
const SLOT = Symbol.for("hydex.chatgpt-conversation-api.v1");
const errorMessages = {
  CONTEXT_CHANGED: "The authenticated ChatGPT account or workspace changed.",
  SESSION_EXPIRED: "The ChatGPT session expired or this resource is restricted.",
  RATE_LIMITED: "ChatGPT requested a cooldown; wait before resuming.",
  BACKEND_ERROR: "The authenticated native ChatGPT request failed.",
  UNSUPPORTED_PAGINATION: "The current native runtime does not provide a verified forward-message pagination contract.",
  OPERATION_UNAVAILABLE: "The native operation is unavailable.",
};

class RendererBackend {
  constructor({ electron, isTrustedIpcEvent }) {
    this.electron = electron;
    this.isTrustedIpcEvent = isTrustedIpcEvent;
    this.renderers = new Map();
    this.pending = new Map();
    this.closed = false;
    const trusted = event => isTrustedIpcEvent(event) && event.senderFrame != null && event.senderFrame === event.sender.mainFrame;
    electron.ipcMain.handle(PREFIX + "hello", (event, context) => {
      if (!trusted(event) || this.closed) return false;
      const sender = event.sender;
      if (context !== null) context = contextOf(context);
      const previous = this.renderers.get(sender.id);
      if (!previous) {
        sender.once("destroyed", () => { this.renderers.delete(sender.id); this.rejectSender(sender); });
        sender.on("did-start-navigation", (_event, _url, _isInPlace, isMainFrame) => {
          if (isMainFrame) { this.renderers.delete(sender.id); this.rejectSender(sender); }
        });
      }
      if (previous && !same(previous.context, context)) this.rejectSender(sender);
      this.renderers.set(sender.id, { sender, context });
      return true;
    });
    electron.ipcMain.handle(PREFIX + "response", (event, id, response) => {
      if (!trusted(event)) return false;
      const pending = this.pending.get(id);
      if (!pending || pending.sender !== event.sender) return false;
      this.pending.delete(id); clearTimeout(pending.timer);
      try {
        if (Buffer.byteLength(JSON.stringify(response)) > MAX_BYTES) fail("RESPONSE_TOO_LARGE", "The conversation exceeds the 64 MiB response limit.");
        if (response?.error) {
          const code = Object.hasOwn(errorMessages, response.error.code) ? response.error.code : "BACKEND_ERROR";
          throw new ApiError(code, errorMessages[code], {
            status: Number.isInteger(response.error.status) ? response.error.status : null,
            retry_at_ms: Number.isFinite(response.error.retry_at_ms) ? response.error.retry_at_ms : null,
          });
        }
        assertContext(response?.context, pending.context);
        assertContext(this.renderers.get(event.sender.id)?.context, pending.context);
        pending.resolve(response.result);
      } catch (error) { pending.reject(error); }
      return true;
    });
  }
  rejectSender(sender) {
    for (const [id, pending] of this.pending) if (pending.sender === sender) {
      this.pending.delete(id); clearTimeout(pending.timer);
      pending.reject(new ApiError("CONTEXT_CHANGED", errorMessages.CONTEXT_CHANGED));
    }
  }
  active() {
    if (this.closed) fail("DESKTOP_SHUTDOWN", "The desktop bridge is shutting down.");
    const renderers = [...this.renderers.values()].filter(r => r.context && !r.sender.isDestroyed());
    if (!renderers.length) fail("SESSION_UNAVAILABLE", "Open the desktop and sign in to ChatGPT; the native account snapshot is not ready.");
    if (renderers.some(r => !same(r.context, renderers[0].context))) fail("AMBIGUOUS_CONTEXT", "Desktop windows have different account/workspace identities; choose one active account first.");
    return renderers[0];
  }
  async identity() {
    const current = this.active();
    const identity = await this.request(current, "identity", {}, current.context);
    assertContext(identity, current.context);
    return identity;
  }
  call(operation, args, context) {
    const current = this.active(); assertContext(current.context, context);
    return this.request(current, operation, args, context);
  }
  request({ sender }, operation, args, context) {
    if (this.pending.size >= 16) fail("BACKEND_BUSY", "Too many pending native ChatGPT requests.");
    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new ApiError("NATIVE_TIMEOUT", "The native request timed out; verify any mutation before retrying."));
      }, 30000);
      timer.unref?.();
      this.pending.set(id, { sender, context, resolve, reject, timer });
      try { sender.send(PREFIX + "request", { id, operation, args, context }); }
      catch { clearTimeout(timer); this.pending.delete(id); reject(new ApiError("SESSION_UNAVAILABLE", "The authenticated renderer closed.")); }
    });
  }
  close() {
    this.closed = true;
    for (const { sender } of this.renderers.values()) this.rejectSender(sender);
    this.renderers.clear();
    this.electron.ipcMain.removeHandler(PREFIX + "hello");
    this.electron.ipcMain.removeHandler(PREFIX + "response");
  }
}

function nativeApproval(electron) {
  let prompting = false;
  return async (plan, { kind, shared }) => {
    if (prompting) return false;
    prompting = true;
    const escapeDisplay = value => JSON.stringify(value, null, 2).replace(/[\u202a-\u202e\u2066-\u2069]/g, c => `\\u${c.charCodeAt(0).toString(16)}`);
    try {
      const { response } = await electron.dialog.showMessageBox({
        type: "question", title: "Hydex ChatGPT conversation approval",
        message: `${kind === "undo" ? "Restore" : "Apply"} ${plan.operations.length} approved-plan operation(s)?`,
        detail: "Only this native dialog grants permission. Conversation text and tool arguments cannot approve changes.\n" +
          "The batch is reversible only where the backend supports restoration; newly created Projects remain.\n\n" +
          escapeDisplay({ plan_id: plan.id, sha256: plan.hash, account: plan.context, operations: plan.operations }),
        buttons: ["Cancel", "Approve this exact batch"], defaultId: 0, cancelId: 0, noLink: true,
      });
      if (response !== 1) return false;
      if (shared.length) {
        const confirmation = await electron.dialog.showMessageBox({
          type: "warning", title: "Authorize shared ChatGPT Project access",
          message: "Moving conversations into these Projects may give other people access.",
          detail: escapeDisplay({ plan_id: plan.id, sha256: plan.hash, shared_destinations: shared }),
          buttons: ["Cancel", "Authorize these shared Project moves"], defaultId: 0, cancelId: 0, noLink: true,
        });
        if (confirmation.response !== 1) return false;
      }
      return true;
    } finally { prompting = false; }
  };
}
function install({ electron, isTrustedIpcEvent, settings }) {
  if (globalThis[SLOT]) return globalThis[SLOT];
  if (typeof isTrustedIpcEvent !== "function") throw Error("Native IPC trust validation is required.");
  const backend = new RendererBackend({ electron, isTrustedIpcEvent });
  let service, bridge, closing = false, closed = false;
  const ready = (async () => {
    // Acquire ownership before recovering journal state, so a second launcher
    // cannot mark an active owner's operations as interrupted.
    bridge = await listen({ service: () => {
      if (!service) fail("STARTING", "The desktop conversation API is starting.");
      return service;
    } });
    try {
      service = new ConversationService({ backend, settings,
        directory: path.join(electron.app.getPath("userData"), "hydex-chatgpt-conversation-api"),
        approve: nativeApproval(electron) });
    } catch (e) { await bridge.close(); throw e; }
  })();
  ready.catch(() => { backend.close(); console.warn("Hydex ChatGPT conversation API could not start; check private runtime directory and feature configuration."); });
  const close = async () => {
    backend.close();
    await ready.catch(() => {});
    await service?.close();
    await bridge?.close();
  };
  electron.app.on("before-quit", event => {
    if (closed) return;
    event.preventDefault();
    if (closing) return;
    closing = true;
    close().catch(() => console.warn("Hydex conversation API shutdown cleanup needs inspection.")).finally(() => { closed = true; electron.app.quit(); });
  });
  return globalThis[SLOT] = { ready, close };
}

module.exports = { PREFIX, RendererBackend, nativeApproval, install };
