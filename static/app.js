const els = {
  messages: document.getElementById("messages"),
  form: document.getElementById("form"),
  input: document.getElementById("input"),
  send: document.getElementById("send"),
  menuBtn: document.getElementById("menuBtn"),
  chatTitle: document.getElementById("chatTitle"),
  attach: document.getElementById("attach"),
  fileInput: document.getElementById("fileInput"),
  box: document.getElementById("box"),
  count: document.getElementById("count"),
  quotes: document.getElementById("quotes"),
};

marked.setOptions({ breaks: true, gfm: true });

// Sticky "jump to latest" button. While streaming we only auto-scroll when the
// reader is already at the bottom, so scrolling up to re-read older turns is
// not yanked back on every delta.
const scrollBtn = document.createElement("button");
scrollBtn.type = "button";
scrollBtn.className = "jump-btn";
scrollBtn.setAttribute("aria-label", "Scroll to latest");
scrollBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>';
scrollBtn.hidden = true;
scrollBtn.addEventListener("click", () => {
  els.messages.scrollTop = els.messages.scrollHeight;
});
els.messages.append(scrollBtn);

const nearBottom = () =>
  els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < 80;

function syncScrollBtn() {
  scrollBtn.hidden = nearBottom();
}

function autoScroll() {
  if (nearBottom()) els.messages.scrollTop = els.messages.scrollHeight;
}

els.messages.addEventListener("scroll", syncScrollBtn, { passive: true });

/* Reply to a selection. Selecting text inside a turn offers one action, and the
   result waits above the field as its own chip rather than as lines in the
   textarea: quoted words and typed words are different things, and once they
   share one box there is no telling them apart. The action chip is fixed rather
   than absolute, because the messages pane clips its own overflow and would cut
   off a child reaching past its edge. */
const QUOTE_MAX = 1000;
const QUOTE_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 17l-5-5 5-5"/><path d="M4 12h11a5 5 0 0 1 5 5v3"/></svg>';
const X_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6L6 18M6 6l12 12"/></svg>';

const quoteBtn = document.createElement("button");
quoteBtn.type = "button";
quoteBtn.className = "quote-btn";
quoteBtn.hidden = true;
quoteBtn.setAttribute("aria-label", "Quote the selected text");
quoteBtn.innerHTML = QUOTE_ICON + "Ask";
document.body.append(quoteBtn);

let quotedText = "";
let pendingQuote = "";

function hideQuote() {
  quotedText = "";
  quoteBtn.hidden = true;
}

// The quote chip carries an X rather than relying on the textarea's own undo:
// it is removable on its own, leaving whatever was typed untouched.
function setQuote(text) {
  pendingQuote = text;
  els.quotes.hidden = !text;
  els.quotes.innerHTML = "";
  if (text) els.quotes.append(quoteChip(text));
  refresh();
}

function quoteChip(text) {
  const chip = document.createElement("span");
  chip.className = "quote-chip";
  chip.title = text;
  chip.insertAdjacentHTML("afterbegin", QUOTE_ICON);
  const name = document.createElement("span");
  name.className = "chip-name";
  name.textContent = text.replace(/\s+/g, " ").trim();
  chip.append(name);

  const x = document.createElement("button");
  x.type = "button";
  x.className = "chip-x";
  x.title = "Remove quote";
  x.setAttribute("aria-label", "Remove quoted text");
  x.innerHTML = X_ICON;
  x.addEventListener("click", () => {
    setQuote("");
    els.input.focus();
  });
  chip.append(x);
  return chip;
}

