// Dressing web app — plain JS, no build step. Renders HTML strings and handles clicks via data-act attributes.

// Dutch colour names (as the AI gives them) to a swatch colour; anything unknown gets a neutral grey.
const COLORS = {
  zwart: "#1d1d1f", wit: "#fafafa", ecru: "#efe6d2", crème: "#f3ead6", creme: "#f3ead6", beige: "#d8c3a0", camel: "#c19a6b",
  zand: "#d6c4a1", taupe: "#a39283", bruin: "#7a5235", cognac: "#9a4e1c", grijs: "#8e8e93", lichtgrijs: "#c7c7cc",
  donkergrijs: "#48484a", antraciet: "#3a3a3c", marineblauw: "#1f2f57", navy: "#1f2f57", blauw: "#2f63b8", lichtblauw: "#9cc3e6",
  denim: "#4a6a94", jeansblauw: "#4a6a94", petrol: "#1f5f6b", turquoise: "#3fb6b0", groen: "#3b7d4a", olijf: "#6b6b2f",
  olijfgroen: "#6b6b2f", kaki: "#8f8a5c", khaki: "#8f8a5c", mint: "#a8dcc4", flessengroen: "#1f4a35", geel: "#e8c33a", oker: "#c9952c",
  mosterd: "#c99a2e", oranje: "#e07a2e", roest: "#a4492a", terracotta: "#c0613f", rood: "#c4302b", bordeaux: "#6d1a2c",
  wijnrood: "#6d1a2c", koraal: "#f07c6c", roze: "#eba3b6", lichtroze: "#f4cad3", oudroze: "#c99197", fuchsia: "#c2268c",
  paars: "#6c3b8f", lila: "#b9a2d6", lavendel: "#b9a2d6", zilver: "#c0c0c8", goud: "#c9a24a", multicolor: "conic-gradient(#c4302b, #e8c33a, #3b7d4a, #2f63b8, #c4302b)",
};
const SUGGESTIONS = [
  ["Gelegenheid", [["💼", "Een gewone werkdag op kantoor"], ["🤝", "Sollicitatiegesprek"], ["🍽️", "Etentje in een restaurant"],
    ["🥂", "Feestje vanavond"], ["💍", "Trouwfeest als gast"], ["❤️", "Date"], ["☕", "Terrasje met vrienden"], ["🛋️", "Luie zondag thuis"],
    ["🥾", "Wandeling in de natuur"], ["✈️", "Een dag reizen"], ["🏋️", "Sporten"]]],
  ["Weer", [["☀️", "Iets voor het weer van vandaag"], ["🌤️", "Iets voor morgen"], ["🌧️", "Een regenachtige dag"], ["🥵", "Een warme zomerdag"],
    ["🥶", "Koud en winderig"]]],
  ["Stijl", [["🔁", "Iets wat ik lang niet meer droeg"], ["🧸", "Zo comfortabel mogelijk"], ["✨", "Opvallend en gedurfd"],
    ["🎩", "Klassiek en verzorgd"], ["🎨", "Kleurrijk"], ["🖤", "Volledig in donkere tinten"]]],
];

let S = { items: [], worn: [], categories: [], statuses: [] };
const ui = {
  tab: "dressing",
  category: "", search: "", sort: "added", place: "",
  stylist: { query: "", loading: false, result: null, error: null, useWeather: true, step: "" },
  auth: null,
  weather: undefined,
};
let stack = []; // pushed pages: {kind: "detail"|"add"|"edit"|"outfit", ...}

// MARK: - Helpers

const $ = (sel) => document.querySelector(sel);
const h = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fold = (s) => String(s ?? "").normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const catLabel = (key) => S.categories.find((c) => c.key === key)?.label || "Overig";
const statusLabel = (key) => S.statuses.find((c) => c.key === key)?.label || "In de dressing";
const away = (i) => (i.status || "dressing") !== "dressing";
const itemById = (id) => S.items.find((i) => i.id === id);
const small = (url) => (url ? `${url}${url.includes("?") ? "&" : "?"}w=1` : url);
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const longDate = (day) => new Date(`${day}T12:00:00`).toLocaleDateString("nl-BE", { weekday: "long", day: "numeric", month: "long" });
const shortDate = (day) => new Date(`${day}T12:00:00`).toLocaleDateString("nl-BE", { day: "numeric", month: "short", year: "numeric" });
const swatchColor = (name) => COLORS[fold(name).replace(/\s+/g, "")] || COLORS[fold(name).split(/[\s-]/).pop()] || "#b8b0ba";
const swatches = (i) => (i.colors?.length ? `<span class="swatches" aria-hidden="true">${i.colors.slice(0, 3).map((c) => `<span class="swatch" style="--c:${swatchColor(c)}"></span>`).join("")}</span>` : "");
const listText = (v) => (Array.isArray(v) ? v.join(", ") : v || "");

/** How often and when a piece was worn, from the outfits in Gedragen. */
function wearOf(id) {
  const logs = S.worn.filter((l) => (l.item_ids || []).includes(id));
  return { count: logs.length, last: logs.map((l) => l.date).sort().at(-1) || null };
}
function daysAgo(day) {
  const n = Math.round((new Date(`${today()}T12:00:00`) - new Date(`${day}T12:00:00`)) / 864e5);
  return n === 0 ? "vandaag" : n === 1 ? "gisteren" : n < 31 ? `${n} dagen geleden` : shortDate(day);
}

