"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { privateDirectory, atomicJson, readJson, identifier, fail, digest } = require("./common.js");

class Journal {
  constructor(directory) {
    this.directory = privateDirectory(directory);
    // A desktop crash never makes an in-flight mutation safe to repeat.
    for (const file of fs.readdirSync(directory).filter(n => /^job-[a-f0-9-]+\.json$/.test(n))) {
      const job = readJson(path.join(directory, file));
      if (["running", "cancelling"].includes(job.status)) {
        job.status = "interrupted";
        job.finished_at = new Date().toISOString();
        this.saveJob(job);
      }
    }
  }
  file(kind, id) { identifier(id); return path.join(this.directory, `${kind}-${id}.json`); }
  createPlan(context, operations, extra = {}) {
    const plan = { schema_version: 1, id: crypto.randomUUID(), context, operations, ...extra,
      created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 86400000).toISOString() };
    plan.hash = digest(plan);
    atomicJson(this.file("plan", plan.id), plan);
    return plan;
  }
  plan(id) {
    const plan = readJson(this.file("plan", id));
    const { hash, ...body } = plan;
    if (plan.id !== id || digest(body) !== hash) fail("PLAN_INTEGRITY", "The stored plan changed; preview a new plan.");
    return plan;
  }
  job(id) {
    const job = readJson(this.file("job", id));
    if (job.id !== id) fail("JOURNAL_INTEGRITY", "The stored job ID does not match.");
    return job;
  }
  saveJob(job) { atomicJson(this.file("job", job.id), job); }
  pacing(key) {
    const file = this.file("pacing", key);
    if (!fs.existsSync(file)) return null;
    const value = readJson(file);
    if (!value || !Number.isFinite(value.cooldown_until_ms) || !Number.isFinite(value.next_request_ms) || !value.buckets || typeof value.buckets !== "object") {
      fail("JOURNAL_INTEGRITY", "Stored native request pacing is invalid.");
    }
    for (const [name, bucket] of Object.entries(value.buckets)) {
      if (!["bodies", "metadata", "writes"].includes(name) || ![bucket.interval_ms, bucket.next_request_ms, bucket.limits, bucket.successes].every(Number.isFinite) || bucket.interval_ms < 0 || bucket.interval_ms > 60000) {
        fail("JOURNAL_INTEGRITY", "Stored native request pacing bucket is invalid.");
      }
    }
    return value;
  }
  savePacing(key, value) { atomicJson(this.file("pacing", key), value); }
  jobs() { return fs.readdirSync(this.directory).filter(n => /^job-[a-f0-9-]+\.json$/.test(n)).map(n => readJson(path.join(this.directory, n))); }
  newJob(context, kind, extra = {}) {
    const job = { schema_version: 1, id: crypto.randomUUID(), context, kind, status: "running",
      created_at: new Date().toISOString(), entries: [], ...extra };
    this.saveJob(job);
    return job;
  }
}

module.exports = { Journal };
