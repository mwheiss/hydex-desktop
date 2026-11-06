"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

class ApiError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.code = code;
    this.details = details;
  }
}
function fail(code, message, details) { throw new ApiError(code, message, details); }
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}
const digest = value => crypto.createHash("sha256").update(canonical(value)).digest("hex");
const same = (a, b) => canonical(a) === canonical(b);
function contextOf(value) {
  if (!value || !["user_id", "account_id", "workspace_id"].every(k =>
    typeof value[k] === "string" && /^[A-Za-z0-9@._:-]{1,200}$/.test(value[k]))) {
    fail("SESSION_UNAVAILABLE", "An authenticated ChatGPT user, account and workspace are required.");
  }
  return Object.fromEntries(["user_id", "account_id", "workspace_id"].map(k => [k, value[k]]));
}
function assertContext(actual, expected) {
  if (!same(contextOf(actual), contextOf(expected))) fail("CONTEXT_CHANGED", "The ChatGPT account or workspace changed.");
}
function identifier(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(value)) fail("INVALID_ARGUMENT", "Invalid resource ID.");
  return value;
}
function textValue(value, max = 500) {
  if (typeof value !== "string" || !value.trim() || value.length > max || /[\u0000-\u0008\u000b-\u001f\u007f]/.test(value)) {
    fail("INVALID_ARGUMENT", `Expected nonempty text of at most ${max} characters.`);
  }
  return value;
}
function integer(value, min, max, fallback) {
  if (value === undefined && fallback !== undefined) return fallback;
  if (!Number.isSafeInteger(value) || value < min || value > max) fail("INVALID_ARGUMENT", `Expected an integer between ${min} and ${max}.`);
  return value;
}
// Conversation prose is untrusted data, never authority. Credential fields are
// excluded even in raw mode; unknown ordinary fields and opaque reasoning stay.
function redact(value) {
  const redactions = [];
  const sensitive = /^(?:authorization|proxy[-_]authorization|cookies?|set[-_]cookie|access[-_]?token|refresh[-_]?token|id[-_]?token|session[-_]?token|client[-_]?secret|password)$/i;
  const visit = (v, at) => {
    if (Array.isArray(v)) return v.map((x, i) => visit(x, `${at}/${i}`));
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.entries(v).map(([k, x]) => {
        const p = `${at}/${k}`;
        if (sensitive.test(k)) { redactions.push(p); return [k, "[redacted]"]; }
        return [k, visit(x, p)];
      }));
    }
    if (typeof v === "string" && /^https?:\/\//i.test(v)) {
      try {
        const url = new URL(v);
        let changed = false;
        for (const k of [...url.searchParams.keys()]) {
          if (/^(?:token|sig|signature|x-amz-signature|x-goog-signature|access_token)$/i.test(k)) {
            url.searchParams.set(k, "[redacted]"); changed = true;
          }
        }
        if (url.username || url.password) { url.username = ""; url.password = ""; changed = true; }
        if (changed) { redactions.push(at); return url.toString(); }
      } catch {}
    }
    return v;
  };
  return { value: visit(value, ""), redactions };
}
function publicError(error) {
  // Do not forward upstream error bodies, request URLs, headers, or stacks.
  return error instanceof ApiError
    ? { code: error.code, message: error.message, details: error.details }
    : { code: "INTERNAL_ERROR", message: "The desktop conversation service failed. No credentials are included in this error." };
}
function privateDirectory(dir) {
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const stat = fs.lstatSync(dir);
  if (!stat.isDirectory() || stat.isSymbolicLink() || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o700) {
    fail("UNSAFE_PATH", "Conversation API directories must be owned by this user with mode 0700.");
  }
  return dir;
}
function privateFile(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.uid !== process.getuid() || (stat.mode & 0o777) !== 0o600) {
    fail("UNSAFE_PATH", "Conversation API files must be private regular files with mode 0600.");
  }
  return stat;
}
function atomicJson(file, value) {
  const dir = privateDirectory(path.dirname(file));
  if (fs.existsSync(file)) privateFile(file);
  const temp = path.join(dir, `.write-${crypto.randomUUID()}`);
  let fd;
  try {
    fd = fs.openSync(temp, "wx", 0o600);
    fs.writeFileSync(fd, JSON.stringify(value) + "\n");
    fs.fsyncSync(fd);
    fs.closeSync(fd); fd = undefined;
    fs.renameSync(temp, file);
    const parent = fs.openSync(dir, "r");
    try { fs.fsyncSync(parent); } finally { fs.closeSync(parent); }
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    if (fs.existsSync(temp)) fs.unlinkSync(temp);
  }
}
function readJson(file) { privateFile(file); return JSON.parse(fs.readFileSync(file, "utf8")); }
function runtimeDirectory(env = process.env) {
  const root = env.XDG_RUNTIME_DIR;
  if (!root || !path.isAbsolute(root)) fail("RUNTIME_UNAVAILABLE", "XDG_RUNTIME_DIR is required for the private desktop bridge.");
  const st = fs.lstatSync(root);
  if (!st.isDirectory() || st.isSymbolicLink() || st.uid !== process.getuid() || (st.mode & 0o077)) fail("UNSAFE_PATH", "XDG_RUNTIME_DIR is not private to this user.");
  return path.join(root, "hydex-chatgpt-conversation-api");
}

module.exports = { ApiError, fail, canonical, digest, same, contextOf, assertContext, identifier,
  textValue, integer, redact, publicError, privateDirectory, privateFile, atomicJson, readJson, runtimeDirectory };
