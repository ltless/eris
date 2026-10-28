/**
 * ErisSidebar — Sidebar state management, chat history, and responsive drawer.
 * Provides consistent API for app.js:
 * .init(), .toggle(), .open(), .close(), .setChats(), .setActive(), .prepend()
 */
const ErisSidebar = (() => {
  const $ = (id) => document.getElementById(id);
  const mobile = () => matchMedia("(max-width:768px)").matches;
  const KEY = "eris-sidebar";

  let side, scrim, historyEl, opts = {};
  let chats = [], activeId = null, collapsed = false;

  const CHAT_ICON =
    '<svg viewBox="0 0 24 24"><path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/></svg>';

  function svgIcon(html) {
    const box = document.createElement("span");
    box.className = "ic";
    box.innerHTML = html;
    return box;
  }

  function applyCollapse() {
    if (!side) return;
    side.classList.toggle("collapsed", collapsed);
    const btn = $("sideToggle");
    if (btn) {
      const label = collapsed ? "Expand sidebar" : "Collapse sidebar";
      btn.setAttribute("aria-label", label);
      btn.setAttribute("aria-expanded", String(!collapsed));
      btn.title = `${label} (Ctrl+B)`;
    }
  }

  function open() {
    if (side) side.classList.add("open");
    if (scrim) scrim.classList.add("show");
  }

  function close() {
    if (side) side.classList.remove("open");
    if (scrim) scrim.classList.remove("show");
  }

  function toggle() {
    if (mobile()) {
      if (side && side.classList.contains("open")) {
        close();
      } else {
        open();
      }
      return;
    }
    collapsed = !collapsed;
    applyCollapse();
    try {
      localStorage.setItem(KEY, collapsed ? "1" : "0");
    } catch (e) {}
  }

  const PIN_ICON =
    '<svg viewBox="0 0 24 24"><path d="M9 4h6l1 7 3 3v2H5v-2l3-3z"/><path d="M12 16v5"/></svg>';

  function actionBtn(title, icon, fn) {
    const x = document.createElement("button");
    x.type = "button";
    x.className = "row-x";
    x.title = title;
    x.setAttribute("aria-label", title);
    x.innerHTML = icon;
    x.onclick = (e) => {
      e.stopPropagation();
      fn();
    };
    return x;
  }

  function render() {
    if (!historyEl) return;
    historyEl.textContent = "";
    if (!chats.length) {
      const p = document.createElement("div");
      p.className = "side-empty";
      p.textContent = "No chats yet";
      historyEl.appendChild(p);
      return;
    }

    const sorted = [...chats].sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
    sorted.forEach((c) => {
      const title = c.title || "New chat";
      const b = document.createElement("div");
      b.className = "side-row" + (c.id === activeId ? " active" : "") + (c.pinned ? " pinned" : "");
      b.title = title;
      if (c.id === activeId) b.setAttribute("aria-current", "true");
      b.setAttribute("role", "button");
      b.tabIndex = 0;

      const lbl = document.createElement("span");
      lbl.className = "lbl";
      lbl.textContent = title;

      // A pinned row swaps the chat glyph for the pin in the same cell. Both are
      // 18px marks on the same axis, so the state reads without moving the title
      // by a pixel, which is what the "▪ " prefix inside the label used to do.
      b.append(svgIcon(c.pinned ? PIN_ICON : CHAT_ICON), lbl);

      const acts = document.createElement("span");
      acts.className = "row-acts";
      if (opts.onPin) acts.append(actionBtn(c.pinned ? "Unpin" : "Pin", PIN_ICON, () => opts.onPin(c.id, !c.pinned)));
      if (opts.onRename) {
        const rn = actionBtn("Rename", '<svg viewBox="0 0 24 24"><path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M13 7l4 4"/></svg>', () => {
          const cur = lbl.textContent;
          const v = prompt("Rename chat:", cur);
          if (v !== null) opts.onRename(c.id, v);
        });
        acts.append(rn);
      }
      if (opts.onDelete) acts.append(actionBtn("Delete", '<svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>', () => {
        if (confirm(`Delete "${title}"?`)) opts.onDelete(c.id);
      }));
      if (acts.childElementCount) b.append(acts);
      const go = () => {
        activeId = c.id;
        render();
        if (mobile()) close();
        if (opts.onSelect) opts.onSelect(c.id);
      };
      b.onclick = go;
      b.onkeydown = (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          go();
        }
      };
      historyEl.appendChild(b);
    });
  }

  function init(options = {}) {
    opts = options;
    side = $("side");
    scrim = $("sideScrim");
    historyEl = $("sideHistory");
    chats = options.chats || [];
    activeId = options.activeId ?? null;

    try {
      collapsed = localStorage.getItem(KEY) === "1";
    } catch (e) {
      collapsed = false;
    }

    const toggleBtn = $("sideToggle");
    if (toggleBtn) {
      toggleBtn.onclick = () => (mobile() ? close() : toggle());
    }

    const newBtn = $("sideNew");
    if (newBtn) {
      newBtn.onclick = () => {
        if (mobile()) close();
        if (opts.onNew) opts.onNew();
      };
    }

    if (scrim) {
      scrim.onclick = close;
    }

    window.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b") {
        e.preventDefault();
        toggle();
      } else if (e.key === "Escape" && mobile() && side && side.classList.contains("open")) {
        close();
      }
    });

    window.addEventListener("resize", () => {
      if (!mobile()) close();
    });

    applyCollapse();
    render();

    // Enable the rail transition only after the first paint, so a sidebar
    // restored in the collapsed state does not animate itself open on load.
    requestAnimationFrame(() => requestAnimationFrame(() => side?.classList.add("anim")));
  }

  const api = {
    init,
    toggle,
    open,
    close,
    setChats(list) {
      chats = list;
      render();
    },
    setActive(id) {
      activeId = id;
      render();
    },
    prepend(chat) {
      chats = [chat, ...chats];
      activeId = chat.id;
      render();
    },
  };

  window.ErisSidebar = api;
  return api;
})();
