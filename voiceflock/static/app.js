/* VoiceFlock UI v2: Home · Library · People · Live · Settings. Vanilla JS over /api/*. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtT = (s) => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(x).padStart(2, "0"); };
const pct = (p) => (p * 100).toFixed(1) + "%";
const probCls = (p, thr, hi) => p >= hi ? "hi" : p >= thr ? "mid" : "lo";
const COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const elapsed = (iso) => { if (!iso) return ""; const s = Math.max(0, (Date.now() - Date.parse(iso + (iso.endsWith("Z") ? "" : "Z"))) / 1000); return fmtT(s); };
let SETTINGS = {}, SPECS = {}, STATE = { tab: "home", sourceId: null, personId: null, peopleView: "known", matchSource: null };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(msg, err = false) { const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 3500); }
const onErr = (e) => toast(e.message || String(e), true);
const mediaPlaying = () => $$("audio,video").some((a) => !a.paused && !a.ended) || (YTP && YTP.getPlayerState && YTP.getPlayerState() === 1);

/* ---------------- routing ---------------- */
$$("#tabs button").forEach((b) => b.addEventListener("click", () => go(b.dataset.tab)));
function go(route) { if (location.hash !== "#" + route) location.hash = route; else render(); }
function render() {
  const [tab, id] = (location.hash.replace("#", "") || "home").split("/");
  STATE.tab = $(`#tab-${tab}`) ? tab : "home";
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === STATE.tab));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + STATE.tab));
  if (STATE.tab === "library") { STATE.sourceId = id ? Number(id) : null; loadLibrary(); }
  else if (STATE.tab === "people") { STATE.personId = id ? Number(id) : null; loadPeople(); }
  else if (STATE.tab === "home") loadHome();
  else if (STATE.tab === "settings") loadSettings();
}
window.addEventListener("hashchange", render);
const openSource = (id) => go(`library/${id}`);
const openPerson = (id) => go(`people/${id}`);
function refreshTab() { if (!mediaPlaying()) render(); }

/* ---------------- header ---------------- */
async function loadStats() {
  try {
    const [s, h] = await Promise.all([api("/api/stats"), api("/api/health")]);
    $("#ver").textContent = "v" + h.version;
    const q = h.queue || {};
    $("#stats").innerHTML = `<span><b>${s.persons_named}</b> people</span><span><b>${s.persons_unknown}</b> unnamed</span><span><b>${fmtT(s.sources.seconds)}</b> audio</span>
      <span>queue: <b>${q.running || 0}</b> running, <b>${q.queued || 0}</b> waiting</span><span>${s.score_mode}${s.calibrated ? " · calibrated" : ""}</span><span>device: <b>${h.device || "?"}</b>${h.gpu && h.gpu.torch_device_name ? " (" + esc(h.gpu.torch_device_name) + ")" : ""}</span>`;
    const b = $("#banner"); const g = h.gpu || {};
    if (g.nvidia_driver_gpu && !g.torch_cuda) { b.innerHTML = `NVIDIA GPU found (${esc(g.nvidia_driver_gpu)}) but VoiceFlock runs on the CPU (10-30× slower). Close VoiceFlock, run <code>gpu.bat</code>, restart.`; b.classList.remove("hidden"); }
    else if (!h.hf_token_set) { b.innerHTML = `No HuggingFace token set: diarization will fail. Add it under <a href="#settings">Settings</a> after accepting the model terms on huggingface.co.`; b.classList.remove("hidden"); }
    else if (!h.ffmpeg) { b.textContent = "ffmpeg not found on PATH."; b.classList.remove("hidden"); }
    else b.classList.add("hidden");
    return (q.running || 0) + (q.queued || 0);
  } catch (e) { return 0; }
}

/* ---------------- uploads ---------------- */
function wireDrop(zoneId, inputId, opts) {
  const zone = $(zoneId), input = $(inputId);
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files], opts));
  input.addEventListener("change", () => { uploadFiles([...input.files], opts); input.value = ""; });
}
function uploadFiles(files, opts) {
  const mode = opts.mode(), list = $(opts.list);
  files.forEach((f) => {
    const row = document.createElement("div"); row.className = "u";
    row.innerHTML = `<span>${esc(f.name)}</span><div class="bar"><i></i></div><span class="st">0%</span>`;
    list.prepend(row);
    const fd = new FormData(); fd.append("files", f); fd.append("mode", mode); fd.append("incognito", mode === "match" ? "true" : "false");
    if (mode === "enroll") { const n = opts.personName(); if (!n) { row.querySelector(".st").textContent = "type the person's name first"; return; } fd.append("person_name", n); }
    const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const p = e.loaded / e.total; row.querySelector("i").style.width = pct(p); row.querySelector(".st").textContent = p < 1 ? pct(p) : "saving…"; } };
    xhr.onload = () => {
      if (xhr.status >= 300) { row.querySelector(".st").textContent = "failed"; return onErr(new Error(xhr.responseText)); }
      const c = JSON.parse(xhr.responseText).created[0];
      if (c.error) { row.querySelector(".st").textContent = c.error; return; }
      if (c.duplicate_of) { row.querySelector(".st").innerHTML = `already in the library: <a class="link" onclick="openSource(${c.duplicate_of})">${esc(c.duplicate_name)}</a>`; return; }
      row.dataset.source = c.source_id; row.querySelector(".st").textContent = "queued"; toast(`Queued ${f.name}`); loadStats();
    };
    xhr.onerror = () => { row.querySelector(".st").textContent = "network error"; };
    xhr.send(fd);
  });
}
async function queueUrl(inputId, opts) {
  const url = $(inputId).value.trim(); if (!url) return;
  const mode = opts.mode();
  try {
    const body = { url, mode, incognito: mode === "match" };
    if (mode === "enroll") { body.person_name = opts.personName(); if (!body.person_name) throw new Error("type the person's name first"); }
    const r = await api("/api/upload_url", { method: "POST", body });
    $(inputId).value = "";
    if (r.duplicate_of) { toast(`Already in the library: ${r.duplicate_name}`); openSource(r.duplicate_of); return; }
    toast("Queued"); loadStats(); if (STATE.tab === "library") loadLibrary();
  } catch (e) { onErr(e); }
}
const mainOpts = { mode: () => $("#add-mode").value, personName: () => $("#enroll-name").value.trim(), list: "#upl-main" };
const enrollOpts = { mode: () => "enroll", personName: () => $("#identify-name").value.trim(), list: "#upl-enroll" };
wireDrop("#drop-main", "#file-main", mainOpts);
wireDrop("#drop-enroll", "#file-enroll", enrollOpts);
$("#btn-url-main").onclick = () => queueUrl("#url-main", mainOpts);
$("#btn-url-enroll").onclick = () => queueUrl("#url-enroll", enrollOpts);
$("#add-mode").addEventListener("change", () => $("#enroll-name").classList.toggle("hidden", $("#add-mode").value !== "enroll"));
async function updateUploadRows() {
  const rows = $$(".uploads .u[data-source]"); if (!rows.length) return;
  const j = await api("/api/jobs?limit=200").catch(() => ({ jobs: [] }));
  const bySrc = {}; j.jobs.forEach((x) => { if (!bySrc[x.source_id]) bySrc[x.source_id] = x; });
  rows.forEach((r) => { const x = bySrc[r.dataset.source]; if (!x) return; const st = r.querySelector(".st");
    if (x.status === "running") { st.textContent = `${x.stage || "running"} ${Math.round(x.progress)}%`; r.querySelector("i").style.width = x.progress + "%"; }
    else if (x.status === "done") st.innerHTML = `done · <a class="link" onclick="openSource(${x.source_id})">open</a>${x.enroll_person_id ? ` · <a class="link" onclick="openPerson(${x.enroll_person_id})">open person</a>` : ""}`;
    else st.textContent = x.status + (x.error ? ": " + x.error : "");
  });
}

