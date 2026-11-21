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
  planStrip: document.getElementById("planStrip"),
};

// The model the next turn will name, kept from the last answer that resolved
// one. Naming it on the request is what makes the model the composer shows the
// model the server runs, instead of two resolutions that can disagree.
let currentModel = "";

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
// The title a chat carries until one is chosen, matching `chats.UNTITLED`
// on the server: the header, the optimistic session and the fallback name
// are one string, not three copies of it.
const UNTITLED = "New chat";

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
let chatListPending = true;
// One run per chat, keyed by the chat it writes into. A single pair of globals
// made the composer's Stop button belong to whichever chat happened to be open:
// switching chats mid-run left the new one showing Stop, and pressing it
// aborted the answer still streaming into the chat left behind.
const runs = new Map();
const isSending = () => runs.has(activeId);
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
    if (isSending()) return;
    const last = s.messages.at(-1);
    const q = s.messages.at(-2);
    if (!last || last.role !== "assistant" || !q || q.role !== "user" || typeof q.content !== "string" || !q.content || (q.images && q.images.length) || (q.files && q.files.length)) return;
    send(q.content, true, "", body);
  });
  body.append(b);
}

// The empty state is markup in index.html, so it is stated once there and
// copied here before any message can clear it.
const EMPTY_HTML = document.getElementById("empty")?.outerHTML || "";

function showEmpty() {
  resetMessages(EMPTY_HTML);
}

// innerHTML wipes destroy the sticky scroll button; every message-area reset
// goes through here so it stays attached and its visibility state stays fresh.
function resetMessages(html = "") {
  hideQuote();
  clearPlan();
  els.messages.innerHTML = html;
  els.messages.append(scrollBtn);
  syncScrollBtn();
}

// The strip above the field holds the plan of the run in flight and nothing
// else, so it empties with the run.
function clearPlan() {
  els.planStrip.hidden = true;
  els.planStrip.textContent = "";
}

// Every avatar in the app draws from the same rule: the picture when the
// account has one, a short label otherwise. Background images need the sizing
// that .avatar and .side-avatar declare, so the photo is set here, not in the
// markup. The user's chip falls back to initials, the assistant's to its name.
function paintFace(el, src, label) {
  if (!el) return;
  el.style.backgroundImage = src ? `url("${src}")` : "";
  el.textContent = src ? "" : label;
}

const initials = () => (userName || "ME").slice(0, 2).toUpperCase();

// One round's prose, in the same element live and after a reload. Live streams
// it by re-rendering the accumulated text into the same node.
function answerBlock(text) {
  const a = document.createElement("div");
  a.className = "ans";
  a.innerHTML = ErisMarkdown.render(text);
  return a;
}

// A run that failed says so in the transcript, and a screen reader is told the
// moment it appears rather than finding it on the next pass.
function errorBlock(message) {
  const err = document.createElement("div");
  err.className = "err";
  err.setAttribute("role", "alert");
  err.textContent = message;
  return err;
}

// A file a turn carried, named rather than printed. It sits in the row between
// the avatar and the question, so the chip reads as riding along with the turn.
// Clicking one opens the text the model was handed.
function filesRow(files) {
  if (!files || !files.length) return null;
  const row = document.createElement("div");
  row.className = "files";
  for (const f of files) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "file-chip";
    chip.title = f.name;
    chip.setAttribute("aria-label", `Open ${f.name}`);
    chip.insertAdjacentHTML("afterbegin", ErisAttach.iconFor(f.name));
    const nm = document.createElement("span");
    nm.className = "chip-name";
    nm.textContent = f.name;
    chip.append(nm);
    chip.addEventListener("click", () => ErisFileView.open(f, chip));
    row.append(chip);
  }
  return row;
}

