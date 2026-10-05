import {updateLogo} from "/branding.js";
import {checkRuntime} from "/reload.js";

const labels = {shooter: "Schütze", discipline: "Disziplin", count: "Trefferzahl", latest: "Letzter Schuss", total: "Gesamt", series: "Serien"};
const fields = Object.keys(labels);
let profiles = [];
let current;
let creating = false;
let previewVersion = 0;
let previewTimer;
const byId = id => document.getElementById(id);
new ResizeObserver(entries => {
  byId("preview").style.transform = `scale(${entries[0].contentRect.width / 1920})`;
}).observe(document.querySelector(".preview-frame"));
const message = (text, error = false) => {
  byId("save-status").textContent = text;
  byId("save-status").className = error ? "error" : "";
};

for (const [field, label] of Object.entries(labels)) {
  const node = document.createElement("label");
  node.className = "checkbox";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = field;
  node.append(input, document.createTextNode(label));
  byId("field-options").append(node);
}

async function api(path, options = {}, root = "/api/admin/profiles") {
  const response = await fetch(`${root}${path}`, {cache: "no-store", ...options, headers: {"Content-Type": "application/json", ...options.headers}});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "Eingaben ungültig. Standnummern, Profil-ID und Zeilen prüfen.");
  }
  return response.status === 204 ? null : response.json();
}

function list() {
  byId("profile-list").replaceChildren(...profiles.map(profile => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `profile-choice ${current?.id === profile.id && !creating ? "selected" : ""}`;
    button.setAttribute("aria-pressed", String(current?.id === profile.id && !creating));
    const name = document.createElement("strong");
    name.textContent = profile.name;
    const detail = document.createElement("small");
    detail.textContent = `${profile.rows.flat().length} Stände · ${profile.rows.length} ${profile.rows.length === 1 ? "Zeile" : "Zeilen"}`;
    button.append(name, detail);
    button.onclick = () => select(profile);
    return button;
  }));
}

function draft() {
  const rows = byId("rows").value.split(/\n/).filter(line => line.trim()).map(line => {
    if (!/^\s*\d+(\s*,\s*\d+)*\s*$/.test(line)) throw new Error("Standnummern durch Kommas trennen.");
    return line.split(",").map(value => Number(value.trim()));
  });
  const lanes = rows.flat();
  if (!rows.length || rows.length > 8 || rows.some(row => row.length > 12) || new Set(lanes).size !== lanes.length || lanes.some(lane => lane < 1 || lane > 32767)) throw new Error("1–8 Zeilen, 1–12 eindeutige Stände pro Zeile.");
  return {id: byId("profile-id").value, name: byId("name").value,
    rows, theme: Object.fromEntries(Object.keys(current.theme).map(color => [color, byId(`color-${color}`).value])),
    fields: [...byId("field-options").querySelectorAll("input:checked")].map(input => input.value),
    hits: byId("hits").value, zoom: byId("zoom").value, practice: byId("practice").checked,
    discipline_inline: byId("discipline-inline").checked,
    shot_highlight: byId("shot-highlight").value, hide_unavailable: byId("hide-unavailable").checked,
    confetti_enabled: byId("confetti-enabled").checked, confetti_threshold: byId("confetti-threshold").valueAsNumber};
}

async function updatePreview() {
  const version = ++previewVersion;
  try {
    const data = draft();
    const base = `${location.origin}/display/${creating ? current.id : byId("profile-id").value}`;
    byId("open-display").href = base;
    byId("obs-url").value = creating ? "Nach dem Speichern verfügbar" : `${base}?obs=1`;
    const snapshot = await api("/preview", {method: "POST", body: JSON.stringify(data)});
    if (checkRuntime(snapshot.runtime_id)) return;
    if (version === previewVersion) byId("preview").contentWindow?.postMessage({type: "profile-preview", snapshot}, location.origin);
  } catch (error) { message(error.message, true); }
}

