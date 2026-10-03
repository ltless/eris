const $ = (id) => document.getElementById(id);
const els = {
  settings: $("settings"),
  avatar: $("avatar"),
  cover: $("cover"),
  name: $("inpName"),
  email: $("emailRow"),
  pickAvatar: $("pickAvatar"),
  pickCover: $("pickCover"),
  fileImg: $("fileImg"),
  pwOld: $("pwOld"),
  pwNew: $("pwNew"),
  pwNew2: $("pwNew2"),
  pwMsg: $("pwMsg"),
  selModel: $("selModel"),
  inpCtx: $("inpCtx"),
  chkThink: $("chkThink"),
  modelMsg: $("modelMsg"),
};

let mediaKind = "avatar";

function msg(el, text, ok) {
  el.hidden = false;
  el.textContent = text;
  el.className = "s-msg " + (ok ? "ok" : "err");
}

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) location.href = "/auth.html";
  return r.json();
}

async function jpost(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.status === 401) location.href = "/auth.html";
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
  return j;
}

function paintProfile(u) {
  els.name.value = u.name || "";
  els.email.textContent = u.email || "";
  const init = (u.name || "ME").slice(0, 2).toUpperCase();
  if (u.avatar) {
    els.avatar.textContent = "";
    els.avatar.style.backgroundImage = `url("${u.avatar}")`;
  } else {
    els.avatar.style.backgroundImage = "";
    els.avatar.textContent = init;
  }
  els.cover.style.backgroundImage = u.cover ? `url("${u.cover}")` : "";
}

function bind() {
  $("cardProfile").querySelector("#fName").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = els.name.value.trim();
    if (!name) return;
    try {
      paintProfile(await jpost("/api/auth/profile", { name }));
    } catch (err) {
      msg(els.email, err.message);
    }
  });

  els.pickAvatar.addEventListener("click", () => {
    mediaKind = "avatar";
    els.fileImg.click();
  });
  els.pickCover.addEventListener("click", () => {
    mediaKind = "cover";
    els.fileImg.click();
  });
  els.fileImg.addEventListener("change", async () => {
    const f = els.fileImg.files[0];
    els.fileImg.value = "";
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f, f.name);
    try {
      const r = await fetch(`/api/auth/media?kind=${mediaKind}`, { method: "POST", body: fd });
      if (r.status === 401) location.href = "/auth.html";
      if (!r.ok) throw new Error((await r.json()).detail || `HTTP ${r.status}`);
      const u = await jget("/api/auth/me");
      paintProfile(u);
    } catch (err) {
      msg(els.email, err.message);
    }
  });

  $("fPw").addEventListener("submit", async (e) => {
    e.preventDefault();
    els.pwMsg.hidden = true;
    if (els.pwNew.value !== els.pwNew2.value) {
      msg(els.pwMsg, "new passwords do not match");
      return;
    }
    try {
      await jpost("/api/auth/password", { old: els.pwOld.value, new: els.pwNew.value });
      msg(els.pwMsg, "password changed, this session stays signed in", true);
      els.pwOld.value = els.pwNew.value = els.pwNew2.value = "";
    } catch (err) {
      msg(els.pwMsg, err.message);
    }
  });

  $("fModel").addEventListener("submit", async (e) => {
    e.preventDefault();
    els.modelMsg.hidden = true;
    const ctx = parseInt(els.inpCtx.value, 10);
    const prefs = {
      model: els.selModel.value,
      thinking: els.chkThink.checked,
      ctx: Number.isNaN(ctx) ? 0 : ctx,
    };
    try {
      const j = await jpost("/api/prefs", prefs);
      msg(els.modelMsg, `saved: ${j.prefs.model}`, true);
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
  const j = await jget("/api/models");
  els.selModel.innerHTML = "";
  for (const id of j.items) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    els.selModel.append(opt);
  }
  els.selModel.value = u.prefs?.model || j.default || "";
  els.chkThink.checked = !!u.prefs?.thinking;
  if (u.prefs?.ctx) els.inpCtx.value = u.prefs.ctx;
  bind();
}

init().catch((e) => {
  els.settings.hidden = true;
});