function addMessage(role, text, images, quote, files) {
  document.getElementById("empty")?.remove();
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const body = document.createElement("div");
  body.className = "body";
  if (images && images.length) {
    const imgs = document.createElement("div");
    imgs.className = "imgs";
    for (const src of images) {
      const im = document.createElement("img");
      im.src = src;
      im.alt = "attachment";
      im.addEventListener("click", () => ErisLightbox.open(src, im.alt, im));
      imgs.append(im);
    }
    body.append(imgs);
  }
  if (role === "user") {
    if (quote) {
      const q = document.createElement("div");
      q.className = "quoted";
      q.textContent = quote;
      body.append(q);
    }
    // A file is named, not printed: the model got its text, the reader only
    // needs to see which file the answer is about. It rides in the body above
    // the question, where the quote sits, so both read as what the turn carried
    // rather than as a column of the answer.
    const filesEl = filesRow(files);
    if (filesEl) body.append(filesEl);
    if (text) {
      const t = document.createElement("div");
      t.className = "bubble";
      t.textContent = text;
      // The question is plain text on purpose, so the URL is wrapped after the
      // fact rather than by inserting HTML into it.
      ErisMarkdown.autolink(t);
      body.append(t);
    }
    const av = document.createElement("div");
    av.className = "avatar";
    paintFace(av, userAvatar, initials());
    wrap.append(av);
    // A turn that is only a file has nothing to put in the body, and an empty
    // one would still take the row's gap.
    if (body.childNodes.length) wrap.append(body);
  } else {
    body.innerHTML = ErisMarkdown.render(text);
    const roleEl = document.createElement("div");
    roleEl.className = "role";
    paintFace(roleEl, modelAvatar, "AI");
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
    els.input.value.trim().length > 0 || ErisAttach.hasImage() || ErisAttach.hasFile() || !!pendingQuote;
  const over = len > MAX;
  els.send.disabled = !isSending() && (!has || over || chatListPending || current()?.remote);
  els.count.textContent = len + " / " + MAX;
  els.count.classList.toggle("show", len > MAX * 0.8);
  els.count.classList.toggle("over", over);
}

function syncInput() {
  // Grow to the content and let the stylesheet's `max-height` stop it: the cap
  // is stated in one place, and this only asks how tall the text is.
  els.input.style.height = "auto";
  els.input.style.height = els.input.scrollHeight + "px";
  refresh();
}

/* Sessions. */

function blankChat() {
  activeId = null;
  ErisSidebar.setActive(null);
  els.chatTitle.textContent = UNTITLED;
  ErisAttach.clear();
  setQuote("");
  resetMessages();
  showEmpty();
  els.input.value = "";
  syncInput();
  syncStreaming();
  ErisStats?.clearCall?.();
  els.input.focus();
}

async function fetchHistory(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  if (!Array.isArray(data.items)) throw new Error("Invalid chat history");
  return data.items;
}

function showHistoryError(message, retry) {
  const error = errorBlock(message);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "regen";
  button.textContent = "Try again";
  button.addEventListener("click", retry);
  error.append(button);
  els.messages.insertBefore(error, scrollBtn);
}

async function loadSession(id) {
  const s = sessions.find((x) => x.id === id);
  if (!s) return;
  activeId = id;
  ErisSidebar.setActive(id);
  els.chatTitle.textContent = s.title;
  ErisAttach.clear();
  setQuote("");
  resetMessages();
  syncStreaming();
  ErisStats.metrics(s.metrics);
  const paint = () => {
    if (activeId !== id) return;
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
        const body = addMessage(m.role, answerRows.size ? "" : m.content, m.images, m.quote, m.files);
        const web = (m.sources || []).filter((x) => x.kind === "web");
        // A turn saved before the search groups were recorded kept only its
        // rows, so those are drawn as one group. The fallback is only taken
        // when the turn carries no cards: a price or a forecast also labels
        // its source `web`, and one of those would otherwise come back as a
        // search nobody ran.
        const legacy = !m.cards?.length && !m.fetches?.length && web.length;
        const groups = (m.web && m.web.length ? m.web : legacy ? [{ query: "", items: web }] : [])
          .map((g) => ({ r: g.r ?? 0, query: g.query, items: g.items,
                         ok: g.ok !== false, reason: g.reason || "" }));
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
        // The plan belongs to the run in flight and lives above the composer,
        // not in the turn: a stored plan row is skipped here so a reopened chat
        // does not show a finished run's steps as if it were still working.
        const cards = (m.cards || [])
          .filter((c) => c.tool !== "plan")
          .map((c) => ({ r: c.r ?? 0, tool: c.tool, card: c.card, reason: c.reason || "" }));
        const cardRowsByRound = new Map();
        for (const c of cards) {
          if (!cardRowsByRound.has(c.r)) cardRowsByRound.set(c.r, []);
          cardRowsByRound.get(c.r).push(c);
        }
        let anchor = null;
        const put = (el) => {
          if (anchor) anchor.after(el);
          else body.prepend(el);
          anchor = el;
        };
        // A round's thinking, then the prose it wrote, then the calls it made:
        // that is the order the round streamed in, and replay has to match it
        // or a reloaded turn shows its cards above text they followed live.
        const roundNums = new Set([...rows.keys(), ...readRows.keys(), ...cardRowsByRound.keys(),
                                   ...answerRows.keys()]);
        const rounds = Math.max(thinkRounds.length, ...[...roundNums].map((r) => r + 1), 0);
        for (let i = 0; i < rounds; i++) {
          if (thinkRounds[i]) {
            const th = ErisToolCall.thinking(body);
            put(th.el);
            th.append(thinkRounds[i]);
            th.done("Thought");
          }
          const text = answerRows.get(i);
          if (text) put(answerBlock(text));
          for (const g of rows.get(i) || []) {
            const tc = ErisToolCall.webSearch(body);
            put(tc.el);
            if (g.query) tc.addQuery(g.query);
            tc.addResults(g.items);
            if (g.ok) tc.done();
            // A search that failed has no rows to show and a sentence to
            // replay, so it comes back in the state the live one ended in
            // rather than as an empty card that looks like no results.
            else tc.fail(g.reason);
          }
          for (const f of readRows.get(i) || []) {
            const fc = ErisToolCall.fetchPage(body);
            put(fc.el);
            fc.setUrl(f.url);
            if (f.ok === false) fc.fail(f.reason);
            else fc.done(f.chars);
          }
          for (const c of cardRowsByRound.get(i) || []) {
            const cc = ErisToolCall[c.tool](body);
            put(cc.el);
            if (c.card) cc.done(c.card);
            else cc.fail(c.reason);
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
  if (!s.remote) {
    paint();
    return;
  }
  const attempt = Symbol();
  s.historyLoad = attempt;
  const loading = document.createElement("p");
  loading.className = "status";
  loading.setAttribute("role", "status");
  loading.textContent = "Loading chat…";
  els.messages.insertBefore(loading, scrollBtn);
  const isCurrent = () => activeId === id && s.historyLoad === attempt;
  try {
    const items = await fetchHistory(`/api/chats/${encodeURIComponent(id)}`);
    if (!isCurrent()) return;
    s.messages = items;
    s.remote = false;
    loading.remove();
    refresh();
    paint();
  } catch {
    if (!isCurrent()) return;
    loading.remove();
    showHistoryError("Could not load this chat.", () => loadSession(id));
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
    const attached = m.files && m.files.length ? { files: m.files } : {};
    if (m.images && m.images.length) {
      const parts = [];
      if (m.content) parts.push({ type: "text", text: m.content });
      for (const url of m.images) parts.push({ type: "image_url", image_url: { url } });
      return { role: m.role, content: parts, ...quoted, ...attached };
    }
    return { role: m.role, content: m.content, ...quoted, ...attached };
  });
}

function setStreaming(on) {
  els.send.classList.toggle("busy", on);
  els.send.title = on ? "Stop generation" : "Send (Enter)";
  els.send.setAttribute("aria-label", on ? "Stop generation" : "Send message");
  refresh();
}

// Whatever run belongs to the chat on screen decides what the composer shows:
// the button is Stop only while this chat is answering, not while any chat is.
function syncStreaming() {
  setStreaming(isSending());
}

async function send(text, regen = false, quote = "", previousBody = null) {
  let s = current();
  if (!s) {
    s = { id: crypto.randomUUID(), title: UNTITLED, messages: [] };
    sessions.unshift(s);
    activeId = s.id;
    ErisSidebar.setActive(activeId);
  }
  let images = [];
  let files = [];

  if (!regen) {
    images = ErisAttach.images();
    files = ErisAttach.files();
    ErisAttach.clear();
  }

  runs.set(s.id, null);
  refresh();

  if (!regen) {
    addMessage("user", text, images, quote, files);
    s.messages.push({
      role: "user",
      content: text,
      ...(images.length ? { images } : {}),
      ...(files.length ? { files } : {}),
      ...(quote ? { quote } : {}),
    });
    if (s.messages.length === 1) {
      s.title = (text || quote).slice(0, 40) || (files[0]?.name || (images.length ? "Image" : UNTITLED));
      els.chatTitle.textContent = s.title;
      ErisSidebar.setChats(sessions);
    }
  }
  previousBody?.querySelector(".regen-error")?.remove();

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
    const line = document.createElement("span");
    line.className = "status-line on";
    line.textContent = label;
    el.append(line);
    body.append(el);
    waitEl = el;
  };
  // The line already on screen is not replaced by its own repeat, so the
  // sequence reads as movement rather than a stutter. The new line arrives
  // under the old one and the two cross-fade, so the swap is a transition
  // rather than a flicker. One role="status" element holds them both, which is
  // what keeps the announcement to a single line while two are on screen.
  const nextWait = () => {
    let i = waitLast;
    while (i === waitLast) i = Math.floor(Math.random() * WAITING.length);
    waitLast = i;
    if (!waitEl) {
      setStatus(WAITING[i]);
      return;
    }
    const prev = waitEl.querySelector(".status-line.on");
    // Whatever left last time is fully faded by now, so it goes before the next
    // one arrives and the cell never holds more than the pair mid-swap.
    waitEl.querySelectorAll(".status-line:not(.on)").forEach((n) => n.remove());
    const line = document.createElement("span");
    line.className = "status-line";
    line.textContent = WAITING[i];
    waitEl.append(line);
    // The outgoing line leaves the accessibility tree with its opacity, so the
    // region still reads as one line.
    if (prev) prev.setAttribute("aria-hidden", "true");
    // Commits the arrived-at opacity before the classes flip, so the cross-fade
    // has a starting frame instead of resolving within the one that added it.
    void line.offsetHeight;
    prev?.classList.remove("on");
    line.classList.add("on");
  };
  nextWait();
  waitTimer = setInterval(nextWait, 5000);
  let raw = "";
  let aborted = false;
  let thinkRounds = [];
  let thinkAll = "";
  let thinkCtl = null;
  let thinkRound = "";
  let thinkR = -1;
  let webCtl = null;
  let webR = -1;
  // The call the open search card belongs to. Two searches can sit in one
  // round, so the round does not tell them apart and the id does.
  let webId = "";
  // Fetch cards, oldest first. A round runs its calls together and reports
  // them as each finishes, so the order they close in is not the order they
  // started in; the closing frame carries the id of the call it belongs to,
  // and that is what a card is matched on. A round holds two reads of the same
  // URL often enough that the URL alone would not tell them apart.
  let fetchQ = [];
  let sources = [];
  let webGroups = [];
  let fetchGroups = [];
  // Weather, wayback, crypto and the market card, in the order they were
  // drawn. Kept whole rather than as a count: a reload rebuilds the card from
  // this, and there is nothing on the page to read it back out of.
  let cardRows = [];
  // The plan card above the field, once the run has drawn one. A revised plan
  // repaints this card rather than adding a second, because the reader wants
  // the plan as it stands, not the history of it.
  let planCtl = null;
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
      // Filed by round number, not by arrival: a turn whose second round
      // reasoned and whose first did not still has to draw that card on the
      // second round's cards on reload, which only the index keeps.
      for (let i = thinkRounds.length; i < thinkR; i++) thinkRounds[i] = "";
      thinkRounds[thinkR] = thinkRound;
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
    webId = "";
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
  // after it. Cards are appended, never hoisted: a round streams its prose
  // before it makes its calls, so the card belongs underneath that prose. The
  // old insert-before-the-answer rule lifted it above prose the reader had
  // already scrolled past, which is what made a card look like it never came.
  //
  // Keyed by round: a model that reasons mid-round is still writing the same
  // answer, and closing on that would split one answer into two blocks.
  const closeAnswer = (r) => {
    if (!ans || ansRound === r) return;
    ans = null;
    ansRound = -1;
    ansText = "";
  };

  const ctl = new AbortController();
  const requestedModel = currentModel || undefined;
  const requestedEffort = ErisPicker.getEffort(requestedModel);
  runs.set(s.id, ctl);
  syncStreaming();
  s.metrics = null;
  if (s.id === activeId) ErisStats.clearCall();

  let receivedDone = false;
  let failure = "";
  let chatChanged = false;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: s.id,
        regenerate: regen,
        messages: regen ? toPayload(s).slice(0, -1) : toPayload(s),
        // Stated on the turn as well as stored on the account: a picker change
        // that has not reached the database yet cannot be the model this
        // answer came from, and the frame the model was called with is what the
        // cards and the readout name.
        model: requestedModel,
        effort: requestedEffort,
      }),
      signal: ctl.signal,
    });
    if (!res.ok || !res.body) {
      if (res.status === 401) location.href = "/auth.html";
      const detail = await res.json().catch(() => ({}));
      throw new Error(typeof detail.detail === "string" ? detail.detail : `HTTP ${res.status}`);
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
        if (data === "[DONE]") {
          receivedDone = true;
          continue;
        }
        let evt;
        try { evt = JSON.parse(data); } catch { continue; }

        if (evt.type === "tool") {
          finishThink();
          // One card per search call, which is also how a reload draws them:
          // the server stores a group per call, so two searches in one round
          // must not share a card here or the live turn and the reopened one
          // would disagree about what was searched.
          const same = webCtl && webR === evt.r && webId === (evt.id || "");
          if (webCtl && !same) finishWeb();
          if (!webCtl) {
            clearStatus();
            closeAnswer(evt.r);
            webCtl = ErisToolCall.webSearch(body);
            webR = evt.r;
            webId = evt.id || "";
          }
          webCtl.addQuery(evt.query || "Search");
          webGroups.push({ id: evt.id || "", r: evt.r, query: evt.query || "", items: [] });
        } else if (evt.type === "fetch") {
          finishThink();
          clearStatus();
          closeAnswer(evt.r);
          const g = { id: evt.id || "", r: evt.r, url: evt.url, chars: 0 };
          const ctl = ErisToolCall.fetchPage(body);
          ctl.setUrl(evt.url);
          fetchQ.push({ ctl, g });
          fetchGroups.push(g);
        } else if (evt.type === "fetch_done") {
          // Closed by round and by the call's own id together, the pair the
          // server stores: a round runs its reads at once and reports them as
          // each finishes, so two reads of the same URL would otherwise close
          // whichever card happened to come first.
          const i = fetchQ.findIndex((f) => f.g.r === evt.r && f.g.id === (evt.id || ""));
          if (i >= 0) {
            const [f] = fetchQ.splice(i, 1);
            f.g.chars = evt.chars || 0;
            f.ctl.done(f.g.chars);
          }
        } else if (evt.type === "plan") {
          finishThink();
          clearStatus();
          closeAnswer(evt.r);
          // The plan is not part of the turn it appeared in: it lives above the
          // field so it stays on screen while the rest of the run scrolls.
          // One card per run, repainted in place as the model revises it.
          if (!planCtl) {
            els.planStrip.hidden = false;
            planCtl = ErisToolCall.plan(els.planStrip);
          }
          planCtl.done(evt.card);
        } else if (ErisToolCall.isCard(evt.type)) {
          finishThink();
          clearStatus();
          closeAnswer(evt.r);
          const ctl = ErisToolCall[evt.type](body);
          ctl.done(evt.card);
          cardRows.push({ r: evt.r, tool: evt.type, card: evt.card });
        } else if (evt.type === "sources") {
          sources.push(...evt.items);
          const web = evt.items.filter((x) => x.kind === "web");
          if (web.length) {
            // Filed under the search call that found them, by round and by the
            // frame's id together, which is how the server files them: a fetch
            // in the same round emits sources of its own, and two searches in
            // one round each want their own list. A row with no group to join is
            // a citation from some other tool — a price, a forecast — which is
            // not a search and must not draw as one.
            const g = webGroups.find((x) => x.r === evt.r && x.id === (evt.id || ""));
            if (g) g.items.push(...web);
            if (webCtl && webR === evt.r && webId === (evt.id || "")) webCtl.addResults(web);
          }
        } else if (evt.type === "tool_error") {
          // What the card says the tool failed at, stated once for every branch
          // below: the tool's own reason when it gave one, else the message,
          // else the code.
          const reason = evt.reason || evt.message || evt.code;
          // A tool that failed still leaves the card it was drawing: the user
          // watching a search go by should see it end, not vanish.
          if (evt.tool === "fetch_url") {
            const i = fetchQ.findIndex((f) => f.g.r === evt.r && f.g.id === (evt.id || ""));
            if (i >= 0) {
              const [f] = fetchQ.splice(i, 1);
              f.ctl.fail(reason);
            }
          } else if (evt.tool === "plan") {
            // A failed revision leaves the plan on screen, and on the server,
            // exactly as it was; only a run that never got a plan draws the
            // failure, in the strip, where the plan would have gone.
            if (!planCtl) {
              els.planStrip.hidden = false;
              planCtl = ErisToolCall.plan(els.planStrip);
              planCtl.fail(reason);
            }
          } else if (ErisToolCall.isCard(evt.tool)) {
            // No card was drawn on start — these render their payload in one
            // pass — so the failure is what raises it, in its failed state.
            finishThink();
            clearStatus();
            closeAnswer(evt.r);
            const ctl = ErisToolCall[evt.tool](body);
            ctl.fail(reason);
            cardRows.push({ r: evt.r, tool: evt.tool, card: null, reason });
          } else {
            // A failed search draws no card of its own: the start frame already
            // opened one, so the failure lands on the stored group and shows on
            // the card on screen. A reload reads the group, and without this the
            // turn comes back as if nothing had been asked. Only a search may
            // claim a group; every other tool that failed a round earlier has no
            // group of its own and must not take the search's.
            const g = evt.tool === "web_search"
              ? webGroups.find((x) => x.r === evt.r && x.id === (evt.id || ""))
              : null;
            if (g) { g.ok = false; g.reason = reason; }
            if (webCtl && webR === evt.r && webId === (evt.id || "")
                     && evt.tool === "web_search") {
              webCtl.fail(reason);
              webCtl = null;
              webR = -1;
              webId = "";
            }
          }
        } else if (evt.type === "thinking") {
          if (!thinkCtl) {
            clearStatus();
            closeAnswer(evt.r);
            thinkCtl = ErisToolCall.thinking(body);
            thinkR = evt.r;
          }
          thinkAll += evt.text;
          thinkRound += evt.text;
          thinkCtl.append(evt.text);
          autoScroll();
        } else if (evt.type === "metrics") {
          s.metrics = evt;
          if (s.id === activeId) ErisStats.metrics(evt);
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
            ans = answerBlock("");
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
          failure = evt.message || "The answer failed.";
          chatChanged = evt.code === "chat_changed";
          body.append(errorBlock(failure));
        }
      }
    }
    if (!receivedDone) throw new Error("Connection closed before the answer finished.");
  } catch (e) {
    if (e.name === "AbortError") {
      aborted = true;
      if (!raw && !thinkAll && !webCtl) setStatus("Stopped.");
    } else {
      failure = e.message;
      body.append(errorBlock(failure));
    }
  } finally {
    // Only this chat's run is cleared. A second chat's run, started after the
    // reader switched away, is a different entry and is left alone.
    if (runs.get(s.id) === ctl) runs.delete(s.id);
    syncStreaming();
    if (s.id === activeId) els.input.focus();
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
    // The strip carries the plan of the run in flight and nothing else, so a
    // turn that has been saved takes it away with it. The handle goes too: the
    // next run must draw its own card, not repaint this detached one.
    if (s.id === activeId) clearPlan();
    planCtl = null;
    ErisMarkdown.highlight(body);
    ErisMarkdown.linkCitations(body, sources);
    const replaced = regen && receivedDone && !aborted && !failure && raw.trim();
    if (replaced) {
      s.messages.pop();
      previousBody?.closest(".msg")?.remove();
    }
    if (regen && !replaced) {
      body.closest(".msg")?.remove();
      const error = errorBlock(aborted ? "Regeneration stopped." : failure || "No replacement answer came back.");
      error.classList.add("regen-error");
      previousBody?.append(error);
    }
    if (raw.trim() && !chatChanged && (!regen || replaced)) {
      // A group with no rows is dropped, but a failed one is kept: it has
      // nothing to show and still has something to say. The server keeps the
      // same two, so the turn on screen matches the one a reload reads back.
      const groups = webGroups.filter((g) => g.items.length || g.ok === false);
      s.messages.push({
        role: "assistant",
        content: raw,
        ...(thinkRounds.length ? { thinking: thinkRounds } : {}),
        ...(sources.length ? { sources } : {}),
        ...(groups.length ? { web: groups } : {}),
        ...(fetchGroups.length ? { fetches: fetchGroups } : {}),
        ...(cardRows.length ? { cards: cardRows } : {}),
        ...(answerRounds.length > 1 ? { answers: answerRounds } : {}),
      });
      addRegen(body, s);
      if (s.id === activeId && !body.isConnected) loadSession(s.id);
    }
  }
}


