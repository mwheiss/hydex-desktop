#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { matchesShellEnvironment } = require("../shell-env-startup/shell-env.js");

const {
  applyQuitConfirmationFocus,
  descriptors,
  patchRequiredCoreBlockers,
} = require("./patch.js");

// This shape comes from the signed official Linux main bundle. Keep the
// fixture independent of patch.js so upstream confirmation drift is visible.
const OFFICIAL_BUNDLE =
  "function qrt({isWindows:e,quitState:r,windows:i}){" +
  "let S=!1;l.app.on(`before-quit`,o=>{if(e||r.canQuitWithoutPrompt()){S=!0,i.markAppQuitting();return}" +
  "if(l.dialog.showMessageBoxSync({message:`Quit?`,buttons:[{messageId:`desktop.quitConfirmation.quit`},`Cancel`]})!==0){o.preventDefault();return}" +
  "r.markQuitApproved(),S=!0,i.markAppQuitting()})," +
  "l.app.on(`will-quit`,t=>{Promise.allSettled([flush(),save()]).then(()=>l.app.quit())})}" +
  "function next(){}";
const OFFICIAL_SHELL =
  "async function load(a,b){let started=Date.now();electron.app.isPackaged||clean();" +
  "let abort=new AbortController;logger(`Failed to load shell env`,{resultSource:`load`})}";

// Current 26.1027 registration: anonymous IIFE, quoted message IDs, nested
// functions before/inside the dialog, pending-worktree guard and ternary result.
const CURRENT_BUNDLE =
  'function({isWindows:e,disableQuitConfirmationPrompt:t,quitState:r,windows:i,getPendingWorktrees:d}){' +
  'let S=!1;l.app.on("before-quit",o=>{' +
  'if(function({platform:e="linux"}={}){return e==="darwin"}()){o.preventDefault();return}' +
  'let a=d()?.some(e=>"failed"!==e.phase&&e.conversationStartResult?.status!=="created")??!1;' +
  'if(e||r.canQuitWithoutPrompt()||t||!a){S=!0,i.markAppQuitting();return}' +
  '0!==l.dialog.showMessageBoxSync({message:"Quit?",buttons:[{messageId:"desktop.quitConfirmation.quit"},"Cancel"],' +
  'detail:function({pending:e}){return e?"pending":"idle"}({pending:a})})?' +
  'o.preventDefault():(r.markQuitApproved(),S=!0,i.markAppQuitting())}),' +
  'l.app.on("will-quit",t=>{Promise.allSettled([flush(),save()]).then(()=>l.app.quit())})}' +
  '({isWindows:params.isWindows,disableQuitConfirmationPrompt:false,quitState:params.quitState,' +
  'windows:params.windows,getPendingWorktrees:params.getPendingWorktrees})';

test("one required descriptor owns both portable core repairs", () => {
  assert.equal(descriptors.length, 1);
  assert.equal(descriptors[0].id, "quit-confirmation-focus");
  assert.equal(descriptors[0].phase, "extracted-app:pre-webview");
  assert.equal(descriptors[0].ciPolicy, "required-upstream");
  assert.equal(Object.hasOwn(descriptors[0], "appliesTo"), false);
});

test("required core descriptor validates every contract before writing", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "required-core-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const buildDir = path.join(root, ".vite", "build");
  fs.mkdirSync(buildDir, { recursive: true });
  const main = path.join(buildDir, "main-fixture.js");
  const shell = path.join(buildDir, "shell-fixture.js");
  fs.writeFileSync(main, OFFICIAL_BUNDLE);
  fs.writeFileSync(shell, OFFICIAL_SHELL.replace("Date.now()", "performance.now()"));

  assert.throws(() => patchRequiredCoreBlockers(root), /shell environment startup function/);
  assert.equal(fs.readFileSync(main, "utf8"), OFFICIAL_BUNDLE);
  fs.writeFileSync(shell, OFFICIAL_SHELL);
  assert.deepEqual(patchRequiredCoreBlockers(root), { changed: true });
  assert.match(fs.readFileSync(main, "utf8"), /codexLinuxQuitDialogParent/);
  assert.match(fs.readFileSync(shell, "utf8"), /await new Promise\(setImmediate\)/);
  assert.deepEqual(patchRequiredCoreBlockers(root), { changed: false });

  const duplicate = path.join(buildDir, "other-name.js");
  fs.copyFileSync(main, duplicate);
  const shellBefore = fs.readFileSync(shell);
  assert.throws(() => patchRequiredCoreBlockers(root), /exactly one official main-process Quit module/);
  assert.deepEqual(fs.readFileSync(shell), shellBefore);
});

