"use strict";

const MAX_BYTES = 64 * 1024 * 1024;
function jsonLines(stream, onMessage, onInvalid) {
  let buffer = Buffer.alloc(0), invalid = false;
  const reject = () => { if (!invalid) { invalid = true; buffer = Buffer.alloc(0); onInvalid(); } };
  stream.on("data", chunk => {
    if (invalid) return;
    if (buffer.length + chunk.length > MAX_BYTES) return reject();
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      const at = buffer.indexOf(10);
      if (at < 0) break;
      const line = buffer.subarray(0, at); buffer = buffer.subarray(at + 1);
      if (!line.length) continue;
      let message;
      try { message = JSON.parse(line.toString("utf8")); } catch { reject(); return; }
      Promise.resolve(onMessage(message)).catch(reject);
    }
  });
  stream.on("end", () => { if (buffer.length) reject(); });
}
function send(stream, value) {
  const data = JSON.stringify(value) + "\n";
  if (Buffer.byteLength(data) > MAX_BYTES) throw Error("Conversation API frame exceeds 64 MiB; use a narrower retrieval.");
  stream.write(data);
}
module.exports = { MAX_BYTES, jsonLines, send };
