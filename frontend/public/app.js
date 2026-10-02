import {drawTarget, TARGETS} from "/target.js";

const number = new Intl.NumberFormat("de-DE", {maximumFractionDigits: 1});
const decimal = new Intl.NumberFormat("de-DE", {minimumFractionDigits: 1, maximumFractionDigits: 1});
export const formatScore = score => score ? (score.unit === "ZehntelRing" ? decimal : number).format(score.value / score.scale) : "—";
const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  if (className === "shooter") node.title = text;
  return node;
};
let stream;
let previewSnapshot;
let profiles = [];
const sponsorChoices = new Map();
const params = new URLSearchParams(location.search);
const range = location.pathname.startsWith("/range/") ? Number(location.pathname.split("/")[2]) : null;
const requestedObs = params.get("obs");
const obs = ["1", "2"].includes(requestedObs) ? requestedObs : null;
const preview = params.get("preview") === "1";
document.body.classList.toggle("obs", obs || preview);
document.body.classList.toggle("obs-cards", obs === "2");
document.body.classList.toggle("preview", preview);

function tile(entry, profile, sponsors) {
  const card = element("article", "range-tile");
  card.dataset.lane = entry.lane;
  const state = entry.target;
  const occupancy = entry.occupancy || "unknown";
  const discipline = state && profile.fields.includes("discipline") ? state.discipline || "Disziplin unbekannt" : null;
  const top = element("div", "tile-top");
  const heading = element("div", "range-heading");
  const laneLink = element("a", "lane", `Stand ${entry.lane}`);
  laneLink.href = `/range/${entry.lane}?profile=${encodeURIComponent(profile.id)}${obs ? `&obs=${obs}` : ""}`;
  laneLink.title = "Einzelstand anzeigen";
  heading.append(laneLink);
  if (discipline && profile.discipline_inline) {
    const label = element("span", "discipline inline", discipline);
    label.title = discipline;
    heading.append(label);
  }
  top.append(heading);
  top.append(element("span", `phase ${state ? (state.practice ? "practice" : "scored") : ""}`, state ? (state.practice ? "Probe" : `Wertung${state.position > 1 ? ` · ${state.position}` : ""}`) : ({free: "Frei", occupied: "Belegt", unknown: "Belegung unbekannt"}[occupancy])));
  card.append(top);
  if (!state) {
    if (occupancy === "occupied" && entry.live_shooter && profile.fields.includes("shooter")) card.append(element("h2", "shooter", entry.live_shooter));
    const empty = element("div", "target-empty");
    const emptyMessage = element("p", "", occupancy === "free" ? "Stand frei" : "Noch keine aktuelle Scheibe");
    if (sponsors.length) {
      let sponsor = sponsors.find(image => image.id === sponsorChoices.get(entry.lane));
      if (!sponsor) {
        sponsor = sponsors[Math.floor(Math.random() * sponsors.length)];
        sponsorChoices.set(entry.lane, sponsor.id);
      }
      empty.classList.add("sponsored");
      const image = element("img", "sponsor-image");
      image.src = sponsor.url;
      image.alt = `Sponsor: ${sponsor.name}`;
      image.onerror = () => { image.remove(); empty.classList.remove("sponsored"); empty.prepend(element("span", "empty-symbol", "◎")); };
      empty.append(emptyMessage, image);
      if (sponsor.text) empty.append(element("div", "sponsor-caption", sponsor.text));
    } else {
      empty.append(element("span", "empty-symbol", "◎"), emptyMessage, element("small", "", occupancy === "occupied" ? "Warten auf Ergebnisexport" : "Stand bleibt im Profil sichtbar"));
    }
    card.append(empty);
    return card;
  }
  sponsorChoices.delete(entry.lane);
  if (profile.fields.includes("shooter")) card.append(element("h2", "shooter", state.shooter || "Unbekannt"));
  if (discipline && !profile.discipline_inline) card.append(element("div", "discipline", discipline));
  const stats = element("div", "tile-stats");
  if (profile.fields.includes("count")) {
    const item = element("div", "stat");
    item.append(element("span", "stat-label", "Treffer"), element("strong", "", state.shot_count));
    stats.append(item);
  }
  if (profile.fields.includes("latest")) {
    const item = element("div", "stat last-score");
    item.append(element("span", "stat-label", "Letzter Schuss"), element("strong", "", formatScore(state.latest?.score)));
    stats.append(item);
  }
  card.append(stats);
  const target = element("div", "target-view");
  if (TARGETS[state.target_kind]) {
    target.append(drawTarget(state, profile));
    if (state.practice) {
      const marker = element("span", "practice-marker");
      marker.setAttribute("role", "img");
      marker.setAttribute("aria-label", "Probestellung");
      target.append(marker);
    }
    if (state.series_pending) target.append(element("span", "target-note", "Serienzuordnung wird geladen"));
    else if (state.shot_count && !state.shots.some(hit => hit.x_mm !== null)) target.append(element("span", "target-note", "Trefferlage verdeckt oder noch nicht bestätigt"));
  } else {
    target.classList.add("unsupported");
    target.append(element("span", "empty-symbol", "◎"), element("p", "", "Scheibentyp nicht unterstützt"), element("small", "", "Ergebnisse bleiben sichtbar"));
  }
  card.append(target);
  const showSeries = profile.fields.includes("series") && state.series.length > 1;
  const showTotal = profile.fields.includes("total");
  if (showSeries) {
    const series = element("table", "series");
    series.append(element("caption", "sr-only", showTotal ? "Serien und Gesamt" : "Serien"));
    const headers = element("tr");
    const values = element("tr");
    for (const item of [...state.series, ...(showTotal ? [{number: null, score: state.total}] : [])]) {
      const className = item.number === null ? "series-total" : "";
      const header = element("th", className, item.number === null ? "Gesamt" : `S${item.number}`);
      header.scope = "col";
      headers.append(header);
      values.append(element("td", className, formatScore(item.score)));
    }
    const head = element("thead");
    const body = element("tbody");
    head.append(headers);
    body.append(values);
    series.append(head, body);
    card.append(series);
  }
  if (showTotal && !showSeries) {
    const total = element("div", "total");
    total.append(element("span", "", "Gesamt"), element("strong", "", formatScore(state.total)));
    card.append(total);
  }
  return card;
}

