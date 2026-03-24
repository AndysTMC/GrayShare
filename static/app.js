const modeIdleBtn = document.getElementById("mode-idle");
const modeShareBtn = document.getElementById("mode-share");
const sharePanel = document.getElementById("share-panel");
const receivePanel = document.getElementById("receive-panel");
const shareForm = document.getElementById("share-form");
const shareStatus = document.getElementById("share-status");
const shareSubmit = document.getElementById("share-submit");
const uploadProgressWrap = document.getElementById("upload-progress-wrap");
const uploadProgressFill = document.getElementById("upload-progress-fill");
const uploadProgressText = document.getElementById("upload-progress-text");
const uploadProgressBar = document.getElementById("upload-progress-bar");
const stopShareBtn = document.getElementById("stop-share");
const sharesList = document.getElementById("shares-list");
const receiveOverlay = document.getElementById("receive-overlay");
const receiveOverlayTitle = document.getElementById("receive-overlay-title");
const receiveOverlayFile = document.getElementById("receive-overlay-file");
const receiveOverlayStatus = document.getElementById("receive-overlay-status");
const receiveProgressBar = document.getElementById("receive-progress-bar");
const receiveProgressFill = document.getElementById("receive-progress-fill");
const shareFileInput = document.getElementById("share-file");
const shareFileLabel = document.getElementById("share-file-label");
const themeToggleBtn = document.getElementById("theme-toggle");
const serverEndpointEl = document.getElementById("server-endpoint");
const showQrBtn = document.getElementById("show-qr");
const qrWrap = document.getElementById("qr-wrap");
const qrCodeEl = document.getElementById("qr-code");
const qrTextEl = document.getElementById("qr-text");

let localSharerId = null;
/** Skip list refresh while downloading so the UI isn’t torn down mid-transfer. */
let receiveInProgress = false;
let receiveHealthTimerId = null;
let networkRefreshTimerId = null;
let qrVisible = false;
let qrInstance = null;
let shareHeartbeatTimerId = null;
const clientLogRecent = new Map();
let serverEndpointUrl = "";
let lastQrValue = "";
let networkInfoInFlight = false;
let refreshSharesInFlight = false;
let receiveHealthInFlight = false;
let shareHeartbeatInFlight = false;
let lastSharesRenderKey = "";
let activePointerCount = 0;
let deferUiRefreshUntil = 0;

const DOWNLOAD_RETRY_LIMIT = 5;
const DOWNLOAD_BASE_BACKOFF_MS = 400;
const DOWNLOAD_TIMEOUT_MS = 20000;
const HEALTH_PING_INTERVAL_MS = 5000;
const HEALTH_PING_TIMEOUT_MS = 3000;
const NETWORK_REFRESH_MS = 10000;
const SHARE_HEARTBEAT_MS = 5000;

function noteUiInteraction(delayMs = 450) {
  deferUiRefreshUntil = Math.max(deferUiRefreshUntil, Date.now() + delayMs);
}

function isUiInteractionActive() {
  return activePointerCount > 0 || Date.now() < deferUiRefreshUntil;
}

window.addEventListener("pointerdown", () => {
  activePointerCount += 1;
  noteUiInteraction(900);
}, true);

function releaseUiInteraction(delayMs = 450) {
  activePointerCount = Math.max(0, activePointerCount - 1);
  noteUiInteraction(delayMs);
}

window.addEventListener("pointerup", () => releaseUiInteraction(), true);
window.addEventListener("pointercancel", () => releaseUiInteraction(650), true);
window.addEventListener("blur", () => {
  activePointerCount = 0;
  noteUiInteraction(250);
}, true);

