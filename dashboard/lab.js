"use strict";
async function installLab() {
  const root = document.getElementById("lab"); root.hidden = false;
  document.getElementById("mode-tag").replaceChildren("Environment ",el("b",{},"Live application lab"));
  document.querySelector(".intro h1").textContent = "Break a checkout. Watch it recover.";
  document.querySelector(".intro .muted").textContent = "Real HTTP requests. Isolated faults. Measured recovery, with your approval.";
  const descriptions = [
    ["Observe","Real HTTP probes","A traffic probe calls a disposable checkout HTTP process once a second and measures its status and latency."],
    ["Detect","Request thresholds","Three consecutive responses breach the HTTP 200 / below 200ms hypothesis."],
    ["Diagnose","Measured evidence","A deterministic rule examines actual request results after controlled fault injection. No AI model is used."],
    ["Policy","Isolated scope","Only restarting this browser session's checkout worker is allowed, and only after approval."],
    ["Remediate","Restart the process","The lab terminates the faulty checkout process and starts a new HTTP worker with a new process identity."],
    ["Verify","Probe the replacement","Three consecutive HTTP 200 responses below 200ms are required to record recovery."]
  ];
  descriptions.forEach((d,i) => Object.assign(BACKEND_STAGES[i],{name:d[0],sub:d[1],desc:d[2]}));
  const nav = document.querySelector('nav a[href="#telemetry"]');
  nav.setAttribute("href","#lab"); nav.replaceChildren(el("span",{"aria-hidden":"true"},"⌁"),"Live experiment");
  let running = false, busy = false;
  const startButtons = [...root.querySelectorAll("[data-fault]")];
  const stopButton = document.getElementById("stop-lab");
  const requestButton = document.getElementById("checkout-request");
  function controls() { startButtons.forEach(b => b.disabled = running || busy); stopButton.disabled = !running || busy; requestButton.disabled = !running || busy; }
  async function post(url,body) {
    busy = true; controls();
    try {
      const r = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
      if (j.status != null) toast(`Actual checkout response: ${j.status || "connection failed"} · ${j.latency_ms}ms`);
    } catch(e) { toast(e.message); }
    finally { busy = false; await refresh(); controls(); }
  }
  startButtons.forEach(b => b.addEventListener("click",()=>post("/api/lab/start",{kind:b.dataset.fault})));
  stopButton.addEventListener("click",()=>post("/api/lab/stop",{}));
  requestButton.addEventListener("click",()=>post("/api/lab/checkout",{}));
  async function refresh() {
    try {
      const [lr,ir] = await Promise.all([fetch("/api/lab"),fetch("/api/incidents")]);
      if (!lr.ok || !ir.ok) throw new Error("Lab connection unavailable");
      const data = await lr.json(), rows = await ir.json();
      incidents.clear(); rows.incidents.forEach(i=>incidents.set(i.id,i)); render(); live(true);
      document.querySelector("#live span").textContent = "live HTTP lab";
      const exp = data.experiment;
      running = exp && !["COMPLETE","REJECTED","STOPPED","EXPIRED","FAILED"].includes(exp.phase);
      const samples = exp?.samples || [], last = samples.at(-1);
      document.getElementById("lab-phase").textContent = exp ? exp.phase.replaceAll("_"," ") : "READY TO EXPERIMENT";
      const fmt = n => n == null ? "—" : `${n}s`;
      const stat = (label,value) => el("div",{},el("small",{},label),el("strong",{},value));
      document.getElementById("lab-stats").replaceChildren(
        stat("Requests meeting hypothesis",exp?.healthy_rate == null ? "—" : exp.healthy_rate+"%"),
        stat("Last response",last ? `${last.latency_ms}ms` : "—"),
        stat("Detection time",fmt(exp?.timings.detection_seconds)),
        stat("Recovery after approval",fmt(exp?.timings.recovery_seconds)));
      document.getElementById("checkout-result").textContent = !last ? "Waiting for the first real request" : last.status === 200 ? `HTTP 200 · ${last.latency_ms}ms · checkout completed` : `HTTP ${last.status || "connection failed"} · checkout unavailable`;
      document.getElementById("checkout-result").className = last && last.status !== 200 ? "checkout-error" : "checkout-ok";
      document.getElementById("worker-id").textContent = exp?.worker_pid ? `Live worker process ${exp.worker_pid} · ${samples.length} measured requests` : exp ? `Worker cleaned up · ${samples.length} measured requests retained` : "A separate HTTP process starts for your experiment.";
      drawLabChart(document.getElementById("lab-chart"),samples);
      const log = document.getElementById("lab-events");
      log.replaceChildren(...(exp?.events.length ? exp.events.map(e => el("div",{class:"lab-event"},el("time",{},t2(e.ts)),e.message)) : [el("p",{class:"muted"},"Start an experiment to collect real request evidence.")]));
      controls();
    } catch(e) { live(false); toast(e.message); }
  }
  await refresh();
  async function poll() { await refresh(); setTimeout(poll,1000); }
  setTimeout(poll,1000);
}
function drawLabChart(canvas,samples) {
  const width = canvas.clientWidth || 700, height = 180, ratio = window.devicePixelRatio || 1;
  canvas.width=width*ratio;canvas.height=height*ratio;
  const c=canvas.getContext("2d");c.scale(ratio,ratio);c.font="10px system-ui";
  const max=Math.max(450,...samples.map(s=>s.latency_ms)), left=42, end=width-12;
  const y=v=>145-v/max*120;
  c.fillStyle="#777368";
  [0,200,400].forEach(v=>{c.fillText(v+"ms",0,y(v)+3);c.strokeStyle="#e9e6df";c.beginPath();c.moveTo(left,y(v));c.lineTo(end,y(v));c.stroke();});
  c.strokeStyle="#b66b12";c.setLineDash([4,4]);c.beginPath();c.moveTo(left,y(200));c.lineTo(end,y(200));c.stroke();c.setLineDash([]);
  if (!samples.length) {c.fillText("Waiting for real HTTP measurements…",left+10,90);return;}
  const first=Date.parse(samples[0].ts),last=Math.max(first+1000,Date.parse(samples.at(-1).ts));
  const x=s=>left+(Date.parse(s.ts)-first)/(last-first)*(end-left);
  c.strokeStyle="#d94c12";c.lineWidth=2;c.beginPath();samples.forEach((s,i)=>c[i?"lineTo":"moveTo"](x(s),y(s.latency_ms)));c.stroke();
  samples.forEach(s=>{c.fillStyle=s.status===200?"#d94c12":"#c63f3f";c.beginPath();c.arc(x(s),y(s.latency_ms),s.status===200?2:4,0,Math.PI*2);c.fill();});
  c.fillStyle="#777368";c.fillText(t2(samples[0].ts),left,171);c.fillText(t2(samples.at(-1).ts),Math.max(left,end-58),171);
}