function select(profile, isNew = false) {
  current = structuredClone(profile);
  creating = isNew;
  byId("name").value = profile.name;
  byId("profile-id").value = profile.id;
  byId("profile-id").readOnly = !isNew;
  byId("rows").value = profile.rows.map(row => row.join(", ")).join("\n");
  for (const [color, value] of Object.entries(profile.theme)) byId(`color-${color}`).value = value;
  for (const input of byId("field-options").querySelectorAll("input")) input.checked = profile.fields.includes(input.value);
  byId("hits").value = profile.hits;
  byId("zoom").value = profile.zoom;
  byId("practice").checked = profile.practice;
  byId("discipline-inline").checked = profile.discipline_inline ?? false;
  byId("shot-highlight").value = profile.shot_highlight ?? "none";
  byId("hide-unavailable").checked = profile.hide_unavailable ?? false;
  byId("confetti-enabled").checked = profile.confetti_enabled ?? false;
  byId("confetti-threshold").value = profile.confetti_threshold ?? 10.5;
  byId("delete-profile").disabled = isNew || profiles.length < 2;
  byId("duplicate-profile").disabled = isNew;
  byId("settings-heading").textContent = isNew ? "Neues Profil" : "Profil bearbeiten";
  byId("preview").src = `/display/${isNew ? profiles[0].id : profile.id}?preview=1`;
  message("");
  list();
  updatePreview();
}

function uniqueId() {
  let number = 1;
  while (profiles.some(profile => profile.id === `profil-${number}`)) number++;
  return `profil-${number}`;
}

byId("profile-form").addEventListener("input", () => {
  message("Ungespeicherte Änderungen");
  previewVersion++;
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 150);
});
setInterval(() => { if (current && !document.hidden) updatePreview(); }, 2000);
byId("preview").addEventListener("load", updatePreview);
byId("profile-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("save-profile").disabled = true;
  try {
    const data = draft();
    const saved = await api(creating ? "" : `/${current.id}`, {method: creating ? "POST" : "PUT", body: JSON.stringify(data)});
    profiles = await api("");
    publicationProfiles();
    select(saved);
    message("Gespeichert · Anzeigen aktualisiert");
  } catch (error) { message(error.message, true); }
  finally { byId("save-profile").disabled = false; }
});
byId("new-profile").onclick = () => select({...structuredClone(profiles[0]), id: uniqueId(), name: "Neues Profil"}, true);
byId("duplicate-profile").onclick = () => select({...draft(), id: uniqueId(), name: `${byId("name").value} Kopie`}, true);
byId("delete-profile").onclick = async () => {
  if (!confirm(`Profil „${current.name}“ löschen?`)) return;
  try {
    await api(`/${current.id}`, {method: "DELETE"});
    profiles = await api("");
    publicationProfiles();
    select(profiles[0]);
    message("Profil gelöscht");
  } catch (error) { message(error.message, true); }
};
byId("copy-url").onclick = async () => {
  try { await navigator.clipboard.writeText(byId("obs-url").value); message("URL kopiert"); }
  catch { byId("obs-url").select(); message("URL markieren und kopieren"); }
};

async function imageUpload(file) {
  if (file.size > 5 * 1024 * 1024) throw new Error("Bilder dürfen höchstens 5 MiB groß sein");
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error("Bild nicht lesbar"));
    reader.readAsDataURL(file);
  });
  return {name: file.name, data};
}

async function loadLogo() {
  const image = await api("", {}, "/api/admin/logo");
  updateLogo(image);
  const preview = byId("logo-preview");
  preview.hidden = !image;
  if (image) preview.src = image.url;
  else preview.removeAttribute("src");
  byId("delete-logo").disabled = !image;
}

let disciplineRules = [];

