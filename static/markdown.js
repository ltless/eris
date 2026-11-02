/* Rendering a reply: markdown to sanitized HTML, syntax highlighting, the copy
   button on a code block, and citation links on the finished text.

   Nothing here reads page state; each call is given the element it works on. */

const ErisMarkdown = (() => {
  function render(text) {
    return DOMPurify.sanitize(marked.parse(text));
  }

  function addCopy(codeEl) {
    const pre = codeEl.parentElement;
    if (!pre || pre.querySelector(".cp")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cp";
    b.title = "Copy code";
    b.textContent = "Copy";
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(codeEl.textContent);
        b.textContent = "Copied";
        setTimeout(() => {
          b.textContent = "Copy";
        }, 1500);
      } catch {
        b.textContent = "Failed";
      }
    });
    pre.append(b);
  }

  // highlightElement is called once per block: re-running it on a block that
  // already carries the marker would wrap the spans it just produced.
  function highlight(body) {
    body.querySelectorAll("pre code").forEach((el) => {
      if (!el.dataset.hl) {
        hljs.highlightElement(el);
        el.dataset.hl = "1";
        addCopy(el);
      }
    });
  }

  // "[3]" in the answer points at the third source of this turn. Text inside a
  // code block or an existing link is skipped, and a number the turn did not
  // produce is marked rather than linked, so it reads as a dangling reference.
  //
  // Every answer block, not just the first: a turn that searched between two
  // answers has one block per round, and the citations of a later round are in
  // a later block.
  function linkCitations(body, items) {
    const blocks = body.querySelectorAll(".ans");
    if (!blocks.length) return;
    const byN = new Map((items || []).filter((x) => x.n).map((x) => [x.n, x]));
    for (const ans of blocks) {
      const walker = document.createTreeWalker(ans, NodeFilter.SHOW_TEXT);
      const targets = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (node.parentElement.closest("pre, code, a")) continue;
        if (/\[\d{1,3}\]/.test(node.textContent)) targets.push(node);
      }
      for (const node of targets) {
        const frag = document.createDocumentFragment();
        let last = 0;
        for (const hit of node.textContent.matchAll(/\[(\d{1,3})\]/g)) {
          frag.append(document.createTextNode(node.textContent.slice(last, hit.index)));
          const a = document.createElement("a");
          a.className = "cite";
          a.textContent = `[${hit[1]}]`;
          const src = byN.get(parseInt(hit[1], 10));
          if (src && src.kind === "web" && /^https?:\/\//i.test(src.source)) {
            a.href = src.source;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.title = src.source;
          } else if (src) {
            a.title = src.source;
          } else {
            a.className = "cite cite-x";
            a.title = "source not in this answer";
          }
          frag.append(a);
          last = hit.index + hit[0].length;
        }
        frag.append(document.createTextNode(node.textContent.slice(last)));
        node.replaceWith(frag);
      }
    }
  }

  const URL_RE = /(?:https?:\/\/|www\.)[^\s<>"']+/gi;
  const count = (s, ch) => s.split(ch).length - 1;

  // Trailing punctuation belongs to the sentence around the link, not the link.
  function trimUrl(u) {
    let end = u.length;
    for (;;) {
      const c = u[end - 1];
      if (end && ".,;:!?".includes(c)) {
        end--;
        continue;
      }
      // A closing bracket the URL never opened is the sentence's.
      const open = c === ")" ? "(" : c === "]" ? "[" : null;
      if (open && count(u.slice(0, end), c) > count(u.slice(0, end), open)) {
        end--;
        continue;
      }
      break;
    }
    return u.slice(0, end);
  }

  // A URL someone typed is still a URL, so it gets the link colour and a click.
  // Text nodes only, never parsed as markdown: a sent question keeps the line
  // breaks and the asterisks it was typed with.
  function autolink(el) {
    const nodes = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.parentElement.closest("a")) continue;
      if (URL_RE.test(n.textContent)) nodes.push(n);
      URL_RE.lastIndex = 0;
    }
    for (const node of nodes) {
      const text = node.textContent;
      const frag = document.createDocumentFragment();
      let last = 0;
      for (const hit of text.matchAll(URL_RE)) {
        const url = trimUrl(hit[0]);
        if (!url) continue;
        frag.append(document.createTextNode(text.slice(last, hit.index)));
        const a = document.createElement("a");
        a.textContent = url;
        a.href = /^www\./i.test(url) ? "https://" + url : url;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        frag.append(a);
        last = hit.index + url.length;
      }
      frag.append(document.createTextNode(text.slice(last)));
      node.replaceWith(frag);
    }
  }

  return { render, highlight, linkCitations, autolink };
})();