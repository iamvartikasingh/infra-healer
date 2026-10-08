"use strict";
// Plot observed samples only. Break lines across pod replacement or container restart.
function drawMemory(canvas, samples, markers) {
  const width = canvas.clientWidth || 900, height = 260, ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const css = getComputedStyle(document.documentElement);
  const text = css.getPropertyValue("--text-secondary").trim();
  const blue = css.getPropertyValue("--series-1").trim();
  const points = [], keys = new Set();
  for (const s of samples) {
    const key = `${s.uid}:${s.restart_count}:${s.metrics_timestamp}`;
    if (s.memory_bytes == null || !s.metrics_timestamp || keys.has(key)) continue;
    keys.add(key); points.push(s);
  }
  points.sort((a,b) => Date.parse(a.metrics_timestamp) - Date.parse(b.metrics_timestamp));
  if (!points.length) { ctx.fillStyle = text; ctx.fillText("Waiting for metrics-server samples…", 20, 100); return; }
  const first = Date.parse(points[0].metrics_timestamp), last = Math.max(first + 1000, Date.parse(points.at(-1).metrics_timestamp));
  const max = Math.max(...points.map(p => Math.max(p.memory_bytes, p.memory_limit_bytes || 0))) * 1.15;
  const x = ts => 55 + (Date.parse(ts) - first) / (last - first) * (width - 80);
  const y = bytes => 220 - bytes / max * 190;
  ctx.font = "12px system-ui"; ctx.fillStyle = text;
  for (let i=0; i<=4; i++) {
    const value = max * i / 4;
    ctx.strokeStyle = css.getPropertyValue("--border").trim(); ctx.beginPath(); ctx.moveTo(55,y(value)); ctx.lineTo(width-25,y(value)); ctx.stroke();
    ctx.fillText((value / 1048576).toFixed(0), 8, y(value)+4);
  }
  ctx.fillText("MiB", 8, 16); ctx.fillText(t2(points[0].metrics_timestamp),55,248);
  ctx.fillText(t2(points.at(-1).metrics_timestamp),Math.max(55,width-100),248);
  ctx.setLineDash([5,5]); ctx.strokeStyle = "#d03b3b"; ctx.beginPath();
  points.forEach((p,i) => { const op = i ? "lineTo" : "moveTo"; ctx[op](x(p.metrics_timestamp), y(p.memory_limit_bytes || 0)); });
  ctx.stroke(); ctx.setLineDash([]);
  ctx.strokeStyle = blue; ctx.lineWidth = 2.5; ctx.beginPath();
  points.forEach((p,i) => {
    const prev = points[i-1], op = !prev || p.uid !== prev.uid || p.restart_count !== prev.restart_count ? "moveTo" : "lineTo";
    ctx[op](x(p.metrics_timestamp),y(p.memory_bytes));
  }); ctx.stroke();
  points.forEach(p => { ctx.fillStyle = blue; ctx.beginPath(); ctx.arc(x(p.metrics_timestamp),y(p.memory_bytes),2.5,0,Math.PI*2); ctx.fill(); });
  ctx.font = "11px system-ui";
  markers.forEach((m,i) => {
    const px = x(m.ts); if (px < 55 || px > width-25) return;
    ctx.strokeStyle = text; ctx.lineWidth = 1; ctx.setLineDash([2,4]); ctx.beginPath(); ctx.moveTo(px,25); ctx.lineTo(px,220); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = text; ctx.fillText(m.label,Math.min(px+4,width-115),30+(i%3)*14);
  });
}

async function refreshTelemetry() {
  const config = await (await fetch("/api/config")).json();
  if (config.demo || config.lab) return; // Public simulation never displays invented cluster measurements.
  const root = document.getElementById("telemetry"); root.hidden = false;
  async function refresh() {
    try {
      const response = await fetch("/api/workloads");
      if (!response.ok) throw new Error("telemetry unavailable");
      const {workloads} = await response.json();
      latestWorkloads = workloads;
      renderBackend([...incidents.values()].sort((a,b) => b.updated_at.localeCompare(a.updated_at)));
      root.replaceChildren();
      if (!workloads.length) root.append(el("div", {class: "telemetry"}, el("h3", {}, "Waiting for cluster observations"), el("p", {}, "Start the real agent against your demo namespace. Memory samples will appear here.")));
      workloads.forEach(w => {
        const s = w.latest, trend = w.trend;
        const age = (Date.now() - Date.parse(w.last_seen)) / 1000;
        const metricAge = s.metrics_timestamp ? (Date.now() - Date.parse(s.metrics_timestamp))/1000 : Infinity;
        const stat = (label,value) => el("div", {}, el("small", {}, label), el("strong", {class: value.length > 20 ? "long" : ""}, value));
        const mib = bytes => bytes == null ? "Unavailable" : (bytes/1048576).toFixed(1) + " MiB";
        const canvas = el("canvas", {role:"img", "aria-label":"Observed pod memory over time; dashed red line is the memory limit. Pod replacements and restarts break the orange line."});
        const box = el("article", {class:"telemetry"},
          el("div", {class:"top"}, el("h3", {}, `${w.namespace} / ${w.selector || "unlabeled workload"}`), el("span", {class:"chip"}, age > 15 ? "Observations stale" : "Cluster observations")),
          el("p", {class:"muted"}, `Pod ${s.name} · ${s.ready ? "Ready" : "Not ready"} · ${s.restart_count} restarts`),
          el("div", {class:"stats"}, stat("Memory",mib(s.memory_bytes)),stat("Limit",mib(s.memory_limit_bytes)),
            stat("Time to limit",w.prediction && trend && trend.eta_seconds != null && metricAge < 60 ? `~${Math.round(trend.eta_seconds)}s` : (metricAge >= 60 ? "Metrics unavailable" : "No active warning")),
            stat("Fit / samples",trend ? `R² ${trend.r2.toFixed(2)} / ${trend.n_points}` : "Collecting")),
          el("p", {}, metricAge > 60 ? "Metrics missing or stale; no current prediction shown." : w.prediction || "No memory warning at the current rule thresholds."), canvas,
          el("p", {class:"muted"}, "Orange: observed memory · Dashed red: configured limit · Gaps: replacement or restart"));
        root.append(box);
        const markers = [];
        for (const inc of incidents.values()) {
          if (inc.namespace !== w.namespace || inc.selector !== w.selector) continue;
          for (const event of inc.events) {
            const labels = {DETECTED: "Detected", REMEDIATING: "Action started", RESOLVED: "Verified recovery", ESCALATED: "Escalated"};
            if (labels[event.to]) markers.push({ts:event.ts,label:labels[event.to]});
          }
        }
        drawMemory(canvas,w.samples,markers);
      });
    } catch (e) {
      root.replaceChildren(el("div",{class:"telemetry"},"Telemetry connection lost. Retrying…"));
    }
    setTimeout(refresh,3000);
  }
  await refresh();
}
refreshTelemetry().catch(() => {});
