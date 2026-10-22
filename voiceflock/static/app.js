/* VoiceFlock UI: vanilla JS, talks to /api/*. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtT = (s) => { s = Math.max(0, Math.round(s || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60; return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(x).padStart(2, "0"); };
const pct = (p) => (p * 100).toFixed(1) + "%";
const probCls = (p, thr, hi) => p >= hi ? "hi" : p >= thr ? "mid" : "lo";
let SETTINGS = {}, SPECS = {}, STATE = { tab: "bulk", matchSource: null, sourceDetail: null, personDetail: null };
const COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]; // validated dark-surface categorical set
const elapsed = (iso) => { if (!iso) return ""; const s = Math.max(0, (Date.now() - Date.parse(iso + (iso.endsWith("Z") ? "" : "Z"))) / 1000); return fmtT(s); };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts, body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
function toast(msg, err = false) { const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), 3500); }
const onErr = (e) => toast(e.message || String(e), true);

/* ---------------- tabs ---------------- */
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  STATE.tab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + name));
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  refreshTab();
}
window.addEventListener("hashchange", () => { const t = location.hash.replace("#", ""); if (t && t !== STATE.tab && $(`#tab-${t}`)) showTab(t); });
function refreshTab() {
  ({ overview: loadOverview, bulk: () => { loadAlerts(); loadFeeds(); }, build: loadBuild, match: loadMatch, live: () => {}, sources: loadSources, settings: loadSettings })[STATE.tab]?.();
}

/* ---------------- header ---------------- */
async function loadStats() {
  try {
    const [s, h] = await Promise.all([api("/api/stats"), api("/api/health")]);
    $("#ver").textContent = "v" + h.version;
    const q = h.queue || {};
    $("#stats").innerHTML = `<span><b>${s.persons_named}</b> people</span><span><b>${s.persons_unknown}</b> unnamed voices</span>
      <span><b>${s.prints}</b> prints</span><span><b>${fmtT(s.sources.seconds)}</b> audio</span>
      <span>queue: <b>${q.running || 0}</b> running, <b>${q.queued || 0}</b> waiting</span>
      <span>scoring: <b>${s.score_mode}</b>${s.calibrated ? " (calibrated)" : " (default calibration)"}</span><span>device: <b>${h.device || "?"}</b>${h.gpu && h.gpu.torch_device_name ? " (" + esc(h.gpu.torch_device_name) + ")" : ""}${h.workers > 1 ? " × " + h.workers + " workers" : ""}</span>`;
    const b = $("#banner");
    const g = h.gpu || {};
    if (g.nvidia_driver_gpu && !g.torch_cuda) { b.innerHTML = `NVIDIA GPU found (${esc(g.nvidia_driver_gpu)}) but VoiceFlock is running on the CPU, which is 10-30× slower. Close VoiceFlock and run <code>gpu.bat</code> (Mac/Linux: reinstall torch from the cu126 index), then restart.`; b.classList.remove("hidden"); }
    else if (!h.hf_token_set) { b.innerHTML = `No HuggingFace token set: diarization will fail. Add it under <a href="#settings" onclick="showTab('settings')">Settings</a> after accepting the model terms on huggingface.co.`; b.classList.remove("hidden"); }
    else if (!h.ffmpeg) { b.textContent = "ffmpeg not found on PATH. Install it to process audio."; b.classList.remove("hidden"); }
    else if (!h.torch) { b.textContent = "PyTorch not importable: " + (h.torch_error || ""); b.classList.remove("hidden"); }
    else b.classList.add("hidden");
    return (q.running || 0) + (q.queued || 0);
  } catch (e) { return 0; }
}

/* ---------------- uploads ---------------- */
function wireDrop(zoneId, inputId, mode, listId, opts = {}) {
  const zone = $(zoneId), input = $(inputId);
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("over"); }));
  zone.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files], mode, listId, opts));
  input.addEventListener("change", () => { uploadFiles([...input.files], mode, listId, opts); input.value = ""; });
}
function uploadFiles(files, mode, listId, opts) {
  const list = $(listId);
  files.forEach((f) => {
    const row = document.createElement("div"); row.className = "u";
    row.innerHTML = `<span>${esc(f.name)}</span><div class="bar"><i></i></div><span class="st">0%</span>`;
    list.prepend(row);
    const fd = new FormData(); fd.append("files", f); fd.append("mode", mode);
    fd.append("incognito", opts.incognito ? String(!!opts.incognito()) : "false");
    if (opts.personName) { const n = opts.personName(); if (!n) { row.querySelector(".st").textContent = "name required"; return; } fd.append("person_name", n); }
    const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { const p = e.loaded / e.total; row.querySelector("i").style.width = pct(p); row.querySelector(".st").textContent = p < 1 ? pct(p) : "processing…"; } };
    xhr.onload = () => {
      if (xhr.status >= 300) { row.querySelector(".st").textContent = "failed"; return onErr(new Error(xhr.responseText)); }
      const j = JSON.parse(xhr.responseText); const c = j.created[0];
      if (c.error) { row.querySelector(".st").textContent = c.error; return; }
      if (c.duplicate_of) { row.querySelector(".st").innerHTML = `already processed as <a class="link" onclick="openSource(${c.duplicate_of})">${esc(c.duplicate_name)}</a>`; return; }
      row.dataset.source = c.source_id; row.querySelector(".st").textContent = "queued";
      if (mode === "match") { STATE.matchSource = c.source_id; }
      toast(`Queued ${f.name}`); loadStats();
    };
    xhr.onerror = () => { row.querySelector(".st").textContent = "network error"; };
    xhr.send(fd);
  });
}
async function queueUrl(inputId, mode, opts = {}) {
  const url = $(inputId).value.trim(); if (!url) return;
  try {
    const body = { url, mode, incognito: opts.incognito ? !!opts.incognito() : false };
    if (opts.personName) { body.person_name = opts.personName(); if (!body.person_name) throw new Error("name required"); }
    const r = await api("/api/upload_url", { method: "POST", body });
    $(inputId).value = "";
    if (r.duplicate_of) { toast(`Already processed: ${r.duplicate_name}`); openSource(r.duplicate_of); return; }
    toast("Queued URL as source #" + r.source_id);
    if (mode === "match") STATE.matchSource = r.source_id;
    loadStats();
  } catch (e) { onErr(e); }
}
wireDrop("#drop-bulk", "#file-bulk", "bulk", "#upl-bulk", { incognito: () => $("#inc-bulk").checked });
wireDrop("#drop-match", "#file-match", "match", "#upl-match", { incognito: () => $("#inc-match").checked });
wireDrop("#drop-enroll", "#file-enroll", "enroll", "#upl-enroll", { personName: () => $("#enroll-name").value.trim() });
$("#btn-url-bulk").onclick = () => queueUrl("#url-bulk", "bulk", { incognito: () => $("#inc-bulk").checked });
$("#btn-url-match").onclick = () => queueUrl("#url-match", "match", { incognito: () => $("#inc-match").checked });
$("#btn-url-enroll").onclick = () => queueUrl("#url-enroll", "enroll", { personName: () => $("#enroll-name").value.trim() });

