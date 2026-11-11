"use strict";

// Serialized into the authenticated application renderer by patch.js. No Node
// APIs, tokens, arbitrary URL input, or generic request entrypoint cross IPC.
function installRenderer(scope, accountAtom, getRequest) {
  const bridge = globalThis.hydexConversationBridge;
  if (!bridge) return;
  const slot = "__hydexConversationApiRendererV1";
  const identity = () => {
    const a = scope.get(accountAtom);
    // requiresAuth describes the provider's authentication requirement, not
    // whether the current user is signed in. OpenAI keeps it true after login.
    if (!["chatgpt", "chatgptAuthTokens"].includes(a.authMethod) || a.authLoading || a.accountLoading ||
        a.hasChatGptToken !== true || !a.userId || !a.authenticatedUserId || !a.accountId || !a.authenticatedAccountId ||
        a.accountId !== a.authenticatedAccountId) return null;
    // The native request principal can differ from the ChatGPT membership user
    // ID. Match Request.expectedIdentity, as the upstream request callers do.
    return { user_id: a.authenticatedUserId, account_id: a.authenticatedAccountId, workspace_id: a.accountId };
  };
  if (globalThis[slot]) {
    globalThis[slot].identity = identity;
    globalThis[slot].getRequest = getRequest;
    bridge.hello(identity()).catch(() => {});
    return;
  }
  const state = { identity, getRequest };
  const changed = event => {
    const context = state.identity();
    if (context && typeof bridge.changed === "function") bridge.changed(context, event).catch(() => {});
  };
  state.notify = event => {
    const id = event?.payload?.conversation_id;
    if (["conversation-history-update", "conversation-turn-complete"].includes(event?.type) &&
        typeof id === "string" && /^[A-Za-z0-9_-]{1,200}$/.test(id)) changed({ kind: "chat_changed", id });
  };
  state.notifyGap = () => changed({ kind: "gap" });
  globalThis[slot] = state;
  const same = (a, b) => a && b && ["user_id", "account_id", "workspace_id"].every(k => a[k] === b[k]);
  const error = (code, status, retryAtMs) => ({ code, status, retry_at_ms: Number.isFinite(retryAtMs) ? retryAtMs : null });
  bridge.onRequest(async request => {
    let response;
    let retryAtMs = null;
    try {
      const before = state.identity();
      if (!same(before, request.context)) throw error("CONTEXT_CHANGED");
      const a = request.args ?? {};
      const client = request.operation === "identity" ? null : state.getRequest();
      const assertRequestCurrent = () => { if (!same(before, state.identity())) throw error("CONTEXT_CHANGED"); };
      const options = extra => ({ ...extra, additionalHeaders: { "ChatGPT-Account-ID": before.workspace_id },
        expectedIdentity: { accountId: before.workspace_id, userId: before.user_id }, assertRequestCurrent,
        retry: "never",
        onResponseHeadersReceived: headers => {
          const value = headers.get?.("Retry-After");
          if (value != null) retryAtMs = /^\d+(\.\d+)?$/.test(value) ? Date.now() + Number(value) * 1000 : Date.parse(value);
        },
      });
      let result;
      switch (request.operation) {
        case "identity": result = before; break;
        case "list": result = await client.safeGet("/conversations", options({ parameters: { query: {
          offset: a.offset, limit: a.limit, order: "updated", is_archived: a.archived,
          hide_snorlax: false, exclude_conversation_origin: "tpp", expand: false,
        } } })); break;
        case "projects": result = await client.safeGet("/gizmos/snorlax/sidebar", options({ parameters: { query: {
          cursor: a.cursor ?? undefined, limit: a.limit, owned_only: false, conversations_per_gizmo: 0,
        } } })); break;
        case "project": result = await client.safeGet("/gizmos/{gizmo_id_or_short_url}", options({
          parameters: { path: { gizmo_id_or_short_url: a.id } },
        })); break;
        case "project_conversations": result = await client.safeGet("/gizmos/{gizmo_id}/conversations", options({
          parameters: { path: { gizmo_id: a.id }, query: { cursor: a.cursor ?? undefined, limit: a.limit, owned_only: false } },
        })); break;
        case "conversation":
          if (a.page && a.page.direction !== "older") throw error("UNSUPPORTED_PAGINATION");
          result = await client.safeGet(a.page ? "/conversations/{conversation_id}/messages" : "/conversation/{conversation_id}", options({
            parameters: { path: { conversation_id: a.id }, ...(a.page ? { query: { before: a.page.cursor, num_turns: 100, include_has_versions: true } } : {}) },
          })); break;
        case "conversation_metadata": result = await client.safePost("/conversations/batch", options({
          requestBody: { conversation_ids: [a.id] },
        })); break;
        case "rename": result = await client.safePost("/conversation/id/{conversation_id}/rename", options({
          parameters: { path: { conversation_id: a.id } }, requestBody: { title: a.title },
        })); break;
        case "move": result = await client.safePatch("/conversation/{conversation_id}", options({
          parameters: { path: { conversation_id: a.id } }, requestBody: { gizmo_id: a.project_id ?? "" },
        })); break;
        case "archive": result = await client.safePatch("/conversation/{conversation_id}", options({
          parameters: { path: { conversation_id: a.id } }, requestBody: { is_archived: a.archived },
        })); break;
        case "create_project": result = await client.safePost("/projects", options({
          requestBody: { name: a.name, instructions: "", ...(a.memory_scope ? { memory_scope: a.memory_scope } : {}) },
        })); break;
        default: throw error("OPERATION_UNAVAILABLE");
      }
      if (!same(before, state.identity())) throw error("CONTEXT_CHANGED");
      response = { result, context: before };
    } catch (e) {
      // A native request error may contain auth headers or backend bodies.
      // Only enumerated codes and numeric retry information leave this scope.
      const codes = ["CONTEXT_CHANGED", "UNSUPPORTED_PAGINATION", "OPERATION_UNAVAILABLE"];
      const status = Number(e.status ?? e.httpStatus ?? e.responseStatus);
      response = { error: error(codes.includes(e.code) ? e.code : status === 401 || status === 403 ? "SESSION_EXPIRED"
        : status === 429 ? "RATE_LIMITED" : "BACKEND_ERROR", Number.isInteger(status) ? status : null, e.retryAtMs ?? retryAtMs) };
    }
    await bridge.respond(request.id, response).catch(() => {});
  });
  bridge.hello(identity()).catch(() => {});
  globalThis.addEventListener("pagehide", () => bridge.hello(null).catch(() => {}), { once: true });
}

function installPreload() {
  const { contextBridge, ipcRenderer } = require("electron");
  const prefix = "hydex:chatgpt-conversation-api:v1:";
  let installed = false;
  contextBridge.exposeInMainWorld("hydexConversationBridge", {
    hello: context => ipcRenderer.invoke(prefix + "hello", context),
    respond: (id, response) => ipcRenderer.invoke(prefix + "response", id, response),
    changed: (context, event) => ipcRenderer.invoke(prefix + "changed", context, event),
    onRequest: callback => {
      if (installed) return;
      installed = true;
      ipcRenderer.on(prefix + "request", (_event, message) => callback(message));
    },
  });
}

module.exports = { installRenderer, installPreload };