/* The stream's frame handlers. */

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (isSending()) {
    // Only the chat on screen is stopped, and only its own run: the composer is
    // the chat's, so the button it shows is the run it aborts.
    runs.get(activeId)?.abort();
    return;
  }
  if (chatListPending || current()?.remote) return;
  const text = els.input.value.trim();
  if (els.input.value.length > MAX) return;
  const hasImg = ErisAttach.hasImage() || ErisAttach.hasFile();
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
    if (!isSending()) els.form.requestSubmit();
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

// The attachment row sits above the box now, so the whole form is the target:
// dropping on a chip lands where the drop on the box does. dragleave fires
// again for every child the pointer crosses, so it only clears when the pointer
// has actually left the form.
for (const ev of ["dragover", "dragenter"]) {
  els.form.addEventListener(ev, (e) => {
    e.preventDefault();
    els.box.classList.add("drag");
  });
}
els.form.addEventListener("dragleave", (e) => {
  if (els.form.contains(e.relatedTarget)) return;
  els.box.classList.remove("drag");
});
els.form.addEventListener("drop", (e) => {
  e.preventDefault();
  els.box.classList.remove("drag");
  const files = e.dataTransfer?.files;
  if (files?.length) ErisAttach.add(files);
});

ErisStats.init();
let prefsWrite = Promise.resolve();