/* update per-row status of uploads from the queue */
async function updateUploadRows() {
  const rows = $$(".uploads .u[data-source]"); if (!rows.length) return;
  const j = await api("/api/jobs?limit=200").catch(() => ({ jobs: [] }));
  const bySrc = {}; j.jobs.forEach((x) => { if (!bySrc[x.source_id]) bySrc[x.source_id] = x; });
  rows.forEach((r) => { const x = bySrc[r.dataset.source]; if (!x) return; const st = r.querySelector(".st");
    if (x.status === "running") { st.textContent = `${x.stage || "running"} ${Math.round(x.progress)}%`; r.querySelector("i").style.width = x.progress + "%"; }
    else if (x.status === "done") { st.innerHTML = `done · <a class="link" onclick="openSource(${x.source_id})">view</a>${x.enroll_person_id ? ` · <a class="link" onclick="openPerson(${x.enroll_person_id})">open person (library re-checked)</a>` : ""}`; }
    else st.textContent = x.status + (x.error ? ": " + x.error : "");
  });
}

/* ---------------- shared renderers ---------------- */
function heardIn(list, cur) {
  if (!list || !list.length) return `<span class="muted">(no other recordings)</span>`;
  return list.map((h) => `<a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> @ ${fmtT(h.first_start_s)}`).join(", ");
}
function confirmReminder(p) {
  const hi = Number(SETTINGS.confirm_threshold || 0.9);
  return p < hi ? `<div class="warnbox">Reminder: only Confirm when you are very sure. This match is ${pct(p)}, below your ${pct(hi)} high-confidence bar. Confirming adds this voice to the person's prints; a wrong confirm pollutes them. (Undo is available.)</div>` : "";
}
function candidateTable(sp, src) {
  const thr = Number(SETTINGS.match_threshold || 0.75), hi = Number(SETTINGS.confirm_threshold || 0.9);
  if (!sp.candidates.length) return `<div class="muted">No candidates (empty gallery).</div>`;
  return `<table><tr><th>#</th><th>Candidate</th><th>Probability</th><th>Margin vs #2</th><th>Score</th><th>Heard in</th><th></th></tr>` +
    sp.candidates.map((c) => {
      const mine = sp.person_id === c.person_id;
      const act = c.status === "confirmed" ? `<span class="pill named">confirmed</span> <button class="small" onclick="unassign(${sp.id})">undo</button>`
        : c.status === "rejected" ? `<span class="pill">rejected</span>`
        : `<button class="small primary" onclick="confirmMatch(${c.id}, ${c.probability})">Confirm</button> <button class="small" onclick="rejectMatch(${c.id})">Reject</button>`;
      return `<tr><td>${c.rank}</td><td><a class="link" onclick="openPerson(${c.person_id})">${esc(c.person_name)}</a><span class="pill ${c.person_kind}">${c.person_kind === "named" ? "known" : "unnamed"}</span>${mine ? '<span class="pill named">assigned</span>' : ""}</td>
        <td><span class="prob ${probCls(c.probability, thr, hi)}">${pct(c.probability)}</span><div class="pbar"><i style="width:${pct(c.probability)}"></i></div></td>
        <td>${c.rank === 1 ? "+" + pct(c.margin) : ""}</td><td class="muted">${c.score_mode}=${c.norm_score.toFixed(2)}<br>cos=${c.raw_score.toFixed(3)}</td>
        <td class="sub">${heardIn(c.heard_in)}</td><td>${act}</td></tr>`;
    }).join("") + `</table>`;
}
function qualityWarning(sp) {
  const w = [];
  if ((sp.speech_seconds || 0) < 10) w.push("under 10 s of speech");
  if ((sp.snr_db ?? 99) < 10) w.push("low SNR");
  if ((sp.clip_fraction || 0) > 0.01) w.push("clipping");
  if ((sp.quality_score ?? 100) < 40 && !w.length) w.push("low quality");
  return w.length ? `<div class="warnbox">Low-confidence clip (${w.join(", ")}): treat the probabilities with caution.</div>` : "";
}
function segChips(sp, src, color) {
  const segs = (sp.segments || []).slice(0, 12);
  if (!segs.length || !src.has_audio) return "";
  return `<div class="chips"><span class="sw" style="background:${color}"></span>` + segs.map((g) => `<button onclick="seek(${src.id}, ${g.start_s})">${fmtT(g.start_s)}</button>`).join("") + (sp.segments.length > 12 ? `<span class="muted">+${sp.segments.length - 12} more</span>` : "") + `</div>`;
}
function timeline(src) {
  if (!src.duration_s || !(src.speakers || []).length) return "";
  const d = src.duration_s;
  const segs = src.speakers.map((sp, i) => (sp.segments || []).map((g) => `<div class="seg" style="left:${(g.start_s / d) * 100}%;width:${Math.max(0.15, ((g.end_s - g.start_s) / d) * 100)}%;background:${COLORS[i % COLORS.length]}" title="${esc(sp.person?.name || sp.label)} ${fmtT(g.start_s)}–${fmtT(g.end_s)}" onclick="event.stopPropagation();seek(${src.id}, ${g.start_s})"></div>`).join("")).join("");
  const legend = src.speakers.map((sp, i) => `<span><i style="background:${COLORS[i % COLORS.length]}"></i>${esc(sp.person?.name || sp.label)}</span>`).join("");
  return `<div class="timeline" id="tl-${src.id}" onclick="seekFrac(${src.id}, event)">${segs}<div class="cursor" style="left:0"></div></div><div class="tl-legend">${legend}</div>`;
}
function seek(sid, t) { const a = $(`#aud-${sid}`); if (!a) return; a.currentTime = t; a.play().catch(() => {}); a.scrollIntoView({ block: "nearest" }); }
function seekFrac(sid, ev) { const tl = $(`#tl-${sid}`), a = $(`#aud-${sid}`); if (!tl || !a || !a.duration) return; const r = tl.getBoundingClientRect(); seek(sid, ((ev.clientX - r.left) / r.width) * a.duration); }
document.addEventListener("timeupdate", (e) => { const a = e.target; if (!(a instanceof HTMLAudioElement) || !a.id.startsWith("aud-")) return; const c = $(`#tl-${a.id.slice(4)} .cursor`); if (c && a.duration) c.style.left = (a.currentTime / a.duration) * 100 + "%"; }, true);
function speakerCard(sp, src, idx = 0) {
  const color = COLORS[idx % COLORS.length];
  const thr = Number(src.settings?.match_threshold ?? SETTINGS.match_threshold ?? 0.75);
  const top = sp.candidates[0];
  let verdict;
  if (sp.person && sp.assignment === "enrolled") verdict = `Enrolled as <b>${esc(sp.person.name)}</b>`;
  else if (sp.person && (sp.assignment === "confirmed" || sp.assignment === "named")) verdict = `<b>${esc(sp.person.name)}</b> <span class="pill named">${sp.assignment}</span>`;
  else if (top && top.probability >= thr && top.person_kind === "named") verdict = `We think this is <b>${esc(top.person_name)}</b> (${pct(top.probability)})`;
  else if (sp.person && sp.person.kind === "provisional") verdict = `Unnamed voice: <b>${esc(sp.person.name)}</b> <span class="muted">(not matched to anyone known)</span>`;
  else if (src.incognito) verdict = `<b>Unknown</b> <span class="muted">(incognito: not saved)</span>`;
  else verdict = `<b>Unknown</b> <span class="muted">(below threshold${top ? `, best ${esc(top.person_name)} ${pct(top.probability)}` : ""})</span>`;
  return `<div class="spk" id="spk-${sp.id}" style="border-left:3px solid ${color}">
    <div class="hdr"><div><span class="title">${esc(sp.label)}</span> · ${verdict}</div>
      <div class="q">speech <b>${fmtT(sp.speech_seconds)}</b> · quality <b>${sp.quality_score?.toFixed(0) ?? "-"}</b>/100 · SNR <b>${sp.snr_db?.toFixed(0) ?? "-"} dB</b> · ${sp.n_segments} turns · first at ${fmtT(sp.first_start_s)}</div></div>
    ${sp.clip_url ? `<audio controls preload="none" src="${sp.clip_url}"></audio>` : `<div class="muted">no preview clip (incognito)</div>`}
    ${segChips(sp, src, color)}
    ${qualityWarning(sp)}
    ${candidateTable(sp, src)}
    <div class="actions">
      <input placeholder="${sp.person ? "Rename / reassign…" : "Not in the database: add as…"}" list="person-names" id="nm-${sp.id}" style="max-width:260px">
      <button class="small ${sp.person ? "" : "primary"}" onclick="nameSpeaker(${sp.id})">${sp.person ? "Name" : "Add to database"}</button>
      ${sp.person_id ? `<button class="small" onclick="unassign(${sp.id})">Unassign / undo</button>` : ""}
    </div></div>`;
}
function sourceView(src) {
  const inc = src.incognito ? '<span class="pill">incognito</span>' : "";
  const st = `<span class="pill ${src.status}">${src.status}</span>`;
  const job = src.job && src.job.status === "running" ? `<div class="sub">${esc(src.job.stage)} ${Math.round(src.job.progress)}%</div>` : "";
  const err = src.error ? `<div class="warnbox">${esc(src.error)}</div>` : "";
  return `<div class="hdr"><h2>${esc(src.name)} ${st}${inc} <span class="pill">${src.mode}</span></h2>
    <div class="q">${src.duration_s ? fmtT(src.duration_s) : ""} · ${src.n_speakers ?? "?"} speakers · uploaded ${esc((src.created_at || "").slice(0, 16).replace("T", " "))}</div></div>${job}${err}
    ${src.has_audio ? `<audio controls preload="none" id="aud-${src.id}" src="/api/sources/${src.id}/audio" style="max-width:100%"></audio>` : ""}
    ${timeline(src)}
    <div class="actions">
      ${src.incognito ? "" : `<button class="small" onclick="forgetSource(${src.id})" title="Remove everything this recording added to the voice database">Forget voices from this file</button>`}
      <button class="small" onclick="reprocess(${src.id})">Reprocess</button>
      <button class="small danger" onclick="deleteSource(${src.id})">Delete recording</button>
    </div>
    ${(src.speakers || []).map((sp, i) => speakerCard(sp, src, i)).join("") || (src.status === "done" ? '<p class="muted">No speakers passed the minimum speech length.</p>' : "")}`;
}