test("required core descriptor composes both repairs in a consolidated module", (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "required-core-consolidated-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const buildDir = path.join(root, ".vite", "build");
  fs.mkdirSync(buildDir, { recursive: true });
  const combined = path.join(buildDir, "main-with-shell.js");
  fs.writeFileSync(combined, `${OFFICIAL_BUNDLE}\n${OFFICIAL_SHELL}`);

  assert.deepEqual(patchRequiredCoreBlockers(root), { changed: true });
  const patched = fs.readFileSync(combined, "utf8");
  assert.match(patched, /codexLinuxQuitDialogParent/);
  assert.match(patched, /await new Promise\(setImmediate\)/);
  assert.deepEqual(patchRequiredCoreBlockers(root), { changed: false });
});

function window(id, { destroyed = false, visible = true } = {}) {
  return {
    id,
    isDestroyed: () => destroyed,
    isVisible: () => visible,
  };
}

function harness({ focused = window(7), primary = window(3), others = [],
  currentBundle = false, pendingWorktrees = [{ phase: "pending" }] } = {}) {
  const handlers = new Map();
  const dialogCalls = [];
  const errors = [];
  let resolveDialog;
  let prevented = 0;
  let accepted = 0;
  let quitCalls = 0;
  let approved = false;
  const emitBeforeQuit = () => {
    const event = { preventDefault: () => { prevented += 1; } };
    for (const handler of handlers.get("before-quit") ?? []) {
      handler(event);
    }
  };
  const context = {
    Promise,
    console: { error: (...args) => errors.push(args) },
    require: (name) => {
      assert.equal(name, "electron");
      return {
        BrowserWindow: {
          getFocusedWindow: () => focused,
          getAllWindows: () => others,
        },
      };
    },
    l: {
      app: {
        on: (name, callback) => handlers.set(name, [...handlers.get(name) ?? [], callback]),
        quit: () => {
          quitCalls += 1;
          emitBeforeQuit();
        },
      },
      dialog: {
        showMessageBoxSync: () => {
          throw new Error("synchronous dialog must not run");
        },
        showMessageBox: (...args) => {
          dialogCalls.push(args);
          return new Promise((resolve) => { resolveDialog = resolve; });
        },
      },
    },
  };
  const register = vm.runInNewContext(
    currentBundle
      ? `(params)=>(${applyQuitConfirmationFocus(CURRENT_BUNDLE)})`
      : `${applyQuitConfirmationFocus(OFFICIAL_BUNDLE)};qrt`,
    context,
  );
  register({
    isWindows: false,
    getPendingWorktrees: () => pendingWorktrees,
    quitState: {
      canQuitWithoutPrompt: () => approved,
      markQuitApproved: () => { approved = true; },
    },
    windows: {
      windowManager: { getPrimaryWindow: () => primary },
      markAppQuitting: () => { accepted += 1; },
    },
  });
  return {
    answer: (response) => resolveDialog({ response }),
    dialogCalls,
    emitBeforeQuit,
    errors,
    get accepted() { return accepted; },
    get prevented() { return prevented; },
    get quitCalls() { return quitCalls; },
  };
}

const tick = () => new Promise(setImmediate);

