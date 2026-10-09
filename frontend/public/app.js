import {drawTarget, setTargetCatalog, TARGETS} from "/target.js";
import {updateLogo} from "/branding.js";
import {checkRuntime} from "/reload.js";
import {publicDisplay, loadPublication, publicationCatalog, publicationProfiles, publicationSnapshot, publicationStatus} from "/publication.js";

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
let ruleCatalogRevision;
const sponsorChoices = new Map();
const shotHighlights = new Map();
const params = new URLSearchParams(location.search);
const range = location.pathname.startsWith("/range/") ? Number(location.pathname.split("/")[2]) : null;
const requestedObs = params.get("obs");
const obs = ["1", "2"].includes(requestedObs) ? requestedObs : null;
const preview = params.get("preview") === "1";
document.body.classList.toggle("obs", obs || preview);
document.body.classList.toggle("obs-cards", obs === "2");
document.body.classList.toggle("preview", preview);

function assignSponsors(lanes, sponsors) {
  const visible = new Set(lanes);
  const counts = new Map(sponsors.map(image => [image.id, 0]));
  for (const [lane, id] of sponsorChoices) {
    if (!visible.has(lane) || !counts.has(id)) sponsorChoices.delete(lane);
    else counts.set(id, counts.get(id) + 1);
  }
  if (!counts.size) return;
  const choose = count => {
    const candidates = [...counts.keys()].filter(id => counts.get(id) === count);
    return candidates[Math.floor(Math.random() * candidates.length)];
  };
  for (const lane of lanes) {
    if (sponsorChoices.has(lane)) continue;
    const id = choose(Math.min(...counts.values()));
    sponsorChoices.set(lane, id);
    counts.set(id, counts.get(id) + 1);
  }
  while (Math.max(...counts.values()) - Math.min(...counts.values()) > 1) {
    const from = choose(Math.max(...counts.values()));
    const to = choose(Math.min(...counts.values()));
    const lane = [...sponsorChoices.keys()].find(lane => sponsorChoices.get(lane) === from);
    sponsorChoices.set(lane, to);
    counts.set(from, counts.get(from) - 1);
    counts.set(to, counts.get(to) + 1);
  }
}

