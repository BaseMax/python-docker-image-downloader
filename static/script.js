/**
 * script.js — Docker Image Downloader UI
 *
 * Sections
 * --------
 * 1. DOM references
 * 2. State
 * 3. Error parsing
 * 4. UI helpers  (clearStatus, setStatusWithSpinner, showError, hidePlatforms)
 * 5. Platform detection
 * 6. Image download
 * 7. Event listeners / init
 */

/* ── 1. DOM references ───────────────────────────────────────────────────────── */
const form         = document.getElementById("downloadForm");
const imageInput   = document.getElementById("imageName");
const downloadBtn  = document.getElementById("downloadBtn");
const detectBtn    = document.getElementById("detectBtn");
const platformRow  = document.getElementById("platformRow");
const osSelect     = document.getElementById("osSelect");
const archSelect   = document.getElementById("archSelect");
const statusEl     = document.getElementById("status");
const progressBar  = document.getElementById("progressBar");
const progressFill = document.getElementById("progressFill");

/* ── 2. State ─────────────────────────────────────────────────────────────────── */
/** Image name that the current platform dropdowns were detected for. */
let detectedForImage = null;

/* ── 3. Error parsing ─────────────────────────────────────────────────────────── */
/**
 * Map a raw error message from the backend to a user-friendly title + body.
 * @param {string} message
 * @returns {{ title: string, message: string, detail: string|null }}
 */
