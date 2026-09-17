const RESULTS_LIMIT = 12;
const COMPARE_LIMIT = 8;
const COMPARED_MODES = ["fulltext", "vector", "hybrid"];
const GEO_LIMIT = 100;
const GEO_CENTER = { lat: 40.7128, lon: -74.006 };
const DEFAULT_RADIUS_KM = 10;
const LEAFLET_BASE = "https://unpkg.com/leaflet@1.9.4/dist";
const AUTOCOMPLETE_DELAY_MS = 150;
const LIVE_SEARCH_DELAY_MS = 300;
// Matches MAX_PHOTO_BYTES in app.py.
const MAX_PHOTO_BYTES = 5 * 1024 * 1024;
const COPIED_MS = 1500;
// Matches the 2.5rem edge fade on .modes in styles.css.
const MODES_FADE_PX = 40;
// Words under 3 letters would mark half of every title.
const MIN_HIGHLIGHT_LENGTH = 3;
const TABLE = "convapparel_products";
const CHAT_MODEL = "shopping_assistant";
const VECTOR_FIELD = "embedding_vector";

const PLACEHOLDERS = { image: "Describe a look, or drop or paste a photo", chat: "Ask a shopping question" };

const MODES = {
  fulltext: {
    label: "Full-text",
    examples: ["lether jaket", "waterprof hiking boots", "linen shirt"],
    explain:
      "Ranks products by how well their title, description and features match your words (BM25). With typo tolerance on, OPTION fuzzy=1 also matches words a letter or two away, and CALL QSUGGEST shows the corrected query.",
  },
  vector: {
    label: "Vector",
    examples: ["outfit for a job interview", "something warm for a winter walk", "clothes for a hot day at the beach"],
    explain:
      "Manticore turns your query into an embedding with the all-MiniLM-L12-v2 model and finds the nearest product embeddings in an HNSW index. Products can match without sharing a single word.",
  },
  hybrid: {
    label: "Hybrid",
    examples: ["comfy shoes for standing all day", "warm waterprof jacket", "summer dress for a beach wedding"],
    explain:
      "Runs the full-text and vector searches in parallel and merges both rankings with Reciprocal Rank Fusion (OPTION fusion_method='rrf'). Each product shows which search found it.",
  },
  image: {
    label: "Image",
    examples: ["red plaid flannel shirt", "white sneakers with a gum sole", "floral summer dress"],
    explain:
      "Fashion CLIP turns your words or photo into a vector in the same space as the product photos. Manticore finds the nearest photo vectors in an HNSW index, then loads those products by id. Upload, drop or paste a photo to search by look.",
  },
  geo: {
    label: "Geo",
    examples: [],
    explain:
      "Every product carries latitude and longitude floats. GEODIST() measures the distance from your pin and filters to the radius you choose — the same query that powers “near me” search. Move the pin, widen the circle, and watch the SQL and results follow.",
  },
  compare: {
    label: "Compare",
    examples: ["comfy shoes for standing all day", "lether jaket", "outfit for a job interview"],
    explain:
      "Sends the same query to full-text, vector and hybrid search. Products found by more than one of them are marked, and pointing at a product highlights it in every list.",
  },
  chat: {
    label: "Ask AI",
    examples: ["Comfortable sneakers I can walk in all day", "A warm jacket for rainy autumn days", "What should I wear to a summer wedding?"],
    explain:
      "CALL CHAT rewrites your question into a search query, retrieves matching products with vector search, and an LLM writes the answer from those products only. Numbers in the answer open the products they come from.",
  },
};

const $ = (id) => document.getElementById(id);
const els = {
  modes: document.querySelector(".modes"),
  tabs: Array.from(document.querySelectorAll('.modes [role="tab"]')),
  form: $("search-form"),
  intro: document.querySelector(".intro"),
  queryBox: $("query-box"),
  chatNote: $("chat-note"),
  query: $("query"),
  submit: $("submit"),
  suggestions: $("suggestions"),
  fuzzy: $("fuzzy"),
  live: $("live"),
  categories: $("categories"),
  examples: $("examples"),
  results: $("results"),
  status: $("status"),
  output: $("output"),
  sql: $("sql"),
  request: $("request"),
  explain: $("explain"),
  dialog: $("product"),
  productImage: $("product-image"),
  productCategory: $("product-category"),
  productTitle: $("product-title"),
  productDescription: $("product-description"),
  productFeatures: $("product-features"),
  similar: $("similar"),
  similarSql: $("similar-sql"),
  similarPhoto: $("similar-photo"),
  similarPhotoSql: $("similar-photo-sql"),
  photoInput: $("photo-input"),
  photoPreview: $("photo-preview"),
  photoImage: $("photo-image"),
  photoClear: $("photo-clear"),
  geoRadius: $("geo-radius"),
  geoRadiusValue: $("geo-radius-value"),
  geoReset: $("geo-reset"),
};