function disciplineRow(mapping = {name: "", rule: disciplineRules[0]?.rule || ""}) {
  const row = document.createElement("div");
  row.className = "discipline-row";
  const name = document.createElement("input");
  name.required = true;
  name.maxLength = 64;
  name.placeholder = "Exakter Disziplinname";
  name.value = mapping.name;
  const rule = document.createElement("select");
  rule.append(...disciplineRules.map(item => {
    const option = document.createElement("option");
    option.value = item.rule;
    const caliber = Array.isArray(item.caliber_mm) ? `${item.caliber_mm[0]}–${item.caliber_mm[1]}` : item.caliber_mm;
    option.textContent = `${item.rule} · ${item.description} · ${String(caliber).replace(".", ",")} mm`;
    return option;
  }));
  rule.value = mapping.rule;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "danger";
  remove.textContent = "Entfernen";
  remove.onclick = () => row.remove();
  row.append(name, rule, remove);
  return row;
}

async function loadDisciplines() {
  const data = await api("", {}, "/api/admin/disciplines");
  disciplineRules = data.rules;
  byId("discipline-list").replaceChildren(...data.mappings.map(disciplineRow));
}

byId("add-discipline").onclick = () => byId("discipline-list").append(disciplineRow());
byId("discipline-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("save-disciplines").disabled = true;
  try {
    const mappings = [...byId("discipline-list").children].map(row => ({
      name: row.querySelector("input").value,
      rule: row.querySelector("select").value,
    }));
    const data = await api("", {method: "PUT", body: JSON.stringify({mappings})}, "/api/admin/disciplines");
    byId("discipline-list").replaceChildren(...data.mappings.map(disciplineRow));
    byId("discipline-status").textContent = "Gespeichert · Anzeigen aktualisiert";
  } catch (error) { byId("discipline-status").textContent = error.message; }
  finally { byId("save-disciplines").disabled = false; }
});

const resourceReasonLabels = {
  inactivity: "30 Minuten ohne Standänderung",
  outage: "ShootMaster-Quellen seit 20 Minuten nicht erreichbar",
  no_viewers: "5 Minuten ohne verbundene Anzeige",
};

function showResourceSaver(data, updateInputs = true) {
  if (updateInputs) {
    byId("resource-saver-enabled").checked = data.settings.enabled;
    byId("resource-saver-interval").value = data.settings.poll_interval_seconds;
  }
  const status = data.status;
  if (!status) {
    byId("resource-saver-status").textContent = "Workerstatus noch nicht verfügbar";
    return;
  }
  const state = !data.settings.enabled ? "Deaktiviert" : status.active ? "Aktiv" : "Bereit · normale Abfragerate";
  const reasons = status.reasons?.length ? `\nGrund: ${status.reasons.map(reason => resourceReasonLabels[reason] || reason).join(", ")}` : "";
  byId("resource-saver-status").textContent = `${state} · ${status.viewer_count} verbundene Anzeige${status.viewer_count === 1 ? "" : "n"}${reasons}`;
}

async function loadResourceSaver(updateInputs = true) {
  const data = await api("", {}, "/api/admin/settings/resource-saver");
  showResourceSaver(data, updateInputs);
}

byId("resource-saver-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("save-resource-saver").disabled = true;
  try {
    const data = await api("", {method: "PUT", body: JSON.stringify({
      enabled: byId("resource-saver-enabled").checked,
      poll_interval_seconds: byId("resource-saver-interval").valueAsNumber,
    })}, "/api/admin/settings/resource-saver");
    showResourceSaver(data);
    byId("resource-saver-status").textContent += "\nGespeichert · wird ohne Neustart übernommen";
  } catch (error) { byId("resource-saver-status").textContent = error.message; }
  finally { byId("save-resource-saver").disabled = false; }
});

