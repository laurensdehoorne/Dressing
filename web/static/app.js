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

// Ways to adjust an answer, one tap each.
const REFINE_SUGGESTIONS = [["👟", "Andere schoenen"], ["👔", "Iets chiquer"], ["👕", "Iets minder chic"], ["🧥", "Warmer"], ["🌬️", "Luchtiger"],
  ["🎨", "Meer kleur"], ["🔁", "Een heel ander idee"]];

const TRIP_SUGGESTIONS = [["Op reis", [["🧳", "Wat neem ik mee? Help me inpakken"], ["🗺️", "Een dag op stap in de stad"], ["🍽️", "Etentje 's avonds"],
  ["🥾", "Een wandeldag"], ["🏖️", "Een dag aan het water"], ["✈️", "De reisdag zelf"]]]];

let S = { items: [], worn: [], liked_outfits: [], categories: [], statuses: [] };
const ui = {
  tab: "dressing",
  category: "", search: "", sort: "added", place: "",
  stylist: { query: "", loading: false, result: null, error: null, useWeather: true, step: "", onTrip: null, thread: [], refine: "" },
  auth: null,
  tripWeather: undefined,
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
/** An id for one save: sent again on a retry, so the server bewaart it once (a second tap, an answer that got lost). */
const requestId = () => crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
/** An estimated cost in euros: tenths of a cent are shown for the small amounts one analysis costs. */
const euros = (v) => (v > 0 && v < 0.0005 ? "< € 0,001"
  : `€ ${(v || 0).toLocaleString("nl-BE", { minimumFractionDigits: v && v < 0.1 ? 3 : 2, maximumFractionDigits: v && v < 0.1 ? 3 : 2 })}`);

/** How often and when a piece was worn, from the outfits in Gedragen. Counted once per render for all pieces
    (render() starts over), not per piece: sorting asks it for every comparison. */
let wearTable = null;
function wearOf(id) {
  if (!wearTable) {
    wearTable = new Map();
    for (const l of S.worn) for (const i of new Set(l.item_ids || [])) {
      const w = wearTable.get(i) || { count: 0, last: null };
      w.count += 1;
      if (!w.last || l.date > w.last) w.last = l.date;
      wearTable.set(i, w);
    }
  }
  return wearTable.get(id) || { count: 0, last: null };
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
  S = { items: [], worn: [], liked_outfits: [], categories: [], statuses: [] };
  stack = [];
  ui.users = null; ui.models = null; ui.weather = undefined; ui.push = null;
  ui.stylist = { query: "", loading: false, result: null, error: null, useWeather: true, step: "", onTrip: null, thread: [], refine: "" };
  ui.tripWeather = undefined;
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

function itemTile(i, { selectable = false, selected = false, act = "toggle-piece" } = {}) {
  const sub = [i.kind || catLabel(i.category), i.location].filter(Boolean).join(" · ");
  return `<button class="tile-item${away(i) ? " away" : ""}" data-act="${selectable ? act : "open"}" data-id="${h(i.id)}"
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
  const here = S.items.filter((i) => !away(i)).length;
  return `<h1 class="large">Dressing</h1>
    <div class="summary"><b>${S.items.length}</b> ${S.items.length === 1 ? "stuk" : "stukken"}${here < S.items.length ? ` · ${here} in de dressing` : ""}</div>
    <label class="search"><span aria-hidden="true">🔍</span><input type="search" placeholder="Zoek op naam, kleur, merk, plaats…" value="${h(ui.search)}" data-bind-ui="search" aria-label="Zoeken"></label>
    ${categoryPills(S.items, ui.category, "category")}
    <div id="dressing-results">${dressingResults()}</div>`;
}

/** The pieces that match the search: typing in the search box only redraws these. */
function dressingResults() {
  const list = filteredItems();
  return list.length ? `<div class="grid">${list.map((i) => itemTile(i)).join("")}</div>` : `<div class="empty"><p>Niets gevonden.</p></div>`;
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
    ${S.trip ? `<button class="btn-secondary" style="margin-top:10px" data-act="pack-one" data-id="${h(i.id)}">🧳 ${packed(i.id) ? "Uit de valies halen" : `In de valies voor ${h(tripName())}`}</button>` : ""}
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
  const batch = page.batch, title = batch ? `Stuk ${page.batchAt + 1} van ${batch.length}` : "Kledingstuk toevoegen";
  const bar = `<div class="topbar"><button class="btn-plain" data-act="back">${page.batchSaved ? "Stop" : "Annuleer"}</button><span class="title">${title}</span><span class="spacer"></span>
    ${batch && page.phase === "review" ? `<button class="btn-plain" data-act="skip-piece" ${page.saving ? "disabled" : ""}>Sla over</button>` : ""}</div>`;
  if (page.phase === "analyzing") return `${bar}<div class="analyzing">${page.preview ? `<img src="${h(page.preview)}" alt="">` : ""}
      <div class="spinner"></div><p><b>De AI bekijkt je kledingstuk…</b></p><p class="muted small">Soort, kleur, stof, seizoen en stijl. Meestal een tiental seconden.</p></div>`;
  if (page.phase === "review") return `${bar}${itemForm(page, "save-new")}`;
  return `${bar}
    ${!aiReady() ? `<div class="notice">Zonder API-sleutel kan de AI niets herkennen. Stel er een in bij Instellingen, of vul het kledingstuk zelf in.</div>` : ""}
    <label class="pick-photo" style="margin-top:12px">${ic("camera")}<b>Foto's nemen of kiezen</b><span>Eén kledingstuk per foto, liefst op een hanger of plat gelegd bij goed licht. Kies er gerust meerdere tegelijk.</span>
      <input class="file-input" type="file" accept="image/*" multiple data-act-change="photo-chosen"></label>
    <div class="card" style="margin-top:12px"><div class="field"><label for="hint">Wat extra uitleg (mag leeg)</label>
      <input id="hint" data-bind-page="hint" value="${h(page.hint || "")}" placeholder="bv. merk Zara, maat M, gekregen van mama"></div></div>
    ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}
    <div class="buttons">
      ${page.hint ? `<button class="btn-secondary" data-act="identify-text" ${aiReady() ? "" : "disabled"}>Herkennen zonder foto</button>` : ""}
      <button class="btn-secondary" data-act="order">🛍️ Uit een bestelmail (Zara, H&amp;M…)</button>
      <button class="btn-secondary" data-act="manual">Zelf invullen</button></div>`;
}

// MARK: - From an order confirmation

/** Pasted mail (HTML) as text, with [foto N] where a picture stood: the server fetches the pictures and the AI matches
    them to the pieces (and reads a colour from a swatch). Tracking pixels and pictures that aren't on the web stay out. */
function mailText(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const photos = [], out = [];
  const BLOCK = /^(P|DIV|TR|TABLE|LI|UL|OL|H[1-6]|BR|SECTION|ARTICLE|HEADER|FOOTER|CENTER|BLOCKQUOTE)$/;
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) { out.push(node.textContent); return; }
    if (node.nodeType !== Node.ELEMENT_NODE || /^(STYLE|SCRIPT|HEAD|TITLE)$/.test(node.tagName)) return;
    if (node.tagName === "IMG") {
      const src = node.getAttribute("src") || "", w = Number(node.getAttribute("width")), hgt = Number(node.getAttribute("height"));
      if (!src.startsWith("https://") || (w && w < 12) || (hgt && hgt < 12)) return;
      let n = photos.indexOf(src) + 1;
      if (!n) n = photos.push(src);
      out.push(` [foto ${n}] `);
      return;
    }
    if (BLOCK.test(node.tagName)) out.push("\n");
    node.childNodes.forEach(walk);
    if (BLOCK.test(node.tagName)) out.push("\n");
    if (node.tagName === "TD") out.push(" ");
  };
  walk(doc.body);
  const text = out.join("").replace(/[ \t\u00a0]+/g, " ").replace(/ *\n */g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  return { text, photos };
}

const alreadyHave = (info) => S.items.some((i) => fold(i.name) === fold(info.name) && fold(i.size) === fold(info.size));

function renderOrder(page) {
  const bar = `<div class="topbar"><button class="btn-plain" data-act="back">Annuleer</button><span class="title">Uit een bestelmail</span><span class="spacer"></span></div>`;
  if (page.phase === "reading") return `${bar}<div class="analyzing"><div class="spinner"></div><p><b>${h(page.step || "De AI leest je bestelling…")}</b></p>
      <p class="muted small">Welke kleren, kleur, maat en prijs, en de foto bij elk stuk.</p></div>`;
  if (page.phase === "pick") {
    const chosen = page.pieces.filter((p) => p.on).length;
    return `${bar}
      <p class="subtitle">${h([page.shop, page.date && shortDate(page.date)].filter(Boolean).join(" · ")) || "Gevonden in de mail"}: ${plural(page.pieces.length, "stuk", "stukken")}.
        Vink aan wat je houdt en zeg waar het hangt.</p>
      <div class="card"><div class="field"><label for="o-all">Waar hangen ze? (voor alle stukken)</label>
        <input id="o-all" data-order-all list="locations" value="${h(page.all)}" placeholder="bv. Linkerkast, plank 2"></div></div>
      ${locationList()}
      ${page.pieces.map((p, n) => `<div class="card order-piece${p.on ? "" : " off"}">
        <div class="row"><button type="button" class="order-check" data-act="toggle-order" data-n="${n}" aria-pressed="${p.on}" aria-label="Toevoegen">${p.on ? "✓" : ""}</button>
          ${p.thumb ? `<img class="order-ph" src="${p.thumb}" alt="">` : `<span class="order-ph none">${ic("shirt")}</span>`}
          <span class="grow" style="min-width:0"><input class="order-name" data-piece="${n}" data-field="name" value="${h(p.draft.name)}" aria-label="Naam">
            <span class="muted small" style="display:block">${swatches(p.draft)}${h([p.draft.brand, p.draft.size && `maat ${p.draft.size}`,
              p.draft.price != null && `€ ${String(p.draft.price).replace(".", ",")}`].filter(Boolean).join(" · "))}</span>
            ${p.dup ? `<span class="small" style="color:var(--orange);display:block">Lijkt al in je dressing te hangen</span>` : ""}</span></div>
        <div class="field split"><div><label for="o-cat-${n}">Soort</label><select id="o-cat-${n}" data-piece="${n}" data-field="category">
            ${S.categories.map((c) => `<option value="${c.key}" ${p.draft.category === c.key ? "selected" : ""}>${h(c.label)}</option>`).join("")}</select></div>
          <div><label for="o-loc-${n}">Plaats</label><input id="o-loc-${n}" data-piece="${n}" data-field="location" list="locations" value="${h(p.draft.location)}"></div></div>
      </div>`).join("")}
      ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}
      <div class="buttons"><button class="btn-primary" data-act="save-order" ${chosen && !page.saving ? "" : "disabled"}>
        ${page.saving ? `<span class="spinner sm"></span>` : ""}${chosen ? `${plural(chosen, "stuk", "stukken")} toevoegen` : "Niets aangevinkt"}</button></div>
      <p class="footnote">Details zoals seizoen, warmte en stijl vulde de AI al in; je past ze later aan met Bewerk.</p>`;
  }
  return `${bar}
    ${!aiReady() ? `<div class="notice">Zonder API-sleutel kan de AI de mail niet lezen. Stel er een in bij Instellingen.</div>` : ""}
    <p class="subtitle">Open de bevestiging of verzendmail van je bestelling, selecteer alles (of kopieer de hele mail) en plak het hieronder.
      De AI haalt er elk kledingstuk uit, met de foto uit de mail.</p>
    <div class="card"><div class="field"><label for="o-mail">De bestelmail</label>
      <textarea id="o-mail" data-paste="order" rows="8" placeholder="Plak hier de mail van Zara, H&amp;M, Zalando…">${h(page.text)}</textarea></div></div>
    ${page.photos?.length ? `<p class="footnote">${plural(page.photos.length, "afbeelding", "afbeeldingen")} gevonden in de mail.</p>` : ""}
    ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}
    <div class="buttons"><button class="btn-primary" data-act="read-order" ${aiReady() ? "" : "disabled"}>Lees de bestelling</button>
      <label class="btn-secondary${aiReady() ? "" : " disabled"}">📄 Of kies de mail als PDF<input class="file-input" type="file" accept="application/pdf,.pdf" data-act-change="order-pdf" ${aiReady() ? "" : "disabled"}></label>
      <label class="btn-secondary${aiReady() ? "" : " disabled"}">📱 Of kies screenshots<input class="file-input" type="file" accept="image/*" multiple data-act-change="order-shots" ${aiReady() ? "" : "disabled"}></label></div>
    <p class="footnote">Een PDF maak je met Afdrukken → Bewaar als PDF. Screenshots mogen van de mail of van je bestelling in de app of op de
      website van de winkel zijn, tot 6 tegelijk; de AI knipt de foto van elk stuk eruit. De foto's uit een geplakte mail zijn het scherpst.</p>`;
}