function formatBytes(n) {
  if (n == null || Number.isNaN(n) || n < 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let u = 0;
  while (v >= 1024 && u < units.length - 1) {
    v /= 1024;
    u += 1;
  }
  const decimals = u <= 1 ? 0 : u >= 3 ? 2 : 1;
  return `${v.toFixed(decimals)} ${units[u]}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function shouldRetryStatus(status) {
  return status === 408 || status === 425 || status === 429 || status >= 500;
}

function computeBackoff(attempt) {
  return Math.min(8000, DOWNLOAD_BASE_BACKOFF_MS * 2 ** Math.max(0, attempt - 1));
}

async function parseResponseError(res, fallbackMessage) {
  const ct = res.headers.get("Content-Type") || "";
  if (ct.includes("application/json")) {
    const data = await res.json().catch(() => null);
    if (data) return data;
  }
  const text = await res.text().catch(() => "");
  if (text) return { detail: text };
  return { detail: fallbackMessage };
}

async function fetchWithTimeout(url, options = {}, timeoutMs = DOWNLOAD_TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } finally {
    clearTimeout(timer);
  }
}

/** Chunked transfer tuning (must match server CHUNK_MIN/MAX defaults). */
const CHUNK_MIN_BYTES = 256 * 1024;
const CHUNK_MAX_BYTES = 64 * 1024 * 1024;

/** ~1 second of data per chunk at measured upload speed (your MB/s ≈ MB chunk idea). */
function clampChunkBytes(speedBps) {
  return Math.min(CHUNK_MAX_BYTES, Math.max(CHUNK_MIN_BYTES, Math.round(speedBps)));
}

/** Parallel workers: budget ~2× chunk size from reported device RAM (Chrome `deviceMemory` in GB). */
function computeParallelWorkers(chunkBytes) {
  const ramGB = navigator.deviceMemory || 4;
  const ramBytes = ramGB * 1024 ** 3;
  const est = Math.floor(ramBytes / (2 * Math.max(chunkBytes, 1)));
  return Math.max(1, Math.min(16, est));
}

async function measureUploadSpeed() {
  const n = 256 * 1024;
  const entropyChunk = 65536;
  const buf = new Uint8Array(n);
  for (let i = 0; i < n; i += entropyChunk) {
    crypto.getRandomValues(buf.subarray(i, i + entropyChunk));
  }
  const blob = new Blob([buf]);
  const fd = new FormData();
  fd.append("probe", blob, "probe.bin");
  const t0 = performance.now();
  await fetch("/api/telemetry/upload-probe", { method: "POST", body: fd });
  const secs = (performance.now() - t0) / 1000;
  return n / Math.max(secs, 0.001);
}

async function uploadFileInChunks(file, displayName, passcode, onChunkProgress, manualChunkBytes = 0, manualWorkers = 0) {
  const speed = await measureUploadSpeed();
  const chunkBytes = manualChunkBytes > 0 ? manualChunkBytes * 1024 * 1024 : clampChunkBytes(speed);
  const workers = manualWorkers > 0 ? manualWorkers : computeParallelWorkers(chunkBytes);

  const initRes = await fetch("/api/share/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      display_name: displayName,
      filename: file.name,
      content_type: file.type || "application/octet-stream",
      total_size: file.size,
      chunk_size: chunkBytes,
      passcode: passcode || null,
    }),
  });
  if (!initRes.ok) {
    const err = await initRes.json().catch(() => ({}));
    throw err;
  }
  const initData = await initRes.json();
  const { sharer_id, total_chunks: serverChunks } = initData;
  const nChunks = serverChunks ?? Math.max(1, Math.ceil(file.size / chunkBytes));

  let done = 0;
  for (let i = 0; i < nChunks; i += workers) {
    const end = Math.min(i + workers, nChunks);
    const batch = [];
    for (let j = i; j < end; j++) {
      const start = j * chunkBytes;
      const sliceEnd = Math.min(start + chunkBytes, file.size);
      const blob = file.slice(start, sliceEnd);
      const fd = new FormData();
      fd.append("chunk_index", String(j));
      fd.append("file", blob, "chunk.bin");
      batch.push(
        fetch(`/api/share/${sharer_id}/chunk`, { method: "POST", body: fd }).then(async (res) => {
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw err;
          }
          done += 1;
          onChunkProgress?.(done, nChunks, chunkBytes, workers);
          return res.json();
        }),
      );
    }
    await Promise.all(batch);
  }
  onChunkProgress?.(nChunks, nChunks, chunkBytes, workers);
  const finalRes = await fetch(`/api/share/${sharer_id}/finalize`, { method: "POST" });
  if (!finalRes.ok) {
    const err = await finalRes.json().catch(() => ({}));
    throw err;
  }
  const finalData = await finalRes.json().catch(() => ({}));
  return {
    sharer_id,
    url: finalData?.url || initData?.url || "",
    endpoint: finalData?.endpoint || initData?.endpoint || "",
    ...finalData,
  };
}

async function downloadFileAdaptive(share, passcode, onProgress) {
  const pq = encodeURIComponent(passcode || "");
  let infoRes = null;
  for (let attempt = 1; attempt <= DOWNLOAD_RETRY_LIMIT; attempt += 1) {
    try {
      infoRes = await fetchWithTimeout(
        `/api/receive/${share.sharer_id}/info?passcode=${pq}`,
        { method: "GET" },
        DOWNLOAD_TIMEOUT_MS,
      );
      if (!infoRes.ok && shouldRetryStatus(infoRes.status) && attempt < DOWNLOAD_RETRY_LIMIT) {
        await sleep(computeBackoff(attempt));
        continue;
      }
      break;
    } catch {
      if (attempt >= DOWNLOAD_RETRY_LIMIT) {
        throw new Error("Unable to connect to sender.");
      }
      await sleep(computeBackoff(attempt));
    }
  }
  if (!infoRes || !infoRes.ok) {
    const err = infoRes
      ? await parseResponseError(infoRes, "Unable to get receive info")
      : new Error("Unable to get receive info");
    throw err;
  }
  const info = await infoRes.json();

  if (info.chunk_count === 1) {
    const body = new FormData();
    body.append("passcode", passcode);
    return postFormWithDownloadProgressRetry(`/api/receive/${share.sharer_id}`, body, onProgress);
  }

  let workers = computeParallelWorkers(info.chunk_size);
  const parts = new Array(info.chunk_count);
  let loaded = 0;
  const totalBytes = info.size_bytes || 1;
  const pending = Array.from({ length: info.chunk_count }, (_, i) => i);

  while (pending.length) {
    const batch = pending.splice(0, workers);
    const results = await Promise.allSettled(
      batch.map((j) =>
        downloadChunkWithRetry(`/api/receive/${share.sharer_id}/chunk/${j}?passcode=${pq}`).then((b) => {
          parts[j] = b;
          loaded += b.size;
          onProgress?.(loaded / totalBytes, loaded, totalBytes);
        }),
      ),
    );

    const failed = [];
    for (let i = 0; i < results.length; i += 1) {
      if (results[i].status === "rejected") {
        failed.push(batch[i]);
      }
    }

    if (failed.length) {
      pending.push(...failed);
      if (workers > 1) {
        workers = Math.max(1, Math.floor(workers / 2));
      }
      receiveOverlayStatus.textContent = `Packet loss detected. Retrying ${failed.length} chunk(s) with ${workers} stream(s)…`;
      await sleep(DOWNLOAD_BASE_BACKOFF_MS);
    }
  }
  return new Blob(parts);
}

function setShareStatus(message, kind = "info") {
  shareStatus.textContent = message;
  shareStatus.classList.remove("hidden", "error", "success");
  if (kind === "error") {
    shareStatus.classList.add("error");
  } else if (kind === "success") {
    shareStatus.classList.add("success");
  }
}

function resetShareUi() {
  shareForm.reset();
  if (shareFileLabel) {
    shareFileLabel.textContent = "No file selected";
    shareFileLabel.classList.remove("form__file-name--selected");
  }
  uploadProgressBar.classList.remove("indeterminate");
  uploadProgressWrap.classList.add("hidden");
  uploadProgressFill.style.width = "0%";
  uploadProgressText.textContent = "";
  shareSubmit.disabled = false;
  shareSubmit.classList.remove("hidden");
  stopShareBtn.classList.add("hidden");
  shareStatus.classList.add("hidden");
}

function setMode(mode) {
  const sharing = mode === "share";
  sharePanel.classList.toggle("hidden", !sharing);
  receivePanel.classList.toggle("hidden", sharing);
  modeShareBtn.classList.toggle("active", sharing);
  modeIdleBtn.classList.toggle("active", !sharing);
  modeShareBtn.setAttribute("aria-pressed", sharing ? "true" : "false");
  modeIdleBtn.setAttribute("aria-pressed", sharing ? "false" : "true");
}

modeIdleBtn.addEventListener("click", () => setMode("idle"));
modeShareBtn.addEventListener("click", () => setMode("share"));

if (shareFileInput && shareFileLabel) {
  shareFileInput.addEventListener("change", () => {
    const f = shareFileInput.files?.[0];
    shareFileLabel.textContent = f ? f.name : "No file selected";
    shareFileLabel.classList.toggle("form__file-name--selected", Boolean(f));
  });
  const fileShell = shareFileInput.closest(".form__file-shell");
  if (fileShell) {
    ["dragenter", "dragover"].forEach((evt) => {
      fileShell.addEventListener(evt, (e) => {
        e.preventDefault();
        fileShell.classList.add("drag-over");
      });
    });
    ["dragleave", "drop"].forEach((evt) => {
      fileShell.addEventListener(evt, (e) => {
        e.preventDefault();
        fileShell.classList.remove("drag-over");
      });
    });
    fileShell.addEventListener("drop", (e) => {
      const dt = e.dataTransfer;
      if (!dt || !dt.files || !dt.files.length) return;
      shareFileInput.files = dt.files;
      const f = dt.files[0];
      shareFileLabel.textContent = f ? f.name : "No file selected";
      shareFileLabel.classList.toggle("form__file-name--selected", Boolean(f));
    });
  }
}

const viewTransfer = document.getElementById("view-transfer");
const viewHistory = document.getElementById("view-history");
const viewSettings = document.getElementById("view-settings");
const navHistory = document.getElementById("nav-history");
const portSettingsBlock = document.getElementById("server-port-block");
const settingRefreshBlock = document.getElementById("setting-refresh")?.closest(".settings-block");
const clearDataBlock = document.getElementById("clear-data")?.closest(".settings-block");
const settingPortInput = document.getElementById("setting-port");
const settingPortStatus = document.getElementById("setting-port-status");
const saveCloseAppBtn = document.getElementById("save-close-app");

function applyClientVisibility() {
  if (settingRefreshBlock) settingRefreshBlock.remove();
  if (saveCloseAppBtn) saveCloseAppBtn.remove();
  if (isLoopbackOrigin()) return;
  if (viewHistory) viewHistory.classList.add("hidden");
  if (navHistory) navHistory.classList.add("hidden");
  if (portSettingsBlock) portSettingsBlock.classList.add("hidden");
  if (clearDataBlock) clearDataBlock.classList.add("hidden");
}

function showView(name) {
  if (name === "history" && !isLoopbackOrigin()) {
    name = "transfer";
  }
  const map = {
    transfer: viewTransfer,
    history: viewHistory,
    settings: viewSettings,
  };
  Object.entries(map).forEach(([key, el]) => {
    if (el) el.classList.toggle("hidden", key !== name);
  });
  document.querySelectorAll(".nav-item[data-view]").forEach((n) => {
    n.classList.toggle("nav-item--active", n.dataset.view === name);
  });
  if (name === "history") loadActivityList();
  if (name === "settings") loadSettingsPanel();
}

document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
  btn.addEventListener("click", () => showView(btn.dataset.view));
});

const DEFAULT_CLIENT_SETTINGS = {
  display_name: "",
  chunk_mb: 0,
  threads: 0,
  refresh_sec: 5,
  theme: "light",
};
const CLIENT_SETTINGS_STORAGE_KEY = "grayshare.clientSettings";
let clientSettings = { ...DEFAULT_CLIENT_SETTINGS };
let desktopConfig = {
  configured_port: 0,
  current_port: 0,
  close_supported: false,
};
let portCheckTimerId = null;
let portCheckRequestId = 0;

function clampInt(value, min, max, fallback) {
  const n = parseInt(value, 10);
  if (Number.isNaN(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function normalizeClientSettings(raw = {}) {
  return {
    display_name: typeof raw.display_name === "string" ? raw.display_name.trim() : "",
    chunk_mb: clampInt(raw.chunk_mb ?? 0, 0, 256, 0),
    threads: clampInt(raw.threads ?? 0, 0, 16, 0),
    refresh_sec: 5,
    theme: raw.theme === "dark" ? "dark" : "light",
  };
}

async function loadClientSettings() {
  if (isLoopbackOrigin()) {
    try {
      const res = await fetchWithTimeout("/api/settings/client", { method: "GET" }, HEALTH_PING_TIMEOUT_MS);
      if (!res.ok) {
        throw new Error("failed");
      }
      clientSettings = normalizeClientSettings(await res.json());
      return;
    } catch {
      clientSettings = { ...DEFAULT_CLIENT_SETTINGS };
      return;
    }
  }

  try {
    const raw = window.localStorage.getItem(CLIENT_SETTINGS_STORAGE_KEY);
    clientSettings = normalizeClientSettings(raw ? JSON.parse(raw) : DEFAULT_CLIENT_SETTINGS);
  } catch {
    clientSettings = { ...DEFAULT_CLIENT_SETTINGS };
  }
}

async function saveClientSettings() {
  clientSettings = normalizeClientSettings(clientSettings);
  if (isLoopbackOrigin()) {
    const res = await fetch("/api/settings/client", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(clientSettings),
    });
    if (!res.ok) {
      throw await parseResponseError(res, "Unable to save settings.");
    }
    clientSettings = normalizeClientSettings(await res.json());
    return clientSettings;
  }
  window.localStorage.setItem(
    CLIENT_SETTINGS_STORAGE_KEY,
    JSON.stringify(clientSettings),
  );
  return clientSettings;
}

function getDisplayName() {
  return clientSettings.display_name || "";
}

function getChunkMb() {
  return clientSettings.chunk_mb || 0;
}

function getThreads() {
  return clientSettings.threads || 0;
}

function getRefreshSec() {
  return 5;
}

let pollTimerId = null;
function configureSharePolling() {
  if (pollTimerId) clearTimeout(pollTimerId);
  pollTimerId = setTimeout(async function runSharePoll() {
    await refreshShares({ allowDefer: true });
    pollTimerId = setTimeout(runSharePoll, getRefreshSec() * 1000);
  }, getRefreshSec() * 1000);
}

function getTheme() {
  return clientSettings.theme || "light";
}

function applyTheme(theme) {
  const dark = theme === "dark";
  document.body.classList.toggle("theme-dark", dark);
  if (themeToggleBtn) {
    themeToggleBtn.textContent = dark ? "Light" : "Dark";
  }
}

function setupTheme() {
  applyTheme(getTheme());
  if (!themeToggleBtn) return;
  themeToggleBtn.addEventListener("click", async () => {
    const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
    clientSettings.theme = next;
    applyTheme(next);
    try {
      await saveClientSettings();
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
    }
  });
}

function sanitizeServerUrl(value) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) return "";
  if (/^undefined$/i.test(text) || /^null$/i.test(text)) return "";
  if (text.includes("undefined") || text.includes("null")) return "";
  try {
    return new URL(text, window.location.origin).toString();
  } catch {
    return "";
  }
}

function resolveServerUrl(data = {}) {
  const directUrl = sanitizeServerUrl(data?.url);
  if (directUrl) return directUrl;

  const ip = typeof data?.ip === "string" ? data.ip.trim() : "";
  const port = Number.parseInt(data?.port, 10);
  if (ip && Number.isInteger(port) && port > 0 && port <= 65535) {
    const base = `${window.location.protocol}//${ip}:${port}/`;
    return sanitizeServerUrl(base);
  }

  return sanitizeServerUrl(window.location.origin ? `${window.location.origin}/` : "");
}

