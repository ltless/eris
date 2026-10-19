/* The dialog shell: open, close, focus, section rail, and the URL hash.
   Library and Settings both use it, so neither has to reimplement Escape, the
   focus trap, or focus restoration.

   The markup contract is in dialog.css. Everything the caller owns is passed
   in: the root, its opener, and two hooks — `onEscape` for a surface that has
   its own step to give up first, and `onHide` for state that must not outlive
   the dialog.

   A module: it is imported by the two page-level modules, and its name would
   otherwise land in the global object every other page script declares into. */

/* Every dialog on the page, so opening one can close the others. */
const dialogs = [];

export function createDialog({ root, opener, onEscape, onHide, onShow }) {
  const sheet = root.querySelector(".dlg-sheet");
  const nav = root.querySelector("[data-nav]");
  const hash = root.dataset.hash;
  let returnTo = null;

  function isOpen() {
    return !root.hidden;
  }

  /* What is behind the sheet — the whole chat page — leaves the accessibility
      tree while a dialog is open, so a screen reader cannot walk into it
      around the focus trap. */
  function setBehind(on) {
    for (const el of document.querySelectorAll(".layout, .side-scrim")) {
      if (on) el.setAttribute("inert", "");
      else el.removeAttribute("inert");
    }
  }

  /* Only what is rendered and reachable counts, which keeps a hidden panel's
     controls out of the cycle. */
  function focusables() {
    return [...sheet.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )].filter((el) => !el.disabled && el.offsetParent !== null);
  }

  function select(section) {
    if (!nav) return;
    for (const b of nav.querySelectorAll("button[data-sec]")) {
      const on = b.dataset.sec === section;
      b.classList.toggle("on", on);
      // A button is not a page, so "true" rather than "page": the current item
      // in this rail, not the current URL.
      if (on) b.setAttribute("aria-current", "true");
      else b.removeAttribute("aria-current");
    }
    for (const panel of root.querySelectorAll(".dlg-panel")) {
      panel.hidden = panel.id !== `sec-${section}`;
    }
  }

  function open() {
    if (isOpen()) return;
    // Only one dialog at a time: a second one would otherwise register a second
    // Escape handler and a second focus trap against the same document.
    for (const other of dialogs) if (other !== api) other.close();
    returnTo = document.activeElement;
    root.hidden = false;
    setBehind(true);
    opener?.setAttribute("aria-expanded", "true");
    // The drawer sits under the scrim at this width, so it closes first.
    if (matchMedia("(max-width:768px)").matches) window.ErisSidebar?.close();
    sheet.focus();
    onShow?.();
  }

  function close() {
    if (!isOpen()) return;
    root.hidden = true;
    setBehind(false);
    opener?.setAttribute("aria-expanded", "false");
    onHide?.();
    if (returnTo && returnTo.isConnected) returnTo.focus();
    returnTo = null;
  }

  /* The hash is kept with replaceState, so the browser's Back button leaves the
     chat page instead of walking through every open and close. */
  function setHash(on) {
    const wanted = on && hash ? `#${hash}` : "";
    if (location.hash === wanted) return;
    history.replaceState(null, "", location.pathname + location.search + wanted);
  }

  function dismiss() {
    close();
    setHash(false);
  }

  opener?.addEventListener("click", () => {
    open();
    setHash(true);
  });

  for (const el of root.querySelectorAll("[data-close]")) {
    el.addEventListener("click", dismiss);
  }

  if (nav) {
    nav.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-sec]");
      if (b) select(b.dataset.sec);
    });
  }

  document.addEventListener("keydown", (e) => {
    if (!isOpen()) return;

    // A surface with its own pending step gives that up on the first Escape.
    if (e.key === "Escape") {
      e.preventDefault();
      if (onEscape?.()) return;
      dismiss();
      return;
    }

    // Tab cycles inside the sheet while it is open.
    if (e.key !== "Tab") return;
    const items = focusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const inside = sheet.contains(document.activeElement);
    const leaving = e.shiftKey
      ? document.activeElement === first || !inside
      : document.activeElement === last || !inside;
    if (!leaving) return;
    e.preventDefault();
    (e.shiftKey ? last : first).focus();
  });

  addEventListener("hashchange", () => {
    if (hash && location.hash === `#${hash}`) open();
    else close();
  });

  const api = { open, close, isOpen, select };
  dialogs.push(api);
  return api;
}