/* ---------------- microphone recording ---------------- */
let REC = null;
$("#btn-rec").onclick = async () => {
  const btn = $("#btn-rec"), st = $("#rec-status");
  if (REC) { REC.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) { st.textContent = "Recording needs localhost or HTTPS."; return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
    const chunks = []; const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const t0 = Date.now(); const tick = setInterval(() => { st.textContent = `recording ${fmtT((Date.now() - t0) / 1000)}… click 🎙 again to stop`; }, 500);
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = () => {
      clearInterval(tick); stream.getTracks().forEach((t) => t.stop()); REC = null; btn.textContent = "🎙";
      const ext = (rec.mimeType || "").includes("mp4") ? "m4a" : (rec.mimeType || "").includes("ogg") ? "ogg" : "webm";
      const f = new File(chunks, `recording ${new Date().toISOString().slice(0, 16).replace("T", " ")}.${ext}`, { type: rec.mimeType || "audio/webm" });
      st.textContent = `recorded ${fmtT((Date.now() - t0) / 1000)}, uploading`;
      uploadFiles([f], mainOpts);
    };
    rec.start(1000); REC = rec; btn.textContent = "⏹";
  } catch (e) { st.textContent = "microphone blocked: " + e.message; }
};

/* ---------------- shared renderers ---------------- */
const bell = (p) => `<span class="bell ${p.alert ? "on" : ""}" title="${p.alert ? "Alerting on this voice (Live + phone). Click to stop." : "Alert me when this voice is heard (Live + phone)"}" onclick="toggleAlert(${p.id}, ${p.alert ? "false" : "true"}, event)">🔔</span>`;
async function toggleAlert(id, on, ev) { ev?.stopPropagation(); try { await api(`/api/persons/${id}`, { method: "PATCH", body: { alert: on } }); toast(on ? "Will alert on this voice" : "Alert off"); render(); } catch (e) { onErr(e); } }
function glyphSvg(g, color = "var(--s1)", size = 28) {
  if (!g) return `<span class="glyph"></span>`;
  const n = g.length, r0 = 5, r1 = 13, cx = 14, cy = 14;
  const bars = g.map((v, i) => { const a = (i / n) * Math.PI * 2, r = r0 + v * (r1 - r0); return `<line x1="${cx + Math.cos(a) * r0}" y1="${cy + Math.sin(a) * r0}" x2="${cx + Math.cos(a) * r}" y2="${cy + Math.sin(a) * r}" stroke="${color}" stroke-width="1.6" stroke-linecap="round"/>`; }).join("");
  return `<svg class="glyph" width="${size}" height="${size}" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="13" fill="var(--panel2)"/>${bars}</svg>`;
}
function heardIn(list) { return list && list.length ? list.map((h) => `<a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> @ ${fmtT(h.first_start_s)}`).join(", ") : `<span class="muted">(no other recordings)</span>`; }
function clip(url) { return url ? `<audio controls preload="metadata" src="${url}"></audio>` : ""; }