test("patches only the official confirmation and is idempotent", () => {
  const patched = applyQuitConfirmationFocus(OFFICIAL_BUNDLE);
  assert.match(patched, /codexLinuxQuitDialogParent\(i\)/);
  assert.match(patched, /dialog\.showMessageBox\(codexLinuxParent,codexLinuxOptions\)/);
  assert.doesNotMatch(patched, /showMessageBoxSync\(/);
  assert.match(patched, /Promise\.allSettled\(\[flush\(\),save\(\)\]\)/);
  assert.equal(applyQuitConfirmationFocus(patched), patched);
  new vm.Script(patched);
});

test("fails closed when the official Quit confirmation changes or is ambiguous", () => {
  assert.throws(
    () => applyQuitConfirmationFocus(OFFICIAL_BUNDLE.replace("showMessageBoxSync", "showMessageBox")),
    /Expected one official synchronous Quit confirmation/,
  );
  assert.throws(
    () => applyQuitConfirmationFocus(OFFICIAL_BUNDLE.replace("quitState:r", "quitStateChanged:r")),
    /Expected exactly one official Quit registration function/,
  );
  assert.throws(
    () => applyQuitConfirmationFocus(OFFICIAL_BUNDLE.replace("markQuitApproved", "approveQuit")),
    /Expected one official synchronous Quit confirmation/,
  );
  assert.throws(
    () => applyQuitConfirmationFocus(`${OFFICIAL_BUNDLE}${OFFICIAL_BUNDLE}`),
    /Expected exactly one official Quit confirmation anchor/,
  );
});

test("parents the confirmation, prevents reentrancy, and resumes Quit on approval", async () => {
  const app = harness();
  app.emitBeforeQuit();
  assert.equal(app.prevented, 1);
  assert.equal(app.dialogCalls.length, 0);
  await tick();
  assert.equal(app.dialogCalls.length, 1);
  assert.equal(app.dialogCalls[0][0].id, 7);
  assert.equal(app.dialogCalls[0][1].message, "Quit?");

  app.emitBeforeQuit();
  assert.equal(app.prevented, 2);
  assert.equal(app.dialogCalls.length, 1);

  app.answer(0);
  await tick();
  assert.equal(app.quitCalls, 1);
  assert.equal(app.accepted, 1);
  assert.deepEqual(app.errors, []);
});

test("uses the visible primary window and allows a later Quit after cancellation", async () => {
  const app = harness({ focused: null });
  app.emitBeforeQuit();
  await tick();
  assert.equal(app.dialogCalls[0][0].id, 3);
  app.answer(1);
  await tick();
  assert.equal(app.quitCalls, 0);
  assert.equal(app.accepted, 0);

  app.emitBeforeQuit();
  await tick();
  assert.equal(app.dialogCalls.length, 2);
  app.answer(0);
  await tick();
  assert.equal(app.accepted, 1);
});

test("falls back to another visible live window", async () => {
  const app = harness({
    focused: window(2, { destroyed: true }),
    primary: window(3, { visible: false }),
    others: [window(4, { visible: false }), window(5)],
  });
  app.emitBeforeQuit();
  await tick();
  assert.equal(app.dialogCalls[0][0].id, 5);
  app.answer(1);
  await tick();
  assert.equal(app.accepted, 0);
});

test("current anonymous registration retains nested worktree detail and async approval", async () => {
  const patched = applyQuitConfirmationFocus(CURRENT_BUNDLE);
  assert.match(patched, /function\(\{[^}]+\}\)\{let codexLinuxQuitPromptPending/);
  assert.match(patched, /conversationStartResult\?\.status!=="created"/);
  assert.equal(applyQuitConfirmationFocus(patched), patched);
  new vm.Script(`(${patched})`);
  const app = harness({ currentBundle: true });
  app.emitBeforeQuit();
  app.emitBeforeQuit();
  await tick();
  assert.equal(app.prevented, 2);
  assert.equal(app.dialogCalls.length, 1);
  assert.equal(app.dialogCalls[0][1].detail, "pending");
  app.answer(0);
  await tick();
  assert.equal(app.quitCalls, 1);
  assert.equal(app.accepted, 1);
  assert.deepEqual(app.errors, []);
});

test("current registration preserves cancellation and completed-worktree shortcut", async () => {
  const app = harness({ currentBundle: true });
  app.emitBeforeQuit();
  await tick();
  app.answer(1);
  await tick();
  assert.equal(app.quitCalls, 0);
  assert.equal(app.accepted, 0);
  app.emitBeforeQuit();
  await tick();
  assert.equal(app.dialogCalls.length, 2);
  app.answer(1);
  await tick();
  const done = harness({ currentBundle: true,
    pendingWorktrees: [{ phase: "failed" }, { conversationStartResult: { status: "created" } }] });
  done.emitBeforeQuit();
  await tick();
  assert.equal(done.dialogCalls.length, 0);
  assert.equal(done.accepted, 1);
});

test("current registration fails closed on changed or ambiguous approval contracts", () => {
  assert.throws(() => applyQuitConfirmationFocus(CURRENT_BUNDLE.replace("0!==l.dialog", "1!==l.dialog")),
    /Expected one official synchronous Quit confirmation/);
  assert.throws(() => applyQuitConfirmationFocus(`${CURRENT_BUNDLE}${CURRENT_BUNDLE}`),
    /exactly one official Quit confirmation anchor/);
});

test("applies both required repairs to uniquely anchored signed campaign modules", {
  skip: process.env.CODEX_SIGNED_EXTRACTED_APP == null,
}, (t) => {
  const buildDir = path.join(process.env.CODEX_SIGNED_EXTRACTED_APP, ".vite", "build");
  const modules = fs.readdirSync(buildDir)
    .filter((name) => name.endsWith(".js"))
    .sort()
    .map((name) => ({
      name,
      source: fs.readFileSync(path.join(buildDir, name), "utf8"),
    }));
  const candidates = modules.filter(({ source }) =>
    /messageId:(["`])desktop\.quitConfirmation\.quit\1/.test(source));
  const shellCandidates = modules.filter(({ source }) =>
    matchesShellEnvironment(source));
  assert.equal(
    candidates.length,
    1,
    `expected one signed main-process Quit anchor, found: ${candidates.map(({ name }) => name).join(", ")}`,
  );
  assert.equal(
    shellCandidates.length,
    1,
    `expected one signed shell environment anchor, found: ${shellCandidates.map(({ name }) => name).join(", ")}`,
  );

  const source = candidates[0].source;
  const patched = applyQuitConfirmationFocus(source);
  assert.notEqual(patched, source);
  assert.match(patched, /function codexLinuxQuitDialogParent\(/);
  assert.match(patched, /codexLinuxQuitPromptPending=!0/);
  assert.equal(patched.split("showMessageBoxSync(").length,
    source.split("showMessageBoxSync(").length - 1);
  new vm.Script(patched);
  assert.equal(applyQuitConfirmationFocus(patched), patched);

  const root = fs.mkdtempSync(path.join(os.tmpdir(), "signed-required-core-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const fixtureBuild = path.join(root, ".vite", "build");
  fs.mkdirSync(fixtureBuild, { recursive: true });
  fs.writeFileSync(path.join(fixtureBuild, "renamed-main.js"), source);
  fs.writeFileSync(path.join(fixtureBuild, "renamed-shell.js"), shellCandidates[0].source);
  assert.deepEqual(patchRequiredCoreBlockers(root), { changed: true });
  assert.match(
    fs.readFileSync(path.join(fixtureBuild, "renamed-main.js"), "utf8"),
    /function codexLinuxQuitDialogParent\(/,
  );
  assert.match(
    fs.readFileSync(path.join(fixtureBuild, "renamed-shell.js"), "utf8"),
    /await new Promise\(setImmediate\)/,
  );
});
