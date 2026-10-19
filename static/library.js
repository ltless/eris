/* The Library panel. Lists what is indexed, opens one document's chunks in
   place, deletes a document behind a two-step confirm, and takes an upload.

   It is a dialog over the chat — see dialog.js for the shell — opened from the
   sidebar row, so there is no route and no back control: Esc, the scrim, and
   the close button dismiss it, and #library keeps the open state addressable.

   Loaded as a module: it shares helper names with app.js, and module scope is
   what keeps them from colliding in the global object.

   Every value that comes back from the server is written with textContent, so
   a filename with quotes or markup in it can never become markup here. */

import { createDialog } from "/dialog.js";

const $ = (id) => document.getElementById(id);
const els = {
  modal: $("libModal"),
  opener: $("sideLibrary"),
  list: $("docList"),
  empty: $("docEmpty"),
  count: $("docCount"),
  msg: $("docMsg"),
  filter: $("docFilter"),
  input: $("docInput"),
  index: $("docIndex"),
};

let docs = [];
const opened = new Set();     // ids whose chunks are shown
const confirming = new Set(); // ids waiting on the delete confirm
const cache = new Map();      // id -> passages

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function size(bytes) {
  if (bytes == null) return "";
  if (bytes >= 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes >= 1_000) return `${Math.round(bytes / 1_000)} KB`;
  return `${bytes} bytes`;
}