/* ---------------- HOME ---------------- */
let OV = null;
async function loadHome() {
  try {
    const [a, o] = await Promise.all([api("/api/alerts"), api("/api/overview")]); OV = o;
    const thr = Number(SETTINGS.match_threshold || 0.75);
    $("#alert-hint").textContent = `≥ ${pct(thr)} · confirm only when very sure (≥ ${pct(a.confirm_threshold)})`;
    $("#alerts-known").innerHTML = a.known.length ? a.known.slice(0, 30).map((k) => `<div class="item">
      <div class="title"><a class="link" onclick="openPerson(${k.person_id})">${esc(k.person_name)}</a> in <a class="link" onclick="openSource(${k.source_id})">${esc(k.source_name)}</a> at ${fmtT(k.first_start_s)}
        <span class="prob ${probCls(k.probability, thr, a.confirm_threshold)}">${pct(k.probability)}</span> <span class="muted">(+${pct(k.margin)} over #2)</span>${k.status === "confirmed" ? '<span class="pill named">confirmed</span>' : ""}</div>
      <div class="sub">also heard in: ${heardIn(k.heard_in)} · speech ${fmtT(k.speech_seconds)} · quality ${Math.round(k.quality_score || 0)}</div>
      ${clip(k.clip_url)}
      <div class="actions">${k.status === "confirmed" ? `<button class="small" onclick="unassign(${k.speaker_id})">Undo confirm</button>` : `<button class="small primary" onclick="confirmMatch(${k.match_id}, ${k.probability})">Confirm</button><button class="small" onclick="rejectMatch(${k.match_id})">Reject</button>`}
        <button class="small" onclick="openSource(${k.source_id})">Open recording</button></div></div>`).join("") : `<p class="muted">Nothing yet. Add recordings in Library and identify people in People.</p>`;
    $("#alerts-recurring").innerHTML = a.recurring_unknown.length ? a.recurring_unknown.slice(0, 10).map((r) => `<div class="item">
      <div class="title"><a class="link" onclick="openPerson(${r.person_id})">${esc(r.name)}</a> <span class="muted">· ${r.n_sources} recordings · ${fmtT(r.speech_seconds)}</span></div>
      ${clip(r.heard_in[0]?.clip_url)}
      <div class="inline"><input placeholder="Name this voice" list="person-names" id="pn-${r.person_id}"><button class="small" onclick="namePerson(${r.person_id})">Name</button></div></div>`).join("") : `<p class="muted">No unnamed voice has shown up twice yet.</p>`;
    const t = o.stats;
    $("#ov-tiles").innerHTML = [[t.hours.toFixed(1), "hours of recordings"], [t.recordings, "recordings"], [t.known, "known people"], [t.unknown, "unnamed voices"], [t.speech_hours.toFixed(1), "hours of attributed speech"], [t.open_alerts, "alerts to review"]]
      .map(([v, l]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
    const maxS = Math.max(1, ...o.top_voices.map((v) => v.speech_seconds));
    $("#ov-top").innerHTML = o.top_voices.map((v) => `<div class="hbar"><a onclick="openPerson(${v.id})" style="cursor:pointer">${glyphSvg(v.glyph, v.kind === "named" ? "var(--s1)" : "var(--s3)")}</a>
      <div><div class="nm"><a class="link" onclick="openPerson(${v.id})">${esc(v.name)}</a><span>${fmtT(v.speech_seconds)} · ${v.n_sources} rec</span></div><div class="bar"><i style="width:${(v.speech_seconds / maxS) * 100}%"></i></div></div></div>`).join("") || '<p class="muted">Nothing processed yet.</p>';
    const act = o.activity; const maxA = Math.max(1, ...act.map((x) => x.hours));
    $("#ov-activity").innerHTML = act.length ? `<div class="cols">${act.map((x) => `<div class="c" style="height:${Math.max(2, (x.hours / maxA) * 100)}%" data-tip="${esc(x.day)}: ${x.recordings} rec · ${x.hours.toFixed(1)} h"></div>`).join("")}</div><div class="xlab"><span>${esc(act[0].day)}</span><span>${esc(act[act.length - 1].day)}</span></div>` : '<p class="muted">No recordings yet.</p>';
    $("#ov-pairs").innerHTML = o.pairs.map((p) => `<div class="item"><a class="link" onclick="openPerson(${p.a_id})">${esc(p.a_name)}</a> + <a class="link" onclick="openPerson(${p.b_id})">${esc(p.b_name)}</a> <span class="muted">· ${p.n}</span></div>`).join("") || '<p class="muted">Needs recordings with two or more identified voices.</p>';
    drawMap(o.voices);
  } catch (e) { onErr(e); }
}
function drawMap(voices) {
  const cv = $("#ov-map"); const dpr = window.devicePixelRatio || 1; const W = cv.clientWidth || 600, H = 380;
  cv.width = W * dpr; cv.height = H * dpr; const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, W, H);
  if (!voices.length) { ctx.fillStyle = "#8a94a3"; ctx.font = "13px system-ui"; ctx.fillText("The map fills in as voices are added.", 16, 28); return; }
  const pad = 28, maxS = Math.max(1, ...voices.map((v) => v.speech_seconds));
  const pts = voices.map((v) => ({ v, x: pad + ((v.x + 1) / 2) * (W - 2 * pad), y: pad + ((1 - v.y) / 2) * (H - 2 * pad), r: 4 + Math.sqrt(v.speech_seconds / maxS) * 14 }));
  cv._pts = pts;
  for (const p of pts) { ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fillStyle = p.v.kind === "named" ? "#3987e5" : "#199e70"; ctx.globalAlpha = 0.85; ctx.fill(); ctx.globalAlpha = 1; ctx.lineWidth = 2; ctx.strokeStyle = "#171b21"; ctx.stroke(); }
  ctx.fillStyle = "#e8ecf1"; ctx.font = "12px system-ui";
  for (const p of pts.filter((p) => p.v.kind === "named").sort((a, b) => b.r - a.r).slice(0, 12)) ctx.fillText(p.v.name, p.x + p.r + 4, p.y + 4);
}
$("#ov-map").addEventListener("mousemove", (e) => {
  const cv = e.target, pts = cv._pts || [], r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
  const hit = pts.find((p) => Math.hypot(p.x - x, p.y - y) <= p.r + 4); const tip = $("#ov-tip");
  if (!hit) { tip.classList.add("hidden"); cv._hit = null; return; }
  cv._hit = hit; tip.classList.remove("hidden"); tip.style.left = hit.x + 12 + "px"; tip.style.top = hit.y - 10 + "px";
  tip.innerHTML = `<b>${esc(hit.v.name)}</b> <span class="muted">${hit.v.kind === "named" ? "known" : "unnamed"}</span><br>${fmtT(hit.v.speech_seconds)} · ${hit.v.n_sources} recordings`;
});
$("#ov-map").addEventListener("mouseleave", () => $("#ov-tip").classList.add("hidden"));
$("#ov-map").addEventListener("click", (e) => { const h = e.target._hit; if (h) openPerson(h.v.id); });
window.addEventListener("resize", () => { if (STATE.tab === "home" && OV) drawMap(OV.voices); });

/* ---------------- LIBRARY ---------------- */
async function loadLibrary() {
  loadNames();
  if (STATE.sourceId) { $("#lib-list").classList.add("hidden"); $("#lib-detail").classList.remove("hidden"); return loadSourceDetail(STATE.sourceId); }
  $("#lib-detail").classList.add("hidden"); $("#lib-detail").innerHTML = ""; YTP = null; $("#lib-list").classList.remove("hidden");
  loadFeeds();
  try {
    const qs = encodeURIComponent($("#source-search").value.trim());
    const [j, q] = await Promise.all([api(`/api/sources?limit=300&q=${qs}`), api("/api/jobs?limit=60")]);
    const active = q.jobs.filter((x) => x.status === "running" || x.status === "queued" || x.status === "failed");
    const label = (x) => x.type === "process_source" ? `<a class="link" onclick="openSource(${x.source_id})">${esc(x.source_name || "#" + x.source_id)}</a>` : `<b>${esc(x.type)}</b>`;
    $("#jobs").innerHTML = active.length ? active.slice(0, 20).map((x) => `<div class="jobrow"><span class="pill ${x.status}">${x.status}</span>${label(x)}
      ${x.status === "running" ? `<span class="muted">${esc(x.stage || "")} ${Math.round(x.progress)}% · ${elapsed(x.started_at)} · updated ${elapsed(x.heartbeat_at) || "-"} ago</span><div class="bar"><i style="width:${x.progress}%"></i></div><button class="small" onclick="cancelJob(${x.id})">stop</button>` : ""}
      ${x.status === "queued" ? `<button class="small" onclick="cancelJob(${x.id})">cancel</button>` : ""}
      ${x.status === "failed" ? `<span class="warnbox" style="margin:0">${esc(x.error)}</span>${x.type === "process_source" ? `<button class="small" onclick="retryJob(${x.id})">retry</button>` : ""}<button class="small" onclick="dismissJob(${x.id})">dismiss</button>${x.log ? `<details style="margin:0;padding:0;border:0"><summary>details</summary><pre class="muted" style="font-size:11px;white-space:pre-wrap">${esc(x.log.slice(-1500))}</pre></details>` : ""}` : ""}
      </div>`).join("") + (active.length > 1 ? `<div class="actions"><button class="small" onclick="clearJobs()">Clear finished</button></div>` : "") : "";
    $("#sources").innerHTML = j.sources.length ? `<table><tr><th>Recording</th><th>Status</th><th>Length</th><th>Voices</th><th></th></tr>` + j.sources.map((s) => `<tr>
      <td><a class="link" onclick="openSource(${s.id})">${esc(s.name)}</a>${s.incognito ? '<span class="pill">identify only</span>' : ""}${s.mode === "enroll" ? '<span class="pill">enroll</span>' : ""}</td>
      <td><span class="pill ${s.status}">${s.status}</span>${s.job && s.job.status === "running" ? ` <span class="muted">${Math.round(s.job.progress)}%</span>` : ""}</td>
      <td>${s.duration_s ? fmtT(s.duration_s) : ""}</td>
      <td class="sub">${s.speakers.map((sp) => sp.person_name ? `<span class="pill ${sp.person_kind}">${esc(sp.person_name)}</span>` : sp.top_name ? `<span class="muted">${esc(sp.label)}≈${esc(sp.top_name)} ${pct(sp.top_prob)}</span>` : `<span class="muted">${esc(sp.label)}</span>`).join(" ")}</td>
      <td><button class="small danger" onclick="deleteSource(${s.id})">✕</button></td></tr>`).join("") + `</table>` : '<p class="muted">No recordings yet. Drop a file above.</p>';
  } catch (e) { onErr(e); }
}
$("#source-search").addEventListener("input", () => { clearTimeout(STATE._st); STATE._st = setTimeout(loadLibrary, 250); });
$("#btn-rematch").onclick = async () => { try { await api("/api/rematch", { method: "POST", body: {} }); toast("Re-match queued"); loadLibrary(); } catch (e) { onErr(e); } };
async function retryJob(id) { try { await api(`/api/jobs/${id}/retry`, { method: "POST" }); loadLibrary(); } catch (e) { onErr(e); } }
async function cancelJob(id) { try { const r = await api(`/api/jobs/${id}/cancel`, { method: "POST" }); toast(r.status === "running" ? "Stopping…" : "Cancelled"); loadLibrary(); } catch (e) { onErr(e); } }
async function dismissJob(id) { try { await api(`/api/jobs/${id}`, { method: "DELETE" }); loadLibrary(); } catch (e) { onErr(e); } }
async function clearJobs() { try { await api("/api/jobs/clear", { method: "POST" }); loadLibrary(); } catch (e) { onErr(e); } }

/* --- recording detail with synced player --- */
let YTP = null, YT_READY = false, YT_TICK = null;
function loadYT() { return new Promise((res) => { if (window.YT && window.YT.Player) return res(); window.onYouTubeIframeAPIReady = () => res(); if (!$("#yt-api")) { const sc = document.createElement("script"); sc.id = "yt-api"; sc.src = "https://www.youtube.com/iframe_api"; document.head.appendChild(sc); } }); }
async function loadSourceDetail(id) {
  try {
    const src = await api(`/api/sources/${id}`);
    const el = $("#lib-detail");
    if (el.dataset.sig === JSON.stringify([src.status, src.n_speakers, src.speakers.map((s) => [s.person_id, s.assignment, s.candidates.map((c) => c.status)])]) && el.innerHTML) return; // nothing changed: keep players alive
    el.dataset.sig = JSON.stringify([src.status, src.n_speakers, src.speakers.map((s) => [s.person_id, s.assignment, s.candidates.map((c) => c.status)])]);
    el.innerHTML = `<button class="back" onclick="go('library')">← Library</button><div class="card">${sourceView(src)}</div>`;
    if (src.media_kind === "youtube") {
      await loadYT();
      YTP = new YT.Player(`yt-${src.id}`, { videoId: src.youtube_id, playerVars: { rel: 0, modestbranding: 1 }, events: { onReady: () => {}, onStateChange: (e) => {
        clearInterval(YT_TICK); if (e.data === 1) YT_TICK = setInterval(() => { const c = $(`#tl-${src.id} .cursor`); if (c && src.duration_s) c.style.left = (YTP.getCurrentTime() / src.duration_s) * 100 + "%"; }, 500); } } });
    } else YTP = null;
    $$(".tab:not(.hidden) *[data-autofocus]").forEach((x) => x.focus());
  } catch (e) { onErr(e); }
}
function sourceView(src) {
  const st = `<span class="pill ${src.status}">${src.status}</span>${src.incognito ? '<span class="pill">identify only</span>' : ""}`;
  const job = src.job && src.job.status === "running" ? `<div class="sub">${esc(src.job.stage)} ${Math.round(src.job.progress)}%</div>` : "";
  const err = src.error ? `<div class="warnbox">${esc(src.error)}</div>` : "";
  const player = src.media_kind === "youtube" ? `<div class="player"><div id="yt-${src.id}"></div></div>`
    : src.media_kind === "video" ? `<div class="player"><video id="aud-${src.id}" controls preload="metadata" src="/api/sources/${src.id}/media"></video></div>`
    : src.has_audio ? `<div class="player"><audio controls preload="metadata" id="aud-${src.id}" src="/api/sources/${src.id}/media" style="max-width:100%"></audio></div>` : "";
  return `<div class="hdr"><h2>${esc(src.name)} ${st}</h2>
    <div class="q">${src.duration_s ? fmtT(src.duration_s) : ""} · ${src.n_speakers ?? "?"} voices · added ${esc((src.created_at || "").slice(0, 16).replace("T", " "))}${src.url ? ` · <a class="link" href="${esc(src.url)}" target="_blank">source ↗</a>` : ""}</div></div>${job}${err}
    ${player}${timeline(src)}
    <p class="muted" style="font-size:12px">Click a block on the timeline, a timestamp chip, or <b>▶ watch longest turn</b> to jump the player there and see who is talking.</p>
    <div class="actions">
      ${src.incognito ? "" : `<button class="small" onclick="forgetSource(${src.id})" title="Remove everything this recording added to the library">Forget voices from this file</button>`}
      <button class="small" onclick="reprocess(${src.id})">Reprocess</button>
      <button class="small danger" onclick="deleteSource(${src.id})">Delete recording</button>
    </div>
    ${(src.speakers || []).map((sp, i) => speakerCard(sp, src, i)).join("") || (src.status === "done" ? '<p class="muted">No speakers passed the minimum speech length.</p>' : "")}`;
}
function timeline(src) {
  if (!src.duration_s || !(src.speakers || []).length) return "";
  const d = src.duration_s;
  const segs = src.speakers.map((sp, i) => (sp.segments || []).map((g) => `<div class="seg" style="left:${(g.start_s / d) * 100}%;width:${Math.max(0.15, ((g.end_s - g.start_s) / d) * 100)}%;background:${COLORS[i % COLORS.length]}" title="${esc(sp.person?.name || sp.label)} ${fmtT(g.start_s)}–${fmtT(g.end_s)}" onclick="event.stopPropagation();seek(${src.id}, ${g.start_s})"></div>`).join("")).join("");
  const legend = src.speakers.map((sp, i) => `<span><i style="background:${COLORS[i % COLORS.length]}"></i>${esc(sp.person?.name || sp.label)}</span>`).join("");
  return `<div class="timeline" id="tl-${src.id}" onclick="seekFrac(${src.id}, event)">${segs}<div class="cursor" style="left:0"></div></div><div class="tl-legend">${legend}</div>`;
}
function seek(sid, t) {
  if (YTP && YTP.seekTo) { YTP.seekTo(t, true); YTP.playVideo(); $(`#yt-${sid}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }); return; }
  const a = $(`#aud-${sid}`); if (!a) return;
  const doSeek = () => { a.currentTime = Math.min(t, a.duration || t); a.play().catch(() => {}); };
  if (a.readyState >= 1) doSeek(); else { a.addEventListener("loadedmetadata", doSeek, { once: true }); a.load(); }
  a.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function seekFrac(sid, ev) { const tl = $(`#tl-${sid}`); if (!tl) return; const r = tl.getBoundingClientRect(); const dur = YTP && YTP.getDuration ? YTP.getDuration() : $(`#aud-${sid}`)?.duration; if (!dur) return; seek(sid, ((ev.clientX - r.left) / r.width) * dur); }
document.addEventListener("timeupdate", (e) => { const a = e.target; if (!(a instanceof HTMLMediaElement) || !a.id.startsWith("aud-")) return; const c = $(`#tl-${a.id.slice(4)} .cursor`); if (c && a.duration) c.style.left = (a.currentTime / a.duration) * 100 + "%"; }, true);

function qualityWarning(sp) {
  const w = [];
  if ((sp.speech_seconds || 0) < 10) w.push("under 10 s of speech");
  if ((sp.snr_db ?? 99) < 10) w.push("low SNR");
  if ((sp.clip_fraction || 0) > 0.01) w.push("clipping");
  return w.length ? `<div class="warnbox">Low-confidence clip (${w.join(", ")}).</div>` : "";
}
function candidateTable(sp) {
  const thr = Number(SETTINGS.match_threshold || 0.75), hi = Number(SETTINGS.confirm_threshold || 0.9);
  if (!sp.candidates.length) return `<div class="muted">No candidates yet (nobody in the library to compare with).</div>`;
  return `<table><tr><th>#</th><th>Candidate</th><th>Probability</th><th>Margin</th><th>Heard in</th><th></th></tr>` +
    sp.candidates.map((c) => {
      const act = c.status === "confirmed" ? `<span class="pill named">confirmed</span> <button class="small" onclick="unassign(${sp.id})">undo</button>`
        : c.status === "rejected" ? `<span class="pill">rejected</span>`
        : `<button class="small primary" onclick="confirmMatch(${c.id}, ${c.probability})">Confirm</button> <button class="small" onclick="rejectMatch(${c.id})">Reject</button>`;
      return `<tr><td>${c.rank}</td><td><a class="link" onclick="openPerson(${c.person_id})">${esc(c.person_name)}</a><span class="pill ${c.person_kind}">${c.person_kind === "named" ? "known" : "unnamed"}</span>${sp.person_id === c.person_id ? '<span class="pill named">assigned</span>' : ""}</td>
        <td><span class="prob ${probCls(c.probability, thr, hi)}">${pct(c.probability)}</span><div class="pbar"><i style="width:${pct(c.probability)}"></i></div></td>
        <td>${c.rank === 1 ? "+" + pct(c.margin) : ""}</td><td class="sub">${heardIn(c.heard_in)}</td><td>${act}</td></tr>`;
    }).join("") + `</table>`;
}
function speakerCard(sp, src, idx = 0) {
  const color = COLORS[idx % COLORS.length];
  const thr = Number(src.settings?.match_threshold ?? SETTINGS.match_threshold ?? 0.75);
  const top = sp.candidates[0];
  let verdict;
  if (sp.person && sp.assignment === "enrolled") verdict = `Enrolled as <b>${esc(sp.person.name)}</b>`;
  else if (sp.person && (sp.assignment === "confirmed" || sp.assignment === "named")) verdict = `<a class="link" onclick="openPerson(${sp.person.id})"><b>${esc(sp.person.name)}</b></a> <span class="pill named">${sp.assignment}</span>`;
  else if (top && top.probability >= thr && top.person_kind === "named") verdict = `Probably <a class="link" onclick="openPerson(${top.person_id})"><b>${esc(top.person_name)}</b></a> (${pct(top.probability)})`;
  else if (sp.person && sp.person.kind === "provisional") verdict = `<a class="link" onclick="openPerson(${sp.person.id})">${esc(sp.person.name)}</a> <span class="muted">(unnamed)</span>`;
  else verdict = `<b>Unknown</b>${src.incognito ? ' <span class="muted">(not saved)</span>' : ""}`;
  const lg = sp.longest ? `<button class="small" onclick="seek(${src.id}, ${sp.longest.start_s})">▶ watch longest turn (${fmtT(sp.longest.start_s)}, ${sp.longest.seconds}s)</button>` : "";
  const chips = (sp.segments || []).slice(0, 10).map((g) => `<button onclick="seek(${src.id}, ${g.start_s})">${fmtT(g.start_s)}</button>`).join("");
  return `<div class="spk" style="border-left:3px solid ${color}">
    <div class="hdr"><div><span class="title">${esc(sp.label)}</span> · ${verdict}</div>
      <div class="q">speech <b>${fmtT(sp.speech_seconds)}</b> · quality <b>${sp.quality_score?.toFixed(0) ?? "-"}</b> · SNR <b>${sp.snr_db?.toFixed(0) ?? "-"} dB</b> · ${sp.n_segments} turns</div></div>
    <div class="actions" style="margin:6px 0">${lg}<div class="chips">${chips}${sp.segments.length > 10 ? `<span class="muted">+${sp.segments.length - 10}</span>` : ""}</div></div>
    ${sp.clip_url ? `<div class="turn">preview clip (best ${SETTINGS.clip_seconds || 8} s of this voice)</div>${clip(sp.clip_url)}` : ""}
    ${qualityWarning(sp)}
    ${candidateTable(sp)}
    <div class="actions">
      <input placeholder="${sp.person ? "Rename / reassign…" : "Who is this? Type a name…"}" list="person-names" id="nm-${sp.id}" style="max-width:260px">
      <button class="small ${sp.person ? "" : "primary"}" onclick="nameSpeaker(${sp.id})">${sp.person ? "Name" : "Add to library"}</button>
      ${sp.person_id ? `<button class="small" onclick="unassign(${sp.id})">Unassign / undo</button>` : ""}
    </div></div>`;
}

/* --- actions --- */
async function confirmMatch(id, p) {
  const hi = Number(SETTINGS.confirm_threshold || 0.9);
  if (p < hi && !confirm(`This match is ${pct(p)}, below your ${pct(hi)} high-confidence bar.\n\nConfirming adds this voice to the person's prints. Confirm anyway?`)) return;
  try { await api(`/api/matches/${id}/confirm`, { method: "POST" }); toast("Confirmed"); render(); loadStats(); } catch (e) { onErr(e); }
}
async function rejectMatch(id) { try { await api(`/api/matches/${id}/reject`, { method: "POST" }); toast("Rejected"); render(); } catch (e) { onErr(e); } }
async function unassign(id) { try { await api(`/api/speakers/${id}/unassign`, { method: "POST" }); toast("Unassigned"); render(); loadStats(); } catch (e) { onErr(e); } }
async function nameSpeaker(id) {
  const name = $(`#nm-${id}`).value.trim(); if (!name) return toast("Type a name first", true);
  try { await api(`/api/speakers/${id}/name`, { method: "POST", body: { name } }); toast(`Saved as ${name}`); render(); loadStats(); } catch (e) { onErr(e); }
}
async function forgetSource(id) { if (!confirm("Remove all prints and unnamed voices created from this recording? Results stay visible.")) return; try { await api(`/api/sources/${id}/forget`, { method: "POST" }); toast("Forgotten"); render(); loadStats(); } catch (e) { onErr(e); } }
async function reprocess(id) { try { await api(`/api/sources/${id}/reprocess`, { method: "POST" }); toast("Queued for reprocessing"); render(); } catch (e) { onErr(e); } }
async function deleteSource(id) { if (!confirm("Delete this recording, its clips and everything it added to the library?")) return; try { await api(`/api/sources/${id}`, { method: "DELETE" }); toast("Deleted"); go("library"); loadStats(); } catch (e) { onErr(e); } }

/* --- youtube channel import --- */
let YTL = { title: null, videos: [] };
$("#btn-yt-list").onclick = async () => {
  const url = $("#yt-url").value.trim(); if (!url) return;
  const out = $("#yt-results"); out.innerHTML = '<p class="muted">Listing… (a big channel takes 10-30 s)</p>';
  try {
    const body = { url, limit: Number($("#yt-limit").value || 300), title_regex: $("#yt-filter").value.trim() || null,
      min_minutes: $("#yt-min").value ? Number($("#yt-min").value) : null, max_minutes: $("#yt-max").value ? Number($("#yt-max").value) : null, date_from: $("#yt-from").value || null };
    const j = await api("/api/youtube/list", { method: "POST", body }); YTL = j;
    const fresh = j.videos.filter((v) => !v.already_imported);
    out.innerHTML = `<div class="hdr"><b>${esc(j.title || url)}</b> <span class="muted">${j.videos.length} match · ${fmtT(j.total_minutes * 60)} · ${fresh.length} new</span>
      <div class="actions"><button class="small" onclick="ytAll(true)">all new</button><button class="small" onclick="ytAll(false)">none</button><button class="small primary" onclick="ytImport()">Queue selected</button></div></div>
      <div style="max-height:360px;overflow:auto">${j.videos.map((v, i) => `<div class="ytrow"><input type="checkbox" data-i="${i}" ${v.already_imported ? "disabled" : "checked"}><div>${esc(v.title)}${v.already_imported ? ` <a class="link" onclick="openSource(${v.already_imported})">(in library)</a>` : ""}</div><div class="muted">${v.duration_s ? fmtT(v.duration_s) : ""}</div><div class="muted">${esc(v.date || "")}</div></div>`).join("")}</div>`;
  } catch (e) { out.innerHTML = ""; onErr(e); }
};
function ytAll(on) { $$("#yt-results input[type=checkbox]:not(:disabled)").forEach((c) => (c.checked = on)); }
async function ytImport() {
  const sel = $$("#yt-results input[type=checkbox]:checked").map((c) => YTL.videos[Number(c.dataset.i)]);
  if (!sel.length) return toast("Nothing selected", true);
  try { const r = await api("/api/youtube/import", { method: "POST", body: { videos: sel, mode: "bulk", prefix: $("#yt-prefix").checked ? YTL.title : null } });
    toast(`Queued ${r.created.length}${r.skipped ? `, ${r.skipped} already in the library` : ""}`); loadLibrary(); } catch (e) { onErr(e); }
}

/* --- feeds --- */
async function loadFeeds() {
  try {
    const j = await api("/api/feeds");
    $("#feeds-count").textContent = j.feeds.length ? `(${j.feeds.length})` : "";
    $("#feeds").innerHTML = j.feeds.map((f) => `<div class="item"><div class="title">${esc(f.title || f.url)} <span class="pill">${f.mode}</span>${f.enabled ? "" : '<span class="pill failed">paused</span>'}</div>
      <div class="sub">${f.n_items} items · every ${f.interval_min} min · last check ${f.last_checked_at ? esc(f.last_checked_at.slice(0, 16).replace("T", " ")) : "never"}${f.last_error ? `<div class="warnbox">${esc(f.last_error)}</div>` : ""}</div>
      <div class="actions"><button class="small" onclick="pollFeed(${f.id})">Check now</button><button class="small" onclick="toggleFeed(${f.id}, ${f.enabled ? "false" : "true"})">${f.enabled ? "Pause" : "Resume"}</button><button class="small danger" onclick="deleteFeed(${f.id})">Remove</button></div></div>`).join("") || '<p class="muted">No feeds yet.</p>';
  } catch (e) {}
}
$("#btn-add-feed").onclick = async () => {
  const url = $("#feed-url").value.trim(); if (!url) return;
  try { const f = await api("/api/feeds", { method: "POST", body: { url, mode: $("#feed-mode").value, interval_min: Number($("#feed-interval").value || 60), backfill: Number($("#feed-backfill").value || 0) } });
    $("#feed-url").value = ""; try { const r = await api(`/api/feeds/${f.id}/poll`, { method: "POST" }); toast(`Feed OK: queued ${r.queued}`); } catch (e) { onErr(e); } loadFeeds(); loadStats(); } catch (e) { onErr(e); }
};
async function pollFeed(id) { try { const r = await api(`/api/feeds/${id}/poll`, { method: "POST" }); toast(`Queued ${r.queued} new`); loadFeeds(); } catch (e) { onErr(e); } }
async function toggleFeed(id, enabled) { try { await api(`/api/feeds/${id}`, { method: "PATCH", body: { enabled } }); loadFeeds(); } catch (e) { onErr(e); } }
async function deleteFeed(id) { if (!confirm("Stop watching this feed?")) return; try { await api(`/api/feeds/${id}`, { method: "DELETE" }); loadFeeds(); } catch (e) { onErr(e); } }

/* ---------------- PEOPLE ---------------- */
$$("#people-seg button").forEach((b) => b.addEventListener("click", () => { STATE.peopleView = b.dataset.view; loadPeople(); }));
$("#person-search").addEventListener("input", () => { clearTimeout(STATE._ps); STATE._ps = setTimeout(loadPeople, 200); });
$("#btn-new-person").onclick = () => { $("#identify-box").open = true; $("#identify-name").focus(); };
async function loadNames() {
  const j = await api("/api/persons?kind=named").catch(() => ({ persons: [] }));
  $("#person-names").innerHTML = j.persons.map((p) => `<option value="${esc(p.name)}">`).join("");
  return j.persons;
}
async function loadPeople() {
  if (STATE.personId) { $("#people-list").classList.add("hidden"); $("#people-detail").classList.remove("hidden"); return loadPersonDetail(STATE.personId); }
  $("#people-detail").classList.add("hidden"); $("#people-list").classList.remove("hidden");
  $$("#people-seg button").forEach((b) => b.classList.toggle("active", b.dataset.view === STATE.peopleView));
  const q = $("#person-search").value.trim().toLowerCase(); const el = $("#people-content");
  try {
    if (STATE.peopleView === "known") {
      const named = await loadNames();
      $$("#people-seg button")[0].textContent = `Known (${named.length})`;
      el.innerHTML = named.filter((p) => p.name.toLowerCase().includes(q)).map((p) => `<div class="item" style="display:flex;gap:10px;align-items:center">${glyphSvg(p.glyph)}<div style="flex:1"><a class="link" onclick="openPerson(${p.id})"><b>${esc(p.name)}</b></a>${bell(p)}
        <div class="muted" style="font-size:12px">${p.n_sources} recordings · ${fmtT(p.speech_seconds)} of speech · ${p.n_prints} prints</div></div><button class="small" onclick="openPerson(${p.id})">Open</button></div>`).join("") || `<p class="muted">No known people yet. Name an unnamed voice, or identify someone from reference audio.</p>`;
    } else if (STATE.peopleView === "unnamed") {
      const [u, named] = await Promise.all([api("/api/voices/unnamed"), loadNames()]);
      $$("#people-seg button")[1].textContent = `Unnamed voices (${u.voices.length})`;
      const mergeOpts = (self) => `<option value="">Merge into…</option>` + u.voices.filter((v) => v.id !== self).map((v) => `<option value="${v.id}">${esc(v.name)}</option>`).join("") + named.map((p) => `<option value="${p.id}">${esc(p.name)} (known)</option>`).join("");
      el.innerHTML = `<p class="muted">Background data, most-heard first. Open one to watch where it was heard; <b>Name</b> it if you know who it is; <b>Keep</b> tracks it as a known voice without a name.</p><div class="grid">` + u.voices.filter((v) => v.name.toLowerCase().includes(q)).map((v) => `<div class="vcard">
        <div class="nm"><a class="link" onclick="openPerson(${v.id})">${esc(v.name)}</a>${bell(v)} <span class="muted">· ${v.n_sources} rec · ${fmtT(v.speech_seconds)}</span></div>
        ${v.heard_in.slice(0, 2).map((h) => `<div class="sub"><a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> @ ${fmtT(h.first_start_s)}</div>${clip(h.clip_url)}`).join("")}
        <div class="inline"><input placeholder="Name" list="person-names" id="pn-${v.id}"><button class="small primary" onclick="namePerson(${v.id})">Name</button></div>
        <div class="inline"><button class="small" onclick="acceptPerson(${v.id})">Keep</button><select id="mg-${v.id}">${mergeOpts(v.id)}</select><button class="small" onclick="mergePerson(${v.id})">Merge</button><button class="small danger" onclick="deletePerson(${v.id})">Delete</button></div></div>`).join("") + `</div>` || `<p class="muted">No unnamed voices yet.</p>`;
    } else {
      const j = await api("/api/voices/merge_suggestions");
      $$("#people-seg button")[2].textContent = `Possible duplicates (${j.pairs.length})`;
      el.innerHTML = j.pairs.length ? `<p class="muted">Different mics or rooms can split one person into several entries. Listen to both, then merge.</p>` + j.pairs.map((p) => `<div class="item"><div class="title"><a class="link" onclick="openPerson(${p.a.id})">${esc(p.a.name)}</a><span class="pill ${p.a.kind}">${p.a.kind === "named" ? "known" : "unnamed"}</span> ≈ <a class="link" onclick="openPerson(${p.b.id})">${esc(p.b.name)}</a><span class="pill ${p.b.kind}">${p.b.kind === "named" ? "known" : "unnamed"}</span> <span class="prob hi">${pct(p.probability)}</span></div>
        <div class="row" style="margin:4px 0 0">${[p.a, p.b].map((x) => `<div class="col">${clip(x.heard_in[0]?.clip_url) || '<span class="muted">no clip</span>'}</div>`).join("")}</div>
        <div class="actions"><button class="small primary" onclick="mergeInto(${p.a.kind === "named" ? p.b.id : p.a.id}, ${p.a.kind === "named" ? p.a.id : p.b.id})">Merge into ${esc(p.a.kind === "named" ? p.a.name : p.b.name)}</button>${p.a.kind !== "named" && p.b.kind !== "named" ? ` <button class="small" onclick="mergeInto(${p.b.id}, ${p.a.id})">Merge into ${esc(p.a.name)}</button>` : ""}</div></div>`).join("") : `<p class="muted">No likely duplicates right now.</p>`;
    }
  } catch (e) { onErr(e); }
}
async function namePerson(id) {
  const name = $(`#pn-${id}`).value.trim(); if (!name) return toast("Type a name first", true);
  try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); toast(`Named ${name}`); render(); loadStats(); } catch (e) { onErr(e); }
}
async function mergePerson(id) { const into = $(`#mg-${id}`).value; if (!into) return; mergeInto(id, Number(into)); }
async function mergeInto(fromId, intoId) { if (!confirm("Merge? Prints and recordings move over; the other entry is deleted.")) return; try { await api(`/api/persons/${fromId}/merge`, { method: "POST", body: { into_id: intoId } }); toast("Merged"); render(); loadStats(); } catch (e) { onErr(e); } }
async function deletePerson(id) { if (!confirm("Delete this voice and all its prints?")) return; try { await api(`/api/persons/${id}`, { method: "DELETE" }); toast("Deleted"); go("people"); loadStats(); } catch (e) { onErr(e); } }
async function acceptPerson(id) { try { const p = await api(`/api/persons/${id}/accept`, { method: "POST" }); toast(`Kept as ${p.name}`); render(); loadStats(); } catch (e) { onErr(e); } }
async function renamePerson(id) { const name = prompt("New name?"); if (!name) return; try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); render(); } catch (e) { onErr(e); } }

