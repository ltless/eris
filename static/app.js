const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("form"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  menuBtn: document.getElementById("menuBtn"),
  chatTitle: document.getElementById("chatTitle"),
  atts: document.getElementById("atts"),
  attach: document.getElementById("attach"),
  fileInput: document.getElementById("fileInput"),
  box: document.getElementById("box"),
  mic: document.getElementById("mic"),
  count: document.getElementById("count"),
  searchBtn: document.getElementById("searchBtn"),
  modeAgentic: document.getElementById("modeAgentic"),
  modeDeep: document.getElementById("modeDeep"),
};

marked.setOptions({ breaks: true, gfm: true });

// Sticky "jump to latest" button. While streaming we only auto-scroll when the
// reader is already at the bottom, so scrolling up to re-read older turns is
// not yanked back on every delta.
const scrollBtn = document.createElement("button");
scrollBtn.type = "button";
scrollBtn.className = "jump-btn";
scrollBtn.setAttribute("aria-label", "Scroll to latest");
scrollBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>';
scrollBtn.hidden = true;
scrollBtn.addEventListener("click", () => {
  els.messages.scrollTop = els.messages.scrollHeight;
});
els.messages.append(scrollBtn);

const nearBottom = () =>
  els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 80;

function syncScrollBtn() {
  scrollBtn.hidden = nearBottom();
}

function autoScroll() {
  if (nearBottom()) els.messages.scrollTop = els.messages.scrollHeight;
}

els.messages.addEventListener("scroll", syncScrollBtn, { passive: true });

const FILE_ICON =
  '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
const X_ICON = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';
const IMG_ICON =
  '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 16l5-5 4 4 3-3 6 6" /><circle cx="9" cy="9" r="1.4" /></svg>';

const DOC_RE =
  /\.(pdf|txt|md|markdown|csv|tsv|jsonl?|xml|ya?ml|toml|ini|cfg|conf|log|rst|tex|srt|vtt|py|js|jsx|ts|tsx|html?|css|scss|sql|sh|bash|rs|go|java|kt|c|cc|cpp|h|hpp|cs|rb|php|vue|svelte|env)$/i;
const MAX_ATTS = 8;
const MAX = 4000;

let sessions = [];
let activeId = null;
let controller = null;
let sending = false;
let atts = [];
let attSeq = 0;
let useRag = true;
let userName = "";
let userAvatar = "";
let modelAvatar = "";
let chatMode = "agentic";

const current = () => sessions.find((s) => s.id === activeId);