function showQuote() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return hideQuote();
  const range = sel.getRangeAt(0);
  const anchor = range.commonAncestorContainer;
  const el = anchor.nodeType === 1 ? anchor : anchor.parentElement;
  // Only a turn's own prose is quotable: the empty state and the toolbar that
  // sits beside a message are not things anyone replies to.
  if (!el || !el.closest(".msg .body")) return hideQuote();
  const text = sel.toString().trim();
  if (!text) return hideQuote();
  quotedText = text.length > QUOTE_MAX ? text.slice(0, QUOTE_MAX) + "…" : text;

  // Placed after it is shown, because a hidden element has no size to measure
  // against. The chip prefers to sit under the selection and flips above it
  // when the messages pane runs out of room, so it never covers the composer.
  quoteBtn.hidden = false;
  const pane = els.messages.getBoundingClientRect();
  const box = range.getBoundingClientRect();
  const w = quoteBtn.offsetWidth;
  const h = quoteBtn.offsetHeight;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, hi));
  quoteBtn.style.left = clamp(box.left + box.width / 2 - w / 2,
                              pane.left + 8, pane.right - w - 8) + "px";
  let top = box.bottom + 6;
  if (top + h > pane.bottom - 8) top = box.top - h - 6;
  quoteBtn.style.top = clamp(top, pane.top + 8, pane.bottom - h - 8) + "px";
}

quoteBtn.addEventListener("mousedown", (e) => e.preventDefault());
quoteBtn.addEventListener("click", () => {
  setQuote(quotedText);
  hideQuote();
  window.getSelection().removeAllRanges();
  els.input.focus();
});

// The quote rides beside the question, never inside it: they are sent as two
// fields and the server joins them into one message for the model, so a
// reopened chat can still draw the quoted words as something nobody typed.

document.addEventListener("selectionchange", showQuote);
// The chip is fixed to the viewport, so it stays put while the text under it
// moves. Any scroll or resize drops it rather than leaving it pointing at
// something the reader can no longer see.
els.messages.addEventListener("scroll", hideQuote, { passive: true });
window.addEventListener("resize", hideQuote);

const MAX = 4000;

let sessions = [];
let activeId = null;
let controller = null;
let sending = false;
let userName = "";
let userAvatar = "";
let modelAvatar = "";

const current = () => sessions.find((s) => s.id === activeId);

function addRegen(body, s) {
  if (body.querySelector(".regen")) return;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "regen";
  b.textContent = "Regenerate";
  b.title = "Re-answer the last question (replaces this response)";
  b.addEventListener("click", () => {
    if (sending) return;
    const last = s.messages.at(-1);
    const q = s.messages.at(-2);
    if (!last || last.role !== "assistant" || !q || q.role !== "user" || typeof q.content !== "string" || !q.content || (q.images && q.images.length)) return;
    send(q.content, true);
  });
  body.append(b);
}

function showEmpty() {
  resetMessages(
    '<div class="empty" id="empty"><p>Ask anything. Attach files or images. Markdown supported.</p></div>'
  );
}

// innerHTML wipes destroy the sticky scroll button; every message-area reset
// goes through here so it stays attached and its visibility state stays fresh.
function resetMessages(html = "") {
  hideQuote();
  els.messages.innerHTML = html;
  els.messages.append(scrollBtn);
  syncScrollBtn();
}

// Every avatar in the app draws from the same two globals: the photo when the
// account has one, initials otherwise. Background images need the sizing that
// .avatar and .side-avatar declare, so the photo is set here, not in the markup.
function paintAvatar(el) {
  if (!el) return;
  if (userAvatar) {
    el.style.backgroundImage = `url("${userAvatar}")`;
    el.textContent = "";
    return;
  }
  el.style.backgroundImage = "";
  el.textContent = (userName || "ME").slice(0, 2).toUpperCase();
}

// The assistant's chip carries the model picture when the account has one, and
// the "AI" label when it does not. Same rule as the user avatar above.
function paintRole(el) {
  if (!el) return;
  if (modelAvatar) {
    el.style.backgroundImage = `url("${modelAvatar}")`;
    el.textContent = "";
    return;
  }
  el.style.backgroundImage = "";
  el.textContent = "AI";
}

