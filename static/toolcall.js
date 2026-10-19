/*
  ErisToolCall.thinking(parentElement, { open: false })

  const th = ErisToolCall.thinking(messageEl);
  th.append('reasoning text chunk...');   // per streaming chunk
  th.done();                              // label jadi "Thought for 6s"

  ---

  ErisToolCall.webSearch(parentElement, { open: false, onRetry })

  const tc = ErisToolCall.webSearch(messageEl);
  tc.addQuery('react 19 features');
  tc.addResults([{ title: '...', url: 'https://...' }]);
  tc.done();
  tc.fail('Network error');               // tombol Retry muncul hanya bila onRetry diisi
  tc.toggle() / tc.setOpen(true|false)
*/
const ErisToolCall = (() => {
  let uid = 0;
  const I = {
    globe: '<svg class="tc-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></svg>',
    alert: '<svg class="tc-ico-err" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 8v5M12 16.5v.01" /></svg>',
    search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>',
    chev: '<svg class="tc-chev" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" /></svg>',
    spark: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" /></svg>',
    ext: '<svg class="tc-ext" viewBox="0 0 24 24"><path d="M7 17L17 7M8 7h9v9" /></svg>',
  };
  const domainOf = (u) => {
    try {
      return new URL(u).hostname.replace(/^www\./, "");
    } catch {
      return typeof u === "string" ? u : "";
    }
  };
  const safeUrl = (u) => (/^https?:\/\//i.test(u) ? u : "#");
  const plural = (n) => (n === 1 ? "1 source" : n + " sources");

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

  function webSearch(parent, opts = {}) {
    const s = shell(parent, I.globe, "Searching the web", opts);
    let list = null;
    let total = 0;

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
        const row = document.createElement("div");
        row.className = "tc-item";
        row.setAttribute("role", "listitem");
        row.innerHTML =
          '<a class="tc-link" target="_blank" rel="noopener noreferrer"><span class="tc-fav"></span><span class="tc-title"></span><span class="tc-domain"></span>' +
          I.ext +
          "</a>";
        row.querySelector("a").href = safeUrl(url);
        row.querySelector(".tc-fav").textContent = domain.charAt(0) || "?";
        row.querySelector(".tc-title").textContent = it.title || domain;
        row.querySelector(".tc-domain").textContent = domain;
        list.appendChild(row);
        total++;
      }
    }

    function done() {
      s.el.dataset.state = "done";
      s.label.textContent = "Searched the web";
      s.meta.textContent = total ? plural(total) : "No results";
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
      e.innerHTML = '<div class="tc-error"></div>';
      e.querySelector(".tc-error").textContent =
        message || "Something went wrong while searching.";
      if (opts.onRetry) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "tc-retry";
        b.textContent = "Retry";
        b.onclick = () => opts.onRetry(api);
        e.appendChild(b);
      }
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    const api = { el: s.el, addQuery, addResults, done, fail, setOpen: s.setOpen, toggle: s.toggle };
    return api;
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

  return { webSearch, thinking };
})();