let publicationSelection = [];
let publicationDeployBusy = false;
function publicationProfiles() {
  const selected = new Set(publicationSelection);
  publicationSelection = profiles.filter(profile => selected.has(profile.id)).map(profile => profile.id);
  byId("publication-profiles").replaceChildren(...profiles.map(profile => {
    const option = document.createElement("option");
    option.value = profile.id;
    option.textContent = profile.name;
    option.selected = selected.has(profile.id);
    return option;
  }));
}
byId("publication-profiles").addEventListener("change", () => {
  publicationSelection = [...byId("publication-profiles").selectedOptions].map(option => option.value);
});
function showPublication(data, updateInputs = true) {
  byId("deploy-publication").disabled = publicationDeployBusy || !data.settings.enabled;
  if (updateInputs) {
    const settings = data.settings;
    for (const name of ["enabled", "names"]) byId(`publication-${name}`).checked = settings[name];
    for (const name of ["host", "port", "user", "directory", "key_file", "known_hosts"]) {
      byId(`publication-${name.replaceAll("_", "-")}`).value = settings[name];
    }
    byId("publication-interval").value = settings.interval_seconds;
    publicationSelection = settings.profiles;
    publicationProfiles();
    byId("publication-password").value = "";
    byId("publication-password").placeholder = settings.password_set ? "Passwort gespeichert" : "Kein Passwort gespeichert";
    byId("publication-clear-password").checked = false;
  }
  const labels = {disabled: "Deaktiviert", connecting: "Verbindung wird hergestellt", connected: "Veröffentlichung aktiv", error: "Übertragung fehlgeschlagen · SFTP-Ziel, Zugangsdaten und Hostschlüssel prüfen"};
  const status = data.status;
  byId("publication-status").textContent = labels[status.state] || status.state;
  if (status.state === "error" && status.message) byId("publication-status").textContent = status.message;
  if (status.last_success_at) byId("publication-status").textContent += ` · Zuletzt bestätigt: ${new Date(status.last_success_at * 1000).toLocaleString("de-DE")}`;
}
async function loadPublicationSettings(updateInputs = true) {
  showPublication(await api("", {}, "/api/admin/settings/publication"), updateInputs);
}
byId("deploy-publication").addEventListener("click", async () => {
  publicationDeployBusy = true;
  byId("deploy-publication").disabled = true;
  byId("publication-deploy-status").textContent = "Webseite wird bereitgestellt …";
  try {
    const result = await api("", {method: "POST", body: "{}"}, "/api/admin/settings/publication/deploy");
    byId("publication-deploy-status").textContent = result.changed_files
      ? `Webseite bereitgestellt · ${result.changed_files} Dateien aktualisiert`
      : "Webseite bereits aktuell";
  } catch (error) { byId("publication-deploy-status").textContent = error.message; }
  finally {
    publicationDeployBusy = false;
    await loadPublicationSettings(false).catch(() => {});
  }
});
byId("publication-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("save-publication").disabled = true;
  try {
    const settings = {
      enabled: byId("publication-enabled").checked,
      names: byId("publication-names").checked,
      profiles: publicationSelection,
      port: byId("publication-port").valueAsNumber,
      interval_seconds: Number(byId("publication-interval").value),
    };
    for (const name of ["host", "user", "directory", "key_file", "known_hosts"]) {
      settings[name] = byId(`publication-${name.replaceAll("_", "-")}`).value;
    }
    if (byId("publication-clear-password").checked) settings.password = "";
    else if (byId("publication-password").value) settings.password = byId("publication-password").value;
    showPublication(await api("", {method: "PUT", body: JSON.stringify(settings)}, "/api/admin/settings/publication"));
    byId("publication-status").textContent += " · Gespeichert, ohne Neustart";
  } catch (error) { byId("publication-status").textContent = error.message; }
  finally { byId("save-publication").disabled = false; }
});

byId("logo-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("upload-logo").disabled = true;
  byId("delete-logo").disabled = true;
  try {
    await api("", {method: "PUT", body: JSON.stringify(await imageUpload(byId("logo-file").files[0]))}, "/api/admin/logo");
    byId("logo-file").value = "";
    byId("logo-status").textContent = "Logo gespeichert · Anzeigen aktualisiert";
  } catch (error) { byId("logo-status").textContent = error.message; }
  finally {
    byId("upload-logo").disabled = false;
    await loadLogo().catch(error => { byId("logo-status").textContent = error.message; });
    updatePreview();
  }
});

