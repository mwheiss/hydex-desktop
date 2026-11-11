"use strict";

const { digest } = require("./common.js");

class Pacing {
  constructor(journal, { minimum = 500, clock = Date.now } = {}) {
    this.journal = journal; this.minimum = minimum; this.clock = clock; this.states = new Map();
  }
  bucket(operation, read) {
    return !read ? "writes" : ["conversation", "conversation_modern", "conversation_metadata"].includes(operation) ? "bodies" : "metadata";
  }
  state(context) {
    const key = digest(context);
    if (!this.states.has(key)) {
      const saved = this.journal.pacing(key);
      this.states.set(key, saved ?? { cooldown_until_ms: 0, next_request_ms: 0, buckets: {} });
    }
    return this.states.get(key);
  }
  slot(context, operation, read = true) {
    const state = this.state(context), key = this.bucket(operation, read);
    const floor = key === "bodies" && this.minimum > 0 ? Math.max(2000, this.minimum) : this.minimum;
    const bucket = state.buckets[key] ??= { interval_ms: floor, next_request_ms: 0, limits: 0, successes: 0 };
    return { state, bucket, key };
  }
  readyAt(context, operation, read = true) {
    const { state, bucket } = this.slot(context, operation, read);
    return Math.max(state.cooldown_until_ms, state.next_request_ms, bucket.next_request_ms);
  }
  reserve(context, operation, read) {
    const { state, bucket } = this.slot(context, operation, read);
    state.next_request_ms = this.clock() + this.minimum;
    bucket.next_request_ms = this.clock() + bucket.interval_ms;
    this.save(context);
  }
  success(context, operation, read) {
    const { bucket } = this.slot(context, operation, read);
    if (++bucket.successes >= 12) {
      const floor = this.bucket(operation, read) === "bodies" && this.minimum > 0 ? Math.max(2000, this.minimum) : this.minimum;
      bucket.interval_ms = Math.max(floor, Math.floor(bucket.interval_ms * 0.85));
      bucket.limits = Math.max(0, bucket.limits - 1); bucket.successes = 0;
    }
    this.save(context);
  }
  failure(context, operation, read, error) {
    const { state, bucket } = this.slot(context, operation, read);
    const limited = error.code === "RATE_LIMITED" || error.details?.status === 429;
    const retry = error.details?.retry_at_ms;
    if (limited) {
      bucket.limits++; bucket.successes = 0;
      bucket.interval_ms = Math.min(60000, Math.max(1000, bucket.interval_ms * 2));
      // Retry-After is a minimum. Repeated limits also increase our own wait.
      const adaptive = Math.min(900000, 30000 * 2 ** Math.min(bucket.limits - 1, 5));
      state.cooldown_until_ms = Math.max(state.cooldown_until_ms, this.clock() + adaptive, Number.isFinite(retry) ? retry : 0);
      bucket.next_request_ms = Math.max(bucket.next_request_ms, state.cooldown_until_ms);
    } else if (Number.isFinite(retry) && retry > this.clock()) {
      state.cooldown_until_ms = Math.max(state.cooldown_until_ms, retry);
    }
    this.save(context);
    return this.readyAt(context, operation, read);
  }
  save(context) { this.journal.savePacing(digest(context), this.state(context)); }
  status(context) { return JSON.parse(JSON.stringify(this.state(context))); }
}

module.exports = { Pacing };
