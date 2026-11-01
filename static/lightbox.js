/* The attachment viewer. A click on a photo opens it over the chat instead of
   a new tab: a data: URL is not allowed to become a top frame, so window.open
   only produced a stalled tab, and the bytes were already decoded here. */
window.ErisLightbox = (() => {
  let root = null;
  let img = null;
  let returnTo = null;

  function build() {
    root = document.createElement("div");
    root.className = "lb";
    root.hidden = true;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", "Attachment");

    const scrim = document.createElement("div");
    scrim.className = "lb-scrim";

    img = document.createElement("img");
    img.className = "lb-img";
    img.tabIndex = -1;

    root.append(scrim, img);
    scrim.addEventListener("click", close);
    img.addEventListener("click", close);
    document.addEventListener("keydown", (e) => {
      if (root.hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      // The photo is the only stop here, so Tab stays on it rather than
      // walking into a page that is inert behind the overlay.
      if (e.key === "Tab") {
        e.preventDefault();
        img.focus();
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

  function open(src, alt, opener) {
    if (!root) build();
    returnTo = opener || null;
    img.src = src;
    img.alt = alt || "Attachment";
    root.hidden = false;
    setBehind(true);
    img.focus();
  }

  function close() {
    if (!root || root.hidden) return;
    root.hidden = true;
    img.removeAttribute("src");
    setBehind(false);
    if (returnTo && returnTo.isConnected) returnTo.focus();
    returnTo = null;
  }

  return { open, close, isOpen: () => !!root && !root.hidden };
})();