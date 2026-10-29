/* The attachment chips above the composer: what was picked, what was read from
   each file, and the row that shows it. The composer asks for images when it
   sends and for a yes/no when it decides whether Send is live. */

const ErisAttach = (() => {
  const MAX_ATTS = 8;
  const X_ICON = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';
  const IMG_ICON =
    '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 16l5-5 4 4 3-3 6 6" /><circle cx="9" cy="9" r="1.4" /></svg>';

  const row = document.getElementById("atts");
  let items = [];
  let seq = 0;
  let changed = () => {};
  // Whether the model in use declared it can see an image. A chip is still
  // shown when it cannot, marked as refused, because a file that vanished
  // silently reads as a broken drop rather than as a model that cannot see.
  let vision = true;

  function classify(file) {
    if (file.type && file.type.startsWith("image/")) return "image";
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

  // A photo straight off a phone is several megabytes of pixels the model will
  // never resolve. Anything past 1280px or a megabyte is re-encoded once, at
  // 0.85, in the same format the browser can already draw.
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

  async function add(fileList) {
    for (const f of [...fileList]) {
      if (items.length >= MAX_ATTS) break;
      const kind = classify(f);
      const a = { id: ++seq, name: f.name || "file", kind, file: f, status: "ok" };
      if (!kind) {
        a.status = "err";
        a.msg = "unsupported type";
        items.push(a);
        render();
        continue;
      }
      if (kind === "image" && !vision) {
        // Kept out of `okImages`, so Send stays honest about what will be sent.
        a.status = "err";
        a.msg = "model ini tidak bisa lihat gambar";
        items.push(a);
        render();
        continue;
      }
      a.status = "busy";
      a.msg = "reading...";
      items.push(a);
      render();
      try {
        a.dataUrl = await shrinkToDataURL(f);
        a.status = "ok";
        a.msg = "";
      } catch {
        a.status = "err";
        a.msg = "read failed";
      }
      render();
    }
  }

  function chip(a) {
    const el = document.createElement("span");
    const thumb = a.kind === "image" && a.dataUrl;
    el.className = "chip" + (thumb ? " chip-img" : "") + (a.status === "err" ? " err" : "");
    el.title = a.msg ? `${a.name} — ${a.msg}` : a.name;

    if (thumb) {
      const im = document.createElement("img");
      im.className = "chip-thumb";
      im.src = a.dataUrl;
      im.alt = a.name;
      el.append(im);
    } else {
      const box = document.createElement("span");
      box.innerHTML = IMG_ICON;
      el.append(box.firstChild);
      const nm = document.createElement("span");
      nm.className = "chip-name";
      nm.textContent = a.name;
      el.append(nm);
      if (a.status === "err" && a.msg) {
        const note = document.createElement("span");
        note.className = "chip-note";
        note.textContent = a.msg;
        el.append(note);
      } else if (a.status === "busy") {
        const note = document.createElement("span");
        note.className = "chip-note";
        note.textContent = "reading...";
        el.append(note);
      }
    }

    const x = document.createElement("button");
    x.type = "button";
    x.className = "chip-x";
    x.title = "Remove";
    x.setAttribute("aria-label", `Remove ${a.name}`);
    x.innerHTML = X_ICON;
    x.addEventListener("click", () => {
      items = items.filter((t) => t.id !== a.id);
      render();
    });
    el.append(x);
    return el;
  }

  function render() {
    row.hidden = items.length === 0;
    row.innerHTML = "";
    for (const a of items) row.append(chip(a));
    changed();
  }

  function clear() {
    items = [];
    render();
  }

  const okImages = () => items.filter((a) => a.kind === "image" && a.status === "ok");

  return {
    add,
    render,
    clear,
    // What the model in use declared about images, told by the picker when the
    // model changes. An undeclared model keeps `true`: nothing was said.
    setVision: (on) => { vision = on !== false; },
    hasImage: () => okImages().length > 0,
    images: () => okImages().map((a) => a.dataUrl),
    // Called after every change so the composer can re-decide whether Send is
    // live without the attachment row knowing about the input.
    onChange: (fn) => { changed = fn; },
  };
})();