const params = new URLSearchParams(location.search);
const state = {
  mode: MODES[params.get("mode")] ? params.get("mode") : "hybrid",
  categories: new Set((params.get("category") || "").split(",").filter(Boolean)),
  fuzzy: params.get("fuzzy") !== "0",
  conversation: null,
  turns: [],
  photo: null,
  geo: {
    lat: Number(params.get("lat")) || GEO_CENTER.lat,
    lon: Number(params.get("lon")) || GEO_CENTER.lon,
    radius: Math.min(Math.max(Number(params.get("radius")) || DEFAULT_RADIUS_KM, 0.5), 25),
    map: null,
    markers: null,
    pin: null,
    circle: null,
    // Last pin+radius the map was fitted to; refitting only when it changes preserves the user's zoom.
    view: null,
  },
};
const products = new Map();
let countedQuery = null;
let questionBank = null;
let searchController = null;
let suggestController = null;
let similarController = null;
let suggestTimer = null;
let liveTimer = null;

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

// Mirrors sql_quote() in app.py, so the CALL CHAT shown matches what the server sends.
function sqlString(value) {
  return `'${value.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;
}

function shellQuote(value) {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

// Amazon serves any width from the same URL; the originals are up to 1500px wide.
function thumbnail(url, width) {
  return url.replace(/\._AC_[A-Z0-9_]+_\./, `._AC_UL${width}_.`);
}

function highlight(text, terms) {
  const words = terms.filter((term) => term.length >= MIN_HIGHLIGHT_LENGTH);
  const safe = escapeHtml(text);
  return words.length ? safe.replace(new RegExp(`\\b((?:${words.join("|")})\\w*)`, "gi"), "<mark>$1</mark>") : safe;
}

function highlightSql(sql) {
  return sql
    .split(/('(?:\\.|[^'\\])*')/)
    .map((part, index) =>
      index % 2
        ? `<span class="sql-str">${escapeHtml(part)}</span>`
        : escapeHtml(part)
            .replace(/\s+(FROM|WHERE|LIMIT|OPTION|FACET)\b/g, "\n$1")
            .replace(/\s+AND\b/g, "\n  AND")
            .replace(/\b(SELECT|FROM|WHERE|AND|LIMIT|OPTION|FACET|ORDER BY|DESC|AS|CALL)\b/g, '<span class="sql-kw">$1</span>'),
    )
    .join("");
}

async function api(path, options = {}, signal = undefined) {
  const response = await fetch(path, { ...options, signal });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail) ? body.detail.map((item) => item.msg).join("; ") : body.detail;
    throw new Error(detail || `The server answered with HTTP ${response.status}.`);
  }
  return body;
}

function categoryParam() {
  return [...state.categories].join(",");
}

function searchPath(mode, query, limit) {
  const search = new URLSearchParams({ q: query, mode, limit });
  const category = categoryParam();
  if (category) search.set("category", category);
  if (!state.fuzzy) search.set("fuzzy", "false");
  return `/api/search?${search}`;
}

function geoPath(limit) {
  const search = new URLSearchParams({
    mode: "geo",
    lat: state.geo.lat.toFixed(6),
    lon: state.geo.lon.toFixed(6),
    radius: state.geo.radius,
    limit,
  });
  const category = categoryParam();
  if (category) search.set("category", category);
  return `/api/search?${search}`;
}

function photoPath(limit) {
  const search = new URLSearchParams({ limit });
  const category = categoryParam();
  if (category) search.set("category", category);
  return `/api/search/image?${search}`;
}

function signalLabel(hit, mode) {
  if (mode === "hybrid") {
    if (hit.matched_words && hit.similarity !== null) return "Words + meaning";
    return hit.matched_words ? "Words" : "Meaning";
  }
  return hit.similarity === null ? "" : `Similarity ${hit.similarity.toFixed(2)}`;
}

function productCard(hit, rank, terms, mode) {
  products.set(hit.id, hit);
  const signal = signalLabel(hit, mode);
  // Cards sit under the page h1, or under an h3 section title inside the product dialog.
  const heading = mode === "similar" ? "h4" : "h2";
  return `<article class="card">
    <div class="card-media"><span class="rank">${rank}</span><img src="${escapeHtml(thumbnail(hit.image_url, 320))}" alt="" loading="lazy" decoding="async"></div>
    <div class="card-body">
      <p class="card-category">${escapeHtml(hit.category)}</p>
      <${heading} class="card-title"><button type="button" class="card-open" data-product="${escapeHtml(hit.id)}">${highlight(hit.title, terms)}</button></${heading}>
      <p class="card-features">${highlight(hit.features, terms)}</p>
      ${signal ? `<p class="signal">${signal}</p>` : ""}
    </div>
  </article>`;
}

function correctedNote(result, className) {
  return result.corrected ? `<span class="${className}">Showing results for <strong>${escapeHtml(result.corrected)}</strong></span>` : "";
}

function setBusy(busy) {
  if (busy) els.results.setAttribute("aria-busy", "true");
  else els.results.removeAttribute("aria-busy");
}

function showInspector(sqls, request) {
  els.sql.innerHTML = sqls.map(highlightSql).join("\n\n");
  els.sql.dataset.raw = sqls.join(";\n");
  els.request.textContent = request;
  els.request.dataset.raw = request;
}

function syncUrl(query) {
  const search = new URLSearchParams({ mode: state.mode });
  if (query) search.set("q", query);
  const category = categoryParam();
  if (category) search.set("category", category);
  if (!state.fuzzy) search.set("fuzzy", "0");
  if (state.mode === "geo") {
    search.set("lat", state.geo.lat.toFixed(6));
    search.set("lon", state.geo.lon.toFixed(6));
    search.set("radius", state.geo.radius);
  }
  history.replaceState(null, "", `?${search}`);
}

function updateCounts(result) {
  // A filtered search only counts its own category, so keep the counts from the unfiltered one.
  if (state.categories.size && result.query === countedQuery && result.mode === "fulltext") return;
  const counts = new Map(state.categories.size ? [] : (result.facets || []).map((facet) => [facet.value, facet.count]));
  countedQuery = result.query;
  els.categories.querySelectorAll("[data-count]").forEach((el) => {
    el.textContent = counts.has(el.dataset.count) ? counts.get(el.dataset.count).toLocaleString("en") : "";
  });
}

async function submit() {
  const query = els.query.value.trim();
  const byPhoto = state.mode === "image" && state.photo;
  if (!query && !byPhoto && state.mode !== "geo") return;
  searchController?.abort();
  const controller = (searchController = new AbortController());
  syncUrl(byPhoto ? "" : query);
  setBusy(true);
  try {
    if (state.mode === "chat") await runChat(query, controller.signal);
    else if (state.mode === "compare") await runCompare(query, controller.signal);
    else if (state.mode === "geo") await runGeo(controller.signal);
    else await runSearch(query, controller.signal);
  } catch (error) {
    if (error.name === "AbortError") return;
    els.status.innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
    // Results from the previous search would read as answers to this one.
    // Results from the previous search would read as answers to this one; the geo map stays.
    if (state.mode !== "chat" && state.mode !== "geo") els.output.innerHTML = "";
  } finally {
    if (controller === searchController) setBusy(false);
  }
}

async function runSearch(query, signal) {
  const photo = state.mode === "image" ? state.photo : null;
  const path = photo ? photoPath(RESULTS_LIMIT) : searchPath(state.mode, query, RESULTS_LIMIT);
  const result = photo
    ? await api(path, { method: "POST", headers: { "Content-Type": photo.type }, body: photo }, signal)
    : await api(path, {}, signal);
  const request = photo
    ? `curl -X POST ${shellQuote(location.origin + path)} \\\n  -H ${shellQuote(`Content-Type: ${photo.type}`)} \\\n  --data-binary @${shellQuote(photo.name)}`
    : `curl ${shellQuote(location.origin + path)}`;
  showInspector([result.sql], request);
  updateCounts(result);
  const rankedBy = { vector: "meaning", hybrid: "words and meaning", image: "look" }[result.mode];
  const found =
    result.mode === "fulltext"
      ? `${result.total.toLocaleString("en")} ${result.total === 1 ? "product" : "products"}`
      : `Top ${result.hits.length} by ${rankedBy}`;
  const embedded = result.mode === "image" ? `<span>${photo ? "Photo" : "Query"} turned into a vector in ${result.embed_ms} ms</span>` : "";
  els.status.innerHTML = `${correctedNote(result, "corrected")}<span>${found} in ${result.took_ms} ms</span>${embedded}`;
  els.output.innerHTML = result.hits.length
    ? `<div class="grid">${result.hits.map((hit, index) => productCard(hit, index + 1, result.terms, result.mode)).join("")}</div>`
    : `<p class="empty">No products match. Try fewer words${result.mode === "fulltext" && !result.fuzzy ? " or turn on typo tolerance" : ""}.</p>`;
}

function loadLeaflet() {
  if (!loadLeaflet.promise) {
    loadLeaflet.promise = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = `${LEAFLET_BASE}/leaflet.css`;
      document.head.appendChild(css);
      const script = document.createElement("script");
      script.src = `${LEAFLET_BASE}/leaflet.js`;
      script.onload = () => resolve(window.L);
      script.onerror = () => reject(new Error("Could not load the map library. Check your connection and reload."));
      document.head.appendChild(script);
    });
  }
  return loadLeaflet.promise;
}

async function ensureGeoMap() {
  const L = await loadLeaflet();
  // Other modes replace els.output, which detaches the map container. The Leaflet instance survives
  // but is wired to orphaned DOM: clicks never reach it and its old markers stay, so the pin cannot
  // move and every marker opens the product it held on the previous render. Rebuild when detached.
  if (state.geo.map && !state.geo.map.getContainer().isConnected) {
    state.geo.map.remove();
    state.geo.map = null;
    state.geo.view = null;
  }
  if (state.geo.map) return L;
  els.output.innerHTML = `<div class="geo-split">
    <div id="geo-map" class="geo-map"></div>
    <div id="geo-list" class="geo-list"></div>
  </div>`;
  const { lat, lon, radius } = state.geo;
  // The sticky topbar covers the map's top edge while the page scrolls, so the zoom control moves to the bottom-right corner.
  // Wheel zoom stays off until the map is clicked: otherwise the map swallows the page's scroll the moment the cursor crosses it.
  state.geo.map = L.map("geo-map", { zoomControl: false, scrollWheelZoom: false }).setView([lat, lon], 11);
  L.control.zoom({ position: "bottomright" }).addTo(state.geo.map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
  }).addTo(state.geo.map);
  // interactive:false keeps clicks inside the circle reaching the map, so clicking anywhere sets the location.
  state.geo.circle = L.circle([lat, lon], { radius: radius * 1000, color: "#879e2a", weight: 2, fillColor: "#879e2a", fillOpacity: 0.08, interactive: false }).addTo(state.geo.map);
  state.geo.markers = L.layerGroup().addTo(state.geo.map);
  state.geo.pin = L.marker([lat, lon], { draggable: true, title: "Your location" }).addTo(state.geo.map);
  state.geo.pin.on("dragend", () => setGeoLocation(state.geo.pin.getLatLng()));
  state.geo.map.on("click", (event) => {
    state.geo.map.scrollWheelZoom.enable();
    setGeoLocation(event.latlng);
  });
  // Leaving the map hands the wheel back to the page.
  state.geo.map.on("mouseout", () => state.geo.map.scrollWheelZoom.disable());
  return L;
}

function setGeoLocation(latlng) {
  state.geo.lat = latlng.lat;
  state.geo.lon = latlng.lng;
  submit();
}

function geoItem(hit) {
  products.set(hit.id, hit);
  return `<li class="geo-item">
    <img src="${escapeHtml(thumbnail(hit.image_url, 160))}" alt="" loading="lazy" decoding="async">
    <div><button type="button" class="card-open" data-product="${escapeHtml(hit.id)}">${escapeHtml(hit.title)}</button>
    <span class="geo-item-meta">${escapeHtml(hit.category)} · ${hit.distance_km} km away</span></div>
  </li>`;
}

async function runGeo(signal) {
  const L = await ensureGeoMap();
  const path = geoPath(GEO_LIMIT);
  const result = await api(path, {}, signal);
  showInspector([result.sql], `curl ${shellQuote(location.origin + path)}`);
  updateCounts(result);
  const list = document.getElementById("geo-list");
  // The SQL samples by id so pins spread across the whole circle; the list still reads nearest-first.
  const byDistance = [...result.hits].sort((a, b) => a.distance_km - b.distance_km);
  list.innerHTML = byDistance.length
    ? `<ol class="geo-list-items">${byDistance.map(geoItem).join("")}</ol>`
    : '<p class="empty">No products within the radius. Widen the circle or move the pin.</p>';
  const { map, markers, circle, pin } = state.geo;
  circle.setLatLng([state.geo.lat, state.geo.lon]).setRadius(state.geo.radius * 1000);
  pin.setLatLng([state.geo.lat, state.geo.lon]);
  markers.clearLayers();
  result.hits.forEach((hit) => {
    products.set(hit.id, hit);
    L.marker([hit.lat, hit.lon], {
      icon: L.divIcon({
        className: "geo-marker",
        html: `<img src="${escapeHtml(thumbnail(hit.image_url, 96))}" alt="${escapeHtml(hit.title)}">`,
        iconSize: [40, 40],
        // Without an anchor Leaflet hangs the icon box from its top-left corner, so the disc sits
        // down-right of its real coordinate and overlaps its neighbours: clicks then land on
        // whichever box is stacked on top, which is why every pin opened the same product.
        iconAnchor: [20, 20],
      }),
      title: `${hit.title} — ${hit.distance_km} km`,
      riseOnHover: true,
    })
      .on("click", () => openProduct(hit.id))
      .addTo(markers);
  });
  // Fit the circle only when the search itself moved or resized it; refitting on every render would
  // undo the zoom the user just set with the wheel or the +/- control.
  const view = `${state.geo.lat.toFixed(5)},${state.geo.lon.toFixed(5)},${state.geo.radius}`;
  if (state.geo.view !== view) {
    state.geo.view = view;
    // The extra top padding keeps the fitted view clear of the sticky topbar on the standalone page.
    map.fitBounds(circle.getBounds(), {
      paddingTopLeft: [24, document.body.classList.contains("is-embedded") ? 24 : 80],
      paddingBottomRight: [24, 24],
      maxZoom: 14,
    });
  }
  const total = result.total ?? result.hits.length;
  els.status.innerHTML = `<span>${total.toLocaleString("en")} ${total === 1 ? "product" : "products"} within ${state.geo.radius} km in ${result.took_ms} ms</span>`;
}

async function runCompare(query, signal) {
  const paths = COMPARED_MODES.map((mode) => searchPath(mode, query, COMPARE_LIMIT));
  const results = await Promise.all(paths.map((path) => api(path, {}, signal)));
  const foundBy = new Map();
  results.forEach((result) => result.hits.forEach((hit) => foundBy.set(hit.id, (foundBy.get(hit.id) || 0) + 1)));
  showInspector(
    results.map((result) => result.sql),
    paths.map((path) => `curl ${shellQuote(location.origin + path)}`).join("\n"),
  );
  updateCounts(results[0]);
  els.status.innerHTML = "<span>The same query in three search types. Products found by more than one are marked.</span>";
  els.output.innerHTML = `<div class="compare">${results.map((result) => compareColumn(result, foundBy)).join("")}</div>`;
}

function compareColumn(result, foundBy) {
  const items = result.hits
    .map((hit) => {
      products.set(hit.id, hit);
      const count = foundBy.get(hit.id);
      const shared = count > 1 ? `<span class="shared">${count === COMPARED_MODES.length ? "Found by all three" : "Found by two"}</span>` : "";
      return `<li class="compare-item" data-id="${escapeHtml(hit.id)}">
        <img src="${escapeHtml(thumbnail(hit.image_url, 160))}" alt="" loading="lazy" decoding="async">
        <div><button type="button" class="card-open" data-product="${escapeHtml(hit.id)}">${highlight(hit.title, result.terms)}</button>${shared}</div>
      </li>`;
    })
    .join("");
  return `<section class="compare-col">
    <header><h2>${MODES[result.mode].label}</h2><span>${result.took_ms} ms</span></header>
    ${result.corrected ? `<p class="compare-note">${correctedNote(result, "corrected")}</p>` : ""}
    ${items ? `<ol class="compare-list">${items}</ol>` : '<p class="empty">No matches</p>'}
  </section>`;
}

function chatSql(message) {
  return `CALL CHAT(${[message, TABLE, CHAT_MODEL, state.conversation || "", VECTOR_FIELD].map(sqlString).join(", ")})`;
}

function chatRequest(message) {
  const body = JSON.stringify({ message, conversation_uuid: state.conversation });
  return `curl -X POST ${shellQuote(`${location.origin}/api/assistant/chat`)} \\\n  -H 'Content-Type: application/json' \\\n  -d ${shellQuote(body)}`;
}

function turnHtml({ message, result }) {
  const sources = result.sources.map((source) => ({ ...source, id: String(source.id), matched_words: false, similarity: null }));
  let answer = escapeHtml(result.response);
  sources.forEach((source, index) => {
    const ref = `<button type="button" class="ref" data-product="${escapeHtml(source.id)}" aria-label="Product ${index + 1}: ${escapeHtml(source.title)}">${index + 1}</button>`;
    answer = answer.split(`[ref:${source.id}]`).join(ref);
  });
  const paragraphs = answer
    .replace(/\[ref:[^\]]*\]/g, "")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .split(/\n\s*\n/)
    .map((paragraph) => `<p>${paragraph.replace(/\n/g, "<br>")}</p>`)
    .join("");
  return `<article class="turn">
    <p class="turn-question">${escapeHtml(message)}</p>
    <div class="answer">${paragraphs}</div>
    ${result.search_query ? `<p class="turn-search">Manticore searched for <strong>${escapeHtml(result.search_query)}</strong></p>` : ""}
    <div class="grid grid--sources">${sources.map((source, index) => productCard(source, index + 1, [], "chat")).join("")}</div>
  </article>`;
}

function renderChat() {
  els.output.innerHTML = state.turns.length
    ? state.turns.map(turnHtml).join("")
    : '<p class="empty">Ask a shopping question. Manticore finds matching products, and the AI answers using only those products.</p>';
  placeQueryBox();
}

// Once a conversation exists, the question box moves under its latest answer so it reads as a reply, not a new search.
function placeQueryBox() {
  const threaded = state.mode === "chat" && state.turns.length > 0;
  els.chatNote.hidden = !threaded;
  // An example would drop an unrelated question into the conversation.
  els.examples.hidden = threaded;
  if (threaded === (els.queryBox.parentElement === els.results)) return;
  // Moving the box takes focus out of the input.
  const focused = els.queryBox.contains(document.activeElement);
  if (threaded) els.results.append(els.queryBox);
  else els.intro.after(els.queryBox);
  if (focused) els.query.focus({ preventScroll: true });
}

function showChat() {
  const message = els.query.value.trim();
  syncUrl(message);
  showInspector([chatSql(message)], chatRequest(message));
  els.status.innerHTML = "<span>Answers take 5 to 15 seconds.</span>";
  renderChat();
}

async function runChat(message, signal) {
  showInspector([chatSql(message)], chatRequest(message));
  els.status.innerHTML = "<span>Finding products and writing an answer. This takes 5 to 15 seconds.</span>";
  const result = await api(
    "/api/assistant/chat",
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, conversation_uuid: state.conversation }) },
    signal,
  );
  state.conversation = result.conversation_uuid;
  state.turns.push({ message, result });
  els.status.innerHTML = `<span>Answer based on ${result.sources.length} products</span>`;
  renderChat();
  els.query.value = "";
  els.query.placeholder = "Ask a follow-up question";
  els.output.querySelector(".turn:last-of-type").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// Every homepage visitor sees the same answer, so it continues as a copy of its own.
async function continueConversation(conversation, sources) {
  searchController?.abort();
  const controller = (searchController = new AbortController());
  setBusy(true);
  // The box and the URL still hold the chat tab's example question.
  els.query.value = "";
  syncUrl("");
  els.status.innerHTML = "<span>Opening the conversation…</span>";
  try {
    const copy = await api(
      `/api/assistant/conversations/${encodeURIComponent(conversation)}/copy`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sources: sources ? sources.split(",") : null }) },
      controller.signal,
    );
    const last = copy.turns[copy.turns.length - 1];
    showInspector([chatSql(last.message)], chatRequest(last.message));
    state.conversation = copy.conversation_uuid;
    state.turns = copy.turns.map((turn) => ({ message: turn.message, result: turn }));
    els.status.innerHTML = "<span>Continuing your conversation from manticoresearch.com</span>";
    renderChat();
    els.query.placeholder = "Ask a follow-up question";
    els.query.focus({ preventScroll: true });
  } catch (error) {
    if (error.name !== "AbortError") els.status.innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
  } finally {
    if (controller === searchController) setBusy(false);
  }
}

async function openProduct(id) {
  const hit = products.get(id);
  els.productImage.src = thumbnail(hit.image_url, 640);
  els.productImage.alt = hit.title;
  els.productCategory.textContent = hit.category;
  els.productTitle.textContent = hit.title;
  els.productDescription.textContent = hit.description;
  els.productFeatures.innerHTML = hit.features
    .split(/,\s*/)
    .filter(Boolean)
    .map((feature) => `<li>${escapeHtml(feature)}</li>`)
    .join("");
  if (!els.dialog.open) els.dialog.showModal();
  els.dialog.scrollTop = 0;

  similarController?.abort();
  similarController = new AbortController();
  const path = `/api/similar/${encodeURIComponent(id)}`;
  loadSimilar(path, els.similar, els.similarSql, similarController.signal);
  loadSimilar(`${path}?by=photo`, els.similarPhoto, els.similarPhotoSql, similarController.signal);
}

async function loadSimilar(path, grid, sql, signal) {
  grid.innerHTML = '<p class="muted">Finding similar products…</p>';
  sql.textContent = "";
  try {
    const result = await api(path, {}, signal);
    grid.innerHTML = result.hits.length
      ? `<div class="grid">${result.hits.map((similar, index) => productCard(similar, index + 1, [], "similar")).join("")}</div>`
      : '<p class="muted">No similar products found.</p>';
    sql.innerHTML = highlightSql(result.sql);
  } catch (error) {
    if (error.name !== "AbortError") grid.innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
  }
}

function setPhoto(file) {
  if (!file.type.startsWith("image/") || file.size > MAX_PHOTO_BYTES) {
    els.status.innerHTML = '<span class="error">Choose an image file up to 5 MB.</span>';
    return;
  }
  clearPhoto();
  state.photo = file;
  els.photoImage.src = URL.createObjectURL(file);
  els.photoPreview.hidden = false;
  if (state.mode === "image") submit();
  else setMode("image");
  els.query.value = "";
  els.query.placeholder = "Type to search by words instead";
}

function clearPhoto() {
  if (!state.photo) return;
  URL.revokeObjectURL(els.photoImage.src);
  state.photo = null;
  els.photoPreview.hidden = true;
  els.photoInput.value = "";
  els.query.placeholder = PLACEHOLDERS.image;
}

function closeSuggestions() {
  clearTimeout(suggestTimer);
  suggestController?.abort();
  els.suggestions.hidden = true;
  els.suggestions.innerHTML = "";
  els.query.setAttribute("aria-expanded", "false");
  els.query.removeAttribute("aria-activedescendant");
}

function showSuggestions(query, suggestions) {
  // Completions extend the typed text, so the untyped rest carries the weight.
  els.suggestions.innerHTML = suggestions
    .map((suggestion, index) => `<li id="suggestion-${index}" role="option" aria-selected="false">${escapeHtml(suggestion.slice(0, query.length))}<b>${escapeHtml(suggestion.slice(query.length))}</b></li>`)
    .join("");
  els.suggestions.hidden = !suggestions.length;
  els.query.setAttribute("aria-expanded", String(suggestions.length > 0));
  els.query.removeAttribute("aria-activedescendant");
}

function fadeModes() {
  const { modes } = els;
  modes.classList.toggle("more-start", modes.scrollLeft > 1);
  modes.classList.toggle("more-end", modes.scrollLeft + modes.clientWidth < modes.scrollWidth - 1);
}

// Links can open any search type, so on narrow screens the selected one scrolls into view.
function revealTab(tab) {
  const { modes } = els;
  const left = tab.getBoundingClientRect().left - modes.getBoundingClientRect().left;
  const hidden = (left < MODES_FADE_PX && modes.scrollLeft > 0) || left + tab.offsetWidth > modes.clientWidth - MODES_FADE_PX;
  if (hidden) modes.scrollLeft += left - (modes.clientWidth - tab.offsetWidth) / 2;
  fadeModes();
}

async function randomQuestion() {
  questionBank ??= api("/static/example_questions.json");
  const questions = (await questionBank).flatMap((example) => example.questions.map((question) => question.text));
  return questions[Math.floor(Math.random() * questions.length)];
}

function renderExamples() {
  if (!MODES[state.mode].examples.length) {
    els.examples.innerHTML = "";
    return;
  }
  const buttons = MODES[state.mode].examples.map((example) => `<button type="button" data-example="${escapeHtml(example)}">${escapeHtml(example)}</button>`);
  if (state.mode === "chat") buttons.push('<button type="button" data-random>Random shopper question</button>');
  els.examples.innerHTML = `<span>Try</span>${buttons.join("")}`;
}

function setMode(mode) {
  // Search types share a query so their results can be compared; a question for the AI and a map pin read differently.
  const queryless = (kind) => kind === "chat" || kind === "geo";
  const switchesKind = queryless(mode) !== queryless(state.mode);
  if (mode !== "image") clearPhoto();
  state.mode = mode;
  document.body.dataset.mode = mode;
  els.tabs.forEach((tab) => {
    const selected = tab.dataset.tab === mode;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected) revealTab(tab);
  });
  els.fuzzy.disabled = mode === "vector" || mode === "image" || mode === "geo";
  els.live.disabled = mode === "chat" || mode === "image" || mode === "geo";
  els.submit.textContent = mode === "chat" ? "Ask AI" : "Search";
  els.query.placeholder = PLACEHOLDERS[mode] || "Search products";
  els.explain.textContent = MODES[mode].explain;
  renderExamples();
  placeQueryBox();
  // After a photo search the box is empty, so give the next search type something to run.
  const example = MODES[mode].examples[0];
  if (example && (switchesKind || !els.query.value.trim())) els.query.value = example;
  if (mode === "chat") showChat();
  else submit();
}

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  // A live search still waiting to fire would repeat this one.
  clearTimeout(liveTimer);
  closeSuggestions();
  submit();
});

els.tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => {
    if (tab.dataset.tab !== state.mode) setMode(tab.dataset.tab);
  });
  tab.addEventListener("keydown", (event) => {
    const step = { ArrowLeft: -1, ArrowRight: 1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = els.tabs[(index + step + els.tabs.length) % els.tabs.length];
    next.focus();
    setMode(next.dataset.tab);
  });
});

els.fuzzy.addEventListener("change", () => {
  state.fuzzy = els.fuzzy.checked;
  submit();
});

els.categories.addEventListener("change", (event) => {
  const input = event.target;
  if (input.checked) state.categories.add(input.value);
  else state.categories.delete(input.value);
  submit();
});

els.geoRadius.addEventListener("input", () => {
  state.geo.radius = Number(els.geoRadius.value);
  els.geoRadiusValue.textContent = `${state.geo.radius} km`;
  // The circle follows the thumb live; the query only reruns on release.
  if (state.geo.circle) state.geo.circle.setRadius(state.geo.radius * 1000);
});

els.geoRadius.addEventListener("change", () => {
  if (state.mode === "geo") submit();
});

els.geoReset.addEventListener("click", () => {
  state.geo.lat = GEO_CENTER.lat;
  state.geo.lon = GEO_CENTER.lon;
  state.geo.radius = DEFAULT_RADIUS_KM;
  els.geoRadius.value = DEFAULT_RADIUS_KM;
  els.geoRadiusValue.textContent = `${DEFAULT_RADIUS_KM} km`;
  submit();
});

els.examples.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  els.query.value = "example" in button.dataset ? button.dataset.example : await randomQuestion();
  submit();
});

els.photoInput.addEventListener("change", () => {
  if (els.photoInput.files[0]) setPhoto(els.photoInput.files[0]);
});

els.photoClear.addEventListener("click", () => {
  clearPhoto();
  els.query.value = MODES.image.examples[0];
  submit();
});

window.addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  document.body.classList.add("is-dragging");
});

window.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) document.body.classList.remove("is-dragging");
});

window.addEventListener("drop", (event) => {
  document.body.classList.remove("is-dragging");
  const file = event.dataTransfer.files[0];
  if (!file) return;
  event.preventDefault();
  setPhoto(file);
});

document.addEventListener("paste", (event) => {
  const file = Array.from(event.clipboardData.files).find((item) => item.type.startsWith("image/"));
  if (!file) return;
  event.preventDefault();
  setPhoto(file);
});

els.query.addEventListener("input", () => {
  clearPhoto();
  clearTimeout(suggestTimer);
  clearTimeout(liveTimer);
  if (state.mode === "chat") return;
  const query = els.query.value;
  if (!query.trim()) {
    closeSuggestions();
    return;
  }
  // Chat answers cost seconds and a photo search ignores the words, so only typed searches run live.
  if (els.live.checked && state.mode !== "image") liveTimer = setTimeout(submit, LIVE_SEARCH_DELAY_MS);
  suggestTimer = setTimeout(async () => {
    suggestController?.abort();
    suggestController = new AbortController();
    try {
      const { suggestions } = await api(`/api/autocomplete?${new URLSearchParams({ q: query })}`, {}, suggestController.signal);
      // An answer for an earlier prefix would flash completions that no longer fit.
      if (query === els.query.value) showSuggestions(query, suggestions);
    } catch (error) {
      if (error.name !== "AbortError") closeSuggestions();
    }
  }, AUTOCOMPLETE_DELAY_MS);
});

els.query.addEventListener("keydown", (event) => {
  if (els.suggestions.hidden) return;
  const options = Array.from(els.suggestions.children);
  const active = options.findIndex((option) => option.getAttribute("aria-selected") === "true");
  const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
  if (step) {
    event.preventDefault();
    const next = active < 0 ? (step > 0 ? 0 : options.length - 1) : (active + step + options.length) % options.length;
    options.forEach((option, index) => option.setAttribute("aria-selected", String(index === next)));
    els.query.setAttribute("aria-activedescendant", options[next].id);
  } else if (event.key === "Enter" && active >= 0) {
    event.preventDefault();
    els.query.value = options[active].textContent;
    els.form.requestSubmit();
  } else if (event.key === "Escape") {
    // Escape in a search box would also wipe the query.
    event.preventDefault();
    closeSuggestions();
  }
});

els.query.addEventListener("blur", closeSuggestions);

// Choosing on mousedown keeps focus in the box, so blur doesn't close the list first.
els.suggestions.addEventListener("mousedown", (event) => {
  event.preventDefault();
  const option = event.target.closest('[role="option"]');
  if (!option) return;
  els.query.value = option.textContent;
  els.form.requestSubmit();
});

els.output.addEventListener("mouseover", (event) => {
  const item = event.target.closest(".compare-item");
  els.output.querySelectorAll(".is-hover").forEach((el) => el.classList.remove("is-hover"));
  if (item) els.output.querySelectorAll(`.compare-item[data-id="${item.dataset.id}"]`).forEach((el) => el.classList.add("is-hover"));
});

els.dialog.addEventListener("click", (event) => {
  if (event.target === els.dialog) els.dialog.close();
});

document.addEventListener("click", (event) => {
  const product = event.target.closest("[data-product]");
  if (product) {
    openProduct(product.dataset.product);
    return;
  }
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    navigator.clipboard.writeText($(copy.dataset.copy).dataset.raw);
    copy.textContent = "Copied";
    setTimeout(() => {
      copy.textContent = "Copy";
    }, COPIED_MS);
    return;
  }
  if (event.target.closest("[data-new-chat]")) {
    state.conversation = null;
    state.turns = [];
    els.query.value = MODES.chat.examples[0];
    showChat();
    els.query.focus();
  }
});

els.modes.addEventListener("scroll", fadeModes, { passive: true });
window.addEventListener("resize", fadeModes);

els.fuzzy.checked = state.fuzzy;
els.geoRadius.value = state.geo.radius;
els.geoRadiusValue.textContent = `${state.geo.radius} km`;
if (params.get("embed") === "1") document.body.classList.add("is-embedded");
state.categories.forEach((value) => {
  const input = els.categories.querySelector(`input[value="${CSS.escape(value)}"]`);
  if (input) input.checked = true;
});
els.query.value = params.get("q") || MODES[state.mode].examples[0] || "";
setMode(state.mode);
// manticoresearch.com's "Ask a follow-up" link lands here with the conversation it showed.
if (state.mode === "chat" && params.get("conversation")) continueConversation(params.get("conversation"), params.get("sources"));