/* ---------------- actions ---------------- */
async function confirmMatch(id, p) {
  const hi = Number(SETTINGS.confirm_threshold || 0.9);
  if (p < hi && !confirm(`This match is ${pct(p)}, below your ${pct(hi)} high-confidence bar.\n\nOnly confirm when you are very sure: confirming adds this voice to the person's prints. Confirm anyway?`)) return;
  try { await api(`/api/matches/${id}/confirm`, { method: "POST" }); toast("Confirmed"); refreshTab(); loadStats(); } catch (e) { onErr(e); }
}
async function rejectMatch(id) { try { await api(`/api/matches/${id}/reject`, { method: "POST" }); toast("Rejected"); refreshTab(); } catch (e) { onErr(e); } }
async function unassign(id) { try { await api(`/api/speakers/${id}/unassign`, { method: "POST" }); toast("Unassigned"); refreshTab(); loadStats(); } catch (e) { onErr(e); } }
async function nameSpeaker(id) {
  const name = $(`#nm-${id}`).value.trim(); if (!name) return toast("Type a name first", true);
  try { await api(`/api/speakers/${id}/name`, { method: "POST", body: { name } }); toast(`Saved as ${name}`); refreshTab(); loadStats(); loadNames(); } catch (e) { onErr(e); }
}
async function forgetSource(id) { if (!confirm("Remove all prints and unnamed voices created from this recording? Results stay visible.")) return; try { const r = await api(`/api/sources/${id}/forget`, { method: "POST" }); toast(`Forgot: removed ${r.removed_unknown_voices} unnamed voice(s)`); refreshTab(); loadStats(); } catch (e) { onErr(e); } }
async function reprocess(id) { try { await api(`/api/sources/${id}/reprocess`, { method: "POST" }); toast("Queued for reprocessing"); refreshTab(); } catch (e) { onErr(e); } }
async function deleteSource(id) { if (!confirm("Delete this recording, its clips and everything it added to the database?")) return; try { await api(`/api/sources/${id}`, { method: "DELETE" }); toast("Deleted"); STATE.sourceDetail = null; if (STATE.matchSource === id) STATE.matchSource = null; refreshTab(); loadStats(); } catch (e) { onErr(e); } }
async function openSource(id) { STATE.sourceDetail = id; showTab("sources"); setTimeout(() => $("#source-detail").scrollIntoView({ behavior: "smooth" }), 50); }
async function openPerson(id) { STATE.personDetail = id; showTab("build"); setTimeout(() => $("#person-detail").scrollIntoView({ behavior: "smooth" }), 50); }
async function namePerson(id) {
  const name = $(`#pn-${id}`).value.trim(); if (!name) return toast("Type a name first", true);
  try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); toast(`Named ${name}`); loadBuild(); loadStats(); loadNames(); } catch (e) { onErr(e); }
}
async function mergePerson(id) {
  const into = $(`#mg-${id}`).value; if (!into) return;
  if (!confirm("Merge this voice into the selected one? Prints and recordings move over; this entry is deleted.")) return;
  try { await api(`/api/persons/${id}/merge`, { method: "POST", body: { into_id: Number(into) } }); toast("Merged"); loadBuild(); loadStats(); } catch (e) { onErr(e); }
}
async function deletePerson(id) { if (!confirm("Delete this voice and all its prints?")) return; try { await api(`/api/persons/${id}`, { method: "DELETE" }); toast("Deleted"); STATE.personDetail = null; loadBuild(); loadStats(); loadNames(); } catch (e) { onErr(e); } }
async function acceptPerson(id) { try { const p = await api(`/api/persons/${id}/accept`, { method: "POST" }); toast(`Kept as ${p.name}; it will now raise alerts`); loadBuild(); loadStats(); loadNames(); } catch (e) { onErr(e); } }
async function renamePerson(id) { const name = prompt("New name?"); if (!name) return; try { await api(`/api/persons/${id}`, { method: "PATCH", body: { name } }); loadBuild(); loadNames(); } catch (e) { onErr(e); } }
async function loadNames() {
  const j = await api("/api/persons?kind=named").catch(() => ({ persons: [] }));
  $("#person-names").innerHTML = j.persons.map((p) => `<option value="${esc(p.name)}">`).join("");
  return j.persons;
}

