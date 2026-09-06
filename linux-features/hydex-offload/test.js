#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const {
  createPatchReport,
  enabledFeatureFailuresFromReport,
} = require("../../scripts/lib/patch-report.js");
const {
  applyWebviewAssetPatchDescriptors,
  normalizePatchDescriptors,
} = require("../../scripts/patches/engine.js");
const {
  loadLinuxFeaturePatchDescriptors,
} = require("../../scripts/lib/linux-features.js");
const {
  CONTROL_MARKER,
  NEXT_TURN_MARKER,
  REQUEST_MARKER,
  applyHydexComposerControlPatch,
  applyHydexProductNamePatch,
  applyHydexRequestBridgePatch,
  descriptors,
  matchesHydexComposerContract,
  matchesHydexRequestBridgeContract,
} = require("./patch.js");

const stageHook = path.join(__dirname, "stage.sh");

function requestBridgeFixture() {
  return [
    "class AppServerClient{",
    "constructor(){this.dispatchMessage=()=>{}}",
    "sendConfigReadRequest(e,t){return{method:`config/read`,params:e,options:t}}",
    "enqueueRequest(e,t,n){return{method:e,params:t,options:n}}",
    "async sendRequest(e,t,n){if(this.dispatchMessage==null)throw Error(`AppServerRequestClient is missing a message dispatcher`);return e===`config/read`?this.sendConfigReadRequest(t,n):this.enqueueRequest(e,t,n)}",
    "}",
  ].join("");
}

function localComposerFixture() {
  return [
    "function modelSelectionWrapper(e){let {modelSettings:baseSettings,setDefaultModelAndReasoningEffort:setBaseDefault,setModelAndReasoningEffort:setBaseModel,setModelAndReasoningEffortForNextTurn:setBaseNext}=useBaseModelSelection(e),selectModel=()=>{};",
    "return{modelSettings:baseSettings,setDefaultModelAndReasoningEffort:setBaseDefault,setModelAndReasoningEffort:setBaseModel,selectComposerModelAndReasoningEffort:selectModel}}",
    "function localModelPicker(e){let cache=(0,memo.c)(10),{allowAeonDraftModelSelection:allow,conversationId:conversation,hideLabel:hide,permissionsCwdOverride:cwd,permissionsHostId:host}=e,enabled=allow!==void 0&&allow,[view,setView]=(0,React.useState)(`simple`);",
    "let modelSelection=useModelSelection(props),{modelSettings:settings,selectComposerModelAndReasoningEffort:select,setDefaultModelAndReasoningEffort:setDefault,setModelAndReasoningEffort:setModel}=modelSelection,tooltip=`composer.intelligenceDropdown.tooltip`,trigger={\"data-codex-intelligence-trigger\":!0};",
    "let result;return cache[0]!==trigger?(result=(0,jsx.jsx)(jsx.Fragment,{children:(0,jsx.jsx)(Menu,{triggerButton:trigger})}),cache[0]=trigger,cache[1]=result):result=cache[1],result}",
    "function supportsPersistent(e){let{reasoningEffort:t}=e;return t===`persistent`}",
  ].join("");
}

function chatComposerFixture() {
  return [
    "function chatModelPicker({selectedModel:e}){",
    "let tooltip=`composer.intelligenceDropdown.tooltip`;",
    "return(0,jsx.jsx)(Menu,{\"data-codex-intelligence-trigger\":!0,model:e})",
    "}",
  ].join("");
}

function makeStorage(value, options = {}) {
  return {
    getItem(key) {
      assert.equal(key, "hydex.offloadOverride");
      if (options.throwOnRead) throw new Error("storage unavailable");
      return value;
    },
  };
}

function makeMutableStorage(initialValue = null) {
  let value = initialValue;
  return {
    getItem(key) {
      assert.equal(key, "hydex.offloadOverride");
      return value;
    },
    removeItem(key) {
      assert.equal(key, "hydex.offloadOverride");
      value = null;
    },
    setItem(key, nextValue) {
      assert.equal(key, "hydex.offloadOverride");
      value = nextValue;
    },
    value() {
      return value;
    },
  };
}

