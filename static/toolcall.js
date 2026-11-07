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

  ---

  ErisToolCall.weather(parentElement) / ErisToolCall.wayback(parentElement)
  / ErisToolCall.crypto(parentElement)

  const wc = ErisToolCall.weather(messageEl);
  wc.done(card);        // the payload the tool computed, drawn in one pass
  wc.fail('the place name was not found');
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
    sun: '<svg class="tc-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M19 5l-1.4 1.4M6.4 17.6L5 19" /></svg>',
    archive: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M3 6h18v3H3z" /><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9" /><path d="M10 13h4" /></svg>',
    coin: '<svg class="tc-ico" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M7.5 14.5l3-3 2 2 4-4.5" /><path d="M16.5 9H14M16.5 9v2.5" /></svg>',
    // A price axis with a line over it: the card is a reading of a listing, not
    // a coin, so it does not borrow the coin's glyph.
    stock: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M4 20V4M4 20h16" /><path d="M7 15l4-4.5 3 2.5 5-6" /></svg>',
    rank: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M9 6h11M9 12h11M9 18h11" /><path d="M4 6h.01M4 12h.01M4 18h.01" /></svg>',
    // A checklist: the run's own steps, ticked as it goes.
    plan: '<svg class="tc-ico" viewBox="0 0 24 24"><path d="M4 6l2 2 3-3M12 7h8M4 12l2 2 3-3M12 13h8M4 18l2 2 3-3M12 19h8" /></svg>',
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
  // its colour across reloads: a remote favicon service would hand every
  // result domain to a third party, which the self-hosted search stack exists
  // to avoid. The real icon comes from `/api/favicon`, which asks the site
  // itself; the letter stays underneath and shows again if the site has none.
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
      '</span>' + I.chev + '<span class="tc-meta"></span>' +
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
    if (domain) {
      const img = document.createElement("img");
      img.alt = "";
      img.decoding = "async";
      // The letter is what the chip is; the icon covers it once it arrives and
      // removes itself if the site has none, so there is never an empty box.
      img.onload = () => fav.classList.add("has-icon");
      img.onerror = () => img.remove();
      img.src = "/api/favicon?domain=" + encodeURIComponent(domain);
      fav.appendChild(img);
    }
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

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not read the page";
      // The reason is the tool's own short phrase ("the server answered 403"),
      // so it sits beside the label like the size does on a success, and the
      // panel states it as a sentence for the reader who opened the card.
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The page could not be read: " + reason + "."
        : "The page could not be read.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    return { el: s.el, setUrl, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  // Weather and wayback draw in one pass, from figures the tool computed; there
  // is no slow page to watch, so the payload arrives on the closing frame. Their
  // hero and link each sit in one block, so the panel's own gap separates them
  // from what follows instead of splitting them apart.
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  // Parsed as UTC: a forecast day is a local calendar date the service already
  // resolved, and reading it in the browser's zone can shift it by one.
  const dayLabel = (iso) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
    if (!m) return String(iso || "");
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return `${DAYS[d.getUTCDay()]} ${+m[3]} ${MONTHS[+m[2] - 1]}`;
  };
  const deg = (v) => (typeof v === "number" ? v.toFixed(1) + "°" : "–");
  const degRound = (v) => (typeof v === "number" ? Math.round(v) + "°" : "–");
  const unit = (v, u, round) =>
    typeof v === "number" ? (round ? Math.round(v) : v.toFixed(1)) + u : "–";

  function fact(label, value) {
    const w = document.createElement("div");
    const t = document.createElement("dt");
    t.textContent = label;
    const d = document.createElement("dd");
    d.textContent = value;
    w.append(t, d);
    return w;
  }

  // The condition the card names in words, as the shape its sky is drawn with.
  // Severe matches come first: "thunderstorm with hail" is a storm before it is
  // hail, and "freezing rain" is rain. An unrecognised label keeps the neutral
  // cloud, so a code the WMO table does not list never turns into sunshine.
  const skyKind = (text) => {
    const t = String(text || "").toLowerCase();
    if (/thunder|storm/.test(t)) return "storm";
    if (/snow|sleet|hail|grains/.test(t)) return "snow";
    if (/rain|drizzle|shower/.test(t)) return "rain";
    if (/fog|mist|haze/.test(t)) return "fog";
    if (/overcast/.test(t)) return "cloud";
    if (/cloud/.test(t)) return "partly";
    if (/clear|sun/.test(t)) return "clear";
    return "cloud";
  };

  const RAYS =
    '<path d="M12 2.4v2.6M12 19v2.6M2.4 12h2.6M19 12h2.6M5.2 5.2l1.8 1.8M17 17l1.8 1.8M18.8 5.2L17 7M7 17l-1.8 1.8"/>';
  // The cloud path fills the whole 24-unit box, so it is scaled and centred by
  // hand: the precipitation scenes shrink it to leave a band of clear space
  // under it for what falls out, and a full-size cloud would swallow them.
  const CLOUD =
    "M19.35 10.04A7.49 7.49 0 0 0 12 4C9.11 4 6.6 5.64 5.35 8.04A5.994 5.994 0 0 0 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z";
  const cloudAt = (scale, top) => {
    const x = (24 - 24 * scale) / 2;
    const y = top - 4 * scale;
    return `<g transform="translate(${x.toFixed(2)} ${y.toFixed(2)}) scale(${scale})"><path class="wx-cloud" d="${CLOUD}"/></g>`;
  };
  const drop = (x, y, i) =>
    `<path class="wx-drop wx-drop${i}" d="M${x} ${y}v3.4"/>`;
  const flake = (y, x, i) =>
    `<circle class="wx-flake wx-flake${i}" cx="${x}" cy="${y}" r="1.15"/>`;

  // One scene per kind. Every part that moves is named here and animated in
  // the stylesheet, so the drawing and its motion stay in separate files.
  const SCENE = {
    clear: `<circle class="wx-sun" cx="12" cy="12" r="4.2"/><g class="wx-rays">${RAYS}</g>`,
    partly: `<circle class="wx-sun" cx="18.4" cy="5.6" r="2.5"/>${cloudAt(0.9, 5.4)}`,
    cloud: cloudAt(1, 1.6),
    rain: `${cloudAt(0.74, 0.6)}${drop(7, 14.5, 1)}${drop(10.5, 16, 2)}${drop(14, 13.8, 3)}${drop(17.5, 15.5, 4)}`,
    snow: `${cloudAt(0.74, 0.6)}${flake(15.5, 7.5, 1)}${flake(18.6, 12, 2)}${flake(16.4, 16.5, 3)}`,
    storm: `${cloudAt(0.74, 0.6)}<path class="wx-bolt" d="M13 13l-3.4 4.8h3l-1.8 3.6"/>`,
    fog: '<path class="wx-fog wx-fog1" d="M4 7.5h16"/><path class="wx-fog wx-fog2" d="M6.5 12.5h13.5"/><path class="wx-fog wx-fog3" d="M4 17.5h11"/>',
  };

  const skyOf = (kind) =>
    '<svg viewBox="0 0 24 24">' + (SCENE[kind] || SCENE.cloud) + "</svg>";

  // The reading now, then the forecast: two sections of one card, so the panel
  // gap separates them and the current figures stay one block.
  function weather(parent, opts = {}) {
    const s = shell(parent, I.sun, "Checking the weather", opts);

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not check the weather";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The weather could not be checked: " + reason + "."
        : "The weather could not be checked.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    // The reading is the place's own clock, so it says so: a figure with no
    // time on it could be an hour old and the reader would not know.
    const clockOf = (iso) => {
      const m = /T(\d{2}):(\d{2})/.exec(String(iso || ""));
      return m ? m[1] + ":" + m[2] : "";
    };

    function done(card) {
      card = card || {};
      const now = card.now || {};
      s.el.dataset.state = "done";
      s.label.textContent = "Checked the weather";
      s.meta.textContent = card.place || "";

      // The reading now and the forecast are the card's two sections, so the
      // panel's own gap separates them and the reading stays one block.
      const here = document.createElement("div");
      here.className = "tc-group";

      const hero = document.createElement("div");
      hero.className = "tc-wx-hero";
      const kind = skyKind(now.text);
      // Drawn only, never read: the condition already says the same thing in
      // words right beside it, so a second copy would only repeat itself.
      const sky = document.createElement("span");
      sky.className = "tc-wx-sky";
      sky.setAttribute("aria-hidden", "true");
      sky.innerHTML = skyOf(kind);
      const big = document.createElement("span");
      big.className = "tc-wx-temp";
      big.textContent = deg(now.temp);
      const cond = document.createElement("span");
      cond.className = "tc-wx-cond";
      cond.textContent = now.text || "unknown";
      hero.append(sky, big, cond);
      const clock = clockOf(now.time);
      if (clock) {
        const at = document.createElement("span");
        at.className = "tc-wx-at";
        // The time is the place's own, taken from the service's `timezone: auto`
        // reply, so it is labelled rather than left to be read as the reader's.
        at.textContent = clock + " local";
        at.title = "at " + clock + " where the place is";
        hero.append(at);
      }
      here.appendChild(hero);

      const facts = document.createElement("dl");
      facts.className = "tc-wx-facts";
      facts.append(
        fact("Feels like", deg(now.feels)),
        fact("Humidity", unit(now.humidity, "%", true)),
        fact("Wind", unit(now.wind, " km/h")),
        fact("Rain", unit(now.rain, " mm"))
      );
      here.appendChild(facts);
      s.panel.appendChild(here);

      const days = card.forecast || [];
      if (!days.length) return;
      const list = document.createElement("div");
      list.className = "tc-wx-days";
      // A column of bare percentages and ranges says nothing on its own, and a
      // reader should not have to open the title text to learn what "70%" is.
      // The table roles are what tie a label to the figures under it, so the
      // header still does its job when the narrow breakpoint hides it.
      list.setAttribute("role", "table");
      list.setAttribute("aria-label", "Forecast");
      const hd = document.createElement("div");
      hd.className = "tc-wx-hd";
      hd.setAttribute("role", "row");
      for (const [cls, text] of [
        ["tc-wx-date", "Day"],
        ["tc-wx-dcond", "Conditions"],
        ["tc-wx-range", "Low to high"],
        ["tc-wx-chance", "Rain"],
      ]) {
        const cell = document.createElement("span");
        cell.className = cls;
        cell.setAttribute("role", "columnheader");
        cell.textContent = text;
        hd.appendChild(cell);
      }
      list.appendChild(hd);
      for (const d of days) {
        const row = document.createElement("div");
        row.className = "tc-wx-day";
        row.setAttribute("role", "row");
        const date = document.createElement("span");
        date.className = "tc-wx-date";
        date.setAttribute("role", "rowheader");
        date.textContent = dayLabel(d.date);
        const text = document.createElement("span");
        text.className = "tc-wx-dcond";
        text.setAttribute("role", "cell");
        text.textContent = d.text || "";
        const range = document.createElement("span");
        range.className = "tc-wx-range";
        range.setAttribute("role", "cell");
        range.textContent = degRound(d.min) + "–" + degRound(d.max);
        const chance = document.createElement("span");
        chance.className = "tc-wx-chance";
        chance.setAttribute("role", "cell");
        chance.textContent = unit(d.chance, "%", true);
        // The chance is the column worth scanning; the millimetres behind it
        // are what a reader asks for next, so they ride on the title.
        if (typeof d.rain === "number") chance.title = unit(d.rain, " mm") + " of rain";
        row.append(date, text, range, chance);
        list.appendChild(row);
      }
      s.panel.appendChild(list);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  // The Wayback card. A row's label is the original URL and its href is the
  // archived copy of it, so the domain column names the site rather than
  // archive.org on every row.
  function wayback(parent, opts = {}) {
    const s = shell(parent, I.archive, "Looking in the archive", opts);

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not open the archive";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The archive could not be reached: " + reason + "."
        : "The archive could not be reached.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      s.el.dataset.state = "done";
      s.label.textContent = "Read an archived page";
      s.meta.textContent = card.when || "";

      // The snapshot that was read, its caveat if it had one, and how much of it
      // came back: one block, so the panel's own gap only separates it from the
      // list of the snapshots that were not read.
      const first = document.createElement("div");
      first.className = "tc-group";
      if (card.note) {
        const note = document.createElement("div");
        note.className = "tc-wb-note";
        note.textContent = card.note;
        first.appendChild(note);
      }
      if (card.read) {
        first.appendChild(resultRow(linkEl(card.read, card.url, domainOf(card.url), true)));
      }
      if (card.chars) {
        const size = document.createElement("div");
        size.className = "tc-wb-size";
        size.textContent = sizeText(card.chars) + " read";
        first.appendChild(size);
      }
      if (first.childElementCount) s.panel.appendChild(first);
      const older = card.older || [];
      if (!older.length) return;
      const more = document.createElement("div");
      more.className = "tc-group";
      const head = document.createElement("div");
      head.className = "tc-wb-more";
      head.textContent = "Other snapshots";
      more.appendChild(head);
      const list = document.createElement("div");
      list.className = "tc-list";
      list.setAttribute("role", "list");
      for (const o of older) {
        list.appendChild(resultRow(linkEl(o.read, o.when, domainOf(o.url), false)));
      }
      more.appendChild(list);
      s.panel.appendChild(more);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  // The quote, its move, then the figures behind it. The price is the one
  // large number for the same reason the temperature is: it answers the
  // question that made the model call the tool.
  const money = (v) => {
    if (typeof v !== "number") return "–";
    const digits = v >= 1 ? 2 : v >= 0.01 ? 4 : 8;
    return v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  };
  // Volume and market cap. A market cap in a smaller currency runs past a
  // trillion often enough that stopping at B would print 1,217,000.00B where
  // 1,217.00T is the same figure in one fewer digit group.
  const big = (v) => {
    if (typeof v !== "number") return "–";
    if (v >= 1e12) return (v / 1e12).toFixed(2) + "T";
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(2) + "K";
    return v.toFixed(2);
  };
  // The move is drawn with an arrow as well as a sign and a colour, so it
  // survives forced-colors mode, where the tint is dropped.
  const move = (v) => {
    if (typeof v !== "number") return { text: "–", dir: "flat" };
    return { text: (v > 0 ? "▲ " : v < 0 ? "▼ " : "") + Math.abs(v).toFixed(2) + "%",
             dir: v > 0 ? "up" : v < 0 ? "down" : "flat" };
  };
  const stamp = (epoch) => {
    if (!epoch) return "";
    const d = new Date(epoch * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + " · " +
      d.toLocaleDateString([], { day: "numeric", month: "short" });
  };

  function crypto(parent, opts = {}) {
    const s = shell(parent, I.coin, "Checking the price", opts);

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not check the price";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The price could not be checked: " + reason + "."
        : "The price could not be checked.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      const m = move(card.change);
      s.el.dataset.state = "done";
      s.label.textContent = "Checked the price";
      s.meta.textContent = card.symbol || "";

      const here = document.createElement("div");
      here.className = "tc-group";

      const hero = document.createElement("div");
      hero.className = "tc-cr-hero";
      // The figure and its currency are one fact, so they sit as one unit with
      // their own tight gap; the move is the separate fact beside them.
      const quote = document.createElement("span");
      quote.className = "tc-cr-quote";
      const price = document.createElement("span");
      price.className = "tc-cr-price";
      price.textContent = money(card.price);
      quote.appendChild(price);
      if (card.quote) {
        const unitEl = document.createElement("span");
        unitEl.className = "tc-cr-unit";
        unitEl.textContent = card.quote;
        quote.appendChild(unitEl);
      }
      hero.appendChild(quote);
      const chg = document.createElement("span");
      chg.className = "tc-cr-move";
      chg.dataset.dir = m.dir;
      // The window is part of the fact. A bare percentage next to a price
      // leaves the reader guessing whether it is the day or the hour. A source
      // that reports no change gets no window, since there is nothing to time.
      chg.textContent = typeof card.change === "number" ? m.text + " 24h" : m.text;
      hero.appendChild(chg);
      here.appendChild(hero);

      const name = document.createElement("div");
      name.className = "tc-cr-name";
      name.textContent = card.name && card.name !== card.symbol
        ? card.name + " · " + card.symbol
        : card.symbol || "";
      if (name.textContent) here.appendChild(name);

      const facts = document.createElement("dl");
      facts.className = "tc-cr-facts";
      const volume = fact("24h volume", big(card.volume));
      // Volume is not one number across sources: an exchange counts its own
      // pair, an aggregator counts every market. The scope stays on the row
      // so two readings can be compared without a hidden 25x swing.
      if (card.volume_scope) {
        const scope = document.createElement("span");
        scope.className = "tc-cr-scope";
        scope.textContent = card.volume_scope;
        volume.querySelector("dd").appendChild(scope);
      }
      facts.append(
        fact("24h high", money(card.high)),
        fact("24h low", money(card.low)),
        volume
      );
      if (typeof card.market_cap === "number") {
        facts.append(fact("Market cap", big(card.market_cap)));
      }
      here.appendChild(facts);
      s.panel.appendChild(here);

      // Where the figure came from and when it was read. A price with no time
      // on it is a price the reader cannot judge, and the source differs by
      // which one answered, so it is stated rather than assumed.
      const foot = document.createElement("div");
      foot.className = "tc-cr-foot";
      const at = stamp(card.at);
      if (at) {
        const when = document.createElement("span");
        when.textContent = at;
        foot.appendChild(when);
      }
      if (card.url) {
        foot.appendChild(linkEl(card.url, card.source, domainOf(card.url), false));
      } else if (card.source) {
        const src = document.createElement("span");
        src.textContent = card.source;
        foot.appendChild(src);
      }
      s.panel.appendChild(foot);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
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

  // The chart, drawn from the numbers the tool computed. Nothing here recomputes
  // an average: the polyline points are the server's own values.
  //
  // The plot is an SVG with a 0-1000 x 0-100 viewBox stretched over the card by
  // `preserveAspectRatio="none"`, so a bar's position is a percentage of the box
  // and no resize handler is needed. Anything that has to stay crisp at any
  // width, such as axis figures, gridlines and the last-price tag, is HTML positioned
  // over the plot instead of text inside the viewBox, which would shrink with
  // it until it is unreadable on a phone.
  const SVG = "http://www.w3.org/2000/svg";

  const shortDate = (epoch) =>
    new Date(epoch * 1000).toLocaleDateString([], { day: "numeric", month: "short" });

  // What one bar covers, in the card's own words. A weekly or monthly equity
  // chart says so; anything without an interval is crypto's daily one.
  const barWord = (interval) =>
    interval === "1wk" ? "weekly bars" : interval === "1mo" ? "monthly bars" : "daily bars";

  // Axis steps on 1/2/2.5/5, so the figures read as round numbers rather than as
  // whatever the range happened to divide into.
  function axisTicks(lo, hi, count) {
    const raw = (hi - lo) / count;
    if (!(raw > 0)) return [lo];
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step / 1000; v += step) out.push(v);
    return out;
  }

  function marketChart(card) {
    const bars = Array.isArray(card.candles) ? card.candles : [];
    const root = document.createElement("div");
    root.className = "tc-cc";
    if (!bars.length) {
      const empty = document.createElement("p");
      empty.className = "tc-empty";
      empty.textContent = "No candles came back to draw.";
      root.appendChild(empty);
      return root;
    }

    const n = bars.length;
    let lo = Infinity;
    let hi = -Infinity;
    for (const b of bars) {
      if (b[3] < lo) lo = b[3];
      if (b[2] > hi) hi = b[2];
    }
    // A flat series would divide by zero; give it a band to sit in.
    const span = hi - lo || hi * 0.01 || 1;
    lo -= span * 0.04;
    hi += span * 0.04;
    const y = (v) => 100 - ((v - lo) / (hi - lo)) * 100;
    const x = (i) => 1000 * ((i + 0.5) / n);
    const slot = 1000 / n;
    const bodyW = Math.max(0.7, Math.min(slot * 0.68, 14));

    const head = document.createElement("div");
    head.className = "tc-cc-head";
    const legend = document.createElement("div");
    legend.className = "tc-chart-legend";
    const read = document.createElement("div");
    read.className = "tc-cc-read";
    read.setAttribute("aria-live", "polite");
    head.append(legend, read);

    const body = document.createElement("div");
    body.className = "tc-cc-body";
    const plot = document.createElement("div");
    plot.className = "tc-cc-plot";
    plot.tabIndex = 0;
    plot.setAttribute("role", "img");
    plot.setAttribute(
      "aria-label",
      (card.symbol || "") + " " + barWord(card.interval) + ", " + n + " bars, from " +
      money(card.chart_low) + " to " + money(card.chart_high) +
      ". Use the arrow keys to step through the bars."
    );

    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "tc-cc-svg");
    svg.setAttribute("viewBox", "0 0 1000 100");
    svg.setAttribute("preserveAspectRatio", "none");

    // Bars as filled rects rather than strokes: a stroke would inherit the
    // stretch of the viewBox and come out uneven across the chart.
    const upBody = [];
    const downBody = [];
    const upWick = [];
    const downWick = [];
    bars.forEach((b, i) => {
      const up = b[4] >= b[1];
      const cx = x(i);
      const top = y(Math.max(b[1], b[4]));
      const bottom = y(Math.min(b[1], b[4]));
      const half = bodyW / 2;
      const wick = slot * 0.12;
      (up ? upWick : downWick).push(
        `<rect x="${cx - wick / 2}" y="${y(b[2])}" width="${wick}" ` +
        `height="${Math.max(0.4, y(b[3]) - y(b[2]))}"/>`
      );
      (up ? upBody : downBody).push(
        `<rect x="${cx - half}" y="${top}" width="${bodyW}" ` +
        `height="${Math.max(0.6, bottom - top)}"/>`
      );
    });
    const group = (inner, cls, kind) => {
      if (!inner.length) return null;
      const g = document.createElementNS(SVG, "g");
      g.setAttribute("class", cls);
      g.setAttribute("data-part", kind);
      g.innerHTML = inner.join("");
      return g;
    };
    // An up-only series has no down bars, so a group can come back empty and is
    // skipped rather than appended as an empty node.
    for (const g of [
      group(upWick, "tc-cc-wick", "up"),
      group(downWick, "tc-cc-wick", "down"),
      group(upBody, "tc-cc-candle", "up"),
      group(downBody, "tc-cc-candle", "down"),
    ]) {
      if (g) svg.appendChild(g);
    }

    const ma = (values, cls) => {
      const pts = [];
      (values || []).forEach((v, i) => {
        if (typeof v === "number" && i < n) pts.push(x(i) + "," + y(v));
      });
      if (pts.length < 2) return;
      const line = document.createElementNS(SVG, "polyline");
      line.setAttribute("points", pts.join(" "));
      line.setAttribute("class", cls);
      line.setAttribute("vector-effect", "non-scaling-stroke");
      svg.appendChild(line);
    };
    ma(card.ma20_series, "tc-cc-ma tc-cc-ma20");
    ma(card.ma50_series, "tc-cc-ma tc-cc-ma50");
    // The last close, as a dashed rule across the plot. It is what the eye looks
    // for first, and it is also stated as a figure on the price column.
    const lastY = y(bars[n - 1][4]);
    const lastLine = document.createElementNS(SVG, "line");
    lastLine.setAttribute("x1", "0");
    lastLine.setAttribute("x2", "1000");
    lastLine.setAttribute("y1", lastY);
    lastLine.setAttribute("y2", lastY);
    lastLine.setAttribute("class", "tc-cc-lastline");
    lastLine.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(lastLine);
    plot.appendChild(svg);

    // Horizontal gridlines sit at the price figures, so a line always has a
    // number beside it.
    const priceCol = document.createElement("div");
    priceCol.className = "tc-cc-price";
    for (const v of axisTicks(lo, hi, 4)) {
      const pct = y(v);
      // A tick near the edge would be clipped by the card, and one sitting on
      // the last close would be covered by the tag below, so both are dropped.
      if (pct < 2 || pct > 98 || Math.abs(pct - lastY) < 5) continue;
      const line = document.createElement("div");
      line.className = "tc-cc-gridline";
      line.style.top = pct + "%";
      plot.prepend(line);
      const tag = document.createElement("span");
      tag.className = "tc-cc-pricetag";
      tag.style.top = pct + "%";
      tag.textContent = money(v);
      priceCol.appendChild(tag);
    }
    const lastTag = document.createElement("span");
    lastTag.className = "tc-cc-lasttag";
    lastTag.style.top = lastY + "%";
    lastTag.textContent = money(bars[n - 1][4]);
    priceCol.appendChild(lastTag);

    // Time labels are placed by percentage, which the SVG cannot do for text at
    // a size the page controls. The first and last are inset so a label does not
    // hang off either edge.
    const timeRow = document.createElement("div");
    timeRow.className = "tc-cc-time";
    const tickIndex = (k) => Math.round(n * 0.06 + (n * 0.88 * k) / 3);
    for (let k = 0; k < 4; k++) {
      const i = Math.min(n - 1, Math.max(0, tickIndex(k)));
      const pct = ((i + 0.5) / n) * 100;
      const vline = document.createElement("div");
      vline.className = "tc-cc-vline";
      vline.style.left = pct + "%";
      plot.prepend(vline);
      const tag = document.createElement("span");
      tag.className = "tc-cc-timetag";
      tag.style.left = pct + "%";
      tag.textContent = shortDate(bars[i][0]);
      timeRow.appendChild(tag);
    }

    // Volume as its own pane under the plot, on the same x, the way a trading
    // chart splits it. It answers "did this move have size behind it", which the
    // candle shape alone cannot.
    const volSvg = document.createElementNS(SVG, "svg");
    volSvg.setAttribute("class", "tc-cc-volsvg");
    volSvg.setAttribute("viewBox", "0 0 1000 100");
    volSvg.setAttribute("preserveAspectRatio", "none");
    const volMax = Math.max(...bars.map((b) => b[5] || 0)) || 1;
    const upVol = [];
    const downVol = [];
    bars.forEach((b, i) => {
      const h = Math.max(0.6, ((b[5] || 0) / volMax) * 100);
      (b[4] >= b[1] ? upVol : downVol).push(
        `<rect x="${x(i) - bodyW / 2}" y="${100 - h}" width="${bodyW}" height="${h}"/>`
      );
    });
    for (const g of [group(upVol, "tc-cc-volbar", "up"),
                     group(downVol, "tc-cc-volbar", "down")]) {
      if (g) volSvg.appendChild(g);
    }

    const vol = document.createElement("div");
    vol.className = "tc-cc-vol";
    vol.appendChild(volSvg);

    // The crosshair. Its readings are also on the row above and in the facts
    // under the chart, so it never holds a number the reader cannot get another
    // way, which is what lets it be an enhancement rather than the only path.
    const cursor = document.createElement("div");
    cursor.className = "tc-cc-cursor";
    cursor.hidden = true;
    plot.appendChild(cursor);
    const dot = document.createElement("span");
    dot.className = "tc-cc-dot";
    dot.hidden = true;
    priceCol.appendChild(dot);

    let at = -1;
    const show = (i) => {
      if (i == null || i < 0 || i >= n) {
        at = -1;
        cursor.hidden = true;
        dot.hidden = true;
        read.textContent = "";
        return;
      }
      at = i;
      const b = bars[i];
      cursor.hidden = false;
      cursor.style.left = ((i + 0.5) / n) * 100 + "%";
      dot.hidden = false;
      dot.style.top = y(b[4]) + "%";
      const dir = b[4] >= b[1] ? "up" : "down";
      read.dataset.dir = dir;
      read.textContent = shortDate(b[0]) + "  O " + money(b[1]) + "  H " + money(b[2]) +
        "  L " + money(b[3]) + "  C " + money(b[4]) + "  Vol " + big(b[5]);
    };
    const fromPointer = (e) => {
      const r = plot.getBoundingClientRect();
      if (!r.width) return;
      show(Math.max(0, Math.min(n - 1, Math.round(((e.clientX - r.left) / r.width) * n - 0.5))));
    };
    plot.addEventListener("pointermove", fromPointer);
    plot.addEventListener("pointerleave", () => show(-1));
    plot.addEventListener("keydown", (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -n, End: n }[e.key];
      if (step === undefined) {
        if (e.key === "Escape") show(-1);
        return;
      }
      e.preventDefault();
      const next = at < 0 ? (step < 0 ? n - 1 : 0) : Math.max(0, Math.min(n - 1, at + step));
      show(next);
    });
    plot.addEventListener("blur", () => show(-1));

    const key = (cls, text) => {
      const item = document.createElement("span");
      item.className = "tc-chart-key " + cls;
      const sw = document.createElement("span");
      sw.className = "tc-chart-swatch";
      item.append(sw, document.createTextNode(text));
      legend.appendChild(item);
    };
    key("is-ma20", "MA20");
    key("is-ma50", "MA50");

    body.append(plot, priceCol, vol, timeRow);
    root.append(head, body);
    return root;
  }

  // The verdict first, because that is the question that was asked. The colour
  // repeats the word rather than standing in for it.
  //
  // The same builder draws the equity chart: the figures come from the same
  // arithmetic on the same shape of candles, so the only difference is the
  // wording and the glyph, which `opts` carries.
  function cryptoTrend(parent, opts = {}) {
    const s = shell(parent, opts.ico || I.coin, opts.start || "Reading the market", opts);
    const doneLabel = opts.done || "Read the market";
    // A window of 30 bars is 30 days only on a daily series; a weekly equity
    // chart would otherwise label a 30-week move "30d".
    const window30 = opts.words ? "30 bars" : "30d";
    const rangeWord = opts.words ? "30-bar" : "30-day";

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = opts.fail || "Could not read the market";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? (opts.error || "The market") + " could not be read: " + reason + "."
        : (opts.error || "The market") + " could not be read.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      s.el.dataset.state = "done";
      s.label.textContent = doneLabel;
      s.meta.textContent = card.symbol || "";

      const head = document.createElement("div");
      head.className = "tc-td-head";
      const verdict = document.createElement("span");
      verdict.className = "tc-td-verdict";
      verdict.dataset.trend = card.verdict || "sideways";
      verdict.textContent = card.verdict || "sideways";
      head.appendChild(verdict);
      if (card.condition && card.condition !== "neutral") {
        const cond = document.createElement("span");
        cond.className = "tc-td-cond";
        cond.dataset.cond = card.condition;
        cond.textContent = card.condition + " · RSI " + card.rsi;
        head.appendChild(cond);
      }
      s.panel.appendChild(head);

      const hero = document.createElement("div");
      hero.className = "tc-cr-hero";
      const price = document.createElement("span");
      price.className = "tc-cr-price";
      price.textContent = money(card.price) + (card.quote ? " " + card.quote : "");
      hero.appendChild(price);
      const chg = move(card.change_30);
      const move30 = document.createElement("span");
      move30.className = "tc-cr-move";
      move30.dataset.dir = chg.dir;
      move30.textContent = chg.text + " " + window30;
      hero.appendChild(move30);
      s.panel.appendChild(hero);

      // The listing's own name and exchange, when the tool sent them. A ticker
      // alone means nothing to a reader who does not trade that market.
      if (opts.words && card.name && card.name !== card.symbol) {
        const sub = document.createElement("div");
        sub.className = "tc-cr-name";
        sub.textContent = card.name + (card.exchange ? " · " + card.exchange : "");
        s.panel.appendChild(sub);
      }

      s.panel.appendChild(marketChart(card));

      // Where the price sits in the 30-day range. The marker carries the same
      // number the text says, so the bar is a second reading, not the only one.
      if (typeof card.range_pos === "number") {
        const row = document.createElement("div");
        row.className = "tc-td-range";
        const bar = document.createElement("div");
        bar.className = "tc-td-rangebar";
        bar.setAttribute("role", "img");
        bar.setAttribute(
          "aria-label",
          "price is at " + Math.round(card.range_pos) + " percent of its " + rangeWord + " range"
        );
        const dot = document.createElement("span");
        dot.style.left = Math.max(0, Math.min(100, card.range_pos)) + "%";
        bar.appendChild(dot);
        const lo = document.createElement("span");
        lo.textContent = money(card.range_low);
        const hi = document.createElement("span");
        hi.textContent = money(card.range_high);
        row.append(lo, bar, hi);
        s.panel.appendChild(row);
      }

      const facts = document.createElement("dl");
      facts.className = "tc-cr-facts";
      const maRow = (label, value, delta) =>
        fact(label, money(value) + (typeof delta === "number"
          ? " (" + (delta >= 0 ? "+" : "") + delta.toFixed(2) + "%)"
          : ""));
      facts.append(
        maRow("MA20", card.ma20, card.above20),
        maRow("MA50", card.ma50, card.above50)
      );
      if (typeof card.ma200 === "number") {
        facts.append(maRow("MA200", card.ma200, card.above200));
      }
      facts.append(
        fact(opts.words ? "7 bars" : "7d", move(card.change_7).text),
        fact(opts.words ? "90 bars" : "90d", move(card.change_90).text),
        fact("Volatility", typeof card.atr_pct === "number"
          ? card.atr_pct.toFixed(2) + "% ATR"
          : "–")
      );
      s.panel.appendChild(facts);

      const foot = document.createElement("div");
      foot.className = "tc-cr-foot";
      const stampEl = stamp(card.at);
      if (stampEl) {
        const when = document.createElement("span");
        when.textContent = stampEl + " · " + (card.bar_count || 0) + " " + barWord(card.interval);
        foot.appendChild(when);
      }
      if (card.url) {
        foot.appendChild(linkEl(card.url, card.source, domainOf(card.url), false));
      }
      s.panel.appendChild(foot);

      // The card is a reading of the past. Said here rather than in the model's
      // prose, which can be talked out of it.
      const note = document.createElement("p");
      note.className = "tc-td-note";
      note.textContent = "Measured from past closes. Not a forecast.";
      s.panel.appendChild(note);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  // Equity quotes: one listing gets the same hero a coin does, since the
  // question is the same one. Several listings take a row each instead, because
  // a stack of heroes is a stack of headlines and nothing to compare.
  function equity(parent, opts = {}) {
    const s = shell(parent, I.stock, "Checking the price", opts);

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not check the price";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The quotes could not be read: " + reason + "."
        : "The quotes could not be read.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      const rows = Array.isArray(card.quotes) ? card.quotes : [];
      s.el.dataset.state = "done";
      s.label.textContent = rows.length > 1 ? "Checked the prices" : "Checked the price";
      s.meta.textContent = rows.length > 1 ? rows.length + " listings" : (rows[0]?.symbol || "");

      if (rows.length === 1) singleQuote(s.panel, rows[0]);
      else listingList(s.panel, rows, { label: "Listings" });

      if ((card.missing || []).length) {
        const note = document.createElement("p");
        note.className = "tc-td-note";
        note.textContent = "No listing found for " + card.missing.join(", ") + ".";
        s.panel.appendChild(note);
      }

      const foot = document.createElement("div");
      foot.className = "tc-cr-foot";
      // Prices on an exchange are delayed, and a card that shows a price
      // without saying so invites a reader to treat it as live.
      const when = stamp(card.at);
      if (when) {
        const t = document.createElement("span");
        t.textContent = when;
        foot.appendChild(t);
      }
      if (card.url) {
        foot.appendChild(linkEl(card.url, card.source, domainOf(card.url), false));
      }
      if (foot.childElementCount) s.panel.appendChild(foot);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  function singleQuote(panel, q) {
    const m = move(q.change);
    const hero = document.createElement("div");
    hero.className = "tc-cr-hero";
    const quote = document.createElement("span");
    quote.className = "tc-cr-quote";
    const price = document.createElement("span");
    price.className = "tc-cr-price";
    price.textContent = money(q.price);
    quote.appendChild(price);
    if (q.quote) {
      const u = document.createElement("span");
      u.className = "tc-cr-unit";
      u.textContent = q.quote;
      quote.appendChild(u);
    }
    hero.appendChild(quote);
    const chg = document.createElement("span");
    chg.className = "tc-cr-move";
    chg.dataset.dir = m.dir;
    chg.textContent = typeof q.change === "number" ? m.text + " today" : m.text;
    hero.appendChild(chg);
    panel.appendChild(hero);

    const sub = document.createElement("div");
    sub.className = "tc-cr-name";
    sub.textContent = [q.name, q.symbol, q.exchange].filter(Boolean).join(" · ");
    if (sub.textContent) panel.appendChild(sub);

    const facts = document.createElement("dl");
    facts.className = "tc-cr-facts";
    const add = (label, value) => {
      if (value != null && value !== "–") facts.appendChild(fact(label, value));
    };
    add("Previous close", typeof q.prev === "number" ? money(q.prev) : null);
    add("Day range", typeof q.low === "number" && typeof q.high === "number"
      ? money(q.low) + " to " + money(q.high) : null);
    add("52-week range", typeof q.week52_low === "number" && typeof q.week52_high === "number"
      ? money(q.week52_low) + " to " + money(q.week52_high) : null);
    add("Volume", typeof q.volume === "number" ? big(q.volume) : null);
    add("Market cap", typeof q.market_cap === "number" ? big(q.market_cap) : null);
    add("P/E", typeof q.pe === "number" ? q.pe.toFixed(2) : null);
    add("Dividend yield", typeof q.dividend === "number" ? q.dividend.toFixed(2) + "%" : null);
    if (q.market_state) add("Session", q.market_state);
    if (facts.childElementCount) panel.appendChild(facts);
  }

  // One row per listing, shared by the multi-quote card and the screener so a
  // price reads the same in both. The grid is fixed so the columns line up down
  // the list whatever the name lengths are.
  function listingList(panel, rows, opts = {}) {
    const list = document.createElement("div");
    list.className = "tc-eq-list";
    // A header over no rows is a table that says nothing. The tool refuses an
    // empty screen before it gets this far, but a replay of an old turn may
    // carry one, and a bare header would read as a broken list.
    if (!rows.length) {
      const empty = document.createElement("p");
      empty.className = "tc-empty";
      empty.textContent = opts.empty || "Nothing came back to list.";
      panel.appendChild(empty);
      return;
    }
    list.setAttribute("role", "table");
    list.setAttribute("aria-label", opts.label || "Listings");

    const hd = document.createElement("div");
    hd.className = "tc-eq-row is-hd" + (opts.rank ? " is-ranked" : "");
    hd.setAttribute("role", "row");
    for (const [cls, text] of [
      [opts.rank ? "tc-eq-rank" : "", opts.rank ? "#" : ""],
      ["tc-eq-sym", "Symbol"],
      ["tc-eq-name", "Name"],
      ["tc-eq-price", "Price"],
      ["tc-eq-move", "Change"],
    ]) {
      if (!text) continue;
      const cell = document.createElement("span");
      if (cls) cell.className = cls;
      cell.setAttribute("role", "columnheader");
      cell.textContent = text;
      hd.appendChild(cell);
    }
    list.appendChild(hd);

    rows.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "tc-eq-row" + (opts.rank ? " is-ranked" : "");
      row.setAttribute("role", "row");

      const link = document.createElement("a");
      link.className = "tc-eq-link";
      link.href = safeUrl(r.url);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.setAttribute("role", "rowheader");
      link.textContent = r.symbol || "";

      if (opts.rank) {
        const n = document.createElement("span");
        n.className = "tc-eq-rank";
        n.setAttribute("role", "cell");
        n.textContent = String(i + 1);
        row.appendChild(n);
      }
      row.appendChild(link);

      const name = document.createElement("span");
      name.className = "tc-eq-name";
      name.setAttribute("role", "cell");
      name.textContent = r.name || "";
      name.title = r.name || "";
      row.appendChild(name);

      const price = document.createElement("span");
      price.className = "tc-eq-price";
      price.setAttribute("role", "cell");
      price.textContent = money(r.price);
      // Two listings from two exchanges are in two currencies, so the unit
      // rides on each row rather than being assumed from the first.
      if (r.quote) {
        const u = document.createElement("span");
        u.className = "tc-eq-unit";
        u.textContent = " " + r.quote;
        price.appendChild(u);
      }
      row.appendChild(price);

      const m = move(r.change);
      const chg = document.createElement("span");
      chg.className = "tc-eq-move";
      chg.dataset.dir = m.dir;
      chg.setAttribute("role", "cell");
      chg.textContent = m.text;
      row.appendChild(chg);

      list.appendChild(row);
    });
    panel.appendChild(list);
  }

  // The screener: a ranking, so the position in it is part of the reading and
  // the row carries its own number.
  const SORT_LABEL = {
    gainers: "Ranked the gainers",
    losers: "Ranked the losers",
    active: "Ranked by volume",
    largest: "Ranked by size",
  };

  function screen(parent, opts = {}) {
    const s = shell(parent, I.rank, "Ranking the market", opts);

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not rank the market";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The market could not be ranked: " + reason + "."
        : "The market could not be ranked.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      const rows = Array.isArray(card.rows) ? card.rows : [];
      s.el.dataset.state = "done";
      s.label.textContent = SORT_LABEL[card.sort] || "Ranked the market";
      s.meta.textContent = card.market || "";

      listingList(s.panel, rows, {
        rank: true,
        label: (card.market || "Market") + " rankings",
      });

      const foot = document.createElement("div");
      foot.className = "tc-cr-foot";
      const when = stamp(card.at);
      if (when) {
        const t = document.createElement("span");
        t.textContent = when;
        foot.appendChild(t);
      }
      if (card.total) {
        const t = document.createElement("span");
        t.textContent = rows.length + " of " + card.total.toLocaleString("en-US") + " listings";
        foot.appendChild(t);
      }
      if (rows[0]?.url) {
        foot.appendChild(linkEl(rows[0].url, card.source, domainOf(rows[0].url), false));
      }
      if (foot.childElementCount) s.panel.appendChild(foot);

      const note = document.createElement("p");
      note.className = "tc-td-note";
      note.textContent = "A snapshot of Yahoo's ranking, not the whole market.";
      s.panel.appendChild(note);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  // The run's own steps. A card redraws itself each time the model revises the
  // plan, rather than adding a second card: the reader wants the plan now, not
  // a history of it.
  const PLAN_WORD = { done: "done", doing: "in progress", todo: "to do" };

  function plan(parent, opts = {}) {
    // Open by default: a plan folded closed is a card that says only that a plan
    // exists, which is the one thing it should not hide.
    const s = shell(parent, I.plan, "Writing a plan", { open: true, ...opts });
    // The panel caps its height, so a long plan scrolls inside it; a scroll
    // container that cannot take focus is one a keyboard cannot scroll.
    s.panel.tabIndex = 0;
    s.panel.setAttribute("aria-label", "Plan steps");

    function fail(reason) {
      s.el.dataset.state = "error";
      s.label.textContent = "Could not record the plan";
      s.meta.textContent = reason || "";
      const e = document.createElement("div");
      e.className = "tc-error";
      e.textContent = reason
        ? "The plan could not be recorded: " + reason + "."
        : "The plan could not be recorded.";
      s.panel.appendChild(e);
      s.setOpen(true);
    }

    function done(card) {
      card = card || {};
      const steps = Array.isArray(card.steps) ? card.steps : [];
      const finished = steps.filter((st) => st.status === "done").length;
      s.el.dataset.state = "done";
      s.label.textContent = "Plan";
      s.meta.textContent = steps.length ? finished + " of " + steps.length + " done" : "";

      // Drawn again in place, so a revised plan replaces the old one instead of
      // stacking beneath it.
      s.panel.textContent = "";
      if (!steps.length) {
        const e = document.createElement("p");
        e.className = "tc-empty";
        e.textContent = "The plan came back empty.";
        s.panel.appendChild(e);
        return;
      }
      const list = document.createElement("div");
      list.className = "tc-list";
      list.setAttribute("role", "list");
      for (const st of steps) {
        const status = st.status === "done" || st.status === "doing" ? st.status : "todo";
        const row = document.createElement("div");
        row.className = "tc-plan-row";
        row.dataset.status = status;
        row.setAttribute("role", "listitem");
        // The mark differs in shape, not only in colour, and the state is spelled
        // out for a screen reader that cannot see either.
        row.setAttribute("aria-label", st.step + ", " + PLAN_WORD[status]);
        const mark = document.createElement("span");
        mark.className = "tc-plan-mark";
        mark.setAttribute("aria-hidden", "true");
        mark.textContent = status === "done" ? "✓" : status === "doing" ? "▸" : "○";
        const text = document.createElement("span");
        text.className = "tc-plan-text";
        text.textContent = st.step;
        row.append(mark, text);
        list.appendChild(row);
      }
      s.panel.appendChild(list);
    }

    return { el: s.el, done, fail, setOpen: s.setOpen, toggle: s.toggle };
  }

  return {
    webSearch,
    fetchPage,
    thinking,
    weather,
    wayback,
    crypto,
    // Keyed by the tool name, because a replay does `ErisToolCall[tool](body)`
    // with whatever the server stored.
    crypto_trend: cryptoTrend,
    equity,
    equity_trend: (parent, opts = {}) =>
      cryptoTrend(parent, {
        ...opts,
        ico: I.stock,
        start: "Reading the chart",
        done: "Read the chart",
        fail: "Could not read the chart",
        error: "The chart",
        words: true,
      }),
    screen,
    plan,
  };
})();
