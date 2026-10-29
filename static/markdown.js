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
  function linkCitations(body, items) {
    const ans = body.querySelector(".ans");
    if (!ans) return;
    const byN = new Map((items || []).filter((x) => x.n).map((x) => [x.n, x]));
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

  return { render, highlight, linkCitations };
})();