function addMessage(role, text, images, quote) {
  document.getElementById("empty")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const roleEl = document.createElement("div");
  roleEl.className = "role";
  roleEl.textContent = role === "user" ? "You" : "AI";
  const body = document.createElement("div");
  body.className = "body";
  if (images && images.length) {
    const imgs = document.createElement("div");
    imgs.className = "imgs";
    for (const src of images) {
      const im = document.createElement("img");
      im.src = src;
      im.alt = "attachment";
      im.addEventListener("click", () => window.open(src, "_blank"));
      imgs.append(im);
    }
    body.append(imgs);
  }
  if (role === "user") {
    // Two surfaces, not one: the quote is text the reader picked out of an
    // earlier answer and the bubble is what they typed, so the quote keeps its
    // own smaller chip instead of riding inside the question.
    if (quote) {
      const q = document.createElement("div");
      q.className = "quoted";
      q.textContent = quote;
      body.append(q);
    }
    if (text) {
      const t = document.createElement("div");
      t.className = "bubble";
      t.textContent = text;
      body.append(t);
    }
    const av = document.createElement("div");
    av.className = "avatar";
    paintAvatar(av);
    wrap.append(av, body);
  } else {
    body.innerHTML = ErisMarkdown.render(text);
    paintRole(roleEl);
    wrap.append(roleEl, body);
  }
  els.messages.insertBefore(wrap, scrollBtn);
  els.messages.scrollTop = els.messages.scrollHeight;
  syncScrollBtn();
  return body;
}

function refresh() {
  const len = els.input.value.length;
  const has =
    els.input.value.trim().length > 0 || ErisAttach.hasImage() || !!pendingQuote;
  const over = len > MAX;
  els.send.disabled = !sending && (!has || over);
  els.count.textContent = len + " / " + MAX;
  els.count.classList.toggle("show", len > MAX * 0.8);
  els.count.classList.toggle("over", over);
}

function syncInput() {
  els.input.style.height = "auto";
  els.input.style.height = Math.min(els.input.scrollHeight, 200) + "px";
  refresh();
}

/* Sessions. */

function blankChat() {
  activeId = null;
  ErisSidebar.setActive(null);
  els.chatTitle.textContent = "New chat";
  ErisAttach.clear();
  setQuote("");
  resetMessages();
  showEmpty();
  els.input.value = "";
  syncInput();
  els.input.focus();
}

