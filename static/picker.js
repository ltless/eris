/*
  ErisPicker — the model and effort controls in the composer toolbar.

  Ported from React Bits' PromptBar (its model picker and its effort slider,
  and nothing else) to plain DOM: this app ships no bundler and no React, so
  the component's own state lives here and its look lives in picker.css, under
  the palette tokens rather than inline colour props.

  Two parts of the original are deliberately not carried over. The spark
  canvas that rains over the field at the top effort step, and the radial wash
  behind it, are both painted in an accent hue; this palette is monochrome, so
  the top step reads from the ink colour instead. The arrow-to-square send
  glyph belongs to the send button, which this page already has.

  ErisPicker.init({ onModel, onEffort })
                                     wire both triggers and their panels; each
                                     runs with the new choice when it changes
  ErisPicker.setModels(ids, id, labels, caps)
                                     fill the model menu, marking `id` as chosen.
                                     `labels` maps an id to the name shown for it;
                                     an id without one is shown as itself. `caps`
                                     maps an id to what it declared it can do; a
                                     model that did not declare thinking has no
                                     effort control to show.

  The effort ladder belongs to the model, so it is asked for rather than
  assumed: the panel fetches it the first time it is opened for a model and
  says "Checking..." until the answer lands, because a short ladder that then
  grows is worse than a moment of honesty. What was chosen last is remembered
  per model in localStorage so the trigger can name it before any probe, and
  the choice itself is stored server-side, in prefs, along with the model.
*/
const ErisPicker = (() => {
  const KEY = "eris-effort"; // model id -> the step last chosen for it
  const TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';

  let box, input;
  let modelBtn, modelMenu, modelLabel, glow;
  let effortBtn, effortMenu, effortLabel, track, rail, level;

  let ids = []; // model ids, in menu order
  let labels = {}; // id -> the name shown for it
  const nameOf = (id) => labels[id] || id;
  let caps = {}; // id -> what it declared, for the ids that declared anything
  let chosen = ""; // the model that is in use
  let active = 0; // the row the pointer or the arrows are on
  let rowEls = [];
  let glowShown = false;
  let modelOpen = false;
  let effortOpen = false;
  let onModel = () => {};
  let onEffort = () => {};

  // The ladder for one model, kept with it: `efforts` is what the server
  // reported for `effortsFor`, and both drop when the model changes. `null`
  // means the answer has not arrived.
  let efforts = null; // [{value, label}], lowest first
  let effortsFor = null; // the model `efforts` describes
  let always = false; // one step, fixed: the model's own default
  let effort = 0; // index into `efforts`
  let probe = 0; // the newest probe, so a stale answer is dropped

  // Steps sit at even fractions of the travel span, not of the track: the span
  // is inset by the thumb's own radius, so the thumb's centre reaches a step
  // exactly when its edge reaches the rail's end.
  const stepAt = (i) => `${(i / Math.max(1, (efforts || [0]).length - 1)) * 100}%`;

  // --- the model menu -------------------------------------------------------

  function paintModels() {
    if (!modelMenu) return;
    modelMenu.textContent = "";
    glow = null;
    rowEls = [];

    // The hover fill slides between rows, so it is one element behind them
    // rather than a background on whichever row the pointer is over.
    glow = document.createElement("span");
    glow.className = "pick-glow";
    glow.setAttribute("aria-hidden", "true");
    modelMenu.append(glow);

    rowEls = ids.map((id, i) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "pick-row";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === active));

      const name = document.createElement("span");
      name.className = "pick-row-name";
      name.textContent = nameOf(id);
      // The label is the readable half of the id and the id is the whole of it,
      // so the full string stays reachable where the shortened one is shown.
      row.title = id;

      const check = document.createElement("span");
      check.className = "pick-row-check";
      if (id === chosen) check.dataset.on = "";
      check.innerHTML = TICK;

      row.append(name, check);
      // A press that would move focus is swallowed, so the caret never leaves
      // the field and only the click lands.
      row.addEventListener("mousedown", (e) => e.preventDefault());
      row.addEventListener("pointerenter", () => setActive(i));
      row.addEventListener("click", () => choose(i));
      modelMenu.append(row);
      return row;
    });

    if (modelLabel && chosen) modelLabel.textContent = nameOf(chosen);
    // The readout beside Send describes whichever model is in use, so it is
    // told whenever this one changes rather than asking for itself.
    if (chosen) ErisStats?.setModel(chosen, nameOf(chosen));
  }

  function setActive(i) {
    if (i === active) return;
    rowEls[active]?.setAttribute("aria-selected", "false");
    active = i;
    rowEls[active]?.setAttribute("aria-selected", "true");
    moveGlow();
  }

  // The fill travels on a transition, but the first paint after the menu opens
  // arrives with none, so it does not fly in from wherever the last menu was.
  function moveGlow() {
    if (!glow || !modelOpen) return;
    const row = rowEls[active];
    if (!row) {
      glow.style.opacity = "0";
      return;
    }
    if (!glowShown) glow.style.transition = "none";
    glow.style.top = `${row.offsetTop}px`;
    glow.style.height = `${row.offsetHeight}px`;
    glow.style.opacity = "1";
    if (!glowShown) {
      void glow.offsetHeight;
      glow.style.transition = "";
      glowShown = true;
    }
  }

  function choose(i) {
    const id = ids[i];
    if (!id) return;
    const changed = id !== chosen;
    chosen = id;
    active = i;
    paintModels();
    close();
    input?.focus({ preventScroll: true });
    if (changed) onModel(id);
  }

  // --- the effort slider ----------------------------------------------------

  // The panel is built from the ladder the server reported, so it is rebuilt
  // when that answer changes and never before: with no ladder there is nothing
  // to draw, and the head says so instead of showing empty marks.
  function paintEffort() {
    if (!effortMenu) return;
    effortMenu.textContent = "";

    // The label names the control and the value answers it, so they share one
    // line instead of stacking; "Faster"/"Smarter" sit under the rail because
    // they describe its two ends, not the panel.
    const head = document.createElement("div");
    head.className = "pick-effort-head";
    const title = document.createElement("span");
    title.className = "pick-effort-title";
    title.textContent = "Effort";
    level = document.createElement("span");
    level.className = "pick-effort-level";
    head.append(title, level);
    effortMenu.append(head);

    if (!efforts) {
      // A probe is a round trip, and a rail drawn from a guess would have to
      // change shape when the answer lands. The head holds the line instead.
      head.dataset.pending = "";
      level.textContent = "Checking...";
      return;
    }

    tracks();
    if (!always) {
      const ends = document.createElement("div");
      ends.className = "pick-effort-ends";
      const fast = document.createElement("span");
      fast.textContent = "Faster";
      const smart = document.createElement("span");
      smart.textContent = "Smarter";
      ends.append(fast, smart);
      effortMenu.append(ends);
    }
  }

  function tracks() {
    track = document.createElement("div");
    track.className = "pick-effort-track";
    track.setAttribute("role", "slider");
    track.tabIndex = 0;
    track.setAttribute("aria-label", "Effort");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(efforts.length - 1));
    // One step is not a choice, so the rail is drawn but never answers a
    // pointer or a key: a control that cannot move must not look like one.
    if (always) {
      track.dataset.fixed = "";
      track.removeAttribute("tabindex");
    }

    // The rail is the thumb's travel: CSS insets it by half a thumb at each
    // end, so one percentage puts the thumb flush with the track at either
    // extreme and drives the fill, the marks and the thumb alike.
    rail = document.createElement("span");
    rail.className = "pick-effort-rail";
    const fill = document.createElement("span");
    fill.className = "pick-effort-fill";
    rail.append(fill);
    // Steps sit over the fill and under the thumb, so the thumb covers the one
    // it is on and the bar keeps reading as one piece.
    for (let i = 0; i < efforts.length; i++) {
      const dot = document.createElement("i");
      dot.className = "pick-effort-dot";
      dot.style.left = stepAt(i);
      rail.append(dot);
    }
    const thumb = document.createElement("span");
    thumb.className = "pick-effort-thumb";
    rail.append(thumb);
    track.append(rail);

    // The press is cancelled instead of answered with a focus() call: the rail
    // never takes focus from the pointer, so it shows no ring for a click that
    // was not a Tab, and the caret stays in the field where the rest of the
    // composer keeps it. The grip is a flag for the same reason, because :active
    // hangs off the default this cancels.
    track.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || always) return;
      e.preventDefault();
      track.dataset.grab = "";
      if (e.pointerId !== undefined) {
        try {
          track.setPointerCapture(e.pointerId);
        } catch {}
      }
      effortFromPointer(e);
    });
    track.addEventListener("pointermove", (e) => {
      if (!always && e.buttons & 1) effortFromPointer(e);
    });
    const ungrip = () => delete track.dataset.grab;
    track.addEventListener("pointerup", ungrip);
    track.addEventListener("pointercancel", ungrip);
    track.addEventListener("keydown", onTrackKey);

    effortMenu.append(track);
  }

  function drawEffort() {
    if (!efforts || !rail) {
      if (effortLabel) effortLabel.textContent = "Effort";
      return;
    }
    // The fill runs to the thumb's centre, where the opaque thumb covers it, so
    // the bar reads as one piece and no step needs a special case.
    rail.style.setProperty("--pick-x", stepAt(effort));
    rail.style.setProperty("--pick-fill", stepAt(effort));
    track.setAttribute("aria-valuenow", String(effort));
    track.setAttribute("aria-valuetext", efforts[effort].label);
    level.textContent = efforts[effort].label;
    if (effortLabel) effortLabel.textContent = efforts[effort].label;
    // The top step is the one state either control calls out, and a ladder
    // with one step is already at it.
    const maxed = always || effort === efforts.length - 1;
    for (const el of [effortBtn, effortMenu]) el.toggleAttribute("data-max", maxed);
  }

  function setEffort(i, remember) {
    if (!efforts || always) return;
    const next = Math.max(0, Math.min(efforts.length - 1, i));
    if (next === effort) return;
    effort = next;
    drawEffort();
    if (remember) {
      saveEffort(effort);
      onEffort(effort);
    }
  }

  // Measured against the rail, whose width is the thumb's travel, so the ends
  // of the panel map to the first and last step with no edge correction.
  function effortFromPointer(e) {
    const r = rail.getBoundingClientRect();
    const k = (e.clientX - r.left) / Math.max(1, r.width);
    setEffort(Math.round(k * (efforts.length - 1)), true);
  }

  function onTrackKey(e) {
    if (always) return;
    const step =
      e.key === "ArrowRight" || e.key === "ArrowUp" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowDown" ? -1 : 0;
    if (step) {
      e.preventDefault();
      setEffort(effort + step, true);
    } else if (e.key === "Home") {
      e.preventDefault();
      setEffort(0, true);
    } else if (e.key === "End") {
      e.preventDefault();
      setEffort(efforts.length - 1, true);
    }
  }

  // --- opening and closing --------------------------------------------------

  // A panel opens above the whole field, aligned to the control that opened it,
  // so a menu is never separated from its trigger. The alignment gives way when
  // the control sits too far right for the panel to clear the field's edge,
  // which is what the effort button does on a narrow screen.
  function show(menu, btn) {
    menu.hidden = false;
    // offsetLeft counts the field's own border, which the panel's left edge
    // does not include.
    const align = btn.offsetLeft - box.clientLeft;
    const furthest = Math.max(0, box.clientWidth - menu.offsetWidth);
    menu.style.left = `${Math.min(align, furthest)}px`;
    btn.setAttribute("aria-expanded", "true");
  }

  function close() {
    if (modelOpen) {
      modelOpen = false;
      glowShown = false;
      modelMenu.hidden = true;
      modelBtn.setAttribute("aria-expanded", "false");
    }
    if (effortOpen) {
      effortOpen = false;
      effortMenu.hidden = true;
      effortBtn.setAttribute("aria-expanded", "false");
    }
  }

  function openModel() {
    if (!ids.length) return;
    close();
    modelOpen = true;
    active = Math.max(0, ids.indexOf(chosen));
    rowEls.forEach((row, i) => row.setAttribute("aria-selected", String(i === active)));
    show(modelMenu, modelBtn);
    moveGlow();
  }

  function openEffort() {
    close();
    effortOpen = true;
    show(effortMenu, effortBtn);
    // Opening the panel is the first moment the ladder is needed, so it is the
    // moment it is asked for: a model nobody opens the panel for is never
    // probed, and the answer is cached server-side once it is.
    if (effortsFor !== chosen) loadEfforts(chosen);
  }

  // The answer is per model, and the model can change while a probe is in
  // flight, so each request carries a ticket and only the newest one lands.
  function loadEfforts(model) {
    const ticket = ++probe;
    effortsFor = model;
    efforts = null;
    always = false;
    effort = readSaved(model);
    paintEffort();
    drawEffort();
    fetch(`/api/effort?model=${encodeURIComponent(model)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`effort ${r.status}`))))
      .then((j) => {
        if (ticket !== probe) return;
        efforts = (j && Array.isArray(j.efforts) && j.efforts.length) ? j.efforts : [{ value: 0, label: "Off" }];
        always = !!(j && j.always);
        // The stored index was remembered against whatever ladder was there
        // then, so it is clamped into this one rather than trusted.
        effort = Math.max(0, Math.min(Number(j && j.current) || 0, efforts.length - 1));
        saveEffort(effort);
        paintEffort();
        drawEffort();
      })
      .catch(() => {
        if (ticket !== probe) return;
        // The probe failed, so nothing is known. The plain pair is what the
        // request would carry anyway, and the control keeps working. The model
        // is forgotten with it, so opening the panel again asks once more
        // rather than keeping a guess a transient failure produced.
        effortsFor = null;
        efforts = [{ value: 0, label: "Off" }, { value: 1, label: "On" }];
        always = false;
        effort = Math.min(effort, efforts.length - 1);
        paintEffort();
        drawEffort();
      });
  }

  // The caret stays in the field for the whole interaction, so the menu's own
  // keys arrive at the field rather than at a row. This listens on the document
  // in the capture phase and stops the keys it consumes, because the field's
  // own Enter handler submits the form and would otherwise send the message the
  // menu is still being chosen for.
  function onKeyDown(e) {
    if (e.key === "Escape" && (modelOpen || effortOpen)) {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (!modelOpen || !rowEls.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      setActive((active + (e.key === "ArrowDown" ? 1 : rowEls.length - 1)) % rowEls.length);
      rowEls[active].scrollIntoView({ block: "nearest" });
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      e.stopPropagation();
      choose(active);
    }
  }

  // --- wiring ---------------------------------------------------------------

  function init(opts = {}) {
    box = document.getElementById("box");
    modelBtn = document.getElementById("modelPick");
    modelMenu = document.getElementById("modelMenu");
    modelLabel = document.getElementById("modelPickName");
    effortBtn = document.getElementById("effortPick");
    effortMenu = document.getElementById("effortMenu");
    effortLabel = document.getElementById("effortPickName");
    if (!box || !modelBtn || !effortBtn) return;
    input = box.querySelector("textarea");
    onModel = opts.onModel || onModel;
    onEffort = opts.onEffort || onEffort;

    // Nothing is drawn yet: the ladder belongs to the model, and the model
    // arrives with setModels. Until then the trigger names the control alone.
    paintEffort();
    drawEffort();

    modelBtn.addEventListener("click", () => (modelOpen ? close() : openModel()));
    effortBtn.addEventListener("click", () => (effortOpen ? close() : openEffort()));
    // The field keeps the focus: a press on a trigger must not take it, or the
    // caret would leave the box the panel is anchored to.
    for (const btn of [modelBtn, effortBtn]) {
      btn.addEventListener("mousedown", (e) => e.preventDefault());
    }

    document.addEventListener("pointerdown", (e) => {
      if ((modelOpen || effortOpen) && !box.contains(e.target)) close();
    });
    input?.addEventListener("focus", close);
    document.addEventListener("keydown", onKeyDown, true);
  }

  // The remembered step is per model, because the same index means different
  // things on two ladders. It only seeds the control until the server's answer
  // arrives, which then replaces it.
  function saved() {
    try {
      return JSON.parse(localStorage.getItem(KEY) || "{}") || {};
    } catch {
      return {};
    }
  }

  function readSaved(model) {
    return Math.max(0, Number(saved()[model]) || 0);
  }

  function saveEffort(i) {
    try {
      const all = saved();
      all[chosen] = i;
      localStorage.setItem(KEY, JSON.stringify(all));
    } catch {}
  }

  function setModels(items, current, names, capabilities) {
    const model = current || (items || [])[0] || "";
    const changed = model !== chosen;
    ids = (items || []).slice();
    labels = names || {};
    caps = capabilities || {};
    chosen = model;
    active = Math.max(0, ids.indexOf(chosen));
    paintModels();
    // A model that declared it cannot think has no ladder to draw, so the
    // control is hidden rather than left to answer a question with one step.
    // A model that declared nothing about thinking keeps it: unknown is not
    // the same as absent, and a control that vanishes on a quiet server would
    // take the feature with it.
    const thinks = caps[chosen] ? caps[chosen].thinking !== false : true;
    if (effortBtn) effortBtn.hidden = !thinks;
    if (!thinks) close();
    // The composer reads the same answer to decide what it can attach, so it
    // is told whenever the model changes rather than asking for itself.
    if (chosen) ErisAttach?.setVision(caps[chosen] ? caps[chosen].vision !== false : true);
    if (changed) {
      // A new model means a new ladder, so the old one is dropped rather than
      // left to label the new model's steps.
      efforts = null;
      effortsFor = null;
      effort = readSaved(chosen);
      if (effortOpen) loadEfforts(chosen);
      else paintEffort();
      drawEffort();
    }
    return ids.length;
  }

  return { init, setModels };
})();