async function readOrder(page, pdf = null, shots = null) {
  if (!pdf && !shots && (page.text || "").trim().length < 20) { page.error = "Plak eerst de volledige bestelmail."; return render(); }
  Object.assign(page, { phase: "reading", error: null, step: "" });
  render();
  let path = "/api/order", opts = { method: "POST", json: { text: page.text, photos: page.photos || [] } };
  if (pdf) { const form = new FormData(); form.append("pdf", pdf, "bestelling.pdf"); path = "/api/order/pdf"; opts = { method: "POST", form }; }
  if (shots) {
    const form = new FormData();
    for (const shot of shots) form.append("shots", await smaller(shot, 2400), "screenshot.jpg");
    path = "/api/order/screenshots"; opts = { method: "POST", form };
  }
  try {
    const r = await slowApi(path, opts,
      (p) => { if (p.step && p.step !== page.step) { page.step = p.step; if (stack.at(-1) === page) render(); } });
    if (!r.pieces.length) throw new Error("Geen kleren gevonden in deze mail. Plak je de bevestiging met de bestelde stukken?");
    page.all = ui.lastLocation || "";
    page.pieces = r.pieces.map((p) => ({ draft: { ...newDraft(), ...p.info, location: page.all }, token: p.photo_token, thumb: p.thumb,
      dup: alreadyHave(p.info), on: !alreadyHave(p.info) }));
    Object.assign(page, { phase: "pick", shop: r.shop, date: r.date });
  } catch (e) { Object.assign(page, { phase: "paste", error: e.message }); }
  if (stack.includes(page)) render();
}

