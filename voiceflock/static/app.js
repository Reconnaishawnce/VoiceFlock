/* VoiceFlock UI v2: Home · Library · People · Live · Settings. Vanilla JS over /api/*. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtT = (s) => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(x).padStart(2, "0"); };
const pct = (p) => (p * 100).toFixed(1) + "%";
const probCls = (p, thr, hi) => p >= hi ? "hi" : p >= thr ? "mid" : "lo";
const COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
const EMPTY_SVG = `<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M10 38v-12M19 46V18M28 42V22M37 50V14M46 40V24M55 36v-8"/></svg>`;
const empty = (text) => `<div class="empty">${EMPTY_SVG}<div>${text}</div></div>`;
const skeleton = (n = 4) => Array.from({ length: n }, (_, i) => `<div class="skel" style="width:${70 - (i % 3) * 15}%"></div>`).join("");
const elapsed = (iso) => { if (!iso) return ""; const s = Math.max(0, (Date.now() - Date.parse(iso + (iso.endsWith("Z") ? "" : "Z"))) / 1000); return fmtT(s); };
const APP_VERSION = "0.2.1";   // must match voiceflock/__init__.py; a mismatch means the server was not restarted after an update
let SETTINGS = {}, SPECS = {}, STATE = { tab: "home", sourceId: null, personId: null, peopleView: "known", matchSource: null };
let HEALTH = null;

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
function go(route) { STATE.navDepth = (STATE.navDepth || 0) + 1; if (location.hash !== "#" + route) location.hash = route; else render(); }
function restoreScroll() { const y = SCROLL[location.hash || "#home"]; if (y != null) requestAnimationFrame(() => window.scrollTo(0, y)); }
const SCROLL = {};
window.addEventListener("scroll", () => { SCROLL[location.hash || "#home"] = window.scrollY; }, { passive: true });
function back(fallback) { if (history.length > 1 && document.referrer !== "" || STATE.navDepth > 0) { STATE.navDepth = Math.max(0, (STATE.navDepth || 0) - 1); history.back(); } else go(fallback); }
function render() {
  if (STATE.lastHash !== location.hash) closeDrawer();
  STATE.lastHash = location.hash;
  const [path, qs] = (location.hash.replace("#", "") || "home").split("?");
  const params = Object.fromEntries((qs || "").split("&").filter(Boolean).map((kv) => kv.split("=")));
  STATE.seekTo = params.t != null ? Number(params.t) : null;
  if (params.view) STATE.peopleView = params.view;
  const [tab, id] = path.split("/");
  STATE.tab = $(`#tab-${tab}`) ? tab : "home";
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === STATE.tab));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + STATE.tab));
  if (STATE.tab === "library") { STATE.sourceId = id ? Number(id) : null; loadLibrary(); }
  else if (STATE.tab === "people") { STATE.personKind = id && id.startsWith("pkg") ? "pkg" : "person"; STATE.personId = id ? Number(id.replace("pkg", "")) : null; loadPeople(); }
  else if (STATE.tab === "home") loadHome();
  else if (STATE.tab === "settings") loadSettings();
}
window.addEventListener("hashchange", render);
const openSource = (id) => go(`library/${id}`);
const openSourceAt = (id, t) => go(`library/${id}?t=${Math.max(0, Math.floor(t || 0))}`);
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
    HEALTH = h;
    const b = $("#banner"); const g = h.gpu || {};
    if (h.version && h.version !== APP_VERSION) { b.innerHTML = `The server is running VoiceFlock ${esc(h.version)} but this page is ${APP_VERSION}: it was updated without a restart, so buttons will fail with "Not Found". ${h.supervised ? `<button class="small" onclick="restartServer()">Restart now</button>` : "Close the VoiceFlock window and start VoiceFlock.bat again."}`; b.classList.remove("hidden"); }
    else if (g.nvidia_driver_gpu && !g.torch_cuda) { b.innerHTML = `NVIDIA GPU found (${esc(g.nvidia_driver_gpu)}) but VoiceFlock runs on the CPU (10-30× slower). Close VoiceFlock, run <code>gpu.bat</code>, restart.`; b.classList.remove("hidden"); }
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
function heardIn(list) { return list && list.length ? list.map((h) => `<a class="link" onclick="openSourceAt(${h.id}, ${h.watch_at ?? h.first_start_s ?? 0})" title="open cued to this voice">${esc(h.name)} ▶ ${fmtT(h.watch_at ?? h.first_start_s)}</a>`).join(", ") : `<span class="muted">(no other recordings)</span>`; }
function clip(url) { return url ? `<audio controls preload="metadata" src="${url}"></audio>` : ""; }
/** "▶ watch" + a video still for a heard-in entry: opens the recording with the video cued to this voice. */
function watchBtn(h) {
  const t = h.watch_at ?? h.first_start_s ?? 0;
  const label = h.media_kind === "audio" ? "▶ listen in context" : "▶ watch";
  return `<button class="small" onclick="openSourceAt(${h.id}, ${t})" title="open the recording cued to ${fmtT(t)}">${label} @ ${fmtT(t)}</button>`;
}
function thumb(h) {
  const t = h.watch_at ?? h.first_start_s ?? 0;
  const src = h.still_url || (h.youtube_id ? `https://i.ytimg.com/vi/${esc(h.youtube_id)}/mqdefault.jpg` : null);
  return src ? `<img class="thumb" src="${esc(src)}" alt="" loading="lazy" onclick="openSourceAt(${h.id}, ${t})" title="${h.still_url ? "still at " : "watch at "}${fmtT(t)}">` : "";
}
function heardInRich(list) {
  return list && list.length ? list.map((h) => `<div class="hi">${thumb(h)}<div><a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> <span class="muted">· ${fmtT(h.speech_seconds || 0)} of speech</span><div class="actions" style="margin:4px 0 0">${watchBtn(h)}</div>${clip(h.clip_url)}</div></div>`).join("") : `<span class="muted">(no other recordings)</span>`;
}