function evaluateRequestClient(source, storage) {
  return Function("localStorage", `${source};return AppServerClient;`)(storage);
}

function applyPatchTwice(patchFn, source) {
  const patched = patchFn(source);
  assert.notEqual(patched, source);
  assert.equal(patchFn(patched), patched);
  return patched;
}

function captureWarnings(callback) {
  const warnings = [];
  const originalWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    return { value: callback(), warnings };
  } finally {
    console.warn = originalWarn;
  }
}

function withTempDir(callback) {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "hydex-offload-feature-"));
  try {
    return callback(tempDir);
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

function withFeatureConfig(enabled, callback) {
  const originalConfig = process.env.CODEX_LINUX_FEATURES_CONFIG;
  return withTempDir((tempDir) => {
    const configPath = path.join(tempDir, "features.json");
    fs.writeFileSync(configPath, `${JSON.stringify({ enabled })}\n`);
    process.env.CODEX_LINUX_FEATURES_CONFIG = configPath;
    try {
      return callback(path.resolve(__dirname, ".."));
    } finally {
      if (originalConfig == null) delete process.env.CODEX_LINUX_FEATURES_CONFIG;
      else process.env.CODEX_LINUX_FEATURES_CONFIG = originalConfig;
    }
  });
}

function hydexDescriptors(featuresRoot) {
  return normalizePatchDescriptors(
    loadLinuxFeaturePatchDescriptors({ featuresRoot }).filter(
      (descriptor) => descriptor.featureId === "hydex-offload",
    ),
  );
}

function writeWebviewAsset(extractedDir, name, source) {
  const assetsDir = path.join(extractedDir, "webview", "assets");
  fs.mkdirSync(assetsDir, { recursive: true });
  fs.writeFileSync(path.join(assetsDir, name), source);
}

function writeFakeCodex(filePath, version, { hydex = false } = {}) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, [
    "#!/bin/sh",
    "case \"${1:-}\" in",
    `  --version) printf 'codex-cli ${version}\\n' ;;`,
    hydex
      ? "  --help) printf '%s\\n' '--offload' '--no-offload' ;;"
      : "  --help) printf '%s\\n' 'official codex' ;;",
    "  *) exit 0 ;;",
    "esac",
    "",
  ].join("\n"), { mode: 0o755 });
}

function runStageHook(root, hydexBinary) {
  const appDir = path.join(root, "app");
  const upstreamDir = path.join(root, "upstream");
  const toolDir = path.join(root, "tools");
  fs.mkdirSync(toolDir, { recursive: true });
  const fileTool = path.join(toolDir, "file");
  fs.writeFileSync(
    fileTool,
    "#!/bin/sh\nprintf '%s: ELF 64-bit LSB pie executable, x86-64, static-pie linked\\n' \"$1\"\n",
    { mode: 0o755 },
  );
  return childProcess.spawnSync("bash", [stageHook], {
    encoding: "utf8",
    env: {
      ...process.env,
      ARCH: "x86_64",
      CODEX_UPSTREAM_APP_DIR: upstreamDir,
      HYDEX_CLI_BINARY: hydexBinary,
      INSTALL_DIR: appDir,
      PATH: `${toolDir}:${process.env.PATH}`,
    },
  });
}

test("hydex-offload is self-contained and does not enable unrelated features", () => {
  withFeatureConfig(["hydex-offload"], (featuresRoot) => {
    assert.deepEqual(
      [...new Set(loadLinuxFeaturePatchDescriptors({ featuresRoot }).map(
        (descriptor) => descriptor.featureId,
      ))],
      ["hydex-offload"],
    );
  });
});