async function saveOrder(page) {
  if (page.saving) return;
  page.saving = true; page.error = null; render();
  let added = 0;
  try {
    for (const p of page.pieces) {
      if (!p.on || p.saved) continue;
      p.requestId ||= requestId();
      await api("/api/items", { method: "POST", json: { data: draftOut(p.draft), photo_token: p.token || "", request_id: p.requestId } });
      p.saved = true; p.on = false; added++;
    }
  } catch (e) { page.error = `${e.message}${added ? ` (${plural(added, "stuk", "stukken")} wel toegevoegd)` : ""}`; }
  const last = page.pieces.find((p) => p.saved && p.draft.location)?.draft.location;
  if (last) ui.lastLocation = last;
  page.saving = false;
  await load();
  if (page.error) return render();
  stack = []; ui.tab = "dressing"; ui.category = ""; ui.search = ""; ui.sort = "added";
  render(); scrollTo(0, 0);
  toast(`${plural(added, "stuk", "stukken")} toegevoegd aan je dressing`);
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

// MARK: - The suitcase

const tripName = () => S.trip?.place?.name.split(",")[0] || "";
const packed = (id) => !!S.trip?.item_ids.includes(id);
const packedItems = () => (S.trip?.item_ids || []).map(itemById).filter(Boolean);
/** On the trip: the stylist chooses from the suitcase with the destination's weather. On by default from the day before leaving. */
function onTrip() {
  if (!S.trip) return false;
  if (ui.stylist.onTrip !== null) return ui.stylist.onTrip;
  const d = new Date(`${today()}T12:00:00`); d.setDate(d.getDate() + 1);
  return d.toISOString().slice(0, 10) >= S.trip.start && today() <= S.trip.end;
}
const tripDates = (t) => `${shortDate(t.start).replace(/ \d{4}$/, "")} – ${shortDate(t.end)}`;

async function loadTripWeather() {
  if (!S.trip) return;
  ui.tripWeather = null;
  try { ui.tripWeather = await api("/api/trip/weather"); } catch { ui.tripWeather = false; }
  render();
}

function tripWeatherHTML() {
  const w = ui.tripWeather;
  if (w === undefined) { loadTripWeather(); return ""; }
  if (w === null) return `<p class="footnote">Weer ophalen…</p>`;
  if (w === false) return `<p class="footnote">Het weer op je bestemming is niet beschikbaar.</p>`;
  if (!w.days.length) return `<p class="footnote">Nog te ver vooruit voor een weersvoorspelling (tot 16 dagen). De stylist gaat uit van het gebruikelijke weer daar in die periode.</p>`;
  return `<div class="card">${w.days.map((d) => `<div class="row"><span class="grow">${h(longDate(d.date))}<span class="muted small" style="display:block">${h(d.summary)}, ${d.rain}% kans op neerslag</span></span>
    <span class="v"><b>${d.min}–${d.max}°</b></span></div>`).join("")}</div>
    ${w.days.at(-1).date < w.end ? `<p class="footnote">Voor de dagen daarna is er nog geen voorspelling.</p>` : ""}`;
}

function tripStrip() {
  if (!S.trip) return `<button class="trip-strip" data-act="trip">🧳 <span class="grow">Op reis? Vul je valies, dan kiest de stylist daaruit met het weer ter plaatse.</span><span aria-hidden="true">›</span></button>`;
  const n = packedItems().length;
  return `<div class="pills" role="group" aria-label="Waar ben je?" style="margin-top:10px">
      <button data-act="trip-mode" data-value="0" aria-pressed="${!onTrip()}">🏠 Thuis</button>
      <button data-act="trip-mode" data-value="1" aria-pressed="${onTrip()}">🧳 ${h(tripName())}<span class="n">${h(tripDates(S.trip))}</span></button></div>
    ${onTrip() ? `<button class="trip-strip" data-act="trip"><span class="grow">${n ? `${plural(n, "stuk", "stukken")} in je valies: de stylist kiest enkel daaruit.` : "Je valies is nog leeg: de stylist helpt je inpakken uit je dressing."}</span><span aria-hidden="true">Valies ›</span></button>` : ""}`;
}

function renderTrip(page) {
  const t = S.trip, d = page.draft;
  const form = `<form class="card" data-act-submit="save-trip">
      <div class="field"><label for="t-dest">Bestemming</label><input id="t-dest" name="destination" value="${h(d.destination)}" placeholder="bv. Barcelona" required></div>
      <div class="field split"><div><label for="t-start">Vertrek</label><input id="t-start" name="start" type="date" value="${h(d.start)}" required></div>
        <div><label for="t-end">Terug</label><input id="t-end" name="end" type="date" value="${h(d.end)}" required></div></div>
      <div class="auth-submit"><button class="btn-primary" ${page.saving ? "disabled" : ""}>${page.saving ? `<span class="spinner sm"></span>` : ""}${t ? "Reis aanpassen" : "Valies klaarzetten"}</button></div></form>`;
  const bar = `<div class="topbar"><button class="btn-plain" data-act="back">‹ Terug</button><span class="title">Valies</span><span class="spacer"></span></div>`;
  if (!t) return `${bar}<p class="subtitle">Waar ga je naartoe en wanneer? Daarna leg je kleren in je valies, en kiest de stylist op reis enkel daaruit, met het weer ter plaatse.</p>${form}`;
  const chosen = new Set(t.item_ids);
  const pool = S.items.filter((i) => page.filter === "packed" ? chosen.has(i.id) : !page.filter || i.category === page.filter);
  return `${bar}<h1 class="large">🧳 ${h(tripName())}</h1><p class="subtitle">${h(tripDates(t))}</p>
    ${tripWeatherHTML()}
    <h2 class="section-title">${chosen.size ? `${plural(chosen.size, "stuk", "stukken")} in je valies` : "Tik aan wat je meeneemt"}</h2>
    <div class="pills" role="group" aria-label="Tonen"><button data-act="trip-filter" data-value="packed" aria-pressed="${page.filter === "packed"}">🧳 In de valies<span class="n">${chosen.size}</span></button></div>
    ${categoryPills(S.items, page.filter === "packed" ? null : page.filter || "", "trip-filter")}
    ${pool.length ? `<div class="grid">${pool.map((i) => itemTile(i, { selectable: true, selected: chosen.has(i.id), act: "toggle-pack" })).join("")}</div>`
      : `<div class="empty"><p>Nog niets in je valies.</p></div>`}
    <button class="btn-secondary" style="margin-top:16px" data-act="pack-help">${ic("sparkle")} Laat de stylist helpen inpakken</button>
    <h2 class="section-title">Reis</h2>${form}
    <div class="card" style="margin-top:20px"><div class="row"><button class="row-button danger" data-act="end-trip">Terug thuis: valies leegmaken</button></div></div>`;
}

async function savePacked(ids) {
  S.trip.item_ids = ids; render(); // at once; the server follows
  try { S.trip = (await api("/api/trip/items", { method: "PUT", json: { item_ids: ids } })).trip; }
  catch (e) { toast(e.message); await load(); }
  render();
}

function weatherLine() {
  if (onTrip()) {
    const w = ui.tripWeather, d = w?.days?.[0];
    if (w === undefined) loadTripWeather();
    const text = w === null || w === undefined ? "Weer ophalen…" : w === false ? "Weer niet beschikbaar" :
      d ? `<b>${d.min}–${d.max}°</b> ${h(d.summary)} in ${h(tripName())} (${h(longDate(d.date))})` : `Nog geen voorspelling voor ${h(tripName())}`;
    return `<div class="weather-strip"><span>🌦️ ${text}</span></div>`;
  }
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
    ${tripStrip()}${weatherLine()}`;
  if (!S.items.length) return `${top}<div class="empty"><p>Voeg eerst kleren toe aan je dressing; de stylist kiest enkel uit wat je hebt.</p></div>`;
  if (!aiReady()) return `${top}<div class="notice">Stel een API-sleutel voor ${h(aiService().name)} in bij Instellingen om outfits te laten voorstellen.</div>`;
  if (st.loading) return `${top}<div class="analyzing"><div class="spinner"></div><p><b>${h(st.step || "Je stylist denkt na…")}</b></p><p class="muted small">${onTrip()
    ? `Kijkt naar ${packedItems().length ? "je valies" : "je dressing"} en het weer in ${h(tripName())}.` : `Kijkt naar je dressing${st.useWeather && S.place ? ", het weer" : ""} en wat je laatst droeg.`}</p></div>`;
  if (st.error && !st.result) return `${top}<div class="notice err">${h(st.error)}</div>${suggestions()}`;
  if (st.result) return `${top}${st.thread.length > 1 ? `<p class="footnote">Bijgestuurd: ${st.thread.slice(1).map((q) => `‘${h(q)}’`).join(" → ")}</p>` : ""}
    ${outfitsHTML(st.result)}${refineBox()}<div class="buttons"><button class="btn-secondary" data-act="stylist-reset">Nieuwe vraag</button></div>`;
  return `${top}${inspirationStrip()}${suggestions()}`;
}

/** Adjusting the answer that is shown ("andere schoenen"): the stylist sees its suggestion and changes only that. */
function refineBox() {
  const st = ui.stylist;
  if (!st.result.outfits.length) return "";
  return `<h2 class="section-title">Bijsturen</h2>
    <form class="ask refine" style="margin-top:0" data-act-submit="refine"><textarea name="query" rows="1" placeholder="bv. andere schoenen, zonder blazer, iets warmer"
      aria-label="Bijsturen" data-bind-stylist="refine">${h(st.refine)}</textarea>
      <button class="btn-pill icon primary" aria-label="Pas het voorstel aan">↑</button></form>
    <div class="sugg refine-sugg">${REFINE_SUGGESTIONS.map(([emoji, text]) => `<button data-act="refine-suggest" data-q="${h(text)}">${emoji} ${h(text)}</button>`).join("")}</div>
    ${st.error ? `<div class="notice err">${h(st.error)}</div>` : ""}
    <p class="footnote">Niet goed? Tik 👎 bij een outfit en zeg waarom: de stylist onthoudt het en stelt het niet opnieuw voor.</p>`;
}

function suggestions() {
  return (onTrip() ? TRIP_SUGGESTIONS : SUGGESTIONS).map(([title, list]) => `<div class="sugg-group"><h3>${h(title)}</h3><div class="sugg">
    ${list.map(([emoji, text]) => `<button data-act="suggest" data-q="${h(text)}">${emoji} ${h(text)}</button>`).join("")}</div></div>`).join("");
}

function outfitsHTML(r) {
  const piece = (id) => {
    const i = itemById(id);
    if (!i) return "";
    return `<button class="piece" data-act="open" data-id="${h(id)}">${i.photo ? `<img src="${h(small(i.photo))}" alt="">` : `<span class="ph">${ic("shirt")}</span>`}
      <span>${h(i.name)}</span><small>${ic("pin").replace('class="ic"', 'class="ic" style="width:11px;height:11px;vertical-align:-1px"')} ${h(i.location || "geen plaats")}</small></button>`;
  };
  // The weather the stylist looked up for the place the question is about, e.g. an outing to Amsterdam.
  const elsewhere = (r.elsewhere || []).map((w) => {
    const d = w.days?.[0], name = w.place.split(",")[0];
    return `<div class="weather-strip"><span>🌦️ ${d ? `<b>${d.min}–${d.max}°</b> ${h(d.summary)} in ${h(name)} (${h(longDate(d.date))})`
      : `Nog geen voorspelling voor ${h(name)}: de stylist gaat uit van het gebruikelijke weer daar`}</span></div>`;
  }).join("");
  return `${elsewhere}${r.advice ? `<p class="advice">${h(r.advice)}</p>` : ""}
    ${r.trend_sources?.length ? `<p class="footnote">Actuele trendartikelen (cache van ${h(r.trend_sources[0].updated)}): ${[...new Map(r.trend_sources.map((s) => [s.source, s])).values()].map((s) => `<a href="${h(s.url)}" target="_blank" rel="noopener noreferrer">${h(s.source)}</a>`).join(" · ")}</p>` : ""}
    ${r.outfits.map((o, n) => {
      const query = ui.stylist.thread.join(" → ") || ui.stylist.query;
      const saved = (S.liked_outfits || []).find((x) => x.item_ids?.length === o.item_ids?.length && x.item_ids.every((id, i) => id === o.item_ids[i]));
      return `<article class="card outfit${o.rejected ? " rejected" : ""}">
      <div class="head"><span class="n">Outfit ${n + 1}</span><h2>${h(o.title)}</h2></div>
      <div class="pieces">${o.item_ids.map(piece).join("")}</div>
      <p class="why">${h(o.reason)}</p>${o.tip ? `<p class="tip">💡 ${h(o.tip)}</p>` : ""}
      ${pictureHTML(o, n)}
      ${o.rejected ? `<div class="notice turned-down"><span class="grow">👎 Afgekeurd: ${h(rejectWhy(o.rejected))}</span><button data-act="undo-reject" data-n="${n}">Ongedaan maken</button></div>`
        : `<div class="foot"><button class="btn-secondary narrow" data-act="reject-outfit" data-n="${n}" aria-label="Niet goed">👎</button>
        ${r.trip && o.item_ids.some((id) => !packed(id)) ? `<button class="btn-secondary" data-act="pack-outfit" data-n="${n}">🧳 In de valies</button>` : ""}
        <button class="btn-secondary" data-act="like-outfit" data-n="${n}" data-id="${h(saved?.id || "")}">${saved ? "★ Bewaard" : "♡ Mooi"}</button>
        <button class="btn-primary" data-act="wear-outfit" data-n="${n}">Dit draag ik</button></div>`}</article>`;
    }).join("")}
    ${!r.outfits.length ? `<div class="notice">Geen outfit gevonden die past.</div>` : ""}
    ${r.missing ? `<div class="notice">🛍️ ${h(r.missing)}</div>` : ""}
    ${r.analysis ? `<p class="footnote" style="text-align:center">${costSentence("Dit antwoord", r.analysis)}</p>` : ""}`;
}

/** The picture of an outfit, only made when asked with its button (ChatGPT draws it from the photos of the pieces). */
function pictureHTML(o, n) {
  if (o.picture) return `<img class="outfit-picture" src="${h(o.picture)}" alt="Beeld van ${h(o.title)}">
    <p class="footnote">Getekend door ${h(S.ai?.image_model || "ChatGPT")} naar de foto's van je stukken: het kan afwijken van hoe ze echt zijn.${o.pictureCost ? ` ${costSentence("Dit beeld", o.pictureCost)}` : ""}</p>`;
  if (o.drawing) return `<p class="footnote"><span class="spinner sm"></span> Beeld tekenen…</p>`;
  if (!S.ai?.can_draw) return "";
  return `${o.drawError ? `<div class="notice err">${h(o.drawError)}</div>` : ""}
    <button class="btn-plain draw-outfit" data-act="draw-outfit" data-n="${n}">🖼️ Toon als beeld</button>`;
}

/** A question for the stylist; with followUp, an adjustment of the answer that is shown (it sees that answer). */
async function askStylist(query, followUp = false) {
  const st = ui.stylist;
  if (!query.trim() || st.loading) return;
  const before = { result: st.result, thread: st.thread };
  const previous = followUp && st.result ? { queries: st.thread, outfits: st.result.outfits.map((o) => ({ title: o.title, item_ids: o.item_ids })) } : null;
  Object.assign(st, { loading: true, error: null, result: null, step: "" }, previous ? {} : { query });
  render();
  try {
    const trip = previous ? before.result.trip : onTrip();
    st.result = await slowApi("/api/stylist", { method: "POST", json: { query, use_weather: st.useWeather, trip, previous } },
      (p) => { if (p.step && p.step !== st.step) { st.step = p.step; if (ui.tab === "stylist" && !stack.length) render(); } });
    if (st.result.weather) ui.weather = st.result.weather;
    st.result.trip = trip;
    st.thread = previous ? [...before.thread, query] : [query];
    st.refine = "";
  } catch (e) {
    st.error = e.message;
    if (previous) Object.assign(st, before); // the answer stays, with the error under it
  }
  st.loading = false;
  render();
}

// MARK: - Turning down a suggestion

const rejectWhy = (r) => [...(r.reasons || []), r.note && `‘${r.note}’`].filter(Boolean).join(", ") || "niet goed";

function openRejectSheet() {
  const rj = ui.reject, o = ui.stylist.result.outfits[rj.n];
  openSheet(`<div class="topbar"><button class="btn-plain" data-act="close-sheet">Annuleer</button><span class="title" id="sheet-title">Wat was er niet goed?</span><span class="spacer"></span></div>
    <p class="subtitle" style="margin-top:4px">${h(o.title)}</p>
    <div class="chips">${(S.reject_reasons || []).map((r) => `<button type="button" class="chip${rj.reasons.includes(r) ? " on" : ""}" data-act="toggle-reason" data-value="${h(r)}" aria-pressed="${rj.reasons.includes(r)}">${h(r)}</button>`).join("")}</div>
    <div class="card" style="margin-top:14px"><div class="field"><label for="r-note">In je eigen woorden (mag leeg)</label>
      <input id="r-note" data-bind-reject="note" value="${h(rj.note)}" placeholder="bv. die broek is te kort voor deze schoenen"></div></div>
    ${rj.error ? `<div class="notice err">${h(rj.error)}</div>` : ""}
    <div class="buttons"><button class="btn-primary" data-act="save-reject" ${rj.saving ? "disabled" : ""}>${rj.saving ? `<span class="spinner sm"></span>` : ""}👎 Afkeuren</button></div>
    <p class="footnote">De stylist onthoudt dit: hij stelt dit outfit niet opnieuw voor en houdt rekening met de reden. Bij Instellingen kan je het vergeten.</p>`);
}

async function saveReject() {
  const rj = ui.reject, st = ui.stylist, o = st.result?.outfits[rj.n];
  if (!o || rj.saving) return;
  if (!rj.reasons.length && !rj.note.trim()) { rj.error = "Tik een reden aan of schrijf kort wat er niet goed aan was."; return openRejectSheet(); }
  rj.saving = true; rj.error = null; openRejectSheet();
  try {
    o.rejected = await api("/api/rejected", { method: "POST", json: { item_ids: o.item_ids, reasons: rj.reasons, note: rj.note,
      query: st.thread.join(" → ") || st.query, title: o.title, request_id: rj.requestId } });
    S.rejected = [o.rejected, ...(S.rejected || [])];
    closeSheet(); render(); toast("Onthouden: de stylist stelt dit niet meer voor");
  } catch (e) { rj.error = e.message; rj.saving = false; openRejectSheet(); }
}

function openRejectedList() {
  const list = S.rejected || [];
  const names = (r) => r.item_ids.map((id) => itemById(id)?.name).filter(Boolean).join(" + ") || "Verwijderde kleren";
  openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Afgekeurde outfits</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
    ${list.length ? `<div class="card">${list.map((r) => `<div class="row"><span class="grow" style="min-width:0">${h(names(r))}
        <span class="muted small" style="display:block">${h(shortDate(r.at.slice(0, 10)))}${r.query ? ` · ${h(r.query)}` : ""} · ${h(rejectWhy(r))}</span></span>
        <button class="btn-plain small" data-act="forget-reject" data-id="${h(r.id)}">Vergeet</button></div>`).join("")}</div>`
      : `<div class="card pad muted">Nog niets afgekeurd. Tik 👎 bij een voorgesteld outfit dat niet goed is.</div>`}
    <p class="footnote">De stylist ziet de laatste 25 en stelt die outfits niet opnieuw voor. Vergeet je er een, dan mag hij het weer voorstellen.</p>`);
}

function openLikedOutfits() {
  const list = S.liked_outfits || [];
  openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Bewaarde voorstellen</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
    ${list.length ? `<div class="card">${list.map((o) => `<div class="row"><span class="grow" style="min-width:0">${h(o.title || "Outfit")}
        <span class="muted small" style="display:block">${h((o.item_ids || []).map((id) => o.item_names?.[id] || itemById(id)?.name).filter(Boolean).join(" + "))} · ${h(o.query || "bewaard")}</span></span>
        <button class="btn-plain small" data-act="forget-liked" data-id="${h(o.id)}">Vergeet</button></div>`).join("")}</div>`
      : `<div class="card pad muted">Nog geen voorstellen bewaard. Tik ♡ Mooi bij een suggestie.</div>`}
    <p class="footnote">Bewaarde voorstellen zijn feedback voor je kleurprofiel en volgende outfitkeuzes. Ze worden niet als gedragen outfits geteld.</p>`);
}

// MARK: - Inspiration: outfits from Instagram or Pinterest, and the style the AI writes from them

const looks = () => S.inspiration || [];
const lookById = (id) => looks().find((l) => l.id === id);

