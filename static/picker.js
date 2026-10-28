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

  ErisPicker.init({ onModel })       wire both triggers and their panels; `onModel`
                                     runs with the new id when the choice changes
  ErisPicker.setModels(ids, id, labels)
                                     fill the model menu, marking `id` as chosen.
                                     `labels` maps an id to the name shown for it;
                                     an id without one is shown as itself.

  The effort step is kept in localStorage and nowhere else: the slider draws
  and remembers a choice that no request carries yet.
*/
const ErisPicker = (() => {
  const EFFORTS = ["Low", "Medium", "High", "Extra", "Max"];
  const KEY = "eris-effort";
  const TICK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';

  let box, input;
  let modelBtn, modelMenu, modelLabel, glow;
  let effortBtn, effortMenu, effortLabel, track, rail, level;

  let ids = []; // model ids, in menu order
  let labels = {}; // id -> the name shown for it
  const nameOf = (id) => labels[id] || id;
  let chosen = ""; // the model that is in use
  let active = 0; // the row the pointer or the arrows are on
  let rowEls = [];
  let glowShown = false;
  let modelOpen = false;
  let effortOpen = false;
  let effort = 0;
  let onModel = () => {};

  // Steps sit at even fractions of the travel span, not of the track: the span
  // is inset by the thumb's own radius, so the thumb's centre reaches a step
  // exactly when its edge reaches the rail's end.
  const stepAt = (i) => `${(i / Math.max(1, EFFORTS.length - 1)) * 100}%`;

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

    track = document.createElement("div");
    track.className = "pick-effort-track";
    track.setAttribute("role", "slider");
    track.tabIndex = 0;
    track.setAttribute("aria-label", "Effort");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(EFFORTS.length - 1));

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
    for (let i = 0; i < EFFORTS.length; i++) {
      const dot = document.createElement("i");
      dot.className = "pick-effort-dot";
      dot.style.left = stepAt(i);
      rail.append(dot);
    }
    const thumb = document.createElement("span");
    thumb.className = "pick-effort-thumb";
    rail.append(thumb);
    track.append(rail);

    const ends = document.createElement("div");
    ends.className = "pick-effort-ends";
    const fast = document.createElement("span");
    fast.textContent = "Faster";
    const smart = document.createElement("span");
    smart.textContent = "Smarter";
    ends.append(fast, smart);

    // The press is cancelled instead of answered with a focus() call: the rail
    // never takes focus from the pointer, so it shows no ring for a click that
    // was not a Tab, and the caret stays in the field where the rest of the
    // composer keeps it. The grip is a flag for the same reason, because :active
    // hangs off the default this cancels.
    track.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
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
      if (e.buttons & 1) effortFromPointer(e);
    });
    const ungrip = () => delete track.dataset.grab;
    track.addEventListener("pointerup", ungrip);
    track.addEventListener("pointercancel", ungrip);
    track.addEventListener("keydown", onTrackKey);

    effortMenu.append(head, track, ends);
  }

  function drawEffort() {
    // The fill runs to the thumb's centre, where the opaque thumb covers it, so
    // the bar reads as one piece and no step needs a special case.
    rail.style.setProperty("--pick-x", stepAt(effort));
    rail.style.setProperty("--pick-fill", stepAt(effort));
    track.setAttribute("aria-valuenow", String(effort));
    track.setAttribute("aria-valuetext", EFFORTS[effort]);
    level.textContent = EFFORTS[effort];
    if (effortLabel) effortLabel.textContent = EFFORTS[effort];
    const maxed = effort === EFFORTS.length - 1;
    for (const el of [effortBtn, effortMenu]) el.toggleAttribute("data-max", maxed);
  }

  function setEffort(i, remember) {
    const next = Math.max(0, Math.min(EFFORTS.length - 1, i));
    if (next === effort) return;
    effort = next;
    drawEffort();
    if (remember) {
      try {
        localStorage.setItem(KEY, EFFORTS[effort]);
      } catch {}
    }
  }

  // Measured against the rail, whose width is the thumb's travel, so the ends
  // of the panel map to the first and last step with no edge correction.
  function effortFromPointer(e) {
    const r = rail.getBoundingClientRect();
    const k = (e.clientX - r.left) / Math.max(1, r.width);
    setEffort(Math.round(k * (EFFORTS.length - 1)), true);
  }

  function onTrackKey(e) {
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
      setEffort(EFFORTS.length - 1, true);
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

    paintEffort();
    const saved = EFFORTS.indexOf(readSaved());
    effort = saved >= 0 ? saved : Math.floor((EFFORTS.length - 1) / 2);
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

  function readSaved() {
    try {
      return localStorage.getItem(KEY) || "";
    } catch {
      return "";
    }
  }

  function setModels(items, current, names) {
    ids = (items || []).slice();
    labels = names || {};
    chosen = current || ids[0] || "";
    active = Math.max(0, ids.indexOf(chosen));
    paintModels();
    return ids.length;
  }

  return { init, setModels };
})();
