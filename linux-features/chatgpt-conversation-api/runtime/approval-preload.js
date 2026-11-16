"use strict";

const { contextBridge, ipcRenderer } = require("electron");
const nonce = process.argv.find(argument => argument.startsWith("--hydex-chatgpt-approval="))?.split("=")[1];
if (/^[a-f0-9-]{36}$/.test(nonce ?? "")) {
  const channel = `hydex:chatgpt-approval:${nonce}`;
  contextBridge.exposeInMainWorld("hydexApproval", {
    read: () => ipcRenderer.invoke(channel + ":read"),
    choose: (decision, plan_id, sha256) => {
      if (!["approve", "cancel"].includes(decision) || typeof plan_id !== "string" || typeof sha256 !== "string") return Promise.resolve(false);
      return ipcRenderer.invoke(channel + ":choose", { decision, plan_id, sha256 });
    },
  });
}
