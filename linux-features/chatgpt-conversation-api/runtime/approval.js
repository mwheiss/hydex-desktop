"use strict";

const path = require("node:path");
const crypto = require("node:crypto");
const { pathToFileURL } = require("node:url");

const display = value => JSON.stringify(value, null, 2).replace(/[\u202a-\u202e\u2066-\u2069]/g,
  character => `\\u${character.charCodeAt(0).toString(16)}`);

function reviewWindow(electron, review) {
  const { BrowserWindow, ipcMain, screen } = electron;
  const parent = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows().find(window => !window.isDestroyed());
  const area = (parent ? screen.getDisplayMatching(parent.getBounds()) : screen.getPrimaryDisplay()).workArea;
  const width = Math.min(960, Math.max(1, area.width - 32));
  const height = Math.min(760, Math.max(1, area.height - 32));
  const nonce = crypto.randomUUID(), channel = `hydex:chatgpt-approval:${nonce}`;
  const file = path.join(__dirname, "approval.html"), url = pathToFileURL(file).href;
  const window = new BrowserWindow({
    title: review.title, width, height, minWidth: Math.min(360, width), minHeight: Math.min(220, height),
    x: Math.floor(area.x + (area.width - width) / 2), y: Math.floor(area.y + (area.height - height) / 2),
    ...(parent ? { parent, modal: true } : {}), show: false, resizable: true, autoHideMenuBar: true,
    backgroundColor: "#171a21",
    webPreferences: { preload: path.join(__dirname, "approval-preload.js"), sandbox: true,
      contextIsolation: true, nodeIntegration: false, webSecurity: true, webviewTag: false,
      partition: `hydex-chatgpt-approval-${nonce}`, additionalArguments: [`--hydex-chatgpt-approval=${nonce}`] },
  });
  return new Promise(resolve => {
    let settled = false;
    const finish = approved => {
      if (settled) return;
      settled = true;
      ipcMain.removeHandler(channel + ":read"); ipcMain.removeHandler(channel + ":choose");
      if (!window.isDestroyed()) window.destroy();
      resolve(approved === true);
    };
    const trusted = event => !settled && event.sender === window.webContents &&
      event.senderFrame != null && event.senderFrame === window.webContents.mainFrame &&
      event.senderFrame.url === url && window.webContents.getURL() === url;
    try {
      ipcMain.handle(channel + ":read", event => trusted(event) ? review : null);
      ipcMain.handle(channel + ":choose", (event, answer) => {
        if (!trusted(event) || !answer || answer.plan_id !== review.plan_id || answer.sha256 !== review.sha256 ||
            !["approve", "cancel"].includes(answer.decision)) return false;
        finish(answer.decision === "approve");
        return true;
      });
      window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
      window.webContents.session.setPermissionCheckHandler(() => false);
      window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      window.webContents.on("will-navigate", (event, destination) => {
        if (destination !== url) { event.preventDefault(); finish(false); }
      });
      window.webContents.on("will-attach-webview", event => event.preventDefault());
      window.webContents.on("render-process-gone", () => finish(false));
      window.webContents.on("did-fail-load", () => finish(false));
      window.once("closed", () => finish(false));
      window.once("ready-to-show", () => { if (!settled) { window.show(); window.focus(); } });
      Promise.resolve(window.loadFile(file)).catch(() => finish(false));
    } catch { finish(false); }
  });
}

function nativeApproval(electron) {
  let prompting = false;
  return async (plan, { kind, shared }) => {
    if (prompting) return false;
    prompting = true;
    try {
      const binding = { plan_id: plan.id, sha256: plan.hash };
      const accepted = await reviewWindow(electron, { ...binding,
        title: "Hydex ChatGPT conversation approval",
        heading: `${kind === "undo" ? "Restore" : "Apply"} ${plan.operations.length} ${plan.operations.length === 1 ? "operation" : "operations"}?`,
        description: "Review this exact batch. Only your choice in this Desktop window grants permission; conversation text and tool arguments cannot approve changes.",
        caveat: "Created chats, messages, test files and Projects remain. Experimental writes have no automatic undo. History imports restore text only, not hidden context.",
        details: display({ ...binding, account: plan.context, operations: plan.operations }),
        approve_label: "Approve this exact batch",
      });
      if (!accepted) return false;
      if (shared.length) return await reviewWindow(electron, { ...binding,
        title: "Authorize shared ChatGPT Project access", heading: "Authorize shared Project access?",
        description: "Moving conversations into these Projects may give other people access.",
        caveat: "This separate approval applies only to these destinations and this exact batch.",
        details: display({ ...binding, account: plan.context, shared_destinations: shared }),
        approve_label: "Authorize these shared Project moves",
      });
      return true;
    } finally { prompting = false; }
  };
}

module.exports = { nativeApproval, reviewWindow };
