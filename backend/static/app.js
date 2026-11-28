/* AutoClipper — logic UI (vanilla JS, polling job status tiap 1s)
   Alur: link → analisis AI (rekomendasi momen + preview video) → pilih momen
   → render klip → hasil dengan caption per platform */
"use strict";

const $ = (sel) => document.querySelector(sel);

const state = {
  preview: null,        // metadata video setelah /api/preview
  uploadId: null,       // kalau sumber = file upload
  analyzeId: null,      // job id fase analisis
  jobId: null,          // job id fase render
  pollTimer: null,
  pollTarget: null,     // "analyze" | "render"
  lastLogLen: 0,
  moments: [],          // rekomendasi dari fase analisis
  duration: 30,         // dari tombol durasi
  subtitleStyle: "karaoke",
};

// contoh teks subtitle per style untuk kotak preview
const SUB_PREVIEWS = {
  karaoke:
    '<span class="word sung">NEVER</span> <span class="word sung">GONNA</span> ' +
    '<span class="word active">GIVE</span> <span class="word">YOU</span> <span class="word">UP</span>',
  klasik: '<span class="word">NEVER GONNA GIVE YOU UP</span>',
  box: '<span class="word box-prev">NEVER GONNA GIVE YOU UP</span>',
  glow: '<span class="word glow-prev">NEVER GONNA</span>',
  word: '<span class="word big-prev">GIVE</span>',
};