function tile(entry, profile, sponsors) {
  const card = element("article", "range-tile");
  card.dataset.lane = entry.lane;
  const state = entry.target;
  const highlight = shotHighlights.get(entry.lane);
  if (["background", "border"].includes(profile.shot_highlight) && highlight?.until > Date.now()) {
    const className = `shot-${profile.shot_highlight}`;
    card.classList.add(className);
    setTimeout(() => card.classList.remove(className), highlight.until - Date.now());
  }
  const occupancy = entry.occupancy || "unknown";
  const discipline = state && profile.fields.includes("discipline") ? state.discipline || "Disziplin unbekannt" : null;
  const top = element("div", "tile-top");
  const heading = element("div", "range-heading");
  const laneLink = element(publicDisplay ? "span" : "a", "lane", `Stand ${entry.lane}`);
  if (!publicDisplay) {
    laneLink.href = `/range/${entry.lane}?profile=${encodeURIComponent(profile.id)}${obs ? `&obs=${obs}` : ""}`;
    laneLink.title = "Einzelstand anzeigen";
  }
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
    const sponsor = sponsors.find(image => image.id === sponsorChoices.get(entry.lane));
    if (sponsor) {
      empty.classList.add("sponsored");
      const image = element("img", "sponsor-image");
      image.src = sponsor.url;
      image.alt = `Sponsor: ${sponsor.name}`;
      image.onerror = () => { image.remove(); empty.classList.remove("sponsored"); empty.prepend(element("span", "empty-symbol", "◎")); };
      empty.append(emptyMessage);
      const topText = sponsor.top_text ?? "mit freundlicher Unterstützung durch";
      if (topText) empty.append(element("div", "sponsor-caption", topText));
      empty.append(image);
      if (sponsor.text) empty.append(element("div", "sponsor-caption", sponsor.text));
    } else {
      empty.append(element("span", "empty-symbol", "◎"), emptyMessage, element("small", "", occupancy === "occupied" ? "Warten auf Ergebnisexport" : "Stand bleibt im Profil sichtbar"));
    }
    card.append(empty);
    return card;
  }
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
  if (TARGETS[state.target_rule]) {
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

function render(snapshot, liveUpdate = false) {
  if (checkRuntime(snapshot.runtime_id)) return;
  if (snapshot.rule_catalog_revision !== undefined && snapshot.rule_catalog_revision !== ruleCatalogRevision) {
    loadTargetCatalog().then(() => render(snapshot, liveUpdate));
    return;
  }
  snapshot = previewSnapshot || snapshot;
  updateLogo(snapshot.logo);
  const profile = snapshot.profile;
  for (const [name, color] of Object.entries(profile.theme)) document.documentElement.style.setProperty(`--${name}`, color);
  const title = range === null ? profile.name : `Stand ${range} · ${profile.name}`;
  document.getElementById("profile-name").textContent = title;
  document.title = `${title} · Meyton Live Display`;
  const grid = document.getElementById("ranges");
  const known = new Map(snapshot.rows.flat().map(entry => [entry.lane, entry]));
  const celebrations = [];
  for (const entry of known.values()) {
    const state = entry.target;
    const key = state?.shot_count ? JSON.stringify([state.id, state.position, state.shot_count, state.last_shot_at]) : null;
    const previous = shotHighlights.get(entry.lane);
    const seen = previous && previous.targetId === state?.id ? previous.seen : new Set();
    for (const hit of [...(state?.shots || []), ...(state?.latest ? [state.latest] : [])]) {
      const hitKey = JSON.stringify([hit.position, hit.number, hit.timestamp]);
      if (liveUpdate && previous && !seen.has(hitKey) && profile.confetti_enabled && !hit.invalid &&
          hit.score && ["Ring", "ZehntelRing"].includes(hit.score.unit) &&
          hit.score.value / hit.score.scale >= (profile.confetti_threshold ?? 10.5)) celebrations.push(entry.lane);
      // A new concealed shot can qualify later when SSMDB2 confirms its score.
      if (!liveUpdate || !profile.confetti_enabled || hit.score || hit.invalid) seen.add(hitKey);
    }
    shotHighlights.set(entry.lane, {key, seen, targetId: state?.id, until: previous && key && key !== previous.key ? Date.now() + 3000 : key ? previous?.until || 0 : 0});
  }
  const liveConnected = ["worker", "local_db"].every(name => snapshot.sources[name]?.state === "connected") &&
    ["lana", "demo"].some(name => snapshot.sources[name]?.state === "connected");
  // ponytail: missing live status approximates unavailable; use an explicit power flag if LANA exposes one.
  const rows = profile.rows.map(row => row.filter(lane => !profile.hide_unavailable || !liveConnected ||
    ["free", "occupied"].includes(known.get(lane)?.occupancy))).filter(row => row.length);
  assignSponsors(rows.flat().filter(lane => !known.get(lane)?.target), snapshot.sponsors || []);
  grid.style.gridTemplateRows = rows.length ? `repeat(${rows.length}, minmax(0, 1fr))` : "none";
  grid.replaceChildren(...rows.map(row => {
    const nodes = element("section", "range-row");
    nodes.setAttribute("aria-label", `Stände ${row.join(", ")}`);
    nodes.style.gridTemplateColumns = `repeat(${row.length}, minmax(0, 1fr))`;
    nodes.append(...row.map(lane => tile(known.get(lane) || {lane, target: null}, profile, snapshot.sponsors || [])));
    return nodes;
  }));
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) for (const lane of celebrations) {
    const card = grid.querySelector(`[data-lane="${lane}"]`);
    if (card) {
      const bounds = card.getBoundingClientRect();
      window.confetti({size: 2, position: {x: bounds.left + bounds.width / 2, y: bounds.top + bounds.height / 3}, fade: true});
    }
  }
  const connected = Object.entries(snapshot.sources).every(([name, source]) => source.state === "connected" || (name === "sdf" && source.state === "disabled"));
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
      shots: snapshot.rows.flat().filter(entry => entry.target?.latest && TARGETS[entry.target.target_rule]).map(entry => ({
        lane: entry.lane, target_id: entry.target.id, ...entry.target.latest,
      })),
    }}));
  }));
}

