"use strict";
// Source-level regression check. Uses the checkout's actual mobile feature,
// isolated from every other feature; never starts Codex or a real user service.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const { execFileSync } = require("node:child_process");

const repo = path.resolve(process.argv[2] || path.join(__dirname, "../.."));
const mobile = path.join(repo, "linux-features/remote-mobile-control");
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "codex-mobile-contract-"));
let passed = 0;
function check(label, callback) {
  callback();
  console.log(`ok ${++passed} - ${label}`);
}
function write(file, value, mode) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, value, mode == null ? undefined : { mode });
}
try {
  const isolated = path.join(tmp, "linux-features/remote-mobile-control");
  // Only this feature is copied. Sibling modules cannot accidentally satisfy
  // imports, staging paths or shared-transport references in the fixture.
  fs.cpSync(mobile, isolated, { recursive: true, dereference: true });
  check("mobile has no declared shared-server dependency", () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(isolated, "feature.json"), "utf8"));
    assert.equal(manifest.id, "remote-mobile-control");
    assert.deepEqual(manifest.requires || [], [], "New mobile requirements need review");
    assert.equal(manifest.entrypoints.patchDescriptors, "./patch.js");
    assert.equal(manifest.entrypoints.stageHook, "./stage.sh");
    assert.deepEqual(fs.readdirSync(path.join(tmp, "linux-features")), ["remote-mobile-control"]);
  });
  const mobilePatch = require(path.join(isolated, "patch.js"));
  const transform = mobilePatch.applyLinuxRemoteMobileAppServerRemoteControlPatch;
  const accepts = mobilePatch.hasLinuxRemoteMobileLocalAppServerRemoteControlPatch;
  assert.equal(typeof transform, "function");
  assert.equal(typeof accepts, "function");
  const plain = '"use strict";function launch(overrides){let base=[`-c`,`features.code_mode_host=true`];return overrides.length===0?[...base,`app-server`,`--analytics-default-enabled`]:[`app-server`,...base,...overrides,`--analytics-default-enabled`]}function trailing(){}';
  let patched;
  check("actual mobile transform patches an ordinary stdio launch without shared transport", () => {
    patched = transform(plain);
    assert.notEqual(patched, plain);
    assert.ok(accepts(patched));
    assert.equal(transform(patched), patched);
    assert.ok(!patched.includes("CodexLinuxSharedAppServerSocketTransport"));
    assert.ok(!patched.includes("CODEX_LINUX_APP_SERVER_BRIDGE_SOCKET"));
  });
  function argv(env, overrides = [], platform = "linux") {
    const context = vm.createContext({ process: { platform, env: { HOME: "/home/test", ...env } } });
    vm.runInContext(patched, context, { timeout: 1000 });
    return Array.from(context.launch(overrides));
  }
  const proxy = { CODEX_REMOTE_CONTROL_APP_SERVER_MODE: "proxy",
    CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET: "/home/test/.codex/control.sock" };
  check("proxy mode selects exactly app-server proxy --sock (no server spawn)", () => {
    assert.deepEqual(argv(proxy), ["-c", "features.code_mode_host=true", "app-server", "proxy", "--sock", proxy.CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET]);
    assert.deepEqual(argv(proxy, ["-c", "example=true"]), ["-c", "features.code_mode_host=true", "-c", "example=true", "app-server", "proxy", "--sock", proxy.CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET]);
  });
  check("home-relative socket expansion and original non-proxy behavior remain valid", () => {
    assert.equal(argv({ ...proxy, CODEX_REMOTE_CONTROL_APP_SERVER_PROXY_SOCKET: "%h/control.sock" }).at(-1), "/home/test/control.sock");
    assert.ok(argv({}).includes("--remote-control"));
    assert.deepEqual(argv(proxy, [], "darwin"), ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]);
  });
  const extracted = path.join(tmp, "work/app-extracted");
  const bundle = path.join(extracted, ".vite/build/plain-launch.js");
  check("registered mobile descriptor applies without a shared-feature descriptor", () => {
    write(bundle, plain);
    const descriptor = mobilePatch.find(d => d.id === "linux-remote-mobile-app-server-remote-control");
    assert.equal(descriptor.phase, "extracted-app:post-webview");
    assert.deepEqual(descriptor.apply(extracted), { matched: 1, changed: 1 });
    assert.ok(accepts(fs.readFileSync(bundle, "utf8")));
  });
  const install = path.join(tmp, "install");
  // Isolate process environment and ensure stage.sh's `node` finds this Node.
  const bin = path.join(tmp, "bin");
  fs.mkdirSync(bin);
  fs.symlinkSync(process.execPath, path.join(bin, "node"));
  const env = { HOME: tmp, PATH: bin + ":/usr/bin:/bin", SCRIPT_DIR: tmp,
    INSTALL_DIR: install, WORK_DIR: path.join(tmp, "work"),
    CODEX_LINUX_APP_DIR: install, CODEX_HOME: path.join(tmp, ".codex") };
  // Fake systemctl is ahead of the real executable. Fake Codex only records an
  // unexpected spawn; no real daemon or service manager is ever contacted.
  write(path.join(bin, "systemctl"), `#!/bin/sh
case "$2:$AUDIT_SYSTEMD_STATE" in
 is-active:active|is-enabled:configured|cat:configured) exit 0 ;;
 *) exit 1 ;;
esac
`, 0o755);
  const spawned = path.join(tmp, "unexpected-codex-spawn");
  write(path.join(install, "resources/codex"), '#!/bin/sh\nprintf "unexpected\\n" >> "$AUDIT_SPAWN_LOG"\nexit 99\n', 0o755);
  check("actual mobile stage hook installs its own markers/hook without shared files", () => {
    execFileSync("/bin/bash", [path.join(isolated, "stage.sh")], { env, timeout: 10000, stdio: "pipe" });
    assert.equal(fs.readFileSync(path.join(install, ".codex-linux/remote-mobile-control-enabled"), "utf8").trim(), "remote-mobile-control");
    assert.equal(fs.readFileSync(path.join(install, ".codex-linux/desktop-app-server-remote-control-enabled"), "utf8"), "version=1\nowner=desktop\n");
    assert.ok(fs.existsSync(path.join(install, ".codex-linux/cold-start.d/remote-mobile-control")));
    assert.ok(!fs.existsSync(spawned), "Stage hook attempted to start Codex");
  });
  for (const [state, disabled, expected] of [
    ["active", "0", "owner: systemd"],
    ["configured", "0", "owner: systemd"],
    ["missing", "1", "owner: disabled"],
  ]) {
    check(`actual cold-start hook defers (${state}, autostart-disabled=${disabled})`, () => {
      const text = execFileSync("/bin/bash", [path.join(isolated, "cold-start-hook.sh"), "--run-main"], {
        env: { ...env, AUDIT_SYSTEMD_STATE: state, AUDIT_SPAWN_LOG: spawned,
          CODEX_REMOTE_CONTROL_DAEMON_AUTOSTART_DISABLED: disabled,
          CODEX_REMOTE_CONTROL_APP_SERVER_MODE: "proxy" },
        encoding: "utf8", timeout: 10000,
      });
      assert.ok(text.includes(expected), text);
      assert.ok(!fs.existsSync(spawned), "Cold-start hook attempted to start another Codex");
    });
  }
  console.log(`${passed} source-level checks passed; no Electron, real Codex or phone session tested.`);
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}
