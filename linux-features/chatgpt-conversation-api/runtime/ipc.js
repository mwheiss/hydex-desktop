"use strict";

const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const crypto = require("node:crypto");
const { privateDirectory, privateFile, atomicJson, readJson, runtimeDirectory, fail, publicError } = require("./common.js");
const { dispatch } = require("./tools.js");
const { jsonLines, send } = require("./wire.js");

function processIdentity(pid) {
  try { const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8"); return stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19]; } catch { return null; }
}
const sameFile = (a, b) => a && b && a.dev === b.dev && a.ino === b.ino;
async function listen({ service, directory = runtimeDirectory() }) {
  privateDirectory(directory);
  const sock = path.join(directory, "desktop.sock"), lock = path.join(directory, "owner.json"), discovery = path.join(directory, "bridge.json");
  if (Buffer.byteLength(sock) > 100) fail("UNSAFE_PATH", "The Unix socket path is too long.");
  const owner = { pid: process.pid, start: processIdentity(process.pid), nonce: crypto.randomUUID() };
  if (!owner.start) fail("RUNTIME_UNAVAILABLE", "Cannot establish the desktop process identity.");
  for (let attempt = 0;; attempt++) {
    try {
      const fd = fs.openSync(lock, "wx", 0o600);
      try { fs.writeFileSync(fd, JSON.stringify(owner)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
      break;
    } catch (e) {
      if (e.code !== "EEXIST" || attempt) throw e;
      const identity = privateFile(lock), previous = readJson(lock);
      if (Number.isInteger(previous.pid) && processIdentity(previous.pid) === previous.start || fs.existsSync(`/proc/${previous.pid}`)) fail("ALREADY_RUNNING", "Another desktop owns the conversation bridge.");
      if (fs.existsSync(sock)) {
        const st = fs.lstatSync(sock);
        if (!st.isSocket() || st.uid !== process.getuid() || (st.mode & 0o077)) fail("UNSAFE_PATH", "Cannot reclaim an unsafe socket.");
        const live = await new Promise(resolve => {
          const c = net.createConnection(sock);
          const done = result => { clearTimeout(timer); c.destroy(); resolve(result); };
          const timer = setTimeout(() => done(true), 500);
          c.once("connect", () => done(true)); c.once("error", error => done(!["ENOENT", "ECONNREFUSED"].includes(error.code)));
        });
        if (live) fail("ALREADY_RUNNING", "The existing conversation socket is live.");
        if (!sameFile(st, fs.lstatSync(sock))) fail("UNSAFE_PATH", "The stale socket changed during cleanup.");
        fs.unlinkSync(sock);
      }
      if (!sameFile(identity, fs.lstatSync(lock))) fail("UNSAFE_PATH", "The stale owner record changed.");
      fs.unlinkSync(lock);
    }
  }
  const lockIdentity = fs.lstatSync(lock);
  const secret = crypto.randomBytes(32).toString("hex");
  const connections = new Set();
  const server = net.createServer(socket => {
    if (connections.size >= 8) { socket.destroy(); return; }
    connections.add(socket);
    let authenticated = false, pending = 0;
    const timer = setTimeout(() => socket.destroy(), 5000); timer.unref();
    socket.on("error", () => {});
    socket.on("close", () => { clearTimeout(timer); connections.delete(socket); });
    jsonLines(socket, async message => {
      if (!authenticated) {
        if (message?.version !== 1 || typeof message.auth !== "string" || message.auth.length !== secret.length ||
            !crypto.timingSafeEqual(Buffer.from(message.auth), Buffer.from(secret))) { socket.destroy(); return; }
        authenticated = true; clearTimeout(timer); send(socket, { authenticated: true }); return;
      }
      if (pending >= 8 || !message || typeof message.id !== "string" || message.id.length > 100) { socket.destroy(); return; }
      pending++;
      try { send(socket, { id: message.id, result: await dispatch(typeof service === "function" ? service() : service, message.tool, message.arguments ?? {}) }); }
      catch (e) { send(socket, { id: message.id, error: publicError(e) }); }
      finally { pending--; }
    }, () => socket.destroy());
  });
  let socketIdentity, discoveryIdentity;
  const cleanup = () => {
    for (const [file, identity] of [[sock, socketIdentity], [discovery, discoveryIdentity], [lock, lockIdentity]]) {
      try { if (sameFile(identity, fs.lstatSync(file))) fs.unlinkSync(file); } catch (e) { if (e.code !== "ENOENT") throw e; }
    }
  };
  try {
    if (fs.existsSync(sock)) fail("UNSAFE_PATH", "A socket already exists under the newly acquired owner.");
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(sock, resolve); });
    fs.chmodSync(sock, 0o600); socketIdentity = fs.lstatSync(sock);
    atomicJson(discovery, { version: 1, socket: sock, auth: secret, pid: owner.pid, start: owner.start });
    discoveryIdentity = fs.lstatSync(discovery);
    return { directory, close: async () => {
      for (const socket of connections) socket.destroy();
      await new Promise(resolve => server.close(resolve)); cleanup();
    } };
  } catch (e) { server.close(); cleanup(); throw e; }
}
async function connect(directory = runtimeDirectory()) {
  const st = fs.lstatSync(directory);
  if (!st.isDirectory() || st.isSymbolicLink() || st.uid !== process.getuid() || (st.mode & 0o777) !== 0o700) fail("UNSAFE_PATH", "Unsafe conversation bridge directory.");
  const record = readJson(path.join(directory, "bridge.json"));
  const expected = path.join(directory, "desktop.sock");
  if (record.version !== 1 || record.socket !== expected || typeof record.auth !== "string" || !/^[a-f0-9]{64}$/.test(record.auth) ||
      !Number.isInteger(record.pid) || record.pid < 1 || typeof record.start !== "string" || processIdentity(record.pid) !== record.start) fail("STALE_BRIDGE", "The desktop bridge discovery record is stale or invalid.");
  const socketStat = fs.lstatSync(expected);
  if (!socketStat.isSocket() || socketStat.uid !== process.getuid() || (socketStat.mode & 0o777) !== 0o600) fail("UNSAFE_PATH", "Unsafe conversation socket permissions.");
  const socket = net.createConnection(expected);
  const pending = new Map();
  let accepted = false;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { socket.destroy(); reject(Error("Desktop bridge authentication timed out.")); }, 5000);
    const stop = () => { clearTimeout(timer); reject(Error("Desktop bridge disconnected.")); };
    socket.once("error", stop); socket.once("close", stop);
    jsonLines(socket, message => {
      if (!accepted) {
        if (message?.authenticated !== true) { socket.destroy(); return; }
        accepted = true; clearTimeout(timer); resolve(); return;
      }
      const callback = pending.get(message.id);
      if (!callback) { socket.destroy(); return; }
      pending.delete(message.id); callback(message);
    }, () => socket.destroy());
    socket.once("connect", () => send(socket, { version: 1, auth: record.auth }));
  });
  socket.on("error", () => {});
  socket.on("close", () => {
    for (const [id, resolve] of pending) resolve({ id, error: { code: "DESKTOP_DISCONNECTED", message: "The desktop connection closed; check job status before retrying a mutation." } });
    pending.clear();
  });
  return { call: (tool, args) => new Promise(resolve => {
    if (socket.destroyed) { resolve({ error: { code: "DESKTOP_DISCONNECTED", message: "The desktop connection is closed." } }); return; }
    const id = crypto.randomUUID(); pending.set(id, resolve); send(socket, { id, tool, arguments: args });
  }), close: () => socket.destroy() };
}

module.exports = { processIdentity, listen, connect };