async function loadProfiles() {
  if (publicDisplay) profiles = publicationProfiles();
  else {
    const response = await fetch("/api/profiles", {cache: "no-store"});
    if (!response.ok) throw new Error("Profile konnten nicht geladen werden");
    profiles = await response.json();
  }
  const select = document.getElementById("profiles");
  select.replaceChildren(...profiles.map(profile => {
    const option = element("option", "", profile.name);
    option.value = profile.id;
    return option;
  }));
  return profiles;
}

async function loadTargetCatalog() {
  let catalog;
  if (publicDisplay) catalog = publicationCatalog();
  else {
    const response = await fetch("/api/target-rules", {cache: "no-store"});
    if (!response.ok) throw new Error("Regelkatalog konnte nicht geladen werden");
    catalog = await response.json();
  }
  setTargetCatalog(catalog);
  ruleCatalogRevision = catalog.revision;
}

async function connectPublic(id) {
  let first = true;
  async function poll() {
    try {
      await loadPublication();
      await loadTargetCatalog();
      await loadProfiles();
      const snapshot = publicationSnapshot(id, range);
      if (!snapshot) {
        document.getElementById("ranges").replaceChildren();
        if (profiles.length && !profiles.some(profile => profile.id === id)) {
          location.assign(displayUrl(profiles[0].id));
          return;
        }
        throw new Error("Profil oder Stand nicht veröffentlicht");
      }
      document.getElementById("profiles").value = id;
      if (publicationStatus()) render(snapshot, !first);
      first = false;
      publicationStatus();
    } catch (error) {
      const status = document.getElementById("connection");
      status.className = "degraded";
      status.textContent = error.message;
      publicationStatus();
    }
    setTimeout(poll, 2000);
  }
  poll();
}

async function connect(id) {
  if (publicDisplay) return connectPublic(id);
  if (stream) stream.close();
  document.getElementById("profiles").value = id;
  const response = await fetch(`${range === null ? "/api/snapshot" : `/api/ranges/${range}`}?profile=${encodeURIComponent(id)}`, {cache: "no-store"});
  if (!response.ok) throw new Error("Profil nicht gefunden");
  render(await response.json());
  stream = new EventSource(`/api/events?profile=${encodeURIComponent(id)}${range === null ? "" : `&lane=${range}`}`);
  stream.addEventListener("snapshot", event => {
    const data = JSON.parse(event.data);
    render(data, true);
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
if (publicDisplay) {
  document.querySelector('a[href="/admin"]')?.remove();
  setInterval(publicationStatus, 1000);
}
async function start() {
  try {
    if (publicDisplay) await loadPublication();
    await loadTargetCatalog();
    await loadProfiles();
    if (range !== null && (!Number.isInteger(range) || range < 1 || range > 32767)) throw new Error("Standnummer ungültig");
    if (!profiles.length) throw new Error("Keine Profile veröffentlicht");
    const id = location.pathname.startsWith("/display/") ? location.pathname.split("/")[2] : params.get("profile") || profiles[0].id;
    await connect(id);
  } catch (error) {
    document.getElementById("connection").textContent = error.message;
    document.getElementById("connection").className = "degraded";
    setTimeout(start, 3000);
  }
}
start();