function loadSession(id) {
  const s = sessions.find((x) => x.id === id);
  if (!s) return;
  activeId = id;
  ErisSidebar.setActive(id);
  els.chatTitle.textContent = s.title;
  ErisAttach.clear();
  setQuote("");
  resetMessages();
  const paint = () => {
    if (!s.messages.length) {
      showEmpty();
    } else {
      s.messages.forEach((m) => {
        const thinkRounds = Array.isArray(m.thinking) ? m.thinking : m.thinking ? [m.thinking] : [];
        // A turn that answered between two searches was drawn as several
        // blocks, so the stored rounds are what rebuilds that order; the flat
        // text is only the fallback for a turn saved before that was recorded.
        const answerRows = new Map(
          (Array.isArray(m.answers) ? m.answers : []).map((a) => [a.r ?? 0, a.text])
        );
        const body = addMessage(m.role, answerRows.size ? "" : m.content, m.images, m.quote);
        const web = (m.sources || []).filter((x) => x.kind === "web");
        const groups = (m.web && m.web.length ? m.web : web.length ? [{ query: "", items: web }] : [])
          .map((g) => ({ r: g.r ?? 0, query: g.query, items: g.items }));
        const reads = (m.fetches || []).map((f) => ({
          r: f.r ?? 0, url: f.url, chars: f.chars, ok: f.ok, reason: f.reason || "",
        }));
        const rows = new Map();
        for (const g of groups) {
          if (!rows.has(g.r)) rows.set(g.r, []);
          rows.get(g.r).push(g);
        }
        const readRows = new Map();
        for (const f of reads) {
          if (!readRows.has(f.r)) readRows.set(f.r, []);
          readRows.get(f.r).push(f);
        }
        let anchor = null;
        const put = (el) => {
          if (anchor) anchor.after(el);
          else body.prepend(el);
          anchor = el;
        };
        // A round's searches and its reads are replayed in the same order the
        // round drew them: the search that prompted a read comes first, and the
        // answer the round produced comes after both.
        const roundNums = new Set([...rows.keys(), ...readRows.keys(), ...answerRows.keys()]);
        const rounds = Math.max(thinkRounds.length, ...[...roundNums].map((r) => r + 1), 0);
        for (let i = 0; i < rounds; i++) {
          if (i < thinkRounds.length) {
            const th = ErisToolCall.thinking(body);
            put(th.el);
            th.append(thinkRounds[i]);
            th.done("Thought");
          }
          for (const g of rows.get(i) || []) {
            const tc = ErisToolCall.webSearch(body);
            put(tc.el);
            if (g.query) tc.addQuery(g.query);
            tc.addResults(g.items);
            tc.done();
          }
          for (const f of readRows.get(i) || []) {
            const fc = ErisToolCall.fetchPage(body);
            put(fc.el);
            fc.setUrl(f.url);
            if (f.ok === false) fc.fail(f.reason);
            else fc.done(f.chars);
          }
          const text = answerRows.get(i);
          if (text) {
            const a = document.createElement("div");
            a.className = "ans";
            a.innerHTML = ErisMarkdown.render(text);
            put(a);
          }
        }
        if (m.role === "assistant") {
          ErisMarkdown.highlight(body);
          ErisMarkdown.linkCitations(body, m.sources);
          if (m === s.messages.at(-1) && m.content) addRegen(body, s);
        }
      });
    }
  };
  if (!s.messages.length && s.remote) {
    fetch(`/api/chats/${encodeURIComponent(id)}`).then((r) => r.json()).then((j) => {
      s.messages = (j.items || []).map((m) => ({ role: m.role, content: m.content, quote: m.quote, thinking: m.thinking, sources: m.sources, web: m.web, fetches: m.fetches, answers: m.answers }));
      s.remote = false;
      paint();
    }).catch(paint);
  } else {
    paint();
  }
}

function toPayload(s) {
  // The whole chat goes back, including the turns rehydrated from the server.
  // The server stores them but never replays them into a request, so leaving
  // them out here is what made a reopened chat start over from nothing.
  return s.messages.map((m) => {
    // Sent as its own field so the server can keep the two apart on disk; it is
    // the server that joins them again into what the model reads.
    const quoted = m.quote ? { quote: m.quote } : {};
    if (m.images && m.images.length) {
      const parts = [];
      if (m.content) parts.push({ type: "text", text: m.content });
      for (const url of m.images) parts.push({ type: "image_url", image_url: { url } });
      return { role: m.role, content: parts, ...quoted };
    }
    return { role: m.role, content: m.content, ...quoted };
  });
}

function setStreaming(on) {
  els.send.classList.toggle("busy", on);
  els.send.title = on ? "Stop generation" : "Send (Enter)";
  els.send.setAttribute("aria-label", on ? "Stop generation" : "Send message");
  refresh();
}