test("feature loader exposes both enforced webview surfaces", () => {
  withFeatureConfig(["hydex-offload"], (featuresRoot) => {
    const loaded = hydexDescriptors(featuresRoot);

    assert.deepEqual(
      loaded.map((descriptor) => [
        descriptor.id,
        descriptor.phase,
        descriptor.ciPolicy,
        descriptor.enforceWhenEnabled,
      ]),
      [
        [
          "feature:hydex-offload:hydex-desktop-product-name",
          "extracted-app:pre-webview",
          "optional",
          true,
        ],
        ["feature:hydex-offload:hydex-offload-request-bridge", "webview-asset", "optional", true],
        ["feature:hydex-offload:hydex-offload-composer-control", "webview-asset", "optional", true],
      ],
    );
  });
});

test("Hydex patcher changes only the visible Electron product name", () => {
  withTempDir((root) => {
    const packagePath = path.join(root, "package.json");
    const original = {
      name: "@openai/codex",
      productName: "Codex",
      version: "26.901.51231",
      description: "Codex",
    };
    fs.writeFileSync(packagePath, `${JSON.stringify(original, null, 2)}\n`);

    assert.deepEqual(applyHydexProductNamePatch(root), {
      changed: true,
      target: "package.json",
    });
    assert.deepEqual(JSON.parse(fs.readFileSync(packagePath, "utf8")), {
      ...original,
      productName: "Hydex",
    });
    assert.deepEqual(applyHydexProductNamePatch(root), {
      changed: false,
      target: "package.json",
    });

    fs.writeFileSync(packagePath, '{"productName":"Unexpected"}\n');
    assert.throws(
      () => applyHydexProductNamePatch(root),
      /Expected Electron productName Codex/,
    );
  });
});

test("stage hook replaces and retains a version-matched Hydex CLI", () => {
  withTempDir((root) => {
    const appBinary = path.join(root, "app", "resources", "codex");
    const upstreamBinary = path.join(root, "upstream", "resources", "codex");
    const hydexBinary = path.join(root, "hydex", "codex");
    writeFakeCodex(appBinary, "0.153.1");
    writeFakeCodex(upstreamBinary, "0.153.1");
    writeFakeCodex(hydexBinary, "0.153.1", { hydex: true });

    const result = runStageHook(root, hydexBinary);

    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(fs.readFileSync(appBinary), fs.readFileSync(hydexBinary));
    assert.deepEqual(
      fs.readFileSync(path.join(root, "app", ".codex-linux", "features", "hydex-offload", "codex")),
      fs.readFileSync(hydexBinary),
    );
    const buildInfo = fs.readFileSync(
      path.join(root, "app", ".codex-linux", "features", "hydex-offload", "build-info"),
      "utf8",
    );
    assert.match(buildInfo, /^codex_version=0\.153\.1$/m);
    assert.match(buildInfo, /^original_codex_sha256=[a-f0-9]{64}$/m);
    assert.match(buildInfo, /^hydex_codex_sha256=[a-f0-9]{64}$/m);
  });
});

test("stage hook rejects a mismatched Hydex CLI without replacing the target", () => {
  withTempDir((root) => {
    const appBinary = path.join(root, "app", "resources", "codex");
    const upstreamBinary = path.join(root, "upstream", "resources", "codex");
    const hydexBinary = path.join(root, "hydex", "codex");
    writeFakeCodex(appBinary, "0.153.1");
    writeFakeCodex(upstreamBinary, "0.153.1");
    writeFakeCodex(hydexBinary, "0.153.0", { hydex: true });
    const original = fs.readFileSync(appBinary);

    const result = runStageHook(root, hydexBinary);

    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Hydex CLI version 0\.153\.0 does not match bundled Codex 0\.153\.1/);
    assert.deepEqual(fs.readFileSync(appBinary), original);
    assert.equal(
      fs.existsSync(path.join(root, "app", ".codex-linux", "features", "hydex-offload", "codex")),
      false,
    );
  });
});

test("stage hook shell syntax is valid", () => {
  assert.equal(childProcess.spawnSync("bash", ["-n", stageHook]).status, 0);
});

