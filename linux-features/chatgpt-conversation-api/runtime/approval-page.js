"use strict";

async function approvalPage(document, api) {
  const element = id => document.getElementById(id);
  let review, submitted = false;
  const choose = async (event, decision) => {
    if (!event.isTrusted || !review || submitted) return;
    submitted = true;
    element("approve").disabled = true; element("cancel").disabled = true;
    try {
      if (await api.choose(decision, review.plan_id, review.sha256) !== true) throw Error("Rejected choice");
    } catch {
      element("status").textContent = "The approval window is no longer connected. Close it and request a fresh review.";
    }
  };
  element("cancel").addEventListener("click", event => choose(event, "cancel"));
  element("approve").addEventListener("click", event => choose(event, "approve"));
  document.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.preventDefault(); void choose(event, "cancel"); }
  });
  try {
    review = await api.read();
    if (!review || typeof review.details !== "string" || typeof review.plan_id !== "string" || typeof review.sha256 !== "string") throw Error("Missing review");
    for (const key of ["heading", "description", "caveat"]) element(key).textContent = review[key];
    element("batch").textContent = review.details;
    element("approve").textContent = review.approve_label;
    element("cancel").disabled = false; element("approve").disabled = false;
    element("cancel").focus();
  } catch {
    element("status").textContent = "This exact batch could not be loaded. Close this window; no approval was granted.";
  }
}

if (typeof module !== "undefined") module.exports = { approvalPage };
else void approvalPage(document, window.hydexApproval);