/* ---------------- HOME ---------------- */
let OV = null;
async function loadHome() {
  try {
    const [a, o] = await Promise.all([api("/api/alerts"), api("/api/overview")]); OV = o;
    const thr = Number(SETTINGS.match_threshold || 0.75);
    $("#alert-hint").textContent = `≥ ${pct(thr)} · confirm only when very sure (≥ ${pct(a.confirm_threshold)})`;
    api("/api/calibration").then((c) => { if (!c.fitted.raw && c.available.enough) $("#alert-hint").innerHTML += ` · <a class="link" onclick="go('settings')" title="the percentages are generic until then">calibrate from your ${c.available.labelled_speakers} decisions</a>`; }).catch(() => {});
    $("#alerts-known").innerHTML = a.known.length ? a.known.slice(0, 30).map((k) => `<div class="item">
      <div class="title"><a class="link" onclick="openPerson(${k.person_id})">${esc(k.person_name)}</a> in <a class="link" onclick="openSource(${k.source_id})">${esc(k.source_name)}</a> at ${fmtT(k.first_start_s)}${k.status === "confirmed" ? '<span class="pill named">confirmed</span>' : ""}</div>
      <div class="scoreline"><span class="prob ${probCls(k.probability, thr, a.confirm_threshold)}">${pct(k.probability)}</span><span class="muted">+${pct(k.margin)} over #2</span></div>
      <div class="sub">speech ${fmtT(k.speech_seconds)} · quality ${Math.round(k.quality_score || 0)} · also heard in: ${heardIn(k.heard_in)}</div>
      ${clip(k.clip_url)}
      <div class="actions">${k.status === "confirmed" ? `<button class="small" onclick="unassign(${k.speaker_id})">Undo confirm</button>` : `<button class="small primary" onclick="confirmMatch(${k.match_id}, ${k.probability})">Confirm</button><button class="small" onclick="rejectMatch(${k.match_id})">Reject</button>`}
        <button class="small" onclick="openSourceAt(${k.source_id}, ${k.first_start_s || 0})">▶ watch @ ${fmtT(k.first_start_s)}</button></div></div>`).join("") : empty("Nothing to review yet. Add recordings in Library, then identify people in People.");
    $("#alerts-recurring").innerHTML = a.recurring_unknown.length ? a.recurring_unknown.slice(0, 10).map((r) => `<div class="item">
      <div class="title"><a class="link" onclick="openPerson(${r.person_id})">${esc(r.name)}</a> <span class="muted">· ${r.n_sources} recordings · ${fmtT(r.speech_seconds)}</span></div>
      ${heardInRich(r.heard_in.slice(0, 1))}
      <div class="inline"><input placeholder="Name this voice" list="person-names" id="pn-${r.person_id}" onkeydown="if(event.key==='Enter')namePerson(${r.person_id}, this)"><button class="small" onclick="namePerson(${r.person_id}, this)">Name</button></div></div>`).join("") : empty("No unnamed voice has shown up twice yet.");
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
  for (const p of pts) { const col = p.v.kind === "named" ? "#3987e5" : "#199e70"; const g = ctx.createRadialGradient(p.x, p.y, p.r * 0.2, p.x, p.y, p.r * 2.2); g.addColorStop(0, col + "55"); g.addColorStop(1, col + "00"); ctx.fillStyle = g; ctx.beginPath(); ctx.arc(p.x, p.y, p.r * 2.2, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2); ctx.fillStyle = col; ctx.globalAlpha = 0.92; ctx.fill(); ctx.globalAlpha = 1; ctx.lineWidth = 1.5; ctx.strokeStyle = "#131619"; ctx.stroke(); }
  ctx.font = "500 12px Inter, system-ui"; ctx.lineWidth = 4; ctx.strokeStyle = "rgba(19,22,25,.9)"; ctx.fillStyle = "#ecebe6";
  for (const p of pts.filter((p) => p.v.kind === "named").sort((a, b) => b.r - a.r).slice(0, 12)) { ctx.strokeText(p.v.name, p.x + p.r + 5, p.y + 4); ctx.fillText(p.v.name, p.x + p.r + 5, p.y + 4); }
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
  if (!$("#sources").innerHTML) $("#sources").innerHTML = skeleton(5);
  loadFeeds();
  try {
    const qs = encodeURIComponent($("#source-search").value.trim());
    const [j, q] = await Promise.all([api(`/api/sources?limit=300&q=${qs}`), api("/api/jobs?limit=60")]);
    const active = q.jobs.filter((x) => x.status === "running" || x.status === "queued");
    const failed = q.jobs.filter((x) => x.status === "failed");
    const label = (x) => x.type === "process_source" ? `<a class="link" onclick="openSource(${x.source_id})">${esc(x.source_name || "#" + x.source_id)}</a>` : `<b>${esc(x.type)}</b>`;
    $("#jobs").innerHTML = active.slice(0, 20).map((x) => `<div class="jobrow"><span class="pill ${x.status}">${x.status}</span>${label(x)}
      ${x.status === "running" ? `<span class="muted">${esc(x.stage || "")} ${Math.round(x.progress)}% · ${elapsed(x.started_at)} · updated ${elapsed(x.heartbeat_at) || "-"} ago</span><div class="bar"><i style="width:${x.progress}%"></i></div><button class="small" onclick="cancelJob(${x.id})">stop</button>` : `<button class="small" onclick="cancelJob(${x.id})">cancel</button>`}</div>`).join("");
    $("#problems-box").classList.toggle("hidden", !failed.length); $("#problems-count").textContent = failed.length ? `(${failed.length})` : "";
    $("#problems").innerHTML = failed.map((x) => `<div class="jobrow"><span class="pill failed">failed</span>${label(x)}<span class="muted" style="flex:1">${esc(x.error)}</span>${x.type === "process_source" ? `<button class="small" onclick="retryJob(${x.id})">retry</button>` : ""}<button class="small" onclick="dismissJob(${x.id})">dismiss</button>${x.log ? `<details style="margin:0;padding:0;border:0"><summary>details</summary><pre class="muted" style="font-size:11px;white-space:pre-wrap">${esc(x.log.slice(-1500))}</pre></details>` : ""}</div>`).join("") + (failed.length > 1 ? `<div class="actions"><button class="small" onclick="clearJobs()">Dismiss all</button></div>` : "");
    const okSources = j.sources.filter((s) => s.status !== "failed" || s.n_speakers);
    const failedSources = j.sources.filter((s) => s.status === "failed" && !s.n_speakers);
    if (failedSources.length) { $("#problems-box").classList.remove("hidden"); $("#problems-count").textContent = `(${failed.length + failedSources.length})`; $("#problems").innerHTML += failedSources.map((s) => `<div class="jobrow"><span class="pill failed">failed</span><a class="link" onclick="openSource(${s.id})">${esc(s.name)}</a><span class="muted" style="flex:1">${esc(s.error || "")}</span><button class="small" onclick="reprocess(${s.id})">retry</button><button class="small danger" onclick="deleteSource(${s.id})">delete</button></div>`).join(""); }
    const qtxt = ($("#source-search")?.value || "").trim();
    if (qtxt.length >= 3) api(`/api/transcripts/search?q=${encodeURIComponent(qtxt)}&limit=30`).then((r) => { const el = $("#tr-hits"); if (!el || ($("#source-search")?.value || "").trim() !== qtxt) return;
      el.innerHTML = r.hits.length ? `<h3>Said in recordings <span class="muted" style="text-transform:none;letter-spacing:0">· ${r.hits.length} lines</span></h3>` + r.hits.map((h) => `<div class="tr-line" onclick="openSourceAt(${h.source_id}, ${h.start_s})"><span class="tr-t">${fmtT(h.start_s)}</span><span class="tr-who">${esc(h.person_name || h.label || "")}</span><span class="tr-x">${esc(h.snippet).replace(/\[/g, "<mark>").replace(/\]/g, "</mark>")} <span class="muted">· ${esc(h.source_name)}</span></span></div>`).join("") : ""; }).catch(() => {});
    else { const el = $("#tr-hits"); if (el) el.innerHTML = ""; }
    $("#sources").innerHTML = okSources.length ? `<table><tr><th>Recording</th><th>Status</th><th>Length</th><th>Voices</th><th></th></tr>` + okSources.map((s) => `<tr>
      <td><a class="link" onclick="openSource(${s.id})">${esc(s.name)}</a>${s.incognito ? '<span class="pill">identify only</span>' : ""}${s.mode === "enroll" ? '<span class="pill">enroll</span>' : ""}${s.review ? '<span class="pill provisional">review</span>' : ""}</td>
      <td><span class="pill ${s.status}">${s.status}</span>${s.job && s.job.status === "running" ? ` <span class="muted">${Math.round(s.job.progress)}%</span>` : ""}</td>
      <td>${s.duration_s ? fmtT(s.duration_s) : ""}</td>
      <td class="sub">${(() => { const named = s.speakers.filter((sp) => sp.person_kind === "named"); const un = s.speakers.length - named.length; return named.slice(0, 8).map((sp) => `<span class="pill named">${esc(sp.person_name)}</span>`).join(" ") + (named.length > 8 ? ` <span class="muted">+${named.length - 8}</span>` : "") + (un ? ` <span class="muted">${un} unnamed</span>` : ""); })()}</td>
      <td><button class="small danger" onclick="deleteSource(${s.id})">✕</button></td></tr>`).join("") + `</table>` : empty("No recordings yet. Drop a file above, paste a link, or import a YouTube channel.");
    restoreScroll();
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
    if (el.dataset.sig === JSON.stringify([src.status, src.n_speakers, src.transcript_status, src.transcript_lines, src.suggest_job?.status, src.speakers.map((s) => [s.person_id, s.assignment, s.ignored, s.review_status, (s.name_hints || []).length, s.candidates.map((c) => c.status)])]) && el.innerHTML) return; // nothing changed: keep players alive
    el.dataset.sig = JSON.stringify([src.status, src.n_speakers, src.transcript_status, src.transcript_lines, src.suggest_job?.status, src.speakers.map((s) => [s.person_id, s.assignment, s.ignored, s.review_status, (s.name_hints || []).length, s.candidates.map((c) => c.status)])]);
    const sameSource = STATE.src?.id === src.id;
    el.innerHTML = `<button class="back" onclick="back('library')">← Back</button><div class="card">${sourceView(src)}</div>`;
    el.classList.add("tab");
    STATE.src = src;
    // the side panel shows a speaker of this recording: redraw it with the fresh data (after naming, confirming, undo)
    if (sameSource && STATE.drawerSpeaker && !$("#drawer").classList.contains("hidden")) openSpeaker(STATE.drawerSpeaker, false);
    loadTranscript(src);
    const cue = STATE.seekTo; STATE.seekTo = null;
    if (src.media_kind === "youtube") {
      await loadYT();
      YTP = new YT.Player(`yt-${src.id}`, { videoId: src.youtube_id, playerVars: { rel: 0, modestbranding: 1, playsinline: 1, start: cue != null ? Math.floor(cue) : undefined }, events: { onReady: () => { if (cue != null) { YTP.mute(); YTP.seekTo(cue, true); YTP.playVideo(); showUnmute(src.id, cue); } }, onStateChange: (e) => {
        clearInterval(YT_TICK); if (e.data === 1) YT_TICK = setInterval(() => { const t = YTP.getCurrentTime(); const c = $(`#tl-${src.id} .cursor`); if (c && src.duration_s) c.style.left = (t / src.duration_s) * 100 + "%"; syncTranscript(src.id, t); }, 500); } } });
    } else { YTP = null; if (cue != null) setTimeout(() => seek(src.id, cue), 150); }
    if (cue != null) $("#lib-detail .player")?.scrollIntoView({ block: "start", behavior: "smooth" });
  } catch (e) { onErr(e); }
}
function sourceView(src) {
  const st = `<span class="pill ${src.status}">${src.status}</span>${src.incognito ? '<span class="pill">identify only</span>' : ""}`;
  const job = src.job && src.job.status === "running" ? `<div class="sub">${esc(src.job.stage)} ${Math.round(src.job.progress)}%</div>` : "";
  const err = src.error ? `<div class="warnbox">${esc(src.error)}</div>` : "";
  const onerr = `onerror="toast('The audio for this recording could not be loaded. If the server was just updated, restart it; otherwise press Reprocess.', true)"`;
  const player = src.media_kind === "youtube" ? `<div class="player"><div id="yt-${src.id}"></div></div>`
    : src.media_kind === "video" ? `<div class="player"><video id="aud-${src.id}" controls preload="metadata" src="/api/sources/${src.id}/media" ${onerr}></video></div>`
    : src.has_audio ? `<div class="player"><audio controls preload="metadata" id="aud-${src.id}" src="/api/sources/${src.id}/media" style="max-width:100%" ${onerr}></audio></div>` : `<div class="warnbox">No audio file on disk for this recording (processed on another machine or deleted). Reprocess to rebuild it.</div>`;
  return `<div class="hdr"><h2>${esc(src.name)} ${st}</h2>
    <div class="q">${src.duration_s ? fmtT(src.duration_s) : ""} · ${src.n_speakers ?? "?"} voices · added ${esc((src.created_at || "").slice(0, 16).replace("T", " "))}${src.url ? ` · <a class="link" href="${esc(src.url)}" target="_blank">source ↗</a>` : ""}</div></div>${job}${err}
    ${player}${timeline(src)}
    <p class="muted" style="font-size:12px">Click a voice in the legend or a block on the timeline: the player jumps to them and a panel opens to name them, without leaving the video.${src.stills_info ? ` · stills: ${src.stills_info.kind === "error" || src.stills_info.kind === "none" ? `<span style="color:var(--warn)">${esc(src.stills_info.error || "none")}</span>` : `${src.stills_info.stills} from ${src.stills_info.kind}`}` : ""}</p>
    ${transcriptBox(src)}
    <div class="inline" style="align-items:flex-start"><textarea class="notes" id="src-notes" placeholder="Notes for this recording: context, date, who was present, links… (saved when you click away)" onblur="saveSourceNotes(${src.id}, this.value)">${esc(src.notes || "")}</textarea></div>
    <div class="actions">
      ${src.status === "done" && !src.incognito ? `<button class="small primary" onclick="queueReview(${src.id})" title="Put every unnamed voice of this recording in the review queue">${src.review_pending ? `Review queue: ${src.review_pending} to do` : "Add voices to review queue"}</button>` : ""}
      ${src.incognito ? "" : `<button class="small" onclick="forgetSource(${src.id})" title="Remove everything this recording added to the library">Forget voices from this file</button>`}
      ${src.status === "done" && !src.incognito ? `<button class="small" onclick="makeStills(${src.id})" title="Cut low-res frames at each speaker's longest turns">${(src.speakers || []).some((x) => (x.stills || []).length) ? "Redo stills" : "Make stills"}</button>` : ""}
      ${src.status === "done" && !src.incognito ? suggestBtn(src) : ""}
      <button class="small" onclick="reprocess(${src.id})">Reprocess</button>
      <button class="small danger" onclick="deleteSource(${src.id})">Delete recording</button>
    </div>
    ${(src.speakers || []).map((sp, i) => speakerCard(sp, src, i)).join("") || (src.status === "done" ? '<p class="muted">No speakers passed the minimum speech length.</p>' : "")}`;
}
function suggestBtn(src) {
  const j = src.suggest_job;
  if (j && (j.status === "queued" || j.status === "running")) return `<span class="muted">suggesting names… ${j.status === "running" ? `${esc(j.stage || "")} ${Math.round(j.progress || 0)}%` : "queued"}</span>`;
  const done = j && j.status === "done" && j.result;
  const tip = done ? `last run: ${j.result.with_suggestions}/${j.result.speakers} voices got suggestions · ${j.result.ocr_frames} caption reads · model: ${j.result.llm || "none"}` : "On-screen captions, title/description, transcript hints and a language model";
  return `<button class="small" onclick="suggestNow(${src.id})" title="${esc(tip)}">${done ? "Suggest names again" : "Suggest names"}</button>${j && j.status === "failed" ? `<span class="muted" style="color:var(--bad)">suggestions failed: ${esc(j.error || "")}</span>` : ""}`;
}
async function suggestNow(sid) { try { await api(`/api/sources/${sid}/suggest`, { method: "POST" }); toast("Name suggestions queued"); render(); } catch (e) { onErr(e); } }
async function suggestBackfill() { try { const r = await api("/api/suggest/backfill", { method: "POST" }); toast(`Queued name suggestions for ${r.queued} recordings`); } catch (e) { onErr(e); } }
async function renderSuggestStatus() {
  const el = $("#suggest-status"); if (!el) return;
  try {
    const st = await api("/api/suggest/status");
    const ocr = st.ocr ? "caption OCR ready" : "caption OCR not installed (run update.bat)";
    let llm;
    if (st.provider === "ollama") llm = st.reachable ? (st.model_present ? `Ollama reachable, model ready` : `Ollama reachable but the model is not pulled: run <code>ollama pull ${esc(SETTINGS.ollama_model)}</code> (have: ${(st.models || []).map(esc).join(", ") || "none"})`) : `Ollama not reachable at ${esc(SETTINGS.ollama_url)}: install it from ollama.com, then <code>ollama pull ${esc(SETTINGS.ollama_model)}</code>`;
    else if (st.provider === "anthropic") llm = st.key_set ? `Claude (${esc(st.model)}) with your key` : "Claude selected but no API key set";
    else llm = "no language model: suggestions come from captions, metadata and transcript hints only";
    el.innerHTML = `${ocr} · ${llm} <button class="small" onclick="suggestBackfill()">Suggest names for all recordings</button>`;
  } catch (e) { el.textContent = e.message; }
}
/* ---------------- transcript ---------------- */
let TR = { sid: null, lines: [], follow: true, filter: null, q: "" };
function transcriptBox(src) {
  if (src.status !== "done" || src.incognito) return "";
  const st = src.transcript_status, job = src.transcript_job;
  let body;
  if (st === "done" && src.transcript_lines) body = `<div class="tr-tools"><input id="tr-q" placeholder="search this transcript…" oninput="trFilter(${src.id}, this.value)"><label class="chk" style="margin:0"><input type="checkbox" id="tr-follow" ${TR.follow ? "checked" : ""} onchange="TR.follow=this.checked"> follow playback</label><span class="muted" id="tr-count"></span><button class="small" onclick="transcribeNow(${src.id}, true)" title="run Whisper again">Redo</button></div><div class="tr-box" id="tr-${src.id}">${skeleton(6)}</div>`;
  else if (st === "queued" || st === "running" || (job && job.status === "running")) body = `<div class="muted">Transcribing… ${job && job.status === "running" ? `${esc(job.stage || "")} ${Math.round(job.progress || 0)}%` : "queued"}</div>`;
  else if (st === "failed") body = `<div class="warnbox">Transcription failed: ${esc(src.transcript_info?.error || "")}</div><button class="small" onclick="transcribeNow(${src.id})">Try again</button>`;
  else body = `<div class="actions"><button class="small primary" onclick="transcribeNow(${src.id})">Transcribe</button><span class="muted">Whisper on this machine (fast on a GPU). Words are attributed to each voice; names spoken around a turn become naming hints.</span></div>`;
  const lang = src.transcript_language ? ` <span class="pill">${esc(src.transcript_language)}</span>` : "";
  return `<details class="transcript" open><summary><b>Transcript</b>${lang} ${src.transcript_lines ? `<span class="muted">· ${src.transcript_lines} lines</span>` : ""}</summary>${body}</details>`;
}
async function loadTranscript(src) {
  if (src.transcript_status !== "done" || !$(`#tr-${src.id}`)) return;
  try {
    if (TR.sid !== src.id) { const j = await api(`/api/sources/${src.id}/transcript`); TR = { sid: src.id, lines: j.lines, follow: TR.follow, filter: null, q: "" }; }
    renderTranscript(src);
  } catch (e) { onErr(e); }
}
function trName(src, sid) { const i = (src.speakers || []).findIndex((x) => x.id === sid); const sp = src.speakers[i]; return { name: sp ? (sp.person?.name || sp.label) : "—", color: i >= 0 ? COLORS[i % COLORS.length] : "#555" }; }
function renderTranscript(src) {
  const box = $(`#tr-${src.id}`); if (!box) return;
  const q = TR.q.toLowerCase();
  const lines = TR.lines.filter((l) => (TR.filter == null || l.speaker_id === TR.filter) && (!q || l.text.toLowerCase().includes(q)));
  const cnt = $("#tr-count"); if (cnt) cnt.textContent = (TR.filter != null || q) ? `${lines.length} of ${TR.lines.length} lines · ` : "";
  if (TR.filter != null && cnt) cnt.innerHTML += `<a class="link" onclick="trFilterSpeaker(${src.id}, null)">show everyone</a>`;
  const mark = (t) => q ? esc(t).replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "ig"), (m) => `<mark>${m}</mark>`) : esc(t);
  let last = null;
  box.innerHTML = lines.slice(0, 3000).map((l) => { const w = trName(src, l.speaker_id); const head = l.speaker_id !== last; last = l.speaker_id;
    return `<div class="tr-line${head ? " head" : ""}" data-t="${l.start_s}" onclick="seek(${src.id}, ${l.start_s})"><span class="tr-t">${fmtT(l.start_s)}</span><span class="tr-who" style="color:${w.color}" onclick="event.stopPropagation();openSpeaker(${l.speaker_id})">${head ? esc(w.name) : ""}</span><span class="tr-x">${mark(l.text)}</span></div>`; }).join("") || `<div class="muted" style="padding:10px">No lines match.</div>`;
}
function trFilter(sid, v) { TR.q = v; if (STATE.src?.id === sid) renderTranscript(STATE.src); }
function trFilterSpeaker(sid, spk) { TR.filter = spk; const q = $("#tr-q"); if (STATE.src?.id === sid) { renderTranscript(STATE.src); $(`#tr-${sid}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }); } }
/** Highlight the line being played; called by the player tick (YouTube) and timeupdate (html media). */
function syncTranscript(sid, t) {
  if (TR.sid !== sid) return; const box = $(`#tr-${sid}`); if (!box) return;
  const nodes = box.children; let lo = 0, hi = nodes.length - 1, cur = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (Number(nodes[mid].dataset.t) <= t) { cur = mid; lo = mid + 1; } else hi = mid - 1; }
  const prev = box.querySelector(".now"); const el = cur >= 0 ? nodes[cur] : null;
  if (prev === el) return; prev?.classList.remove("now");
  if (el) { el.classList.add("now"); if (TR.follow) el.scrollIntoView({ block: "center", behavior: "smooth" }); }
}
async function transcribeNow(sid, force = false) { try { await api(`/api/sources/${sid}/transcribe${force ? "?force=true" : ""}`, { method: "POST" }); toast(force ? "Re-transcribing" : "Transcription queued"); render(); } catch (e) { onErr(e); } }
async function transcribeBackfill() { try { const r = await api("/api/transcribe/backfill", { method: "POST" }); toast(r.queued ? `Queued transcripts for ${r.queued} recordings` : "Everything is transcribed"); } catch (e) { onErr(e); } }
function timeline(src) {
  if (!src.duration_s || !(src.speakers || []).length) return "";
  const d = src.duration_s;
  const segs = src.speakers.map((sp, i) => (sp.segments || []).map((g) => `<div class="seg" style="left:${(g.start_s / d) * 100}%;width:${Math.max(0.15, ((g.end_s - g.start_s) / d) * 100)}%;background:${COLORS[i % COLORS.length]}" title="${esc(sp.person?.name || sp.label)} ${fmtT(g.start_s)}–${fmtT(g.end_s)}" onclick="event.stopPropagation();seek(${src.id}, ${g.start_s});openSpeaker(${sp.id})"></div>`).join("")).join("");
  const legend = src.speakers.map((sp, i) => `<span class="link${sp.ignored ? " ignored" : ""}" onclick="openSpeaker(${sp.id}, true)" title="${sp.ignored ? "ignored (not a voice)" : "play this voice and open its panel"}"><i style="background:${COLORS[i % COLORS.length]}"></i>${esc(sp.person?.name || sp.label)}${sp.ignored ? " ·  ignored" : ""}</span>`).join("");
  return `<div class="timeline" id="tl-${src.id}" onclick="seekFrac(${src.id}, event)">${segs}<div class="cursor" style="left:0"></div></div><div class="tl-legend">${legend}</div>`;
}
/** Names spoken around this voice's turns ("the chair recognizes Senator Warren"): one click fills the name box. */
function hintChips(sp, withWhy = false) {
  const h = sp.name_hints || []; if (!h.length) return "";
  const why = (x) => (x.evidence || []).join(" · ") || "introduced or addressed by name in the transcript";
  return `<div class="hints"><span class="muted">${h.some((x) => x.evidence) ? "Suggested:" : "Heard in the room:"}</span> ${h.map((x) => `<button class="small" onclick="useHint(this)" data-name="${esc(x.name)}" title="${esc(why(x))} (score ${x.score})">${esc(x.name)}</button>`).join("")}</div>
    ${withWhy ? `<div class="why">${h.slice(0, 2).map((x) => `<div><b>${esc(x.name)}</b> · ${esc(why(x))}</div>`).join("")}</div>` : ""}`;
}
/** Put a name hint into the nearest name box (side panel or speaker card). */
function useHint(btn) {
  const scope = btn.closest(".namebox") || btn.closest("#drawer-body") || btn.closest(".spk") || document;
  const input = scope.querySelector("input[id^=nm-]"); if (!input) return;
  input.value = btn.dataset.name; input.focus();
}
function saidBox(src, sp) {
  if (TR.sid !== src?.id) return "";
  const mine = TR.lines.filter((l) => l.speaker_id === sp.id); if (!mine.length) return "";
  const pick = mine.slice().sort((a, b) => (b.end_s - b.start_s) - (a.end_s - a.start_s)).slice(0, 6).sort((a, b) => a.start_s - b.start_s);
  return `<h3>What they said <span class="muted" style="text-transform:none;letter-spacing:0">· ${mine.length} lines · <a class="link" onclick="trFilterSpeaker(${src.id}, ${sp.id})">show all in transcript</a></span></h3>
    <div class="said">${pick.map((l) => `<div class="tr-line" onclick="seek(${src.id}, ${l.start_s})"><span class="tr-t">${fmtT(l.start_s)}</span><span class="tr-x">${esc(l.text.length > 220 ? l.text.slice(0, 220) + "…" : l.text)}</span></div>`).join("")}</div>`;
}
/* ---------------- side panel for one voice ---------------- */
function openSpeaker(id, play = false) {
  const src = STATE.src; const sp = (src?.speakers || []).find((x) => x.id === id); if (!sp) return;
  if (play && sp.longest) seek(src.id, sp.longest.start_s);
  $("#drawer-title").innerHTML = `${esc(sp.label)} <span class="muted">· ${fmtT(sp.speech_seconds)} of speech</span>`;
  const top = sp.candidates[0]; const thr = Number(SETTINGS.match_threshold || 0.75);
  const who = sp.person ? `<a class="link" onclick="openPerson(${sp.person.id})"><b>${esc(sp.person.name)}</b></a> <span class="pill ${sp.person.kind}">${sp.person.kind === "named" ? "known" : "unnamed"}</span>` : top && top.probability >= thr && top.person_kind === "named" ? `probably <a class="link" onclick="openPerson(${top.person_id})"><b>${esc(top.person_name)}</b></a>` : `<b>Unknown</b>`;
  const unnamed = !sp.person || sp.person.kind === "provisional";
  if (sp.ignored) {
    $("#drawer-body").innerHTML = `<div class="warnbox">Ignored: treated as noise, not a voice. It is never matched and never becomes a print.</div>
      <div class="actions">${sp.longest ? `<button class="small" onclick="seek(${src.id}, ${sp.longest.start_s})">▶ play longest turn</button>` : ""}<button class="small primary" onclick="unignoreSpeaker(${sp.id})">Undo · it is a voice</button></div>${sp.clip_url ? clip(sp.clip_url) : ""}`;
    STATE.drawerSpeaker = id; $("#drawer").classList.remove("hidden"); document.body.classList.add("drawer-open"); return;
  }
  $("#drawer-body").innerHTML = `
    <div style="margin-bottom:8px">${who}${sp.review_status === "pending" ? ' <span class="pill provisional">to review</span>' : ""}</div>
    <div class="namebox">
      <div class="nb-label">${unnamed ? "Who is this?" : "Rename or reassign"}</div>
      <div class="inline" style="margin:0"><input placeholder="${unnamed ? "Type a name and press Enter" : sp.person.name}" list="person-names" id="nm-${sp.id}" onkeydown="if(event.key==='Enter')nameSpeaker(${sp.id}, this)"><button class="primary" onclick="nameSpeaker(${sp.id}, this)">${unnamed ? "Name" : "Save"}</button></div>
      ${hintChips(sp, true)}
      <div class="muted" style="font-size:12px;margin-top:6px">${sp.person && sp.person.kind === "provisional" ? `Names <b>${esc(sp.person.name)}</b> everywhere it was heard. ` : ""}An existing name merges them with that person.</div>
    </div>
    ${saidBox(src, sp)}
    <div class="actions">${sp.longest ? `<button class="small" onclick="seek(${src.id}, ${sp.longest.start_s})">▶ play longest turn (${fmtT(sp.longest.start_s)}, ${sp.longest.seconds}s)</button>` : ""}</div>
    <div class="chips" style="margin:6px 0">${(sp.segments || []).slice(0, 14).map((g) => `<button onclick="seek(${src.id}, ${g.start_s})">${fmtT(g.start_s)}</button>`).join("")}${sp.segments.length > 14 ? `<span class="muted">+${sp.segments.length - 14}</span>` : ""}</div>
    ${(sp.stills || []).length ? `<div class="strip">${sp.stills.map((x) => `<img class="thumb" loading="lazy" src="${esc(x.url)}" title="jump to ${fmtT(x.t)}" onclick="seek(${src.id}, ${x.t})">`).join("")}</div>` : ""}
    ${sp.clip_url ? `<div class="turn">this voice, best ${SETTINGS.clip_seconds || 8} s</div>${clip(sp.clip_url)}` : ""}
    <div class="actions">${sp.person ? `<button class="small" onclick="openPerson(${sp.person.id})">Open profile · other clips</button><button class="small" onclick="unassign(${sp.id})">Not them / undo</button>` : ""}
      ${sp.review_status === "pending" ? `<button class="small" onclick="setReview(${sp.id}, 'skipped')">Skip for now</button><button class="small" onclick="setReview(${sp.id}, 'done')">Mark done</button>` : `<button class="small" onclick="setReview(${sp.id}, 'pending')">Add to review queue</button>`}
      <button class="small" onclick="ignoreSpeaker(${sp.id})" title="noise, laughter, crosstalk: drop it from matching for good">Not a voice</button></div>
    <textarea class="notes" placeholder="Notes about this voice (context, guesses, links…)" onblur="saveSpeakerNotes(${sp.id}, this.value)">${esc(sp.notes || "")}</textarea>
    <h3>Candidates <span class="muted" style="text-transform:none;letter-spacing:0">· listen to both before deciding</span></h3><div id="cands-${sp.id}">${candidateTable(sp)}</div>`;
  const fresh = STATE.drawerSpeaker !== id || $("#drawer").classList.contains("hidden");
  STATE.drawerSpeaker = id;
  $("#drawer").classList.remove("hidden"); document.body.classList.add("drawer-open");
  if (fresh) setTimeout(() => $(`#drawer #nm-${sp.id}`)?.focus(), 250);
}
function closeDrawer() { STATE.drawerSpeaker = null; $("#drawer")?.classList.add("hidden"); document.body.classList.remove("drawer-open"); }
async function saveSourceNotes(id, v) { try { await api(`/api/sources/${id}`, { method: "PATCH", body: { notes: v } }); toast("Notes saved"); } catch (e) { onErr(e); } }
async function saveSpeakerNotes(id, v) { try { await api(`/api/speakers/${id}`, { method: "PATCH", body: { notes: v } }); toast("Notes saved"); } catch (e) { onErr(e); } }
async function setReview(id, status) { try { await api(`/api/speakers/${id}`, { method: "PATCH", body: { review_status: status } }); toast(status === "pending" ? "Added to review queue" : status === "done" ? "Done" : "Skipped"); render(); loadStats(); } catch (e) { onErr(e); } }
async function queueReview(id) { try { const r = await api(`/api/sources/${id}/review`, { method: "POST" }); toast(r.queued ? `${r.queued} voice(s) added to the review queue` : "Nothing left to review here"); render(); loadStats(); } catch (e) { onErr(e); } }
function focusSpeaker(id) { const el = $(`#spk-${id}`); if (!el) return; el.scrollIntoView({ block: "start", behavior: "smooth" }); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
function showUnmute(sid, t) {
  const wrap = $(`#yt-${sid}`)?.parentElement; if (!wrap) return; wrap.classList.add("ytwrap");
  if (!$(".unmute", wrap)) { const b = document.createElement("button"); b.className = "unmute"; b.textContent = `🔊 Playing from ${fmtT(t)} muted (browser rule) · click for sound`; b.onclick = () => { YTP.unMute(); YTP.playVideo(); b.remove(); }; wrap.appendChild(b); }
}
function seek(sid, t) {
  if (YTP && YTP.seekTo) { YTP.seekTo(t, true); YTP.unMute(); YTP.playVideo(); $(".unmute")?.remove(); $(`#yt-${sid}`)?.scrollIntoView({ block: "nearest", behavior: "smooth" }); return; }
  const a = $(`#aud-${sid}`); if (!a) return;
  const doSeek = () => { a.currentTime = Math.min(t, a.duration || t); a.play().catch(() => {}); };
  if (a.readyState >= 1) doSeek(); else { a.addEventListener("loadedmetadata", doSeek, { once: true }); a.load(); }
  a.scrollIntoView({ block: "nearest", behavior: "smooth" });
}
function seekFrac(sid, ev) { const tl = $(`#tl-${sid}`); if (!tl) return; const r = tl.getBoundingClientRect(); const dur = YTP && YTP.getDuration ? YTP.getDuration() : $(`#aud-${sid}`)?.duration; if (!dur) return; seek(sid, ((ev.clientX - r.left) / r.width) * dur); }
document.addEventListener("timeupdate", (e) => { const a = e.target; if (!(a instanceof HTMLMediaElement) || !a.id.startsWith("aud-")) return; const sid = Number(a.id.slice(4)); const c = $(`#tl-${sid} .cursor`); if (c && a.duration) c.style.left = (a.currentTime / a.duration) * 100 + "%"; syncTranscript(sid, a.currentTime); }, true);

function qualityWarning(sp) {
  const w = [];
  if ((sp.speech_seconds || 0) < 10) w.push("under 10 s of speech");
  if ((sp.snr_db ?? 99) < 10) w.push("low SNR");
  if ((sp.clip_fraction || 0) > 0.01) w.push("clipping");
  return w.length ? `<div class="warnbox">Low-confidence clip (${w.join(", ")}). If this is noise, laughter or crosstalk rather than a voice, <a class="link" onclick="ignoreSpeaker(${sp.id})">ignore it</a> so it never matches anything.</div>` : "";
}
async function ignoreSpeaker(id) { try { await api(`/api/speakers/${id}/ignore`, { method: "POST" }); toast("Ignored: it will not be matched or used as a print"); render(); loadStats(); } catch (e) { onErr(e); } }
async function unignoreSpeaker(id) { try { await api(`/api/speakers/${id}/unignore`, { method: "POST" }); toast("Back in play; re-matching"); render(); loadStats(); } catch (e) { onErr(e); } }
function candidateTable(sp, showAll = false) {
  const thr = Number(SETTINGS.match_threshold || 0.75), hi = Number(SETTINGS.confirm_threshold || 0.9), minp = Number(SETTINGS.candidate_min_prob ?? 0.5);
  if (!sp.candidates.length) return `<div class="muted">No candidates yet (nobody in the library to compare with).</div>`;
  const strong = sp.candidates.filter((c) => c.probability >= minp || c.status === "confirmed");
  const list = showAll ? sp.candidates : strong;
  const weakNote = !showAll && strong.length < sp.candidates.length ? `<div class="muted" style="font-size:12px;margin:4px 0">${strong.length ? "" : `No candidate above ${pct(minp)}: this is probably someone new. `}<a class="link" onclick="toggleWeak(${sp.id})">show ${sp.candidates.length - strong.length} weaker candidate(s)</a></div>` : "";
  if (!list.length) return weakNote;
  return `<table class="cands"><tr><th>#</th><th>Candidate</th><th>Probability</th><th>Heard in</th><th></th></tr>` +
    list.map((c) => {
      const act = c.status === "confirmed" ? `<span class="pill named">confirmed</span> <button class="small" onclick="unassign(${sp.id})">undo</button>`
        : c.status === "rejected" ? `<span class="pill">rejected</span> <button class="small" onclick="reopenMatch(${c.id})">Undo</button>`
        : `<button class="small primary" onclick="confirmMatch(${c.id}, ${c.probability})">Confirm</button> <button class="small" onclick="rejectMatch(${c.id})">Reject</button>`;
      // the candidate's own voice, so the two can be compared by ear (longest-speech recording that has a clip)
      const ref = (c.heard_in || []).filter((h) => h.clip_url).sort((a, b) => (b.speech_seconds || 0) - (a.speech_seconds || 0))[0];
      const listen = ref ? `<div class="cmp"><span class="muted">their voice in ${esc(ref.name)}</span>${clip(ref.clip_url)}</div>` : "";
      return `<tr><td>${c.rank}</td><td><a class="link" onclick="openPerson(${c.person_id})">${esc(c.person_name)}</a><span class="pill ${c.person_kind}">${c.person_kind === "named" ? "known" : "unnamed"}</span>${sp.person_id === c.person_id ? '<span class="pill named">assigned</span>' : ""}</td>
        <td><span class="prob ${probCls(c.probability, thr, hi)}">${pct(c.probability)}</span>${c.rank === 1 ? `<span class="margin">+${pct(c.margin)}</span>` : ""}<div class="pbar"><i style="width:${pct(c.probability)}"></i></div></td>
        <td class="sub heard">${heardIn(c.heard_in)}${listen}</td><td class="act">${act}</td></tr>`;
    }).join("") + `</table>` + weakNote;
}
function toggleWeak(id) { const holder = $(`#cands-${id}`); const sp = (STATE.src?.speakers || []).concat(STATE.reviewItems || []).find((x) => x.id === id); if (!holder || !sp) return; holder.dataset.all = holder.dataset.all ? "" : "1"; holder.innerHTML = candidateTable(sp, !!holder.dataset.all); }
function speakerCard(sp, src, idx = 0) {
  const color = COLORS[idx % COLORS.length];
  if (sp.ignored) return `<div class="spk ignored" id="spk-${sp.id}" style="border-left:3px solid ${color}">
    <div class="hdr"><div><span class="title" onclick="openSpeaker(${sp.id})">${esc(sp.label)}</span> · <span class="muted">ignored (not a voice)</span></div>
      <div class="q">speech <b>${fmtT(sp.speech_seconds)}</b> · ${sp.n_segments} turns · <button class="small" onclick="unignoreSpeaker(${sp.id})">Undo</button></div></div></div>`;
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
  return `<div class="spk" id="spk-${sp.id}" style="border-left:3px solid ${color}">
    <div class="hdr"><div><span class="title" onclick="openSpeaker(${sp.id})" title="open in the side panel">${esc(sp.label)}</span> · ${verdict}${sp.review_status === "pending" ? ' <span class="pill provisional">to review</span>' : ""}</div>
      <div class="q">speech <b>${fmtT(sp.speech_seconds)}</b> · quality <b>${sp.quality_score?.toFixed(0) ?? "-"}</b> · SNR <b>${sp.snr_db?.toFixed(0) ?? "-"} dB</b> · ${sp.n_segments} turns</div></div>
    <div class="actions" style="margin:6px 0">${lg}<div class="chips">${chips}${sp.segments.length > 10 ? `<span class="muted">+${sp.segments.length - 10}</span>` : ""}</div></div>
    ${(sp.stills || []).length ? `<div class="strip">${sp.stills.map((x) => `<img class="thumb" loading="lazy" src="${esc(x.url)}" title="jump to ${fmtT(x.t)}" onclick="seek(${src.id}, ${x.t})">`).join("")}</div>` : ""}
    ${sp.clip_url ? `<div class="turn">preview clip (best ${SETTINGS.clip_seconds || 8} s of this voice)</div>${clip(sp.clip_url)}` : ""}
    ${qualityWarning(sp)}
    <div id="cands-${sp.id}">${candidateTable(sp)}</div>
    <div class="actions">
      <input placeholder="${sp.person ? "Rename / reassign…" : "Who is this? Type a name…"}" list="person-names" id="nm-${sp.id}" style="max-width:260px" onkeydown="if(event.key==='Enter')nameSpeaker(${sp.id}, this)">
      <button class="small ${sp.person ? "" : "primary"}" onclick="nameSpeaker(${sp.id}, this)">${sp.person ? "Name" : "Add to library"}</button>${hintChips(sp)}
      ${sp.person_id ? `<button class="small" onclick="unassign(${sp.id})">Unassign / undo</button>` : ""}
      <button class="small" onclick="ignoreSpeaker(${sp.id})" title="noise, laughter, crosstalk: drop it from matching for good">Not a voice</button>
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
/** The input that belongs with a button (or the input itself on Enter). Ids repeat across tabs and the side
 *  panel, so looking an input up by id can land on a hidden copy; always resolve relative to the element. */
function inputNear(el) { if (!el) return null; if (el.tagName === "INPUT") return el; return el.parentElement?.querySelector("input") || el.closest(".actions, .inline, .item, .spk")?.querySelector("input"); }
async function nameSpeaker(id, el) {
  const input = inputNear(el); const name = (input?.value || "").trim(); if (!name) { input?.focus(); return toast("Type a name first", true); }
  try {
    const sp = await api(`/api/speakers/${id}/name`, { method: "POST", body: { name } });
    toast(`Saved as ${sp.person?.name || name}`); render(); loadStats();
  } catch (e) { onErr(e); }
}
async function reopenMatch(id) { try { await api(`/api/matches/${id}/reopen`, { method: "POST" }); toast("Rejection undone"); render(); } catch (e) { onErr(e); } }
async function forgetSource(id) { if (!confirm("Remove all prints and unnamed voices created from this recording? Results stay visible.")) return; try { await api(`/api/sources/${id}/forget`, { method: "POST" }); toast("Forgotten"); render(); loadStats(); } catch (e) { onErr(e); } }
async function makeStills(id) { try { await api(`/api/sources/${id}/stills`, { method: "POST" }); toast("Stills queued"); } catch (e) { onErr(e); } }
async function stillsBackfill() { try { const r = await api("/api/stills/backfill", { method: "POST" }); toast(r.queued ? `Queued stills for ${r.queued} recordings (≈${r.estimated_mb} MB)` : "Every video recording already has stills"); } catch (e) { onErr(e); } }
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
$$("#people-seg button").forEach((b) => b.addEventListener("click", () => { STATE.peopleView = b.dataset.view; history.replaceState(null, "", `#people?view=${b.dataset.view}`); loadPeople(); }));
$("#person-search").addEventListener("input", () => { clearTimeout(STATE._ps); STATE._ps = setTimeout(loadPeople, 200); });
$("#btn-new-person").onclick = () => { $("#identify-box").open = true; $("#identify-name").focus(); };
async function loadNames() {
  const j = await api("/api/persons?kind=named").catch(() => ({ persons: [] }));
  $("#person-names").innerHTML = j.persons.map((p) => `<option value="${esc(p.name)}">`).join("");
  return j.persons;
}
async function createGroup() { const name = $("#newpkg").value.trim(); if (!name) return; try { const g = await api("/api/groups", { method: "POST", body: { name } }); go(`people/pkg${g.id}`); } catch (e) { onErr(e); } }
async function patchGroup(gid, body) { try { await api(`/api/groups/${gid}`, { method: "PATCH", body }); toast("Package updated"); } catch (e) { onErr(e); render(); } }
async function importPackage(ev) {
  const f = ev.target.files[0]; if (!f) return; const fd = new FormData(); fd.append("file", f);
  try { const r = await fetch("/api/import/gallery", { method: "POST", body: fd }); const j = await r.json(); if (!r.ok) throw new Error(j.detail); toast(`Imported ${j.persons_created} new, ${j.persons_merged} merged${j.package ? " into package " + j.package : ""}`); render(); loadStats(); } catch (e) { onErr(e); }
}
async function loadGroupDetail(gid) {
  try {
    const g = await api(`/api/groups/${gid}`); const named = await loadNames();
    const el = $("#people-detail");
    el.innerHTML = `<button class="back" onclick="back('people')">← Back</button>
      <div class="card">
        <div class="profile"><div><h2>📦 ${esc(g.name)} <span class="muted" style="font-size:14px">${g.n} voices</span></h2><div class="meta">${g.description ? esc(g.description) + " · " : ""}<a class="link" href="/api/groups/${g.id}/export">download package file</a> · <a class="link" onclick="renameGroup(${g.id})">rename</a> · <a class="link" onclick="deleteGroup(${g.id})">delete package</a></div></div></div>
        <h3>Alert for everyone in this package</h3>
        <label class="chk"><input type="checkbox" ${g.alert ? "checked" : ""} onchange="patchGroup(${g.id}, {alert: this.checked})"> Alert when any of these voices is heard (Live + phone)</label>
        <div class="inline" style="flex-wrap:wrap;align-items:flex-start">
          <select id="gs-${g.id}" style="flex:0 0 220px">${soundOptions(g.alert_sound, `default (${esc(SETTINGS.alert_default_sound || "siren")})`)}</select>
          <textarea id="gm-${g.id}" rows="2" style="flex:1 1 320px" placeholder="${esc(SETTINGS.alert_default_message || "")}">${esc(g.alert_message || "")}</textarea>
          <button class="small primary" onclick="patchGroup(${g.id}, {alert_sound: $('#gs-${g.id}').value, alert_message: $('#gm-${g.id}').value})">Save</button>
          <button class="small" onclick="runAlert('Nancy Pelosi', $('#gs-${g.id}').value || null, $('#gm-${g.id}').value || null)">▶ Test</button></div>
        <div class="sub">A person's own sound or line (set on their profile) overrides the package's. <code>[NAME]</code> is replaced per person.</div>
        <h3>Members</h3>
        <div class="inline" style="max-width:560px"><input id="addm-${g.id}" placeholder="Add a known person by name…" list="person-names"><button class="small" onclick="addMemberByName(${g.id}, this)">Add</button><button class="small" onclick="addAllKnown(${g.id})" title="Add every known person">Add all known (${named.length})</button></div>
        ${g.members.map((m) => `<div class="appear"><div>${glyphSvg(m.glyph, "var(--s1)", 24)} <a class="link" onclick="openPerson(${m.id})"><b>${esc(m.name)}</b></a> <span class="muted">· ${m.n_sources} rec · ${fmtT(m.speech_seconds)}${m.alert_sound || m.alert_message ? " · has own alert settings" : ""}</span></div>
          <div class="actions"><button class="small" onclick="removeFromGroup(${g.id}, ${m.id})">Remove</button></div></div>`).join("") || '<p class="muted">No members yet.</p>'}
      </div>`;
  } catch (e) { onErr(e); }
}
async function addMemberByName(gid, el) {
  const name = (inputNear(el)?.value || "").trim(); if (!name) return;
  try { const named = await loadNames(); const p = named.find((x) => x.name.toLowerCase() === name.toLowerCase()); if (!p) throw new Error("no known person with that name");
    await api(`/api/groups/${gid}/members`, { method: "POST", body: { person_ids: [p.id] } }); render(); } catch (e) { onErr(e); }
}
async function addAllKnown(gid) { if (!confirm("Add every known person to this package?")) return; try { const named = await loadNames(); await api(`/api/groups/${gid}/members`, { method: "POST", body: { person_ids: named.map((p) => p.id) } }); render(); } catch (e) { onErr(e); } }
async function renameGroup(gid) { const name = prompt("New package name?"); if (!name) return; try { await api(`/api/groups/${gid}`, { method: "PATCH", body: { name } }); render(); } catch (e) { onErr(e); } }
async function deleteGroup(gid) { if (!confirm("Delete this package? The people stay; only the grouping and its alert setting go.")) return; try { await api(`/api/groups/${gid}`, { method: "DELETE" }); go("people"); } catch (e) { onErr(e); } }
async function loadPeople() {
  if (STATE.personId && STATE.personKind === "pkg") { $("#people-list").classList.add("hidden"); $("#people-detail").classList.remove("hidden"); return loadGroupDetail(STATE.personId); }
  if (STATE.personId) { $("#people-list").classList.add("hidden"); $("#people-detail").classList.remove("hidden"); return loadPersonDetail(STATE.personId); }
  $("#people-detail").classList.add("hidden"); $("#people-list").classList.remove("hidden");
  $$("#people-seg button").forEach((b) => b.classList.toggle("active", b.dataset.view === STATE.peopleView));
  api("/api/stats").then((st) => { $$("#people-seg button")[0].textContent = `To review (${st.review_pending})`; }).catch(() => {});
  const q = $("#person-search").value.trim().toLowerCase(); const el = $("#people-content");
  if (!el.innerHTML) el.innerHTML = skeleton(5);
  try {
    if (STATE.peopleView === "review") {
      const j = await api("/api/review?limit=40"); STATE.reviewItems = j.items; await loadNames();
      $$("#people-seg button")[0].textContent = `To review (${j.total})`;
      el.innerHTML = j.items.length ? `<p class="muted">Voices you asked to label. Watch, type a name (Enter saves), or skip. Naming or confirming removes the item.</p>` + j.items.map((sp) => {
        const h = { id: sp.source.id, name: sp.source.name, youtube_id: sp.source.youtube_id, media_kind: sp.source.media_kind, watch_at: sp.longest?.start_s ?? sp.first_start_s, first_start_s: sp.first_start_s, still_url: sp.stills?.[0]?.url, speech_seconds: sp.speech_seconds };
        return `<div class="review-item">${thumb(h) || '<div class="thumb"></div>'}<div>
          <div><a class="link" onclick="openSourceAt(${sp.source.id}, ${h.watch_at || 0})"><b>${esc(sp.source.name)}</b></a> <span class="muted">· ${esc(sp.label)} · ${fmtT(sp.speech_seconds)} of speech${sp.source.notes ? " · " + esc(sp.source.notes.slice(0, 80)) : ""}</span></div>
          <div class="actions" style="margin:4px 0">${watchBtn(h)}${sp.clip_url ? clip(sp.clip_url) : ""}</div>
          <div id="cands-${sp.id}">${candidateTable(sp)}</div>
          <div class="inline" style="margin:6px 0"><input placeholder="Who is this? Type a name…" list="person-names" id="nm-${sp.id}" onkeydown="if(event.key==='Enter')nameSpeaker(${sp.id}, this)"><button class="small primary" onclick="nameSpeaker(${sp.id}, this)">Add</button><button class="small" onclick="setReview(${sp.id}, 'skipped')">Skip</button><button class="small" onclick="ignoreSpeaker(${sp.id})" title="noise, laughter, crosstalk">Not a voice</button></div>
          <textarea class="notes" placeholder="Notes about this voice" onblur="saveSpeakerNotes(${sp.id}, this.value)">${esc(sp.notes || "")}</textarea></div></div>`; }).join("") : empty("Nothing to review. Upload a recording with the Review intent, or open a recording and press “Add voices to review queue”.");
    } else if (STATE.peopleView === "known") {
      const named = await loadNames();
      $$("#people-seg button")[1].textContent = `Known (${named.length})`;
      el.innerHTML = named.filter((p) => p.name.toLowerCase().includes(q)).map((p) => `<div class="item" style="display:flex;gap:10px;align-items:center">${glyphSvg(p.glyph)}<div style="flex:1"><a class="link" onclick="openPerson(${p.id})"><b>${esc(p.name)}</b></a>${bell(p)}
        <div class="muted" style="font-size:12px">${p.n_sources} recordings · ${fmtT(p.speech_seconds)} of speech · ${p.n_prints} prints</div></div><button class="small" onclick="openPerson(${p.id})">Open</button></div>`).join("") || empty("No known people yet. Name an unnamed voice, or identify someone from reference audio.");
    } else if (STATE.peopleView === "unnamed") {
      const [u, named] = await Promise.all([api("/api/voices/unnamed"), loadNames()]);
      $$("#people-seg button")[2].textContent = `Unnamed voices (${u.voices.length})`;
      const mergeOpts = (self) => `<option value="">Merge into…</option>` + u.voices.filter((v) => v.id !== self).map((v) => `<option value="${v.id}">${esc(v.name)}</option>`).join("") + named.map((p) => `<option value="${p.id}">${esc(p.name)} (known)</option>`).join("");
      el.innerHTML = `<p class="muted">Background data, most-heard first. Open one to watch where it was heard; <b>Name</b> it if you know who it is; <b>Keep</b> tracks it as a known voice without a name.</p><div class="grid">` + u.voices.filter((v) => v.name.toLowerCase().includes(q)).map((v) => `<div class="vcard">
        <div class="nm"><a class="link" onclick="openPerson(${v.id})">${esc(v.name)}</a>${bell(v)} <span class="muted">· ${v.n_sources} rec · ${fmtT(v.speech_seconds)}</span></div>
        ${heardInRich(v.heard_in.slice(0, 2))}
        <div class="inline"><input placeholder="Name" list="person-names" id="pn-${v.id}" onkeydown="if(event.key==='Enter')namePerson(${v.id}, this)"><button class="small primary" onclick="namePerson(${v.id}, this)">Name</button></div>
        <div class="inline"><button class="small" onclick="acceptPerson(${v.id})">Keep</button><select id="mg-${v.id}">${mergeOpts(v.id)}</select><button class="small" onclick="mergePerson(${v.id})">Merge</button><button class="small danger" onclick="deletePerson(${v.id})">Delete</button></div></div>`).join("") + `</div>` || empty("No unnamed voices yet. They appear as recordings are processed.");
    } else if (STATE.peopleView === "packages") {
      const j = await api("/api/groups"); ALLGROUPS = j.groups;
      $$("#people-seg button")[3].textContent = `Packages (${j.groups.length})`;
      el.innerHTML = `<p class="muted">A package is a set of voices with one alert setting: flag 500 politicians at once, give them all the same alarm and line, download or share them as one file.</p>
        <div class="inline" style="max-width:520px"><input id="newpkg" placeholder="New package name, e.g. Politicians"><button class="small primary" onclick="createGroup()">Create</button><label class="link" style="align-self:center;white-space:nowrap">import package<input type="file" id="import-pkg" accept="application/json" hidden></label></div>
        ${j.groups.map((g) => `<div class="item" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <div style="flex:1"><a class="link" onclick="go('people/pkg${g.id}')"><b>${esc(g.name)}</b></a> <span class="muted">· ${g.n} voices${g.description ? " · " + esc(g.description) : ""}</span>
            <div class="muted" style="font-size:12px">${g.alert ? `🔔 alerting · ${esc(g.alert_sound || SETTINGS.alert_default_sound || "siren")} · “${esc(g.spoken_example)}”` : "not alerting"}</div></div>
          <label class="chk" style="margin:0"><input type="checkbox" ${g.alert ? "checked" : ""} onchange="patchGroup(${g.id}, {alert: this.checked})"> alert</label>
          <button class="small" onclick="go('people/pkg${g.id}')">Open</button><a class="link" href="/api/groups/${g.id}/export">download</a></div>`).join("") || '<p class="muted">No packages yet.</p>'}`;
      $("#import-pkg").addEventListener("change", importPackage);
    } else {
      const j = await api("/api/voices/merge_suggestions");
      $$("#people-seg button")[4].textContent = `Possible duplicates (${j.pairs.length})`;
      el.innerHTML = j.pairs.length ? `<p class="muted">Different mics or rooms can split one person into several entries. Listen to both, then merge.</p>` + j.pairs.map((p) => `<div class="item"><div class="title"><a class="link" onclick="openPerson(${p.a.id})">${esc(p.a.name)}</a><span class="pill ${p.a.kind}">${p.a.kind === "named" ? "known" : "unnamed"}</span> ≈ <a class="link" onclick="openPerson(${p.b.id})">${esc(p.b.name)}</a><span class="pill ${p.b.kind}">${p.b.kind === "named" ? "known" : "unnamed"}</span> <span class="prob hi">${pct(p.probability)}</span></div>
        <div class="row" style="margin:4px 0 0">${[p.a, p.b].map((x) => `<div class="col">${clip(x.heard_in[0]?.clip_url) || '<span class="muted">no clip</span>'}</div>`).join("")}</div>
        <div class="actions"><button class="small primary" onclick="mergeInto(${p.a.kind === "named" ? p.b.id : p.a.id}, ${p.a.kind === "named" ? p.a.id : p.b.id})">Merge into ${esc(p.a.kind === "named" ? p.a.name : p.b.name)}</button>${p.a.kind !== "named" && p.b.kind !== "named" ? ` <button class="small" onclick="mergeInto(${p.b.id}, ${p.a.id})">Merge into ${esc(p.a.name)}</button>` : ""}</div></div>`).join("") : empty("No likely duplicates right now.");
    }
  } catch (e) { onErr(e); }
}
async function namePerson(id, el) {
  const input = inputNear(el); const name = (input?.value || "").trim(); if (!name) { input?.focus(); return toast("Type a name first", true); }
  try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); toast(`Named ${name}`); render(); loadStats(); } catch (e) { onErr(e); }
}
async function mergePerson(id) { const into = $(`#mg-${id}`).value; if (!into) return; mergeInto(id, Number(into)); }
async function mergeInto(fromId, intoId) { if (!confirm("Merge? Prints and recordings move over; the other entry is deleted.")) return; try { await api(`/api/persons/${fromId}/merge`, { method: "POST", body: { into_id: intoId } }); toast("Merged"); render(); loadStats(); } catch (e) { onErr(e); } }
async function deletePerson(id) { if (!confirm("Delete this voice and all its prints?")) return; try { await api(`/api/persons/${id}`, { method: "DELETE" }); toast("Deleted"); go("people"); loadStats(); } catch (e) { onErr(e); } }
async function acceptPerson(id) { try { const p = await api(`/api/persons/${id}/accept`, { method: "POST" }); toast(`Kept as ${p.name}`); render(); loadStats(); } catch (e) { onErr(e); } }
async function savePersonNotes(id, v) { try { await api(`/api/persons/${id}`, { method: "PATCH", body: { notes: v } }); toast("Notes saved"); } catch (e) { onErr(e); } }
async function addToGroup(pid) {
  let gid = $(`#addg-${pid}`).value; if (!gid) return;
  try {
    if (gid === "__new") { const name = prompt("Package name (e.g. Politicians)?"); if (!name) return; gid = (await api("/api/groups", { method: "POST", body: { name } })).id; }
    await api(`/api/groups/${gid}/members`, { method: "POST", body: { person_ids: [pid] } }); toast("Added"); render();
  } catch (e) { onErr(e); }
}
async function removeFromGroup(gid, pid) { try { await api(`/api/groups/${gid}/members/${pid}`, { method: "DELETE" }); render(); } catch (e) { onErr(e); } }
async function renamePerson(id) { const name = prompt("New name?"); if (!name) return; try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); render(); } catch (e) { onErr(e); } }