test("descriptors target the semantic current bundle owners", () => {
  assert.deepEqual(
    descriptors.map((descriptor) => [descriptor.id, descriptor.phase]),
    [
      ["hydex-desktop-product-name", "extracted-app:pre-webview"],
      ["hydex-offload-request-bridge", "webview-asset"],
      ["hydex-offload-composer-control", "webview-asset"],
    ],
  );
  assert.equal(descriptors[1].pattern.test("app-initial-c8dbea294abe.js"), true);
  assert.equal(descriptors[1].pattern.test("app-primary-7eef500906c5.js"), false);
  assert.equal(descriptors[2].pattern.test("app-primary-7eef500906c5.js"), true);
  assert.equal(descriptors[2].pattern.test("app-initial-c8dbea294abe.js"), false);
  assert.equal(matchesHydexRequestBridgeContract(requestBridgeFixture()), true);
  assert.equal(matchesHydexComposerContract(localComposerFixture()), true);
});

test("request bridge sends every Hydex override state on turn start", async () => {
  const patched = applyPatchTwice(applyHydexRequestBridgePatch, requestBridgeFixture());

  for (const [stored, expected] of [
    [null, null],
    ["force_on", "force_on"],
    ["force_off", "force_off"],
    ["invalid", null],
  ]) {
    const Client = evaluateRequestClient(patched, makeStorage(stored));
    const result = await new Client().sendRequest("turn/start", { threadId: "thread-1" });
    assert.deepEqual(result.params, {
      threadId: "thread-1",
      modelOffloadOverride: expected,
    });
  }

  const Client = evaluateRequestClient(patched, makeStorage(null, { throwOnRead: true }));
  const result = await new Client().sendRequest("turn/start", { threadId: "thread-1" });
  assert.equal(result.params.modelOffloadOverride, null);
});

test("request bridge supports flat and nested thread settings updates", async () => {
  const patched = applyHydexRequestBridgePatch(requestBridgeFixture());
  const Client = evaluateRequestClient(patched, makeStorage("force_on"));
  const client = new Client();

  const flat = await client.sendRequest("thread/settings/update", {
    effort: "high",
    threadId: "thread-1",
  });
  assert.deepEqual(flat.params, {
    effort: "high",
    modelOffloadOverride: "force_on",
    threadId: "thread-1",
  });

  const nested = await client.sendRequest("thread/settings/update", {
    threadId: "thread-1",
    threadSettings: { effort: "high" },
  });
  assert.deepEqual(nested.params, {
    threadId: "thread-1",
    threadSettings: {
      effort: "high",
      modelOffloadOverride: "force_on",
    },
  });
});

test("request bridge leaves unrelated app-server requests unchanged", async () => {
  const patched = applyHydexRequestBridgePatch(requestBridgeFixture());
  const Client = evaluateRequestClient(patched, makeStorage("force_off"));
  const params = { cursor: null };
  const result = await new Client().sendRequest("thread/list", params);

  assert.equal(result.params, params);
  assert.deepEqual(result.params, { cursor: null });
});

test("ambiguous request bridges warn and remain byte-identical", () => {
  const source = `${requestBridgeFixture()}${requestBridgeFixture().replaceAll("AppServerClient", "OtherClient")}`;
  const { value, warnings } = captureWarnings(() => applyHydexRequestBridgePatch(source));

  assert.equal(value, source);
  assert.deepEqual(warnings, [
    "WARN: Expected one app-server request bridge, found 2 - skipping Hydex offload request bridge patch",
  ]);
});