function inspirationStrip() {
  const n = looks().length;
  return `<button class="trip-strip" data-act="inspiration">✨ <span class="grow">${n ? `Je stijl uit ${plural(n, "outfit", "outfits")} die je mooi vindt gaat mee.`
    : "Leer de stylist je smaak: bewaar outfits die je mooi vindt op Instagram of Pinterest."}</span><span aria-hidden="true">Inspiratie ›</span></button>`;
}

function lookTile(l) {
  return `<button class="tile-item" data-act="open-look" data-id="${h(l.id)}"><img class="ph" src="${h(small(l.photo))}" alt="" loading="lazy">
    <span class="txt"><span class="sub">${swatches(l)}${h((l.style_tags || []).slice(0, 3).join(", "))}</span></span></button>`;
}

function renderInspiration(page) {
  const bar = `<div class="topbar"><button class="btn-plain" data-act="back">‹ Terug</button><span class="title">Inspiratie</span><span class="spacer"></span></div>`;
  const list = looks(), ready = aiReady();
  const adding = page.phase === "reading" ? `<div class="analyzing"><div class="spinner"></div><p><b>${h(page.step || "De AI bekijkt je outfits…")}</b></p>
      <p class="muted small">Kleuren, silhouet en sfeer, en wat dat zegt over je stijl. Een heel bord duurt een paar minuten.</p></div>`
    : `<div class="buttons" style="margin-top:0"><label class="btn-primary${ready ? "" : " disabled"}">📱 Screenshots kiezen
        <input class="file-input" type="file" accept="image/*" multiple data-act-change="inspiration-shots" ${ready ? "" : "disabled"}></label></div>
      <form class="card" style="margin-top:12px" data-act-submit="inspiration-link"><div class="field"><label for="i-url">Of plak de link van een pin of een heel bord</label>
        <input id="i-url" name="url" type="url" inputmode="url" autocapitalize="off" spellcheck="false" placeholder="https://pinterest.com/jouwnaam/outfits" value="${h(page.url || "")}" required></div>
        <div class="auth-submit"><button class="btn-secondary" ${ready ? "" : "disabled"}>Ophalen</button></div></form>`;
  const style = page.editing ? `<form class="card" data-act-submit="save-style"><div class="field"><label for="i-style">Je stijl</label>
        <textarea id="i-style" name="profile" rows="6">${h(S.style_profile)}</textarea></div>
        <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
      <div class="buttons"><button class="btn-secondary" data-act="cancel-style">Annuleer</button></div>`
    : S.style_profile ? `<div class="card pad" style="white-space:pre-line;line-height:1.45">${h(S.style_profile)}</div>
      <div class="buttons"><button class="btn-secondary" data-act="edit-style">Aanpassen</button>
        ${list.length && ready ? `<button class="btn-secondary" data-act="rewrite-style" ${page.phase === "reading" ? "disabled" : ""}>Opnieuw opmaken uit je outfits</button>` : ""}</div>`
    : `<div class="card pad muted">Nog leeg. Bewaar een paar outfits, dan schrijft de AI hier je stijl.</div>`;
  return `${bar}<h1 class="large">Inspiratie</h1>
    <p class="subtitle">Outfits die je mooi vindt op Instagram of Pinterest. De AI haalt er je stijl uit, en de stylist kiest daarnaar uit je eigen kleren.</p>
    ${!ready ? `<div class="notice">Zonder API-sleutel kan de AI geen outfits bekijken. Stel er een in bij Instellingen.</div>` : ""}
    ${adding}
    ${page.error ? `<div class="notice err">${h(page.error)}</div>` : ""}${page.note ? `<div class="notice ok">${h(page.note)}</div>` : ""}
    <h2 class="section-title">Je stijl</h2>${style}
    <p class="footnote">Gaat mee naar de stylist bij elke vraag, naast wat je bij Over jou schreef. Tik een outfit aan om het na te maken met je eigen kleren.</p>
    ${list.length ? `<h2 class="section-title">${plural(list.length, "outfit", "outfits")}</h2><div class="grid">${list.map(lookTile).join("")}</div>` : ""}
    <p class="footnote">Instagram: neem een screenshot van de post (tot 6 tegelijk); de AI knipt het outfit eruit. Pinterest: een screenshot,
      of Delen → Link kopiëren en hier plakken, van een pin of van een heel bord. Van een bord komen de nieuwste pins (meestal tot 25)
      die er nog niet in staan; het bord moet openbaar zijn. Importeer later opnieuw voor wat er bijkwam.</p>`;
}