/* --- person profile --- */
async function loadPersonDetail(id) {
  try {
    const p = await api(`/api/persons/${id}`);
    const el = $("#people-detail");
    el.innerHTML = `<button class="back" onclick="go('people')">← People</button>
      <div class="card">
        <div class="profile">${glyphSvg(p.glyph, p.kind === "named" ? "var(--s1)" : "var(--s3)", 56)}
          <div><h2>${esc(p.name)} <span class="pill ${p.kind}">${p.kind === "named" ? "known" : "unnamed"}</span>${bell(p)}</h2>
          <div class="meta">${p.n_sources} recordings · ${fmtT(p.speech_seconds)} of speech · ${p.n_prints} prints${p.notes ? " · " + esc(p.notes) : ""}</div></div>
          <div class="actions" style="margin-left:auto">${p.kind === "provisional" ? `<input id="pn-${p.id}" placeholder="Name this voice" list="person-names" style="max-width:200px"><button class="small primary" onclick="namePerson(${p.id})">Name</button><button class="small" onclick="acceptPerson(${p.id})">Keep</button>` : `<button class="small" onclick="renamePerson(${p.id})">Rename</button>`}<button class="small danger" onclick="deletePerson(${p.id})">Delete</button></div>
        </div>
        <details ${p.alert ? "open" : ""}><summary>Alert: alarm + spoken line ${p.alert ? "(on)" : "(off, click the bell to turn on)"}</summary>
          <div class="inline" style="flex-wrap:wrap;align-items:flex-start;margin-top:8px">
            <select id="as-${p.id}" style="flex:0 0 170px"><option value="" ${!p.alert_sound ? "selected" : ""}>default alarm (${esc(SETTINGS.alert_default_sound || "siren")})</option>${["siren", "klaxon", "chime", "none"].map((k) => `<option value="${k}" ${p.alert_sound === k ? "selected" : ""}>${k}</option>`).join("")}</select>
            <textarea id="am-${p.id}" data-name="${esc(p.name)}" rows="2" style="flex:1 1 320px" placeholder="${esc(SETTINGS.alert_default_message || "")}">${esc(p.alert_message || "")}</textarea>
            <button class="small primary" onclick="saveAlertSettings(${p.id})">Save</button><button class="small" onclick="testAlert(${p.id})">▶ Test</button></div>
          <div class="sub">Will say: “${esc(p.spoken)}”. <code>[NAME]</code> = ${esc(p.name)}; empty = default from Settings.</div>
        </details>
        <h3>Heard in ${p.heard_in.length} recordings</h3>
        ${p.heard_in.map((h) => `<div class="appear"><div><a class="link" onclick="openSource(${h.id})"><b>${esc(h.name)}</b></a> <span class="muted">from ${fmtT(h.first_start_s)} · <span class="pill">${esc(h.assignment)}</span></span>${clip(h.clip_url)}</div>
          <div class="actions"><button class="small" onclick="openSource(${h.id})">Open recording</button><button class="small" onclick="unassign(${h.speaker_id})" title="This recording is not them">Not them</button></div></div>`).join("") || '<p class="muted">No recordings yet (enrolled from the command line or imported).</p>'}
        <details><summary>Details: prints and recent matches</summary>
          <table><tr><th>print</th><th>type</th><th>origin</th><th>recording</th><th>speech</th><th>added</th></tr>
          ${p.prints.map((pr) => `<tr><td>${pr.id}</td><td>${pr.is_mean ? "mean" : "chunk"}</td><td>${esc(pr.origin)}</td><td>${pr.source_id ? `<a class="link" onclick="openSource(${pr.source_id})">${esc(pr.source_name)}</a> / ${esc(pr.label)}` : "cli / import"}</td><td>${pr.speech_seconds ? fmtT(pr.speech_seconds) : ""}</td><td class="muted">${esc((pr.created_at || "").slice(0, 16))}</td></tr>`).join("")}</table>
          <h3>Recent matches</h3>${p.matches.slice(0, 20).map((m) => `<div class="item"><a class="link" onclick="openSource(${m.source_id})">${esc(m.source_name)}</a> / ${esc(m.label)} · <span class="prob">${pct(m.probability)}</span> <span class="pill">${m.status}</span></div>`).join("") || '<p class="muted">none</p>'}
        </details>
      </div>`;
  } catch (e) { onErr(e); }
}