/* ---------------- bulk / alerts ---------------- */
async function loadAlerts() {
  try {
    const a = await api("/api/alerts");
    $("#alert-hint").textContent = `Matches at or above your ${pct(SETTINGS.match_threshold || 0.75)} threshold. Confirm only when very sure (≥ ${pct(a.confirm_threshold)}).`;
    const thr = Number(SETTINGS.match_threshold || 0.75);
    $("#alerts-known").innerHTML = a.known.length ? a.known.map((k) => `<div class="item">
      <div class="title">We think <a class="link" onclick="openPerson(${k.person_id})">${esc(k.person_name)}</a> is in <a class="link" onclick="openSource(${k.source_id})">${esc(k.source_name)}</a> at ${fmtT(k.first_start_s)}
        <span class="prob ${probCls(k.probability, thr, a.confirm_threshold)}">${pct(k.probability)}</span> <span class="muted">(+${pct(k.margin)} over #2)</span> ${k.status === "confirmed" ? '<span class="pill named">confirmed</span>' : ""}</div>
      <div class="sub">Check against: ${heardIn(k.heard_in)} · speech ${fmtT(k.speech_seconds)} · quality ${Math.round(k.quality_score || 0)}/100</div>
      ${k.clip_url ? `<audio controls preload="none" src="${k.clip_url}"></audio>` : ""}
      ${k.status === "confirmed" ? "" : confirmReminder(k.probability)}
      <div class="actions">${k.status === "confirmed" ? `<button class="small" onclick="unassign(${k.speaker_id})">Undo confirm</button>` : `<button class="small primary" onclick="confirmMatch(${k.match_id}, ${k.probability})">Confirm</button><button class="small" onclick="rejectMatch(${k.match_id})">Reject</button>`}
        <button class="small" onclick="openSource(${k.source_id})">Open recording</button></div></div>`).join("") : `<p class="muted">Nothing yet. Enroll people under Build Database, then scan files.</p>`;
    $("#alerts-recurring").innerHTML = a.recurring_unknown.length ? a.recurring_unknown.map((r) => `<div class="item">
      <div class="title"><a class="link" onclick="openPerson(${r.person_id})">${esc(r.name)}</a> heard in <b>${r.n_sources}</b> recordings (${fmtT(r.speech_seconds)} of speech)</div>
      <div class="sub">${heardIn(r.heard_in)}</div>
      ${r.heard_in[0]?.clip_url ? `<audio controls preload="none" src="${r.heard_in[0].clip_url}"></audio>` : ""}
      <div class="inline"><input placeholder="Name this voice" list="person-names" id="pn-${r.person_id}"><button class="small" onclick="namePerson(${r.person_id})">Name</button></div></div>`).join("") : `<p class="muted">No unnamed voice has shown up in two or more recordings yet.</p>`;
  } catch (e) { onErr(e); }
}

/* ---------------- alert flags ---------------- */
const bell = (p) => `<span class="bell ${p.alert ? "on" : ""}" title="${p.alert ? "Alerting on this voice (Live + phone). Click to stop." : "Alert me when this voice is heard (Live + phone)"}" onclick="toggleAlert(${p.id}, ${p.alert ? "false" : "true"}, event)">🔔</span>`;
async function toggleAlert(id, on, ev) { ev?.stopPropagation(); try { await api(`/api/persons/${id}`, { method: "PATCH", body: { alert: on } }); toast(on ? "Will alert on this voice" : "Alert off"); refreshTab(); loadNames(); } catch (e) { onErr(e); } }