function queuePrefs(body) {
  const write = prefsWrite.then(() => postPrefs(body));
  prefsWrite = write.catch(() => {});
  return write;
}

ErisPicker.init({
  onModel: (model) => {
    currentModel = model;
    ErisStats.metrics(current()?.metrics);
    queuePrefs({ model })
      .then((j) => {
        // A model the server moved away from (a fallback, or a list that did
        // not hold it) is not the one the picker just chose. The control is
        // repainted from the server's answer rather than left to name a model
        // no turn would ask for.
        if (currentModel === model && j && j.model && j.model !== model) reconcile(j, model);
      })
      .catch(() => {});
  },
  onEffort: (model, effort) => queuePrefs({ effort_model: model, effort }),
});

/* The picked id and the resolved one disagree, so the list is asked for once
   more and both the menu and the readout are drawn from it. */
function reconcile(j, previous) {
  fetch("/api/models")
    .then((r) => r.json())
    .then((m) => {
      if (currentModel !== previous) return;
      currentModel = j.model;
      ErisPicker.setModels(m.items, j.model, m.labels, m.capabilities);
      ErisStats.metrics(current()?.metrics);
    })
    .catch(() => {});
}

/* A preference is followed by the model the account is on, resolved by the
 * route rather than assumed here: the client writes an id, and a fallback the
 * server applied afterwards is not visible from this side. */