function render(text) {
  return DOMPurify.sanitize(marked.parse(text));
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function renderDocChips(container, items) {
  const docs = (items || []).filter((x) => x.kind === "doc");
  if (!docs.length) return;
  const el = document.createElement("div");
  el.className = "msg-sources";
  el.innerHTML = docs
    .map((x) => {
      const n = x.n ? `<span class="chip-n">${x.n}</span>` : "";
      return `<span class="chip" title="${escapeHtml(x.source)}">${n}${escapeHtml(x.source)}</span>`;
    })
    .join("");
  container.append(el);
}


function addRegen(body, s) {
  if (body.querySelector(".regen")) return;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "regen";
  b.textContent = "Regenerate";
  b.title = "Re-answer the last question (replaces this response)";
  b.addEventListener("click", () => {
    if (sending) return;
    const last = s.messages.at(-1);
    const q = s.messages.at(-2);
    if (!last || last.role !== "assistant" || !q || q.role !== "user" || typeof q.content !== "string" || !q.content || (q.images && q.images.length)) return;
    send(q.content, true);
  });
  body.append(b);
}

function showEmpty() {
  resetMessages(
    '<div class="empty" id="empty"><p>Ask anything. Attach files or images. Markdown supported.</p></div>'
  );
}

// innerHTML wipes destroy the sticky scroll button; every message-area reset
// goes through here so it stays attached and its visibility state stays fresh.
function resetMessages(html = "") {
  els.messages.innerHTML = html;
  els.messages.append(scrollBtn);
  syncScrollBtn();
}

// Every avatar in the app draws from the same two globals: the photo when the
// account has one, initials otherwise. Background images need the sizing that
// .avatar and .side-avatar declare, so the photo is set here, not in the markup.
function paintAvatar(el) {
  if (!el) return;
  if (userAvatar) {
    el.style.backgroundImage = `url("${userAvatar}")`;
    el.textContent = "";
    return;
  }
  el.style.backgroundImage = "";
  el.textContent = (userName || "ME").slice(0, 2).toUpperCase();
}

// The assistant's chip carries the model picture when the account has one, and
// the "AI" label when it does not. Same rule as the user avatar above.
function paintRole(el) {
  if (!el) return;
  if (modelAvatar) {
    el.style.backgroundImage = `url("${modelAvatar}")`;
    el.textContent = "";
    return;
  }
  el.style.backgroundImage = "";
  el.textContent = "AI";
}

function addMessage(role, text, images) {
  document.getElementById("empty")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const roleEl = document.createElement("div");
  roleEl.className = "role";
  roleEl.textContent = role === "user" ? "You" : "AI";
  const body = document.createElement("div");
  body.className = "body";
  if (images && images.length) {
    const imgs = document.createElement("div");
    imgs.className = "imgs";
    for (const src of images) {
      const im = document.createElement("img");
      im.src = src;
      im.alt = "attachment";
      im.addEventListener("click", () => window.open(src, "_blank"));
      imgs.append(im);
    }
    body.append(imgs);
  }
  if (role === "user") {
    const t = document.createElement("div");
    t.className = "bubble";
    t.textContent = text;
    body.append(t);
    const av = document.createElement("div");
    av.className = "avatar";
    paintAvatar(av);
    wrap.append(av, body);
  } else {
    body.innerHTML = render(text);
    paintRole(roleEl);
    wrap.append(roleEl, body);
  }
  els.messages.insertBefore(wrap, scrollBtn);
  els.messages.scrollTop = els.messages.scrollHeight;
  syncScrollBtn();
  return body;
}

function highlight(body) {
  body.querySelectorAll("pre code").forEach((el) => {
    if (!el.dataset.hl) {
      hljs.highlightElement(el);
      el.dataset.hl = "1";
      addCopy(el);
    }
  });
}

function addCopy(codeEl) {
  const pre = codeEl.parentElement;
  if (!pre || pre.querySelector(".cp")) return;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "cp";
  b.title = "Copy code";
  b.textContent = "Copy";
  b.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(codeEl.textContent);
      b.textContent = "Copied";
      setTimeout(() => {
        b.textContent = "Copy";
      }, 1500);
    } catch {
      b.textContent = "Failed";
    }
  });
  pre.append(b);
}

function linkCitations(body, items) {
  const ans = body.querySelector(".ans");
  if (!ans) return;
  const byN = new Map((items || []).filter((x) => x.n).map((x) => [x.n, x]));
  const walker = document.createTreeWalker(ans, NodeFilter.SHOW_TEXT);
  const targets = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (node.parentElement.closest("pre, code, a")) continue;
    if (/\[\d{1,3}\]/.test(node.textContent)) targets.push(node);
  }
  for (const node of targets) {
    const frag = document.createDocumentFragment();
    let last = 0;
    for (const hit of node.textContent.matchAll(/\[(\d{1,3})\]/g)) {
      frag.append(document.createTextNode(node.textContent.slice(last, hit.index)));
      const a = document.createElement("a");
      a.className = "cite";
      a.textContent = `[${hit[1]}]`;
      const src = byN.get(parseInt(hit[1], 10));
      if (src && src.kind === "web" && /^https?:\/\//i.test(src.source)) {
        a.href = src.source;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.title = src.source;
      } else if (src) {
        a.title = src.source;
      } else {
        a.className = "cite cite-x";
        a.title = "source not in this answer";
      }
      frag.append(a);
      last = hit.index + hit[0].length;
    }
    frag.append(document.createTextNode(node.textContent.slice(last)));
    node.replaceWith(frag);
  }
}

