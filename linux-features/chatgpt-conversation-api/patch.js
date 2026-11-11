"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { installRenderer, installPreload } = require("./renderer.js");
const { extractedAppPatch } = require("../../scripts/patches/descriptor.js");
const { findMatchingBrace, escapeRegExp } = require("../../scripts/patches/lib/minified-js.js");

const ID = String.raw`[A-Za-z_$][\w$]*`;
const MARK = "/*hydex-chatgpt-conversation-api-v1*/";
const mainAnchor = new RegExp(String.raw`(?<trust>${ID})=(?<event>${ID})=>(?<registry>${ID})\.isTrustedIpcSender\(\k<event>\.sender,\k<event>\.senderFrame\?\?null\);`, "g");
const identityAnchor = new RegExp(String.raw`(?<scope>${ID})\.set\(${ID}\.${ID},\{accountId:[^{};]{0,120}accountInfoError:[^{};]{0,500}workCloudSupportedSurface:!0\}\)`, "g");

function chunkModules(source) {
  const marker = "export const __webpack_modules__={";
  const start = source.indexOf(marker);
  if (start < 0) throw Error("Conversation API drift: expected an rspack renderer chunk.");
  const header = /([A-Za-z0-9_$]+|"[^"\\]*")\(([A-Za-z_$][\w$]*(?:,[A-Za-z_$][\w$]*)*)?\)\{/y;
  const modules = [];
  let at = start + marker.length;
  while (source[at] !== "}") {
    header.lastIndex = at;
    const m = header.exec(source);
    if (!m) throw Error("Conversation API drift: invalid rspack module boundary.");
    const end = findMatchingBrace(source, at + m[0].length - 1);
    if (end < 0) throw Error("Conversation API drift: unbalanced rspack module.");
    modules.push({ id: m[1].replace(/^"|"$/g, ""), require: m[2]?.split(",")[2], start: at, end: end + 1, source: source.slice(at, end + 1) });
    at = end + 1;
    if (source[at] === ",") at++;
  }
  return modules;
}