function updateServerEndpoint(url) {
  if (!serverEndpointEl) return;
  const nextUrl = sanitizeServerUrl(url);
  if (nextUrl === serverEndpointUrl) {
    if (qrVisible) {
      renderQr(nextUrl);
    }
    return;
  }
  serverEndpointUrl = nextUrl;
  serverEndpointEl.textContent = `URL: ${nextUrl || "unavailable"}`;
  serverEndpointEl.title = nextUrl || "unavailable";
  if (qrVisible) {
    renderQr(nextUrl);
  } else if (!nextUrl) {
    lastQrValue = "";
  }
}

async function loadNetworkInfo() {
  if (!serverEndpointEl) return;
  if (networkInfoInFlight) return;
  if (document.hidden) return;
  if (isUiInteractionActive()) {
    noteUiInteraction(250);
    return;
  }
  networkInfoInFlight = true;
  try {
    const res = await fetchWithTimeout("/api/network/info", { method: "GET" }, HEALTH_PING_TIMEOUT_MS);
    if (!res.ok) {
      throw new Error("failed");
    }
    const data = await res.json();
    updateServerEndpoint(resolveServerUrl(data));
  } catch {
    if (!serverEndpointUrl) {
      updateServerEndpoint("");
    }
  } finally {
    networkInfoInFlight = false;
  }
}

