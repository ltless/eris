/* The file viewer. A sent file chip is a name, not the contents, so a click
   opens the text the model was handed, over the chat rather than in a new tab:
   the text was read in the browser and never became a URL. */
window.ErisFileView = (() => {
  let root = null;
  let nameEl = null;
  let pre = null;
  let closeBtn = null;
  let returnTo = null;

  const X_ICON = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';

  function build() {
    root = document.createElement("div");
    root.className = "fv";
    root.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");

    const scrim = document.createElement("div");
    scrim.className = "fv-scrim";

    const sheet = document.createElement("div");
    sheet.className = "fv-sheet";

    const head = document.createElement("div");
    head.className = "fv-head";
    nameEl = document.createElement("span");
    nameEl.className = "fv-name";
    nameEl.id = "fv-name";
    closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "fv-close";
    closeBtn.setAttribute("aria-label", "Close file");
    closeBtn.innerHTML = X_ICON;
    closeBtn.addEventListener("click", close);
    head.append(nameEl, closeBtn);

    pre = document.createElement("pre");
    pre.className = "fv-body";
    pre.tabIndex = -1;

    sheet.append(head, pre);
    root.append(scrim, sheet);
    root.setAttribute("aria-labelledby", "fv-name");
    scrim.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (root.hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      // Only the sheet and the close button are reachable here, so Tab cycles
      // between them instead of walking into the inert page behind.
      if (e.key === "Tab") {
        e.preventDefault();
        (document.activeElement === closeBtn ? pre : closeBtn).focus();
      }
    });
    document.body.append(root);
  }

  function setBehind(on) {
    for (const el of document.querySelectorAll(".layout, .side-scrim")) {
      if (on) el.setAttribute("inert", "");
      else el.removeAttribute("inert");
    }
  }

  function open(file, opener) {
    if (!root) build();
    returnTo = opener || null;
    const name = file?.name || "file";
    nameEl.textContent = name;
    nameEl.title = name;
    // A file sent with no text still opens: it says why it is empty rather
    // than showing a blank sheet.
    pre.textContent = file?.text || "(empty file)";
    root.hidden = false;
    pre.scrollTop = 0;
    setBehind(true);
    closeBtn.focus();
  }

  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    pre.textContent = "";
    setBehind(false);
    if (returnTo && returnTo.isConnected) returnTo.focus();
    returnTo = null;
  }

  return { open, close, isOpen: () => !!root && !root.hidden };
})();