/* ---------- attachments ---------- */

function classify(file) {
  if (file.type && file.type.startsWith("image/")) return "image";
  if (file.type === "application/pdf" || file.type.startsWith("text/")) return "doc";
  if (DOC_RE.test(file.name || "")) return "doc";
  return null;
}

function fileToDataURL(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}

async function shrinkToDataURL(file) {
  const raw = await fileToDataURL(file);
  if (file.type === "image/gif") return raw;
  try {
    const img = await new Promise((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = rej;
      i.src = raw;
    });
    const scale = Math.min(1, 1280 / Math.max(img.naturalWidth, img.naturalHeight));
    if (scale === 1 && file.size <= 1_000_000) return raw;
    const c = document.createElement("canvas");
    c.width = Math.round(img.naturalWidth * scale);
    c.height = Math.round(img.naturalHeight * scale);
    c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL(file.type === "image/png" ? "image/png" : "image/jpeg", 0.85);
  } catch {
    return raw;
  }
}

async function addFiles(fileList) {
  for (const f of [...fileList]) {
    if (atts.length >= MAX_ATTS) break;
    const kind = classify(f);
    const a = { id: ++attSeq, name: f.name || "file", kind: kind || "doc", file: f, status: "ok" };
    if (!kind) {
      a.status = "err";
      a.msg = "unsupported type";
      atts.push(a);
      renderAtts();
      continue;
    }
    if (kind === "image") {
      a.status = "busy";
      a.msg = "reading…";
      atts.push(a);
      renderAtts();
      try {
        a.dataUrl = await shrinkToDataURL(f);
        a.status = "ok";
        a.msg = "";
      } catch {
        a.status = "err";
        a.msg = "read failed";
      }
      renderAtts();
    } else {
      atts.push(a);
      renderAtts();
    }
  }
}

function renderAtts() {
  els.atts.hidden = atts.length === 0;
  els.atts.innerHTML = "";
  for (const a of atts) {
    const chip = document.createElement("span");
    const thumb = a.kind === "image" && a.dataUrl;
    chip.className = "chip" + (thumb ? " chip-img" : "") + (a.status === "err" ? " err" : "");
    chip.title = a.msg ? `${a.name} — ${a.msg}` : a.name;

    if (thumb) {
      const im = document.createElement("img");
      im.className = "chip-thumb";
      im.src = a.dataUrl;
      im.alt = a.name;
      chip.append(im);
    } else {
      const box = document.createElement("span");
      box.innerHTML = a.kind === "image" ? IMG_ICON : FILE_ICON;
      chip.append(box.firstChild);
      const nm = document.createElement("span");
      nm.className = "chip-name";
      nm.textContent = a.name;
      chip.append(nm);
      if (a.status === "err" && a.msg) {
        const note = document.createElement("span");
        note.className = "chip-note";
        note.textContent = a.msg;
        chip.append(note);
      } else if (a.status === "busy") {
        const note = document.createElement("span");
        note.className = "chip-note";
        note.textContent = a.kind === "image" ? "reading..." : "indexing...";
        chip.append(note);
      }
    }

    const x = document.createElement("button");
    x.type = "button";
    x.className = "chip-x";
    x.title = "Remove";
    x.setAttribute("aria-label", `Remove ${a.name}`);
    x.innerHTML = X_ICON;
    x.addEventListener("click", () => {
      atts = atts.filter((t) => t.id !== a.id);
      renderAtts();
    });
    chip.append(x);
    els.atts.append(chip);
  }
}

