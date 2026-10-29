/*
  ErisToolCall.thinking(parentElement, { open: false })

  const th = ErisToolCall.thinking(messageEl);
  th.append('reasoning text chunk...');   // per streaming chunk
  th.done();                              // label jadi "Thought for 6s"

  ---

  ErisToolCall.webSearch(parentElement, { open: false })

  const tc = ErisToolCall.webSearch(messageEl);
  tc.addQuery('react 19 features');
  tc.addResults([{ title: '...', url: 'https://...', read: true }]);
  tc.done();
  tc.toggle() / tc.setOpen(true|false)

  ---

  ErisToolCall.fetchPage(parentElement, { open: false })

  const fc = ErisToolCall.fetchPage(messageEl);
  fc.setUrl('https://example.com/a');
  fc.done(4200);        // chars read, or omit while it is still running
  fc.fail('could not read it');
*/
const ErisToolCall = (() => {
  let uid = 0;
  const I = {
    globe: '<svg class="tc-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>',
    page: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></svg>',
    alert: '<svg class="tc-ico-err" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.01" /></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>',
    chev: '<svg class="tc-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>',
    spark: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" /></svg>',
    ext: '<svg class="tc-ext" viewBox="0 0 24 24"><path d="M7 17L17 7M8 7h9v9" /></svg>',
    // The mark on a result whose page was actually opened. Same page glyph as
    // the fetch card, so "read" reads the same wherever it appears.
    read: '<svg class="tc-read-ico" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></svg>',
  };
  const domainOf = (u) => {
    try {
      return new URL(u).hostname.replace(/^www\./, "");
    } catch {
      return typeof u === "string" ? u : "";
    }
  };
  // Favicon chips get a quiet tint instead of the same grey for every site.
  // The hue is hashed from the domain over a fixed palette, so a site keeps
  // its colour across reloads with no network request: a remote favicon
  // service would hand every result domain to a third party, which the
  // self-hosted search stack exists to avoid.
  const HUES = [215, 190, 152, 95, 38, 20, 358, 330, 270, 245];
  const hueOf = (d) => {
    let h = 0;
    for (let i = 0; i < d.length; i++) h = (h * 31 + d.charCodeAt(i)) >>> 0;
    return HUES[h % HUES.length];
  };
  const safeUrl = (u) => (/^https?:\/\//i.test(u) ? u : "#");
  const plural = (n) => (n === 1 ? "1 source" : n + " sources");
  const sizeText = (n) =>
    n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k chars" : n + " chars";

  function shell(parent, ico, labelText, opts) {
    const id = "tc-" + ++uid;
    const root = document.createElement("div");
    root.className = "tc";
    root.dataset.state = "running";
    root.dataset.open = String(!!opts.open);
    root.innerHTML =
      '<button class="tc-head" type="button" aria-expanded="' +
      !!opts.open +
      '" aria-controls="' +
      id +
      '">' +
      ico +
      I.alert +
      '<span class="tc-label" aria-live="polite">' +
      labelText +
      '</span><span class="tc-meta"></span>' +
      I.chev +
      '</button><div class="tc-body" id="' +
      id +
      '"><div class="tc-inner"><div class="tc-panel"></div></div></div>';
    parent.appendChild(root);
    const head = root.querySelector(".tc-head");
    const panel = root.querySelector(".tc-panel");
    const api = {
      el: root,
      head,
      panel,
      label: root.querySelector(".tc-label"),
      meta: root.querySelector(".tc-meta"),
      setOpen: (v) => {
        root.dataset.open = String(v);
        head.setAttribute("aria-expanded", String(v));
      },
      toggle: () => head.click(),
    };
    head.onclick = () => api.setOpen(root.dataset.open !== "true");
    return api;
  }

  // One result row, shared by the search list and the fetch card's own link so
  // a URL looks the same wherever it is shown.
  function resultRow(child) {
    const row = document.createElement("div");
    row.className = "tc-item";
    row.setAttribute("role", "listitem");
    row.append(child);
    return row;
  }

  function linkEl(url, label, domain, mark) {
    const a = document.createElement("a");
    a.className = "tc-link";
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.href = safeUrl(url);
    const fav = document.createElement("span");
    fav.className = "tc-fav";
    fav.textContent = (domain || "?").charAt(0) || "?";
    fav.style.setProperty("--fav-h", String(hueOf(domain || "?")));
    const title = document.createElement("span");
    title.className = "tc-title";
    title.textContent = label || domain;
    const dom = document.createElement("span");
    dom.className = "tc-domain";
    dom.textContent = domain;
    a.append(fav, title);
    if (mark) {
      const rd = document.createElement("span");
      rd.className = "tc-read";
      rd.title = "this page was read in full";
      rd.innerHTML = I.read;
      a.append(rd);
    }
    a.append(dom);
    a.insertAdjacentHTML("beforeend", I.ext);
    return a;
  }

  function webSearch(parent, opts = {}) {
    const s = shell(parent, I.globe, "Searching the web", opts);
    let list = null;
    let total = 0;
    let read = 0;

    function addQuery(q) {
      const g = document.createElement("div");
      g.className = "tc-group";
      g.innerHTML =
        '<div class="tc-q">' + I.search + '<span></span></div><div class="tc-list" role="list"></div>';
      g.querySelector(".tc-q span").textContent = q;
      s.panel.appendChild(g);
      list = g.querySelector(".tc-list");
      s.meta.textContent = q;
      s.panel.querySelector(".tc-empty")?.remove();
    }

    function addResults(items) {
      if (!list) addQuery("Search");
      for (const it of items) {
        const url = it.url || it.source || "";
        const domain = it.domain || domainOf(url);
        const wasRead = !!it.read;
        list.appendChild(
          resultRow(linkEl(url, it.title || domain, domain, wasRead))
        );
        total++;
        if (wasRead) read++;
      }
      // The count is restated as results arrive, so a card left open shows a
      // number that keeps up rather than one frozen at the first batch.
      s.meta.textContent = plural(total) + (read ? ` · ${read} read` : "");
    }

    function done() {
      s.el.dataset.state = "done";
      s.label.textContent = "Searched the web";
      s.meta.textContent = total
        ? plural(total) + (read ? ` · ${read} read` : "")
        : "No results";
      if (!total && !s.panel.querySelector(".tc-empty")) {
        const note = document.createElement("div");
        note.className = "tc-empty";
        note.textContent = "No results found.";
        s.panel.appendChild(note);
      }
    }

    function fail(message) {
      s.el.dataset.state = "error";
      s.label.textContent = "Search failed";
      s.meta.textContent = message || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = message || "Something went wrong while searching.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    const api = { el: s.el, addQuery, addResults, done, fail, setOpen: s.setOpen, toggle: s.toggle };
    return api;
  }

  // The model going back for one page in depth. It is drawn the moment the
  // call starts, so a slow fetch is a visible wait rather than a stall, and
  // the panel keeps the URL as a link: whatever the model read, the reader
  // can open too.
  function fetchPage(parent, opts = {}) {
    const s = shell(parent, I.page, "Reading a page", opts);
    let url = "";
    let chars = 0;

    function setUrl(u) {
      url = String(u || "");
      const domain = domainOf(url);
      s.meta.textContent = domain || url;
      s.panel.innerHTML = "";
      s.panel.appendChild(resultRow(linkEl(url, url, domain, false)));
    }

    function done(n) {
      if (typeof n === "number") chars = n;
      s.el.dataset.state = "done";
      const domain = domainOf(url) || url;
      s.label.textContent = "Read a page";
      s.meta.textContent = chars ? `${domain} · ${sizeText(chars)}` : domain;
    }

    function fail(message) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not read the page";
      s.meta.textContent = message || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = message || "The page could not be read.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    return { el: s.el, setUrl, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  function thinking(parent, opts = {}) {
    const s = shell(parent, I.spark, "Thinking", opts);
    s.el.dataset.kind = "thinking";
    const box = document.createElement("div");
    box.className = "tc-think";
    s.panel.appendChild(box);
    const start = Date.now();
    const secs = () => Math.round((Date.now() - start) / 1000);
    const fmt = (x) =>
      x < 1 ? "a moment" : x < 60 ? x + "s" : Math.floor(x / 60) + "m " + (x % 60) + "s";
    const timer = setInterval(() => {
      s.meta.textContent = fmt(secs());
    }, 1000);

    function append(text) {
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      const sp = document.createElement("span");
      sp.textContent = text;
      box.appendChild(sp);
      if (stick) box.scrollTop = box.scrollHeight;
    }

    function done(label) {
      clearInterval(timer);
      s.el.dataset.state = "done";
      s.label.textContent = label || "Thought for " + fmt(secs());
      s.meta.textContent = "";
    }

    return { el: s.el, append, done, setOpen: s.setOpen, toggle: s.toggle };
  }

  return { webSearch, fetchPage, thinking };
})();