function date(seconds) {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function meta(d) {
  if (d.legacy) return `indexed before this panel existed · ${plural(d.chunks, "chunk")}`;
  return [plural(d.chunks, "chunk"), size(d.bytes), date(d.created)].filter(Boolean).join(" · ");
}

/* The toolbar line carries the server's message verbatim. `ok` marks a note
   that reports a completed action instead of a failure. */
function note(text, ok) {
  els.msg.hidden = !text;
  els.msg.textContent = text || "";
  els.msg.classList.toggle("ok", !!ok && !!text);
}

function detail(body, status) {
  return typeof body.detail === "string" ? body.detail : `HTTP ${status}`;
}

async function jget(url) {
  const r = await fetch(url);
  if (r.status === 401) {
    location.href = "/auth.html";
    throw new Error("Not signed in");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(detail(body, r.status));
  return body;
}

function line(cls, text) {
  const p = document.createElement("p");
  p.className = cls;
  p.textContent = text;
  return p;
}

/* Every row action carries its name, so focus can be put back on the same
   control after a repaint moves the DOM out from under it. */
function button(label, cls, act, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = label;
  b.dataset.act = act;
  b.addEventListener("click", onClick);
  return b;
}

function passagesBlock(d) {
  const box = document.createElement("div");
  box.className = "lib-passages";
  box.id = `passages-${d.id}`;
  const shown = opened.has(d.id);
  box.hidden = !shown;
  if (!shown) return box;

  const chunks = cache.get(d.id);
  if (!chunks) {
    box.append(line("lib-more", "Loading…"));
    return box;
  }
  if (!chunks.length) {
    box.append(line("lib-more", "No chunks came back for this document."));
    return box;
  }
  for (const chunk of chunks) box.append(line("lib-passage", chunk.text));
  if (d.chunks > chunks.length) {
    box.append(line("lib-more", `Showing ${chunks.length} of ${d.chunks} chunks.`));
  }
  return box;
}

function rowEl(d) {
  const li = document.createElement("li");
  li.className = "lib-row";
  li.dataset.id = d.id;

  const who = document.createElement("div");
  who.className = "lib-id";
  who.append(line("lib-name", d.name), line("lib-meta", meta(d)));

  const acts = document.createElement("div");
  acts.className = "lib-acts";
  if (confirming.has(d.id)) {
    acts.append(line("lib-ask", `Delete ${plural(d.chunks, "chunk")}?`));
    acts.append(
      button("Delete", "lib-btn lib-danger", "confirm", () => remove(d)),
      button("Cancel", "lib-btn", "cancel", () => {
        confirming.delete(d.id);
        paint({ id: d.id, act: "del" });
      })
    );
  } else {
    const opener = button("Open", "lib-btn", "open", () => toggle(d));
    opener.setAttribute("aria-expanded", String(opened.has(d.id)));
    opener.setAttribute("aria-controls", `passages-${d.id}`);
    acts.append(
      opener,
      button("Delete", "lib-btn", "del", () => {
        confirming.add(d.id);
        paint({ id: d.id, act: "confirm" });
      })
    );
  }

  const head = document.createElement("div");
  head.className = "lib-line";
  head.append(who, acts);

  li.append(head, passagesBlock(d));
  return li;
}

function paint(focus) {
  const query = els.filter.value.trim().toLowerCase();
  const shown = query ? docs.filter((d) => d.name.toLowerCase().includes(query)) : docs;
  els.list.replaceChildren(...shown.map(rowEl));

  const total = docs.reduce((n, d) => n + (d.chunks || 0), 0);
  els.count.textContent = docs.length ? `${plural(docs.length, "document")} · ${plural(total, "chunk")}` : "";
  els.empty.hidden = shown.length > 0;
  els.empty.textContent =
    query && !shown.length ? "No document matches that filter." : "No documents indexed yet.";

  if (!focus) return;
  const row = els.list.querySelector(`.lib-row[data-id="${CSS.escape(focus.id)}"]`);
  const next = row && row.querySelector(`[data-act="${focus.act}"]`);
  (next || els.index).focus();
}

async function load() {
  try {
    const body = await jget("/api/documents");
    docs = body.items || [];
    note("");            // a failure line must not outlive the success that follows
    paint();
    return true;
  } catch (err) {
    note(err.message);
    return false;        // not loaded, so the next open tries again
  }
}

async function toggle(d) {
  if (opened.has(d.id)) {
    opened.delete(d.id);
    paint({ id: d.id, act: "open" });
    return;
  }
  opened.add(d.id);
  paint({ id: d.id, act: "open" });
  if (cache.has(d.id)) return;
  try {
    const body = await jget(`/api/documents/${d.id}`);
    cache.set(d.id, body.passages || []);
  } catch (err) {
    opened.delete(d.id);
    note(err.message);
  }
  paint({ id: d.id, act: "open" });
}

async function remove(d) {
  try {
    const r = await fetch(`/api/documents/${d.id}`, { method: "DELETE" });
    if (r.status === 401) {
      location.href = "/auth.html";
      return;
    }
    if (!r.ok) throw new Error(detail(await r.json().catch(() => ({})), r.status));
    confirming.delete(d.id);
    opened.delete(d.id);
    cache.delete(d.id);
    note("");
    await load();
    els.index.focus();
  } catch (err) {
    note(err.message);
  }
}

async function upload(file) {
  const form = new FormData();
  form.append("file", file, file.name);
  els.index.disabled = true;
  try {
    const r = await fetch("/api/documents/upload", { method: "POST", body: form });
    if (r.status === 401) {
      location.href = "/auth.html";
      return;
    }
    if (!r.ok) throw new Error(detail(await r.json().catch(() => ({})), r.status));
    const body = await r.json().catch(() => ({}));
    note("");
    const loaded = await load();
    // Re-uploading a filename replaces it. Say so: the row keeps the name, so
    // otherwise nothing on screen would show what the upload did. A load that
    // failed keeps its own message.
    if (loaded && body.replaced) {
      note(
        body.replaced === 1
          ? `Replaced the previous copy of ${file.name}.`
          : `Replaced ${body.replaced} previous copies of ${file.name}.`,
        true,
      );
    }
  } catch (err) {
    note(err.message);
  } finally {
    els.index.disabled = false;
  }
}

els.index.addEventListener("click", () => els.input.click());
els.input.addEventListener("change", () => {
  const file = els.input.files[0];
  els.input.value = "";
  if (file) upload(file);
});
els.filter.addEventListener("input", () => paint());

const dialog = createDialog({
  root: els.modal,
  opener: els.opener,
  /* A pending delete confirm gives up on Escape before the dialog does. */
  onEscape() {
    if (!confirming.size) return false;
    const [id] = confirming;
    confirming.clear();
    paint({ id, act: "del" });
    return true;
  },
  onHide() {
    // The rows hold baked-in confirm state, so a dialog reopened with a pending
    // confirm would offer a delete that no longer has one. Clear and repaint.
    if (!confirming.size) return;
    confirming.clear();
    paint();
  },
  onShow: () => load(),
});

if (location.hash === "#library") dialog.open();
