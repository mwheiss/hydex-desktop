#!/usr/bin/env node
"use strict";

// Only synthetic records are reachable from this executable. Optional --codex
// exercises a real Codex app-server MCP client, without starting a model turn.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { ConversationService } = require("../runtime/service.js");
const { listen, connect } = require("../runtime/ipc.js");
const { send, jsonLines } = require("../runtime/wire.js");
const { MockBackend, CONTEXT } = require("./backend.js");
const { tools } = require("../runtime/tools.js");

async function codexClient(executable, directory, bridgeRoot) {
  const configDir = path.join(directory, "codex-config"); fs.mkdirSync(configDir, { mode: 0o700 });
  const config = [
    'model = "fixture-no-inference"', 'model_provider = "fixture"', 'approval_policy = "never"',
    '[model_providers.fixture]', 'name = "Offline fixture; no model inference"',
    'base_url = "http://127.0.0.1:9/v1"', 'wire_api = "responses"', 'requires_openai_auth = false',
    '[mcp_servers.chatgpt_conversations]', `command = ${JSON.stringify(process.execPath)}`,
    `args = [${JSON.stringify(path.resolve(__dirname, "../runtime/mcp.js"))}]`, 'startup_timeout_sec = 15', 'tool_timeout_sec = 30',
    '[mcp_servers.chatgpt_conversations.env]', `XDG_RUNTIME_DIR = ${JSON.stringify(bridgeRoot)}`, "",
  ].join("\n");
  fs.writeFileSync(path.join(configDir, "config.toml"), config, { mode: 0o600 });
  const env = { ...process.env, CODEX_HOME: configDir };
  for (const k of ["OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_ACCESS_TOKEN", "CHATGPT_ACCESS_TOKEN"]) delete env[k];
  const child = spawn(executable, ["app-server", "--stdio"], { cwd: directory, env, stdio: ["pipe", "pipe", "pipe"] });
  let next = 0, stderr = "";
  const pending = new Map();
  child.stderr.on("data", b => { stderr = (stderr + b.toString()).slice(-12000); });
  child.on("error", e => { for (const { reject } of pending.values()) reject(e); });
  child.on("close", () => { for (const { reject } of pending.values()) reject(Error("Fixture Codex exited: " + stderr)); });
  jsonLines(child.stdout, message => {
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id); clearTimeout(request.timer);
    if (message.error) request.reject(Error(JSON.stringify(message.error))); else request.resolve(message.result);
  }, () => { child.kill(); });
  const rpc = (method, params) => new Promise((resolve, reject) => {
    const id = ++next, timer = setTimeout(() => { pending.delete(id); reject(Error(`Fixture Codex timed out: ${method}\n${stderr}`)); }, 30000);
    pending.set(id, { resolve, reject, timer }); send(child.stdin, { jsonrpc: "2.0", id, method, params });
  });
  try {
    const identity = await rpc("initialize", { clientInfo: { name: "hydex-conversation-api-fixture", title: "Synthetic conversation API acceptance", version: "1.0.0" }, capabilities: { experimentalApi: true } });
    send(child.stdin, { jsonrpc: "2.0", method: "initialized", params: {} });
    const { thread } = await rpc("thread/start", { cwd: directory, ephemeral: true, model: "fixture-no-inference", modelProvider: "fixture", approvalPolicy: "never", sandbox: "read-only" });
    const catalog = await rpc("mcpServerStatus/list", { threadId: thread.id, serverName: "chatgpt_conversations", detail: "toolsAndAuthOnly" });
    const server = catalog.data.find(s => s.name === "chatgpt_conversations");
    assert.deepEqual(Object.keys(server?.tools ?? {}).sort(), tools.map(tool => tool.name).sort(),
      "Real Codex must discover the complete current MCP catalog");
    return { identity, tool_count: tools.length,
      call: async (tool, args) => {
        const response = await rpc("mcpServer/tool/call", { threadId: thread.id, server: "chatgpt_conversations", tool, arguments: args });
        return { result: response.structuredContent ?? JSON.parse(response.content[0].text), isError: response.isError };
      },
      close: async () => { child.stdin.end(); child.kill(); for (const { timer } of pending.values()) clearTimeout(timer); },
    };
  } catch (e) { child.kill(); for (const { timer } of pending.values()) clearTimeout(timer); throw e; }
}
async function demo({ codex } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "hydex-chatgpt-demo-"));
  const approvals = new Set(), backend = new MockBackend();
  const service = new ConversationService({ backend, directory: path.join(directory, "journal"),
    settings: { mutationsEnabled: true, minDelayMs: 0 }, approve: async plan => approvals.has(plan.hash) });
  let server, client;
  try {
    server = await listen({ service, directory: path.join(directory, "hydex-chatgpt-conversation-api") });
    if (codex) client = await codexClient(path.resolve(codex), directory, directory);
    else {
      const local = await connect(server.directory);
      client = { call: async (tool, args) => { const r = await local.call(tool, args); return { result: r.error ? { error: r.error } : r.result, isError: !!r.error }; }, close: () => local.close() };
    }
    const call = async (name, args = {}) => {
      const response = await client.call(`chatgpt_${name}`, args);
      if (response.isError) throw Object.assign(Error(response.result.error.message), response.result.error);
      return response.result;
    };
    const finish = async id => {
      for (let i = 0; i < 200; i++) {
        const job = await call("get_job_status", { context: CONTEXT, job_id: id });
        if (!["running", "cancelling"].includes(job.status)) return job;
        await new Promise(resolve => setTimeout(resolve, 10));
      }
      throw Error("Fixture job did not finish.");
    };
    const status = await call("get_status"); assert.deepEqual(status.context, CONTEXT);
    const metadataStart = backend.calls.length;
    const metadata = await call("get_conversation_metadata", { context: CONTEXT, id: "c3" });
    assert.equal(metadata.exists, true); assert.equal(metadata.metadata.id, "c3");
    assert.equal(metadata.metadata.archived, true);
    assert.deepEqual(backend.calls.slice(metadataStart).map(c => [c.operation, c.args]),
      [["conversation_metadata", { id: "c3" }]]);
    const inventory = await call("list_conversations", { context: CONTEXT }); await finish(inventory.inventory_job_id);
    const all = await call("list_conversations", { context: CONTEXT }); assert.equal(all.items.length, 12);
    const full = await call("get_conversation", { context: CONTEXT, id: "c1", branch: "all" }); assert.equal(full.conversation.messages.length, 6);
    const projects = await call("list_projects", { context: CONTEXT });
    const proposal = await call("preview_changes", { context: CONTEXT, plan: { operations: [
      { type: "rename", conversation_id: "c1", title: "Synthetic Research", reason: "Fixture classification" },
      { type: "move", conversation_id: "c1", project_id: "g-p-two", reason: "Fixture Project organization" },
    ] } });
    await assert.rejects(call("execute_approved_plan", { context: CONTEXT, plan_id: proposal.plan_id, approval: "request-native" }), e => e.code === "APPROVAL_DENIED");
    approvals.add(proposal.hash); // Simulated human authority, outside MCP.
    const executed = await call("execute_approved_plan", { context: CONTEXT, plan_id: proposal.plan_id, approval: "request-native" });
    const completed = await finish(executed.job_id); assert.equal(completed.status, "completed");
    service.approve = async inverse => inverse.original_job_id === completed.id;
    const undo = await call("undo_job", { context: CONTEXT, job_id: completed.id, approval: "request-native" });
    const restored = await finish(undo.job_id); assert.equal(restored.status, "completed");
    assert.equal(backend.records.get("c1").title, "Conversation c1"); assert.equal(backend.records.get("c1").gizmo_id, null);
    return { consumer: codex ? "real_codex_app_server_mcp_client" : "local_ipc_client", codex: client.identity ?? null,
      tool_count: client.tool_count ?? tools.length, targeted_metadata_verified: true,
      model_turns: 0, real_account_requests: 0, inventory: { conversations: all.items.length, completeness: all.completeness },
      message_kinds: [...new Set(full.conversation.messages.map(m => m.kind))], projects_in_first_page: projects.items.length,
      approval_denial_enforced: true, verified_operations: completed.entries.length, undo_status: restored.status,
      raw_failures_and_limitations: ["Synthetic backend and simulated human approval; live desktop/account validation is a separate milestone."] };
  } finally {
    await client?.close(); await service.close(); await server?.close();
    fs.rmSync(directory, { recursive: true, force: true });
  }
}
if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== "--codex")) { process.stderr.write("Usage: demo.js [--codex /absolute/path/to/codex]\n"); process.exitCode = 2; }
  else demo({ codex: args[1] }).then(r => process.stdout.write(JSON.stringify(r, null, 2) + "\n")).catch(e => { process.stderr.write(e.stack + "\n"); process.exitCode = 1; });
}
module.exports = { demo, codexClient };