function openLook(id) {
  const l = lookById(id);
  if (!l) return;
  const chips = (list) => (list?.length ? `<div class="chips" style="margin-top:12px">${list.map((x) => `<span class="chip">${h(x)}</span>`).join("")}</div>` : "");
  openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Outfit</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
    <button class="hero-photo" data-act="view-photo" data-src="${h(l.photo)}" aria-label="Foto groot bekijken"><img src="${h(l.photo)}" alt=""></button>
    <p style="margin:14px 4px 0;line-height:1.45">${h(l.description)}</p>${chips(l.pieces)}${chips(l.style_tags)}
    ${l.url ? `<p class="footnote"><a href="${h(l.url)}" target="_blank" rel="noopener noreferrer">Bekijk de pin</a></p>` : ""}
    <div class="buttons"><button class="btn-primary" data-act="copy-look" data-id="${h(l.id)}" ${aiReady() ? "" : "disabled"}>${ic("sparkle")} Maak na met mijn kleren</button></div>
    <div class="card" style="margin-top:20px"><div class="row"><button class="row-button danger" data-act="delete-look" data-id="${h(l.id)}">Verwijderen uit Inspiratie</button></div></div>`);
}

async function addInspiration(page, shots = null, url = "") {
  if (page.phase === "reading") return;
  Object.assign(page, { phase: "reading", error: null, note: null, step: "", url });
  render();
  const form = new FormData();
  if (shots) for (const shot of shots) form.append("shots", await smaller(shot, 2400), "screenshot.jpg");
  else form.append("url", url);
  try {
    const r = await slowApi("/api/inspiration", { method: "POST", form },
      (p) => { if (p.step && p.step !== page.step) { page.step = p.step; if (stack.at(-1) === page) render(); } });
    const board = r.pins !== undefined;
    if (board && !r.added.length && !r.skipped) throw new Error(`Alle ${plural(r.pins, "pin", "pins")} van dit bord staan al in Inspiratie.`);
    if (!r.added.length) throw new Error(board ? "Op de nieuwe pins van dit bord zag de AI geen outfit."
      : shots?.length > 1 ? "Op deze foto's zag de AI geen outfit." : "Op deze foto zag de AI geen outfit.");
    page.url = "";
    page.note = [`${plural(r.added.length, "outfit", "outfits")} bewaard`, r.skipped && `${plural(r.skipped, board ? "pin" : "foto", board ? "pins" : "foto's")} zonder outfit overgeslagen`,
      r.known && `${plural(r.known, "pin stond", "pins stonden")} er al in`].filter(Boolean).join(", ") + ". Je stijl is bijgewerkt.";
    await load();
  } catch (e) { page.error = e.message; }
  page.phase = "idle";
  if (stack.includes(page)) render();
}

async function rewriteStyle(page) {
  if (page.phase === "reading") return;
  Object.assign(page, { phase: "reading", error: null, note: null, step: "" });
  render();
  try {
    S.style_profile = (await slowApi("/api/inspiration/profile", { method: "POST" })).style_profile;
    page.note = "Je stijl is opnieuw opgemaakt.";
  } catch (e) { page.error = e.message; }
  page.phase = "idle";
  if (stack.includes(page)) render();
}

// MARK: - Worn tab and the outfit page

function renderWorn() {
  if (!S.worn.length) return `<h1 class="large">Gedragen</h1><div class="empty"><div class="icon">${ic("calendar")}</div><h2>Nog niets gedragen</h2>
    <p>Zeg wat je aantrok: zo weet de stylist wat je graag draagt en wat al te lang in de kast hangt.</p>
    <button class="btn-primary empty-action" data-act="new-outfit">Outfit van vandaag</button></div>`;
  const byMonth = {};
  const visible = ui.wornMonth ? S.worn.filter((l) => l.date.slice(0, 7) === ui.wornMonth) : S.worn;
  for (const l of visible) (byMonth[l.date.slice(0, 7)] ||= []).push(l);
  const never = S.items.filter((i) => !away(i) && !wearOf(i.id).count).length;
  return `<h1 class="large">Gedragen</h1>
    <div class="card" style="padding:12px"><div class="row"><div class="field grow" style="margin:0"><label for="worn-month">Bekijk een maand</label><input id="worn-month" type="month" value="${h(ui.wornMonth || "")}" data-act-change="worn-month"></div>
      <button class="btn-secondary" style="width:auto;padding:8px;font-size:.9rem;white-space:nowrap" data-act="worn-last-year">Jaar terug</button><button class="btn-secondary" style="width:auto;padding:8px;font-size:.9rem" data-act="worn-all">Alles</button></div></div>
    ${ui.wornMonth && !visible.length ? `<p class="empty">Geen outfits in ${h(new Date(`${ui.wornMonth}-15T12:00:00`).toLocaleDateString("nl-BE", { month: "long", year: "numeric" }))}.</p>` : ""}
    ${never ? `<p class="subtitle">${plural(never, "stuk hangt", "stukken hangen")} er nog ongedragen bij.</p>` : ""}
    ${Object.entries(byMonth).map(([month, logs]) => `<h2 class="section-title">${h(new Date(`${month}-15T12:00:00`).toLocaleDateString("nl-BE", { month: "long", year: "numeric" }))}</h2>
      <div class="card">${logs.map(logRow).join("")}</div>`).join("")}`;
}

function logRow(l) {
  const snapshots = new Map((l.item_snapshots || []).map((i) => [i.id, i]));
  const items = (l.item_ids || []).map((id) => {
    const current = itemById(id), snapshot = snapshots.get(id);
    return current ? { ...current, name: snapshot?.name || current.name } : snapshot;
  }).filter(Boolean);
  const names = items.map((i) => i.name || i.kind || "Kledingstuk").join(" + ") || "Verwijderde kleren";
  return `<button class="log" data-act="open-log" data-id="${h(l.id)}"><span class="stack">${items.slice(0, 4).map((i) => (i.photo ? `<img src="${h(small(i.photo))}" alt="">` : `<span class="ph"></span>`)).join("")}</span>
    <span class="grow" style="min-width:0"><span class="name">${h(longDate(l.date))}</span>
      <span class="sub" style="display:block">${l.rating ? `<span class="stars">${"★".repeat(l.rating)}</span> ` : ""}${h([[l.season, l.temperature == null ? "" : `${l.temperature}°C`].filter(Boolean).join(" · "), l.occasion, names].filter(Boolean).join(" · "))}</span></span></button>`;
}

function renderOutfit(page) {
  const d = page.draft;
  const chosen = new Set(d.item_ids);
  const pool = S.items.filter((i) => !page.filter || i.category === page.filter);
  return `<div class="topbar"><button class="btn-plain" data-act="back">Annuleer</button><span class="title">${page.logId ? "Outfit bewerken" : "Gedragen"}</span>
      <button class="btn-pill strong primary" data-act="save-outfit" ${chosen.size && !page.saving ? "" : "disabled"}>Bewaar</button></div>
    <div class="card">
      <div class="field"><label for="o-date">Datum</label><input id="o-date" type="date" data-bind="date" value="${h(d.date)}" max="${today()}"></div>
      <div class="field"><label for="o-temp">Temperatuur bij het dragen (°C)</label><input id="o-temp" type="number" inputmode="decimal" step="0.1" min="-80" max="70" data-bind="temperature" value="${d.temperature ?? ""}" placeholder="optioneel"></div>
      <div class="field"><label for="o-occ">Gelegenheid</label><input id="o-occ" data-bind="occasion" value="${h(d.occasion)}" placeholder="bv. kantoor, verjaardag"></div>
      <div class="field"><span class="label">Hoe voelde het?</span><div class="star-pick">${[1, 2, 3, 4, 5].map((n) => `<button type="button" data-act="rate" data-value="${n}" aria-pressed="${(d.rating || 0) >= n}" aria-label="${n} op 5">★</button>`).join("")}</div></div>
      <div class="field"><label for="o-note">Notitie</label><input id="o-note" data-bind="note" value="${h(d.note)}" placeholder="bv. te warm, veel complimentjes"></div></div>
    <h2 class="section-title">${chosen.size ? plural(chosen.size, "stuk", "stukken") + " gekozen" : "Kies wat je droeg"}</h2>
    ${categoryPills(S.items, page.filter || "", "outfit-filter")}
    <div class="grid">${pool.map((i) => itemTile(i, { selectable: true, selected: chosen.has(i.id) })).join("")}</div>
    ${page.logId ? `<div class="card" style="margin-top:28px"><div class="row"><button class="row-button danger" data-act="delete-log">Outfit verwijderen</button></div></div>` : ""}`;
}

function openOutfit(ids = [], extra = {}) {
  const temperature = !onTrip() && Number.isFinite(ui.weather?.current?.temp) ? ui.weather.current.temp : null;
  push({ kind: "outfit", draft: { item_ids: [...ids], date: today(), occasion: "", note: "", rating: null, temperature, ...extra } });
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

/** The AI service this account uses (Claude or ChatGPT), with its key (only a hint) and model. */
const aiProvider = () => S.ai?.provider || "claude";
const aiService = (name = aiProvider()) => S.ai?.services?.[name] || { name: "Claude", api_key: {}, model: "" };
const aiReady = () => ["app", "env"].includes(aiService().api_key?.source);

function renderSettings() {
  const user = ui.auth.user, service = aiService(), key = service.api_key || {};
  const keyText = key.source === "app" ? `Ingesteld (${h(key.hint)})` : key.source === "env" ? "Uit de serverinstellingen (.env)" : key.source === "locked" ? "Vergrendeld: log opnieuw in" : "Niet ingesteld";
  return `<h1 class="large">Instellingen</h1>
    <h2 class="section-title">Over jou</h2>
    <form class="card" data-act-submit="save-profile">
      <div class="field"><label for="s-profile">Je stijl en wat de stylist moet weten</label>
        <textarea id="s-profile" name="profile" rows="3" placeholder="bv. man, 38, werk in een creatief bureau; hou van smart casual in aardetinten; geen skinny jeans">${h(S.profile)}</textarea></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
    <p class="footnote">Gaat mee naar de stylist bij elke vraag.</p>
    <h2 class="section-title">Kleurvoorkeuren</h2>
    <div class="card pad"><div class="muted small">Lokaal afgeleid uit je goed beoordeelde outfits en bewaarde voorstellen</div>
      <p style="margin-bottom:0">${h(S.learned_colors || "Nog te weinig feedback om kleurpatronen af te leiden.")}</p></div>
    <form class="card" data-act-submit="save-color-profile"><div class="field"><label for="s-color-profile">Jouw correcties of favoriete kleuren</label>
      <textarea id="s-color-profile" name="profile" rows="2" placeholder="bv. draag graag olijfgroen; vermijd pastelgeel">${h(S.color_profile || "")}</textarea></div>
      <div class="auth-submit"><button class="btn-primary">Kleurvoorkeuren bewaren</button></div></form>
    <p class="footnote">Je correcties wegen mee bij elk outfitvoorstel; de lokale samenvatting wordt automatisch opnieuw berekend als je feedback geeft.</p>
    <div class="card" style="margin-top:12px"><div class="row"><button class="row-button" data-act="inspiration">✨ Inspiratie van Instagram en Pinterest
      <span class="muted small" style="display:block">${looks().length ? `${plural(looks().length, "outfit", "outfits")} · je stijl gaat mee naar de stylist` : "Bewaar outfits die je mooi vindt"}</span></button>
      <span class="muted" aria-hidden="true">›</span></div>
      <div class="row"><button class="row-button" data-act="rejected-list">👎 Afgekeurde outfits
      <span class="muted small" style="display:block">${S.rejected?.length ? `${plural(S.rejected.length, "outfit", "outfits")} die de stylist niet meer voorstelt` : "Nog niets afgekeurd"}</span></button>
      <span class="muted" aria-hidden="true">›</span></div></div>
    <div class="card" style="margin-top:12px"><div class="row"><button class="row-button" data-act="liked-list">♡ Bewaarde voorstellen
      <span class="muted small" style="display:block">${plural((S.liked_outfits || []).length, "outfit", "outfits")} die je mooi vindt</span></button><span class="muted" aria-hidden="true">›</span></div></div>
    <h2 class="section-title">Weer</h2>
    <form class="card" data-act-submit="save-place">
      <div class="field"><label for="s-place">Woonplaats</label><input id="s-place" name="place" value="${h(S.place?.name || "")}" placeholder="bv. Kortrijk"></div>
      <div class="auth-submit"><button class="btn-primary">Bewaren</button></div></form>
    <p class="footnote">Het weer komt van Open-Meteo, zonder sleutel of account.</p>
    <h2 class="section-title">AI-dienst</h2>
    <div class="pills" role="group" aria-label="AI-dienst" style="margin-bottom:10px">
      ${Object.entries(S.ai?.services || {}).map(([name, s]) => `<button data-act="set-provider" data-value="${name}" aria-pressed="${aiProvider() === name}">${h(s.name)}
        <span class="n">${h(s.company)}${["app", "env"].includes(s.api_key?.source) ? " · ✓" : ""}</span></button>`).join("")}</div>
    <div class="card">
      <div class="row"><span class="grow">API-sleutel voor ${h(service.name)}</span><span class="v">${keyText}</span></div>
      <form data-act-submit="save-key"><div class="field"><label for="s-key">${key.source === "app" ? "Andere sleutel" : "Sleutel"}</label>
        <input id="s-key" name="key" type="password" autocomplete="off" placeholder="${aiProvider() === "openai" ? "sk-proj-…" : "sk-ant-…"}" spellcheck="false"></div>
        <div class="auth-submit"><button class="btn-primary" ${ui.keyBusy ? "disabled" : ""}>${ui.keyBusy ? `<span class="spinner sm"></span>` : ""}Sleutel controleren en bewaren</button></div></form>
      ${key.source === "app" ? `<div class="row"><button class="row-button danger" data-act="clear-key">Sleutel verwijderen</button></div>` : ""}
      <div class="row"><span class="grow">Model</span><span class="v">${h(service.model)}</span></div>
      ${aiReady() ? `<div class="row"><button class="row-button" data-act="pick-model">Ander model kiezen</button></div>` : ""}
    </div>
    ${routingSection(service)}
    <div class="pills" role="group" aria-label="Analyse" style="margin:14px 0 0">
      ${[["standaard", "Standaard"], ["zuinig", "Zuinig"]].map(([q, label]) => `<button data-act="set-quality" data-value="${q}" aria-pressed="${(S.ai?.quality || "standaard") === q}">${label}</button>`).join("")}</div>
    <p class="footnote">${qualityNote(service)}</p>
    <h2 class="section-title">Achtergrond-AI</h2>
    <div class="card">
      <label class="row"><span class="grow">Alle achtergrond AI werk uit</span><input type="checkbox" data-act-change="background-ai-off" ${(S.background_ai?.enabled === false) ? "checked" : ""}></label>
      <div class="field"><label for="s-background-ai-interval">Frequentie van AI-samenvatting</label>
        <select id="s-background-ai-interval" data-act-change="background-ai-interval" ${S.background_ai?.enabled === false ? "disabled" : ""}>
          ${[[7,"Wekelijks"],[14,"Om de 2 weken"],[21,"Om de 3 weken"],[30,"Maandelijks"]].map(([days,label]) => `<option value="${days}" ${(S.background_ai?.interval_days || 30) === days ? "selected" : ""}>${label}</option>`).join("")}
        </select></div>
    </div>
    <p class="footnote">De AI vat je outfitgeschiedenis op deze frequentie samen als er nieuwe feedback is. Lokale verwerking blijft actief. Modetrends worden apart maandelijks opgehaald.</p>
    <div class="card"><div class="row"><button class="row-button" data-act="refresh-learning-now" ${ui.learningRefreshBusy ? "disabled" : ""}>${ui.learningRefreshBusy ? "Geschiedenis en trends worden bijgewerkt…" : "Nu outfitgeschiedenis samenvatten en trends ophalen"}</button></div></div>
    <p class="footnote">De AI-samenvatting gebruikt je ingestelde AI-dienst en kan API-kosten veroorzaken. Trends verversen maximaal eenmaal per 12 uur; normaal gebeurt dit maandelijks.</p>
    <p class="footnote">Maak een sleutel op ${h(service.console || "console.anthropic.com")}. Je kan voor beide diensten een sleutel bewaren
      en wisselen wanneer je wil; de herkenning en de stylist gebruiken de gekozen dienst. Een sleutel wordt versleuteld met je wachtwoord bewaard: niemand anders kan hem lezen.
      Een foto herkennen of een outfit vragen kost meestal minder dan een cent.</p>
    ${costsSection()}
    <h2 class="section-title">Meldingen</h2>
    ${notificationsSection()}
    <h2 class="section-title">Account</h2>
    <div class="card">
      <div class="row"><span class="grow">Ingelogd als</span><span class="v">${h(user.username)}${user.is_admin ? " (beheerder)" : ""}</span></div>
      <div class="row"><button class="row-button" data-act="password-sheet">Wachtwoord wijzigen</button></div>
      <div class="row"><button class="row-button" data-act="audit">Aanmeldingen bekijken</button></div>
      <div class="row"><button class="row-button danger" data-act="logout">Afmelden</button></div></div>
    ${user.is_admin ? usersSection() : ""}
    <p class="footnote" style="margin-top:24px;text-align:center">Dressing${S.version ? ` · versie ${h(S.version)}` : ""}</p>`;
}

const TASKS = { herkennen: "Foto herkennen", bestelling: "Bestelling lezen", stylist: "Stylist", inspiratie: "Inspiratie",
  outfitbeeld: "Outfitbeeld" };

// ChatGPT on its default model (Luna): may the stylist call in Sol for a dress code and Astra for a second try, or
// does Luna do it all. Another model is always one fixed model; Zuinig keeps it on Luna whatever is chosen here.
function routingSection(service) {
  if (aiProvider() !== "openai" || service.model !== service.default_model) return "";
  const auto = S.ai?.routing !== "fixed", m = h(service.model);
  const note = S.ai?.quality === "zuinig" ? `Zuinig staat aan: zolang dat zo is, doet ${m} alles, ook met Automatisch.`
    : auto ? `Automatisch: ${m} doet het gewone werk. Voor een dresscode of een look om na te maken zet de stylist ${h(S.ai.complex_model)} in,
      en als een antwoord niet door de controle raakt krijgt ${h(S.ai.review_model)} een tweede kans. Die kosten meer per antwoord.`
    : `Vast model: ${m} doet alles, ook een dresscode en een tweede poging. ${h(S.ai.complex_model)} en ${h(S.ai.review_model)} komen er niet aan te pas.`;
  return `<div class="pills" role="group" aria-label="Sterkere modellen" style="margin:14px 0 0">
      ${[["auto", "Automatisch"], ["fixed", "Vast model"]].map(([v, label]) => `<button data-act="set-routing" data-value="${v}" aria-pressed="${(auto ? "auto" : "fixed") === v}">${label}</button>`).join("")}</div>
    <p class="footnote">${note}</p>`;
}

// What Standaard and Zuinig really do for this service. Recognising a photo and reading an order or inspiration only
// gets cheaper when the helper model differs from the account's; at ChatGPT, Zuinig also keeps the stylist from
// calling in its stronger models for a dress code or a second try.
function qualityNote(service) {
  const thrifty = S.ai?.quality === "zuinig", m = h(service.model), helper = h(service.helper_model);
  const cheaperHelper = service.helper_model !== service.model;
  const upgrades = aiProvider() === "openai" && service.model === service.default_model && S.ai?.routing !== "fixed";
  const reading = "een foto herkennen en een bestelling of inspiratie lezen";
  if (thrifty) {
    const parts = [cheaperHelper ? `${reading} doet het goedkopere ${helper}` : `${reading} doet ${m}, net als bij Standaard`];
    parts.push(upgrades ? `de stylist blijft altijd op ${m}, ook voor een dresscode, een look om na te maken of een tweede poging`
      : `de stylist blijft op ${m}`);
    return `Zuinig: ${parts.join("; ")}.`;
  }
  const standard = upgrades ? `Standaard: ${m} herkent foto's en leest bestellingen; de stylist zet de sterkere modellen in zoals hierboven.`
    : `Standaard: alles met ${m}.`;
  const saving = [cheaperHelper && `laat ${reading} over aan het goedkopere ${helper}`,
    upgrades && `houdt de stylist altijd op ${m}`].filter(Boolean);
  return saving.length ? `${standard} Zuinig ${saving.join(" en ")}.` : `${standard} Zuinig maakt bij dit model geen verschil.`;
}