test("composer patch adds an authoritative local control and next-turn update", () => {
  const patched = applyPatchTwice(applyHydexComposerControlPatch, localComposerFixture());

  assert.match(patched, new RegExp(CONTROL_MARKER));
  assert.match(patched, /hydex\.offloadOverride/);
  assert.match(patched, /popover:`auto`/);
  assert.match(patched, /role:`menuitemradio`/);
  assert.match(
    patched,
    /rounded-xl bg-surface-elevated-secondary p-1 text-default shadow-lg ring-1 ring-border/,
  );
  assert.equal([...patched.matchAll(/"data-value":/g)].length, 3);
  assert.equal(patched.includes(")(`select`,{"), false);
  assert.match(
    patched,
    new RegExp(
      `selectComposerModelAndReasoningEffort:selectModel,\\/\\*${NEXT_TURN_MARKER}\\*\\/` +
      "setModelAndReasoningEffortForNextTurn:setBaseNext",
    ),
  );
  assert.match(
    patched,
    /onApply:conversation==null\?void 0:e=>modelSelection\.setModelAndReasoningEffortForNextTurn\(settings\.model,settings\.reasoningEffort,\{threadSettings:\{modelOffloadOverride:e\}\}\)/,
  );
  assert.match(patched, /let u=l===`auto`\?null:l/);
  assert.match(patched, /inline-flex min-w-0 items-center gap-1/);
  assert.doesNotThrow(() => new vm.Script(patched));
});

test("composer control renders an app-styled popover and persists choices", () => {
  const patched = applyHydexComposerControlPatch(localComposerFixture());
  const helperStart = patched.indexOf(`/*${CONTROL_MARKER}*/`);
  const helperEnd = patched.indexOf("function localModelPicker", helperStart);
  const helperSource = patched.slice(helperStart, helperEnd);
  const stateUpdates = [[], []];
  const refs = [];
  let stateSlot = 0;
  const React = {
    useState(initializer) {
      const slot = stateSlot++;
      const value = typeof initializer === "function" ? initializer() : initializer;
      return [value, (nextValue) => stateUpdates[slot].push(nextValue)];
    },
    useRef(initialValue) {
      const ref = { current: initialValue };
      refs.push(ref);
      return ref;
    },
  };
  const jsx = {
    jsx(type, props) {
      return { type, props };
    },
    jsxs(type, props) {
      return { type, props };
    },
  };
  const storage = makeMutableStorage();
  const documentState = { activeElement: null };
  const control = Function(
    "React",
    "jsx",
    "localStorage",
    "window",
    "document",
    "requestAnimationFrame",
    `${helperSource};return ${CONTROL_MARKER};`,
  )(
    React,
    jsx,
    storage,
    { innerHeight: 800, innerWidth: 1000 },
    documentState,
    (callback) => callback(),
  );
  const applied = [];
  const rendered = control({ onApply: (value) => applied.push(value) });

  assert.equal(rendered.type, "span");
  assert.equal(rendered.props.className, "relative inline-flex");
  const [trigger, menu] = rendered.props.children;
  assert.equal(trigger.type, "button");
  assert.equal(trigger.props["aria-haspopup"], "menu");
  assert.equal(trigger.props["aria-expanded"], false);
  assert.equal(trigger.props.children[0].props.children, "Hydex auto");
  assert.equal(menu.type, "div");
  assert.equal(menu.props.popover, "auto");
  assert.equal(menu.props.role, "menu");
  assert.deepEqual(menu.props.style, {
    inset: "auto",
    margin: 0,
    position: "fixed",
  });
  const options = menu.props.children;
  assert.deepEqual(
    options.map(({ type, props }) => [
      type,
      props.role,
      props["aria-checked"],
      props["data-value"],
      props.children[0],
    ]),
    [
      ["button", "menuitemradio", true, "auto", "Hydex auto"],
      ["button", "menuitemradio", false, "force_on", "Hydex on"],
      ["button", "menuitemradio", false, "force_off", "Hydex off"],
    ],
  );

  const rowFocus = [];
  const rows = options.map((_, index) => ({
    focus: () => rowFocus.push(index),
  }));
  documentState.activeElement = rows[0];
  let arrowPrevented = false;
  menu.props.onKeyDown({
    currentTarget: { querySelectorAll: () => rows },
    key: "ArrowDown",
    preventDefault: () => {
      arrowPrevented = true;
    },
  });
  assert.equal(arrowPrevented, true);
  assert.deepEqual(rowFocus, [1]);

  const focusLog = [];
  const popover = {
    open: false,
    style: {},
    hidePopover() {
      this.open = false;
    },
    matches(selector) {
      assert.equal(selector, ":popover-open");
      return this.open;
    },
    querySelector(selector) {
      assert.equal(selector, '[aria-checked="true"]');
      return { focus: () => focusLog.push("selected") };
    },
    showPopover() {
      this.open = true;
    },
  };
  refs[0].current = {
    focus: () => focusLog.push("trigger"),
    getBoundingClientRect: () => ({ left: 40, top: 500 }),
  };
  refs[1].current = popover;
  trigger.props.onClick();
  assert.equal(popover.open, true);
  assert.deepEqual(popover.style, { bottom: "308px", left: "40px" });
  menu.props.onToggle({ currentTarget: popover });

  options[1].props.onClick({ currentTarget: { dataset: { value: "force_on" } } });
  options[0].props.onClick({ currentTarget: { dataset: { value: "auto" } } });
  assert.deepEqual(stateUpdates, [["force_on", "auto"], [true]]);
  assert.deepEqual(applied, ["force_on", null]);
  assert.equal(storage.value(), null);
  assert.deepEqual(focusLog, ["selected", "trigger", "trigger"]);
});

