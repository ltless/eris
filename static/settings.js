/* Settings, as a dialog over the chat — see dialog.js for the shell. Loaded by
   the chat page and opened from the sidebar account row, so there is no route
   and no back control.

   Loaded as a module: it shares helper names with app.js, and module scope is
   what keeps them from colliding in the global object. */

import { createDialog } from "/dialog.js";

const $ = (id) => document.getElementById(id);
const els = {
  root: $("setModal"),
  opener: $("sideProfile"),
  avatar: $("avatar"),
  face: $("avatarFace"),
  cover: $("cover"),
  name: $("inpName"),
  nameMsg: $("nameMsg"),
  email: $("emailRow"),
  acctEmail: $("acctEmail"),
  fileImg: $("fileImg"),
  pwOld: $("pwOld"),
  pwNew: $("pwNew"),
  pwNew2: $("pwNew2"),
  pwMsg: $("pwMsg"),
  modelAvatar: $("modelAvatar"),
  modelFace: $("modelFace"),
  clearModel: $("clearModel"),
  selModel: $("selModel"),
  inpCtx: $("inpCtx"),
  chkThink: $("chkThink"),
  taPrompt: $("taPrompt"),
  resetPrompt: $("resetPrompt"),
  modelMsg: $("modelMsg"),
};

let mediaKind = "avatar";
let ready = false;

/* The chat page keeps its own copy of the name, the avatars, and the model
   picture, and nothing navigates any more, so it is told when one of them
   changes. app.js listens for this and repaints. */
function saved() {
  window.dispatchEvent(new Event("eris:settings-saved"));
}

function msg(el, text, ok) {
  el.hidden = false;
  el.textContent = text;
  el.className = "s-msg " + (ok ? "ok" : "err");
}

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) {
    // Transient 401s happen even when the cookie is valid (e.g. session table
    // not yet visible right after login). Record it so a failure can be
    // diagnosed, then throw — the caller decides whether to redirect.
    try {
      sessionStorage.setItem("eris-settings-last401", new Date().toISOString() + " " + url);
    } catch (_) {}
    throw new Error("Not signed in");
  }
  return r.json();
}

async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 401) throw new Error("Not signed in");
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
  return j;
}

// The model picture is a prefs key, not a users column, so it is painted from
// the prefs blob and the remove action only appears when there is something to
// remove.
function paintModel(url) {
  els.modelAvatar.style.backgroundImage = url ? `url("${url}")` : "";
  els.modelFace.textContent = url ? "" : "AI";
  els.clearModel.hidden = !url;
}

function paintProfile(u) {
  els.name.value = u.name || "";
  els.email.textContent = u.email || "";
  els.acctEmail.textContent = u.email || "";
  // The initials live in their own span so the picker hint survives the swap.
  els.face.textContent = u.avatar ? "" : (u.name || "ME").slice(0, 2).toUpperCase();
  els.avatar.style.backgroundImage = u.avatar ? `url("${u.avatar}")` : "";
  els.cover.style.backgroundImage = u.cover ? `url("${u.cover}")` : "";
  paintModel((u.prefs || {}).model_avatar || "");
}

/* ---------- theme ---------- */

function bindTheme() {
  const mode = window.ErisTheme ? window.ErisTheme.mode : "system";
  const input = document.querySelector(`input[name="theme"][value="${mode}"]`);
  if (input) input.checked = true;
  for (const r of document.querySelectorAll('input[name="theme"]')) {
    r.addEventListener("change", () => {
      if (r.checked && window.ErisTheme) window.ErisTheme.set(r.value);
    });
  }
}

/* ---------- forms ---------- */