/** An amount with what is known of it: ≈ all of it priced, ≥ a part couldn't be priced, ? nothing could. */
const costText = (a) => {
  if (a.status === "unknown" && !a.cost_eur) return "?";
  const amount = euros(a.cost_eur);  // "< € 0,001" for a tiny amount: already says what it is
  return amount.startsWith("<") && a.status === "estimated" ? amount : `${a.status === "estimated" ? "≈" : "≥"} ${amount}`;
};
const costSentence = (what, a) => a.status === "unknown" && !a.cost_eur ? `Wat ${what.toLowerCase()} kostte, is niet bekend.`
  : `${what} kostte ${a.status === "estimated" ? "ongeveer" : "minstens"} ${euros(a.cost_eur)}.`;

/** What the AI cost this year: an estimate from the tokens each answer reports, times the list price; per model id
 * (each API call counted once, for the model that answered it) and per analysis. */
function costsSection() {
  const u = S.ai_usage;
  if (!u?.count) return "";
  const when = (at) => new Date(at).toLocaleString("nl-BE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const status = u.status || (u.partial ? "partial" : "estimated");
  return `<h2 class="section-title">AI-kosten in ${u.year}</h2>
    <div class="card">
      <div class="row"><span class="grow">${plural(u.count, "analyse", "analyses")}</span><span class="v">${costText({ ...u, status })}</span></div>
      ${u.per_model.map((m) => `<div class="row"><span class="grow muted">${h(m.model)}<span class="small" style="display:block">${plural(m.analyses, "analyse", "analyses")} · ${plural(m.calls, "oproep", "oproepen")}</span></span>
        <span class="v">${costText(m)}</span></div>`).join("")}
    </div>
    <h2 class="section-title">Laatste analyses</h2>
    <div class="card">${u.latest.map((a) => `<div class="row"><span class="grow">${h(TASKS[a.task] || a.task)}${a.failed ? " (mislukt)" : ""}
        <span class="muted small" style="display:block">${h(when(a.at))} · ${(a.per_model || []).map((m) => `${h(m.model)} ${costText(m)}`).join(" + ") || h(a.models)} · ${(a.input_tokens + a.output_tokens).toLocaleString("nl-BE")} tokens</span></span>
        <span class="v">${costText(a)}</span></div>`).join("")}</div>
    <p class="footnote">Een schatting: de tokens die elk antwoord meldt, maal de prijslijst. Per model telt elke oproep één keer, bij het model dat ze
      beantwoordde: samen maken ze het totaal. ≥ betekent dat een deel niet geprijsd kon worden (geen prijs, geen tokentelling of geen antwoord binnen
      de tijd), ? dat niets bekend is. Wat je echt betaalt, staat op ${h(aiService().console || "console.anthropic.com")}.</p>`;
}

// MARK: - Notifications (Web Push), as in CellarPeaks

const pushSupported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
// An iPad says it is a Mac (with a touch screen).
const onIOS = () => !/Android/.test(navigator.userAgent)
  && (/iPhone|iPad/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1));

/** Whether this device has notifications on; checked once when the settings are shown. */
async function checkPush() {
  ui.push = { ...(ui.push || {}), checked: true };
  try {
    const reg = await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription();
    ui.push.here = !!sub;
    // Same handle as the server's: the first 16 hex characters of the endpoint's SHA-256.
    const hash = sub ? new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(sub.endpoint))) : null;
    ui.push.hereId = hash ? [...hash].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16) : null;
  } catch { ui.push.here = false; }
  try { ui.push.devices = await api("/api/push/devices"); } catch { ui.push.devices = null; }
  if (ui.tab === "settings" && !stack.length) render();
}

function notificationsSection() {
  const p = ui.push || {};
  if (!pushSupported()) {
    return `<div class="card pad muted">${onIOS() && !standalone()
      ? "Meldingen werken op de iPhone enkel in de app op je beginscherm (iOS 16.4 of nieuwer). Open Dressing via dat icoon."
      : "Deze browser ondersteunt geen meldingen."}</div>`;
  }
  if (!p.checked) checkPush();
  const others = (p.devices || []).filter((d) => d.id !== p.hereId);
  return `<div class="card">
      <div class="row"><span class="grow">Hoe viel je outfit?<span class="muted small" style="display:block">${p.here ? "Aan op dit toestel" : "Uit op dit toestel"}</span></span>
        ${p.busy ? `<div class="spinner sm"></div>` : ""}</div>
      ${p.here ? `<div class="row"><button class="row-button" data-act="push-test" ${p.busy ? "disabled" : ""}>Stuur nu een melding</button></div>
        <div class="row"><button class="row-button" data-act="push-off" ${p.busy ? "disabled" : ""}>Uitzetten op dit toestel</button></div>`
        : `<div class="row"><button class="row-button" data-act="push-on" ${p.busy ? "disabled" : ""}>Meldingen aanzetten</button></div>`}
    </div>
    ${others.length ? `<div class="card" style="margin-top:12px">
      <div class="row muted small">Ook aan op:</div>
      ${others.map((d) => `<div class="row"><span class="grow">${h(d.device)}<span class="muted small" style="display:block">sinds ${h(shortDate(d.created_at.slice(0, 10)))}</span></span>
        <button class="btn-plain small" style="color:var(--red)" data-act="push-forget" data-id="${h(d.id)}">Verwijder</button></div>`).join("")}</div>` : ""}
    ${p.error ? `<div class="notice err">${h(p.error)}</div>` : ""}
    <p class="footnote">Noteer je een outfit van vandaag zonder score, dan vragen we om 20 uur hoe hij viel. Elke score helpt de stylist kiezen.</p>`;
}