async function api(path, { method = "GET", json, form } = {}) {
  // The server only accepts changes that carry this header, which another website can't add.
  const opts = { method, headers: method === "GET" ? {} : { "X-Dressing": "1" } };
  if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers["Content-Type"] = "application/json"; }
  if (form) opts.body = form;
  let res;
  try { res = await fetch(path, opts); }
  catch { const err = new Error("Geen verbinding met de server."); err.offline = true; throw err; }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !path.startsWith("/api/auth")) { signedOut(); throw new Error("Log opnieuw in."); }
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Fout ${res.status}`);
  return data;
}

/** An AI analysis: the server works on it in the background and the app asks every 1.5 seconds whether it is done.
    One request held open that long is broken off by Safari, and by a phone that sleeps or switches apps. */
async function slowApi(path, opts, onProgress) {
  const { job } = await api(`${path}${path.includes("?") ? "&" : "?"}background=1`, opts);
  const until = Date.now() + 10 * 60 * 1000;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1500));
    let r;
    try { r = await api(`/api/jobs/${job}`); }
    catch (e) { if (e.offline && Date.now() < until) continue; throw e; }
    if (r.done) return r.result;
    if (r.progress) onProgress?.(r.progress);
    if (Date.now() > until) throw new Error("Het duurde te lang. Probeer opnieuw.");
  }
}

/** A photo made smaller before it is sent, so a photo from the camera (3-5 MB) goes quickly over a phone's connection. */
async function smaller(file, side = 1600) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    const scale = Math.min(1, side / Math.max(bitmap.width, bitmap.height));
    if (scale === 1 && file.size < 1_500_000) return file;
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", 0.85));
    return blob && blob.size < file.size ? blob : file;
  } catch { return file; }
}

function signedOut() {
  ui.auth = { setup_needed: false, user: null };
  S = { items: [], worn: [], categories: [], statuses: [] };
  stack = [];
  ui.users = null; ui.models = null; ui.weather = undefined;
  ui.stylist = { query: "", loading: false, result: null, error: null, useWeather: true, step: "" };
  if ($("#sheet").open) closeSheet();
  render();
}

async function boot() {
  try { ui.auth = await api("/api/auth"); }
  catch { ui.auth = { setup_needed: false, user: null }; ui.authError = "Geen verbinding met de server."; return render(); }
  if (ui.auth.user) await load();
  else render();
}

async function load() {
  try {
    S = await api("/api/state");
    ui.auth.user = S.user;
  } catch (e) { toast(e.message); }
  render();
  if (S.place && ui.weather === undefined) loadWeather();
}

async function loadWeather() {
  ui.weather = null;
  try { ui.weather = await api("/api/weather"); } catch { ui.weather = false; }
  if (ui.tab === "stylist" && !stack.length) render();
}

function toast(msg, action = null) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const el = document.createElement("div");
  el.className = "toast"; el.setAttribute("role", "status");
  el.innerHTML = `${h(msg)}${action ? `<button type="button">${h(action.label)}</button>` : ""}`;
  if (action) el.querySelector("button").onclick = () => { el.remove(); action.run(); };
  document.body.append(el);
  setTimeout(() => el.remove(), action ? 6000 : 3500);
}

// MARK: - Icons

const icon = (paths) => `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
  stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const GEAR = Array.from({ length: 8 }, (_, i) => [[-17, 7.4], [-9, 9.6], [9, 9.6], [17, 7.4]].map(([da, r]) => {
  const a = ((i * 45 + da - 90) * Math.PI) / 180;
  return `${(12 + r * Math.cos(a)).toFixed(2)} ${(12 + r * Math.sin(a)).toFixed(2)}`;
}).join("L")).map((teeth, i) => `${i ? "L" : "M"}${teeth}`).join("") + "Z";
const ICONS = {
  hanger: `<path d="M12 7.5a2 2 0 1 1 2-2c0 1-1 1.4-2 2.2V9"/><path d="M12 9 3.2 15.6c-.8.6-.4 1.9.6 1.9h16.4c1 0 1.4-1.3.6-1.9z"/>`,
  sparkle: `<path d="M12 3.5c.5 3.6 1.9 5 5.5 5.5-3.6.5-5 1.9-5.5 5.5-.5-3.6-1.9-5-5.5-5.5 3.6-.5 5-1.9 5.5-5.5z"/><path d="M18.5 14.5c.3 1.8 1 2.5 2.8 2.8-1.8.3-2.5 1-2.8 2.8-.3-1.8-1-2.5-2.8-2.8 1.8-.3 2.5-1 2.8-2.8z"/>`,
  calendar: `<rect x="3.5" y="5" width="17" height="15.5" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/><path d="m9 15 2 2 4-4"/>`,
  gear: `<path d="${GEAR}"/><circle cx="12" cy="12" r="3"/>`,
  camera: `<path d="M4 8.5A2.5 2.5 0 0 1 6.5 6h1.7l1.3-2h5l1.3 2h1.7A2.5 2.5 0 0 1 20 8.5v8A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5z"/><circle cx="12" cy="12.5" r="3.5"/>`,
  pin: `<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/>`,
  shirt: `<path d="M8.5 3.5 4 6l1.8 4 2.2-1v11.5h8V9l2.2 1L20 6l-4.5-2.5c-.5 1.6-1.9 2.5-3.5 2.5s-3-.9-3.5-2.5z"/>`,
};
const ic = (name) => icon(ICONS[name]);
const TABS = [["dressing", ic("hanger"), "Dressing"], ["stylist", ic("sparkle"), "Wat aantrekken?"], ["worn", ic("calendar"), "Gedragen"], ["settings", ic("gear"), "Instellingen"]];

// MARK: - Dressing tab

function filteredItems() {
  const q = fold(ui.search).trim();
  let list = S.items.filter((i) => !ui.category || (ui.category === "away" ? away(i) : ui.category === "fav" ? i.favorite : i.category === ui.category));
  if (q) list = list.filter((i) => fold([i.name, i.kind, i.brand, i.material, i.pattern, i.location, listText(i.colors), listText(i.style_tags),
    listText(i.occasions), catLabel(i.category), i.description, i.notes].join(" ")).includes(q));
  const sorts = {
    added: (a, b) => b.date_added.localeCompare(a.date_added),
    name: (a, b) => (a.name || "").localeCompare(b.name || "", "nl"),
    worn: (a, b) => wearOf(b.id).count - wearOf(a.id).count,
    unworn: (a, b) => (wearOf(a.id).last || "").localeCompare(wearOf(b.id).last || ""),
  };
  return list.sort(sorts[ui.sort] || sorts.added);
}

function itemTile(i, { selectable = false, selected = false } = {}) {
  const sub = [i.kind || catLabel(i.category), i.location].filter(Boolean).join(" · ");
  return `<button class="tile-item${away(i) ? " away" : ""}" data-act="${selectable ? "toggle-piece" : "open"}" data-id="${h(i.id)}"
      ${selectable ? `aria-pressed="${selected}"` : ""}>
      ${i.photo ? `<img class="ph" src="${h(small(i.photo))}" alt="" loading="lazy">` : `<span class="ph none">${ic("shirt")}</span>`}
      ${away(i) ? `<span class="flag">${h(statusLabel(i.status))}</span>` : ""}
      ${selectable ? `<span class="check" aria-hidden="true">${selected ? "✓" : ""}</span>` : i.favorite ? `<span class="fav" aria-label="Favoriet">⭐</span>` : ""}
      <span class="txt"><span class="name">${h(i.name)}</span><span class="sub">${swatches(i)}${h(sub)}</span></span></button>`;
}

function categoryPills(items, current, act) {
  const count = (fn) => items.filter(fn).length;
  const pill = (key, label, n) => `<button data-act="${act}" data-value="${key}" aria-pressed="${current === key}">${h(label)}<span class="n">${n}</span></button>`;
  const used = S.categories.filter((c) => items.some((i) => i.category === c.key));
  return `<div class="pills" role="group" aria-label="Soort">${pill("", "Alles", items.length)}
    ${used.map((c) => pill(c.key, c.label, count((i) => i.category === c.key))).join("")}
    ${act === "category" && items.some((i) => i.favorite) ? pill("fav", "⭐ Favorieten", count((i) => i.favorite)) : ""}
    ${act === "category" && items.some(away) ? pill("away", "Niet in de dressing", count(away)) : ""}</div>`;
}

function renderDressing() {
  if (!S.items.length) return `<h1 class="large">Dressing</h1>
    <div class="empty"><div class="icon">${ic("hanger")}</div><h2>Je dressing is nog leeg</h2>
      <p>Neem een foto van een kledingstuk: de AI ziet wat het is, welke kleur en stof, en jij zegt waar het hangt.</p>
      <button class="btn-primary empty-action" data-act="add">${ic("camera")} Eerste kledingstuk toevoegen</button></div>`;
  const list = filteredItems();
  const here = S.items.filter((i) => !away(i)).length;
  return `<h1 class="large">Dressing</h1>
    <div class="summary"><b>${S.items.length}</b> ${S.items.length === 1 ? "stuk" : "stukken"}${here < S.items.length ? ` · ${here} in de dressing` : ""}</div>
    <label class="search"><span aria-hidden="true">🔍</span><input type="search" placeholder="Zoek op naam, kleur, merk, plaats…" value="${h(ui.search)}" data-bind-ui="search" aria-label="Zoeken"></label>
    ${categoryPills(S.items, ui.category, "category")}
    ${list.length ? `<div class="grid">${list.map((i) => itemTile(i)).join("")}</div>` : `<div class="empty"><p>Niets gevonden.</p></div>`}`;
}