function renderQr(url) {
  if (!qrCodeEl || !qrTextEl) return;
  const value = url || "";
  if (value === lastQrValue) return;
  lastQrValue = value;
  qrCodeEl.innerHTML = "";
  qrTextEl.textContent = value;
  if (!value || !window.QRCode) {
    qrInstance = null;
    return;
  }
  qrInstance = new window.QRCode(qrCodeEl, {
    text: value,
    width: 360,
    height: 360,
    colorDark: "#000000",
    colorLight: "#ffffff",
    correctLevel: window.QRCode.CorrectLevel.H,
  });
}

function setupNetworkInfo() {
  void loadNetworkInfo();
  if (networkRefreshTimerId) clearTimeout(networkRefreshTimerId);
  networkRefreshTimerId = setTimeout(async function runNetworkRefresh() {
    await loadNetworkInfo();
    networkRefreshTimerId = setTimeout(runNetworkRefresh, NETWORK_REFRESH_MS);
  }, NETWORK_REFRESH_MS);
  if (!showQrBtn || !qrWrap) return;
  showQrBtn.addEventListener("click", () => {
    qrVisible = !qrVisible;
    qrWrap.classList.toggle("hidden", !qrVisible);
    showQrBtn.textContent = qrVisible ? "Hide QR" : "Show QR";
    if (qrVisible) {
      renderQr(serverEndpointUrl);
    }
  });
}

function formatTs(iso) {
  try {
    return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return iso;
  }
}

function activityKindClass(kind) {
  if (kind === "share_start") return "activity-kind activity-kind--share_start";
  if (kind === "share_stop") return "activity-kind activity-kind--share_stop";
  if (kind === "receive") return "activity-kind activity-kind--receive";
  return "activity-kind";
}

function activityKindLabel(kind) {
  const m = { share_start: "Send", share_stop: "Stop", receive: "Receive" };
  return m[kind] || kind;
}

async function loadActivityList() {
  const listEl = document.getElementById("activity-list");
  const emptyEl = document.getElementById("activity-empty");
  if (!listEl || !emptyEl) return;
  emptyEl.textContent = "No activity yet — share or receive a file on Transfer.";
  listEl.innerHTML = "";
  try {
    const res = await fetch("/api/activity");
    const entries = await res.json();
    if (!Array.isArray(entries) || entries.length === 0) {
      emptyEl.classList.remove("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    entries.forEach((e) => {
      const li = document.createElement("li");
      const head = document.createElement("div");
      head.className = "activity-list__head";
      const kind = document.createElement("span");
      kind.className = activityKindClass(e.kind);
      kind.textContent = activityKindLabel(e.kind);
      const time = document.createElement("span");
      time.className = "activity-list__time";
      time.textContent = formatTs(e.ts);
      head.appendChild(kind);
      head.appendChild(time);
      const msg = document.createElement("p");
      msg.className = "activity-list__msg";
      msg.textContent = e.message;
      li.appendChild(head);
      li.appendChild(msg);
      listEl.appendChild(li);
    });
  } catch {
    emptyEl.textContent = "Could not load activity.";
    emptyEl.classList.remove("hidden");
  }
}

async function loadSettingsPanel() {
  const nameInput = document.getElementById("setting-display-name");
  const chunkInput = document.getElementById("setting-chunk-mb");
  const threadsInput = document.getElementById("setting-threads");
  if (nameInput) nameInput.value = getDisplayName();
  if (chunkInput) chunkInput.value = String(getChunkMb());
  if (threadsInput) threadsInput.value = String(getThreads());
  if (isLoopbackOrigin()) {
    await loadDesktopConfig();
  }
}

function normalizeLogValue(value, depth = 0) {
  if (value == null) return value;
  if (depth > 3) return "[truncated]";
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack || "",
    };
  }
  if (Array.isArray(value)) {
    return value.slice(0, 8).map((item) => normalizeLogValue(item, depth + 1));
  }
  if (typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value).slice(0, 12)) {
      out[key] = normalizeLogValue(item, depth + 1);
    }
    return out;
  }
  if (typeof value === "string" && value.length > 500) {
    return `${value.slice(0, 500)}…`;
  }
  return value;
}

