// app.js — makes the portal buttons work and keeps each camera's status up to date.
"use strict";

const PORTAL_KEY = new URLSearchParams(window.location.search).get("key") || "";
const POLL_MS = 2000;       // Refresh camera status every 2 seconds.
const SNAPSHOT_MS = 1000;   // Refresh each tile's still image about once a second.
let stopped = false;        // Set when the link is renewed. Stops all background requests.

// ── Talking to the server ────────────────────────────────────────────────────

async function api(path, method = "GET", body = null) {
  const options = { method, headers: { "X-Portal-Key": PORTAL_KEY } };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401) {              // Session ended (password changed, link renewed elsewhere).
    window.location.reload();                 // Reload shows the sign-in page.
    throw new Error("Signed out.");
  }
  let data = {};
  try { data = await response.json(); } catch { /* 404 pages aren't JSON */ }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

// ── Messages ─────────────────────────────────────────────────────────────────

let messageTimer = null;
function showMessage(text, isError = false) {
  const box = document.getElementById("message");
  box.textContent = text;
  box.classList.toggle("error", isError);
  box.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { box.hidden = true; }, 4000);
}

// ── Updating tiles from /api/cameras ─────────────────────────────────────────

function updateTile(tile, cam) {
  const status = tile.querySelector(".status");
  status.textContent = cam.status;
  status.className = "status " + (cam.online ? "live" : "down");

  const overlay = tile.querySelector(".overlay");
  overlay.hidden = cam.online;
  overlay.textContent = cam.status;

  tile.querySelector(".rec-badge").hidden = !cam.recording;

  const recordButton = tile.querySelector('[data-action="record"]');
  recordButton.textContent = cam.recording ? "Stop Recording" : "Start Recording";
  recordButton.disabled = !cam.online && !cam.recording;
}

function updateCount(count) {
  document.getElementById("camera-count").textContent = `${count} camera${count === 1 ? "" : "s"}`;
  document.getElementById("empty").hidden = count > 0;
}

async function refreshStatus() {
  try {
    const { cameras } = await api("/api/cameras");
    const byId = new Map(cameras.map(cam => [cam.id, cam]));
    document.querySelectorAll(".tile").forEach(tile => {
      const cam = byId.get(tile.dataset.id);
      if (cam) updateTile(tile, cam);
      else {                                    // Removed from another browser.
        if (liveCameraId === tile.dataset.id) closeLive();
        tile.remove();
      }
    });
    updateCount(cameras.length);
    if (cameras.some(cam => !document.querySelector(`.tile[data-id="${CSS.escape(cam.id)}"]`))) {
      window.location.reload();                 // Added from another browser: reload to show it.
    }
  } catch (err) {
    showMessage("Lost contact with the camera server. Retrying...", true);
  }
}

// ── Tile stills ──────────────────────────────────────────────────────────────
// Each tile fetches one JPEG, shows it, waits SNAPSHOT_MS, and repeats. A still is a
// short request, so the browser's ~6-connection limit is never used up by tiles.

async function snapshotLoop(tile) {
  const img = tile.querySelector(".video img");
  while (!stopped && tile.isConnected) {
    if (!document.hidden) {                       // Pause while the tab is in the background.
      try {
        const response = await fetch(tile.dataset.snapshot, { cache: "no-store" });
        if (response.status === 404) break;       // Camera removed or link renewed.
        if (response.status === 200) {            // 204 = no picture yet. Keep the last one.
          const blobUrl = URL.createObjectURL(await response.blob());
          const oldUrl = img.dataset.blobUrl;
          img.src = blobUrl;
          img.dataset.blobUrl = blobUrl;
          if (oldUrl) URL.revokeObjectURL(oldUrl);  // Free the previous still's memory.
        }
      } catch { /* Network blip. Try again next round. */ }
    }
    await new Promise(resolve => setTimeout(resolve, SNAPSHOT_MS));
  }
}

// ── Live viewer (one camera at a time) ───────────────────────────────────────

const liveDialog = document.getElementById("live-dialog");
const liveVideo = document.getElementById("live-video");
let liveCameraId = null;

