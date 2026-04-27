const PLATFORMS = ["telegram", "whatsapp", "signal"];

const state = {
  groups: [],
  filtered: [],
  markers: new Map(),
  userPos: null,
  userMarker: null,
  query: "",
  radiusKm: 0,
};

const map = L.map("map").setView([20, 0], 2);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 19,
  minZoom: 2,
}).addTo(map);

const listEl = document.getElementById("group-list");
const searchEl = document.getElementById("search");
const locateBtn = document.getElementById("locate");
const radiusEl = document.getElementById("radius");
const statusEl = document.getElementById("status");

function setStatus(msg) { statusEl.textContent = msg || ""; }

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function haversineKm(a, b) {
  const R = 6371;
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const x =
    Math.sin(dLat / 2) ** 2 +
    Math.sin(dLng / 2) ** 2 * Math.cos(lat1) * Math.cos(lat2);
  return 2 * R * Math.asin(Math.sqrt(x));
}

function popupHtml(g) {
  return `
    <strong>${escapeHtml(g.name)}</strong><br>
    <span class="badge ${g.platform}">${g.platform}</span>
    <p style="margin: 0.5rem 0 0.5rem;">${escapeHtml(g.description || "")}</p>
    <a class="join-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer">Open chat &#8599;</a>
  `;
}

async function loadGroups() {
  const res = await fetch("groups.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`Failed to load groups.json (${res.status})`);
  const data = await res.json();
  state.groups = data.filter(
    (g) =>
      PLATFORMS.includes(g.platform) &&
      Number.isFinite(g.lat) &&
      Number.isFinite(g.lng)
  );
  for (const g of state.groups) {
    const m = L.marker([g.lat, g.lng]).bindPopup(popupHtml(g));
    m.on("click", () => highlightInList(g.id));
    state.markers.set(g.id, m);
    m.addTo(map);
  }
  if (state.groups.length) {
    const bounds = L.latLngBounds(state.groups.map((g) => [g.lat, g.lng]));
    map.fitBounds(bounds.pad(0.2));
  }
  requestAnimationFrame(() => map.invalidateSize());
  window.addEventListener("resize", () => map.invalidateSize());
  applyFilters();
}

function applyFilters() {
  const q = state.query.trim().toLowerCase();
  const radius = state.radiusKm;
  let result = state.groups.filter((g) => {
    if (q) {
      const hay = `${g.name} ${g.description || ""} ${g.address || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (state.userPos && radius > 0) {
      const d = haversineKm(state.userPos, g);
      if (d > radius) return false;
    }
    return true;
  });

  if (state.userPos) {
    result = result
      .map((g) => ({ ...g, _distance: haversineKm(state.userPos, g) }))
      .sort((a, b) => a._distance - b._distance);
  } else {
    result = [...result].sort((a, b) => a.name.localeCompare(b.name));
  }

  state.filtered = result;
  renderList();
  syncMarkerVisibility();
}

function syncMarkerVisibility() {
  const visibleIds = new Set(state.filtered.map((g) => g.id));
  for (const [id, m] of state.markers) {
    const shouldBeVisible = visibleIds.has(id);
    const isVisible = map.hasLayer(m);
    if (shouldBeVisible && !isVisible) m.addTo(map);
    else if (!shouldBeVisible && isVisible) map.removeLayer(m);
  }
}

function renderList() {
  listEl.innerHTML = "";
  if (state.filtered.length === 0) {
    const li = document.createElement("li");
    li.textContent = "No groups match. Try a different search or wider radius.";
    li.style.cursor = "default";
    listEl.appendChild(li);
    return;
  }
  for (const g of state.filtered) {
    const li = document.createElement("li");
    li.dataset.id = g.id;
    li.innerHTML = `
      <div class="row">
        <h3>${escapeHtml(g.name)}</h3>
        <span class="distance">${g._distance != null ? g._distance.toFixed(1) + " km" : ""}</span>
      </div>
      <div class="row">
        <span class="badge ${g.platform}">${g.platform}</span>
        <span class="address">${escapeHtml(g.address || "")}</span>
      </div>
      <p>${escapeHtml(g.description || "")}</p>
      <a class="join-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer">Open chat &#8599;</a>
    `;
    li.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
      const marker = state.markers.get(g.id);
      if (marker) {
        map.setView([g.lat, g.lng], Math.max(map.getZoom(), 11));
        marker.openPopup();
      }
      highlightInList(g.id);
    });
    listEl.appendChild(li);
  }
}

function highlightInList(id) {
  for (const li of listEl.querySelectorAll("li")) {
    li.classList.toggle("active", li.dataset.id === id);
  }
  const target = listEl.querySelector(`li[data-id="${CSS.escape(id)}"]`);
  if (target) target.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

searchEl.addEventListener("input", () => {
  state.query = searchEl.value;
  applyFilters();
});

radiusEl.addEventListener("change", () => {
  state.radiusKm = Number(radiusEl.value) || 0;
  applyFilters();
});

locateBtn.addEventListener("click", () => {
  if (!navigator.geolocation) {
    setStatus("Geolocation isn't supported by your browser.");
    return;
  }
  setStatus("Locating…");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      state.userPos = { lat: pos.coords.latitude, lng: pos.coords.longitude };
      if (state.userMarker) map.removeLayer(state.userMarker);
      state.userMarker = L.circleMarker(
        [state.userPos.lat, state.userPos.lng],
        { radius: 8, color: "#d32f2f", fillColor: "#d32f2f", fillOpacity: 0.8 }
      ).addTo(map).bindPopup("You are here");
      map.setView([state.userPos.lat, state.userPos.lng], 11);
      setStatus("Located. Sorting by distance.");
      applyFilters();
    },
    (err) => setStatus(`Couldn't get location: ${err.message}`),
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 60000 }
  );
});

loadGroups().catch((e) => setStatus(e.message));