function reportClientLog(level, message, details = {}) {
  try {
    const normalizedLevel = String(level || "INFO").toUpperCase();
    const normalizedMessage = String(message || "").trim();
    if (!normalizedMessage) return;

    const dedupeKey = `${normalizedLevel}|${normalizedMessage}|${window.location.pathname}`;
    const now = Date.now();
    const lastTs = clientLogRecent.get(dedupeKey) || 0;
    if (now - lastTs < 15000) {
      return;
    }
    clientLogRecent.set(dedupeKey, now);
    for (const [key, ts] of clientLogRecent.entries()) {
      if (now - ts > 60000) clientLogRecent.delete(key);
    }

    const payload = {
      level: normalizedLevel,
      message: normalizedMessage,
      source: "app.js",
      page: window.location.href,
      user_agent: navigator.userAgent || "",
      details: normalizeLogValue(details),
    };
    const body = JSON.stringify(payload);
    if (navigator.sendBeacon) {
      const ok = navigator.sendBeacon("/api/log/client", new Blob([body], { type: "application/json" }));
      if (ok) return;
    }
    void fetch("/api/log/client", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  } catch {
  }
}

const activityRefreshBtn = document.getElementById("activity-refresh");
if (activityRefreshBtn) activityRefreshBtn.addEventListener("click", () => loadActivityList());

const settingDisplayName = document.getElementById("setting-display-name");
if (settingDisplayName) {
  settingDisplayName.addEventListener("change", async () => {
    clientSettings.display_name = settingDisplayName.value.trim();
    try {
      await saveClientSettings();
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
    }
  });
}

const settingChunkMb = document.getElementById("setting-chunk-mb");
if (settingChunkMb) {
  settingChunkMb.addEventListener("change", async () => {
    const v = Math.max(0, Math.min(256, parseInt(settingChunkMb.value, 10) || 0));
    clientSettings.chunk_mb = v;
    settingChunkMb.value = String(v);
    try {
      await saveClientSettings();
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
    }
  });
}

const settingThreads = document.getElementById("setting-threads");
if (settingThreads) {
  settingThreads.addEventListener("change", async () => {
    const v = Math.max(0, Math.min(16, parseInt(settingThreads.value, 10) || 0));
    clientSettings.threads = v;
    settingThreads.value = String(v);
    try {
      await saveClientSettings();
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
    }
  });
}

function parsePortInput(value) {
  const text = String(value ?? "").trim();
  if (!text) {
    return { valid: false, port: 0, message: "Enter a port between 1 and 65535." };
  }
  const port = parseInt(text, 10);
  if (Number.isNaN(port) || port < 1 || port > 65535) {
    return { valid: false, port: 0, message: "Enter a port between 1 and 65535." };
  }
  return { valid: true, port, message: "" };
}

function setPortStatus(message) {
  if (settingPortStatus) {
    settingPortStatus.textContent = message;
  }
}

async function loadDesktopConfig() {
  if (!isLoopbackOrigin() || !settingPortInput) return;
  try {
    const res = await fetchWithTimeout("/api/app/config", { method: "GET" }, HEALTH_PING_TIMEOUT_MS);
    if (!res.ok) {
      throw await parseResponseError(res, "Unable to load desktop settings.");
    }
    desktopConfig = await res.json();
    const preferredPort = desktopConfig.configured_port || desktopConfig.current_port || 0;
    settingPortInput.value = preferredPort > 0 ? String(preferredPort) : "";
    if (
      desktopConfig.configured_port > 0 &&
      desktopConfig.current_port > 0 &&
      desktopConfig.configured_port !== desktopConfig.current_port
    ) {
      setPortStatus(
        `Configured port ${desktopConfig.configured_port} is busy. GrayShare is running on temporary port ${desktopConfig.current_port} for this launch.`,
      );
      return;
    }
    if (preferredPort > 0) {
      await checkPortAvailability(preferredPort);
      return;
    }
    setPortStatus("Enter a port between 1 and 65535.");
  } catch (err) {
    setPortStatus(parseErrorDetail(err));
  }
}

async function fetchPortAvailability(port) {
  const res = await fetchWithTimeout(
    `/api/app/port-check?port=${encodeURIComponent(port)}`,
    { method: "GET" },
    HEALTH_PING_TIMEOUT_MS,
  );
  if (!res.ok) {
    throw await parseResponseError(res, "Unable to check port.");
  }
  return res.json();
}

async function savePortConfig(port) {
  const res = await fetch("/api/app/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ port }),
  });
  if (!res.ok) {
    throw await parseResponseError(res, "Unable to save port.");
  }
  const result = await res.json();
  desktopConfig.configured_port = result.configured_port;
  setPortStatus(result.message);
  return result;
}

async function checkPortAvailability(port, { save = false } = {}) {
  if (!isLoopbackOrigin()) return;
  const requestId = ++portCheckRequestId;
  setPortStatus(`Checking port ${port}...`);
  try {
    const result = await fetchPortAvailability(port);
    if (requestId !== portCheckRequestId) {
      return;
    }
    if (save && result.available) {
      await savePortConfig(port);
      return;
    }
    setPortStatus(result.message || `Port ${port} checked.`);
  } catch (err) {
    if (requestId !== portCheckRequestId) {
      return;
    }
    setPortStatus(parseErrorDetail(err));
  }
}

function schedulePortAvailabilityCheck() {
  if (!settingPortInput) return;
  if (portCheckTimerId) clearTimeout(portCheckTimerId);
  const parsed = parsePortInput(settingPortInput.value);
  if (!parsed.valid) {
    portCheckRequestId += 1;
    setPortStatus(parsed.message);
    return;
  }
  portCheckTimerId = setTimeout(() => {
    checkPortAvailability(parsed.port);
  }, 250);
}

if (settingPortInput) {
  settingPortInput.addEventListener("input", schedulePortAvailabilityCheck);
  settingPortInput.addEventListener("change", async () => {
    const parsed = parsePortInput(settingPortInput.value);
    if (!parsed.valid) {
      setPortStatus(parsed.message);
      return;
    }
    try {
      await checkPortAvailability(parsed.port, { save: true });
      showToast("Port saved for the next launch.", "success");
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
      schedulePortAvailabilityCheck();
    }
  });
}

function isEmbeddedDesktopApp() {
  return Boolean(window.pywebview);
}

async function unregisterServiceWorkers() {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map((reg) => reg.unregister()));
  } catch {
  }
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) {
    return;
  }
  if (isEmbeddedDesktopApp()) {
    void unregisterServiceWorkers();
    return;
  }
  void navigator.serviceWorker.register("/sw.js").catch(() => {});
}

function saveBlob(filename, blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  setTimeout(() => {
    URL.revokeObjectURL(url);
    link.remove();
  }, 30000);
}

function isLoopbackOrigin() {
  const host = window.location.hostname || "";
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

function canUseDesktopSaveBridge() {
  return Boolean(window.pywebview?.api?.choose_save_path) && isLoopbackOrigin();
}

function canUseBrowserSaveHandle() {
  return typeof window.showSaveFilePicker === "function";
}

function shouldUseNativeBrowserDownload() {
  return !canUseDesktopSaveBridge() && !canUseBrowserSaveHandle();
}

async function chooseDesktopSavePath(filename) {
  if (!canUseDesktopSaveBridge()) return "";
  try {
    const path = await window.pywebview.api.choose_save_path(filename);
    return typeof path === "string" ? path : "";
  } catch {
    return "";
  }
}

async function chooseBrowserSaveHandle(filename) {
  if (!canUseBrowserSaveHandle()) return null;
  try {
    return await window.showSaveFilePicker({ suggestedName: filename });
  } catch (err) {
    if (err && err.name === "AbortError") {
      return null;
    }
    throw err;
  }
}

async function writeBlobToHandle(handle, blob) {
  const writable = await handle.createWritable();
  try {
    await writable.write(blob);
  } finally {
    await writable.close();
  }
}

async function saveShareLocally(share, passcode, targetPath) {
  const res = await fetch(`/api/receive/${share.sharer_id}/save-local`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      passcode: passcode || "",
      target_path: targetPath,
    }),
  });
  if (!res.ok) {
    throw await parseResponseError(res, "Unable to save file locally.");
  }
  return res.json();
}

function triggerNativeBrowserDownload(share, passcode) {
  const url = new URL(`/api/receive/${share.sharer_id}/download`, window.location.origin);
  if (passcode) {
    url.searchParams.set("passcode", passcode);
  }
  const link = document.createElement("a");
  link.href = url.toString();
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  setTimeout(() => link.remove(), 1000);
}

function stopShareHeartbeat() {
  if (shareHeartbeatTimerId) {
    clearTimeout(shareHeartbeatTimerId);
    shareHeartbeatTimerId = null;
  }
  shareHeartbeatInFlight = false;
}

async function resetSenderState(message, kind = "error") {
  stopShareHeartbeat();
  localSharerId = null;
  resetShareUi();
  setMode("idle");
  setShareStatus(message, kind);
  setTimeout(() => {
    shareStatus.classList.add("hidden");
  }, 1800);
  await refreshShares();
}