function openLive(tile, name) {
  liveCameraId = tile.dataset.id;
  document.getElementById("live-title").textContent = name;
  liveVideo.src = tile.dataset.stream;
  liveDialog.showModal();
}

function closeLive() {
  liveVideo.removeAttribute("src");             // Ends the MJPEG connection.
  liveCameraId = null;
  if (liveDialog.open) liveDialog.close();
}

document.getElementById("live-close").addEventListener("click", closeLive);
liveDialog.addEventListener("close", closeLive);  // Also covers the Esc key.

// ── Tile buttons (one listener handles every tile) ───────────────────────────

document.getElementById("grid").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const tile = button.closest(".tile");
  const id = encodeURIComponent(tile.dataset.id);
  const name = tile.querySelector("h2").firstChild.textContent.trim();

  if (button.dataset.action === "live") {
    openLive(tile, name);
    return;
  }

  button.disabled = true;
  try {
    if (button.dataset.action === "record") {
      const turnOn = button.textContent.trim() === "Start Recording";
      await api(`/api/cameras/${id}/record`, "POST", { on: turnOn });
      showMessage(`${name}: recording ${turnOn ? "started" : "stopped"}.`);
    } else if (button.dataset.action === "reconnect") {
      await api(`/api/cameras/${id}/reconnect`, "POST");
      showMessage(`${name}: reconnecting...`);
    } else if (button.dataset.action === "remove") {
      if (!confirm(`Remove "${name}" from the portal?\nIt stops streaming and recording now, and comes back when the server restarts.`)) return;
      if (liveCameraId === tile.dataset.id) closeLive();   // Close its live video first.
      await api(`/api/cameras/${id}/remove`, "POST");
      tile.remove();
      showMessage(`${name} removed.`);
    }
  } catch (err) {
    showMessage(`${name}: ${err.message}`, true);
  } finally {
    button.disabled = false;
    refreshStatus();
  }
});

// ── Toolbar ──────────────────────────────────────────────────────────────────

document.getElementById("start-all").addEventListener("click", async () => {
  try {
    await api("/api/record-all", "POST", { on: true });
    showMessage("Recording started on every live camera.");
  } catch (err) { showMessage(err.message, true); }
  refreshStatus();
});

document.getElementById("stop-all").addEventListener("click", async () => {
  try {
    await api("/api/record-all", "POST", { on: false });
    showMessage("Recording stopped on every camera.");
  } catch (err) { showMessage(err.message, true); }
  refreshStatus();
});

// ── Add camera dialog ────────────────────────────────────────────────────────

const dialog = document.getElementById("add-dialog");
const form = document.getElementById("add-form");
const formError = document.getElementById("add-error");
const streamSelect = document.getElementById("stream-select");
const customStream = document.getElementById("custom-stream");
const streamHint = document.getElementById("stream-hint");
const submitButton = document.getElementById("add-submit");

const STREAM_HINTS = {
  detect: "The portal asks the camera for its stream. Needs ONVIF turned on in the camera's settings.",
  custom: "Find the RTSP path in the camera's manual or web settings. The port is usually 554.",
  preset: "Uses this brand's usual main-stream path on port 554.",
};

function updateStreamFields() {
  const mode = streamSelect.value;
  customStream.hidden = mode !== "custom";
  form.elements.rtsp_path.required = mode === "custom";
  streamHint.textContent = STREAM_HINTS[mode] || STREAM_HINTS.preset;
  submitButton.textContent = "Add";
}
streamSelect.addEventListener("change", updateStreamFields);

function openAddDialog(prefill = {}) {
  form.reset();
  formError.hidden = true;
  form.elements.ip_address.value = prefill.ip_address || "";
  form.elements.name.value = prefill.name || "";
  updateStreamFields();
  dialog.showModal();
  (prefill.ip_address ? form.elements.username : form.elements.ip_address).focus();
}

document.getElementById("add-camera").addEventListener("click", () => openAddDialog());
document.getElementById("add-cancel").addEventListener("click", () => dialog.close());