test("Chat model picker is outside the Hydex composer contract", () => {
  const source = chatComposerFixture();

  assert.equal(matchesHydexComposerContract(source), false);
  assert.equal(captureWarnings(() => applyHydexComposerControlPatch(source)).value, source);
});

test("ambiguous local model pickers warn and remain byte-identical", () => {
  const source = `${localComposerFixture()}${localComposerFixture().replaceAll("localModelPicker", "otherLocalModelPicker")}`;
  const { value, warnings } = captureWarnings(() => applyHydexComposerControlPatch(source));

  assert.equal(value, source);
  assert.deepEqual(warnings, [
    "WARN: Expected one local Codex model picker, found 2 - skipping Hydex offload composer control patch",
  ]);
});

test("feature descriptors patch both extracted webview assets", () => {
  withFeatureConfig(["hydex-offload"], (featuresRoot) => {
    withTempDir((extractedDir) => {
      writeWebviewAsset(extractedDir, "app-initial-current.js", requestBridgeFixture());
      writeWebviewAsset(extractedDir, "app-primary-current.js", localComposerFixture());
      const report = createPatchReport();
      report.enabledFeatures = ["hydex-offload"];

      applyWebviewAssetPatchDescriptors(
        extractedDir,
        hydexDescriptors(featuresRoot),
        {},
        report,
      );

      assert.deepEqual(
        report.patches.map((entry) => [entry.name, entry.status]),
        [
          ["feature:hydex-offload:hydex-offload-request-bridge", "applied"],
          ["feature:hydex-offload:hydex-offload-composer-control", "applied"],
        ],
      );
      assert.match(
        fs.readFileSync(path.join(extractedDir, "webview", "assets", "app-initial-current.js"), "utf8"),
        new RegExp(REQUEST_MARKER),
      );
      assert.match(
        fs.readFileSync(path.join(extractedDir, "webview", "assets", "app-primary-current.js"), "utf8"),
        new RegExp(CONTROL_MARKER),
      );
      assert.deepEqual(enabledFeatureFailuresFromReport(report), []);
    });
  });
});

test("a missing enabled surface is reported as candidate-rejecting drift", () => {
  withFeatureConfig(["hydex-offload"], (featuresRoot) => {
    withTempDir((extractedDir) => {
      writeWebviewAsset(extractedDir, "app-initial-current.js", requestBridgeFixture());
      const report = createPatchReport();
      report.enabledFeatures = ["hydex-offload"];

      const { warnings } = captureWarnings(() => applyWebviewAssetPatchDescriptors(
        extractedDir,
        hydexDescriptors(featuresRoot),
        {},
        report,
      ));

      assert.ok(warnings.some((warning) => warning.includes("current local Codex model picker bundle")));
      assert.deepEqual(enabledFeatureFailuresFromReport(report), [
        {
          featureId: "hydex-offload",
          name: "feature:hydex-offload:hydex-offload-composer-control",
          reason: report.patches[1].reason,
          status: "skipped-optional",
        },
      ]);
    });
  });
});