/* ---------------- glyphs & overview ---------------- */
function glyphSvg(g, color = "var(--s1)", size = 28) {
  if (!g) return `<span class="glyph"></span>`;
  const n = g.length, r0 = 5, r1 = 13, cx = 14, cy = 14;
  const bars = g.map((v, i) => { const a = (i / n) * Math.PI * 2, r = r0 + v * (r1 - r0); return `<line x1="${cx + Math.cos(a) * r0}" y1="${cy + Math.sin(a) * r0}" x2="${cx + Math.cos(a) * r}" y2="${cy + Math.sin(a) * r}" stroke="${color}" stroke-width="1.6" stroke-linecap="round"/>`; }).join("");
  return `<svg class="glyph" width="${size}" height="${size}" viewBox="0 0 28 28" aria-hidden="true"><circle cx="14" cy="14" r="13" fill="var(--panel2)"/>${bars}</svg>`;
}
let OV = null;
async function loadOverview() {
  try {
    const o = await api("/api/overview"); OV = o;
    const t = o.stats;
    $("#ov-tiles").innerHTML = [[t.hours.toFixed(1), "hours of recordings"], [t.recordings, "recordings"], [t.known, "known people"], [t.unknown, "unnamed voices"], [t.speech_hours.toFixed(1), "hours of attributed speech"], [t.open_alerts, "alerts to review"]]
      .map(([v, l]) => `<div class="tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");
    const maxS = Math.max(1, ...o.top_voices.map((v) => v.speech_seconds));
    $("#ov-top").innerHTML = o.top_voices.map((v) => `<div class="hbar"><a onclick="openPerson(${v.id})" style="cursor:pointer">${glyphSvg(v.glyph, v.kind === "named" ? "var(--s1)" : "var(--s3)")}</a>
      <div><div class="nm"><a class="link" onclick="openPerson(${v.id})">${esc(v.name)}</a><span>${fmtT(v.speech_seconds)} · ${v.n_sources} rec</span></div><div class="bar"><i style="width:${(v.speech_seconds / maxS) * 100}%"></i></div></div></div>`).join("") || '<p class="muted">Nothing processed yet.</p>';
    const act = o.activity; const maxA = Math.max(1, ...act.map((a) => a.hours));
    $("#ov-activity").innerHTML = act.length ? `<div class="cols">${act.map((a) => `<div class="c" style="height:${Math.max(2, (a.hours / maxA) * 100)}%" data-tip="${esc(a.day)}: ${a.recordings} rec · ${a.hours.toFixed(1)} h"></div>`).join("")}</div><div class="xlab"><span>${esc(act[0].day)}</span><span>hours of audio added per day</span><span>${esc(act[act.length - 1].day)}</span></div>` : '<p class="muted">No recordings yet.</p>';
    $("#ov-pairs").innerHTML = o.pairs.map((p) => `<div class="item"><a class="link" onclick="openPerson(${p.a_id})">${esc(p.a_name)}</a> + <a class="link" onclick="openPerson(${p.b_id})">${esc(p.b_name)}</a> <span class="muted">· ${p.n} recording${p.n > 1 ? "s" : ""}</span></div>`).join("") || '<p class="muted">Needs recordings with two or more identified voices.</p>';
    drawMap(o.voices);
  } catch (e) { onErr(e); }
}
function drawMap(voices) {
  const cv = $("#ov-map"); const dpr = window.devicePixelRatio || 1; const W = cv.clientWidth || 600, H = 420;
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
  tip.innerHTML = `<b>${esc(hit.v.name)}</b> <span class="muted">${hit.v.kind === "named" ? "known" : "unnamed"}</span><br>${fmtT(hit.v.speech_seconds)} of speech · ${hit.v.n_sources} recordings`;
});
$("#ov-map").addEventListener("mouseleave", () => $("#ov-tip").classList.add("hidden"));
$("#ov-map").addEventListener("click", (e) => { const h = e.target._hit; if (h) openPerson(h.v.id); });
window.addEventListener("resize", () => { if (STATE.tab === "overview" && OV) drawMap(OV.voices); });

/* ---------------- live listen ---------------- */
let LIVE = null;
function beep() { try { const ac = new (window.AudioContext || window.webkitAudioContext)(); const o = ac.createOscillator(), g = ac.createGain(); o.frequency.value = 880; g.gain.value = 0.08; o.connect(g); g.connect(ac.destination); o.start(); o.stop(ac.currentTime + 0.18); } catch (e) {} }
$("#btn-live").onclick = async () => {
  if (LIVE) { stopLive(); return; }
  const st = $("#live-status");
  if (!navigator.mediaDevices?.getUserMedia) { st.textContent = "Microphone needs localhost or HTTPS."; return; }
  if ($("#live-notify").checked && "Notification" in window && Notification.permission === "default") Notification.requestPermission();
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true } });
    const ac = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
    const src = ac.createMediaStreamSource(stream);
    const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws/live");
    ws.binaryType = "arraybuffer";
    const ratio = ac.sampleRate / 16000; // browsers may ignore the requested rate; downsample if so
    const proc = ac.createScriptProcessor(4096, 1, 1);
    proc.onaudioprocess = (e) => {
      if (ws.readyState !== 1) return;
      const inp = e.inputBuffer.getChannelData(0); const n = Math.floor(inp.length / ratio); const out = new Int16Array(n);
      let peak = 0;
      for (let i = 0; i < n; i++) { const v = Math.max(-1, Math.min(1, inp[Math.floor(i * ratio)])); out[i] = v * 32767; peak = Math.max(peak, Math.abs(v)); }
      $("#live-level").style.width = Math.min(100, peak * 140) + "%";
      ws.send(out.buffer);
    };
    src.connect(proc); proc.connect(ac.destination);
    LIVE = { stream, ac, proc, ws, lastAlert: 0, log: [] };
    $("#btn-live").textContent = "⏹ Stop listening"; st.textContent = "connecting…";
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "status") { st.textContent = m.text; return; }
      if (m.type === "error") { st.textContent = m.text; stopLive(); return; }
      renderLive(m);
    };
    ws.onclose = () => { if (LIVE) { st.textContent = "connection closed"; stopLive(); } };
  } catch (e) { st.textContent = "microphone blocked: " + e.message; }
};
function stopLive() {
  if (!LIVE) return;
  try { LIVE.proc.disconnect(); LIVE.ac.close(); LIVE.stream.getTracks().forEach((t) => t.stop()); LIVE.ws.close(); } catch (e) {}
  LIVE = null; $("#btn-live").textContent = "▶ Start listening"; $("#live-level").style.width = "0"; $("#live-now").className = "livenow"; $("#live-now").innerHTML = '<div class="muted">Not listening.</div>';
}
function renderLive(m) {
  const now = $("#live-now"); const thr = Number(SETTINGS.match_threshold || 0.75);
  if (!m.speech) { now.className = "livenow"; now.innerHTML = `<div class="muted">silence · ${m.level_db} dBFS</div>`; return; }
  const top = m.candidates[0];
  if (!top) { now.innerHTML = `<div class="who muted">speech, empty gallery</div>`; return; }
  const known = top.kind === "named";
  now.className = "livenow" + (m.alert ? " alert" : "");
  now.innerHTML = `<div class="muted">${m.alert && top.flagged ? "🔔 FLAGGED VOICE" : m.alert ? "KNOWN VOICE" : top.probability >= thr ? "unnamed voice from the library" : "no confident match"}</div>
    <div class="who" style="${m.alert ? "" : "opacity:.7"}">${esc(top.name)} <span class="prob ${probCls(top.probability, thr, SETTINGS.confirm_threshold || 0.9)}" style="font-size:18px">${pct(top.probability)}</span></div>
    <div class="meter"><i style="width:${pct(top.probability)}"></i></div>
    <div class="others">${m.candidates.slice(1).map((c) => `${esc(c.name)} ${pct(c.probability)}`).join(" · ") || ""}</div>`;
  if (m.alert && Date.now() - LIVE.lastAlert > 8000) {
    LIVE.lastAlert = Date.now();
    if ($("#live-beep").checked) beep();
    if ($("#live-notify").checked && "Notification" in window && Notification.permission === "granted") new Notification("VoiceFlock", { body: `${top.name} (${pct(top.probability)})` });
    LIVE.log.unshift(`${new Date().toLocaleTimeString()} — ${top.name} ${pct(top.probability)}${known ? "" : " (unnamed)"}`);
    $("#live-log").innerHTML = LIVE.log.slice(0, 50).map((l) => `<div class="item">${esc(l)}</div>`).join("");
  }
}

/* ---------------- youtube channel import ---------------- */
let YT = { title: null, videos: [] };
$("#btn-yt-list").onclick = async () => {
  const url = $("#yt-url").value.trim(); if (!url) return;
  const out = $("#yt-results"); out.innerHTML = '<p class="muted">Listing… (a big channel takes 10-30 s)</p>';
  try {
    const body = { url, limit: Number($("#yt-limit").value || 300), title_regex: $("#yt-filter").value.trim() || null,
      min_minutes: $("#yt-min").value ? Number($("#yt-min").value) : null, max_minutes: $("#yt-max").value ? Number($("#yt-max").value) : null,
      date_from: $("#yt-from").value || null };
    const j = await api("/api/youtube/list", { method: "POST", body }); YT = j;
    const fresh = j.videos.filter((v) => !v.already_imported);
    out.innerHTML = `<div class="hdr"><b>${esc(j.title || url)}</b> <span class="muted">${j.videos.length} videos match · ${fmtT(j.total_minutes * 60)} total · ${fresh.length} not yet in the library</span>
      <div class="actions"><button class="small" onclick="ytAll(true)">select all new</button><button class="small" onclick="ytAll(false)">none</button><button class="small primary" onclick="ytImport()">Queue selected</button></div></div>
      <div style="max-height:360px;overflow:auto">${j.videos.map((v, i) => `<div class="ytrow"><input type="checkbox" data-i="${i}" ${v.already_imported ? "disabled" : "checked"}>
        <div>${esc(v.title)}${v.already_imported ? ` <a class="link" onclick="openSource(${v.already_imported})">(in library)</a>` : ""}</div>
        <div class="muted">${v.duration_s ? fmtT(v.duration_s) : ""}</div><div class="muted">${esc(v.date || "")}</div></div>`).join("")}</div>`;
  } catch (e) { out.innerHTML = ""; onErr(e); }
};
function ytAll(on) { $$("#yt-results input[type=checkbox]:not(:disabled)").forEach((c) => (c.checked = on)); }
async function ytImport() {
  const sel = $$("#yt-results input[type=checkbox]:checked").map((c) => YT.videos[Number(c.dataset.i)]);
  if (!sel.length) return toast("Nothing selected", true);
  try {
    const r = await api("/api/youtube/import", { method: "POST", body: { videos: sel, mode: "bulk", incognito: $("#inc-bulk").checked, prefix: $("#yt-prefix").checked ? YT.title : null } });
    toast(`Queued ${r.created.length} video(s)${r.skipped ? `, ${r.skipped} already in the library` : ""}`); loadStats(); $("#btn-yt-list").click();
  } catch (e) { onErr(e); }
}

/* ---------------- feeds ---------------- */
async function loadFeeds() {
  try {
    const j = await api("/api/feeds");
    $("#feeds-count").textContent = j.feeds.length ? `(${j.feeds.length})` : "";
    $("#feeds").innerHTML = j.feeds.map((f) => `<div class="item">
      <div class="title">${esc(f.title || f.url)} <span class="pill">${f.mode}</span>${f.incognito ? '<span class="pill">incognito</span>' : ""}${f.enabled ? "" : '<span class="pill failed">paused</span>'}</div>
      <div class="sub">${esc(f.url)}<br>${f.n_items} items · every ${f.interval_min} min · last check ${f.last_checked_at ? esc(f.last_checked_at.slice(0, 16).replace("T", " ")) : "never"}${f.last_error ? `<div class="warnbox">${esc(f.last_error)}</div>` : ""}</div>
      ${f.recent.map((r) => `<div class="sub">· ${r.source_id ? `<a class="link" onclick="openSource(${r.source_id})">${esc(r.title || r.url)}</a> <span class="pill ${r.source_status || ""}">${r.source_status || ""}</span>` : `<span class="muted">${esc(r.title || r.url)} (skipped)</span>`}</div>`).join("")}
      <div class="actions"><button class="small" onclick="pollFeed(${f.id})">Check now</button><button class="small" onclick="toggleFeed(${f.id}, ${f.enabled ? "false" : "true"})">${f.enabled ? "Pause" : "Resume"}</button><button class="small danger" onclick="deleteFeed(${f.id})">Remove</button></div></div>`).join("") || '<p class="muted">No feeds yet.</p>';
  } catch (e) { onErr(e); }
}
$("#btn-add-feed").onclick = async () => {
  const url = $("#feed-url").value.trim(); if (!url) return;
  try {
    const f = await api("/api/feeds", { method: "POST", body: { url, mode: $("#feed-mode").value, incognito: $("#feed-incognito").checked, interval_min: Number($("#feed-interval").value || 60), backfill: Number($("#feed-backfill").value || 0) } });
    $("#feed-url").value = ""; toast("Feed added; checking now…"); loadFeeds();
    try { const r = await api(`/api/feeds/${f.id}/poll`, { method: "POST" }); toast(`Feed OK: queued ${r.queued} episode(s)`); } catch (e) { onErr(e); }
    loadFeeds(); loadStats();
  } catch (e) { onErr(e); }
};
async function pollFeed(id) { try { const r = await api(`/api/feeds/${id}/poll`, { method: "POST" }); toast(`Queued ${r.queued} new`); loadFeeds(); loadStats(); } catch (e) { onErr(e); } }
async function toggleFeed(id, enabled) { try { await api(`/api/feeds/${id}`, { method: "PATCH", body: { enabled } }); loadFeeds(); } catch (e) { onErr(e); } }
async function deleteFeed(id) { if (!confirm("Stop watching this feed? Already-downloaded recordings stay.")) return; try { await api(`/api/feeds/${id}`, { method: "DELETE" }); loadFeeds(); } catch (e) { onErr(e); } }

/* ---------------- build database ---------------- */
async function loadBuild() {
  try {
    const [u, named] = await Promise.all([api("/api/voices/unnamed"), loadNames()]);
    $("#unnamed-count").textContent = `(${u.voices.length})`;
    const mergeOpts = (self) => `<option value="">Merge into…</option>` + u.voices.filter((v) => v.id !== self).map((v) => `<option value="${v.id}">${esc(v.name)}</option>`).join("") + named.map((p) => `<option value="${p.id}">${esc(p.name)} (known)</option>`).join("");
    $("#unnamed").innerHTML = u.voices.length ? `<div class="grid">` + u.voices.map((v) => `<div class="vcard">
      <div class="nm">${esc(v.name)}${bell(v)} <span class="muted">· ${v.n_sources} rec · ${fmtT(v.speech_seconds)}</span></div>
      ${v.heard_in.slice(0, 3).map((h) => `<div class="sub"><a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> @ ${fmtT(h.first_start_s)}</div>${h.clip_url ? `<audio controls preload="none" src="${h.clip_url}"></audio>` : ""}`).join("")}
      <div class="inline"><input placeholder="Name" list="person-names" id="pn-${v.id}"><button class="small primary" onclick="namePerson(${v.id})">Name</button></div>
      <div class="inline"><button class="small" onclick="acceptPerson(${v.id})" title="Track this voice as a known voice without naming it">Keep</button><select id="mg-${v.id}">${mergeOpts(v.id)}</select><button class="small" onclick="mergePerson(${v.id})">Merge</button><button class="small danger" onclick="deletePerson(${v.id})">Delete</button></div>
    </div>`).join("") + `</div>` : `<p class="muted">No unnamed voices yet. Scan some recordings.</p>`;
    renderPeople(named);
    loadMergeSuggestions();
    if (STATE.personDetail) loadPersonDetail(STATE.personDetail); else $("#person-detail").classList.add("hidden");
  } catch (e) { onErr(e); }
}
async function loadMergeSuggestions() {
  try {
    const j = await api("/api/voices/merge_suggestions");
    const el = $("#merge-suggestions");
    if (!j.pairs.length) { el.innerHTML = ""; return; }
    el.innerHTML = `<h2>Probably the same person <span class="muted">(${j.pairs.length})</span></h2><p class="muted">Different mics or rooms can split one person into several entries. Listen to both, then merge.</p>` +
      j.pairs.slice(0, 20).map((p) => `<div class="item"><div class="title"><a class="link" onclick="openPerson(${p.a.id})">${esc(p.a.name)}</a><span class="pill ${p.a.kind}">${p.a.kind === "named" ? "known" : "unnamed"}</span> ≈ <a class="link" onclick="openPerson(${p.b.id})">${esc(p.b.name)}</a><span class="pill ${p.b.kind}">${p.b.kind === "named" ? "known" : "unnamed"}</span> <span class="prob hi">${pct(p.probability)}</span></div>
        <div class="row" style="margin:4px 0 0">${[p.a, p.b].map((x) => `<div class="col">${x.heard_in[0]?.clip_url ? `<audio controls preload="none" src="${x.heard_in[0].clip_url}"></audio>` : '<span class="muted">no clip</span>'}</div>`).join("")}</div>
        <div class="actions"><button class="small primary" onclick="mergeInto(${p.a.kind === "named" ? p.b.id : p.a.id}, ${p.a.kind === "named" ? p.a.id : p.b.id})">Merge into ${esc(p.a.kind === "named" ? p.a.name : p.b.name)}</button>${p.a.kind !== "named" && p.b.kind !== "named" ? ` <button class="small" onclick="mergeInto(${p.b.id}, ${p.a.id})">Merge into ${esc(p.a.name)}</button>` : ""}</div></div>`).join("");
  } catch (e) { onErr(e); }
}
async function mergeInto(fromId, intoId) { try { await api(`/api/persons/${fromId}/merge`, { method: "POST", body: { into_id: intoId } }); toast("Merged"); loadBuild(); loadStats(); } catch (e) { onErr(e); } }
function renderPeople(list) {
  const q = ($("#person-search").value || "").toLowerCase();
  $("#people").innerHTML = list.filter((p) => p.name.toLowerCase().includes(q)).map((p) => `<div class="item" style="display:flex;gap:8px;align-items:center">${glyphSvg(p.glyph)}<div><a class="link" onclick="openPerson(${p.id})">${esc(p.name)}</a>${bell(p)}
    <div class="muted" style="font-size:12px">${p.n_prints} prints · ${p.n_sources} rec · ${fmtT(p.speech_seconds)}</div></div></div>`).join("") || `<p class="muted">No people yet.</p>`;
}
$("#person-search").addEventListener("input", () => loadNames().then(renderPeople));
async function loadPersonDetail(id) {
  try {
    const p = await api(`/api/persons/${id}`);
    const el = $("#person-detail"); el.classList.remove("hidden");
    el.innerHTML = `<div class="hdr"><h2 style="display:flex;align-items:center;gap:10px">${glyphSvg(p.glyph, p.kind === "named" ? "var(--s1)" : "var(--s3)", 36)} ${esc(p.name)} <span class="pill ${p.kind}">${p.kind === "named" ? "known" : "unnamed"}</span>${bell(p)}</h2>
      <div class="actions"><button class="small" onclick="renamePerson(${p.id})">Rename</button><button class="small danger" onclick="deletePerson(${p.id})">Delete</button><button class="small" onclick="STATE.personDetail=null;loadBuild()">Close</button></div></div>
      <div class="sub">${p.n_prints} prints from ${p.n_sources} recordings · ${fmtT(p.speech_seconds)} of speech ${p.notes ? "· " + esc(p.notes) : ""}</div>
      <h3>Heard in</h3>${p.heard_in.map((h) => `<div class="item"><a class="link" onclick="openSource(${h.id})">${esc(h.name)}</a> @ ${fmtT(h.first_start_s)} <span class="pill">${esc(h.assignment)}</span>
        ${h.clip_url ? `<audio controls preload="none" src="${h.clip_url}"></audio>` : ""}<div class="actions"><button class="small" onclick="unassign(${h.speaker_id})">Remove this recording from the person (undo)</button></div></div>`).join("") || '<p class="muted">none</p>'}
      <h3>Prints (provenance)</h3><table><tr><th>id</th><th>type</th><th>origin</th><th>recording</th><th>speech</th><th>quality</th><th>added</th></tr>
      ${p.prints.map((pr) => `<tr><td>${pr.id}</td><td>${pr.is_mean ? "mean" : "chunk"}</td><td>${esc(pr.origin)}</td><td>${pr.source_id ? `<a class="link" onclick="openSource(${pr.source_id})">${esc(pr.source_name)}</a> / ${esc(pr.label)}` : "cli"}</td><td>${pr.speech_seconds ? fmtT(pr.speech_seconds) : ""}</td><td>${pr.quality ?? ""}</td><td class="muted">${esc((pr.created_at || "").slice(0, 16))}</td></tr>`).join("")}</table>
      <h3>Recent matches</h3>${p.matches.slice(0, 20).map((m) => `<div class="item"><a class="link" onclick="openSource(${m.source_id})">${esc(m.source_name)}</a> / ${esc(m.label)} · <span class="prob">${pct(m.probability)}</span> <span class="pill">${m.status}</span></div>`).join("") || '<p class="muted">none</p>'}`;
  } catch (e) { onErr(e); }
}

/* ---------------- microphone recording ---------------- */
let REC = null;
$("#btn-rec").onclick = async () => {
  const btn = $("#btn-rec"), st = $("#rec-status");
  if (REC) { REC.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia) { st.textContent = "Recording needs HTTPS or localhost (browser rule). Use the laptop itself, or Tailscale HTTPS."; return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
    const chunks = []; const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const t0 = Date.now(); const tick = setInterval(() => { st.textContent = `recording ${fmtT((Date.now() - t0) / 1000)}… click again to stop`; }, 500);
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = () => {
      clearInterval(tick); stream.getTracks().forEach((t) => t.stop()); REC = null; btn.textContent = "🎙 Record from microphone";
      const ext = (rec.mimeType || "").includes("mp4") ? "m4a" : (rec.mimeType || "").includes("ogg") ? "ogg" : "webm";
      const f = new File(chunks, `recording ${new Date().toISOString().slice(0, 16).replace("T", " ")}.${ext}`, { type: rec.mimeType || "audio/webm" });
      st.textContent = `recorded ${fmtT((Date.now() - t0) / 1000)}, uploading`;
      uploadFiles([f], "match", "#upl-match", { incognito: () => $("#inc-match").checked });
    };
    rec.start(1000); REC = rec; btn.textContent = "⏹ Stop recording";
  } catch (e) { st.textContent = "microphone blocked: " + e.message; }
};

/* ---------------- match ---------------- */
async function loadMatch() {
  try {
    const j = await api("/api/sources?mode=match&limit=15");
    $("#match-recent").innerHTML = j.sources.map((s) => `<div class="item"><a class="link" onclick="STATE.matchSource=${s.id};loadMatch()">${esc(s.name)}</a> <span class="pill ${s.status}">${s.status}</span>${s.incognito ? '<span class="pill">incognito</span>' : ""}
      <div class="sub">${s.speakers.map((sp) => sp.top_name ? `${esc(sp.label)}→${esc(sp.top_name)} ${pct(sp.top_prob)}` : esc(sp.label)).join(" · ")}</div></div>`).join("") || '<p class="muted">No match uploads yet.</p>';
    if (!STATE.matchSource && j.sources.length) STATE.matchSource = j.sources[0].id;
    if (STATE.matchSource) { const src = await api(`/api/sources/${STATE.matchSource}`); $("#match-result").innerHTML = sourceView(src); }
  } catch (e) { onErr(e); }
}

/* ---------------- sources & queue ---------------- */
async function loadSources() {
  try {
    const qs = encodeURIComponent($("#source-search").value.trim());
    const [j, q] = await Promise.all([api(`/api/sources?limit=200&q=${qs}`), api("/api/jobs?limit=50")]);
    const jobLabel = (x) => x.type === "process_source" ? `<a class="link" onclick="openSource(${x.source_id})">${esc(x.source_name || "#" + x.source_id)}</a>` : `<b>${esc(x.type)}</b>${x.source_id ? " #" + x.source_id : ""}`;
    $("#jobs").innerHTML = q.jobs.filter((x) => x.status !== "cancelled").slice(0, 30).map((x) => `<div class="item"><span class="pill ${x.status}">${x.status}</span> ${jobLabel(x)}
      <div class="sub">${x.status === "running" ? `${esc(x.stage || "")} ${Math.round(x.progress)}% · ${elapsed(x.started_at)} elapsed · last update ${elapsed(x.heartbeat_at) || "-"} ago <button class="small" onclick="cancelJob(${x.id})">stop</button><div class="bar"><i style="width:${x.progress}%"></i></div>${x.log ? `<pre class="muted" style="margin:4px 0;font-size:11px;white-space:pre-wrap">${esc(x.log.split("\n").slice(-4).join("\n"))}</pre>` : ""}` : x.status === "failed" ? `<span class="warnbox">${esc(x.error)}</span> ${x.type === "process_source" ? `<button class="small" onclick="retryJob(${x.id})">retry</button>` : ""} <button class="small" onclick="dismissJob(${x.id})">dismiss</button>${x.log ? ` <details><summary class="muted" style="cursor:pointer">details</summary><pre class="muted" style="font-size:11px;white-space:pre-wrap">${esc(x.log.slice(-1500))}</pre></details>` : ""}` : x.status === "queued" ? `<button class="small" onclick="cancelJob(${x.id})">cancel</button>` : `${esc((x.finished_at || "").slice(0, 16).replace("T", " "))}${x.type === "rematch" && x.result ? " · " + esc(x.result) : ""}${x.enroll_person_id ? ` · <a class="link" onclick="openPerson(${x.enroll_person_id})">open person</a>` : ""}`}</div></div>`).join("") || '<p class="muted">Queue is empty.</p>';
    $("#sources").innerHTML = `<table><tr><th>Recording</th><th>Mode</th><th>Status</th><th>Length</th><th>Speakers</th><th></th></tr>` + j.sources.map((s) => `<tr>
      <td><a class="link" onclick="openSource(${s.id})">${esc(s.name)}</a>${s.incognito ? '<span class="pill">incognito</span>' : ""}</td><td>${s.mode}</td>
      <td><span class="pill ${s.status}">${s.status}</span>${s.job && s.job.status === "running" ? ` <span class="muted">${Math.round(s.job.progress)}%</span>` : ""}</td>
      <td>${s.duration_s ? fmtT(s.duration_s) : ""}</td>
      <td class="sub">${s.speakers.map((sp) => sp.person_name ? `<span class="pill ${sp.person_kind}">${esc(sp.person_name)}</span>` : sp.top_name ? `<span class="muted">${esc(sp.label)}≈${esc(sp.top_name)} ${pct(sp.top_prob)}</span>` : `<span class="muted">${esc(sp.label)}</span>`).join(" ")}</td>
      <td><button class="small danger" onclick="deleteSource(${s.id})">✕</button></td></tr>`).join("") + `</table>`;
    if (STATE.sourceDetail) { const src = await api(`/api/sources/${STATE.sourceDetail}`).catch(() => null); const el = $("#source-detail"); if (src) { el.classList.remove("hidden"); el.innerHTML = `<div class="actions" style="float:right"><button class="small" onclick="STATE.sourceDetail=null;loadSources()">Close</button></div>` + sourceView(src); } else el.classList.add("hidden"); }
    else $("#source-detail").classList.add("hidden");
  } catch (e) { onErr(e); }
}
$("#btn-clear-jobs").onclick = async () => { try { const r = await api("/api/jobs/clear", { method: "POST" }); toast(`Cleared ${r.deleted}`); loadSources(); } catch (e) { onErr(e); } };
async function dismissJob(id) { try { await api(`/api/jobs/${id}`, { method: "DELETE" }); loadSources(); } catch (e) { onErr(e); } }
$("#source-search").addEventListener("input", () => { clearTimeout(STATE._st); STATE._st = setTimeout(loadSources, 250); });
$("#btn-rematch").onclick = async () => { try { await api("/api/rematch", { method: "POST", body: {} }); toast("Re-match queued"); loadSources(); } catch (e) { onErr(e); } };
async function retryJob(id) { try { await api(`/api/jobs/${id}/retry`, { method: "POST" }); loadSources(); } catch (e) { onErr(e); } }
async function cancelJob(id) { try { const r = await api(`/api/jobs/${id}/cancel`, { method: "POST" }); toast(r.status === "running" ? "Stopping at the next progress tick…" : "Cancelled"); loadSources(); } catch (e) { onErr(e); } }

/* ---------------- settings ---------------- */
async function loadSettings() {
  try {
    const j = await api("/api/settings"); SETTINGS = j.values; SPECS = j.specs;
    const groups = {}; Object.entries(SPECS).forEach(([k, s]) => (groups[s.group] ||= []).push(k));
    const f = $("#settings-form"); f.className = "sform";
    f.innerHTML = Object.entries(groups).map(([g, keys]) => `<div class="grp">${g}</div>` + keys.map((k) => { const s = SPECS[k], v = SETTINGS[k];
      const input = s.type === "bool" ? `<input type="checkbox" name="${k}" ${v ? "checked" : ""}>` : s.type === "secret" ? `<input type="password" name="${k}" value="${esc(v)}" autocomplete="off">` : s.type === "float" ? `<input type="number" step="0.01" name="${k}" value="${v}">` : s.type === "int" ? `<input type="number" step="1" name="${k}" value="${v}">` : `<input name="${k}" value="${esc(v)}">`;
      return `<div class="f"><label>${esc(s.label)}<small>${esc(s.help)}</small></label>${input}</div>`; }).join("")).join("");
    renderSelftest();
    renderPhone();
    const st = await api("/api/stats");
    $("#calib-info").textContent = `scoring mode: ${st.score_mode} (cohort size ${st.cohort_size}, min ${SETTINGS.cohort_min_size})\ncalibration: ${st.calibrated ? "fitted by `voiceflock calibrate`" : "defaults"}  raw a=${st.calibration.raw.a.toFixed(3)} b=${st.calibration.raw.b.toFixed(3)}  snorm a=${st.calibration.snorm.a.toFixed(3)} b=${st.calibration.snorm.b.toFixed(3)}`;
  } catch (e) { onErr(e); }
}
async function renderPhone() {
  try {
    const h = await api("/api/health");
    const url = location.origin;
    const port = location.port || (location.protocol === "https:" ? 443 : 80);
    const addrs = (h.lan_ips || []).map((ip) => `http://${ip}:${port}`);
    $("#phone-info").textContent = `Address to enter in the app (phone on the same Wi-Fi):\n  ${addrs.join("\n  ") || url}\nPassword: ${h.password_required ? "the one in .env" : "none set"}\nIf the phone can't connect but this page works on the laptop, run allow-firewall.bat once (Windows Firewall), or use Tailscale.`;
    const r = await fetch("/api/mobile/model.onnx", { method: "HEAD" });
    const ms = $("#model-status");
    if (r.status === 200) { const mb = Math.round((Number(r.headers.get("content-length")) || 0) / 1048576); ms.textContent = `phone model ready (${mb} MB)`; }
    else ms.textContent = "phone model not exported yet (the app's first sync triggers it, or press the button)";
  } catch (e) {}
}
$("#btn-export-model").onclick = async () => { try { await api("/api/mobile/export_model", { method: "POST" }); toast("Model export queued (about a minute)"); } catch (e) { onErr(e); } };
async function renderSelftest() {
  const { job } = await api("/api/selftest").catch(() => ({ job: null }));
  const st = $("#selftest-status"), out = $("#selftest-result");
  if (!job) { st.textContent = "never run"; out.textContent = ""; return; }
  st.innerHTML = `<span class="pill ${job.status}">${job.status}</span> ${job.status === "running" ? esc(job.stage || "") + " · " + elapsed(job.started_at) : esc((job.finished_at || job.created_at || "").slice(0, 16).replace("T", " "))}`;
  const r = job.result || {};
  out.textContent = job.status === "failed" ? (job.error || "") + "\n" + (r.traceback || "") : job.status === "done"
    ? `OK on ${r.device}: embedder load ${r.embedder_load_s}s, diarizer load ${r.diarizer_load_s}s (${r.diarization_pipeline_loaded}), 8 s test diarized in ${r.diarize_s}s (${r.realtime_factor}x realtime), ${r.n_speakers} speakers / ${r.n_turns} turns` : "";
}
$("#btn-selftest").onclick = async () => { try { await api("/api/selftest", { method: "POST" }); toast("Self-test queued (first run downloads models)"); renderSelftest(); } catch (e) { onErr(e); } };
$("#import-gallery").addEventListener("change", async () => {
  const f = $("#import-gallery").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f);
  try { const r = await fetch("/api/import/gallery", { method: "POST", body: fd }); const j = await r.json(); if (!r.ok) throw new Error(j.detail);
    $("#import-msg").textContent = `imported: ${j.persons_created} new people, ${j.persons_merged} merged, ${j.prints} prints`; loadStats(); } catch (e) { onErr(e); }
  $("#import-gallery").value = "";
});
$("#btn-save-settings").onclick = async (e) => {
  e.preventDefault();
  const body = {}; $$("#settings-form [name]").forEach((i) => { body[i.name] = i.type === "checkbox" ? i.checked : i.value; });
  try { const j = await api("/api/settings", { method: "PUT", body }); SETTINGS = j.values; $("#settings-msg").textContent = "Saved."; toast("Settings saved"); loadStats(); } catch (er) { onErr(er); }
};

/* ---------------- boot & polling ---------------- */
(async function boot() {
  const j = await api("/api/settings").catch(() => ({ values: {}, specs: {} })); SETTINGS = j.values; SPECS = j.specs;
  $("#inc-match").checked = !!SETTINGS.incognito_default; $("#inc-bulk").checked = !!SETTINGS.incognito_default;
  loadNames();
  showTab(location.hash.replace("#", "") || "overview");
  loadStats();
  let lastBusy = 0;
  setInterval(async () => {
    const busy = await loadStats(); updateUploadRows();
    if (busy || lastBusy) { if (STATE.tab === "settings") renderSelftest(); else if (!document.activeElement?.matches("input,select,textarea")) refreshTab(); }
    lastBusy = busy;
  }, 3000);
})();