form.addEventListener("submit", async event => {
  event.preventDefault();
  const fields = Object.fromEntries(new FormData(form));
  const body = {
    ip_address: fields.ip_address,
    name: fields.name,
    username: fields.username,
    password: fields.password,
  };
  if (fields.stream === "detect") {
    body.detect = true;
  } else if (fields.stream === "custom") {
    body.rtsp_path = fields.rtsp_path;
    body.rtsp_port = fields.rtsp_port;
  } else {
    body.rtsp_path = fields.stream;           // A brand preset. Its value is the path.
    body.rtsp_port = 554;
  }

  submitButton.disabled = true;
  submitButton.textContent = body.detect ? "Detecting..." : "Adding...";
  formError.hidden = true;
  try {
    await api("/api/cameras", "POST", body);
    dialog.close();
    window.location.reload();                    // Show the new tile.
  } catch (err) {
    formError.textContent = err.message;
    formError.hidden = false;
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Add";
  }
});

// ── Find cameras (ONVIF discovery) ───────────────────────────────────────────

const findDialog = document.getElementById("find-dialog");
const findList = document.getElementById("find-list");
const findStatus = document.getElementById("find-status");
const findAgain = document.getElementById("find-again");

function renderFound(cameras) {
  findList.replaceChildren();
  for (const cam of cameras) {
    // Names come from devices on the network, so they're set with textContent, never as HTML.
    const item = document.createElement("li");
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = cam.name;
    const detail = document.createElement("span");
    detail.className = "muted";
    detail.textContent = cam.hardware ? `${cam.ip_address}, model ${cam.hardware}` : cam.ip_address;
    info.append(title, detail);

    const button = document.createElement("button");
    button.type = "button";
    if (cam.added) {
      button.textContent = "Added";
      button.disabled = true;
    } else {
      button.textContent = "Add";
      button.className = "primary";
      button.addEventListener("click", () => {
        findDialog.close();
        openAddDialog({ ip_address: cam.ip_address, name: cam.name === cam.ip_address ? "" : cam.name });
      });
    }
    item.append(info, button);
    findList.append(item);
  }
}

async function searchNetwork() {
  findAgain.disabled = true;
  findList.replaceChildren();
  findStatus.textContent = "Searching this network (about 3 seconds)...";
  try {
    const { cameras } = await api("/api/discover");
    renderFound(cameras);
    findStatus.textContent = cameras.length
      ? `Found ${cameras.length} camera${cameras.length === 1 ? "" : "s"}.`
      : "No ONVIF cameras answered. Check that ONVIF is turned on in each camera, " +
        "or add the camera with “+ Add camera” and its IP address.";
  } catch (err) {
    findStatus.textContent = `Search failed: ${err.message}`;
  } finally {
    findAgain.disabled = false;
  }
}

document.getElementById("find-cameras").addEventListener("click", () => {
  findDialog.showModal();
  searchNetwork();
});
findAgain.addEventListener("click", searchNetwork);
document.getElementById("find-close").addEventListener("click", () => findDialog.close());

// ── Renew link ───────────────────────────────────────────────────────────────

let pollTimer = null;

document.getElementById("renew-link").addEventListener("click", async () => {
  if (!confirm("Make a new portal link?\n\nThe current link stops working immediately. " +
               "Every other phone or laptop will need the new link.")) return;
  try {
    const { url } = await api("/api/renew-link", "POST");
    clearInterval(pollTimer);                                     // The old address is going away.
    stopped = true;                                               // Stops every tile's still loop.
    closeLive();
    const renewDialog = document.getElementById("renew-dialog");
    const linkBox = document.getElementById("new-link");
    linkBox.value = url;
    renewDialog.showModal();
    linkBox.select();                                             // Easy to copy on phone or PC.
    document.getElementById("open-new-link").onclick = () => { window.location.href = url; };
    let secondsLeft = 5;
    const countdown = setInterval(() => {
      secondsLeft -= 1;
      document.getElementById("renew-countdown").textContent = secondsLeft;
      if (secondsLeft <= 0) {
        clearInterval(countdown);
        window.location.href = url;
      }
    }, 1000);
  } catch (err) {
    showMessage(`Could not renew the link: ${err.message}`, true);
  }
});

// ── Start ────────────────────────────────────────────────────────────────────

pollTimer = setInterval(refreshStatus, POLL_MS);
refreshStatus();
document.querySelectorAll(".tile").forEach(tile => { snapshotLoop(tile); });