/* ---------------- alarms & speech (browser) ---------------- */
let ALARM_AC = null;
function alarmCtx() { ALARM_AC = ALARM_AC || new (window.AudioContext || window.webkitAudioContext)(); if (ALARM_AC.state === "suspended") ALARM_AC.resume(); return ALARM_AC; }
function beep() { try { const ac = alarmCtx(); const o = ac.createOscillator(), g = ac.createGain(); o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.18); } catch (e) {} }
function playAlarm(kind) {
  kind = kind || SETTINGS.alert_default_sound || "siren";
  if (kind === "none") return 0;
  const ac = alarmCtx(), t0 = ac.currentTime, g = ac.createGain();
  if (kind === "siren") {
    const dur = 8; const lp = ac.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 2200; g.connect(lp); lp.connect(ac.destination);
    g.gain.setValueAtTime(0.0001, t0); g.gain.exponentialRampToValueAtTime(0.5, t0 + 0.6); g.gain.setValueAtTime(0.5, t0 + dur - 0.8); g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    [1, 2].forEach((h, i) => { const o = ac.createOscillator(); o.type = "sawtooth"; const og = ac.createGain(); og.gain.value = i ? 0.25 : 0.7; o.connect(og); og.connect(g);
      for (let c = 0; c < 2; c++) { o.frequency.setValueAtTime(400 * h, t0 + c * 4); o.frequency.exponentialRampToValueAtTime(800 * h, t0 + c * 4 + 2); o.frequency.exponentialRampToValueAtTime(400 * h, t0 + c * 4 + 4); }
      o.start(t0); o.stop(t0 + dur); });
    return dur;
  }
  g.connect(ac.destination);
  if (kind === "klaxon") { const dur = 4; const o = ac.createOscillator(); o.type = "square"; o.frequency.value = 330; o.connect(g); g.gain.value = 0;
    for (let i = 0; i < 8; i++) { g.gain.setValueAtTime(0.35, t0 + i * 0.5); g.gain.setValueAtTime(0, t0 + i * 0.5 + 0.3); } o.start(t0); o.stop(t0 + dur); return dur; }
  [660, 880, 1320].forEach((f, i) => { const o = ac.createOscillator(); o.type = "sine"; o.frequency.value = f; const og = ac.createGain(); og.gain.setValueAtTime(0.25, t0 + i * 0.18); og.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.18 + 1.2); o.connect(og); og.connect(ac.destination); o.start(t0 + i * 0.18); o.stop(t0 + i * 0.18 + 1.3); });
  return 1.8;
}
function speak(text) { if (!("speechSynthesis" in window) || !text) return; speechSynthesis.cancel(); const u = new SpeechSynthesisUtterance(text); u.rate = 0.95; const v = speechSynthesis.getVoices().find((v) => /en[-_]US/i.test(v.lang)); if (v) u.voice = v; speechSynthesis.speak(u); }
const renderMessage = (tpl, name) => (tpl || SETTINGS.alert_default_message || "").replace(/\[NAME\]/g, name).replace(/\{name\}/g, name);
function runAlert(name, sound, message) { const d = playAlarm(sound); setTimeout(() => speak(renderMessage(message, name)), Math.max(0, d * 1000 - 400)); }
function testAlert(personId) { runAlert($(`#am-${personId}`)?.dataset.name || "this person", $(`#as-${personId}`)?.value || null, $(`#am-${personId}`)?.value || null); }
async function saveAlertSettings(personId) { try { await api(`/api/persons/${personId}`, { method: "PATCH", body: { alert_sound: $(`#as-${personId}`).value, alert_message: $(`#am-${personId}`).value } }); toast("Alert saved"); } catch (e) { onErr(e); } }