function render(snapshot) {
  snapshot = previewSnapshot || snapshot;
  const profile = snapshot.profile;
  for (const [name, color] of Object.entries(profile.theme)) document.documentElement.style.setProperty(`--${name}`, color);
  const title = range === null ? profile.name : `Stand ${range} · ${profile.name}`;
  document.getElementById("profile-name").textContent = title;
  document.title = `${title} · Meyton Live Display`;
  const grid = document.getElementById("ranges");
  const known = new Map(snapshot.rows.flat().map(entry => [entry.lane, entry]));
  grid.style.gridTemplateRows = `repeat(${profile.rows.length}, minmax(0, 1fr))`;
  grid.replaceChildren(...profile.rows.map(row => {
    const nodes = element("section", "range-row");
    nodes.setAttribute("aria-label", `Stände ${row.join(", ")}`);
    nodes.style.gridTemplateColumns = `repeat(${row.length}, minmax(0, 1fr))`;
    nodes.append(...row.map(lane => tile(known.get(lane) || {lane, target: null}, profile, snapshot.sponsors || [])));
    return nodes;
  }));
  const connected = Object.values(snapshot.sources).every(source => source.state === "connected");
  const status = document.getElementById("connection");
  status.className = connected ? "connected" : "degraded";
  status.textContent = connected ? (snapshot.sources.demo ? "Demobetrieb · simulierte Daten" : "Datenquellen verbunden") : "Verbindung eingeschränkt";
  const sources = Object.entries(snapshot.sources).map(([name, source]) => {
    const time = source.last_check_at ? new Date(source.last_check_at).toLocaleTimeString("de-DE", {timeZone: "Europe/Berlin"}) : "—";
    return `${{sdf: "SDF", db: "SSMDB2", lana: "LANA", demo: "Demo", worker: "Worker", local_db: "Lokale DB"}[name] || name}: ${source.message} · geprüft ${time}`;
  });
  document.getElementById("source-status").textContent = sources.join("  |  ");
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.dispatchEvent(new CustomEvent("meyton-render", {detail: {
      revision: snapshot.revision, rendered_at: Date.now(),
      shots: snapshot.rows.flat().filter(entry => entry.target?.latest && TARGETS[entry.target.target_kind]).map(entry => ({
        lane: entry.lane, target_id: entry.target.id, ...entry.target.latest,
      })),
    }}));
  }));
}

