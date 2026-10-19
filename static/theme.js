// Resolve the stored theme (Light / Dark / System) before first paint.
// Loaded synchronously in <head> of every page so data-theme on <html> is set
// with no flash of the wrong palette. Choice lives in localStorage because it
// must apply on the auth page too, before any account exists.
(function () {
  var mode = localStorage.getItem("eris-theme") || "system";
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
      localStorage.setItem("eris-theme", m);
      apply();
    },
  };
})();