/* ---------------- LIVE ---------------- */
let LIVE = null;
$("#btn-live").onclick = async () => {
  if (LIVE) { stopLive(); return; }
  const st = $("#live-status");
  if (!navigator.mediaDevices?.getUserMedia) { st.textContent = "Microphone needs localhost or HTTPS."; return; }
  if ($("#live-notify").checked && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true, channelCount: 1 } });
    let ac; try { ac = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 }); } catch (e) { ac = new (window.AudioContext || window.webkitAudioContext)(); }
    const src = ac.createMediaStreamSource(stream);
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live"); ws.binaryType = "arraybuffer";
    const ratio = ac.sampleRate / 16000; const proc = ac.createScriptProcessor(4096, 1, 1); let carry = new Float32Array(0);
    proc.onaudioprocess = (e) => {
      if (ws.readyState !== 1) return;
      const raw = e.inputBuffer.getChannelData(0); const inp = new Float32Array(carry.length + raw.length); inp.set(carry); inp.set(raw, carry.length);
      const n = Math.floor(inp.length / ratio); const out = new Int16Array(n); let peak = 0;
      for (let i = 0; i < n; i++) { const a = i * ratio, b = (i + 1) * ratio; let acc = 0, cnt = 0; for (let j = Math.floor(a); j < Math.min(inp.length, Math.ceil(b)); j++) { acc += inp[j]; cnt++; } const v = Math.max(-1, Math.min(1, cnt ? acc / cnt : 0)); out[i] = v * 32767; peak = Math.max(peak, Math.abs(v)); }
      carry = inp.slice(Math.floor(n * ratio)); $("#live-level").style.width = Math.min(100, peak * 140) + "%"; ws.send(out.buffer);
    };
    src.connect(proc); proc.connect(ac.destination);
    LIVE = { stream, ac, proc, ws, lastAlert: 0, log: [] };
    $("#btn-live").textContent = "⏹ Stop listening"; st.textContent = "connecting…";
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "status") { st.textContent = m.text; return; } if (m.type === "error") { st.textContent = m.text; stopLive(); return; } renderLive(m); };
    ws.onclose = () => { if (LIVE) { st.textContent = "connection closed"; stopLive(); } };
  } catch (e) { st.textContent = "microphone blocked: " + e.message; }
};
function stopLive() { if (!LIVE) return; try { LIVE.proc.disconnect(); LIVE.ac.close(); LIVE.stream.getTracks().forEach((t) => t.stop()); LIVE.ws.close(); } catch (e) {} LIVE = null; $("#btn-live").textContent = "▶ Start listening"; $("#live-level").style.width = "0"; $("#live-now").className = "livenow"; $("#live-now").innerHTML = '<div class="muted">Not listening.</div>'; }
function renderLive(m) {
  const now = $("#live-now"); const thr = Number(SETTINGS.match_threshold || 0.75);
  if (!m.speech) { now.className = "livenow"; now.innerHTML = `<div class="muted">${m.speech_s > 0 ? `listening… ${m.speech_s}s of speech buffered` : "silence"} · ${m.level_db} dBFS</div>`; return; }
  const top = m.candidates[0]; if (!top) { now.innerHTML = `<div class="who muted">speech, empty library</div>`; return; }
  now.className = "livenow" + (m.alert ? " alert" : "");
  now.innerHTML = `<div class="muted">${m.alert && top.flagged ? "🔔 FLAGGED VOICE" : m.alert ? "KNOWN VOICE" : top.probability >= thr ? "unnamed voice from the library" : "no confident match"}</div>
    <div class="who" style="${m.alert ? "" : "opacity:.7"}">${esc(top.name)} <span class="prob ${probCls(top.probability, thr, SETTINGS.confirm_threshold || 0.9)}" style="font-size:18px">${pct(top.probability)}</span></div>
    <div class="meter"><i style="width:${pct(top.probability)}"></i></div>
    <div class="others">${m.candidates.slice(1).map((c) => `${esc(c.name)} ${pct(c.probability)}`).join(" · ")}<span style="float:right">${m.speech_s}s of speech</span></div>`;
  if (m.alert && Date.now() - LIVE.lastAlert > 8000) {
    LIVE.lastAlert = Date.now();
    if (top.flagged && SETTINGS.alert_speak_on_laptop) runAlert(top.name, top.alert_sound, top.alert_message); else if ($("#live-beep").checked) beep();
    if ($("#live-notify").checked && "Notification" in window && Notification.permission === "granted") new Notification("VoiceFlock", { body: `${top.name} (${pct(top.probability)})` });
    LIVE.log.unshift(`${new Date().toLocaleTimeString()} — ${top.name} ${pct(top.probability)}`);
    $("#live-log").innerHTML = LIVE.log.slice(0, 50).map((l) => `<div class="item">${esc(l)}</div>`).join("");
  }
}

