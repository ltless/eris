/*
  ErisStats — the ring beside Send, and the panel it opens.

  The ring answers one question without being opened: how much of the context
  window the current conversation is using. It is a circle because that is the
  shape with no ends to get wrong at 7% or 93%, and because it sits next to a
  send button without competing with it.

  Everything else — what the server has loaded, what the KV cache costs, what
  the card is holding — is in the panel, which is read-only and cheap to ask
  for: the endpoint reports what it has loaded rather than being made to
  generate anything, so this can be refreshed freely and never loads a model.

  ErisStats.init()
  ErisStats.setModel(id, label)   the model the readout describes
  ErisStats.metrics(obj)          the counts from a finished model call

  The live numbers arrive in two pieces. The allocated context and the memory
  come from /api/stats on demand; the tokens actually used are only ever stated
  by the server on the frame that ends a model call, so they are handed in
  from the chat stream and kept here until the next call replaces them.
*/
const ErisStats = (() => {
  const RING = 2 * Math.PI * 10; // r=10, the ring's own path length
  const POLL = 30000; // the unload countdown is the only thing that drifts

  let btn, panel, box;
  let open = false;
  let model = "";
  let label = "";
  let data = null; // the last /api/stats answer
  let failed = false; // the last ask went unanswered
  let last = null; // the counts from the last model call
  let ticket = 0; // the newest request, so a stale answer is dropped
  let tick = null;

  // --- formatting -----------------------------------------------------------

  function bytes(n) {
    if (!Number.isFinite(n)) return null;
    if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`;
    if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`;
    if (n >= 1024) return `${Math.round(n / 1024)} KB`;
    return `${n} B`;
  }

  function tokens(n) {
    if (!Number.isFinite(n)) return null;
    if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  }

  function seconds(ns) {
    if (!Number.isFinite(ns)) return null;
    const s = ns / 1e9;
    return s >= 10 ? `${s.toFixed(0)}s` : `${s.toFixed(1)}s`;
  }

  // The server states when it will drop the model, which is the one figure
  // here that changes while nobody is looking, so it is stated as a wait.
  function countdown(iso) {
    if (!iso) return null;
    const at = new Date(iso);
    if (Number.isNaN(at.getTime())) return null;
    const left = Math.round((at - Date.now()) / 1000);
    if (left <= 0) return "now";
    if (left < 60) return `${left}s`;
    if (left < 3600) return `${Math.round(left / 60)}m`;
    return `${(left / 3600).toFixed(1)}h`;
  }

  // The ring's whole job is the share, so the one figure it always answers in
  // the tooltip is the percentage — a fraction of a circle is read as a shape,
  // not a number, and "37%" is what a person asked for.
  function percent(share) {
    if (!Number.isFinite(share)) return null;
    if (share > 0 && share < 0.01) return "<1%";
    return `${Math.round(share * 100)}%`;
  }

  // --- the ring -------------------------------------------------------------

  function draw() {
    if (!btn) return;
    const ctx = (data && data.context) || {};
    // The allocated window is the denominator whenever the runner has stated
    // one; before it loads, the context this account would ask for is the only
    // honest stand-in, and the panel says which of the two it is showing.
    const allocated = ctx.allocated || ctx.wanted || 0;
    const used = last ? Number(last.prompt_eval_count) || 0 : null;
    const share = allocated && used !== null ? used / allocated : null;
    const fill = share === null ? 0 : Math.min(1, share);
    btn.style.setProperty("--stat-fill", String(fill));
    btn.style.setProperty("--stat-c", String(RING));
    // An empty ring is drawn too: 0% of the window is a fact, and a frame with
    // nothing in it reads as exactly that.
    btn.toggleAttribute("data-live", share !== null);
    if (share !== null && share >= 1) btn.setAttribute("data-full", "over");
    else if (share !== null && share >= 0.75) btn.setAttribute("data-full", "warn");
    else btn.removeAttribute("data-full");
    btn.title = share === null
      ? "Context used"
      : `Context ${percent(share)} used — ${tokens(used)} of ${tokens(allocated)} tokens`;
  }

  // --- the panel ------------------------------------------------------------

  function group(title, rows) {
    const sec = document.createElement("section");
    sec.className = "stat-group";
    const h = document.createElement("h4");
    h.textContent = title;
    const dl = document.createElement("dl");
    for (const [name, value] of rows) {
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = name;
      const dd = document.createElement("dd");
      const shown = value === null || value === undefined || value === "";
      if (shown) dd.dataset.none = "";
      dd.textContent = shown ? "—" : String(value);
      row.append(dt, dd);
      dl.append(row);
    }
    sec.append(h, dl);
    return sec;
  }

  function paint() {
    if (!panel) return;
    panel.textContent = "";

    const head = document.createElement("div");
    head.className = "stat-head";
    const title = document.createElement("span");
    title.className = "stat-title";
    title.textContent = "Model server";
    const state = document.createElement("span");
    state.className = "stat-state";
    state.textContent = failed ? "Unreachable" : !data ? "Reading..." : data.loaded ? "Loaded" : "Not loaded";
    if (failed || (data && !data.loaded)) state.dataset.off = "";
    head.append(title, state);
    panel.append(head);

    const name = document.createElement("p");
    name.className = "stat-model";
    name.textContent = label || model || "no model selected";
    name.title = model || "";
    panel.append(name);

    if (!data) {
      const note = document.createElement("p");
      note.className = "stat-note";
      // A refused ask is an answer too, and it is not the same as one still on
      // its way: the reader is told which happened rather than left waiting.
      note.textContent = failed
        ? "The server did not answer. It may be down, or the session may have ended."
        : "Asking the server what it has loaded.";
      panel.append(note);
      return;
    }

    const ctx = data.context || {};
    const allocated = ctx.allocated || ctx.wanted;
    const used = (last && last.prompt_eval_count) ?? null;
    const fill = allocated && used !== null ? Math.min(1, used / allocated) : 0;

    // The bar is the context, so it is drawn before the groups rather than
    // inside one: it is the shape of the first figure, not a section header.
    const bar = document.createElement("div");
    bar.className = "stat-bar";
    if (used !== null && allocated && used > allocated) bar.dataset.over = "";
    bar.style.setProperty("--stat-fill", String(fill));
    bar.append(document.createElement("i"));
    panel.append(bar);

    const prompt = last || {};
    panel.append(group("Context", [
      ["Allocated", tokens(ctx.allocated)],
      ["Prompt used", tokens(prompt.prompt_eval_count)],
      ["Reused from cache", tokens(prompt.prompt_eval_cached_count)],
      ["Model maximum", tokens(ctx.max)],
    ]));

    const kv = data.kv || {};
    panel.append(group("Cache", [
      ["KV at this context", bytes(kv.bytes)],
      ["Weights and KV", bytes(data.size)],
      ["In video memory", bytes(data.size_vram)],
    ]));

    const gpu = data.gpu || {};
    panel.append(group("Hardware", [
      ["Card in use", gpu.used !== null && gpu.used !== undefined ? bytes(gpu.used) : null],
      ["Card total", bytes(gpu.total)],
      ["GPU busy", Number.isFinite(gpu.util) ? `${gpu.util}%` : null],
    ]));

    const rate = Number.isFinite(prompt.eval_count) && Number.isFinite(prompt.eval_duration)
      ? prompt.eval_duration > 0 ? prompt.eval_count / (prompt.eval_duration / 1e9) : null
      : null;
    panel.append(group("Last call", [
      ["Generated", tokens(prompt.eval_count)],
      ["Speed", Number.isFinite(rate) ? `${rate.toFixed(1)} tok/s` : null],
      ["Load", seconds(prompt.load_duration)],
      ["Unloads in", countdown(data.expires_at)],
    ]));

    // The KV figure is arithmetic on the model's own metadata, not a number the
    // server publishes, and a reader is entitled to know which one they have.
    const notes = [];
    if (kv.bytes && kv.estimated) {
      notes.push(`KV is estimated for a ${tokens(kv.ctx)}-token window; this model is not loaded yet.`);
    }
    if (!used) {
      notes.push("Prompt usage appears after the first answer in this session.");
    }
    if (notes.length) {
      const note = document.createElement("p");
      note.className = "stat-note";
      note.textContent = notes.join(" ");
      panel.append(note);
    }
  }

  function place() {
    if (!box || !btn || !panel) return;
    // Same rule the pickers use: aligned to the trigger, pulled left when that
    // would push the panel past the field's edge.
    const align = btn.offsetLeft - box.clientLeft;
    const furthest = Math.max(0, box.clientWidth - panel.offsetWidth);
    panel.style.left = `${Math.min(align, furthest)}px`;
  }

  // --- loading --------------------------------------------------------------

  function load() {
    const id = ++ticket;
    fetch(`/api/stats?model=${encodeURIComponent(model)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`stats ${r.status}`))))
      .then((j) => {
        if (id !== ticket) return;
        data = j;
        failed = false;
        draw();
        if (open) paint();
      })
      .catch(() => {
        if (id !== ticket) return;
        data = null;
        failed = true;
        draw();
        if (open) paint();
      });
  }

  function show() {
    open = true;
    panel.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    paint();
    place();
    load();
    // Only the unload countdown moves on its own, so the panel is repainted
    // on a slow clock while it is open and never while it is closed.
    clearInterval(tick);
    tick = setInterval(() => {
      if (!open) return;
      // A failed ask is the one state worth asking again about: the countdown
      // cannot be redrawn from an answer that never arrived.
      if (failed) load();
      else if (data) paint();
    }, POLL);
  }

  function hide() {
    open = false;
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    clearInterval(tick);
    tick = null;
  }

  // --- wiring ---------------------------------------------------------------

  function init() {
    btn = document.getElementById("statBtn");
    panel = document.getElementById("statPanel");
    box = document.getElementById("box");
    if (!btn || !panel || !box) return;

    btn.addEventListener("click", () => (open ? hide() : show()));
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    document.addEventListener("pointerdown", (e) => {
      if (open && !box.contains(e.target)) hide();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && open) {
        e.preventDefault();
        hide();
      }
    });
    // A panel anchored to a control that moved is worse than no panel.
    window.addEventListener("resize", () => {
      if (open) place();
    });
    document.getElementById("input")?.addEventListener("focus", hide);
    draw();
  }

  function setModel(id, name) {
    model = id || "";
    label = name || "";
    data = null; // the previous model's numbers describe a different model
    failed = false;
    draw();
    if (open) {
      paint();
      load();
    }
  }

  function metrics(obj) {
    if (!obj) return;
    last = obj;
    draw();
    if (open) paint();
  }

  return { init, setModel, metrics };
})();
