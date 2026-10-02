// ISSF 2026, rules 6.3.4.2, 6.3.4.3, and 6.3.4.6. Geometry and shot centers use physical millimeters.
export const TARGETS = {
  lg: {diameters: [45.5, 40.5, 35.5, 30.5, 25.5, 20.5, 15.5, 10.5, 5.5, 0.5], bull: 30.5, caliber: 4.5},
  kk: {diameters: [154.4, 138.4, 122.4, 106.4, 90.4, 74.4, 58.4, 42.4, 26.4, 10.4], bull: 112.4, caliber: 5.6},
  lp: {diameters: [155.5, 139.5, 123.5, 107.5, 91.5, 75.5, 59.5, 43.5, 27.5, 11.5], bull: 59.5, caliber: 4.5},
  // Meyton LANA GetResults, TargetDefinition 9010: 100 white 9 mm cells.
  schach10: {size: 90, caliber: 4.5},
};

function svgElement(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

export function targetExtent(kind, hits, zoom) {
  const target = TARGETS[kind];
  const full = (target.size || target.diameters[0]) / 2 + target.caliber;
  if (zoom === "full" || !hits.length) return full;
  // A centered view includes the whole projectile circle, not just its center.
  return Math.max(target.caliber * 1.25, ...hits.map(hit =>
    Math.max(Math.abs(hit.x_mm), Math.abs(hit.y_mm)) + target.caliber / 2)) * 1.12;
}

export function drawTarget(state, profile) {
  const target = TARGETS[state.target_kind];
  const hits = state.shots.filter(hit => Number.isFinite(hit.x_mm) && Number.isFinite(hit.y_mm));
  const extent = targetExtent(state.target_kind, hits, profile.zoom);
  const svg = svgElement("svg", {viewBox: `${-extent} ${-extent} ${extent * 2} ${extent * 2}`, "data-target-kind": state.target_kind, role: "img", "aria-label": `Schussbild Stand ${state.lane}, ${hits.length} Treffer`});
  svg.append(svgElement("rect", {x: -extent, y: -extent, width: extent * 2, height: extent * 2, fill: "#ffffff"}));
  if (state.target_kind === "schach10") {
    for (let row = 0; row < 10; row++) {
      for (let column = 0; column < 10; column++) {
        const x = -45 + column * 9, y = -45 + row * 9;
        const value = [1, 3, 5, 9][(row * 10 + column) % 4];
        const cell = svgElement("g", {"data-cell": `${row},${column}`, "data-value": value});
        cell.append(svgElement("rect", {x, y, width: 9, height: 9, fill: "#fff", stroke: "#000", "stroke-width": 0.15}));
        cell.append(svgElement("text", {x: x + 4.5, y: y + 4.5, fill: "#000", "font-size": 3.6, "text-anchor": "middle", "dominant-baseline": "central"}, value));
        svg.append(cell);
      }
    }
  } else {
    svg.append(svgElement("circle", {cx: 0, cy: 0, r: target.diameters[0] / 2, fill: profile.theme.target}));
    svg.append(svgElement("circle", {cx: 0, cy: 0, r: target.bull / 2, fill: "#052a22", opacity: 0.15}));
    target.diameters.forEach((diameter, index) => {
      const radius = diameter / 2;
      svg.append(svgElement("circle", {cx: 0, cy: 0, r: radius, fill: index === 9 && state.target_kind === "lg" ? "#ffffff" : "none", stroke: "#e8fff6", "stroke-width": Math.max(0.12, extent / 190)}));
      const next = (target.diameters[index + 1] || 0) / 2;
      const labelPosition = (radius + next) / 2;
      if (index < 8 && labelPosition < extent * 0.94) {
        for (const [x, y] of [[labelPosition, 0], [-labelPosition, 0], [0, labelPosition], [0, -labelPosition]]) {
          svg.append(svgElement("text", {x, y, fill: "#d5f5e9", "font-size": Math.min(extent / 11, (radius - next) * 0.8), "text-anchor": "middle", "dominant-baseline": "central"}, String(index + 1)));
        }
      }
    });
  }
  hits.forEach(hit => {
    const latest = state.latest && state.latest.number === hit.number && state.latest.position === hit.position;
    const ring = hit.score ? Math.floor(hit.score.value / hit.score.scale) : null;
    const fill = latest ? (ring >= 10 ? "#ec433a" : ring >= 9 ? "#e4ce26" : "#409cdc") : "#122622";
    const group = svgElement("g", {"data-shot": hit.number, "data-latest": latest, "data-x-mm": hit.x_mm, "data-y-mm": hit.y_mm});
    const strokeWidth = target.caliber / (latest ? 14 : 25);
    // SVG centers the stroke on the path; inset it to keep the outside diameter at caliber.
    const circle = svgElement("circle", {cx: hit.x_mm, cy: -hit.y_mm, r: (target.caliber - strokeWidth) / 2, fill, stroke: latest ? "#fff" : "#95b1a6", "stroke-width": strokeWidth, opacity: hit.invalid ? 0.45 : 0.94});
    group.append(circle);
    group.append(svgElement("text", {x: hit.x_mm, y: -hit.y_mm, fill: latest ? "#11261f" : "#ffffff", "font-size": target.caliber * (hit.number >= 100 ? 0.45 : 0.55), "font-weight": 600, "text-anchor": "middle", "dominant-baseline": "central"}, hit.number));
    group.append(svgElement("title", {}, `Treffer ${hit.number}${hit.score ? ` · ${(hit.score.value / hit.score.scale).toLocaleString("de-DE")}` : ""}`));
    svg.append(group);
  });
  return svg;
}
