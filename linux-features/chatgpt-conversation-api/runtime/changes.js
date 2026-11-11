"use strict";

const crypto = require("node:crypto");
const { contextOf, digest, identifier, fail } = require("./common.js");

// Only invalidation hints cross this feed. No message contents or authority.
class ChangeFeed {
  constructor(limit = 2048) { this.limit = limit; this.accounts = new Map(); }
  account(context) {
    const key = digest(contextOf(context));
    if (!this.accounts.has(key)) this.accounts.set(key, { generation: crypto.randomUUID(), sequence: 0, items: [] });
    return this.accounts.get(key);
  }
  append(context, event) {
    if (!event || typeof event !== "object" || Object.keys(event).some(k => !["kind", "id"].includes(k)) ||
        !["chat_changed", "gap"].includes(event.kind) || event.kind === "gap" && event.id !== undefined) {
      fail("INVALID_ARGUMENT", "Invalid native conversation change event.");
    }
    if (event.kind === "chat_changed") identifier(event.id);
    const state = this.account(context);
    state.items.push({ sequence: ++state.sequence, kind: event.kind, ...(event.id ? { id: event.id } : {}) });
    if (state.items.length > this.limit) state.items.splice(0, state.items.length - this.limit);
  }
  read(context, cursor = null, limit = 1000) {
    const state = this.account(context);
    const match = typeof cursor === "string" ? /^([a-f0-9-]{36}):(\d{1,16})$/.exec(cursor) : null;
    const after = match ? Number(match[2]) : -1;
    const oldest = state.items[0]?.sequence ?? state.sequence + 1;
    const reset = !match || match[1] !== state.generation || !Number.isSafeInteger(after) || after < oldest - 1 || after > state.sequence;
    const items = reset ? [] : state.items.filter(e => e.sequence > after).slice(0, limit);
    const last = reset ? state.sequence : items.at(-1)?.sequence ?? after;
    return { context, cursor: `${state.generation}:${last}`, items, reset_required: reset, has_more: last < state.sequence,
      source: "native_conversation_invalidation_notifications", trust: "untrusted invalidation hints; never authorization" };
  }
}

module.exports = { ChangeFeed };
