// Shared renderer, static snapshots. Enabled only in the exported public HTML.
export const publicDisplay = document.querySelector('meta[name="meyton-public"]') !== null;
let bundle;
let fetchedAt;
let ageAtFetch = Infinity;

export async function loadPublication() {
  const response = await fetch('/live.json', {cache: 'no-store', signal: AbortSignal.timeout(10000)});
  if (!response.ok) throw new Error('Live-Veröffentlichung nicht verfügbar');
  const data = await response.json();
  if (data.version !== 1 || !data.catalog || !data.snapshots || !Number.isFinite(data.published_at)) {
    throw new Error('Live-Veröffentlichung ungültig');
  }
  // HTTP dates avoid depending on the range computer's clock for outage detection.
  const serverTime = Date.parse(response.headers.get('Date'));
  const modified = Date.parse(response.headers.get('Last-Modified'));
  ageAtFetch = Number.isFinite(serverTime) && Number.isFinite(modified)
    ? Math.max(0, serverTime - modified)
    : Math.max(0, Date.now() - data.published_at * 1000);
  fetchedAt = performance.now();
  bundle = data;
}

export function publicationCatalog() { return bundle.catalog; }
export function publicationProfiles() { return Object.values(bundle.snapshots).map(snapshot => snapshot.profile); }
export function publicationSnapshot(id, lane) {
  const original = bundle.snapshots[id];
  if (!original) return null;
  const snapshot = structuredClone(original);
  if (lane !== null) {
    const entry = snapshot.rows.flat().find(entry => entry.lane === lane);
    if (!entry) return null; // Never expose a stand outside an approved profile.
    snapshot.rows = [[entry]];
    snapshot.profile.rows = [[lane]];
  }
  return snapshot;
}

export function publicationStatus() {
  const age = ageAtFetch + (fetchedAt === undefined ? 0 : performance.now() - fetchedAt);
  const interval = Math.min(3600, Math.max(10, Number(bundle?.interval_seconds) || 60)) * 1000;
  const expired = age >= Math.max(300000, interval * 3);
  const stale = age >= Math.max(15000, interval * 1.25);
  const status = document.getElementById('connection');
  if (stale) {
    status.className = 'degraded';
    status.textContent = expired ? 'Live-Feed nicht verfügbar' : 'Live-Daten veraltet';
  }
  const updated = bundle ? new Date(bundle.published_at * 1000).toLocaleString('de-DE', {timeZone: 'Europe/Berlin'}) : '—';
  document.getElementById('source-status').textContent = `Letzte Veröffentlichung: ${updated}`;
  document.getElementById('ranges').hidden = expired;
  return !expired;
}