function openSort() {
  const opts = { added: "Recent toegevoegd", name: "Naam", worn: "Meest gedragen", unworn: "Langst niet gedragen" };
  openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Sorteren</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
    <div class="card">${Object.entries(opts).map(([k, v]) => `<div class="row"><button class="row-button" data-act="set-sort" data-value="${k}">${h(v)}</button>${ui.sort === k ? "✓" : ""}</div>`).join("")}</div>`);
}

// MARK: - Detail page

function level(value, words) { return value ? `<span class="dots" aria-hidden="true">${"●".repeat(value)}${"○".repeat(5 - value)}</span> ${h(words[value - 1])}` : ""; }

function renderDetail(page) {
  const i = itemById(page.id);
  if (!i) return `<div class="topbar"><button class="btn-plain" data-act="back">‹ Terug</button></div><div class="empty"><p>Dit kledingstuk bestaat niet meer.</p></div>`;
  const wear = wearOf(i.id);
  const fact = (label, value) => (value ? `<div class="row"><span class="grow muted">${h(label)}</span><span>${value}</span></div>` : "");
  const chips = (list) => (list?.length ? `<div class="chips">${list.map((x) => `<span class="chip">${h(x)}</span>`).join("")}</div>` : "");
  return `<div class="topbar"><button class="btn-plain" data-act="back">‹ Terug</button><span class="title"></span>
      <button class="btn-pill" data-act="edit" data-id="${h(i.id)}">Bewerk</button></div>
    ${i.photo ? `<button class="hero-photo" data-act="view-photo" data-src="${h(i.photo)}" aria-label="Foto groot bekijken"><img src="${h(i.photo)}" alt="${h(i.name)}"></button>`
      : `<div class="hero-photo none">${ic("shirt")}</div>`}
    <div class="detail-head"><h1>${i.favorite ? "⭐ " : ""}${h(i.name)}</h1>
      <p class="sub">${swatches(i)}${h([catLabel(i.category), i.kind, i.brand].filter(Boolean).join(" · "))}</p></div>
    <div class="where">${ic("pin")}<div>${h(i.location || "Nog geen plaats")}<span>${h(statusLabel(i.status))}</span></div></div>
    <div class="actions-row">
      <button class="btn-primary" data-act="wear-one" data-id="${h(i.id)}">Vandaag gedragen</button>
      <button class="btn-secondary" data-act="status-sheet" data-id="${h(i.id)}">Waar is het?</button></div>
    <button class="btn-secondary" style="margin-top:10px" data-act="style-with" data-id="${h(i.id)}">${ic("sparkle")} Outfit rond dit stuk</button>
    ${i.description ? `<p style="margin:18px 4px 0;line-height:1.45">${h(i.description)}</p>` : ""}
    <h2 class="section-title">Kenmerken</h2>
    <div class="card">
      ${fact("Kleur", h(listText(i.colors)))}${fact("Patroon", h(i.pattern))}${fact("Materiaal", h(i.material))}
      ${fact("Pasvorm", h(i.fit))}${fact("Maat", h(i.size))}
      ${fact("Warmte", level(i.warmth, S.warmth || []))}${fact("Stijl", level(i.formality, S.formality || []))}
      ${fact("Seizoen", h(listText(i.seasons)))}${fact("Onderhoud", h(i.care))}
      ${fact("Gekocht", h([i.purchase_date, i.price != null ? `€ ${String(i.price).replace(".", ",")}` : ""].filter(Boolean).join(" · ")))}
      ${fact("Gedragen", wear.count ? `${plural(wear.count, "keer", "keer")}, laatst ${daysAgo(wear.last)}` : "nog nooit")}
    </div>
    ${i.occasions?.length ? `<h2 class="section-title">Gelegenheden</h2>${chips(i.occasions)}` : ""}
    ${i.style_tags?.length ? `<h2 class="section-title">Stijl</h2>${chips(i.style_tags)}` : ""}
    ${i.notes ? `<h2 class="section-title">Notitie</h2><div class="card pad">${h(i.notes)}</div>` : ""}
    <div class="card" style="margin-top:28px"><div class="row"><button class="row-button danger" data-act="delete-item" data-id="${h(i.id)}">Verwijderen uit de dressing</button></div></div>`;
}

function openStatusSheet(id) {
  const i = itemById(id);
  openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Waar is het?</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
    <div class="card">${S.statuses.map((s) => `<div class="row"><button class="row-button" data-act="set-status" data-id="${h(id)}" data-value="${s.key}">${h(s.label)}</button>${(i.status || "dressing") === s.key ? "✓" : ""}</div>`).join("")}</div>
    <h2 class="section-title">Plaats in de dressing</h2>
    <form class="card" data-act-submit="set-location" data-id="${h(id)}">
      <div class="field"><label for="loc">Waar het hangt of ligt</label><input id="loc" name="location" list="locations" value="${h(i.location)}" placeholder="bv. Linkerkast, plank 2"></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>${locationList()}`);
}