function parseError(message) {
    const result = { title: "Download Failed", message, detail: null };

    const checks = [
        [/image not found/,                      "Image Not Found",         "The requested image does not exist."],
        [/MANIFEST_UNKNOWN|manifest unknown/,    "Image Not Found",         "The requested image tag does not exist."],
        [/NAME_UNKNOWN|name unknown/,            "Repository Not Found",    "The requested repository does not exist."],
        [/access denied/,                        "Access Denied",           "The image may not exist, or you need credentials to access it."],
        [/UNAUTHORIZED|unauthorized/,            "Authentication Required", "You need to be authenticated to access this image."],
        [/DENIED|denied/,                        "Access Denied",           "You do not have permission to access this image."],
        [/connection refused|no such host/,      "Connection Error",        "Could not connect to the registry server."],
        [/timeout/i,                             "Request Timeout",         "The request took too long to complete."],
        [/image name cannot be empty/,           "Invalid Image Name",      "Image name cannot be empty."],
        [/image name too long/,                  "Invalid Image Name",      "Image name is too long (maximum 256 characters)."],
        [/contains invalid characters/,          "Invalid Image Name",      "Image name contains invalid characters."],
        [/authentication failed/,                "Authentication Failed",   "Failed to authenticate with the registry."],
        [/no manifest found for platform/,       "Platform Not Available",  "This image does not support the selected platform."],
        [/failed to get manifest/,               "Manifest Error",          "Could not retrieve the image manifest from the registry."],
        [/failed to download/,                   "Download Error",          "Failed to download one or more image layers."],
    ];

    for (const [pattern, title, msg] of checks) {
        if (pattern.test(message)) {
            result.title   = title;
            result.message = msg;
            break;
        }
    }

    if (/MANIFEST_UNKNOWN|manifest unknown/.test(message)) {
        const m = message.match(/unknown tag=([^\s}"]+)/);
        if (m) result.detail = `Tag "${m[1]}" was not found in the registry.`;
    } else if (/no manifest found for platform/.test(message)) {
        result.detail = 'Use "Detect platforms" to see what is available.';
    } else if (/invalid image name/.test(message)) {
        const m = message.match(/invalid image name:\s*(.+)/i);
        if (m) result.detail = m[1];
    } else if (/authentication failed/.test(message)) {
        const m = message.match(/(\d{3})\s*-/);
        if (m) result.detail = `Server returned status ${m[1]}`;
    } else if (/unexpected status/.test(message)) {
        const m = message.match(/unexpected status:\s*(\d+)/);
        if (m) result.detail = `Status code: ${m[1]}`;
    }

    return result;
}

/* ── 4. UI helpers ────────────────────────────────────────────────────────────── */
function clearStatus() {
    while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
    statusEl.classList.remove("error");
}

function setStatusWithSpinner(text) {
    clearStatus();
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    statusEl.appendChild(spinner);
    statusEl.appendChild(document.createTextNode(text));
}

function showError(rawMessage) {
    const { title, message, detail } = parseError(rawMessage);
    clearStatus();

    const box = document.createElement("div");
    box.className = "error-box";
    box.appendChild(createDiv("error-title", title));
    box.appendChild(createDiv("error-message", message));
    if (detail) box.appendChild(createDiv("error-detail", detail));

    statusEl.appendChild(box);
}

/** Small utility: create a <div> with a class and text content. */
function createDiv(className, text) {
    const el = document.createElement("div");
    el.className = className;
    el.textContent = text;
    return el;
}

function hidePlatforms() {
    detectedForImage = null;
    platformRow.classList.remove("visible");
    osSelect.innerHTML  = "";
    archSelect.innerHTML = "";
}

function setButtonsDisabled(disabled) {
    downloadBtn.disabled = disabled;
    detectBtn.disabled   = disabled;
}

/* ── 5. Platform detection ────────────────────────────────────────────────────── */
async function detectPlatforms() {
    const imageName = imageInput.value.trim();
    if (!imageName) {
        statusEl.textContent = "Please enter an image name first";
        statusEl.classList.add("error");
        return;
    }

    clearStatus();
    setStatusWithSpinner("Detecting available platforms…");
    detectBtn.disabled = true;
    hidePlatforms();

    try {
        const resp = await fetch(`/platforms?name=${encodeURIComponent(imageName)}`);
        const data = await resp.json();

        if (!resp.ok) {
            showError(data.error || "Failed to detect platforms");
            return;
        }

        const platforms = (data.platforms || []).filter(
            (p) => p.os && p.os !== "unknown" && p.architecture && p.architecture !== "unknown"
        );

        if (platforms.length === 0) {
            statusEl.textContent = "Single-arch image — no platform selection needed.";
            return;
        }

        populatePlatformSelects(platforms);
        detectedForImage = imageName;
        platformRow.classList.add("visible");
        clearStatus();

    } catch (err) {
        showError(err.message);
    } finally {
        detectBtn.disabled = false;
    }
}

function populatePlatformSelects(platforms) {
    const osValues   = [...new Set(platforms.map((p) => p.os))];
    const archValues = [...new Set(platforms.map((p) =>
        p.variant ? `${p.architecture}/${p.variant}` : p.architecture
    ))];

    osSelect.innerHTML = "";
    osValues.forEach((o) => osSelect.appendChild(makeOption(o)));

    archSelect.innerHTML = "";
    archValues.forEach((a) => archSelect.appendChild(makeOption(a)));
}

function makeOption(value) {
    const opt = document.createElement("option");
    opt.value       = value;
    opt.textContent = value;
    return opt;
}

/* ── 6. Image download ────────────────────────────────────────────────────────── */
async function downloadImage() {
    const imageName = imageInput.value.trim();
    if (!imageName) {
        statusEl.textContent = "Please enter an image name";
        statusEl.classList.add("error");
        return;
    }

    const url = buildDownloadURL(imageName);

    clearStatus();
    setStatusWithSpinner("Preparing download…");
    showProgressBar(true);
    setButtonsDisabled(true);

    try {
        const resp = await fetch(url);

        if (!resp.ok) {
            const errorData = await resp.json();
            throw new Error(errorData.error || "Download failed");
        }

        const totalBytes = getContentLength(resp);
        const filename   = extractFilename(resp) || "image.tar";

        const { chunks, received } = await streamResponse(resp, totalBytes);

        triggerBrowserDownload(chunks, filename);

        imageInput.value = "";
        hidePlatforms();
        clearStatus();
        statusEl.textContent = `Download complete! (${toMB(received)} MB)`;
        showProgressBar(false);

    } catch (err) {
        showError(err.message);
        showProgressBar(false);
    } finally {
        setButtonsDisabled(false);
    }
}

/** Build the /image query URL, including platform params when detected. */
function buildDownloadURL(imageName) {
    let url = `/image?name=${encodeURIComponent(imageName)}`;

    if (detectedForImage === imageName) {
        const selectedOS    = osSelect.value;
        const archParts     = archSelect.value.split("/");
        const arch          = archParts[0];
        const variant       = archParts[1] || "";
        url += `&os=${encodeURIComponent(selectedOS)}&arch=${encodeURIComponent(arch)}`;
        if (variant) url += `&variant=${encodeURIComponent(variant)}`;
    }

    return url;
}

/** Read + track progress of a streaming fetch response. */
async function streamResponse(resp, totalBytes) {
    const reader = resp.body.getReader();
    const chunks = [];
    let received = 0;

    progressFill.classList.remove("indeterminate");
    setStatusWithSpinner("Downloading…");

    while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        chunks.push(value);
        received += value.length;
        updateProgress(received, totalBytes);
    }

    return { chunks, received };
}

function updateProgress(received, totalBytes) {
    if (totalBytes > 0) {
        const pct = Math.min(100, Math.max(0, Math.round((received / totalBytes) * 100)));
        progressFill.style.width = `${pct}%`;
        setStatusWithSpinner(
            `Downloading… ${toMB(received)} MB / ${toMB(totalBytes)} MB (${pct}%)`
        );
    } else {
        setStatusWithSpinner(`Downloading… ${toMB(received)} MB`);
    }
}

function triggerBrowserDownload(chunks, filename) {
    const blob    = new Blob(chunks);
    const blobUrl = URL.createObjectURL(blob);
    const a       = document.createElement("a");
    a.href     = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(blobUrl);
}

function showProgressBar(visible) {
    progressBar.classList.toggle("visible", visible);
    if (visible) {
        progressFill.style.width = "0%";
        progressFill.classList.add("indeterminate");
    } else {
        progressFill.style.width = "0%";
    }
}

/* ── Tiny utilities ──────────────────────────────────────────────────────────── */
function getContentLength(resp) {
    const header = resp.headers.get("Content-Length");
    return header ? parseInt(header, 10) : 0;
}

function extractFilename(resp) {
    const disposition = resp.headers.get("Content-Disposition");
    if (!disposition) return null;

    const m = disposition.match(/filename="(.+)"/);
    if (!m) return null;

    const sanitized = m[1]
        .replace(/[^a-zA-Z0-9._\-]/g, "_")
        .replace(/^\.+/, "")
        .substring(0, 255);

    return sanitized && /^[a-zA-Z0-9][a-zA-Z0-9._\-]*$/.test(sanitized)
        ? sanitized
        : null;
}

function toMB(bytes) {
    return (bytes / 1_048_576).toFixed(1);
}

/* ── 7. Event listeners ──────────────────────────────────────────────────────── */
imageInput.addEventListener("input", hidePlatforms);
detectBtn.addEventListener("click", detectPlatforms);
form.addEventListener("submit", (e) => { e.preventDefault(); downloadImage(); });