/* --- person profile --- */
let ALLGROUPS = [];
async function loadPersonDetail(id) {
  try {
    const [p, gj] = await Promise.all([api(`/api/persons/${id}`), api("/api/groups")]); ALLGROUPS = gj.groups;
    const el = $("#people-detail");
    el.innerHTML = `<button class="back" onclick="back('people')">← Back</button>
      <div class="card">
        <div class="profile">${glyphSvg(p.glyph, p.kind === "named" ? "var(--s1)" : "var(--s3)", 56)}
          <div><h2>${esc(p.name)} <span class="pill ${p.kind}">${p.kind === "named" ? "known" : "unnamed"}</span>${bell(p)}</h2>
          <div class="meta">${p.n_sources} recordings · ${fmtT(p.speech_seconds)} of speech · ${p.n_prints} prints${p.notes ? " · " + esc(p.notes) : ""}</div></div>
          <div class="actions" style="margin-left:auto">${p.kind === "provisional" ? `<input id="pn-${p.id}" placeholder="Name this voice" list="person-names" style="max-width:200px" onkeydown="if(event.key==='Enter')namePerson(${p.id}, this)"><button class="small primary" onclick="namePerson(${p.id}, this)">Name</button><button class="small" onclick="acceptPerson(${p.id})">Keep</button>` : `<button class="small" onclick="renamePerson(${p.id})">Rename</button>`}<button class="small danger" onclick="deletePerson(${p.id})">Delete</button></div>
        </div>
        <div class="sub" style="margin:6px 0">Packages: ${(p.groups || []).map((g) => `<span class="pill ${g.alert ? "named" : ""}" title="${g.alert ? "this package alerts" : ""}">${esc(g.name)} <a class="link" onclick="removeFromGroup(${g.id}, ${p.id})" title="remove">✕</a></span>`).join(" ") || '<span class="muted">none</span>'}
          <select id="addg-${p.id}" style="max-width:200px;display:inline-block;width:auto"><option value="">+ add to package…</option>${(ALLGROUPS || []).filter((g) => !(p.groups || []).some((x) => x.id === g.id)).map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}<option value="__new">+ new package…</option></select>
          <button class="small" onclick="addToGroup(${p.id})">Add</button></div>
        <details ${p.effective?.alert ? "open" : ""}><summary>Alert: alarm + spoken line ${p.effective?.alert ? `(on${p.effective.via && p.effective.via !== "person" ? ", via " + esc(p.effective.via.replace("package:", "package ")) : ""})` : "(off, click the bell or add to an alerting package)"}</summary>
          <div class="inline" style="flex-wrap:wrap;align-items:flex-start;margin-top:8px">
            <select id="as-${p.id}" style="flex:0 0 220px">${soundOptions(p.alert_sound, `inherit (${esc(p.effective?.sound || SETTINGS.alert_default_sound || "siren")})`)}</select>
            <textarea id="am-${p.id}" data-name="${esc(p.name)}" rows="2" style="flex:1 1 320px" placeholder="${esc(SETTINGS.alert_default_message || "")}">${esc(p.alert_message || "")}</textarea>
            <button class="small primary" onclick="saveAlertSettings(${p.id})">Save</button><button class="small" onclick="testAlert(${p.id})">▶ Test</button></div>
          <div class="sub">Will say: “${esc(p.spoken)}”. <code>[NAME]</code> = ${esc(p.name)}; empty = default from Settings.</div>
        </details>
        <textarea class="notes" placeholder="Notes about this person (role, context, links…)" onblur="savePersonNotes(${p.id}, this.value)">${esc(p.notes || "")}</textarea>
        <h3>Heard in ${p.heard_in.length} recordings</h3>
        ${p.heard_in.map((h) => `<div class="appear"><div class="hi">${thumb(h)}<div><a class="link" onclick="openSource(${h.id})"><b>${esc(h.name)}</b></a> <span class="muted">· ${fmtT(h.speech_seconds || 0)} of speech · <span class="pill">${esc(h.assignment)}</span></span>${clip(h.clip_url)}</div></div>
          <div class="actions">${watchBtn(h)}<button class="small" onclick="unassign(${h.speaker_id})" title="This recording is not them">Not them</button></div></div>`).join("") || '<p class="muted">No recordings yet (enrolled from the command line or imported).</p>'}
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
const SOUNDS = [["siren", "siren (civil-defence wail)"], ["horn", "horn (emergency two-tone)"], ["vault", "vault (blast door opening)"], ["radar", "radar (phone-style alarm)"], ["klaxon", "klaxon"], ["chime", "chime"], ["none", "no sound"]];
const soundOptions = (cur, defaultLabel) => `<option value="" ${!cur ? "selected" : ""}>${defaultLabel}</option>` + SOUNDS.map(([k, l]) => `<option value="${k}" ${cur === k ? "selected" : ""}>${l}</option>`).join("");
function noiseBuffer(ac, seconds) { const b = ac.createBuffer(1, Math.ceil(seconds * ac.sampleRate), ac.sampleRate); const d = b.getChannelData(0); for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1; return b; }
function playAlarm(kind) {
  kind = kind || SETTINGS.alert_default_sound || "siren";
  if (kind === "none") return 0;
  const ac = alarmCtx(), t0 = ac.currentTime, g = ac.createGain();
  if (kind === "horn") {              // two-tone emergency horn: 330/415 Hz, 0.7 s each, 4 pairs, resonant
    const dur = 5.8; g.connect(ac.destination); g.gain.value = 0;
    const bp = ac.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = 500; bp.Q.value = 1.2; bp.connect(g);
    for (let i = 0; i < 8; i++) { const o = ac.createOscillator(); o.type = "sawtooth"; o.frequency.value = i % 2 ? 415 : 330; const o2 = ac.createOscillator(); o2.type = "square"; o2.frequency.value = (i % 2 ? 415 : 330) / 2;
      const og = ac.createGain(); og.gain.value = 0.5; o.connect(og); o2.connect(og); og.connect(bp); o.start(t0 + i * 0.7); o2.start(t0 + i * 0.7); o.stop(t0 + i * 0.7 + 0.66); o2.stop(t0 + i * 0.7 + 0.66);
      g.gain.setValueAtTime(0.0001, t0 + i * 0.7); g.gain.exponentialRampToValueAtTime(0.9, t0 + i * 0.7 + 0.05); g.gain.setValueAtTime(0.9, t0 + i * 0.7 + 0.55); g.gain.exponentialRampToValueAtTime(0.0001, t0 + i * 0.7 + 0.66); }
    return dur;
  }
  if (kind === "vault") {             // blast door: sub thump, metallic clank, 5 s grinding sweep with tremolo, hiss release
    const dur = 7;
    const sub = ac.createOscillator(); sub.type = "sine"; sub.frequency.setValueAtTime(60, t0); sub.frequency.exponentialRampToValueAtTime(30, t0 + 1.2); const sg = ac.createGain(); sg.gain.setValueAtTime(1.0, t0); sg.gain.exponentialRampToValueAtTime(0.0001, t0 + 1.4); sub.connect(sg); sg.connect(ac.destination); sub.start(t0); sub.stop(t0 + 1.5);
    [1900, 2600, 3300].forEach((f, i) => { const n = ac.createBufferSource(); n.buffer = noiseBuffer(ac, 0.5); const f1 = ac.createBiquadFilter(); f1.type = "bandpass"; f1.frequency.value = f; f1.Q.value = 25; const ng = ac.createGain(); ng.gain.setValueAtTime(0.6 / (i + 1), t0 + 0.05); ng.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.9); n.connect(f1); f1.connect(ng); ng.connect(ac.destination); n.start(t0 + 0.05); });
    const grind = ac.createOscillator(); grind.type = "sawtooth"; grind.frequency.setValueAtTime(70, t0 + 0.8); grind.frequency.linearRampToValueAtTime(150, t0 + 5.8);
    const lfo = ac.createOscillator(); lfo.frequency.setValueAtTime(9, t0); lfo.frequency.linearRampToValueAtTime(18, t0 + 5.8); const lg = ac.createGain(); lg.gain.value = 0.3; lfo.connect(lg);
    const gg = ac.createGain(); gg.gain.setValueAtTime(0.0001, t0 + 0.8); gg.gain.exponentialRampToValueAtTime(0.5, t0 + 1.6); gg.gain.setValueAtTime(0.5, t0 + 5.2); gg.gain.exponentialRampToValueAtTime(0.0001, t0 + 6.0); lg.connect(gg.gain);
    const lp = ac.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.setValueAtTime(600, t0); lp.frequency.linearRampToValueAtTime(1800, t0 + 5.8); grind.connect(lp); lp.connect(gg); gg.connect(ac.destination); grind.start(t0 + 0.8); grind.stop(t0 + 6.1); lfo.start(t0); lfo.stop(t0 + 6.1);
    const hiss = ac.createBufferSource(); hiss.buffer = noiseBuffer(ac, 1.6); const hp = ac.createBiquadFilter(); hp.type = "highpass"; hp.frequency.value = 3000; const hg = ac.createGain(); hg.gain.setValueAtTime(0.35, t0 + 5.6); hg.gain.exponentialRampToValueAtTime(0.0001, t0 + 7.0); hiss.connect(hp); hp.connect(hg); hg.connect(ac.destination); hiss.start(t0 + 5.6);
    return dur;
  }
  if (kind === "radar") {             // phone-style alarm: 4 quick 1 kHz/1.25 kHz pips, pause, repeat ×4
    const dur = 5.2; g.connect(ac.destination); g.gain.value = 0;
    for (let r = 0; r < 4; r++) for (let i = 0; i < 4; i++) { const t = t0 + r * 1.3 + i * 0.16; const o = ac.createOscillator(); o.type = "square"; o.frequency.value = i % 2 ? 1250 : 1000; o.connect(g); o.start(t); o.stop(t + 0.09); g.gain.setValueAtTime(0.25, t); g.gain.setValueAtTime(0, t + 0.09); }
    return dur;
  }
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
      carry = inp.slice(Math.floor(n * ratio)); const lv = Math.min(1, peak * 1.4); $("#live-level").style.width = lv * 100 + "%"; const ring = $("#live-ring"); if (ring) ring.style.strokeDashoffset = String(326.7 * (1 - lv)); LIVE.sent += out.byteLength; ws.send(out.buffer);
    };
    src.connect(proc); proc.connect(ac.destination);
    LIVE = { stream, ac, proc, ws, lastAlert: 0, log: [], frames: 0, sent: 0 };
    $("#btn-live").textContent = "⏹ Stop listening"; st.textContent = `connecting… (mic ${ac.sampleRate} Hz)`;
    $("#live-now").innerHTML = `<div class="muted">starting: connecting to the server…</div>`;
    ws.onopen = () => { st.textContent = "connected · loading model…"; $("#live-now").innerHTML = `<div class="muted">connected · waiting for the first audio frame…</div>`; };
    ws.onerror = () => { st.textContent = "websocket error (is the page served by VoiceFlock itself?)"; };
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (m.type === "status") { st.textContent = m.text; return; } if (m.type === "error") { st.textContent = m.text; $("#live-now").innerHTML = `<div class="warnbox">${esc(m.text)}</div>`; stopLive(); return; } LIVE.frames++; renderLive(m); };
    ws.onclose = (e) => { if (LIVE) { st.textContent = `connection closed (${e.code}) after ${LIVE.frames} frames`; stopLive(); } };
    LIVE.watchdog = setTimeout(() => { if (LIVE && LIVE.frames === 0) { $("#live-now").innerHTML = `<div class="warnbox">No results after 10 s. Sent ${Math.round(LIVE.sent / 32000)} s of audio, socket state ${ws.readyState}. ${LIVE.sent === 0 ? "The microphone is not delivering samples: check the browser's mic permission for this site, or that another app isn't holding the mic." : "The server is not answering: check the VoiceFlock window for errors (model download on first use can take a minute)."}</div>`; } }, 10000);
  } catch (e) {
    st.textContent = "microphone blocked: " + e.message;
    $("#live-now").innerHTML = `<div class="warnbox"><b>Could not open the microphone:</b> ${esc(e.message)}.<br>${/not found/i.test(e.message) ? "No microphone is available to the browser: check Windows Settings → Privacy → Microphone (allow desktop apps and this browser), and that a mic is selected as default input." : /denied|permission/i.test(e.message) ? "Allow the microphone for this site (click the padlock/camera icon in the address bar) and press Start again." : "Close other apps using the mic (Zoom, Teams) and try again."}</div>`;
  }
};
function stopLive() { if (!LIVE) return; clearTimeout(LIVE.watchdog); try { LIVE.proc.disconnect(); LIVE.ac.close(); LIVE.stream.getTracks().forEach((t) => t.stop()); LIVE.ws.close(); } catch (e) {} LIVE = null; $("#btn-live").textContent = "▶ Start listening"; $("#live-level").style.width = "0"; const ring = $("#live-ring"); if (ring) ring.style.strokeDashoffset = "326.7"; $("#live-now").className = "livenow"; $("#live-now").innerHTML = '<div class="muted">Not listening.</div>'; }
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
    renderSelftest(); renderPhone(); renderVersion(); renderSuggestStatus();
    const st = await api("/api/stats");
    $("#stills-info").innerHTML = `Stills on disk: <b>${st.stills_mb} MB</b>. <button class="small" onclick="stillsBackfill()">Make stills for existing recordings</button> <button class="small" onclick="transcribeBackfill()">Transcribe existing recordings (${st.untranscribed} left)</button> <span class="muted">(videos and YouTube recordings processed before stills existed)</span>`;
    $("#calib-info").textContent = `scoring mode: ${st.score_mode} (cohort ${st.cohort_size}, min ${SETTINGS.cohort_min_size}) · raw a=${st.calibration.raw.a.toFixed(2)} b=${st.calibration.raw.b.toFixed(2)}`;
    renderCalibration(st);
  } catch (e) { onErr(e); }
}
async function renderCalibration(st) {
  const el = $("#calibration"); if (!el) return;
  try {
    const c = await api("/api/calibration"); const av = c.available; const fit = c.fitted[st.score_mode] || c.fitted.raw;
    const state = fit ? `<b>Fitted from your decisions</b> on ${esc((fit.created_at || "").slice(0, 16).replace("T", " "))} · ${fit.n_target} same-voice and ${fit.n_nontarget} different-voice trials · equal-error rate ${(fit.eer * 100).toFixed(1)}%`
      : `<b>Using default constants.</b> The percentages shown everywhere are a generic guess until you calibrate.`;
    el.innerHTML = `<p>${state}</p>
      <p class="muted">Available now: <b>${av.labelled_speakers}</b> confirmed/named speakers across <b>${av.labelled_persons}</b> people, <b>${av.rejections}</b> rejections, <b>${av.named_persons}</b> known people. Every decision you make in a recording or the review queue adds to this.</p>
      <div class="actions"><button class="primary" id="btn-calibrate" ${av.enough ? "" : "disabled"}>Calibrate from my decisions</button>${fit ? `<button id="btn-calib-reset">Back to defaults</button>` : ""}<span class="muted" id="calib-msg">${av.enough ? "" : "Needs at least 8 confirmed or named speakers across 2 people."}</span></div>
      <div id="calib-result"></div>`;
    $("#btn-calibrate").onclick = async () => {
      $("#calib-msg").textContent = "fitting…";
      try {
        const r = await api("/api/calibration/decisions", { method: "POST" }); const m = r.modes[r.active_mode] || r.modes.raw;
        $("#calib-result").innerHTML = `<div class="warnbox" style="color:var(--fg)">Done: equal-error rate <b>${(m.eer * 100).toFixed(1)}%</b>, miss rate at 1% false alarms <b>${(m.miss_at_1pct_fa * 100).toFixed(1)}%</b>, from ${r.n_target} same-voice and ${r.n_nontarget} different-voice trials. The library is being re-scored.
          <div class="actions">Suggested match threshold <b>${pct(r.suggested_match_threshold)}</b> (about 1% false alarms) <button class="small primary" onclick="applyThreshold(${r.suggested_match_threshold})">Use it</button> <span class="muted">current ${pct(SETTINGS.match_threshold)}</span></div></div>`;
        $("#calib-msg").textContent = ""; loadStats(); const st2 = await api("/api/stats"); renderCalibration(st2);
      } catch (e) { $("#calib-msg").textContent = ""; onErr(e); }
    };
    $("#btn-calib-reset") && ($("#btn-calib-reset").onclick = async () => { try { await api("/api/calibration/reset", { method: "POST" }); toast("Back to default calibration; re-scoring"); loadStats(); renderCalibration(await api("/api/stats")); } catch (e) { onErr(e); } });
  } catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; }
}
async function applyThreshold(v) {
  try { const j = await api("/api/settings", { method: "PUT", body: { match_threshold: String(v) } }); SETTINGS = j.values; toast(`Match threshold set to ${pct(v)}`); const f = $("#settings-form [name=match_threshold]"); if (f) f.value = v; loadStats(); } catch (e) { onErr(e); }
}
$("#btn-save-settings").onclick = async (e) => {
  e.preventDefault(); const body = {}; $$("#settings-form [name]").forEach((i) => { body[i.name] = i.type === "checkbox" ? i.checked : i.value; });
  try { const j = await api("/api/settings", { method: "PUT", body }); SETTINGS = j.values; $("#settings-msg").textContent = "Saved."; toast("Settings saved"); loadStats(); } catch (er) { onErr(er); }
};
async function renderPhone() {
  try {
    const es = await api("/api/mobile/export_status");
    $("#export-status").textContent = es.path ? `Exporting to ${es.path}${es.exported_at ? ` (last written ${es.exported_at.slice(0, 16).replace("T", " ")} UTC${es.dirty ? ", changes pending" : ""})` : " (not written yet)"}.` : "(no export folder set yet)";
    const h = await api("/api/health"); const port = location.port || (location.protocol === "https:" ? 443 : 80);
    const addrs = (h.lan_ips || []).map((ip) => `http://${ip}:${port}`);
    $("#phone-info").textContent = `Direct sync address (same Wi-Fi):\n  ${addrs.join("\n  ") || location.origin}\nPassword: ${h.password_required ? "the one in .env" : "none set"}\nIf the phone can't connect, use the library file instead (above).`;
    const r = await fetch("/api/mobile/model.onnx", { method: "HEAD" }); const ms = $("#model-status");
    ms.textContent = r.status === 200 ? `laptop-side phone model ready (${Math.round((Number(r.headers.get("content-length")) || 0) / 1048576)} MB)` : "not exported on this laptop (the phone downloads it from GitHub anyway)";
  } catch (e) {}
}
async function renderVersion() {
  const h = HEALTH || (await api("/api/health").catch(() => null)); if (!h) return;
  $("#version-info").innerHTML = `Server <b>${esc(h.version)}</b> · page ${APP_VERSION} · up since ${esc((h.started_at || "").slice(0, 16).replace("T", " "))} UTC${h.supervised ? "" : ' · <span class="muted">started by hand: restart from here is not available</span>'}`;
  $("#btn-restart").classList.toggle("hidden", !h.supervised);
}
async function restartServer() {
  try { await api("/api/admin/restart", { method: "POST" }); toast("Restarting… the page reloads when the server is back"); waitForServer(); } catch (e) { onErr(e); }
}
function waitForServer() {
  const started = HEALTH?.started_at; let tries = 0;
  const t = setInterval(async () => { tries++; try { const h = await api("/api/health"); if (h.started_at !== started) { clearInterval(t); location.reload(); } } catch (e) {} if (tries > 90) clearInterval(t); }, 2000);
}
$("#btn-check-update").onclick = async () => {
  $("#update-msg").textContent = "checking…"; $("#update-log").textContent = "";
  try {
    const u = await api("/api/admin/update");
    if (u.error) { $("#update-msg").textContent = u.error; return; }
    $("#update-msg").textContent = u.behind ? `${u.behind} update(s) available (${u.local} → ${u.remote})` : `up to date (${u.local})`;
    $("#update-log").textContent = (u.log || []).join("\n");
    $("#btn-apply-update").classList.toggle("hidden", !u.behind);
    if (u.behind && !u.supervised) $("#update-msg").textContent += " · after updating, close and reopen VoiceFlock.bat";
  } catch (e) { onErr(e); }
};
$("#btn-apply-update").onclick = async () => {
  try {
    const r = await api("/api/admin/update", { method: "POST" }); $("#update-msg").textContent = "updating…";
    const poll = setInterval(async () => {
      try { const st = await api("/api/admin/update/status"); $("#update-log").textContent = st.log.join("\n");
        if (st.error) { clearInterval(poll); $("#update-msg").textContent = "update failed: " + st.error; }
        else if (!st.running) { clearInterval(poll); $("#update-msg").textContent = r.will_restart ? "restarting…" : "updated: close and reopen VoiceFlock.bat"; if (r.will_restart) waitForServer(); } }
      catch (e) { /* server is restarting */ }
    }, 1500);
  } catch (e) { onErr(e); }
};
$("#btn-restart").onclick = restartServer;
$("#wipe-confirm").addEventListener("input", () => { $("#btn-wipe").disabled = $("#wipe-confirm").value.trim() !== "WIPE"; });
$("#btn-wipe").onclick = async () => {
  if (!confirm("Really delete every recording, voice and print? Settings and models stay.")) return;
  try { const r = await api("/api/admin/wipe", { method: "POST", body: { confirm: "WIPE", keep_settings: true } }); $("#wipe-msg").textContent = "Wiped: " + Object.entries(r.wiped).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", "); $("#wipe-confirm").value = ""; $("#btn-wipe").disabled = true; loadStats(); } catch (e) { onErr(e); }
};
$("#btn-export-now").onclick = async () => { try { const r = await api("/api/mobile/export_now", { method: "POST" }); toast("Written: " + r.written); renderPhone(); } catch (e) { onErr(e); } };
$("#btn-phone-edits").onclick = async () => { try { const body = JSON.parse($("#phone-edits").value); const r = await api("/api/import/phone_settings", { method: "POST", body }); $("#phone-edits-msg").textContent = `applied: ${r.persons} people, ${r.groups} packages`; $("#phone-edits").value = ""; } catch (e) { onErr(e); } };
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
