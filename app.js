const PLATFORMS = ["telegram", "whatsapp", "signal"];
const CATEGORIES = ["circle", "pantry", "free-food"];
const STORAGE_KEY = "sharedpot:categories";

const state = {
  groups: [],
  filtered: [],
  markers: new Map(),
  userPos: null,
  userMarker: null,
  query: "",
  radiusKm: 0,
  visibleCategories: loadCategoryPrefs(),
};

const map = L.map("map", {
  worldCopyJump: true,
  maxBounds: L.latLngBounds([[-85, -10000], [85, 10000]]),
  maxBoundsViscosity: 1.0,
  minZoom: 2,
  maxZoom: 19,
}).setView([20, 0], 2);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);

const listEl = document.getElementById("group-list");
const searchEl = document.getElementById("search");
const locateBtn = document.getElementById("locate");
const radiusEl = document.getElementById("radius");
const statusEl = document.getElementById("status");
const filterEls = document.querySelectorAll('#category-filters input[type="checkbox"]');

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

function loadCategoryPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return new Set(CATEGORIES);
    const arr = JSON.parse(raw);
    return new Set(arr.filter((c) => CATEGORIES.includes(c)));
  } catch {
    return new Set(CATEGORIES);
  }
}

function saveCategoryPrefs() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify([...state.visibleCategories]));
  } catch { /* ignore quota errors */ }
}

function ctaLabel(g) {
  return g.category === "circle" ? "Open chat &#8599;" : "Visit listing &#8599;";
}

function badgeHtml(g) {
  if (g.category === "circle" && PLATFORMS.includes(g.platform)) {
    return `<span class="badge ${g.platform}">${g.platform}</span>`;
  }
  if (g.category === "pantry") return `<span class="badge cat-pantry">Food pantry</span>`;
  if (g.category === "free-food") return `<span class="badge cat-free-food">Free food</span>`;
  return `<span class="badge cat-circle">Circle</span>`;
}

function sourceHtml(g) {
  if (!g.source || !g.source.name) return "";
  const url = g.source.url ? escapeHtml(g.source.url) : null;
  const name = escapeHtml(g.source.name);
  return `<p class="source-attribution">via ${url ? `<a href="${url}" target="_blank" rel="noopener noreferrer">${name}</a>` : name}</p>`;
}

function popupHtml(g) {
  return `
    <strong>${escapeHtml(g.name)}</strong><br>
    ${badgeHtml(g)}
    <p style="margin: 0.5rem 0 0.5rem;">${escapeHtml(g.description || "")}</p>
    ${sourceHtml(g)}
    <a class="join-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer">${ctaLabel(g)}</a>
  `;
}

function markerIcon(category) {
  const cls = CATEGORIES.includes(category) ? `cat-${category}` : "cat-circle";
  return L.divIcon({
    className: "cat-marker",
    html: `<div class="cat-pin ${cls}"></div>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
    popupAnchor: [0, -12],
  });
}

function normalize(entries, defaultCategory) {
  return entries
    .map((g) => ({ ...g, category: g.category || defaultCategory }))
    .filter(
      (g) =>
        CATEGORIES.includes(g.category) &&
        Number.isFinite(g.lat) &&
        Number.isFinite(g.lng) &&
        typeof g.id === "string"
    );
}

async function fetchJson(path, optional) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) {
    if (optional && res.status === 404) {
      console.info(`${path} not found (yet) — skipping.`);
      return [];
    }
    throw new Error(`Failed to load ${path} (${res.status})`);
  }
  return res.json();
}

async function loadGroups() {
  const [circles, foodResources] = await Promise.all([
    fetchJson("groups.json").then((d) => normalize(d, "circle")),
    fetchJson("food_resources.json", true).then((d) => normalize(d, "pantry")).catch((e) => {
      console.warn("food_resources.json failed:", e);
      return [];
    }),
  ]);
  state.groups = [...circles, ...foodResources];

  for (const g of state.groups) {
    const m = L.marker([g.lat, g.lng], { icon: markerIcon(g.category) }).bindPopup(popupHtml(g));
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
  const cats = state.visibleCategories;
  let result = state.groups.filter((g) => {
    if (!cats.has(g.category)) return false;
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
    li.textContent = "No matches. Try a different search, wider radius, or enable more categories.";
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
        ${badgeHtml(g)}
        <span class="address">${escapeHtml(g.address || "")}</span>
      </div>
      <p>${escapeHtml(g.description || "")}</p>
      ${sourceHtml(g)}
      <a class="join-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer">${ctaLabel(g)}</a>
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

for (const cb of filterEls) {
  const cat = cb.dataset.category;
  cb.checked = state.visibleCategories.has(cat);
  cb.addEventListener("change", () => {
    if (cb.checked) state.visibleCategories.add(cat);
    else state.visibleCategories.delete(cat);
    saveCategoryPrefs();
    applyFilters();
  });
}

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
