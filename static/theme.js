// Resolve the stored theme (Light / Dark / System) before first paint.
// Loaded synchronously in <head> of every page so data-theme on <html> is set
// with no flash of the wrong palette. Choice lives in localStorage because it
// must apply on the auth page too, before any account exists.
(function () {
  // A blocked or absent store throws rather than returning null, and this runs
  // before anything else does: an uncaught throw here leaves data-theme unset
  // and window.ErisTheme missing for the rest of the page's life.
  function stored() {
    try {
      return localStorage.getItem("eris-theme");
    } catch {
      return null;
    }
  }

  var mode = stored() || "system";
  var mq = matchMedia("(prefers-color-scheme: dark)");

  function resolve() {
    return mode === "system" ? (mq.matches ? "dark" : "light") : mode;
  }

  function apply() {
    document.documentElement.dataset.theme = resolve();
    // highlight.js stylesheet is picked per theme so code blocks match.
    var sheet = document.getElementById("hljs-theme");
    if (sheet) {
      sheet.href =
        "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/" +
        (resolve() === "dark" ? "github-dark.min.css" : "github.min.css");
    }
  }

  apply();
  mq.addEventListener("change", function () {
    if (mode === "system") apply();
  });

  window.ErisTheme = {
    get mode() {
      return mode;
    },
    set: function (m) {
      mode = m;
      try {
        localStorage.setItem("eris-theme", m);
      } catch {}
      apply();
    },
  };
})();