function bind() {
  $("fName").addEventListener("submit", async (e) => {
    e.preventDefault();
    els.nameMsg.hidden = true;
    const name = els.name.value.trim();
    if (!name) return;
    try {
      paintProfile(await jpost("/api/auth/profile", { name }));
      msg(els.nameMsg, "saved", true);
      saved();
    } catch (err) {
      msg(els.nameMsg, err.message);
    }
  });

  // The pictures are the pickers; no separate buttons.
  els.avatar.addEventListener("click", () => {
    mediaKind = "avatar";
    els.fileImg.click();
  });
  els.cover.addEventListener("click", () => {
    mediaKind = "cover";
    els.fileImg.click();
  });
  els.modelAvatar.addEventListener("click", () => {
    mediaKind = "model";
    els.fileImg.click();
  });
  els.fileImg.addEventListener("change", async () => {
    const f = els.fileImg.files[0];
    els.fileImg.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f, f.name);
    // Media errors land in the panel the picked picture belongs to.
    const out = mediaKind === "model" ? els.modelMsg : els.nameMsg;
    try {
      const r = await fetch(`/api/auth/media?kind=${mediaKind}`, { method: "POST", body: fd });
      if (r.status === 401) throw new Error("Not signed in");
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      paintProfile(await jget("/api/auth/me"));
      if (mediaKind === "model") msg(out, "saved", true);
      saved();
    } catch (err) {
      msg(out, err.message);
    }
  });

  els.clearModel.addEventListener("click", async () => {
    els.modelMsg.hidden = true;
    try {
      const r = await fetch("/api/auth/media?kind=model", { method: "DELETE" });
      if (r.status === 401) throw new Error("Not signed in");
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      paintModel("");
      msg(els.modelMsg, "saved", true);
      saved();
    } catch (err) {
      msg(els.modelMsg, err.message);
    }
  });

  $("fPw").addEventListener("submit", async (e) => {
    e.preventDefault();
    els.pwMsg.hidden = true;
    if (els.pwNew.value !== els.pwNew2.value) {
      msg(els.pwMsg, "New passwords do not match.");
      return;
    }
    try {
      await jpost("/api/auth/password", { old: els.pwOld.value, new: els.pwNew.value });
      msg(els.pwMsg, "Password changed. This session stays signed in.", true);
      els.pwOld.value = els.pwNew.value = els.pwNew2.value = "";
    } catch (err) {
      msg(els.pwMsg, err.message);
    }
  });

  els.resetPrompt.addEventListener("click", () => {
    els.taPrompt.value = "";
    els.taPrompt.focus();
  });

  $("fModel").addEventListener("submit", async (e) => {
    e.preventDefault();
    els.modelMsg.hidden = true;
    // Nothing to save until the list arrived: an empty select would write an
    // empty model back over the user's choice.
    if (!ready) {
      msg(els.modelMsg, "Settings haven't finished loading. Close and reopen to try again.");
      return;
    }
    const ctx = parseInt(els.inpCtx.value, 10);
    const prefs = {
      model: els.selModel.value,
      thinking: els.chkThink.checked,
      ctx: Number.isNaN(ctx) ? 0 : ctx,
      system_prompt: els.taPrompt.value,
    };
    try {
      await jpost("/api/prefs", prefs);
      msg(els.modelMsg, "Saved.", true);
      saved();
    } catch (err) {
      msg(els.modelMsg, err.message);
    }
  });

  $("logout").addEventListener("click", async () => {
    await fetch("/api/auth/logout", { method: "POST" });
    location.href = "/auth.html";
  });
}

async function init() {
  const u = await jget("/api/auth/me");
  paintProfile(u);
  els.taPrompt.value = (u.prefs && u.prefs.system_prompt) || "";
  const j = await jget("/api/models");
  els.selModel.innerHTML = "";
  const items = (j && Array.isArray(j.items)) ? j.items : [];
  for (const id of items) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    els.selModel.append(opt);
  }
  const prefModel = (u.prefs && u.prefs.model) || (j && j.default) || "";
  if (items.includes(prefModel)) els.selModel.value = prefModel;
  els.chkThink.checked = !!(u.prefs && u.prefs.thinking);
  if (u.prefs && u.prefs.ctx) els.inpCtx.value = u.prefs.ctx;
  ready = true;
  // A note from an earlier failed open no longer applies.
  for (const stale of els.root.querySelectorAll(".s-fatal")) stale.remove();
}

/* The dialog is rendered before any of this is fetched, so a failure has to be
   shown rather than swallowed: the visible panel carries the note. */
function fatal(e) {
  if (e && e.message === "Not signed in") {
    location.href = "/auth.html";
    return;
  }
  const panel = els.root.querySelector(".dlg-panel:not([hidden])") || els.root.querySelector(".dlg-panel");
  if (!panel || panel.querySelector(".s-fatal")) return;
  const note = document.createElement("p");
  note.className = "s-note s-fatal";
  note.textContent = "Some settings couldn't load: " + (e && e.message ? e.message : e);
  panel.prepend(note);
}

const dialog = createDialog({
  root: els.root,
  opener: els.opener,
  // Data refetches on every open, so a failed one simply retries next time.
  onShow() {
    init().catch(fatal);
  },
});

/* The forms are static markup, so their listeners bind once at load: a failed
   fetch must never leave the dialog inert, and closing the dialog must not
   duplicate them. Theme is local too — never on the network — so the
   Appearance panel is already right the first time it is opened. */
bind();
bindTheme();

if (location.hash === "#settings") dialog.open();