function urlBytes(b64) {
  const s = atob((b64 + "=".repeat((4 - (b64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

async function pushAction(fn) {
  ui.push = { ...(ui.push || {}), busy: true, error: null }; render();
  try { await fn(); } catch (e) { ui.push.error = e.message; }
  ui.push.busy = false;
  ui.push.checked = false; // check again: this device, and the list of devices
  render();
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
  wearTable = null; // S.worn may have changed
  // Keep typing where you were in the search box or the question, cursor included.
  const active = document.activeElement, focus = active?.dataset?.bindUi || active?.dataset?.bindStylist;
  const selection = focus ? [active.selectionStart, active.selectionEnd] : null;
  $("#app").innerHTML = `<div class="screen with-nav">${navBar()}${screens[ui.tab]()}</div>`;
  if (focus) {
    const el = $(`[data-bind-ui="${focus}"], [data-bind-stylist="${focus}"]`);
    el?.focus();
    try { el?.setSelectionRange(...selection.map((n) => n ?? el.value.length)); } catch { /* not every input has a selection */ }
  }
  $("#tabbar").innerHTML = TABS.map(([id, svg, label]) => `<button data-act="tab" data-tab="${id}" ${ui.tab === id ? 'aria-current="page"' : ""}>${svg}${label}</button>`).join("");
  updateBars();
  const page = stack.at(-1), pages = $("#pages");
  document.title = `${page ? pageTitle(page) : TABS.find(([id]) => id === ui.tab)[2]} | Dressing`;
  if (!page) { pages.innerHTML = ""; document.body.style.overflow = ""; return; }
  const same = pages.firstElementChild?.dataset.key === String(page.key);
  const scroll = same ? pages.firstElementChild.scrollTop : 0;
  const views = { detail: renderDetail, add: renderAdd, edit: renderEdit, outfit: renderOutfit, order: renderOrder, trip: renderTrip, inspiration: renderInspiration };
  pages.innerHTML = `<div class="page${same ? "" : " enter"}" data-key="${page.key}"><div class="inner">${views[page.kind](page)}</div></div>`;
  pages.firstElementChild.scrollTop = scroll;
  pages.firstElementChild.addEventListener("scroll", (e) => updateBars(e.currentTarget), { passive: true });
  updateBars(pages.firstElementChild);
  document.body.style.overflow = "hidden";
}

function pageTitle(page) {
  if (page.kind === "detail") return itemById(page.id)?.name || "Kledingstuk";
  return { add: "Toevoegen", edit: "Bewerken", outfit: "Gedragen", order: "Uit een bestelmail", trip: "Valies", inspiration: "Inspiratie" }[page.kind];
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

function openLog(id) {
  const l = S.worn.find((x) => x.id === id);
  if (!l) return toast("Deze outfit staat niet meer in Gedragen.");
  push({ kind: "outfit", logId: l.id, draft: { item_ids: [...l.item_ids], date: l.date, occasion: l.occasion || "", note: l.note || "", rating: l.rating || null, temperature: l.temperature ?? null } });
}

/** What a notification is about: "gedragen=<id>" opens that outfit to give it a score. */
function openFromNotification(what) {
  const [kind, id] = what.split("=");
  if (kind === "gedragen" && id) { stack = []; ui.tab = "worn"; render(); openLog(id); }
}

async function mutate(fn) {
  try { const result = await fn(); await load(); return result ?? true; }
  catch (e) { toast(e.message); return null; }
}

// MARK: - Actions

/** The models in the picker; at ChatGPT the tested ones apart from the others. */
function modelRows(models) {
  const row = (m) => `<div class="row"><button class="row-button" data-act="set-model" data-value="${h(m.id)}">${h(m.name)}<span class="muted small" style="display:block">${
    m.recommended ? `Getest op ${h(m.checked)}` : m.failed?.length ? `Niet geschikt: ${h(m.failed.join(", "))}` : m.recommended === false ? "Niet getest" : h(m.id)}</span></button>${aiService().model === m.id ? "✓" : ""}</div>`;
  if (!models.some((m) => "recommended" in m)) return models.map(row).join("");
  const tested = models.filter((m) => m.recommended), other = models.filter((m) => !m.recommended);
  const head = (text) => `<div class="row"><span class="grow muted small">${text}</span></div>`;
  return (tested.length ? head("Aanbevolen") + tested.map(row).join("") : head("Nog geen model getest"))
    + (other.length ? head("Andere modellen") + other.map(row).join("") : "");
}

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
  order: () => { stack[stack.length - 1] = { key: ++pageSeq, kind: "order", phase: "paste", text: "", photos: [] }; render(); },
  "read-order": () => readOrder(stack.at(-1)),
  "toggle-order": (el) => { const p = stack.at(-1).pieces[Number(el.dataset.n)]; p.on = !p.on; render(); },
  "save-order": () => saveOrder(stack.at(-1)),
  manual: () => { const p = stack.at(-1); Object.assign(p, { phase: "review", draft: newDraft(), error: null }); render(); },
  async "identify-text"() { await identify(stack.at(-1), null); },
  "skip-piece": () => nextBatchPiece(stack.at(-1)),
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
    if (!page.logId) page.requestId ||= requestId();
    const draft = { ...page.draft, temperature: page.draft.temperature === "" || page.draft.temperature == null ? null : Number(page.draft.temperature) };
    const ok = await mutate(() => api(page.logId ? `/api/worn/${page.logId}` : "/api/worn",
      { method: page.logId ? "PUT" : "POST", json: { ...draft, request_id: page.logId ? "" : page.requestId } }));
    page.saving = false;
    if (ok) { stack = []; if (!page.logId) ui.tab = "worn"; render(); toast(page.logId ? "Bewaard" : "Genoteerd in Gedragen"); }
  },
  "open-log": (el) => openLog(el.dataset.id),
  "worn-last-year": () => {
    const base = ui.wornMonth ? new Date(`${ui.wornMonth}-15T12:00:00`) : new Date();
    base.setFullYear(base.getFullYear() - 1);
    ui.wornMonth = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}`;
    render();
  },
  "worn-all": () => { ui.wornMonth = ""; render(); },
  async "refresh-learning-now"() {
    if (ui.learningRefreshBusy) return;
    ui.learningRefreshBusy = true;
    render();
    try {
      const result = await slowApi("/api/settings/refresh-learning", { method: "POST" });
      await load();
      const messages = [];
      if (result.profile_updated) messages.push("outfitgeschiedenis samengevat");
      else if (result.profile_failed) messages.push("samenvatten mislukt");
      else messages.push("geen nieuwe samenvatting nodig of geen outfitgeschiedenis");
      if (result.trends_updated) messages.push("trends vernieuwd");
      else if (result.trends_failed) messages.push("trends ophalen mislukt");
      else messages.push("trends waren recent opgehaald of feeds bevatten niets nieuws");
      toast(messages.join(" · "));
    } catch (e) { toast(e.message); }
    finally { ui.learningRefreshBusy = false; render(); }
  },
  async "delete-log"() {
    const page = stack.at(-1);
    if (!confirm("Deze outfit verwijderen uit Gedragen?")) return;
    if (await mutate(() => api(`/api/worn/${page.logId}`, { method: "DELETE" }))) { stack = []; render(); }
  },
  suggest: (el) => askStylist(el.dataset.q),
  trip: () => {
    const t = S.trip;
    push({ kind: "trip", filter: t?.item_ids.length ? "packed" : "", draft: { destination: t?.place.query || "", start: t?.start || today(), end: t?.end || today() } });
  },
  "trip-mode": (el) => { ui.stylist.onTrip = el.dataset.value === "1"; Object.assign(ui.stylist, { result: null, error: null }); render(); },
  "trip-filter": (el) => { const p = stack.at(-1); p.filter = el.dataset.value === "packed" && p.filter === "packed" ? "" : el.dataset.value; render(); },
  "toggle-pack": (el) => { const ids = S.trip.item_ids, id = el.dataset.id; savePacked(ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]); },
  "pack-one": (el) => { const ids = S.trip.item_ids, id = el.dataset.id; savePacked(ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]).then(() => toast(packed(id) ? "In de valies" : "Uit de valies")); },
  "pack-outfit": (el) => { const o = ui.stylist.result.outfits[Number(el.dataset.n)]; savePacked([...new Set([...S.trip.item_ids, ...o.item_ids])]).then(() => toast("In de valies")); },
  "pack-help": () => { stack = []; ui.tab = "stylist"; ui.stylist.onTrip = true; askStylist(`Wat neem ik mee naar ${tripName()}? Help me inpakken.`); },
  async "end-trip"() {
    if (!confirm(`Je reis naar ${tripName()} afsluiten en de valies leegmaken? De kleren blijven gewoon in je dressing.`)) return;
    if (await mutate(() => api("/api/trip", { method: "DELETE" }))) { ui.stylist.onTrip = null; ui.tripWeather = undefined; stack = []; render(); toast("Welkom thuis"); }
  },
  inspiration: () => push({ kind: "inspiration", phase: "idle", url: "" }),
  "open-look": (el) => openLook(el.dataset.id),
  "copy-look": (el) => {
    const l = lookById(el.dataset.id);
    closeSheet(); stack = []; ui.tab = "stylist"; ui.stylist.onTrip = false;
    askStylist(`Maak deze look na met mijn kleren: ${l.description}`);
  },
  async "delete-look"(el) {
    if (!confirm("Dit outfit verwijderen uit Inspiratie? Je stijl blijft zoals hij is tot je hem opnieuw opmaakt.")) return;
    if (await mutate(() => api(`/api/inspiration/${el.dataset.id}`, { method: "DELETE" }))) { closeSheet(); toast("Verwijderd"); }
  },
  "edit-style": () => { stack.at(-1).editing = true; render(); },
  "cancel-style": () => { stack.at(-1).editing = false; render(); },
  "rewrite-style": () => rewriteStyle(stack.at(-1)),
  "stylist-reset": () => { Object.assign(ui.stylist, { query: "", result: null, error: null, thread: [], refine: "" }); render(); },
  "refine-suggest": (el) => askStylist(el.dataset.q, true),
  async "draw-outfit"(el) {
    const o = ui.stylist.result?.outfits[Number(el.dataset.n)];
    if (!o || o.drawing) return;
    Object.assign(o, { drawing: true, drawError: null });
    render();
    try {
      const r = await slowApi("/api/outfit-picture", { method: "POST", json: { item_ids: o.item_ids } });
      Object.assign(o, { picture: r.image, pictureCost: r.analysis || null });
    } catch (e) {
      o.drawError = e.message;
    }
    o.drawing = false;
    if (ui.tab === "stylist") render();
  },
  "reject-outfit": (el) => { ui.reject = { n: Number(el.dataset.n), reasons: [], note: "", requestId: requestId() }; openRejectSheet(); },
  "toggle-reason": (el) => { const rj = ui.reject, v = el.dataset.value; rj.reasons = rj.reasons.includes(v) ? rj.reasons.filter((x) => x !== v) : [...rj.reasons, v]; openRejectSheet(); },
  "save-reject": () => saveReject(),
  async "undo-reject"(el) {
    const o = ui.stylist.result.outfits[Number(el.dataset.n)];
    try { await api(`/api/rejected/${o.rejected.id}`, { method: "DELETE" }); } catch (e) { if (!/bestaat niet/.test(e.message)) return toast(e.message); }
    S.rejected = (S.rejected || []).filter((r) => r.id !== o.rejected.id);
    delete o.rejected; render();
  },
  "rejected-list": () => openRejectedList(),
  "liked-list": () => openLikedOutfits(),
  async "like-outfit"(el) {
    const outfit = ui.stylist.result?.outfits[Number(el.dataset.n)];
    if (!outfit) return;
    try {
      if (el.dataset.id) {
        await api(`/api/liked-outfits/${el.dataset.id}`, { method: "DELETE" });
        S.liked_outfits = (S.liked_outfits || []).filter((x) => x.id !== el.dataset.id);
        toast("Bewaard voorstel verwijderd");
      } else {
        const saved = await api("/api/liked-outfits", { method: "POST", json: {
          item_ids: outfit.item_ids, query: ui.stylist.thread.join(" → ") || ui.stylist.query,
          title: outfit.title, request_id: requestId(),
        } });
        S.liked_outfits = [saved, ...(S.liked_outfits || []).filter((x) => x.id !== saved.id)];
        toast("Onthouden: dit voorstel vind je mooi");
      }
      render();
    } catch (e) { toast(e.message); }
  },
  async "forget-liked"(el) {
    try {
      await api(`/api/liked-outfits/${el.dataset.id}`, { method: "DELETE" });
      S.liked_outfits = (S.liked_outfits || []).filter((x) => x.id !== el.dataset.id);
      openLikedOutfits();
      toast("Vergeten");
    } catch (e) { toast(e.message); }
  },
  async "forget-reject"(el) {
    try { await api(`/api/rejected/${el.dataset.id}`, { method: "DELETE" }); } catch (e) { return toast(e.message); }
    S.rejected = (S.rejected || []).filter((r) => r.id !== el.dataset.id);
    for (const o of ui.stylist.result?.outfits || []) if (o.rejected?.id === el.dataset.id) delete o.rejected;
    openRejectedList(); render();
  },
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
    if (!confirm(`Je ${aiService().name}-sleutel verwijderen?`)) return;
    await mutate(() => api(`/api/settings/api-key?provider=${aiProvider()}`, { method: "DELETE" }));
  },
  async "pick-model"() {
    let models;
    try { models = await api(`/api/settings/models?provider=${aiProvider()}`); } catch (e) { return toast(e.message); }
    openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Model</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
      <div class="card"><div class="row"><button class="row-button" data-act="set-model" data-value="">Standaard (${h(aiService().default_model)})</button></div>
      ${modelRows(models)}</div>
      <p class="footnote">${aiProvider() === "openai" ? `Aanbevolen zijn enkel modellen die een live controle doorstonden: een foto lezen, een functie aanroepen,
        een reasoning-instelling aannemen en prompt caching melden. Een niet getest model kan werken, maar dat is niet nagegaan.`
        : "Enkel modellen die foto's kunnen lezen."} Een groter model kiest wat fijner, maar kost meer.</p>`);
  },
  async "set-routing"(el) {
    if (el.dataset.value === (S.ai?.routing || "auto")) return;
    if (await mutate(() => api("/api/settings/routing", { method: "PUT", json: { routing: el.dataset.value } })))
      toast(el.dataset.value === "fixed" ? "Vast model: de stylist blijft op één model" : "Automatisch: de stylist mag sterkere modellen inzetten");
  },
  async "set-provider"(el) {
    if (el.dataset.value === aiProvider()) return;
    if (await mutate(() => api("/api/settings/provider", { method: "PUT", json: { provider: el.dataset.value } }))) toast(`${aiService().name} herkent en kiest nu je kleren`);
  },
  async "set-quality"(el) {
    if (el.dataset.value === (S.ai?.quality || "standaard")) return;
    if (await mutate(() => api("/api/settings/quality", { method: "PUT", json: { quality: el.dataset.value } }))) toast(el.dataset.value === "zuinig" ? "Zuinig staat aan" : "Standaard staat aan");
  },
  "push-on": () => pushAction(async () => {
    // Asking first, straight from the tap: iOS only shows the question for a tap.
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error(onIOS() ? "Meldingen zijn niet toegestaan. Zet ze aan bij Instellingen op je iPhone → Meldingen → Dressing."
      : "Meldingen zijn niet toegestaan. Zet ze aan bij de instellingen van je browser of gsm.");
    const reg = await navigator.serviceWorker.ready;
    const { key } = await api("/api/push/key");
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBytes(key) });
    await api("/api/push/subscribe", { method: "POST", json: { subscription: sub.toJSON() } });
    toast("Meldingen staan aan");
  }),
  "push-off": () => pushAction(async () => {
    const reg = await navigator.serviceWorker.ready, sub = await reg.pushManager.getSubscription();
    if (sub) {
      await api("/api/push/unsubscribe", { method: "POST", json: { endpoint: sub.endpoint } });
      await sub.unsubscribe();
    }
  }),
  "push-forget": (el) => pushAction(async () => { await api(`/api/push/devices/${el.dataset.id}`, { method: "DELETE" }); toast("Geen meldingen meer naar dat toestel"); }),
  "push-test": () => pushAction(async () => { await api("/api/push/test", { method: "POST" }); toast("Melding verstuurd"); }),
  async "set-model"(el) { if (await mutate(() => api("/api/settings/model", { method: "PUT", json: { model: el.dataset.value || null, provider: aiProvider() } }))) closeSheet(); },
  async audit() {
    let list;
    try { list = await api("/api/audit"); } catch (e) { return toast(e.message); }
    openSheet(`<div class="topbar"><span class="spacer"></span><span class="title" id="sheet-title">Aanmeldingen</span><button class="btn-pill strong" data-act="close-sheet">Klaar</button></div>
      <div class="card">${list.slice(0, 80).map((a) => `<div class="row"><span class="grow">${h(a.label)}<span class="muted small" style="display:block">${h(a.username)} · ${h(a.device)} · ${h(a.ip)}</span></span>
        <span class="v small">${h(new Date(a.at).toLocaleString("nl-BE", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }))}</span></div>`).join("") || `<div class="row muted">Nog niets.</div>`}</div>`);
  },
};

