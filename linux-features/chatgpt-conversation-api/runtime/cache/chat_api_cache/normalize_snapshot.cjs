"use strict";
// Pure local transformation using the exact normalizer shipped with the native API.
// No backend connection, JavaScript evaluation of content, or attachment retrieval.
const fs = require("node:fs");
const path = require("node:path");
try {
  const { raw, branch, projection, snapshot_sha256 } = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!raw.complete) throw Object.assign(new Error("The saved raw response is incomplete; inspect it in raw mode."), { code: "INCOMPLETE_CONVERSATION" });
  const pages = raw.pages;
  // The current native API fetches only older pages and explicitly cannot fetch
  // newer pages. For complete snapshots its original header is the final page.
  const header = pages.at(-1);
  if (header?.page_info?.has_next_page === true) throw Object.assign(new Error("Unsupported newer-page coverage."), { code: "UNSUPPORTED_PAGINATION" });
  const { normalize } = require(path.resolve(process.argv[2]));
  const conversation = normalize(pages, { branch, complete: raw.complete, header });
  if (projection === "index_text") {
    const messages = [];
    for (const m of conversation.messages) {
      if (!["user", "assistant"].includes(m.role) || m.visible === false ||
          !["user_message", "assistant_reply"].includes(m.kind) || ![null, "all"].includes(m.recipient) ||
          !["text", "multimodal_text", "code"].includes(m.content_type)) continue;
      const c = m.content;
      const text = Array.isArray(c.parts) ? c.parts.flatMap(p => typeof p === "string" ? [p] :
        p && ["text", "input_text", "output_text"].includes(p.content_type ?? p.type) && typeof p.text === "string" ? [p.text] : []).join("\n") : c.text;
      if (typeof text !== "string" || !text.trim()) continue;
      messages.push({ id: m.id, node_id: m.node_id, role: m.role, recipient: m.recipient, kind: m.kind,
        visible: m.visible, created_at: m.created_at, content_type: "text", content: { content_type: "text", parts: [text] } });
    }
    conversation.messages = messages;
    conversation.nodes = [];
    conversation.projection = { kind: "visible_message_text", source_snapshot_sha256: snapshot_sha256,
      note: "Indexing projection only; the full raw API response is retained separately." };
  }
  process.stdout.write(JSON.stringify({ conversation }));
} catch (error) {
  process.stdout.write(JSON.stringify({ error: { code: error.code ?? "CACHE_NORMALIZATION_FAILED", message: error.message } }));
}