function startShareHeartbeat() {
  stopShareHeartbeat();
  if (!localSharerId) return;
  shareHeartbeatTimerId = setTimeout(async function runShareHeartbeat() {
    if (!localSharerId) return;
    if (shareHeartbeatInFlight) {
      shareHeartbeatTimerId = setTimeout(runShareHeartbeat, SHARE_HEARTBEAT_MS);
      return;
    }
    shareHeartbeatInFlight = true;
    try {
      const res = await fetchWithTimeout(
        `/api/share/${localSharerId}/heartbeat`,
        { method: "POST" },
        HEALTH_PING_TIMEOUT_MS,
      );
      if (!res.ok) {
        throw new Error("Share heartbeat failed.");
      }
    } catch (err) {
      reportClientLog("WARN", "Share heartbeat failed on the client.", {
        sharer_id: localSharerId,
        error: err,
      });
      await resetSenderState("Sharing ended because the sender session was lost.");
      return;
    } finally {
      shareHeartbeatInFlight = false;
    }
    shareHeartbeatTimerId = setTimeout(runShareHeartbeat, SHARE_HEARTBEAT_MS);
  }, SHARE_HEARTBEAT_MS);
}

function stopShareBeacon(sharerId) {
  const url = `/api/share/${sharerId}/stop`;
  try {
    if (navigator.sendBeacon) {
      const payload = new Blob([""], { type: "text/plain;charset=UTF-8" });
      navigator.sendBeacon(url, payload);
      return;
    }
  } catch {
  }
  void fetch(url, { method: "POST", keepalive: true }).catch(() => {});
}

function notifyShareStopOnUnload() {
  if (!localSharerId) return;
  stopShareBeacon(localSharerId);
  localSharerId = null;
}

window.addEventListener("pagehide", notifyShareStopOnUnload);
window.addEventListener("beforeunload", notifyShareStopOnUnload);

function parseErrorDetail(err) {
  if (!err) return "Something went wrong.";
  if (typeof err.detail === "string") return err.detail;
  if (Array.isArray(err.detail)) {
    return err.detail
      .map((d) => (typeof d === "string" ? d : d.msg || JSON.stringify(d)))
      .join(" ");
  }
  if (err.message) return err.message;
  return "Something went wrong.";
}

window.addEventListener("error", (event) => {
  reportClientLog("ERROR", "Unhandled browser error.", {
    message: event?.message || "",
    filename: event?.filename || "",
    lineno: event?.lineno || 0,
    colno: event?.colno || 0,
    error: event?.error || null,
  });
});

window.addEventListener("unhandledrejection", (event) => {
  reportClientLog("ERROR", "Unhandled promise rejection.", {
    reason: parseErrorDetail(event?.reason),
    error: event?.reason || null,
  });
});

function showToast(message, kind = "error") {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const item = document.createElement("div");
  item.className = `toast toast--${kind}`;
  item.textContent = message;
  root.appendChild(item);
  setTimeout(() => {
    item.classList.add("toast--out");
    setTimeout(() => item.remove(), 200);
  }, 2800);
}

const clearDataBtn = document.getElementById("clear-data");
if (clearDataBtn) {
  clearDataBtn.addEventListener("click", async () => {
    const confirmed = window.confirm(
      "Clear stored files, logs, and transfer data from %USERPROFILE%/.grayshare? Your saved settings will be kept.",
    );
    if (!confirmed) return;

    clearDataBtn.disabled = true;
    const prevText = clearDataBtn.textContent;
    clearDataBtn.textContent = "Clearing...";
    try {
      const res = await fetch("/api/data/clear", { method: "POST" });
      if (!res.ok) {
        throw await parseResponseError(res, "Unable to clear data.");
      }
      const result = await res.json();
      localSharerId = null;
      resetShareUi();
      setMode("idle");
      await refreshShares();
      if (viewHistory && !viewHistory.classList.contains("hidden")) {
        await loadActivityList();
      }
      const skipped = Array.isArray(result.skipped) && result.skipped.length ? ` Skipped: ${result.skipped.join(" | ")}` : "";
      showToast(`Cleared ${result.deleted_items} item(s). Settings were preserved.${skipped}`, "success");
    } catch (err) {
      showToast(parseErrorDetail(err), "error");
    } finally {
      clearDataBtn.disabled = false;
      clearDataBtn.textContent = prevText;
    }
  });
}

async function downloadChunkWithRetry(url, maxAttempts = DOWNLOAD_RETRY_LIMIT) {
  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const res = await fetchWithTimeout(url, { method: "GET" }, DOWNLOAD_TIMEOUT_MS);
      if (!res.ok) {
        const err = await parseResponseError(res, "Chunk download failed");
        const retryable = shouldRetryStatus(res.status);
        lastErr = err;
        if (retryable && attempt < maxAttempts) {
          await sleep(computeBackoff(attempt));
          continue;
        }
        throw err;
      }
      return await res.blob();
    } catch (err) {
      lastErr = err;
      if (attempt >= maxAttempts) {
        throw lastErr;
      }
      await sleep(computeBackoff(attempt));
    }
  }
  throw lastErr || new Error("Chunk download failed");
}

async function postFormWithDownloadProgressRetry(url, formData, onProgress, maxAttempts = DOWNLOAD_RETRY_LIMIT) {
  let lastErr = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await postFormWithDownloadProgress(url, formData, onProgress);
    } catch (err) {
      lastErr = err;
      if (attempt >= maxAttempts) {
        throw lastErr;
      }
      await sleep(computeBackoff(attempt));
    }
  }
  throw lastErr || new Error("Download failed");
}

function startReceiveHealthPing() {
  stopReceiveHealthPing();
  receiveHealthTimerId = setTimeout(async function runReceiveHealthPing() {
    if (!receiveInProgress) return;
    if (receiveHealthInFlight) {
      receiveHealthTimerId = setTimeout(runReceiveHealthPing, HEALTH_PING_INTERVAL_MS);
      return;
    }
    receiveHealthInFlight = true;
    try {
      const res = await fetchWithTimeout("/api/health", { method: "GET" }, HEALTH_PING_TIMEOUT_MS);
      if (!res.ok) {
        throw new Error("Health check failed");
      }
    } catch {
      receiveOverlayStatus.textContent = "Connection unstable. Reconnecting...";
    } finally {
      receiveHealthInFlight = false;
    }
    receiveHealthTimerId = setTimeout(runReceiveHealthPing, HEALTH_PING_INTERVAL_MS);
  }, HEALTH_PING_INTERVAL_MS);
}

function stopReceiveHealthPing() {
  if (receiveHealthTimerId) {
    clearTimeout(receiveHealthTimerId);
    receiveHealthTimerId = null;
  }
  receiveHealthInFlight = false;
}