/** What the AI makes of a photo and/or the hint, as fields for the add page. */
async function recognize(file, hint) {
  if (!aiReady()) return { phase: "review", draft: newDraft(), file };
  const form = new FormData();
  if (file) form.append("photo", await smaller(file), "foto.jpg");
  form.append("hint", hint || "");
  try {
    const r = await slowApi("/api/identify", { method: "POST", form });
    return { phase: "review", draft: { ...newDraft(), ...r.info }, photoToken: r.photo_token, recognized: true };
  } catch (e) {
    // The photo goes along with a piece filled in by hand: keep it as preview only (it is uploaded after saving).
    return { phase: file ? "review" : "pick", draft: newDraft(), error: e.message, file };
  }
}

async function identify(page, file) {
  Object.assign(page, { phase: "analyzing", error: null, preview: file ? URL.createObjectURL(file) : null });
  render();
  Object.assign(page, await recognize(file, page.hint));
  if (stack.includes(page)) render();
}

const MAX_BATCH = 20;

/** Several photos, one piece each: the AI reads them one after the other in the background, while you check them in order. */
function identifyBatch(page, files) {
  let previous = Promise.resolve();
  page.batch = files.map((file) => {
    const result = previous.then(() => stack.includes(page) ? recognize(file, page.hint) : null); // stopped: no more AI
    previous = result;
    return { file, preview: URL.createObjectURL(file), result };
  });
  Object.assign(page, { batchAt: 0, batchSaved: 0 });
  showBatchPiece(page);
}

async function showBatchPiece(page) {
  const at = page.batchAt, piece = page.batch[at];
  Object.assign(page, { phase: "analyzing", error: null, preview: piece.preview, draft: null, photoToken: null, file: null, recognized: false, requestId: null, saving: false });
  render(); scrollTo(0, 0);
  const found = await piece.result;
  if (!found || page.batchAt !== at || !stack.includes(page)) return;
  Object.assign(page, found);
  page.draft.location ||= ui.lastLocation || ""; // read before the previous pieces were saved
  render();
}

/** On to the next photo, or done: back to the dressing with what was added. */
function nextBatchPiece(page) {
  if (page.batchAt < page.batch.length - 1) { page.batchAt++; return showBatchPiece(page); }
  const added = page.batchSaved;
  stack = []; ui.tab = "dressing"; ui.category = ""; ui.search = ""; ui.sort = "added";
  render(); scrollTo(0, 0);
  toast(added ? `${plural(added, "stuk", "stukken")} toegevoegd aan je dressing` : "Niets toegevoegd");
}

const submits = {
  async login(form) { await authSubmit("/api/auth/login", form); },
  async setup(form) {
    if (form.password.value !== form.password2.value) { ui.authError = "De wachtwoorden zijn niet gelijk."; return render(); }
    await authSubmit("/api/auth/setup", form);
  },
  ask(form) { askStylist(form.query.value); },
  refine(form) { askStylist(form.query.value, true); },
  async "save-trip"(form) {
    const page = stack.at(-1);
    Object.assign(page.draft, { destination: form.destination.value, start: form.start.value, end: form.end.value });
    page.saving = true; render();
    const r = await mutate(() => api("/api/trip", { method: "PUT", json: page.draft }));
    page.saving = false;
    if (r) { ui.tripWeather = undefined; ui.stylist.onTrip = null; page.filter = ""; toast(`Valies voor ${tripName()}`); }
    render();
  },
  async "save-new"() { await saveItem(stack.at(-1), true); },
  async "save-edit"() { await saveItem(stack.at(-1), false); },
  async "set-location"(form) {
    if (await mutate(() => api(`/api/items/${form.dataset.id}`, { method: "PATCH", json: { location: form.location.value } }))) { closeSheet(); toast("Plaats bewaard"); }
  },
  "inspiration-link"(form) { addInspiration(stack.at(-1), null, form.url.value.trim()); },
  async "save-style"(form) {
    const page = stack.at(-1);
    if (await mutate(() => api("/api/settings/style-profile", { method: "PUT", json: { profile: form.profile.value } }))) { page.editing = false; render(); toast("Bewaard"); }
  },
  async "save-profile"(form) { if (await mutate(() => api("/api/settings/profile", { method: "PUT", json: { profile: form.profile.value } }))) toast("Bewaard"); },
  async "save-color-profile"(form) { if (await mutate(() => api("/api/settings/color-profile", { method: "PUT", json: { profile: form.profile.value } }))) toast("Kleurvoorkeuren bewaard"); },
  async "save-place"(form) {
    const r = await mutate(() => api("/api/settings/place", { method: "PUT", json: { place: form.place.value } }));
    if (r) { toast(r.place ? `Weer van ${r.place.name}` : "Woonplaats gewist"); ui.weather = undefined; if (r.place) loadWeather(); }
  },
  async "save-key"(form) {
    ui.keyBusy = true; render();
    const ok = await mutate(() => api("/api/settings/api-key", { method: "PUT", json: { key: form.key.value, provider: aiProvider() } }));
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
    if (isNew) page.requestId ||= requestId(); // the same id on a retry of this save
    const item = isNew ? await api("/api/items", { method: "POST", json: { data, photo_token: page.photoToken || "", request_id: page.requestId } })
      : await api(`/api/items/${page.id}`, { method: "PATCH", json: data });
    if (page.file && !page.photoToken) { // a photo the AI couldn't read: still the piece's photo
      const form = new FormData(); form.append("photo", await smaller(page.file), "foto.jpg");
      await api(`/api/items/${item.id}/photo`, { method: "POST", form });
    }
    if (data.location) ui.lastLocation = data.location; // the next piece probably hangs in the same place
    await load();
    if (isNew && page.batch) { page.batchSaved++; return nextBatchPiece(page); }
    stack =isNew ? [{ key: ++pageSeq, kind: "detail", id: item.id }] : stack.slice(0, -1);
    render();
    toast(isNew ? "Toegevoegd aan je dressing" : "Bewaard", isNew ? { label: "Nog een", run: () => actions.add() } : null);
  } catch (e) { page.error = e.message; page.saving = false; render(); }
}

const changes = {
  "photo-chosen": (el) => {
    const files = [...(el.files || [])], page = stack.at(-1);
    if (files.length > MAX_BATCH) { page.error = `Kies hoogstens ${MAX_BATCH} foto's tegelijk.`; return render(); }
    if (files.length > 1) return identifyBatch(page, files);
    const file = files[0];
    if (file) aiReady() ? identify(page, file) : Object.assign(page, { phase: "review", draft: newDraft(), file, preview: URL.createObjectURL(file) }) && render();
  },
  async "replace-photo"(el) {
    const file = el.files?.[0], page = stack.at(-1);
    if (!file) return;
    const form = new FormData(); form.append("photo", await smaller(file), "foto.jpg");
    if (await mutate(() => api(`/api/items/${page.id}/photo`, { method: "POST", form }))) { page.preview = null; render(); toast("Foto vervangen"); }
  },
  "order-pdf": (el) => { const file = el.files?.[0]; if (file) readOrder(stack.at(-1), file); },
  "order-shots": (el) => {
    const files = [...(el.files || [])];
    if (files.length > 6) { stack.at(-1).error = "Kies hoogstens 6 screenshots."; return render(); }
    if (files.length) readOrder(stack.at(-1), null, files);
  },
  "inspiration-shots": (el) => {
    const files = [...(el.files || [])];
    if (files.length > 6) { stack.at(-1).error = "Kies hoogstens 6 screenshots."; return render(); }
    if (files.length) addInspiration(stack.at(-1), files);
  },
  "use-weather": (el) => { ui.stylist.useWeather = el.checked; },
  "worn-month": (el) => { ui.wornMonth = el.value; render(); },
  async "background-ai-off"(el) {
    const config = S.background_ai || { enabled: true, interval_days: 30 };
    if (await mutate(() => api("/api/settings/background-ai", { method: "PUT", json: { enabled: !el.checked, interval_days: config.interval_days || 30 } }))) toast(el.checked ? "Achtergrond-AI uitgeschakeld" : "Achtergrond-AI ingeschakeld");
  },
  async "background-ai-interval"(el) {
    const config = S.background_ai || { enabled: true, interval_days: 30 };
    await mutate(() => api("/api/settings/background-ai", { method: "PUT", json: { enabled: config.enabled !== false, interval_days: Number(el.value) } }));
  },
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
  if (el.dataset.piece) stack.at(-1).pieces[Number(el.dataset.piece)].draft[el.dataset.field] = el.value;
  if (el.dataset.paste === "order") { const p = stack.at(-1); p.text = el.value; if (!el.value.trim()) p.photos = []; }
  if (el.dataset.orderAll !== undefined) { // one place for every piece, except one given its own place
    const page = stack.at(-1), old = page.all;
    page.all = el.value;
    page.pieces.forEach((p, n) => { if (p.draft.location === old) { p.draft.location = el.value; const input = document.getElementById(`o-loc-${n}`); if (input) input.value = el.value; } });
  }
  if (el.dataset.bindPage) { const p = stack.at(-1); p[el.dataset.bindPage] = el.value; if (el.dataset.bindPage === "hint") { const had = !!e.target.closest(".page").querySelector('[data-act="identify-text"]'); if (had !== !!el.value) render(); } }
  if (el.dataset.bindUi) {
    ui[el.dataset.bindUi] = el.value;
    const results = $("#dressing-results");
    if (results) results.innerHTML = dressingResults(); else render();
  }
  if (el.dataset.bindStylist) ui.stylist[el.dataset.bindStylist] = el.value;
  if (el.dataset.bindReject) ui.reject[el.dataset.bindReject] = el.value;
});
// A mail pasted with its layout: keep where the pictures stood, as [foto N].
document.addEventListener("paste", (e) => {
  if (e.target.dataset?.paste !== "order") return;
  const html = e.clipboardData?.getData("text/html");
  if (!html) return;
  const { text, photos } = mailText(html);
  if (!text) return;
  e.preventDefault();
  const page = stack.at(-1);
  Object.assign(page, { text, photos, error: null });
  render();
});
// Enter sends the question; Shift+Enter makes a new line.
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && e.target.dataset?.bindStylist) { e.preventDefault(); askStylist(e.target.value, e.target.dataset.bindStylist === "refine"); }
});
addEventListener("scroll", () => updateBars(), { passive: true });
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && ui.auth?.user && !stack.length) load(); });
window.addEventListener("unhandledrejection", (e) => { console.error(e.reason); toast(`Er liep iets mis: ${e.reason?.message || e.reason}`); });

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
navigator.serviceWorker?.addEventListener("message", (e) => { if (typeof e.data?.open === "string" && ui.auth?.user) openFromNotification(e.data.open); });
boot().then(() => {
  const what = location.hash.slice(1);
  if (!what) return;
  history.replaceState(null, "", location.pathname); // a reload doesn't open it again
  if (ui.auth?.user) openFromNotification(what);
});