async function send(text, regen = false, quote = "") {
  let s = current();
  if (!s) {
    s = { id: crypto.randomUUID(), title: "New chat", messages: [] };
    sessions.unshift(s);
    activeId = s.id;
    ErisSidebar.setActive(activeId);
  }
  let images = [];

  if (!regen) {
    images = ErisAttach.images();
    ErisAttach.clear();
  }

  sending = true;
  refresh();

  if (!regen) {
    addMessage("user", text, images, quote);
    s.messages.push({
      role: "user",
      content: text,
      ...(images.length ? { images } : {}),
      ...(quote ? { quote } : {}),
    });
    if (s.messages.length === 1) {
      s.title = (text || quote).slice(0, 40) || (images.length ? "Image" : "New chat");
      els.chatTitle.textContent = s.title;
      ErisSidebar.setChats(sessions);
    }
  } else if (s.messages.at(-1)?.role === "assistant") {
    s.messages.pop();
    // Last DOM child is the sticky scroll button, not the message.
    [...els.messages.querySelectorAll(".msg")].pop()?.remove();
  }

  const body = addMessage("assistant", "");

  // The wait before the first frame has no shape of its own, so it says what it
  // is doing in Eris's own voice and moves on to another line while it waits.
  // It never outlives the wait: the first real frame clears it.
  const WAITING = [
    "Waking Eris up…",
    "Bonking Eris…",
    "Rubbing the sleep out of her eyes…",
    "Brewing a thought…",
    "Dusting off the brain cells…",
    "Poking Eris awake…",
    "Shuffling some ideas around…",
    "Tuning the antennas…",
    "Sharpening a pencil…",
    "Warming up the neurons…",
    "Consulting the void…",
    "Straightening her headphones…",
  ];
  let waitTimer = null;
  let waitLast = -1;
  let waitEl = null;
  const stopWait = () => {
    clearInterval(waitTimer);
    waitTimer = null;
  };
  const clearStatus = () => {
    stopWait();
    waitEl?.remove();
    waitEl = null;
  };
  const setStatus = (label) => {
    clearStatus();
    const el = document.createElement("div");
    el.className = "status";
    el.setAttribute("role", "status");
    el.textContent = label;
    body.append(el);
    waitEl = el;
  };
  // The line already on screen is not replaced by its own repeat, so the
  // sequence reads as movement rather than a stutter. The element stays put and
  // only its text changes, which is also what a screen reader hears.
  const nextWait = () => {
    let i = waitLast;
    while (i === waitLast) i = Math.floor(Math.random() * WAITING.length);
    waitLast = i;
    if (!waitEl) setStatus(WAITING[i]);
    else waitEl.textContent = WAITING[i];
  };
  nextWait();
  waitTimer = setInterval(nextWait, 2400);
  let raw = "";
  let aborted = false;
  let thinkRounds = [];
  let thinkAll = "";
  let thinkCtl = null;
  let thinkRound = "";
  let webCtl = null;
  let webR = -1;
  // Fetch cards, oldest first. The engine runs one round's calls in order, so
  // the card that started first is the one `fetch_done` closes first; the
  // frame carries no URL to match on.
  let fetchQ = [];
  let sources = [];
  let webGroups = [];
  let fetchGroups = [];
  // What each round's answer said, for the turn this session pushes back into
  // `s.messages`: without it a reopen falls back to the flat text and loses the
  // order the round logic just established on screen.
  let answerRounds = [];
  // The answer is drawn one block per round, not one per turn. A later round's
  // reasoning can only sit between two pieces of prose if those pieces are
  // separate nodes; with a single block it had nowhere to go but above it.
  let ans = null;
  let ansRound = -1;
  let ansText = "";

  const finishThink = () => {
    if (!thinkCtl) return;
    if (thinkRound) {
      thinkCtl.done();
      thinkRounds.push(thinkRound);
    } else {
      // done() clears the label timer; a detached node would keep ticking.
      thinkCtl.done();
      thinkCtl.el.remove();
    }
    thinkCtl = null;
    thinkRound = "";
  };
  const finishWeb = () => {
    if (!webCtl) return;
    webCtl.done();
    webCtl = null;
    webR = -1;
  };
  // A fetch card is closed by its own `fetch_done`, not by the round ending:
  // a card left spinning through the rest of the round would be claiming a
  // read that already finished. A run that ends without the closing frame
  // still leaves nothing spinning, which is what the sweep is for.
  const finishFetch = () => {
    for (const f of fetchQ) f.ctl.done(f.g.chars);
    fetchQ = [];
  };
  // Ends the block currently being written so whatever arrives next lands
  // after it. Cards are appended, never hoisted across rounds: the stream is
  // already in the order things happened, and the old insert-before-the-answer
  // rule is what put a new round's thinking on top of the previous round's prose.
  //
  // Keyed by round: a model that reasons mid-round is still writing the same
  // answer, and closing on that would split one answer into two blocks.
  const closeAnswer = (r) => {
    if (!ans || ansRound === r) return;
    ans = null;
    ansRound = -1;
    ansText = "";
  };
  // A card raised while the same round is still writing its prose goes above
  // that prose, as it always has; one from a later round closes the block and
  // lands underneath it.
  const placeCard = (ctl, r) => {
    if (ans && ansRound === r) body.insertBefore(ctl.el, ans);
    else closeAnswer(r);
  };

  controller = new AbortController();
  setStreaming(true);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: s.id,
        regenerate: regen,
        messages: toPayload(s),
      }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      if (res.status === 401) location.href = "/auth.html";
      throw new Error(`HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        let evt;
        try { evt = JSON.parse(data); } catch { continue; }

        if (evt.type === "tool") {
          finishThink();
          if (webCtl && evt.r !== webR) finishWeb();
          if (!webCtl) {
            clearStatus();
            webCtl = ErisToolCall.webSearch(body);
            placeCard(webCtl, evt.r);
            webR = evt.r;
          }
          webCtl.addQuery(evt.query || "Search");
          webGroups.push({ r: evt.r, query: evt.query || "", items: [] });
        } else if (evt.type === "fetch") {
          finishThink();
          clearStatus();
          const g = { r: evt.r, url: evt.url, chars: 0 };
          const ctl = ErisToolCall.fetchPage(body);
          ctl.setUrl(evt.url);
          placeCard(ctl, evt.r);
          fetchQ.push({ ctl, g });
          fetchGroups.push(g);
        } else if (evt.type === "fetch_done") {
          // Closed by round, not by arrival order: calls of one round run in
          // turn, but a card from an earlier round that already closed is gone
          // from the queue, and this must not close someone else's.
          const i = fetchQ.findIndex((f) => f.g.r === evt.r);
          if (i >= 0) {
            const [f] = fetchQ.splice(i, 1);
            f.g.chars = evt.chars || 0;
            f.ctl.done(f.g.chars);
          }
        } else if (evt.type === "sources") {
          sources.push(...evt.items);
          const web = evt.items.filter((x) => x.kind === "web");
          if (web.length) {
            // Filed by the round the frame names: a search's results follow its
            // own `tool` frame, but a fetch in the same round emits sources of
            // its own, and those must not be appended to the search group.
            const g = webGroups.find((x) => x.r === evt.r) ||
              (webGroups[webGroups.length] = { r: evt.r, query: "", items: [] });
            g.items.push(...web);
            if (evt.r === webR) webCtl?.addResults(web);
          }
        } else if (evt.type === "tool_error") {
          // A tool that failed still leaves the card it was drawing: the user
          // watching a search go by should see it end, not vanish.
          if (evt.tool === "fetch_url") {
            const i = fetchQ.findIndex((f) => f.g.r === evt.r);
            if (i >= 0) {
              const [f] = fetchQ.splice(i, 1);
              f.ctl.fail(evt.reason || evt.message || evt.code);
            }
          } else if (webCtl && (!evt.tool || evt.tool === "web_search")) {
            webCtl.fail(evt.message || evt.code);
            webCtl = null;
            webR = -1;
          }
        } else if (evt.type === "thinking") {
          if (!thinkCtl) {
            clearStatus();
            thinkCtl = ErisToolCall.thinking(body);
            placeCard(thinkCtl, evt.r);
          }
          thinkAll += evt.text;
          thinkRound += evt.text;
          thinkCtl.append(evt.text);
          autoScroll();
        } else if (evt.type === "metrics") {
          // The server states prompt usage once per call, on the frame that
          // ends it, and nowhere else; the readout keeps the newest.
          ErisStats.metrics(evt);
        } else if (evt.type === "title") {
          // The server names the chat after the answer, from what was asked;
          // the opening words it was showing were only a placeholder.
          s.title = evt.title;
          if (s.id === activeId) els.chatTitle.textContent = s.title;
          ErisSidebar.setChats(sessions);
        } else if (evt.type === "delta") {
          // Keyed by round, and rendered from this block's own text rather than
          // from `raw`: `raw` spans the whole turn, so a second round drawing
          // from it would repeat the first round's answer above its own cards.
          if (evt.r !== ansRound) {
            finishThink();
            finishWeb();
            clearStatus();
            ans = document.createElement("div");
            ans.className = "ans";
            body.append(ans);
            ansRound = evt.r;
            ansText = "";
            // One block per round on screen, one string in the stored turn; the
            // blank line is what keeps a reload from running them together.
            if (raw) raw += "\n\n";
          }
          raw += evt.text;
          ansText += evt.text;
          if (answerRounds.at(-1)?.r === evt.r) answerRounds.at(-1).text = ansText;
          else answerRounds.push({ r: evt.r, text: ansText });
          ans.innerHTML = ErisMarkdown.render(ansText);
          autoScroll();
        } else if (evt.type === "error") {
          const err = document.createElement("div");
          err.className = "err";
          err.setAttribute("role", "alert");
          err.textContent = evt.message;
          body.append(err);
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      aborted = true;
      if (!raw && !thinkAll && !webCtl) setStatus("Stopped.");
    } else {
      const err = document.createElement("div");
      err.className = "err";
      err.setAttribute("role", "alert");
      err.textContent = e.message;
      body.append(err);
    }
  } finally {
    controller = null;
    sending = false;
    setStreaming(false);
    els.input.focus();
    clearStatus();
    if (!aborted && !raw && !body.querySelector(".err")) {
      // Thinking rows and search rows are not an answer, so they no longer
      // suppress this. Silence after a run of tool calls is what the user reads
      // as "it stopped".
      setStatus(thinkAll || webCtl ? "No answer came back. Try again, or narrow the question." : "No response.");
    }
    finishThink();
    finishWeb();
    finishFetch();
    ErisMarkdown.highlight(body);
    ErisMarkdown.linkCitations(body, sources);
    if (raw) {
      const groups = webGroups.filter((g) => g.items.length);
      s.messages.push({
        role: "assistant",
        content: raw,
        ...(thinkRounds.length ? { thinking: thinkRounds } : {}),
        ...(sources.length ? { sources } : {}),
        ...(groups.length ? { web: groups } : {}),
        ...(fetchGroups.length ? { fetches: fetchGroups } : {}),
        ...(answerRounds.length > 1 ? { answers: answerRounds } : {}),
      });
      addRegen(body, s);
    }
  }
}


/* The stream's frame handlers. */

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (sending) {
    if (controller) controller.abort();
    return;
  }
  const text = els.input.value.trim();
  if (els.input.value.length > MAX) return;
  const hasImg = ErisAttach.hasImage();
  if (!text && !hasImg && !pendingQuote) return;
  const quote = pendingQuote;
  setQuote("");
  els.input.value = "";
  syncInput();
  send(text, false, quote);
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!sending) els.form.requestSubmit();
  }
});

els.input.addEventListener("input", syncInput);

// The on-screen keyboard shrinks the visual viewport without resizing the
// layout, so a fixed-height app leaves the composer underneath it.
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", () => {
    if (document.activeElement === els.input) {
      els.messages.scrollTop = els.messages.scrollHeight;
    }
  });
}

ErisAttach.onChange(refresh);

els.menuBtn.addEventListener("click", () => ErisSidebar.open());

els.attach.addEventListener("click", () => els.fileInput.click());

els.fileInput.addEventListener("change", () => {
  ErisAttach.add(els.fileInput.files);
  els.fileInput.value = "";
});

els.input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    ErisAttach.add(files);
  }
});

for (const ev of ["dragover", "dragenter"]) {
  els.box.addEventListener(ev, (e) => {
    e.preventDefault();
    els.box.classList.add("drag");
  });
}
for (const ev of ["dragleave", "drop"]) {
  els.box.addEventListener(ev, (e) => {
    e.preventDefault();
    els.box.classList.remove("drag");
  });
}
els.box.addEventListener("drop", (e) => {
  const files = e.dataTransfer?.files;
  if (files?.length) ErisAttach.add(files);
});

/* The model picker sits in the composer and writes the same preference the
   settings dialog does, so /api/chat keeps resolving the model from the
   account rather than from each request. The effort step is stored the same
   way: it is an index into the model's own ladder, which the server resolves
   against whichever model is in use. */
ErisStats.init();

ErisPicker.init({
  onModel: (model) => {
    fetch("/api/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model }),
    }).catch(() => {});
  },
  onEffort: (effort) => {
    fetch("/api/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ effort }),
    }).catch(() => {});
  },
});

fetch("/api/models").then((r) => r.json()).then((j) => {
  ErisPicker.setModels(j.items, j.default, j.labels, j.capabilities);
}).catch(() => {});


ErisSidebar.init({
  chats: [],
  onNew: () => blankChat(),
  onSelect: (id) => loadSession(id),
  onPin: async (id, pinned) => {
    const s = sessions.find((x) => x.id === id);
    if (s) s.pinned = pinned;
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned }),
    }).catch(() => {});
  },
  onRename: async (id, title) => {
    const s = sessions.find((x) => x.id === id);
    if (s) {
      s.title = (title || "").trim().slice(0, 80) || s.title;
      if (s.id === activeId) els.chatTitle.textContent = s.title;
    }
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    }).catch(() => {});
  },
  onDelete: async (id) => {
    sessions = sessions.filter((x) => x.id !== id);
    ErisSidebar.setChats(sessions);
    await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" }).catch(() => {});
    if (id === activeId) {
      if (!sessions.length) {
        blankChat();
      } else {
        loadSession(sessions[0].id);
      }
    }
  },
});

function refreshAccount() {
  fetch("/api/auth/me").then((r) => {
    if (!r.ok) {
      location.href = "/auth.html";
      return null;
    }
    return r.json().catch(() => ({}));
  }).then((u) => {
    if (!u || !u.name) return;
    userName = u.name;
    userAvatar = u.avatar || "";
    modelAvatar = (u.prefs || {}).model_avatar || "";
    const nm = document.getElementById("sideName");
    if (nm) nm.textContent = u.name;
    paintAvatar(document.getElementById("sideAvatar"));
    // Messages can render before this resolves, so repaint what is already there.
    els.messages.querySelectorAll(".avatar").forEach(paintAvatar);
    els.messages.querySelectorAll(".msg.assistant .role").forEach(paintRole);
  }).catch(() => {
    // A background repaint: a network blip must not raise, the next save fires
    // this again.
  });
}

refreshAccount();

/* The settings dialog writes to the account this page is showing. Nothing
   navigates any more, so it asks for a repaint once a change lands. The model
   it saves is the one the composer's picker shows. */
window.addEventListener("eris:settings-saved", () => {
  refreshAccount();
  fetch("/api/models")
    .then((r) => r.json())
    .then((j) => ErisPicker.setModels(j.items, j.default, j.labels, j.capabilities))
    .catch(() => {});
});

fetch("/api/chats").then((r) => r.json()).then((j) => {
  sessions = (j.items || []).map((c) => ({ id: c.id, title: c.title, pinned: !!c.pinned, messages: [], remote: true }));
  ErisSidebar.setChats(sessions);
}).catch(() => {}).finally(() => {
  if (!sessions.length) {
    blankChat();
  } else {
    const first = sessions[0];
    activeId = first.id;
    ErisSidebar.setChats(sessions);
    ErisSidebar.setActive(activeId);
    els.chatTitle.textContent = first.title || "New chat";
    showEmpty();
  }
});
syncInput();
els.input.focus();
