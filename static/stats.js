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
  const POLL = 30000; // the unload countdown is the only thing that drifts

  let btn, panel, box;
  let open = false;
  let model = "";
  let label = "";
  let data = null; // the last /api/stats answer
  let failed = false; // the last ask came back with nothing
  let refused = false; // ...because it was turned away, not because nobody answered
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

  function contextUse() {
    const finite = (value) => Number.isFinite(value) && value >= 0 ? value : null;
    const prompt = finite(last?.prompt_eval_count);
    const generated = finite(last?.eval_count);
    const window = finite(last?.context_window) || null;
    const used = prompt === null ? null : prompt + (generated ?? 0);
    const estimated = last?.context_source === "requested";
    return { window, prompt, generated, used, estimated,
      share: window && used !== null ? used / window : null };
  }

  function draw() {
    if (!btn) return;
    const { window, used, share, generated, estimated } = contextUse();
    const fill = share === null ? 0 : Math.min(1, share);
    btn.style.setProperty("--stat-fill", String(fill));
    // An empty ring is drawn too: 0% of the window is a fact, and a frame with
    // nothing in it reads as exactly that.
    btn.toggleAttribute("data-live", share !== null);
    if (share !== null && share >= 1) btn.setAttribute("data-full", "over");
    else if (share !== null && share >= 0.75) btn.setAttribute("data-full", "warn");
    else btn.removeAttribute("data-full");
    const metric = generated === null ? "Prompt" : "Last call";
    btn.title = share === null
      ? used === null ? "Context usage is unavailable" : `${metric}: ${tokens(used)} tokens`
      : `${metric}: ${estimated ? "~" : ""}${percent(share)} (${tokens(used)} of ${tokens(window)} tokens)`;
    btn.setAttribute("aria-label", btn.title);
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
    state.textContent = refused ? "Refused"
      : failed ? "Unreachable"
      : !data ? "Reading..."
      : data.status_known === false ? "Unknown"
      : data.loaded ? "Loaded" : "Not loaded";
    if (failed || (data && !data.loaded)) state.dataset.off = "";
    head.append(title, state);
    panel.append(head);

    const name = document.createElement("p");
    name.className = "stat-model";
    name.textContent = label || model || "no model selected";
    name.title = model || "";
    panel.append(name);

    const ctx = data?.context || {};
    const { window, used, share, prompt: input, generated, estimated } = contextUse();
    const fill = share === null ? 0 : Math.min(1, share);
    if (share !== null) {
      const bar = document.createElement("div");
      bar.className = "stat-bar";
      if (share > 1) bar.dataset.over = "";
      bar.style.setProperty("--stat-fill", String(fill));
      bar.append(document.createElement("i"));
      panel.append(bar);
    }

    const prompt = last || {};
    panel.append(group("Context", [
      ["Last-call window", window && estimated ? `~${tokens(window)}` : tokens(window)],
      ["Prompt", tokens(input)],
      ["Generated", tokens(generated)],
      ["Remaining", generated !== null && share !== null && share <= 1
        ? tokens(window - used) : null],
      ["Reused from cache", tokens(prompt.prompt_eval_cached_count)],
      ["Runner now", tokens(ctx.allocated)],
      ["Requested", tokens(ctx.requested)],
      ["Model maximum", tokens(ctx.max)],
    ]));

    if (!data) {
      const note = document.createElement("p");
      note.className = "stat-note";
      // Three states, because two of them send the reader somewhere different:
      // a lapsed session is fixed by signing in, and a server that did not
      // answer is not fixed from here at all. A refusal is named as a refusal
      // rather than as an outage: this endpoint answers 401 when the session is
      // not accepted, and the model server behind it is usually fine.
      note.textContent = refused
        ? "The session was not accepted. Sign in again if this does not clear."
        : failed
          ? "The server did not answer. It may be down, or the session may have ended."
          : "Asking the server what it has loaded.";
      panel.append(note);
      return;
    }

    const kv = data.kv || {};
    panel.append(group("Cache", [
      ["Estimated KV", bytes(kv.bytes)],
      // The server never reports the cache type, so this is the value eris was
      // told to assume. It is named rather than hidden: a KV figure computed
      // for a type the server is not using is wrong, and the reader is the one
      // who can tell that from a number alone.
      ["Cache type", kv.type ? kv.type.toUpperCase() : null],
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
      ["Effort", !last ? null : typeof prompt.effort === "boolean"
        ? (prompt.effort ? "On" : "Off") : prompt.effort === "none" ? "Off" : (prompt.effort || "Default")],
      ["Speed", Number.isFinite(rate) ? `${rate.toFixed(1)} tok/s` : null],
      ["Load", seconds(prompt.load_duration)],
      ["Unloads in", countdown(data.expires_at)],
    ]));

    // The KV figure is arithmetic on the model's own metadata, not a number the
    // server publishes, and a reader is entitled to know which one they have.
    const notes = [];
    // An unknown cache type is stated before the estimate note, because it is
    // the reason there is no figure to call an estimate.
    if (kv.type && kv.known === false) {
      notes.push(`Cache type "${kv.type}" is not one this can size, so the KV figure is blank. Set KV_CACHE_TYPE to f16, q8_0, or q4_0.`);
    }
    if (estimated) notes.push("The runner did not confirm the window for this call.");
    if (share > 1) notes.push("This call processed more tokens than the reported window.");
    if (used === null) notes.push("Usage appears when the model reports a completed call.");
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
    // Answered by this call and read by its own catch, so a slow 401 cannot
    // describe a later ask.
    let denied = false;
    // The model is read here rather than captured when this was wired: the
    // picker and the settings dialog both change it, and a figure asked for
    // once must describe the model in use when it is asked.
    fetch(`/api/stats?model=${encodeURIComponent(model)}`)
      .then((r) => {
        // A refused ask is not a server that went quiet, and saying it was
        // sends the reader to look at the wrong machine: this endpoint answers
        // 401 when the session has lapsed, and the model server is fine. app.js
        // draws the same line for a refused send, settings.js for a refused
        // read.
        if (r.status === 401) {
          denied = true;
          throw new Error("stats 401");
        }
        if (!r.ok) throw new Error(`stats ${r.status}`);
        return r.json();
      })
      .then((j) => {
        if (id !== ticket) return;
        data = j;
        failed = false;
        refused = false;
        draw();
        if (open) paint();
      })
      .catch(() => {
        if (id !== ticket) return;
        data = null;
        failed = true;
        refused = denied;
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
      // Either kind of nothing is worth asking again about: the countdown
      // cannot be redrawn from an answer that never arrived, and a refusal is
      // not always the end of the session — a valid cookie is refused while the
      // session table is being re-read after a reload. The wording says which
      // happened; the retry is what lets it come back on its own.
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
    const next = id || "";
    label = name || "";
    if (model === next) {
      if (open) paint();
      return;
    }
    ticket++;
    model = next;
    data = null; // the previous model's numbers describe a different model
    // The prompt count goes with it: it was measured against the old window,
    // and the next model's window is a different denominator, so a share drawn
    // from it would be a fraction of the wrong thing.
    last = null;
    failed = false;
    refused = false;
    draw();
    if (open) {
      paint();
      load();
    }
  }

  function metrics(obj) {
    last = obj && (!model || obj.model === model) ? obj : null;
    draw();
    if (open) paint();
  }

  // The ring reads one chat's last call, so opening another chat drops it: the
  // count belongs to the turn that made it, and a ring left at the chat before
  // would be describing a conversation no longer on screen.
  function clearCall() {
    last = null;
    draw();
    if (open) paint();
  }

  return { init, setModel, metrics, load, clearCall };
})();