const locations = () => [...new Set(S.items.map((i) => (i.location || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "nl"));
const locationList = () => `<datalist id="locations">${locations().map((l) => `<option value="${h(l)}">`).join("")}</datalist>`;

// MARK: - Add and edit

const newDraft = () => ({ name: "", category: "top", colors: [], seasons: [], occasions: [], style_tags: [], status: "dressing", location: ui.lastLocation || "" });

function renderAdd(page) {
  const bar = `<div class="topbar"><button class="btn-plain" data-act="back">Annuleer</button><span class="title">Kledingstuk toevoegen</span><span class="spacer"></span></div>`;
  if (page.phase === "analyzing") return `${bar}<div class="analyzing">${page.preview ? `<img src="${h(page.preview)}" alt="">` : ""}
      <div class="spinner"></div><p><b>De AI bekijkt je kledingstuk…</b></p><p class="muted small">Soort, kleur, stof, seizoen en stijl. Meestal een tiental seconden.</p></div>`;
  if (page.phase === "review") return `${bar}${itemForm(page, "save-new")}`;
  return `${bar}
    ${!aiReady() ? `<div class="notice">Zonder API-sleutel kan de AI niets herkennen. Stel er een in bij Instellingen, of vul het kledingstuk zelf in.</div>` : ""}
    <label class="pick-photo" style="margin-top:12px">${ic("camera")}<b>Foto nemen of kiezen</b><span>Eén kledingstuk per foto, liefst op een hanger of plat gelegd bij goed licht.</span>
      <input class="file-input" type="file" accept="image/*" data-act-change="photo-chosen"></label>
    <div class="card" style="margin-top:12px"><div class="field"><label for="hint">Wat extra uitleg (mag leeg)</label>
      <input id="hint" data-bind-page="hint" value="${h(page.hint || "")}" placeholder="bv. merk Zara, maat M, gekregen van mama"></div></div>
    ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}
    <div class="buttons">
      ${page.hint ? `<button class="btn-secondary" data-act="identify-text" ${aiReady() ? "" : "disabled"}>Herkennen zonder foto</button>` : ""}
      <button class="btn-secondary" data-act="manual">Zelf invullen</button></div>`;
}

function renderEdit(page) {
  return `<div class="topbar"><button class="btn-plain" data-act="back">Annuleer</button><span class="title">Bewerken</span><span class="spacer"></span></div>${itemForm(page, "save-edit")}`;
}

function chipPicker(field, options, chosen) {
  return `<div class="chips">${options.map((o) => `<button type="button" class="chip${chosen.includes(o) ? " on" : ""}" data-act="toggle-chip" data-field="${field}" data-value="${h(o)}" aria-pressed="${chosen.includes(o)}">${h(o)}</button>`).join("")}</div>`;
}
function levelPicker(field, words, value) {
  return `<div class="level">${words.map((w, n) => `<button type="button" data-act="set-level" data-field="${field}" data-value="${n + 1}" aria-pressed="${value === n + 1}" title="${h(w)}">${n + 1}</button>`).join("")}</div>
    <span class="muted small" style="margin-top:4px">${value ? h(words[value - 1]) : "niet ingevuld"}</span>`;
}

function itemForm(page, submit) {
  const d = page.draft, item = page.id ? itemById(page.id) : null;
  const photo = page.preview || item?.photo;
  const input = (field, label, extra = "") => `<div class="field"><label for="f-${field}">${label}</label><input id="f-${field}" data-bind="${field}" value="${h(listText(d[field]))}" ${extra}></div>`;
  return `<form data-act-submit="${submit}">
    ${photo ? `<div class="hero-photo" style="cursor:default"><img src="${h(photo)}" alt=""></div>` : ""}
    ${submit === "save-edit" ? `<label class="btn-secondary" style="margin-top:10px">${ic("camera")} ${photo ? "Andere foto" : "Foto toevoegen"}<input class="file-input" type="file" accept="image/*" data-act-change="replace-photo"></label>` : ""}
    ${page.recognized ? `<div class="notice ok">Herkend door de AI. Kijk het even na en zeg waar het hangt.</div>` : ""}
    <h2 class="section-title">Waar hangt het?</h2>
    <div class="card">${input("location", "Plaats in de dressing", `list="locations" placeholder="bv. Linkerkast, plank 2" ${page.recognized ? "autofocus" : ""}`)}
      <div class="field"><label for="f-status">Waar is het nu</label><select id="f-status" data-bind="status">${S.statuses.map((s) => `<option value="${s.key}" ${d.status === s.key ? "selected" : ""}>${h(s.label)}</option>`).join("")}</select></div></div>
    ${locationList()}
    <h2 class="section-title">Wat is het?</h2>
    <div class="card">
      ${input("name", "Naam", 'required placeholder="bv. Marineblauwe wollen trui"')}
      <div class="field"><label for="f-category">Soort</label><select id="f-category" data-bind="category">${S.categories.map((c) => `<option value="${c.key}" ${d.category === c.key ? "selected" : ""}>${h(c.label)}</option>`).join("")}</select></div>
      ${input("kind", "Precies", 'placeholder="bv. chino, cardigan, enkellaars"')}
      ${input("colors", "Kleuren (komma ertussen)", 'placeholder="bv. marineblauw, wit"')}
      <div class="field split"><div><label for="f-pattern">Patroon</label><input id="f-pattern" data-bind="pattern" value="${h(d.pattern)}"></div>
        <div><label for="f-material">Materiaal</label><input id="f-material" data-bind="material" value="${h(d.material)}"></div></div>
      <div class="field split"><div><label for="f-brand">Merk</label><input id="f-brand" data-bind="brand" value="${h(d.brand)}"></div>
        <div><label for="f-size">Maat</label><input id="f-size" data-bind="size" value="${h(d.size)}"></div></div>
      ${input("fit", "Pasvorm", 'placeholder="slim, regular, oversized…"')}
    </div>
    <h2 class="section-title">Wanneer draag je het?</h2>
    <div class="card">
      <div class="field"><span class="label">Seizoenen</span>${chipPicker("seasons", S.seasons || [], d.seasons || [])}</div>
      <div class="field"><span class="label">Warmte (1 heel licht, 5 heel warm)</span>${levelPicker("warmth", S.warmth || [], d.warmth)}</div>
      <div class="field"><span class="label">Stijl (1 sportief, 5 feestelijk)</span>${levelPicker("formality", S.formality || [], d.formality)}</div>
      ${input("occasions", "Gelegenheden (komma ertussen)", 'placeholder="kantoor, weekend, etentje"')}
      ${input("style_tags", "Stijl in woorden", 'placeholder="klassiek, minimalistisch"')}
    </div>
    <h2 class="section-title">Meer</h2>
    <div class="card">
      <div class="field"><label for="f-description">Beschrijving</label><textarea id="f-description" data-bind="description" rows="2">${h(d.description)}</textarea></div>
      ${input("care", "Onderhoud")}
      <div class="field split"><div><label for="f-purchase">Gekocht op</label><input id="f-purchase" type="date" data-bind="purchase_date" value="${h(d.purchase_date)}"></div>
        <div><label for="f-price">Prijs (€)</label><input id="f-price" inputmode="decimal" data-bind="price" value="${h(d.price ?? "")}"></div></div>
      <div class="field"><label for="f-notes">Notitie</label><textarea id="f-notes" data-bind="notes" rows="2">${h(d.notes)}</textarea></div>
      <label class="row"><span class="grow">Favoriet</span><input type="checkbox" switch data-bind="favorite" ${d.favorite ? "checked" : ""}></label>
    </div>
    ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}
    <div class="buttons"><button class="btn-primary" ${page.saving ? "disabled" : ""}>${page.saving ? `<span class="spinner sm"></span>` : ""}Bewaren</button></div>
  </form>`;
}

/** The form's fields as the server wants them: lists from "a, b" text, numbers from text. */
function draftOut(d) {
  const list = (v) => (Array.isArray(v) ? v : String(v || "").split(",")).map((x) => x.trim()).filter(Boolean);
  const price = String(d.price ?? "").replace(",", ".").trim();
  return { ...d, colors: list(d.colors), occasions: list(d.occasions), style_tags: list(d.style_tags), price: price ? Number(price) : null };
}

// MARK: - Stylist tab

function weatherLine() {
  if (!S.place) return `<div class="weather-strip">🌍 <span>Stel je woonplaats in bij Instellingen, dan houdt de stylist rekening met het weer.</span></div>`;
  const w = ui.weather;
  const now = w?.current, d = w?.days?.[0];
  const text = w === null ? "Weer ophalen…" : w === false ? "Weer niet beschikbaar" :
    now ? `<b>${now.temp}°</b> ${h(now.summary)} in ${h(S.place.name.split(",")[0])}${d ? ` · ${d.min}–${d.max}°, ${d.rain}% regen` : ""}` : "";
  return `<div class="weather-strip"><span>🌦️ ${text}</span>
    <label><input type="checkbox" data-act-change="use-weather" ${ui.stylist.useWeather ? "checked" : ""}> Weer meenemen</label></div>`;
}

function renderStylist() {
  const st = ui.stylist;
  const top = `<h1 class="large">Wat aantrekken?</h1>
    <form class="ask" data-act-submit="ask"><textarea name="query" rows="1" placeholder="Gelegenheid, weer, stemming… bv. 'etentje bij schoonouders, het regent'" aria-label="Je vraag" data-bind-stylist="query">${h(st.query)}</textarea>
      <button class="btn-pill icon primary" aria-label="Vraag een outfit" ${st.loading ? "disabled" : ""}>${st.loading ? `<span class="spinner sm"></span>` : "↑"}</button></form>
    ${weatherLine()}`;
  if (!S.items.length) return `${top}<div class="empty"><p>Voeg eerst kleren toe aan je dressing; de stylist kiest enkel uit wat je hebt.</p></div>`;
  if (!aiReady()) return `${top}<div class="notice">Stel een API-sleutel in bij Instellingen om outfits te laten voorstellen.</div>`;
  if (st.loading) return `${top}<div class="analyzing"><div class="spinner"></div><p><b>${h(st.step || "Je stylist denkt na…")}</b></p><p class="muted small">Kijkt naar je dressing${st.useWeather && S.place ? ", het weer" : ""} en wat je laatst droeg.</p></div>`;
  if (st.error) return `${top}<div class="notice err">${h(st.error)}</div>${suggestions()}`;
  if (st.result) return `${top}${outfitsHTML(st.result)}<div class="buttons"><button class="btn-secondary" data-act="stylist-reset">Nieuwe vraag</button></div>`;
  return `${top}${suggestions()}`;
}

function suggestions() {
  return SUGGESTIONS.map(([title, list]) => `<div class="sugg-group"><h3>${h(title)}</h3><div class="sugg">
    ${list.map(([emoji, text]) => `<button data-act="suggest" data-q="${h(text)}">${emoji} ${h(text)}</button>`).join("")}</div></div>`).join("");
}

function outfitsHTML(r) {
  const piece = (id) => {
    const i = itemById(id);
    if (!i) return "";
    return `<button class="piece" data-act="open" data-id="${h(id)}">${i.photo ? `<img src="${h(small(i.photo))}" alt="">` : `<span class="ph">${ic("shirt")}</span>`}
      <span>${h(i.name)}</span><small>${ic("pin").replace('class="ic"', 'class="ic" style="width:11px;height:11px;vertical-align:-1px"')} ${h(i.location || "geen plaats")}</small></button>`;
  };
  return `${r.advice ? `<p class="advice">${h(r.advice)}</p>` : ""}
    ${r.outfits.map((o, n) => `<article class="card outfit">
      <div class="head"><span class="n">Outfit ${n + 1}</span><h2>${h(o.title)}</h2></div>
      <div class="pieces">${o.item_ids.map(piece).join("")}</div>
      <p class="why">${h(o.reason)}</p>${o.tip ? `<p class="tip">💡 ${h(o.tip)}</p>` : ""}
      <div class="foot"><button class="btn-primary" data-act="wear-outfit" data-n="${n}">Dit draag ik</button></div></article>`).join("")}
    ${!r.outfits.length ? `<div class="notice">Geen outfit gevonden die past.</div>` : ""}
    ${r.missing ? `<div class="notice">🛍️ ${h(r.missing)}</div>` : ""}`;
}

async function askStylist(query) {
  const st = ui.stylist;
  if (!query.trim() || st.loading) return;
  Object.assign(st, { query, loading: true, error: null, result: null, step: "" });
  render();
  try {
    st.result = await slowApi("/api/stylist", { method: "POST", json: { query, use_weather: st.useWeather } },
      (p) => { if (p.step && p.step !== st.step) { st.step = p.step; if (ui.tab === "stylist" && !stack.length) render(); } });
    if (st.result.weather) ui.weather = st.result.weather;
  } catch (e) { st.error = e.message; }
  st.loading = false;
  render();
}

// MARK: - Worn tab and the outfit page

function renderWorn() {
  if (!S.worn.length) return `<h1 class="large">Gedragen</h1><div class="empty"><div class="icon">${ic("calendar")}</div><h2>Nog niets gedragen</h2>
    <p>Zeg wat je aantrok: zo weet de stylist wat je graag draagt en wat al te lang in de kast hangt.</p>
    <button class="btn-primary empty-action" data-act="new-outfit">Outfit van vandaag</button></div>`;
  const byMonth = {};
  for (const l of S.worn) (byMonth[l.date.slice(0, 7)] ||= []).push(l);
  const never = S.items.filter((i) => !away(i) && !wearOf(i.id).count).length;
  return `<h1 class="large">Gedragen</h1>
    ${never ? `<p class="subtitle">${plural(never, "stuk hangt", "stukken hangen")} er nog ongedragen bij.</p>` : ""}
    ${Object.entries(byMonth).map(([month, logs]) => `<h2 class="section-title">${h(new Date(`${month}-15T12:00:00`).toLocaleDateString("nl-BE", { month: "long", year: "numeric" }))}</h2>
      <div class="card">${logs.map(logRow).join("")}</div>`).join("")}`;
}

function logRow(l) {
  const items = (l.item_ids || []).map(itemById).filter(Boolean);
  const names = items.map((i) => i.name).join(" + ") || "Verwijderde kleren";
  return `<button class="log" data-act="open-log" data-id="${h(l.id)}"><span class="stack">${items.slice(0, 4).map((i) => (i.photo ? `<img src="${h(small(i.photo))}" alt="">` : `<span class="ph"></span>`)).join("")}</span>
    <span class="grow" style="min-width:0"><span class="name">${h(longDate(l.date))}</span>
      <span class="sub" style="display:block">${l.rating ? `<span class="stars">${"★".repeat(l.rating)}</span> ` : ""}${h([l.occasion, names].filter(Boolean).join(" · "))}</span></span></button>`;
}

function renderOutfit(page) {
  const d = page.draft;
  const chosen = new Set(d.item_ids);
  const pool = S.items.filter((i) => !page.filter || i.category === page.filter);
  return `<div class="topbar"><button class="btn-plain" data-act="back">Annuleer</button><span class="title">${page.logId ? "Outfit bewerken" : "Gedragen"}</span>
      <button class="btn-pill strong primary" data-act="save-outfit" ${chosen.size && !page.saving ? "" : "disabled"}>Bewaar</button></div>
    <div class="card">
      <div class="field"><label for="o-date">Datum</label><input id="o-date" type="date" data-bind="date" value="${h(d.date)}" max="${today()}"></div>
      <div class="field"><label for="o-occ">Gelegenheid</label><input id="o-occ" data-bind="occasion" value="${h(d.occasion)}" placeholder="bv. kantoor, verjaardag"></div>
      <div class="field"><span class="label">Hoe voelde het?</span><div class="star-pick">${[1, 2, 3, 4, 5].map((n) => `<button type="button" data-act="rate" data-value="${n}" aria-pressed="${(d.rating || 0) >= n}" aria-label="${n} op 5">★</button>`).join("")}</div></div>
      <div class="field"><label for="o-note">Notitie</label><input id="o-note" data-bind="note" value="${h(d.note)}" placeholder="bv. te warm, veel complimentjes"></div></div>
    <h2 class="section-title">${chosen.size ? plural(chosen.size, "stuk", "stukken") + " gekozen" : "Kies wat je droeg"}</h2>
    ${categoryPills(S.items, page.filter || "", "outfit-filter")}
    <div class="grid">${pool.map((i) => itemTile(i, { selectable: true, selected: chosen.has(i.id) })).join("")}</div>
    ${page.logId ? `<div class="card" style="margin-top:28px"><div class="row"><button class="row-button danger" data-act="delete-log">Outfit verwijderen</button></div></div>` : ""}`;
}

function openOutfit(ids = [], extra = {}) {
  push({ kind: "outfit", draft: { item_ids: [...ids], date: today(), occasion: "", note: "", rating: null, ...extra } });
}

// MARK: - Sign-in

function authHero(title, tagline = "") {
  return `<div class="auth-hero"><img src="/icon-192.png" alt="" width="76" height="76" class="auth-icon">
      <h1>${h(title)}</h1>${tagline ? `<p>${h(tagline)}</p>` : ""}
      <svg class="auth-rail" viewBox="0 0 400 48" preserveAspectRatio="none" aria-hidden="true">
        <path class="shelf" d="M0 48 L0 30 L400 30 L400 48 Z"/><path class="bar" d="M24 14 L376 14"/></svg></div>`;
}

function renderAuth() {
  if (!ui.auth) return `<div class="empty"><div class="spinner"></div></div>`;
  const setup = ui.auth.setup_needed;
  return `<div class="auth">${authHero(setup ? "Welkom" : "Dressing", setup ? "" : "Je kleerkast, altijd bij de hand")}
      ${setup ? `<p class="muted">Maak het beheerdersaccount aan. Andere accounts maak je daarna aan bij Instellingen.</p>` : ""}
      <form class="card" data-act-submit="${setup ? "setup" : "login"}">
        <div class="field"><label for="a-user">Gebruikersnaam</label><input id="a-user" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required></div>
        <div class="field"><label for="a-pass">Wachtwoord</label><input id="a-pass" name="password" type="password" autocomplete="${setup ? "new-password" : "current-password"}" required></div>
        ${setup ? `<div class="field"><label for="a-pass2">Herhaal wachtwoord</label><input id="a-pass2" name="password2" type="password" autocomplete="new-password" required></div>` : ""}
        <div class="auth-submit"><button class="btn-primary" ${ui.authBusy ? "disabled" : ""}>${ui.authBusy ? `<span class="spinner sm"></span>` : ""}${setup ? "Beheerder aanmaken" : "Inloggen"}</button></div></form>
      ${ui.authError ? `<div class="notice err">${h(ui.authError)}</div>` : ""}
      <p class="footnote">${setup ? "Minstens 8 tekens." : "Wachtwoord vergeten? Vraag het aan de beheerder."}</p></div>`;
}

// MARK: - Settings tab

const aiReady = () => ["app", "env"].includes(S.api_key?.source);

function renderSettings() {
  const user = ui.auth.user, key = S.api_key || {};
  const keyText = key.source === "app" ? `Ingesteld (${h(key.hint)})` : key.source === "env" ? "Uit de serverinstellingen (.env)" : key.source === "locked" ? "Vergrendeld: log opnieuw in" : "Niet ingesteld";
  return `<h1 class="large">Instellingen</h1>
    <h2 class="section-title">Over jou</h2>
    <form class="card" data-act-submit="save-profile">
      <div class="field"><label for="s-profile">Je stijl en wat de stylist moet weten</label>
        <textarea id="s-profile" name="profile" rows="3" placeholder="bv. man, 38, werk in een creatief bureau; hou van smart casual in aardetinten; geen skinny jeans">${h(S.profile)}</textarea></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
    <p class="footnote">Gaat mee naar de stylist bij elke vraag.</p>
    <h2 class="section-title">Weer</h2>
    <form class="card" data-act-submit="save-place">
      <div class="field"><label for="s-place">Woonplaats</label><input id="s-place" name="place" value="${h(S.place?.name || "")}" placeholder="bv. Kortrijk"></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
    <p class="footnote">Het weer komt van Open-Meteo, zonder sleutel of account.</p>
    <h2 class="section-title">AI (Claude van Anthropic)</h2>
    <div class="card">
      <div class="row"><span class="grow">API-sleutel</span><span class="v">${keyText}</span></div>
      <form data-act-submit="save-key"><div class="field"><label for="s-key">${key.source === "app" ? "Andere sleutel" : "Sleutel"}</label>
        <input id="s-key" name="key" type="password" autocomplete="off" placeholder="sk-ant-…" spellcheck="false"></div>
        <div class="auth-submit"><button class="btn-primary" ${ui.keyBusy ? "disabled" : ""}>${ui.keyBusy ? `<span class="spinner sm"></span>` : ""}Sleutel controleren en bewaren</button></div></form>
      ${key.source === "app" ? `<div class="row"><button class="row-button danger" data-act="clear-key">Sleutel verwijderen</button></div>` : ""}
      <div class="row"><span class="grow">Model</span><span class="v">${h(S.model)}</span></div>
      ${aiReady() ? `<div class="row"><button class="row-button" data-act="pick-model">Ander model kiezen</button></div>` : ""}
    </div>
    <p class="footnote">Maak een sleutel op console.anthropic.com. Hij wordt versleuteld met je wachtwoord bewaard: niemand anders kan hem lezen.
      Een foto herkennen of een outfit vragen kost meestal minder dan een cent.</p>
    <h2 class="section-title">Account</h2>
    <div class="card">
      <div class="row"><span class="grow">Ingelogd als</span><span class="v">${h(user.username)}${user.is_admin ? " (beheerder)" : ""}</span></div>
      <div class="row"><button class="row-button" data-act="password-sheet">Wachtwoord wijzigen</button></div>
      <div class="row"><button class="row-button" data-act="audit">Aanmeldingen bekijken</button></div>
      <div class="row"><button class="row-button danger" data-act="logout">Afmelden</button></div></div>
    ${user.is_admin ? usersSection() : ""}
    <p class="footnote" style="margin-top:24px;text-align:center">Dressing${S.version ? ` · versie ${h(S.version)}` : ""}</p>`;
}

function usersSection() {
  if (ui.users === undefined || ui.users === null) { if (ui.users === undefined) loadUsers(); return `<h2 class="section-title">Accounts</h2><div class="card pad muted">Laden…</div>`; }
  return `<h2 class="section-title">Accounts</h2><div class="card">
    ${ui.users.map((u) => `<div class="row"><span class="grow">${h(u.username)}${u.is_admin ? " (beheerder)" : ""}<span class="muted small" style="display:block">${plural(u.items, "kledingstuk", "kledingstukken")}</span></span>
      ${u.id === ui.auth.user.id ? "" : `<button class="btn-plain small" data-act="reset-user" data-id="${h(u.id)}" data-name="${h(u.username)}">Reset</button>
      <button class="btn-plain small" style="color:var(--red)" data-act="delete-user" data-id="${h(u.id)}" data-name="${h(u.username)}">Verwijder</button>`}</div>`).join("")}
    <div class="row"><button class="row-button" data-act="add-user">Account toevoegen</button></div></div>`;
}

async function loadUsers() {
  ui.users = null;
  try { ui.users = await api("/api/users"); } catch (e) { ui.users = []; toast(e.message); }
  if (ui.tab === "settings") render();
}

function passwordSheet({ title, submit, id = "", current = false, username = false }) {
  openSheet(`<div class="topbar"><button class="btn-plain" data-act="close-sheet">Annuleer</button><span class="title" id="sheet-title">${h(title)}</span><span class="spacer"></span></div>
    <form class="card" data-act-submit="${submit}" data-id="${h(id)}">
      ${username ? `<div class="field"><label for="p-user">Gebruikersnaam</label><input id="p-user" name="username" autocapitalize="off" required></div>` : ""}
      ${current ? `<div class="field"><label for="p-cur">Huidig wachtwoord</label><input id="p-cur" name="current" type="password" autocomplete="current-password" required></div>` : ""}
      <div class="field"><label for="p-new">${username ? "Wachtwoord" : "Nieuw wachtwoord"}</label><input id="p-new" name="password" type="password" autocomplete="new-password" minlength="8" required></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
    <p class="footnote">Minstens 8 tekens.${id ? " Na een reset moet die persoon de API-sleutel opnieuw invullen." : ""}</p>`);
}

// MARK: - Rendering

function navBar() {
  const title = { dressing: "Dressing", stylist: "Wat aantrekken?", worn: "Gedragen", settings: "Instellingen" }[ui.tab];
  if (ui.tab === "dressing" && S.items.length) return `<header class="navbar">
      <span><button class="btn-pill icon" data-act="sort" aria-label="Sorteren">☰</button></span>
      <span class="nav-title" aria-hidden="true">${h(title)}</span>
      <span class="end"><button class="btn-pill icon primary" data-act="add" aria-label="Kledingstuk toevoegen">＋</button></span></header>`;
  if (ui.tab === "worn" && S.worn.length) return `<header class="navbar"><span></span><span class="nav-title" aria-hidden="true">${h(title)}</span>
      <span class="end"><button class="btn-pill icon primary" data-act="new-outfit" aria-label="Outfit toevoegen">＋</button></span></header>`;
  return `<header class="navbar bare"><span></span><span class="nav-title" aria-hidden="true">${h(title)}</span></header>`;
}

function updateBars(scroller = null) {
  if (!scroller) {
    const nav = $("#app .navbar"), title = $("#app h1.large"), root = document.documentElement;
    root.classList.toggle("nav-under", !!nav && scrollY > 4);
    root.classList.toggle("nav-titled", !!title && scrollY > title.offsetTop + title.offsetHeight - (nav?.offsetHeight || 0));
    return;
  }
  scroller.classList.toggle("scrolled", scroller.scrollTop > 4);
}

function render() {
  $("#tabbar").hidden = !ui.auth?.user;
  if (!ui.auth?.user) {
    $("#app").innerHTML = `<div class="screen">${renderAuth()}</div>`;
    $("#pages").innerHTML = "";
    document.body.style.overflow = "";
    document.title = ui.auth?.setup_needed ? "Beheerder aanmaken | Dressing" : "Inloggen | Dressing";
    return;
  }
  const screens = { dressing: renderDressing, stylist: renderStylist, worn: renderWorn, settings: renderSettings };
  const focus = document.activeElement?.dataset?.bindUi || document.activeElement?.dataset?.bindStylist; // keep typing in the search box
  $("#app").innerHTML = `<div class="screen with-nav">${navBar()}${screens[ui.tab]()}</div>`;
  if (focus) { const el = $(`[data-bind-ui="${focus}"], [data-bind-stylist="${focus}"]`); el?.focus(); el?.setSelectionRange?.(el.value.length, el.value.length); }
  $("#tabbar").innerHTML = TABS.map(([id, svg, label]) => `<button data-act="tab" data-tab="${id}" ${ui.tab === id ? 'aria-current="page"' : ""}>${svg}${label}</button>`).join("");
  updateBars();
  const page = stack.at(-1), pages = $("#pages");
  document.title = `${page ? pageTitle(page) : TABS.find(([id]) => id === ui.tab)[2]} | Dressing`;
  if (!page) { pages.innerHTML = ""; document.body.style.overflow = ""; return; }
  const same = pages.firstElementChild?.dataset.key === String(page.key);
  const scroll = same ? pages.firstElementChild.scrollTop : 0;
  const views = { detail: renderDetail, add: renderAdd, edit: renderEdit, outfit: renderOutfit };
  pages.innerHTML = `<div class="page${same ? "" : " enter"}" data-key="${page.key}"><div class="inner">${views[page.kind](page)}</div></div>`;
  pages.firstElementChild.scrollTop = scroll;
  pages.firstElementChild.addEventListener("scroll", (e) => updateBars(e.currentTarget), { passive: true });
  updateBars(pages.firstElementChild);
  document.body.style.overflow = "hidden";
}

function pageTitle(page) {
  if (page.kind === "detail") return itemById(page.id)?.name || "Kledingstuk";
  return { add: "Toevoegen", edit: "Bewerken", outfit: "Gedragen" }[page.kind];
}

let pageSeq = 0;
function push(page) { stack.push({ key: ++pageSeq, ...page }); render(); }
function pop() { stack.pop(); render(); }

function openSheet(html) {
  const sheet = $("#sheet");
  sheet.innerHTML = `<div class="inner">${html}</div>`;
  if (!sheet.open) { sheet.showModal(); sheet.focus({ preventScroll: true }); sheet.scrollTop = 0; }
}
function closeSheet() { $("#sheet").close(); }

function viewPhoto(src) {
  const viewer = $("#viewer");
  viewer.innerHTML = `<img src="${h(src)}" alt="Foto"><button class="close" data-act="close-viewer" aria-label="Sluiten">✕</button>`;
  viewer.showModal();
}

async function mutate(fn) {
  try { const result = await fn(); await load(); return result ?? true; }
  catch (e) { toast(e.message); return null; }
}

// MARK: - Actions

const actions = {
  tab: (el) => { ui.tab = el.dataset.tab; stack = []; render(); scrollTo(0, 0); if (ui.tab === "stylist" && S.place && !ui.weather) loadWeather(); },
  open: (el) => push({ kind: "detail", id: el.dataset.id }),
  back: () => pop(),
  add: () => push({ kind: "add", phase: "pick", hint: "" }),
  sort: () => openSort(),
  "set-sort": (el) => { ui.sort = el.dataset.value; closeSheet(); render(); },
  category: (el) => { ui.category = el.dataset.value; render(); },
  "close-sheet": () => closeSheet(),
  "view-photo": (el) => viewPhoto(el.dataset.src),
  "close-viewer": () => $("#viewer").close(),
  manual: () => { const p = stack.at(-1); Object.assign(p, { phase: "review", draft: newDraft(), error: null }); render(); },
  async "identify-text"() { await identify(stack.at(-1), null); },
  edit: (el) => { const i = itemById(el.dataset.id); push({ kind: "edit", id: i.id, draft: structuredClone(i) }); },
  "toggle-chip": (el) => {
    const d = stack.at(-1).draft, f = el.dataset.field, list = d[f] || [];
    d[f] = list.includes(el.dataset.value) ? list.filter((x) => x !== el.dataset.value) : [...list, el.dataset.value];
    render();
  },
  "set-level": (el) => { const d = stack.at(-1).draft, v = Number(el.dataset.value); d[el.dataset.field] = d[el.dataset.field] === v ? null : v; render(); },
  async "delete-item"(el) {
    const i = itemById(el.dataset.id);
    if (!confirm(`‘${i.name}’ verwijderen uit je dressing? De foto gaat mee.`)) return;
    if (await mutate(() => api(`/api/items/${i.id}`, { method: "DELETE" }))) { stack = []; render(); toast("Verwijderd"); }
  },
  "status-sheet": (el) => openStatusSheet(el.dataset.id),
  async "set-status"(el) {
    if (await mutate(() => api(`/api/items/${el.dataset.id}`, { method: "PATCH", json: { status: el.dataset.value } }))) openStatusSheet(el.dataset.id);
  },
  "wear-one": (el) => openOutfit([el.dataset.id]),
  "style-with": (el) => {
    const i = itemById(el.dataset.id);
    stack = []; ui.tab = "stylist";
    askStylist(`Een outfit rond mijn ${i.name}${ui.weather ? " voor het weer van vandaag" : ""}`);
  },
  "new-outfit": () => openOutfit(),
  "wear-outfit": (el) => {
    const o = ui.stylist.result.outfits[Number(el.dataset.n)];
    openOutfit(o.item_ids, { occasion: ui.stylist.query.slice(0, 80) });
  },
  "toggle-piece": (el) => {
    const d = stack.at(-1).draft, id = el.dataset.id;
    d.item_ids = d.item_ids.includes(id) ? d.item_ids.filter((x) => x !== id) : [...d.item_ids, id];
    render();
  },
  "outfit-filter": (el) => { stack.at(-1).filter = el.dataset.value; render(); },
  rate: (el) => { const d = stack.at(-1).draft, v = Number(el.dataset.value); d.rating = d.rating === v ? null : v; render(); },
  async "save-outfit"() {
    const page = stack.at(-1);
    if (page.saving) return;
    page.saving = true;
    const ok = await mutate(() => api(page.logId ? `/api/worn/${page.logId}` : "/api/worn", { method: page.logId ? "PUT" : "POST", json: page.draft }));
    page.saving = false;
    if (ok) { stack = []; if (!page.logId) ui.tab = "worn"; render(); toast(page.logId ? "Bewaard" : "Genoteerd in Gedragen"); }
  },
  "open-log": (el) => {
    const l = S.worn.find((x) => x.id === el.dataset.id);
    push({ kind: "outfit", logId: l.id, draft: { item_ids: [...l.item_ids], date: l.date, occasion: l.occasion || "", note: l.note || "", rating: l.rating || null } });
  },
  async "delete-log"() {
    const page = stack.at(-1);
    if (!confirm("Deze outfit verwijderen uit Gedragen?")) return;
    if (await mutate(() => api(`/api/worn/${page.logId}`, { method: "DELETE" }))) { stack = []; render(); }
  },
  suggest: (el) => askStylist(el.dataset.q),
  "stylist-reset": () => { Object.assign(ui.stylist, { query: "", result: null, error: null }); render(); },
  async logout() { await api("/api/auth/logout", { method: "POST" }).catch(() => {}); signedOut(); },
  "password-sheet": () => passwordSheet({ title: "Wachtwoord wijzigen", submit: "change-password", current: true }),
  "add-user": () => passwordSheet({ title: "Account toevoegen", submit: "create-user", username: true }),
  "reset-user": (el) => passwordSheet({ title: `Wachtwoord van ${el.dataset.name}`, submit: "reset-password", id: el.dataset.id }),
  async "delete-user"(el) {
    if (!confirm(`Het account ‘${el.dataset.name}’ met al zijn kleren en outfits verwijderen? Dit kan niet ongedaan gemaakt worden.`)) return;
    try { await api(`/api/users/${el.dataset.id}`, { method: "DELETE" }); toast("Account verwijderd"); } catch (e) { toast(e.message); }
    loadUsers();
  },
  async "clear-key"() {
    if (!confirm("Je API-sleutel verwijderen?")) return;
    await mutate(() => api("/api/settings/api-key", { method: "DELETE" }));
  },
  async "pick-model"() {
    let models;
    try { models = await api("/api/settings/models"); } catch (e) { return toast(e.message); }
    openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Model</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
      <div class="card"><div class="row"><button class="row-button" data-act="set-model" data-value="">Standaard (${h(S.default_model)})</button></div>
      ${models.map((m) => `<div class="row"><button class="row-button" data-act="set-model" data-value="${h(m.id)}">${h(m.name)}<span class="muted small" style="display:block">${h(m.id)}</span></button>${S.model === m.id ? "✓" : ""}</div>`).join("")}</div>
      <p class="footnote">Enkel modellen die foto's kunnen lezen. Een groter model kiest wat fijner, maar kost meer.</p>`);
  },
  async "set-model"(el) { if (await mutate(() => api("/api/settings/model", { method: "PUT", json: { model: el.dataset.value || null } }))) closeSheet(); },
  async audit() {
    let list;
    try { list = await api("/api/audit"); } catch (e) { return toast(e.message); }
    openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Aanmeldingen</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
      <div class="card">${list.slice(0, 80).map((a) => `<div class="row"><span class="grow">${h(a.label)}<span class="muted small" style="display:block">${h(a.username)} · ${h(a.device)} · ${h(a.ip)}</span></span>
        <span class="v small">${h(new Date(a.at).toLocaleString("nl-BE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}</span></div>`).join("") || `<div class="row muted">Nog niets.</div>`}</div>`);
  },
};

async function identify(page, file) {
  Object.assign(page, { phase: "analyzing", error: null, preview: file ? URL.createObjectURL(file) : null });
  render();
  const form = new FormData();
  if (file) form.append("photo", await smaller(file), "foto.jpg");
  form.append("hint", page.hint || "");
  try {
    const r = await slowApi("/api/identify", { method: "POST", form });
    Object.assign(page, { phase: "review", draft: { ...newDraft(), ...r.info }, photoToken: r.photo_token, recognized: true });
  } catch (e) {
    // The photo goes along with a piece filled in by hand: keep it as preview only (it is uploaded after saving).
    Object.assign(page, { phase: file ? "review" : "pick", draft: newDraft(), error: e.message, file });
  }
  if (stack.includes(page)) render();
}

const submits = {
  async login(form) { await authSubmit("/api/auth/login", form); },
  async setup(form) {
    if (form.password.value !== form.password2.value) { ui.authError = "De wachtwoorden zijn niet gelijk."; return render(); }
    await authSubmit("/api/auth/setup", form);
  },
  ask(form) { askStylist(form.query.value); },
  async "save-new"() { await saveItem(stack.at(-1), true); },
  async "save-edit"() { await saveItem(stack.at(-1), false); },
  async "set-location"(form) {
    if (await mutate(() => api(`/api/items/${form.dataset.id}`, { method: "PATCH", json: { location: form.location.value } }))) { closeSheet(); toast("Plaats bewaard"); }
  },
  async "save-profile"(form) { if (await mutate(() => api("/api/settings/profile", { method: "PUT", json: { profile: form.profile.value } }))) toast("Bewaard"); },
  async "save-place"(form) {
    const r = await mutate(() => api("/api/settings/place", { method: "PUT", json: { place: form.place.value } }));
    if (r) { toast(r.place ? `Weer van ${r.place.name}` : "Woonplaats gewist"); ui.weather = undefined; if (r.place) loadWeather(); }
  },
  async "save-key"(form) {
    ui.keyBusy = true; render();
    const ok = await mutate(() => api("/api/settings/api-key", { method: "PUT", json: { key: form.key.value } }));
    ui.keyBusy = false; render();
    if (ok) toast("Sleutel bewaard");
  },
  async "change-password"(form) {
    try { await api("/api/auth/password", { method: "PUT", json: { current: form.current.value, new: form.password.value } }); closeSheet(); toast("Wachtwoord gewijzigd"); }
    catch (e) { toast(e.message); }
  },
  async "create-user"(form) {
    try { await api("/api/users", { method: "POST", json: { username: form.username.value, password: form.password.value } }); closeSheet(); toast("Account aangemaakt"); loadUsers(); }
    catch (e) { toast(e.message); }
  },
  async "reset-password"(form) {
    try { await api(`/api/users/${form.dataset.id}/password`, { method: "PUT", json: { password: form.password.value } }); closeSheet(); toast("Wachtwoord gereset"); }
    catch (e) { toast(e.message); }
  },
};

async function authSubmit(path, form) {
  ui.authBusy = true; ui.authError = null; render();
  try {
    const r = await api(path, { method: "POST", json: { username: form.username.value, password: form.password.value } });
    ui.auth = { setup_needed: false, user: r.user };
    ui.authBusy = false;
    await load();
  } catch (e) { ui.authBusy = false; ui.authError = e.message; render(); }
}

async function saveItem(page, isNew) {
  if (page.saving) return;
  page.saving = true; page.error = null; render();
  try {
    const data = draftOut(page.draft);
    const item = isNew ? await api("/api/items", { method: "POST", json: { data, photo_token: page.photoToken || "" } })
      : await api(`/api/items/${page.id}`, { method: "PATCH", json: data });
    if (page.file && !page.photoToken) { // a photo the AI couldn't read: still the piece's photo
      const form = new FormData(); form.append("photo", await smaller(page.file), "foto.jpg");
      await api(`/api/items/${item.id}/photo`, { method: "POST", form });
    }
    if (data.location) ui.lastLocation = data.location; // the next piece probably hangs in the same place
    await load();
    stack = isNew ? [{ key: ++pageSeq, kind: "detail", id: item.id }] : stack.slice(0, -1);
    render();
    toast(isNew ? "Toegevoegd aan je dressing" : "Bewaard", isNew ? { label: "Nog een", run: () => actions.add() } : null);
  } catch (e) { page.error = e.message; page.saving = false; render(); }
}

const changes = {
  "photo-chosen": (el) => { const file = el.files?.[0]; if (file) { const page = stack.at(-1); aiReady() ? identify(page, file) : Object.assign(page, { phase: "review", draft: newDraft(), file, preview: URL.createObjectURL(file) }) && render(); } },
  async "replace-photo"(el) {
    const file = el.files?.[0], page = stack.at(-1);
    if (!file) return;
    const form = new FormData(); form.append("photo", await smaller(file), "foto.jpg");
    if (await mutate(() => api(`/api/items/${page.id}/photo`, { method: "POST", form }))) { page.preview = null; render(); toast("Foto vervangen"); }
  },
  "use-weather": (el) => { ui.stylist.useWeather = el.checked; },
};

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-act]");
  if (!el || el.tagName === "INPUT" || !actions[el.dataset.act]) return;
  e.preventDefault();
  actions[el.dataset.act](el);
});
document.addEventListener("submit", (e) => {
  const form = e.target.closest("[data-act-submit]");
  if (!form) return;
  e.preventDefault();
  submits[form.dataset.actSubmit]?.(form);
});
document.addEventListener("change", (e) => { const el = e.target.closest("[data-act-change]"); if (el) changes[el.dataset.actChange]?.(el); });
document.addEventListener("input", (e) => {
  const el = e.target;
  if (el.dataset.bind) { const d = stack.at(-1)?.draft; if (d) d[el.dataset.bind] = el.type === "checkbox" ? el.checked : el.value; }
  if (el.dataset.bindPage) { const p = stack.at(-1); p[el.dataset.bindPage] = el.value; if (el.dataset.bindPage === "hint") { const had = !!e.target.closest(".page").querySelector('[data-act="identify-text"]'); if (had !== !!el.value) render(); } }
  if (el.dataset.bindUi) { ui[el.dataset.bindUi] = el.value; render(); }
  if (el.dataset.bindStylist) ui.stylist[el.dataset.bindStylist] = el.value;
});
// Enter sends the question; Shift+Enter makes a new line.
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && e.target.dataset?.bindStylist) { e.preventDefault(); askStylist(e.target.value); }
});
addEventListener("scroll", () => updateBars(), { passive: true });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && ui.auth?.user && !stack.length) load(); });
window.addEventListener("unhandledrejection", (e) => { console.error(e.reason); toast(`Er liep iets mis: ${e.reason?.message || e.reason}`); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
boot();