function refresh() {
  const len = els.input.value.length;
  const has = els.input.value.trim().length > 0 || atts.some((a) => a.kind === "image" && a.status === "ok");
  const over = len > MAX;
  els.send.disabled = !sending && (!has || over);
  els.count.textContent = len + " / " + MAX;
  els.count.classList.toggle("show", len > MAX * 0.8);
  els.count.classList.toggle("over", over);
}

function syncInput() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
  refresh();
}

async function uploadDocs() {
  const docs = atts.filter((a) => a.kind === "doc" && a.status === "ok");
  if (!docs.length) return;
  docs.forEach((a) => (a.status = "busy"));
  renderAtts();
  await Promise.all(
    docs.map(async (a) => {
      try {
        const fd = new FormData();
        fd.append("file", a.file, a.name);
        const r = await fetch("/api/documents/upload", { method: "POST", body: fd });
        if (r.status === 401) location.href = "/auth.html";
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : `HTTP ${r.status}`);
        a.status = "done";
      } catch (e) {
        a.status = "err";
        a.msg = String(e.message || e).slice(0, 120);
      }
    })
  );
  atts = atts.filter((a) => a.status !== "done");
  renderAtts();
}

function syncComposer() {
  els.searchBtn.setAttribute("aria-pressed", String(useRag));
}

function setRag(v) {
  useRag = v;
  syncComposer();
  saveSettings();
}

function loadSettings() {
  try {
    useRag = localStorage.getItem("eris-rag") !== "0";
  } catch {}
}

function saveSettings() {
  try {
    localStorage.setItem("eris-rag", useRag ? "1" : "0");
  } catch {}
}



/* ---------- sessions ---------- */

function blankChat() {
  activeId = null;
  ErisSidebar.setActive(null);
  els.chatTitle.textContent = "New chat";
  atts = [];
  renderAtts();
  resetMessages();
  showEmpty();
  els.input.value = "";
  syncInput();
  els.input.focus();
}

function loadSession(id) {
  const s = sessions.find((x) => x.id === id);
  if (!s) return;
  activeId = id;
  ErisSidebar.setActive(id);
  els.chatTitle.textContent = s.title;
  atts = [];
  renderAtts();
  resetMessages();
  const paint = () => {
    if (!s.messages.length) {
      showEmpty();
    } else {
      s.messages.forEach((m) => {
        const body = addMessage(m.role, m.content, m.images);
        const thinkRounds = Array.isArray(m.thinking) ? m.thinking : m.thinking ? [m.thinking] : [];
        const web = (m.sources || []).filter((x) => x.kind === "web");
        const groups = (m.web && m.web.length ? m.web : web.length ? [{ query: "", items: web }] : [])
          .map((g) => ({ r: g.r ?? 0, query: g.query, items: g.items }));
        const rows = new Map();
        for (const g of groups) {
          if (!rows.has(g.r)) rows.set(g.r, []);
          rows.get(g.r).push(g);
        }
        let anchor = null;
        const put = (el) => {
          if (anchor) anchor.after(el);
          else body.prepend(el);
          anchor = el;
        };
        const rounds = Math.max(thinkRounds.length, ...[...rows.keys()].map((r) => r + 1), 0);
        for (let i = 0; i < rounds; i++) {
          if (i < thinkRounds.length) {
            const th = ErisToolCall.thinking(body);
            put(th.el);
            th.append(thinkRounds[i]);
            th.done("Thought");
          }
          for (const g of rows.get(i) || []) {
            const tc = ErisToolCall.webSearch(body);
            put(tc.el);
            if (g.query) tc.addQuery(g.query);
            tc.addResults(g.items);
            tc.done();
          }
        }
        if (m.role === "assistant") {
          renderDocChips(body, m.sources);
          highlight(body);
          linkCitations(body, m.sources);
          if (m === s.messages.at(-1) && m.content) addRegen(body, s);
        }
      });
    }
  };
  if (!s.messages.length && s.remote) {
    fetch(`/api/chats/${encodeURIComponent(id)}`).then((r) => r.json()).then((j) => {
      s.messages = (j.items || []).map((m) => ({ role: m.role, content: m.content, thinking: m.thinking, sources: m.sources, web: m.web, remote: true }));
      s.remote = false;
      paint();
    }).catch(paint);
  } else {
    paint();
  }
}