function settingsOf(settings = {}) {
  const allowed = ["mutationsEnabled", "sharedProjectMovesEnabled", "minDelayMs", "maxOperations"];
  if (Object.keys(settings).some(k => !allowed.includes(k))) throw Error("Unknown conversation API setting.");
  for (const key of ["mutationsEnabled", "sharedProjectMovesEnabled"]) {
    if (settings[key] !== undefined && typeof settings[key] !== "boolean") throw Error(`${key} must be boolean.`);
  }
  if (settings.minDelayMs !== undefined && (!Number.isInteger(settings.minDelayMs) || settings.minDelayMs < 250 || settings.minDelayMs > 60000)) throw Error("minDelayMs must be between 250 and 60000.");
  if (settings.maxOperations !== undefined && (!Number.isInteger(settings.maxOperations) || settings.maxOperations < 1 || settings.maxOperations > 1000)) throw Error("maxOperations must be between 1 and 1000.");
  return { mutationsEnabled: false, sharedProjectMovesEnabled: false, minDelayMs: 500, maxOperations: 1000, ...settings };
}
function unique(source, regex, description) {
  const found = [...source.matchAll(regex)];
  if (found.length !== 1) throw Error(`Conversation API drift: expected one ${description}, found ${found.length}.`);
  return found[0];
}
function insert(source, anchor, extra) {
  const pos = anchor.index + anchor[0].length;
  const tail = source.slice(pos);
  if (tail.startsWith(extra) && source.split(MARK).length === 2) return source;
  if (source.includes(MARK)) throw Error("Conversation API patch is incomplete or uses different settings.");
  return source.slice(0, pos) + extra + tail;
}
function patchMain(source, settings) {
  const anchor = unique(source, mainAnchor, "trusted renderer IPC registration");
  const extra = MARK + `require(require("node:path").join(process.resourcesPath,"..",".codex-linux","features","chatgpt-conversation-api","desktop.js")).install({electron:require("electron"),isTrustedIpcEvent:${anchor.groups.trust},settings:${JSON.stringify(settingsOf(settings))}});`;
  return insert(source, anchor, extra);
}
function patchPreload(source) {
  if (!source.includes('contextBridge.exposeInMainWorld("electronBridge",') ||
      !source.includes("ipcRenderer.invoke(")) throw Error("Conversation API drift: native context-isolated preload is missing.");
  const extra = MARK + `(${installPreload.toString()})();\n`;
  if (source.endsWith(extra)) return source;
  if (source.includes(MARK)) throw Error("Conversation API preload is partially patched.");
  return source + "\n" + extra;
}
function patchRenderer(source) {
  const anchor = unique(source, identityAnchor, "authenticated ChatGPT account snapshot");
  const modules = chunkModules(source);
  const auth = modules.find(m => m.start <= anchor.index && m.end > anchor.index);
  const requests = modules.filter(m => m.source.includes("getChatGptRequestProfile:") && /Request:\(\)=>/.test(m.source));
  if (!auth || requests.length !== 1) throw Error("Conversation API drift: native request profile module is missing or ambiguous.");
  const current = unique(auth.source, new RegExp(String.raw`let (?<snapshot>${ID})=(?<scope>${ID})\.get\((?<account>${ID}\.${ID})\);\([^;]{0,180}\k<snapshot>\.authenticatedAccountId!==`, "g"), "native authenticated identity read");
  if (current.groups.scope !== anchor.groups.scope) throw Error("Conversation API drift: account scopes disagree.");
  const ns = current.groups.account.split(".")[0];
  const binding = unique(auth.source, new RegExp(String.raw`${escapeRegExp(ns)}=${escapeRegExp(auth.require)}\("(?<module>[^"\\]+)"\)`, "g"), "account module import");
  const accountModule = modules.find(m => m.id === binding.groups.module);
  // The account module can reside in another chunk, but its import and the
  // authenticatedAccountId/authenticatedUserId comparisons are proven here.
  if (accountModule && !accountModule.source.includes("authenticatedUserId")) throw Error("Conversation API drift: account identity contract changed.");
  for (const contract of ["/conversation/{conversation_id}", "/conversations/batch", "/gizmos/{gizmo_id}/conversations", "/gizmos/snorlax/sidebar",
    "/conversation/id/{conversation_id}/rename", "/conversations/{conversation_id}/messages", 'Request.safePost("/projects"', "ChatGPT-Account-ID"]) {
    if (!source.includes(contract)) throw Error(`Conversation API drift: missing native contract ${contract}.`);
  }
  if (!/exclude_conversation_origin:[^,{};]{1,120}"tpp"/.test(source)) throw Error("Conversation API drift: native Work-chat exclusion changed.");
  const { scope } = anchor.groups;
  // Capture the loader in its module factory. The account layout effect has a
  // local snapshot named like the minified require parameter in current Linux.
  const loader = "hydexConversationApiNativeRequestV1";
  const capture = `const ${loader}=()=>${auth.require}(${JSON.stringify(requests[0].id)}).Request;`;
  const bodyStart = auth.start + auth.source.indexOf("{") + 1;
  const captured = source.startsWith(capture, bodyStart);
  if (captured !== source.includes(MARK) || !captured && auth.source.includes(loader)) {
    throw Error("Conversation API request loader is incomplete or collides with a native binding.");
  }
  const extra = `,(${installRenderer.toString()})(${scope},${current.groups.account},${loader})${MARK}`;
  const next = insert(source, anchor, extra);
  return captured ? next : next.slice(0, bodyStart) + capture + next.slice(bodyStart);
}
function patchExtracted(extractedDir, { feature } = {}) {
  const build = path.join(extractedDir, ".vite", "build");
  const assets = path.join(extractedDir, "webview", "assets");
  const find = (dir, predicate) => fs.readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) return find(file, predicate);
    if (!entry.isFile() || !entry.name.endsWith(".js")) return [];
    const source = fs.readFileSync(file, "utf8");
    return predicate(source) ? [{ file, source }] : [];
  });
  const main = find(build, s => [...s.matchAll(mainAnchor)].length > 0);
  const renderer = find(assets, s => [...s.matchAll(identityAnchor)].length > 0);
  if (main.length !== 1 || renderer.length !== 1) throw Error("Conversation API drift: expected one native main and renderer bundle.");
  const preload = { file: path.join(build, "preload.js") };
  preload.source = fs.readFileSync(preload.file, "utf8");
  // Precompute every result before writing anything: drift leaves all bytes intact.
  const edits = [
    [main[0], patchMain(main[0].source, feature?.settings)],
    [preload, patchPreload(preload.source)],
    [renderer[0], patchRenderer(renderer[0].source)],
  ];
  const changed = edits.filter(([original, next]) => original.source !== next);
  for (const [original, next] of changed) fs.writeFileSync(original.file, next);
  return changed.length ? { changed: true } : { changed: false, alreadyApplied: true };
}

const descriptors = [extractedAppPatch({ id: "desktop-service", phase: "extracted-app:pre-webview",
  order: 20500, ciPolicy: "optional", apply: patchExtracted })];
module.exports = { MARK, settingsOf, chunkModules, patchMain, patchPreload, patchRenderer, patchExtracted, descriptors };
