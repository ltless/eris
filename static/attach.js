/* The attachment chips above the composer: what was picked, what was read from
   each file, and the row that shows it. The composer asks for images when it
   sends and for a yes/no when it decides whether Send is live. */

const ErisAttach = (() => {
  const MAX_ATTS = 8;
  const MAX_FILE_CHARS = 40_000;
  // Anything past this is refused before it is read: a file that big is not a
  // question, and reading it would freeze the tab for no answer.
  const MAX_FILE_BYTES = 2_000_000;
  const X_ICON = '<svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';
  const IMG_ICON =
    '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 16l5-5 4 4 3-3 6 6" /><circle cx="9" cy="9" r="1.4" /></svg>';
  // One glyph per kind of text file: at 15px the file outline is the constant
  // and the mark inside it is what tells a script from a data file.
  const ICONS = {
    code: '<svg viewBox="0 0 24 24"><path d="M14 3v5h5" /><path d="M19 21H5V3h9l5 5v13z" /><path d="M10.5 12.5L8.5 15l2 2.5" /><path d="M13.5 12.5l2 2.5-2 2.5" /></svg>',
    data: '<svg viewBox="0 0 24 24"><path d="M14 3v5h5" /><path d="M19 21H5V3h9l5 5v13z" /><path d="M5 12h14" /><path d="M11 12v9" /></svg>',
    config: '<svg viewBox="0 0 24 24"><path d="M14 3v5h5" /><path d="M19 21H5V3h9l5 5v13z" /><path d="M8 12h8" /><path d="M8 16.5h8" /><circle cx="10.5" cy="12" r="1.4" /><circle cx="13.5" cy="16.5" r="1.4" /></svg>',
    text: '<svg viewBox="0 0 24 24"><path d="M14 3v5h5" /><path d="M19 21H5V3h9l5 5v13z" /><path d="M8.5 13h7" /><path d="M8.5 16.5h4.5" /></svg>',
  };
  const CODE_EXT = new Set([
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "py", "rb", "go", "rs", "java", "kt", "swift",
    "c", "h", "cc", "cpp", "hpp", "cs", "php", "lua", "sh", "bash", "zsh", "ps1", "bat",
    "sql", "graphql", "html", "htm", "xml", "css", "scss", "svelte", "vue",
  ]);
  const DATA_EXT = new Set(["json", "jsonl", "yaml", "yml", "csv", "tsv"]);
  const CONFIG_EXT = new Set([
    "toml", "ini", "cfg", "conf", "env", "properties", "lock", "dockerfile", "makefile", "gitignore",
  ]);

  function iconFor(name) {
    const lower = String(name || "").toLowerCase();
    const ext = lower.split(".").pop();
    if (DATA_EXT.has(ext)) return ICONS.data;
    if (CONFIG_EXT.has(ext) || CONFIG_EXT.has(lower)) return ICONS.config;
    if (CODE_EXT.has(ext) || CODE_EXT.has(lower)) return ICONS.code;
    return ICONS.text;
  }

  const row = document.getElementById("atts");
  let items = [];
  let seq = 0;
  let changed = () => {};
  // Whether the model in use declared it can see an image. A chip is still
  // shown when it cannot, marked as refused, because a file that vanished
  // silently reads as a broken drop rather than as a model that cannot see.
  let vision = true;

  // A file the model can read as text. The extension is what decides, because a
// browser reports `""` for most of these and `application/octet-stream` for the
// rest; anything typed as text/* is taken as well, so a `.env` or a `.gitignore`
// still lands here.
  const TEXT_EXT = new Set([
    "txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "env", "properties", "xml", "html", "htm", "css", "scss",
    "js", "mjs", "cjs", "jsx", "ts", "tsx", "py", "rb", "go", "rs", "java", "kt", "swift",
    "c", "h", "cc", "cpp", "hpp", "cs", "php", "lua", "sh", "bash", "zsh", "ps1", "bat",
    "sql", "graphql", "svelte", "vue", "dockerfile", "makefile", "gitignore", "lock",
  ]);

  function classify(file) {
    if (file.type && file.type.startsWith("image/")) return "image";
    if (file.type && file.type.startsWith("text/")) return "text";
    const name = (file.name || "").toLowerCase();
    if (TEXT_EXT.has(name.split(".").pop()) || TEXT_EXT.has(name)) return "text";
    return null;
  }

  // Read as text, capped at the same limit the server applies, so what was
  // shown here is what the model is handed.
  function readText(file) {
    return file.slice(0, MAX_FILE_CHARS * 4).text().then((raw) => {
      const over = raw.length > MAX_FILE_CHARS;
      return { text: over ? raw.slice(0, MAX_FILE_CHARS) : raw, cut: over };
    });
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
  // never resolve, and whatever is drawn here is what the server ends up
  // storing. The longest side is capped at 1024, and everything but a GIF comes
  // back as JPEG: a PNG round-trip ignores the quality argument, so a screenshot
  // would keep its full lossless weight all the way into the database.
  const MAX_SIDE = 1024;
  const QUALITY = 0.75;

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
      const scale = Math.min(1, MAX_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      // A JPEG already this small is left alone: re-encoding it would cost a
      // generation of quality and save nothing.
      if (scale === 1 && file.size <= 1_000_000 && file.type === "image/jpeg") return raw;
      const c = document.createElement("canvas");
      c.width = Math.round(img.naturalWidth * scale);
      c.height = Math.round(img.naturalHeight * scale);
      const ctx = c.getContext("2d");
      // JPEG has no alpha channel, and an unpainted canvas paints black.
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.drawImage(img, 0, 0, c.width, c.height);
      return c.toDataURL("image/jpeg", QUALITY);
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
      // A file past this size is not a question, and reading it would eat the
      // window for nothing, so it is refused rather than attached.
      if (kind === "text" && f.size > MAX_FILE_BYTES) {
        a.status = "err";
        a.msg = "file too big";
        items.push(a);
        render();
        continue;
      }
      if (kind === "text") {
        a.status = "busy";
        a.msg = "reading...";
        items.push(a);
        render();
        try {
          const out = await readText(f);
          a.text = out.text;
          a.status = "ok";
          a.msg = out.cut ? "truncated" : "";
        } catch {
          a.status = "err";
          a.msg = "read failed";
        }
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
      box.innerHTML = a.kind === "text" ? iconFor(a.name) : IMG_ICON;
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
      } else if (a.msg) {
        const note = document.createElement("span");
        note.className = "chip-note";
        note.textContent = a.msg;
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
  const okFiles = () => items.filter((a) => a.kind === "text" && a.status === "ok");

  return {
    add,
    clear,
    // The same glyph a chip wears before sending, so a file looks the same on
    // both sides of the send.
    iconFor,
    // What the model in use declared about images, told by the picker when the
    // model changes. An undeclared model keeps `true`: nothing was said.
    setVision: (on) => { vision = on !== false; },
    hasImage: () => okImages().length > 0,
    hasFile: () => okFiles().length > 0,
    images: () => okImages().map((a) => a.dataUrl),
    // What the model is handed, and what the turn keeps so a reload can draw
    // the same chips.
    files: () => okFiles().map((a) => ({ name: a.name, text: a.text })),
    // Called after every change so the composer can re-decide whether Send is
    // live without the attachment row knowing about the input.
    onChange: (fn) => { changed = fn; },
  };
})();