// ------------------------------------------------------------- helpers
function fmtDur(sec) {
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}j ${m}m ${s}s` : `${m}m ${s}s`;
}

function showError(msg, el = "#global-error") {
  const e = $(el);
  e.textContent = msg;
  e.classList.remove("hidden");
}

function hideError(el = "#global-error") {
  $(el).classList.add("hidden");
}

async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
  } catch (err) {
    // fetch gagal total: server mati/restart/network lokal bermasalah
    throw new Error("Server tidak merespons — pastikan aplikasi masih berjalan "
      + "(python run.py), tunggu beberapa detik, lalu coba lagi.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ------------------------------------------------------------- health
async function checkHealth() {
  const badge = $("#health-badge");
  try {
    const h = await api("/api/health");
    if (h.ok) {
      badge.textContent = h.api_key_set ? "● AI siap" : "● mode heuristik";
      badge.className = "badge ok";
    } else {
      badge.textContent = "● komponen kurang";
      badge.className = "badge bad";
      badge.title = `ffmpeg:${h.ffmpeg} ffprobe:${h.ffprobe} whisper:${h.whisper} yt-dlp:${h.yt_dlp}`;
    }
  } catch {
    badge.textContent = "● server tidak merespons";
    badge.className = "badge bad";
  }
}

// ------------------------------------------------------------- preview
$("#btn-preview").addEventListener("click", previewUrl);
$("#url-input").addEventListener("keydown", (e) => { if (e.key === "Enter") previewUrl(); });

async function previewUrl() {
  const url = $("#url-input").value.trim();
  hideError("#url-error");
  if (!url) { showError("Masukkan link YouTube dulu ya", "#url-error"); return; }

  const btn = $("#btn-preview");
  btn.disabled = true; btn.textContent = "Memeriksa…";
  try {
    const meta = await api("/api/preview", { method: "POST", body: JSON.stringify({ url }) });
    state.preview = meta;
    $("#preview-thumb").src = meta.thumbnail;
    $("#preview-title").textContent = meta.title;
    $("#preview-channel").textContent = meta.channel;
    $("#preview-duration").textContent = `⏱ ${fmtDur(meta.duration)}`;
    $("#preview-card").classList.remove("hidden");
    $("#card-options").classList.remove("hidden");
    $("#card-options").scrollIntoView({ behavior: "smooth", block: "center" });
    if (meta.duration < 30) {
      showError("Video terlalu pendek (minimal 30 detik) untuk di-auto-clip", "#url-error");
    }
  } catch (err) {
    state.preview = null;
    $("#preview-card").classList.add("hidden");
    $("#card-options").classList.add("hidden");
    showError(err.message, "#url-error");
  } finally {
    btn.disabled = false; btn.textContent = "Periksa";
  }
}

// ------------------------------------------------------------- options
// chip styling: tandai .checked sesuai input
document.addEventListener("change", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  if (e.target.type === "checkbox") chip.classList.toggle("checked", e.target.checked);
  else if (e.target.type === "radio") {
    document.querySelectorAll(`input[name="${e.target.name}"]`)
      .forEach((r) => r.closest(".chip").classList.toggle("checked", r.checked));
  }
});

$("#clip-count").addEventListener("input", (e) => $("#val-count").textContent = e.target.value);

// tombol durasi 15/30/60
document.querySelectorAll("#dur-btns .dur").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#dur-btns .dur").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.duration = parseInt(btn.dataset.dur, 10);
  });
});

// subtitle off → sembunyikan pilihan style + preview
$("#sub-toggle").addEventListener("change", (e) => {
  $("#sub-style-row").classList.toggle("hidden", !e.target.checked);
});

// tombol style subtitle + kotak preview
function setSubStyle(style) {
  state.subtitleStyle = style;
  document.querySelectorAll("#sub-style-btns .dur")
    .forEach((b) => b.classList.toggle("active", b.dataset.style === style));
  $("#sub-preview").className = `sub-preview ${style}`;
  $("#sub-preview-text").innerHTML = SUB_PREVIEWS[style];
}
document.querySelectorAll("#sub-style-btns .dur").forEach((btn) => {
  btn.addEventListener("click", () => setSubStyle(btn.dataset.style));
});

// ------------------------------------------------------------- PiP player
// klik video preview di kartu momen → popup melayang ala picture-in-picture.
// Tutup popup = video benar-benar berhenti (pause + reset + lepas src).
function openPip(src, title) {
  const player = $("#pip-player");
  const video = $("#pip-video");
  $("#pip-title").textContent = title || "Preview momen";
  video.src = src;
  player.classList.remove("hidden");
  video.play().catch(() => {}); // autoplay bisa diblokir browser — abaikan
}

function closePip() {
  const player = $("#pip-player");
  const video = $("#pip-video");
  video.pause();
  video.removeAttribute("src");
  video.load();                 // benar-benar stop, bukan play di belakang
  player.classList.add("hidden");
}

$("#pip-close").addEventListener("click", closePip);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#pip-player").classList.contains("hidden")) closePip();
});

// ------------------------------------------------------------- upload video
$("#btn-upload").addEventListener("click", () => $("#file-input").click());

$("#file-input").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  hideError("#url-error");
  $("#upload-progress").classList.remove("hidden");
  $("#btn-upload").disabled = true;

  const xhr = new XMLHttpRequest();
  const form = new FormData();
  form.append("file", file);
  xhr.open("POST", "/api/upload");
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) {
      const pct = Math.round(ev.loaded / ev.total * 100);
      $("#upload-fill").style.width = pct + "%";
      $("#upload-pct").textContent = pct + "%";
    }
  };
  xhr.onload = () => {
    $("#btn-upload").disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch {}
    if (xhr.status === 200) {
      state.uploadId = data.upload_id;
      state.preview = { title: data.filename, channel: "upload lokal", duration: data.duration };
      $("#upload-progress").classList.add("hidden");
      $("#preview-thumb").src = "";
      $("#preview-title").textContent = data.filename;
      $("#preview-channel").textContent = "📁 upload lokal · " + data.size_mb + " MB";
      $("#preview-duration").textContent = `⏱ ${fmtDur(data.duration)}`;
      $("#preview-card").classList.remove("hidden");
      $("#card-options").classList.remove("hidden");
      $("#card-options").scrollIntoView({ behavior: "smooth", block: "center" });
    } else {
      $("#upload-progress").classList.add("hidden");
      showError(data.detail || `Upload gagal (HTTP ${xhr.status})`, "#url-error");
    }
    $("#file-input").value = "";
  };
  xhr.onerror = () => {
    $("#btn-upload").disabled = false;
    $("#upload-progress").classList.add("hidden");
    showError("Upload gagal — server tidak merespons", "#url-error");
  };
  xhr.send(form);
});

// ------------------------------------------------------------- fase 1: analisis
$("#btn-analyze").addEventListener("click", startAnalyze);

function collectOptions() {
  return {
    platforms: [...document.querySelectorAll("#platform-chips input:checked")].map((c) => c.value),
    ratio: document.querySelector("input[name=ratio]:checked").value,
    layout: document.querySelector("input[name=layout]:checked").value,
    clip_mode: document.querySelector("input[name=clipmode]:checked").value,
    resolution: document.querySelector("input[name=resolution]:checked").value,
    clip_count: parseInt($("#clip-count").value, 10),
    clip_duration: state.duration,
    subtitles: $("#sub-toggle").checked,
    subtitle_style: state.subtitleStyle,
  };
}

function setAnalyzeButton(text, disabled) {
  const btn = $("#btn-analyze");
  btn.textContent = text;
  btn.disabled = disabled;
}

async function startAnalyze() {
  if (!state.preview) return;
  hideError();
  const opts = collectOptions();
  if (!opts.platforms.length) { showError("Pilih minimal satu platform"); return; }

  setAnalyzeButton("Menganalisis…", true);
  // tutup panel lama kalau analisis ulang
  $("#card-moments").classList.add("hidden");
  $("#card-results").classList.add("hidden");

  const source = state.uploadId
    ? { upload_id: state.uploadId }
    : { url: $("#url-input").value.trim() };

  try {
    const { job_id } = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({ ...source, ...opts }),
    });
    state.analyzeId = job_id;
    state.pollTarget = "analyze";
    state.lastLogLen = 0;
    $("#progress-title").textContent = "Menganalisis video…";
    $("#card-progress").classList.remove("hidden");
    $("#progress-log").textContent = "";
    $("#card-progress").scrollIntoView({ behavior: "smooth" });
    pollStatus();
  } catch (err) {
    showError(err.message);
    setAnalyzeButton("🔍 Cari Momen Terbaik", false);
  }
}

function pollStatus() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    const target = state.pollTarget === "analyze" ? state.analyzeId : state.jobId;
    try {
      const job = await api(`/api/jobs/${target}`);
      renderProgress(job);
      if (job.status === "running" || job.status === "queued") pollStatus();
      else if (state.pollTarget === "analyze") onAnalyzeDone(job);
      else onRenderDone(job);
    } catch { pollStatus(); } // retry jaringan
  }, 1000);
}

function renderProgress(job) {
  $("#progress-fill").style.width = `${job.progress}%`;
  $("#progress-msg").textContent = `${job.stage_label} — ${job.message || ""} (${Math.round(job.progress)}%)`;
  if (job.logs && job.logs.length > state.lastLogLen) {
    const logEl = $("#progress-log");
    logEl.textContent = job.logs.slice(-30).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    state.lastLogLen = job.logs.length;
  }
}

function onAnalyzeDone(job) {
  if (job.status === "failed") {
    $("#progress-title").textContent = "❌ Gagal";
    $("#progress-msg").textContent = job.error || "Terjadi kesalahan";
    showError(job.error || "Analisis gagal");
    setAnalyzeButton("🔍 Cari Momen Terbaik", false);
    return;
  }
  state.moments = job.moments || [];
  setAnalyzeButton("🔍 Cari Momen Terbaik", false);
  renderMoments();
}

function renderMoments() {
  const list = $("#moments-list");
  list.innerHTML = "";
  const nSelect = parseInt($("#clip-count").value, 10);

  state.moments.forEach((m, idx) => {
    const dur = Math.round(m.end - m.start);
    const card = document.createElement("div");
    card.className = "moment-card" + (idx < nSelect ? " checked" : "");
    card.innerHTML = `
      <input type="checkbox" class="moment-check" ${idx < nSelect ? "checked" : ""}>
      <span class="moment-rank">${idx + 1}</span>
      <div class="moment-info">
        <h4>${escapeHtml(m.title)}</h4>
        ${m.hook ? `<p class="hook">${escapeHtml(m.hook)}</p>` : ""}
        <div class="meta">
          <span>⏱ ${Math.round(m.start)}s–${Math.round(m.end)}s (${dur} detik)</span>
          ${m.chunks && m.chunks.length > 1 ? `<span>🔗 ${m.chunks.length} potongan digabung</span>` : ""}
          <span class="moment-score">skor ${(m.score * 100).toFixed(0)}</span>
          <span>${m.source === "AI" ? "🤖 AI" : "📊 heuristik"}</span>
        </div>
        <p class="moment-hint">▶ klik preview untuk buka player — klik info/checkbox untuk memilih</p>
      </div>
      ${m.preview_url
        ? `<video class="moment-video" src="${m.preview_url}" preload="metadata" playsinline title="Klik untuk buka player"></video>`
        : ""}`;
    // klik preview kecil → buka PiP player (bukan play inline kecil)
    const mv = card.querySelector(".moment-video");
    if (mv) {
      mv.addEventListener("click", () => openPip(m.preview_url, `Momen ${idx + 1} — ${m.title}`));
    }
    const check = card.querySelector(".moment-check");
    // klik area info = toggle pilih (video biarkan fokus ke kontrolnya)
    card.querySelector(".moment-info").addEventListener("click", () => {
      check.checked = !check.checked;
      check.dispatchEvent(new Event("change", { bubbles: true }));
    });
    check.addEventListener("change", (e) => {
      card.classList.toggle("checked", e.target.checked);
      updateSelCount();
    });
    list.appendChild(card);
  });

  updateSelCount();
  $("#moments-source").textContent =
    state.moments.some((m) => m.source === "AI") ? "🤖 dari AI" : "📊 heuristik lokal";
  $("#card-moments").classList.remove("hidden");
  $("#card-moments").scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateSelCount() {
  const n = document.querySelectorAll(".moment-check:checked").length;
  $("#sel-count").textContent = n;
  $("#btn-render").disabled = n === 0;
}

// ------------------------------------------------------------- fase 2: render
$("#btn-render").addEventListener("click", startRender);

async function startRender() {
  hideError();
  // urutan kartu = urutan state.moments (dirender berurutan)
  const moments = [...document.querySelectorAll(".moment-card")]
    .map((c, i) => (c.querySelector(".moment-check").checked ? { ...state.moments[i] } : null))
    .filter(Boolean);
  if (!moments.length) { showError("Centang minimal satu momen"); return; }

  const opts = collectOptions();
  $("#btn-render").disabled = true;
  $("#btn-render").textContent = "Merender…";
  try {
    const { job_id } = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        analysis_id: state.analyzeId,
        moments,
        platforms: opts.platforms,
        resolution: opts.resolution,
        ratio: opts.ratio,
        layout: opts.layout,
        clip_mode: opts.clip_mode,
        subtitles: opts.subtitles,
        subtitle_style: opts.subtitle_style,
      }),
    });
    state.jobId = job_id;
    state.pollTarget = "render";
    state.lastLogLen = 0;
    $("#progress-title").textContent = "Merender klip…";
    $("#card-progress").classList.remove("hidden");
    $("#progress-log").textContent = "";
    $("#card-progress").scrollIntoView({ behavior: "smooth" });
    pollStatus();
  } catch (err) {
    showError(err.message);
    $("#btn-render").disabled = false;
    $("#btn-render").innerHTML = '✂️ Buat Klip Terpilih (<span id="sel-count">0</span>)';
    updateSelCount();
  }
}

function onRenderDone(job) {
  $("#btn-render").disabled = false;
  $("#btn-render").innerHTML = '✂️ Buat Klip Terpilih (<span id="sel-count">0</span>)';
  updateSelCount();
  if (job.status === "failed") {
    $("#progress-title").textContent = "❌ Gagal";
    $("#progress-msg").textContent = job.error || "Terjadi kesalahan";
    showError(job.error || "Render gagal");
    return;
  }
  $("#progress-title").textContent = "🎉 Selesai!";
  renderResults(job);
}

// ------------------------------------------------------------- results
function renderResults(job) {
  const grid = $("#results-grid");
  grid.innerHTML = "";

  job.clips.forEach((clip) => {
    const srcBadge = clip.source === "AI" ? "" : " heuristik";
    const card = document.createElement("div");
    card.className = "clip-card";
    // caption per platform (kalau >1 platform) — tiap baris bisa dicopy sendiri
    const caps = clip.captions && clip.captions.length ? clip.captions
      : [{ platform: clip.platforms?.[0] || "", text: clip.caption || "" }];
    const capsHtml = caps.length === 1
      ? `<div class="clip-caption">${escapeHtml(caps[0].text)}</div>`
      : `<div class="clip-captions">${caps.map((c, ci) => `
          <div class="cap-row">
            <span class="cap-plat">${escapeHtml(c.platform)}</span>
            <div class="cap-text">${escapeHtml(c.text)}</div>
            <button class="btn btn-ghost cap-copy" data-ci="${ci}">📋</button>
          </div>`).join("")}</div>`;
    card.innerHTML = `
      <video src="${clip.video_url}" controls preload="metadata" ${clip.thumb_url ? `poster="${clip.thumb_url}"` : ""}></video>
      <div class="clip-body">
        <span class="clip-badge${srcBadge}">${clip.source} · #${clip.index}</span>
        <h4>${escapeHtml(clip.title)}</h4>
        <div class="clip-meta">⏱ ${clip.duration}s · ${clip.start}s–${clip.end}s · ${clip.platforms.join(", ")}</div>
        ${capsHtml}
        <div class="clip-actions">
          <a class="btn btn-primary" href="${clip.video_url}" download>⬇️ Download</a>
          ${caps.length === 1 ? `<button class="btn btn-ghost btn-copy">📋 Caption</button>` : ""}
        </div>
      </div>`;
    card.querySelectorAll(".cap-copy").forEach((btn) => {
      btn.addEventListener("click", () => {
        navigator.clipboard.writeText(caps[parseInt(btn.dataset.ci, 10)].text).then(() => {
          btn.textContent = "✅";
          setTimeout(() => (btn.textContent = "📋"), 1500);
        });
      });
    });
    const singleCopy = card.querySelector(".btn-copy");
    if (singleCopy) {
      singleCopy.addEventListener("click", (e) => {
        navigator.clipboard.writeText(caps[0].text).then(() => {
          e.target.textContent = "✅ Tersalin";
          setTimeout(() => (e.target.textContent = "📋 Caption"), 1500);
        });
      });
    }
    grid.appendChild(card);
  });

  $("#card-results").classList.remove("hidden");
  $("#card-results").scrollIntoView({ behavior: "smooth" });
}

$("#btn-open-folder").addEventListener("click", async () => {
  try { await api(`/api/open-folder?job_id=${state.jobId}`, { method: "POST" }); }
  catch (err) { showError(err.message); }
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ------------------------------------------------------------- settings
$("#btn-settings").addEventListener("click", openSettings);

async function openSettings() {
  try {
    const s = await api("/api/settings");
    $("#set-api-key").placeholder = s.zai_api_key ? `tersimpan: ${s.zai_api_key}` : "kosongkan = mode heuristik lokal";
    $("#set-model").value = s.zai_model || "glm-4.5-flash";
    $("#set-whisper").value = s.whisper_model || "base";
    $("#set-encoder").value = s.render_encoder || "cpu";
    $("#set-cookies-browser").value = s.cookies_browser || "";
    $("#set-cookies-file").value = s.cookies_file || "";
  } catch (err) { showError(err.message); }
  $("#modal-settings").classList.remove("hidden");
}

$("#btn-settings-cancel").addEventListener("click", () => $("#modal-settings").classList.add("hidden"));

$("#btn-settings-save").addEventListener("click", async () => {
  const body = {
    zai_model: $("#set-model").value.trim() || "glm-4.5-flash",
    whisper_model: $("#set-whisper").value,
    render_encoder: $("#set-encoder").value,
    cookies_browser: $("#set-cookies-browser").value,
    cookies_file: $("#set-cookies-file").value.trim(),
  };
  const key = $("#set-api-key").value.trim();
  if (key) body.zai_api_key = key; // kosong = biarkan tersimpan
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    $("#modal-settings").classList.add("hidden");
    checkHealth();
  } catch (err) { showError(err.message); }
});

$("#modal-settings").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) $("#modal-settings").classList.add("hidden");
});

// ------------------------------------------------------------- channel monitor
async function loadChannels() {
  try {
    const data = await api("/api/channels");
    $("#ch-auto-toggle").checked = data.auto;
    const list = $("#ch-list");
    list.innerHTML = "";
    data.channels.forEach((ch) => {
      const row = document.createElement("div");
      row.className = "ch-row";
      const last = ch.last_check
        ? ` · cek terakhir ${new Date(ch.last_check * 1000).toLocaleTimeString("id", { hour: "2-digit", minute: "2-digit" })}`
        : "";
      row.innerHTML = `
        <span class="name">${escapeHtml(ch.name)}</span>
        <span class="meta">${last}</span>
        <button class="btn btn-ghost btn-sm ch-check" data-id="${ch.id}">🔍 Cek</button>
        <button class="btn btn-ghost btn-sm ch-del" data-id="${ch.id}">🗑️</button>`;
      row.querySelector(".ch-check").addEventListener("click", () => checkChannel(ch.id, ch.name));
      row.querySelector(".ch-del").addEventListener("click", async () => {
        await api(`/api/channels/${ch.id}`, { method: "DELETE" });
        loadChannels();
      });
      list.appendChild(row);
    });
  } catch (err) { /* server offline saat load awal — abaikan */ }
}

async function checkChannel(id, name) {
  try {
    const r = await api(`/api/channels/${id}/check`, { method: "POST" });
    if (r.found === 0) showError(`Tidak ada video baru di "${name}"`, "#global-error");
    else showError(`✅ ${r.found} video baru ditemukan — ${r.processed} langsung diproses (lihat Riwayat job)`, "#global-error");
    loadChannels();
    pollHistory();
  } catch (err) { showError(err.message); }
  setTimeout(hideError, 6000);
}

$("#btn-ch-add").addEventListener("click", async () => {
  const url = $("#ch-url").value.trim();
  hideError();
  if (!url) return;
  try {
    await api("/api/channels", { method: "POST", body: JSON.stringify({ url }) });
    $("#ch-url").value = "";
    loadChannels();
  } catch (err) { showError(err.message); setTimeout(hideError, 6000); }
});

$("#ch-auto-toggle").addEventListener("change", async (e) => {
  try {
    await api("/api/channels/auto", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) });
  } catch (err) { e.target.checked = !e.target.checked; showError(err.message); }
});

$("#btn-ch-refresh").addEventListener("click", async () => {
  $("#btn-ch-refresh").disabled = true;
  try {
    const results = await api("/api/channels/check-all", { method: "POST" });
    const total = results.reduce((s, r) => s + r.found, 0);
    showError(total ? `✅ ${total} video baru ditemukan — proses jalan (lihat Riwayat job)` : "Tidak ada video baru di semua channel",
      "#global-error");
    loadChannels();
    pollHistory();
  } catch (err) { showError(err.message); }
  $("#btn-ch-refresh").disabled = false;
  setTimeout(hideError, 6000);
});

// ------------------------------------------------------------- riwayat job
function statusLabel(j) {
  const s = j.status === "running" ? j.stage_label || "berjalan" : j.status;
  return { done: "✅ selesai", failed: "❌ gagal", queued: "⏳ antre", running: `⏱ ${s}` }[j.status] || s;
}

function renderHistory(jobs) {
  const list = $("#history-list");
  list.innerHTML = "";
  if (!jobs.length) {
    list.innerHTML = '<p class="hint">Belum ada job. Mulai dari langkah 1, atau via Channel monitor.</p>';
    return;
  }
  jobs.forEach((j) => {
    const row = document.createElement("div");
    row.className = "hist-row";
    const title = (j.video && j.video.title) || j.id;
    const canOpen = j.status === "done" && ((j.clips && j.clips.length) || (j.moments && j.moments.length));
    row.innerHTML = `
      <span class="hist-status ${j.status}">${statusLabel(j)}</span>
      <span class="hist-title" title="${escapeHtml(title)}">${escapeHtml(title)}</span>
      <div class="hist-bar"><div style="width:${Math.round(j.progress)}%"></div></div>
      ${canOpen ? '<button class="btn btn-ghost btn-sm hist-open">📂 Hasil</button>' : ""}`;
    if (canOpen) {
      row.querySelector(".hist-open").addEventListener("click", () => openJobResult(j.id));
    }
    list.appendChild(row);
  });
}

async function openJobResult(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.clips && job.clips.length) {
      state.jobId = jobId;
      $("#progress-title").textContent = "🎉 Selesai!";
      renderResults(job);
    } else if (job.moments && job.moments.length) {
      state.analyzeId = jobId;
      state.moments = job.moments;
      renderMoments();
    }
  } catch (err) { showError(err.message); }
}

function pollHistory() {
  api("/api/jobs").then(renderHistory).catch(() => {});
  setTimeout(pollHistory, 5000);
}

// ------------------------------------------------------------- init
setSubStyle("karaoke");
checkHealth();
loadChannels();
pollHistory();