function postPrefs(body) {
  return fetch("/api/prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`prefs ${r.status}`))));
}

fetch("/api/models").then((r) => r.json()).then((j) => {
  // The default here is already resolved through the account's stored pick, so
  // it is the id a turn would use before anyone touches a control.
  if (j && j.default) currentModel = j.default;
  ErisPicker.setModels(j.items, j.default, j.labels, j.capabilities);
  ErisStats.metrics(current()?.metrics);
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
    try {
      const res = await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      els.messages.insertBefore(errorBlock("Could not delete this chat. Try again."), scrollBtn);
      return;
    }
    runs.get(id)?.abort();
    sessions = sessions.filter((x) => x.id !== id);
    ErisSidebar.setChats(sessions);
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
    paintFace(document.getElementById("sideAvatar"), userAvatar, initials());
    // Messages can render before this resolves, so repaint what is already there.
    els.messages.querySelectorAll(".avatar").forEach((el) => paintFace(el, userAvatar, initials()));
    els.messages.querySelectorAll(".msg.assistant .role").forEach((el) => paintFace(el, modelAvatar, "AI"));
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
    .then((j) => {
      if (j && j.default) currentModel = j.default;
      ErisPicker.setModels(j.items, j.default, j.labels, j.capabilities);
      ErisStats.metrics(current()?.metrics);
    })
    .catch(() => {});
  // The dialog may have changed the model or the context, and the readout
  // beside Send describes both. It is asked to load again rather than left to
  // show the figures of the model that was in use before the save.
  ErisStats?.load?.();
});

async function loadChatList() {
  chatListPending = true;
  refresh();
  try {
    const items = await fetchHistory("/api/chats");
    const known = new Set(sessions.map((s) => s.id));
    sessions.push(...items.filter((c) => !known.has(c.id)).map((c) => ({
      id: c.id, title: c.title, pinned: !!c.pinned, messages: [], remote: true,
    })));
    ErisSidebar.setChats(sessions);
    if (!activeId && sessions.length) loadSession(sessions[0].id);
    else if (!activeId) blankChat();
  } catch {
    if (!activeId) {
      resetMessages();
      showHistoryError("Could not load your chat list.", loadChatList);
    }
  } finally {
    chatListPending = false;
    refresh();
  }
}

loadChatList();
syncInput();
els.input.focus();