/* ---------------- SETTINGS ---------------- */
async function loadSettings() {
  try {
    const j = await api("/api/settings"); SETTINGS = j.values; SPECS = j.specs;
    const groups = {}; Object.entries(SPECS).forEach(([k, s]) => (groups[s.group] ||= []).push(k));
    const f = $("#settings-form"); f.className = "sform";
    f.innerHTML = Object.entries(groups).map(([g, keys]) => `<div class="grp">${g}</div>` + keys.map((k) => { const s = SPECS[k], v = SETTINGS[k];
      const input = s.type === "bool" ? `<input type="checkbox" name="${k}" ${v ? "checked" : ""}>` : s.type === "secret" ? `<input type="password" name="${k}" value="${esc(v)}" autocomplete="off">` : s.type === "float" ? `<input type="number" step="0.01" name="${k}" value="${v}">` : s.type === "int" ? `<input type="number" step="1" name="${k}" value="${v}">` : (k === "alert_default_message" ? `<textarea name="${k}" rows="2">${esc(v)}</textarea>` : `<input name="${k}" value="${esc(v)}">`);
      return `<div class="f"><label>${esc(s.label)}<small>${esc(s.help)}</small></label>${input}</div>`; }).join("")).join("");
    renderSelftest(); renderPhone();
    const st = await api("/api/stats");
    $("#calib-info").textContent = `scoring mode: ${st.score_mode} (cohort ${st.cohort_size}, min ${SETTINGS.cohort_min_size}) · calibration: ${st.calibrated ? "fitted" : "defaults"} raw a=${st.calibration.raw.a.toFixed(2)} b=${st.calibration.raw.b.toFixed(2)}`;
  } catch (e) { onErr(e); }
}
$("#btn-save-settings").onclick = async (e) => {
  e.preventDefault(); const body = {}; $$("#settings-form [name]").forEach((i) => { body[i.name] = i.type === "checkbox" ? i.checked : i.value; });
  try { const j = await api("/api/settings", { method: "PUT", body }); SETTINGS = j.values; $("#settings-msg").textContent = "Saved."; toast("Settings saved"); loadStats(); } catch (er) { onErr(er); }
};
async function renderPhone() {
  try {
    const h = await api("/api/health"); const port = location.port || (location.protocol === "https:" ? 443 : 80);
    const addrs = (h.lan_ips || []).map((ip) => `http://${ip}:${port}`);
    $("#phone-info").textContent = `Direct sync address (same Wi-Fi):\n  ${addrs.join("\n  ") || location.origin}\nPassword: ${h.password_required ? "the one in .env" : "none set"}\nIf the phone can't connect, use the library file instead (above).`;
    const r = await fetch("/api/mobile/model.onnx", { method: "HEAD" }); const ms = $("#model-status");
    ms.textContent = r.status === 200 ? `laptop-side phone model ready (${Math.round((Number(r.headers.get("content-length")) || 0) / 1048576)} MB)` : "not exported on this laptop (the phone downloads it from GitHub anyway)";
  } catch (e) {}
}
$("#btn-export-model").onclick = async () => { try { await api("/api/mobile/export_model", { method: "POST" }); toast("Model export queued"); } catch (e) { onErr(e); } };
async function renderSelftest() {
  const { job } = await api("/api/selftest").catch(() => ({ job: null })); const st = $("#selftest-status"), out = $("#selftest-result");
  if (!job) { st.textContent = "never run"; out.textContent = ""; return; }
  st.innerHTML = `<span class="pill ${job.status}">${job.status}</span> ${job.status === "running" ? esc(job.stage || "") + " · " + elapsed(job.started_at) : esc((job.finished_at || job.created_at || "").slice(0, 16).replace("T", " "))}`;
  const r = job.result || {};
  out.textContent = job.status === "failed" ? (job.error || "") + "\n" + (r.traceback || "") : job.status === "done" ? `OK on ${r.device}: embedder ${r.embedder_load_s}s, diarizer ${r.diarizer_load_s}s (${r.diarization_pipeline_loaded}), 8 s test in ${r.diarize_s}s (${r.realtime_factor}× realtime), ${r.n_speakers} speakers` : "";
}
$("#btn-selftest").onclick = async () => { try { await api("/api/selftest", { method: "POST" }); toast("Self-test queued"); renderSelftest(); } catch (e) { onErr(e); } };
$("#import-gallery").addEventListener("change", async () => {
  const f = $("#import-gallery").files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
  try { const r = await fetch("/api/import/gallery", { method: "POST", body: fd }); const j = await r.json(); if (!r.ok) throw new Error(j.detail); $("#import-msg").textContent = `imported ${j.persons_created} new, ${j.persons_merged} merged, ${j.prints} prints`; loadStats(); } catch (e) { onErr(e); }
  $("#import-gallery").value = "";
});

/* ---------------- boot & polling ---------------- */
(async function boot() {
  const j = await api("/api/settings").catch(() => ({ values: {}, specs: {} })); SETTINGS = j.values; SPECS = j.specs;
  loadNames(); render(); loadStats();
  let lastBusy = 0;
  setInterval(async () => {
    const busy = await loadStats(); updateUploadRows();
    if (!(busy || lastBusy)) { lastBusy = busy; return; }
    lastBusy = busy;
    if (mediaPlaying() || document.activeElement?.matches("input,select,textarea")) return;     // never yank a playing clip or a half-typed name
    if (STATE.tab === "library") { if (STATE.sourceId) loadSourceDetail(STATE.sourceId); else loadLibrary(); }
    else if (STATE.tab === "home") loadHome();
    else if (STATE.tab === "settings") renderSelftest();
  }, 3000);
})();
