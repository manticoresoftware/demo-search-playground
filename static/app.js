const RESULTS_LIMIT = 12;
const COMPARE_LIMIT = 8;
const COMPARED_MODES = ["fulltext", "vector", "hybrid"];
const AUTOCOMPLETE_DELAY_MS = 150;
// Matches MAX_PHOTO_BYTES in app.py.
const MAX_PHOTO_BYTES = 5 * 1024 * 1024;
const COPIED_MS = 1500;
// Words under 3 letters would mark half of every title.
const MIN_HIGHLIGHT_LENGTH = 3;
const TABLE = "convapparel_products";
const CHAT_MODEL = "assistant_gpt41mini";
const VECTOR_FIELD = "embedding_vector";

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
  tabs: Array.from(document.querySelectorAll('.modes [role="tab"]')),
  form: $("search-form"),
  query: $("query"),
  submit: $("submit"),
  suggestions: $("suggestions"),
  fuzzy: $("fuzzy"),
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
};

const params = new URLSearchParams(location.search);
const state = {
  mode: MODES[params.get("mode")] ? params.get("mode") : "hybrid",
  category: params.get("category") || "",
  fuzzy: params.get("fuzzy") !== "0",
  conversation: null,
  turns: [],
  photo: null,
};
const products = new Map();
let countedQuery = null;
let questionBank = null;
let searchController = null;
let suggestController = null;
let similarController = null;
let suggestTimer = null;

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

function searchPath(mode, query, limit) {
  const search = new URLSearchParams({ q: query, mode, limit });
  if (state.category) search.set("category", state.category);
  if (!state.fuzzy) search.set("fuzzy", "false");
  return `/api/search?${search}`;
}

function photoPath(limit) {
  const search = new URLSearchParams({ limit });
  if (state.category) search.set("category", state.category);
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
  return `<article class="card">
    <div class="card-media"><span class="rank">${rank}</span><img src="${escapeHtml(thumbnail(hit.image_url, 320))}" alt="" loading="lazy" decoding="async"></div>
    <div class="card-body">
      <p class="card-category">${escapeHtml(hit.category)}</p>
      <h3 class="card-title"><button type="button" class="card-open" data-product="${escapeHtml(hit.id)}">${highlight(hit.title, terms)}</button></h3>
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
  if (state.category) search.set("category", state.category);
  if (!state.fuzzy) search.set("fuzzy", "0");
  history.replaceState(null, "", `?${search}`);
}

function updateCounts(result) {
  // A filtered search only counts its own category, so keep the counts from the unfiltered one.
  if (state.category && result.query === countedQuery && result.mode === "fulltext") return;
  const counts = new Map(state.category ? [] : (result.facets || []).map((facet) => [facet.value, facet.count]));
  countedQuery = result.query;
  els.categories.querySelectorAll("[data-count]").forEach((el) => {
    el.textContent = counts.has(el.dataset.count) ? counts.get(el.dataset.count).toLocaleString("en") : "";
  });
}

async function submit() {
  const query = els.query.value.trim();
  const byPhoto = state.mode === "image" && state.photo;
  if (!query && !byPhoto) return;
  searchController?.abort();
  const controller = (searchController = new AbortController());
  syncUrl(byPhoto ? "" : query);
  setBusy(true);
  try {
    if (state.mode === "chat") await runChat(query, controller.signal);
    else if (state.mode === "compare") await runCompare(query, controller.signal);
    else await runSearch(query, controller.signal);
  } catch (error) {
    if (error.name === "AbortError") return;
    els.status.innerHTML = `<span class="error">${escapeHtml(error.message)}</span>`;
    // Results from the previous search would read as answers to this one.
    if (state.mode !== "chat") els.output.innerHTML = "";
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
    <header><h3>${MODES[result.mode].label}</h3><span>${result.took_ms} ms</span></header>
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
    ? `${state.turns.map(turnHtml).join("")}<button type="button" class="link-button" data-new-chat>Start a new conversation</button>`
    : '<p class="empty">Ask a shopping question. Manticore finds matching products, and the AI answers using only those products.</p>';
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
  els.query.placeholder = "Search products";
}

async function randomQuestion() {
  questionBank ??= api("/static/example_questions.json");
  const questions = (await questionBank).flatMap((example) => example.questions.map((question) => question.text));
  return questions[Math.floor(Math.random() * questions.length)];
}

function renderExamples() {
  const buttons = MODES[state.mode].examples.map((example) => `<button type="button" data-example="${escapeHtml(example)}">${escapeHtml(example)}</button>`);
  if (state.mode === "chat") buttons.push('<button type="button" data-random>Random shopper question</button>');
  els.examples.innerHTML = `<span>Try</span>${buttons.join("")}`;
}

function setMode(mode) {
  // Search types share a query so their results can be compared; a question for the AI reads differently.
  const switchesKind = (mode === "chat") !== (state.mode === "chat");
  if (mode !== "image") clearPhoto();
  state.mode = mode;
  document.body.dataset.mode = mode;
  els.tabs.forEach((tab) => {
    const selected = tab.dataset.tab === mode;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  });
  els.fuzzy.disabled = mode === "vector" || mode === "image";
  els.submit.textContent = mode === "chat" ? "Ask AI" : "Search";
  els.query.placeholder = mode === "chat" ? "Ask a shopping question" : "Search products";
  if (mode === "chat") els.query.removeAttribute("list");
  else els.query.setAttribute("list", "suggestions");
  els.explain.textContent = MODES[mode].explain;
  renderExamples();
  // After a photo search the box is empty, so give the next search type something to run.
  if (switchesKind || !els.query.value.trim()) els.query.value = MODES[mode].examples[0];
  if (mode === "chat") showChat();
  else submit();
}

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
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
  state.category = event.target.value;
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
  if (state.mode === "chat") return;
  if (!els.query.value.trim()) {
    els.suggestions.innerHTML = "";
    return;
  }
  suggestTimer = setTimeout(async () => {
    suggestController?.abort();
    suggestController = new AbortController();
    try {
      const { suggestions } = await api(`/api/autocomplete?${new URLSearchParams({ q: els.query.value })}`, {}, suggestController.signal);
      els.suggestions.innerHTML = suggestions.map((suggestion) => `<option value="${escapeHtml(suggestion)}"></option>`).join("");
    } catch (error) {
      if (error.name !== "AbortError") els.suggestions.innerHTML = "";
    }
  }, AUTOCOMPLETE_DELAY_MS);
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

els.fuzzy.checked = state.fuzzy;
const categoryInput = els.categories.querySelector(`input[value="${CSS.escape(state.category)}"]`);
if (categoryInput) categoryInput.checked = true;
else state.category = "";
els.query.value = params.get("q") || MODES[state.mode].examples[0];
setMode(state.mode);