function toPayload(s) {
  return s.messages.filter((m) => !m.remote).map((m) => {
    if (m.images && m.images.length) {
      const parts = [];
      if (m.content) parts.push({ type: "text", text: m.content });
      for (const url of m.images) parts.push({ type: "image_url", image_url: { url } });
      return { role: m.role, content: parts };
    }
    return { role: m.role, content: m.content };
  });
}

function setStreaming(on) {
  els.send.classList.toggle("busy", on);
  els.send.title = on ? "Stop generation" : "Send (Enter)";
  els.send.setAttribute("aria-label", on ? "Stop generation" : "Send message");
  refresh();
}

async function send(text, regen = false) {
  let s = current();
  if (!s) {
    s = { id: crypto.randomUUID(), title: "New chat", messages: [] };
    sessions.unshift(s);
    activeId = s.id;
    ErisSidebar.setActive(activeId);
  }
  let images = [];

  if (!regen) {
    images = atts.filter((a) => a.kind === "image" && a.status === "ok").map((a) => a.dataUrl);
    await uploadDocs();
    atts = [];
    renderAtts();
  }

  sending = true;
  refresh();

  if (!regen) {
    addMessage("user", text, images);
    s.messages.push({ role: "user", content: text, ...(images.length ? { images } : {}) });
    if (s.messages.length === 1) {
      s.title = text.slice(0, 40) || (images.length ? "Image" : "New chat");
      els.chatTitle.textContent = s.title;
      ErisSidebar.setChats(sessions);
    }
  } else if (s.messages.at(-1)?.role === "assistant") {
    s.messages.pop();
    // Last DOM child is the sticky scroll button, not the message.
    [...els.messages.querySelectorAll(".msg")].pop()?.remove();
  }

  const body = addMessage("assistant", "");
  const setStatus = (label) => {
    body.querySelector(".status")?.remove();
    const el = document.createElement("div");
    el.className = "status";
    el.innerHTML = `<span class="dot"></span>${label}`;
    body.append(el);
  };
  setStatus("Sending...");
  let raw = "";
  let aborted = false;
  let thinkRounds = [];
  let thinkAll = "";
  let thinkCtl = null;
  let thinkRound = "";
  let webCtl = null;
  let webR = -1;
  let sources = [];
  let webGroups = [];

  const finishThink = () => {
    if (!thinkCtl) return;
    if (thinkRound) {
      thinkCtl.done();
      thinkRounds.push(thinkRound);
    } else {
      // done() clears the label timer; a detached node would keep ticking.
      thinkCtl.done();
      thinkCtl.el.remove();
    }
    thinkCtl = null;
    thinkRound = "";
  };
  const finishWeb = () => {
    if (!webCtl) return;
    webCtl.done();
    webCtl = null;
    webR = -1;
  };

  controller = new AbortController();
  setStreaming(true);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        use_rag: useRag,
        mode: chatMode,
        chat_id: s.id,
        regenerate: regen,
        messages: toPayload(s),
      }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      if (res.status === 401) location.href = "/auth.html";
      throw new Error(`HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let evt;
        try { evt = JSON.parse(data); } catch { continue; }

        if (evt.type === "tool") {
          finishThink();
          if (webCtl && evt.r !== webR) finishWeb();
          if (!webCtl) {
            body.querySelector(".status")?.remove();
            webCtl = ErisToolCall.webSearch(body);
            webR = evt.r;
          }
          webCtl.addQuery(evt.query || "Search");
          webGroups.push({ r: evt.r, query: evt.query || "", items: [] });
        } else if (evt.type === "sources") {
          sources.push(...evt.items);
          const web = evt.items.filter((x) => x.kind === "web");
          if (web.length) {
            (webGroups[webGroups.length - 1] ||= { query: "", items: [] }).items.push(...web);
            webCtl?.addResults(web);
          }
        } else if (evt.type === "thinking") {
          if (!thinkCtl) {
            body.querySelector(".status")?.remove();
            thinkCtl = ErisToolCall.thinking(body);
          }
          thinkAll += evt.text;
          thinkRound += evt.text;
          thinkCtl.append(evt.text);
          autoScroll();
        } else if (evt.type === "delta") {
          raw += evt.text;
          let ans = body.querySelector(".ans");
          if (!ans) {
            finishThink();
            finishWeb();
            body.querySelector(".status")?.remove();
            ans = document.createElement("div");
            ans.className = "ans";
            body.append(ans);
          }
          ans.innerHTML = render(raw) + '<span class="cursor">&#9613;</span>';
          autoScroll();
        } else if (evt.type === "error") {
          const err = document.createElement("div");
          err.className = "err";
          err.textContent = evt.message;
          body.append(err);
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      aborted = true;
      if (!raw && !thinkAll && !webCtl) setStatus("Stopped.");
    } else {
      const err = document.createElement("div");
      err.className = "err";
      err.textContent = e.message;
      body.append(err);
    }
  } finally {
    controller = null;
    sending = false;
    setStreaming(false);
    els.input.focus();
    body.querySelector(".cursor")?.remove();
    body.querySelector(".status")?.remove();
    if (!aborted && !raw && !body.querySelector(".err")) {
      // Thinking rows and search rows are not an answer, so they no longer
      // suppress this. Silence after a run of tool calls is what the user reads
      // as "it stopped".
      setStatus(thinkAll || webCtl ? "No answer came back. Try again, or narrow the question." : "No response.");
    }
    finishThink();
    finishWeb();
    renderDocChips(body, sources);
    highlight(body);
    linkCitations(body, sources);
    if (raw) s.messages.push({ role: "assistant", content: raw, thinking: thinkRounds.length ? thinkRounds : undefined, ...(sources.length ? { sources } : {}), ...(webGroups.filter((g) => g.items.length).length ? { web: webGroups.filter((g) => g.items.length) } : {}) });
    if (raw) addRegen(body, s);
  }
}


/* ---------- events ---------- */

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (sending) {
    if (controller) controller.abort();
    return;
  }
  const text = els.input.value.trim();
  if (els.input.value.length > MAX) return;
  const hasImg = atts.some((a) => a.kind === "image" && a.status === "ok");
  if (!text && !hasImg) return;
  els.input.value = "";
  syncInput();
  send(text);
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!sending) els.form.requestSubmit();
  }
});

els.input.addEventListener("input", syncInput);

els.menuBtn.addEventListener("click", () => ErisSidebar.open());

els.attach.addEventListener("click", () => els.fileInput.click());

els.fileInput.addEventListener("change", () => {
  addFiles(els.fileInput.files);
  els.fileInput.value = "";
});

els.input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    addFiles(files);
  }
});

for (const ev of ["dragover", "dragenter"]) {
  els.box.addEventListener(ev, (e) => {
    e.preventDefault();
    els.box.classList.add("drag");
  });
}
for (const ev of ["dragleave", "drop"]) {
  els.box.addEventListener(ev, (e) => {
    e.preventDefault();
    els.box.classList.remove("drag");
  });
}
els.box.addEventListener("drop", (e) => {
  const files = e.dataTransfer?.files;
  if (files?.length) addFiles(files);
});

els.searchBtn.addEventListener("click", () => {
  setRag(!useRag);
  els.input.focus();
});

function setMode(m) {
  chatMode = m;
  els.modeAgentic.classList.toggle("on", m === "agentic");
  els.modeDeep.classList.toggle("on", m === "deep");
  try {
    localStorage.setItem("eris-mode", m);
  } catch {}
}

els.modeAgentic.addEventListener("click", () => setMode("agentic"));
els.modeDeep.addEventListener("click", () => setMode("deep"));

try {
  if (localStorage.getItem("eris-mode") === "deep") {
    setMode("deep");
  }
} catch {}


/* ---------- voice input ---------- */

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!SR) {
  els.mic.hidden = true;
} else {
  const rec = new SR();
  rec.interimResults = false;
  rec.continuous = false;
  let listening = false;
  const endListen = () => {
    listening = false;
    els.mic.setAttribute("aria-pressed", "false");
  };
  rec.onresult = (e) => {
    const t = e.results[0][0].transcript;
    els.input.value = els.input.value.trim()
      ? els.input.value.replace(/\s+$/, "") + " " + t
      : t;
    syncInput();
    els.input.focus();
  };
  rec.onend = endListen;
  rec.onerror = endListen;
  els.mic.addEventListener("click", () => {
    if (listening) {
      rec.stop();
      return;
    }
    try {
      rec.start();
      listening = true;
      els.mic.setAttribute("aria-pressed", "true");
    } catch {}
  });
}


ErisSidebar.init({
  chats: [],
  onNew: () => blankChat(),
  onSelect: (id) => loadSession(id),
  onPin: async (id, pinned) => {
    const s = sessions.find((x) => x.id === id);
    if (s) s.pinned = pinned;
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned }),
    }).catch(() => {});
  },
  onRename: async (id, title) => {
    const s = sessions.find((x) => x.id === id);
    if (s) {
      s.title = (title || "").trim().slice(0, 80) || s.title;
      if (s.id === activeId) els.chatTitle.textContent = s.title;
    }
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).catch(() => {});
  },
  onDelete: async (id) => {
    sessions = sessions.filter((x) => x.id !== id);
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
    if (id === activeId) {
      if (!sessions.length) {
        blankChat();
      } else {
        loadSession(sessions[0].id);
      }
    }
  },
});

function refreshAccount() {
  fetch("/api/auth/me").then((r) => {
    if (!r.ok) {
      location.href = "/auth.html";
      return null;
    }
    return r.json().catch(() => ({}));
  }).then((u) => {
    if (!u || !u.name) return;
    userName = u.name;
    userAvatar = u.avatar || "";
    modelAvatar = (u.prefs || {}).model_avatar || "";
    const nm = document.getElementById("sideName");
    if (nm) nm.textContent = u.name;
    paintAvatar(document.getElementById("sideAvatar"));
    // Messages can render before this resolves, so repaint what is already there.
    els.messages.querySelectorAll(".avatar").forEach(paintAvatar);
    els.messages.querySelectorAll(".msg.assistant .role").forEach(paintRole);
  }).catch(() => {
    // A background repaint: a network blip must not raise, the next save fires
    // this again.
  });
}

refreshAccount();

/* The settings dialog writes to the account this page is showing. Nothing
   navigates any more, so it asks for a repaint once a change lands. */
window.addEventListener("eris:settings-saved", refreshAccount);

loadSettings();
syncComposer();
fetch("/api/chats").then((r) => r.json()).then((j) => {
  sessions = (j.items || []).map((c) => ({ id: c.id, title: c.title, pinned: !!c.pinned, messages: [], remote: true }));
  ErisSidebar.setChats(sessions);
}).catch(() => {}).finally(() => {
  if (!sessions.length) {
    blankChat();
  } else {
    const first = sessions[0];
    activeId = first.id;
    ErisSidebar.setChats(sessions);
    ErisSidebar.setActive(activeId);
    els.chatTitle.textContent = first.title || "New chat";
    showEmpty();
  }
});
syncInput();
els.input.focus();