async function loadProfiles() {
  const response = await fetch("/api/profiles", {cache: "no-store"});
  if (!response.ok) throw new Error("Profile konnten nicht geladen werden");
  profiles = await response.json();
  const select = document.getElementById("profiles");
  select.replaceChildren(...profiles.map(profile => {
    const option = element("option", "", profile.name);
    option.value = profile.id;
    return option;
  }));
  return profiles;
}

async function connect(id) {
  if (stream) stream.close();
  document.getElementById("profiles").value = id;
  const response = await fetch(`${range === null ? "/api/snapshot" : `/api/ranges/${range}`}?profile=${encodeURIComponent(id)}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Profil nicht gefunden");
  render(await response.json());
  stream = new EventSource(`/api/events?profile=${encodeURIComponent(id)}${range === null ? "" : `&lane=${range}`}`);
  stream.addEventListener("snapshot", event => {
    const data = JSON.parse(event.data);
    render(data);
    const existing = profiles.find(profile => profile.id === data.profile.id);
    if (!existing || existing.name !== data.profile.name) loadProfiles().then(() => { document.getElementById("profiles").value = id; });
  });
  stream.addEventListener("deleted", async () => {
    stream.close();
    await loadProfiles();
    location.assign(displayUrl(profiles[0].id));
  });
  stream.onerror = () => {
    const status = document.getElementById("connection");
    status.className = "degraded";
    status.textContent = "Anzeige getrennt · Ergebnisse bleiben erhalten";
  };
}

function displayUrl(id) {
  return range === null ? `/display/${id}${obs ? `?obs=${obs}` : ""}` : `/range/${range}?profile=${encodeURIComponent(id)}${obs ? `&obs=${obs}` : ""}`;
}
document.getElementById("profiles").addEventListener("change", event => location.assign(displayUrl(event.target.value)));
document.getElementById("fullscreen").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch { document.getElementById("connection").textContent = "Vollbild ist hier nicht verfügbar"; }
});
if (preview) window.addEventListener("message", event => {
  if (event.origin !== location.origin || event.source !== parent || event.data?.type !== "profile-preview") return;
  previewSnapshot = event.data.snapshot;
  render(previewSnapshot);
});
function clock() { document.getElementById("clock").textContent = new Date().toLocaleTimeString("de-DE", {timeZone: "Europe/Berlin"}); }
clock();
setInterval(clock, 1000);
async function start() {
  try {
    await loadProfiles();
    if (range !== null && (!Number.isInteger(range) || range < 1 || range > 32767)) throw new Error("Standnummer ungültig");
    const id = location.pathname.startsWith("/display/") ? location.pathname.split("/")[2] : params.get("profile") || profiles[0].id;
    await connect(id);
  } catch (error) {
    document.getElementById("connection").textContent = error.message;
    document.getElementById("connection").className = "degraded";
    setTimeout(start, 3000);
  }
}
start();
