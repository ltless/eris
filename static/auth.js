const $ = (id) => document.getElementById(id);
const tabLogin = $("tabLogin"), tabReg = $("tabReg");
const fLogin = $("fLogin"), fReg = $("fReg");

if (fetch("/api/auth/me").then((r) => { if (r.ok) location.href = "/"; })) {}

function tab(login) {
  tabLogin.classList.toggle("on", login);
  tabReg.classList.toggle("on", !login);
  tabLogin.setAttribute("aria-selected", String(login));
  tabReg.setAttribute("aria-selected", String(!login));
  tabLogin.tabIndex = login ? 0 : -1;
  tabReg.tabIndex = login ? -1 : 0;
  fLogin.hidden = !login;
  fReg.hidden = login;
}
tabLogin.onclick = () => tab(true);
tabReg.onclick = () => tab(false);
tab(true);

// One tab stop for the pair, arrows to move between them.
for (const el of [tabLogin, tabReg]) {
  el.addEventListener("keydown", (e) => {
    const next = { ArrowLeft: true, ArrowRight: false, Home: true, End: false }[e.key];
    if (next === undefined) return;
    e.preventDefault();
    tab(next);
    (next ? tabLogin : tabReg).focus();
  });
}

async function go(url, body, errEl) {
  errEl.hidden = true;
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    errEl.textContent = typeof j.detail === "string" ? j.detail : "Failed";
    errEl.hidden = false;
    return;
  }
  location.href = "/";
}

fLogin.onsubmit = (e) => {
  e.preventDefault();
  go("/api/auth/login", { email: $("liEmail").value, password: $("liPw").value }, $("liErr"));
};

fReg.onsubmit = (e) => {
  e.preventDefault();
  go(
    "/api/auth/register",
    { name: $("rgName").value, email: $("rgEmail").value, password: $("rgPw").value },
    $("rgErr")
  );
};