byId("delete-logo").onclick = async () => {
  byId("upload-logo").disabled = true;
  byId("delete-logo").disabled = true;
  try {
    await api("", {method: "DELETE"}, "/api/admin/logo");
    byId("logo-status").textContent = "Logo entfernt · Bisheriger Titel wiederhergestellt";
  } catch (error) { byId("logo-status").textContent = error.message; }
  finally {
    byId("upload-logo").disabled = false;
    await loadLogo().catch(error => { byId("logo-status").textContent = error.message; });
    updatePreview();
  }
};

async function loadSponsors() {
  const images = await api("", {}, "/api/admin/sponsors");
  byId("sponsor-list").replaceChildren(...images.map(image => {
    const item = document.createElement("div");
    item.className = "sponsor-item";
    item.dataset.id = image.id;
    const thumbnail = document.createElement("img");
    thumbnail.src = image.url;
    thumbnail.alt = image.name;
    const name = document.createElement("span");
    name.textContent = image.name;
    name.title = image.name;
    const label = document.createElement("label");
    label.textContent = "Text (optional)";
    const text = document.createElement("textarea");
    text.rows = 2;
    text.maxLength = 300;
    text.value = image.text || "";
    text.placeholder = "z. B. powered by Meral IT";
    label.append(text);
    const save = document.createElement("button");
    save.type = "button";
    save.textContent = "Text speichern";
    save.setAttribute("aria-label", `${image.name}: Text speichern`);
    save.onclick = async () => {
      save.disabled = true;
      try {
        const saved = await api(`/${encodeURIComponent(image.id)}`, {method: "PUT", body: JSON.stringify({text: text.value})}, "/api/admin/sponsors");
        text.value = saved.text;
        byId("sponsor-status").textContent = "Text gespeichert · Anzeigen aktualisiert";
        updatePreview();
      } catch (error) { byId("sponsor-status").textContent = error.message; }
      finally { save.disabled = false; }
    };
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Entfernen";
    remove.setAttribute("aria-label", `${image.name} entfernen`);
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        await api(`/${encodeURIComponent(image.id)}`, {method: "DELETE"}, "/api/admin/sponsors");
        item.remove();
        byId("sponsor-status").textContent = "Bild entfernt · Anzeigen aktualisiert";
        updatePreview();
      } catch (error) { byId("sponsor-status").textContent = error.message; remove.disabled = false; }
    };
    item.append(thumbnail, name, label, save, remove);
    return item;
  }));
}

byId("sponsor-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("upload-sponsors").disabled = true;
  try {
    const files = [...byId("sponsor-files").files];
    for (const file of files) {
      await api("", {method: "POST", body: JSON.stringify(await imageUpload(file))}, "/api/admin/sponsors");
    }
    byId("sponsor-files").value = "";
    byId("sponsor-status").textContent = "Bilder gespeichert · Anzeigen aktualisiert";
  } catch (error) { byId("sponsor-status").textContent = error.message; }
  finally {
    byId("upload-sponsors").disabled = false;
    await loadSponsors().catch(error => { byId("sponsor-status").textContent = error.message; });
    updatePreview();
  }
});
try {
  profiles = await api("");
  select(profiles[0]);
  await loadSponsors();
  await loadLogo();
  await loadDisciplines();
  await loadResourceSaver();
  await loadPublicationSettings();
  setInterval(() => loadPublicationSettings(false).catch(error => { byId("publication-status").textContent = error.message; }), 5000);
  setInterval(() => loadResourceSaver(false).catch(error => { byId("resource-saver-status").textContent = error.message; }), 5000);
} catch (error) { message(error.message, true); }
