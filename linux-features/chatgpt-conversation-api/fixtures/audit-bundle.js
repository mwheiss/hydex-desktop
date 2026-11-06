#!/usr/bin/env node
"use strict";

// Read-only official-ASAR compatibility audit. This does not build an app or
// suppress the repository's required core patch acceptance gates.
const fs = require("node:fs");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");
const assert = require("node:assert/strict");
const { patchMain, patchPreload, patchRenderer, settingsOf } = require("../patch.js");

async function audit(file) {
  const fd = fs.openSync(file, "r");
  try {
    const header = Buffer.alloc(16); fs.readSync(fd, header, 0, 16, 0);
    const length = header.readUInt32LE(12), base = 8 + header.readUInt32LE(4);
    if (length > 64 * 1024 * 1024) throw Error("Unexpected ASAR header size.");
    const bytes = Buffer.alloc(length); fs.readSync(fd, bytes, 0, length, 16);
    const root = JSON.parse(bytes.toString("utf8"));
    const matches = { main: [], preload: [], renderer: [] };
    function walk(node, prefix = "") {
      for (const [name, record] of Object.entries(node.files ?? {})) {
        const relative = `${prefix}/${name}`;
        if (record.files) { walk(record, relative); continue; }
        if (record.unpacked || !name.endsWith(".js") || !/\/(?:\.vite\/build|webview\/assets)\//.test(relative)) continue;
        const data = Buffer.alloc(record.size); fs.readSync(fd, data, 0, record.size, base + Number(record.offset));
        const source = data.toString("utf8");
        if (relative === "/.vite/build/preload.js") matches.preload.push({ relative, source });
        if (/\b[A-Za-z_$][\w$]*=[A-Za-z_$][\w$]*=>[A-Za-z_$][\w$]*\.isTrustedIpcSender\(/.test(source)) matches.main.push({ relative, source });
        if (source.includes("getChatGptRequestProfile:") && source.includes("workCloudSupportedSurface:!0") && source.includes("/gizmos/snorlax/sidebar")) matches.renderer.push({ relative, source });
      }
    }
    walk(root);
    const results = [];
    for (const [kind, apply] of [["main", patchMain], ["preload", patchPreload], ["renderer", patchRenderer]]) {
      assert.equal(matches[kind].length, 1, `One official ${kind} contract required`);
      const { relative, source } = matches[kind][0];
      for (const settings of [settingsOf(), settingsOf({ mutationsEnabled: true, sharedProjectMovesEnabled: true })]) {
        const next = apply(source, settings);
        assert.notEqual(next, source); assert.equal(apply(next, settings), next);
        if (kind === "renderer") {
          const checked = spawnSync(process.execPath, ["--input-type=module", "--check"], { input: next, encoding: "utf8", maxBuffer: 8192 });
          assert.equal(checked.status, 0, checked.stderr);
        } else new vm.Script(next, { filename: relative });
      }
      results.push({ kind, path: relative, source_sha256: crypto.createHash("sha256").update(source).digest("hex"),
        default_and_mutating_settings: "applied", idempotent: true, syntax: "passed" });
    }
    const hash = crypto.createHash("sha256");
    for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
    return { source_asar_sha256: hash.digest("hex"), contracts: results,
      scope: "Feature-only compatibility audit; required core patch build gates remain enforced." };
  } finally { fs.closeSync(fd); }
}
if (require.main === module) {
  if (process.argv.length !== 3) { process.stderr.write("Usage: audit-bundle.js /path/to/verified/official/app.asar\n"); process.exitCode = 2; }
  else audit(process.argv[2]).then(r => process.stdout.write(JSON.stringify(r, null, 2) + "\n")).catch(e => { process.stderr.write(e.stack + "\n"); process.exitCode = 1; });
}
module.exports = { audit };