function showReceiveOverlay(filename) {
  receiveOverlay.classList.remove("hidden");
  receiveOverlay.setAttribute("aria-hidden", "false");
  receiveOverlayTitle.textContent = "Downloading file";
  receiveOverlayFile.textContent = filename;
  receiveProgressBar.classList.add("indeterminate");
  receiveProgressFill.style.width = "0%";
  receiveOverlayStatus.textContent = "Connecting to server…";
}

function hideReceiveOverlay() {
  receiveOverlay.classList.add("hidden");
  receiveOverlay.setAttribute("aria-hidden", "true");
  receiveProgressBar.classList.remove("indeterminate");
  receiveProgressFill.style.width = "0%";
  receiveOverlayStatus.textContent = "";
}

/**
 * POST form and track *download* progress (response body).
 * Server should send Content-Length for accurate % on large files.
 */
function postFormWithDownloadProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "blob";

    xhr.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) {
        receiveProgressBar.classList.remove("indeterminate");
        onProgress(e.loaded / e.total, e.loaded, e.total);
      } else {
        onProgress(-1, e.loaded, 0);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const ct = xhr.getResponseHeader("Content-Type") || "";
        if (ct.includes("application/json")) {
          const reader = new FileReader();
          reader.onload = () => {
            try {
              reject(JSON.parse(reader.result));
            } catch {
              reject(new Error("Invalid response"));
            }
          };
          reader.readAsText(xhr.response);
          return;
        }
        resolve(xhr.response);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        try {
          reject(JSON.parse(reader.result));
        } catch {
          reject(new Error(xhr.statusText || "Download failed"));
        }
      };
      reader.readAsText(xhr.response);
    };

    xhr.onerror = () => reject(new Error("Network error"));
    xhr.send(formData);
  });
}

/** XMLHttpRequest so we get upload progress for multi-GB files. */
function postFormWithUploadProgress(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) {
        onProgress(e.loaded / e.total, e.loaded, e.total);
      } else {
        onProgress(-1, e.loaded, 0);
      }
    };
    xhr.onload = () => {
      const ct = xhr.getResponseHeader("Content-Type") || "";
      if (xhr.status >= 200 && xhr.status < 300) {
        if (ct.includes("application/json")) {
          try {
            resolve(JSON.parse(xhr.responseText));
          } catch (err) {
            reject(err);
          }
        } else {
          resolve(xhr.responseText);
        }
      } else {
        try {
          reject(JSON.parse(xhr.responseText));
        } catch {
          reject(new Error(xhr.statusText || "Upload failed"));
        }
      }
    };
    xhr.onerror = () => reject(new Error("Network error"));
    xhr.send(formData);
  });
}

function setSharesEmptyState(message) {
  const renderKey = `empty:${message}`;
  if (renderKey === lastSharesRenderKey) {
    return;
  }
  lastSharesRenderKey = renderKey;
  sharesList.innerHTML = `<li class="empty">${message}</li>`;
}

async function refreshShares({ allowDefer = false } = {}) {
  if (!viewTransfer || viewTransfer.classList.contains("hidden")) {
    return;
  }
  if (localSharerId || receiveInProgress || refreshSharesInFlight || document.hidden) {
    return;
  }
  if (allowDefer && isUiInteractionActive()) {
    noteUiInteraction(250);
    return;
  }

  refreshSharesInFlight = true;
  let shares = [];
  try {
    const res = await fetchWithTimeout("/api/shares", { method: "GET" }, HEALTH_PING_TIMEOUT_MS);
    if (!res.ok) {
      throw new Error("Unable to load active sharers");
    }
    shares = await res.json();
  } catch {
    setSharesEmptyState("Connection issue. Retrying...");
    refreshSharesInFlight = false;
    return;
  }

  const renderKey = JSON.stringify(
    Array.isArray(shares)
      ? shares.map((share) => [
          share?.sharer_id || "",
          share?.display_name || "",
          share?.filename || "",
          Number(share?.size_bytes || 0),
          Boolean(share?.has_passcode),
        ])
      : [],
  );
  if (renderKey === lastSharesRenderKey) {
    refreshSharesInFlight = false;
    return;
  }

  lastSharesRenderKey = renderKey;
  sharesList.innerHTML = "";
  if (!shares.length) {
    setSharesEmptyState("No active sharers right now.");
    refreshSharesInFlight = false;
    return;
  }

  const fragment = document.createDocumentFragment();
  shares.forEach((share) => {
    const li = document.createElement("li");
    const main = document.createElement("div");
    main.className = "share-main";

    const name = document.createElement("div");
    name.className = "share-name";
    name.textContent = share.display_name;

    if (share.has_passcode) {
      const chip = document.createElement("span");
      chip.className = "lock-chip";
      chip.textContent = "Passcode";
      name.appendChild(chip);
    }

    const meta = document.createElement("div");
    meta.className = "share-meta";
    meta.textContent = `${share.filename} - ${formatBytes(share.size_bytes)}`;
    main.appendChild(name);
    main.appendChild(meta);

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-list-receive";
    btn.textContent = "Receive";
    btn.addEventListener("click", async () => {
      let passcode = "";
      if (share.has_passcode) {
        passcode = prompt(`Passcode required for ${share.display_name}`) || "";
      }

      const prevLabel = btn.textContent;
      let desktopSavePath = "";
      let browserSaveHandle = null;
      try {
        if (canUseDesktopSaveBridge()) {
          desktopSavePath = await chooseDesktopSavePath(share.filename);
          if (!desktopSavePath) {
            return;
          }
        } else if (!shouldUseNativeBrowserDownload()) {
          browserSaveHandle = await chooseBrowserSaveHandle(share.filename);
          if (canUseBrowserSaveHandle() && !browserSaveHandle) {
            return;
          }
        }
      } catch (err) {
        showToast(parseErrorDetail(err), "error");
        return;
      }
      btn.disabled = true;
      btn.textContent = "Receiving...";
      receiveInProgress = true;
      showReceiveOverlay(share.filename);
      startReceiveHealthPing();

      try {
        if (desktopSavePath) {
          receiveProgressBar.classList.add("indeterminate");
          receiveOverlayStatus.textContent = "Saving to selected location...";
          const result = await saveShareLocally(share, passcode, desktopSavePath);
          receiveProgressBar.classList.remove("indeterminate");
          receiveProgressFill.style.width = "100%";
          receiveOverlayStatus.textContent = `Saved to ${result.saved_path}`;
          await new Promise((r) => setTimeout(r, 900));
          return;
        }

        if (shouldUseNativeBrowserDownload()) {
          receiveProgressBar.classList.remove("indeterminate");
          receiveProgressFill.style.width = "100%";
          receiveOverlayStatus.textContent = "Starting browser download...";
          triggerNativeBrowserDownload(share, passcode);
          await new Promise((r) => setTimeout(r, 900));
          return;
        }

        const onDl = (ratio, loaded, total) => {
          if (ratio < 0) {
            receiveProgressBar.classList.add("indeterminate");
            receiveOverlayStatus.textContent = `Downloaded ${formatBytes(loaded)}...`;
            return;
          }
          receiveProgressBar.classList.remove("indeterminate");
          const pct = Math.min(100, Math.round(ratio * 100));
          receiveProgressFill.style.width = `${pct}%`;
          receiveOverlayStatus.textContent = `Downloaded ${formatBytes(loaded)} / ${formatBytes(total)} (${pct}%)`;
        };

        const blob = await downloadFileAdaptive(share, passcode, onDl);
        if (browserSaveHandle) {
          receiveOverlayStatus.textContent = "Writing to selected location...";
          receiveProgressFill.style.width = "100%";
          await writeBlobToHandle(browserSaveHandle, blob);
          receiveOverlayStatus.textContent = `Saved ${share.filename}`;
          await new Promise((r) => setTimeout(r, 600));
          return;
        }

        receiveOverlayStatus.textContent = "Saving to your device...";
        receiveProgressFill.style.width = "100%";
        saveBlob(share.filename, blob);
        receiveOverlayStatus.textContent = "Done - check your downloads folder.";
        await new Promise((r) => setTimeout(r, 600));
      } catch (err) {
        reportClientLog("ERROR", "Receive operation failed in the client.", {
          sharer_id: share.sharer_id,
          filename: share.filename,
          error: err,
        });
        showToast(parseErrorDetail(err), "error");
      } finally {
        receiveInProgress = false;
        stopReceiveHealthPing();
        hideReceiveOverlay();
        btn.disabled = false;
        btn.textContent = prevLabel;
      }
    });

    li.appendChild(main);
    li.appendChild(btn);
    fragment.appendChild(li);
  });
  sharesList.appendChild(fragment);
  refreshSharesInFlight = false;
}

shareForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = new FormData(shareForm);
  const file = shareFileInput?.files?.[0];
  if (!file) {
    shareStatus.classList.remove("hidden");
    setShareStatus("Choose a file first.", "error");
    return;
  }
  const displayName = getDisplayName();
  if (!displayName) {
    shareStatus.classList.remove("hidden");
    setShareStatus("Set your display name in Settings first.", "error");
    return;
  }
  const passcode = (body.get("passcode") || "").trim();
  body.set("display_name", displayName);

  uploadProgressWrap.classList.remove("hidden");
  uploadProgressBar.classList.add("indeterminate");
  uploadProgressFill.style.width = "0%";
  uploadProgressText.textContent = "Preparing upload…";
  shareSubmit.disabled = true;
  shareStatus.classList.remove("hidden");
  setShareStatus(
    "Uploading to server (this can take a while for very large files)…",
    "info",
  );

  try {
    const settingsRes = await fetchWithTimeout("/api/settings", { method: "GET" }, DOWNLOAD_TIMEOUT_MS);
    if (!settingsRes.ok) {
      throw await parseResponseError(settingsRes, "Unable to load server settings.");
    }
    const settings = await settingsRes.json();

    if (settings.smb_active) {
      const data = await postFormWithUploadProgress("/api/share", body, (ratio, loaded, total) => {
        if (ratio < 0) {
          uploadProgressBar.classList.add("indeterminate");
          uploadProgressText.textContent = `Uploaded ${formatBytes(loaded)}…`;
          return;
        }
        uploadProgressBar.classList.remove("indeterminate");
        const pct = Math.min(100, Math.round(ratio * 100));
        uploadProgressFill.style.width = `${pct}%`;
        uploadProgressText.textContent = `Uploaded ${formatBytes(loaded)} / ${formatBytes(total)} (${pct}%)`;
      });
      localSharerId = data.sharer_id;
      updateServerEndpoint(resolveServerUrl(data));
    } else {
      uploadProgressText.textContent = "Measuring upload speed…";
      const shareResult = await uploadFileInChunks(
        file,
        displayName,
        passcode,
        (done, total, chunkBytes, workers) => {
          uploadProgressBar.classList.remove("indeterminate");
          const pct = Math.round((done / total) * 100);
          uploadProgressFill.style.width = `${pct}%`;
          uploadProgressText.textContent = `Chunks ${done}/${total} (${pct}%) · ${formatBytes(chunkBytes)} each · ${workers} parallel`;
        },
        getChunkMb(),
        getThreads(),
      );
      localSharerId = shareResult.sharer_id;
      updateServerEndpoint(resolveServerUrl(shareResult));
    }

    uploadProgressBar.classList.add("indeterminate");
    uploadProgressFill.style.width = "0%";
    uploadProgressText.textContent = "Finalizing…";
    shareSubmit.classList.add("hidden");
    stopShareBtn.classList.remove("hidden");
    setShareStatus("Sharing started. You are now sender-only.", "success");
    startShareHeartbeat();
    uploadProgressBar.classList.remove("indeterminate");
    uploadProgressFill.style.width = "100%";
    uploadProgressText.textContent = "Complete.";
    setTimeout(() => {
      uploadProgressWrap.classList.add("hidden");
      uploadProgressFill.style.width = "0%";
      uploadProgressText.textContent = "";
    }, 800);
  } catch (err) {
    reportClientLog("ERROR", "Share submission failed in the client.", {
      filename: file?.name || "",
      display_name: displayName,
      error: err,
    });
    const msg =
      err && typeof err === "object" && "detail" in err
        ? err.detail
        : err && err.message
          ? err.message
          : "Unable to share file.";
    setShareStatus(
      typeof msg === "string" ? msg : Array.isArray(msg) ? msg.map((m) => m.msg || m).join(" ") : "Unable to share file.",
      "error",
    );
    shareSubmit.classList.remove("hidden");
    stopShareBtn.classList.add("hidden");
    shareSubmit.disabled = false;
    uploadProgressBar.classList.remove("indeterminate");
    setTimeout(() => {
      uploadProgressWrap.classList.add("hidden");
      uploadProgressFill.style.width = "0%";
      uploadProgressText.textContent = "";
    }, 800);
  }
});

stopShareBtn.addEventListener("click", async () => {
  if (localSharerId) {
    try {
      await fetch(`/api/share/${localSharerId}/stop`, { method: "POST" });
    } catch {
    }
  }
  stopShareHeartbeat();
  localSharerId = null;
  resetShareUi();
  setShareStatus("Sharing stopped. You can receive again.", "success");
  setTimeout(() => {
    shareStatus.classList.add("hidden");
  }, 1200);
  setMode("idle");
  await refreshShares();
});

async function initApp() {
  applyClientVisibility();
  setMode("idle");
  resetShareUi();
  await loadClientSettings();
  setupTheme();
  registerServiceWorker();
  await loadSettingsPanel();
  configureSharePolling();
  setupNetworkInfo();
  await refreshShares();
}

initApp().catch((err) => {
  reportClientLog("ERROR", "GrayShare frontend initialization failed.", { error: err });
  showToast(parseErrorDetail(err), "error");
});
