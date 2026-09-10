/* Complaion GDPR Monitor — dashboard client
   Legge docs/data/dashboard.json (prodotto da scripts/scrape_gdpr.py) e popola
   metriche, grafico trend e tabella filtrabile. Nessuna dipendenza oltre Chart.js. */
(function () {
  "use strict";

  var DATA_URL = "data/dashboard.json";
  var state = { items: [], filtered: [], sortKey: "last_change", sortDir: -1 };

  // ---------- Tema (tre stati: system / light / dark) ----------
  function prefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function effectiveTheme() {
    var explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") return explicit;
    return prefersDark() ? "dark" : "light";  // stato "system"
  }
  function updateToggleIcon() {
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = effectiveTheme() === "dark" ? "☀️" : "🌙";
  }
  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("gdpr-theme"); } catch (e) {}
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
    updateToggleIcon();
    var btn = document.getElementById("theme-toggle");
    btn.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("gdpr-theme", next); } catch (e) {}
      updateToggleIcon();
      if (window._timelineChart) renderChart(state.timeline || []);
    });
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (!document.documentElement.getAttribute("data-theme")) {
          updateToggleIcon();
          if (window._timelineChart) renderChart(state.timeline || []);
        }
      });
    }
  }

  // ---------- Utility ----------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  var REL_LABEL = { high: "Alta", medium: "Media", low: "Bassa" };
  var ST_LABEL = { new: "Nuovo", changed: "Aggiornato", seen: "Presente" };

  // ---------- Caricamento ----------
  function load() {
    // Dati embeddati (usati solo per anteprime offline); in produzione assente.
    if (window.__DASHBOARD_DATA__) { render(window.__DASHBOARD_DATA__); return; }
    fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(render)
      .catch(function (err) {
        document.getElementById("items-body").innerHTML =
          '<tr><td colspan="6" class="loading">Impossibile caricare i dati (' +
          esc(err.message) + "). Il primo run del monitor deve ancora popolare " +
          "<code>data/dashboard.json</code>.</td></tr>";
      });
  }

  function render(data) {
    state.items = data.items || [];
    state.timeline = data.timeline || [];

    // last updated
    var lu = document.getElementById("last-updated");
    if (data.generated_utc) lu.textContent = "scan: " + data.generated_utc;

    // stats
    var s = data.stats || {};
    setText("stat-total", s.total_items);
    setText("stat-7d", s.changes_7d);
    setText("stat-30d", s.changes_30d);
    setText("stat-sources", s.sources_count);

    // errori
    var eb = document.getElementById("error-banner");
    if (data.errors && data.errors.length) {
      eb.hidden = false;
      eb.innerHTML = "<strong>⚠️ Alcune fonti non sono state lette nell'ultimo scan:</strong><ul>" +
        data.errors.map(function (e) {
          return "<li>" + esc(e.source_label) + " — " + esc(e.error) + "</li>";
        }).join("") + "</ul>";
    } else { eb.hidden = true; }

    populateFilters();
    renderChart(state.timeline);
    applyFilters();
    wireSort();
  }
  function setText(id, v) {
    var el = document.getElementById(id);
    if (el) el.textContent = (v == null ? "—" : v);
  }

  // ---------- Filtri ----------
  function populateFilters() {
    var sources = {}, categories = {};
    state.items.forEach(function (it) {
      if (it.source_label) sources[it.source_label] = 1;
      if (it.category) categories[it.category] = 1;
    });
    fill("filter-source", Object.keys(sources).sort());
    fill("filter-category", Object.keys(categories).sort());
    ["filter-search", "filter-source", "filter-category", "filter-relevance"].forEach(function (id) {
      var el = document.getElementById(id);
      el.addEventListener("input", applyFilters);
      el.addEventListener("change", applyFilters);
    });
  }
  function fill(id, values) {
    var sel = document.getElementById(id);
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = v; sel.appendChild(o);
    });
  }

  function applyFilters() {
    var q = (document.getElementById("filter-search").value || "").toLowerCase();
    var src = document.getElementById("filter-source").value;
    var cat = document.getElementById("filter-category").value;
    var rel = document.getElementById("filter-relevance").value;

    state.filtered = state.items.filter(function (it) {
      if (src && it.source_label !== src) return false;
      if (cat && it.category !== cat) return false;
      if (rel && it.relevance !== rel) return false;
      if (q) {
        var hay = (it.title + " " + it.summary + " " + it.category).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    sortAndRender();
  }

  // ---------- Ordinamento ----------
  function wireSort() {
    document.querySelectorAll("th[data-sort]").forEach(function (th) {
      th.addEventListener("click", function () {
        var key = th.getAttribute("data-sort");
        if (state.sortKey === key) state.sortDir *= -1;
        else { state.sortKey = key; state.sortDir = -1; }
        sortAndRender();
      });
    });
  }
  var REL_ORDER = { high: 3, medium: 2, low: 1, "": 0 };
  function sortAndRender() {
    var k = state.sortKey, dir = state.sortDir;
    state.filtered.sort(function (a, b) {
      var va = a[k], vb = b[k];
      if (k === "relevance") { va = REL_ORDER[va] || 0; vb = REL_ORDER[vb] || 0; }
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
    renderTable();
  }

  // ---------- Tabella ----------
  function renderTable() {
    var body = document.getElementById("items-body");
    var note = document.getElementById("empty-note");
    if (!state.filtered.length) {
      body.innerHTML = "";
      note.hidden = false;
      return;
    }
    note.hidden = true;
    body.innerHTML = state.filtered.map(function (it) {
      var st = it.status || "seen";
      var rel = it.relevance || "";
      var titleHtml = it.url
        ? '<a class="item-title" href="' + esc(it.url) + '" target="_blank" rel="noopener">' + esc(it.title) + "</a>"
        : '<span class="item-title">' + esc(it.title) + "</span>";
      var summary = it.summary ? '<div class="item-summary">' + esc(it.summary) + "</div>" : "";
      return "<tr>" +
        '<td><span class="badge st-' + esc(st) + '">' + esc(ST_LABEL[st] || st) + "</span></td>" +
        '<td class="src-pill">' + esc(it.source_label) + "</td>" +
        "<td>" + titleHtml + summary + "</td>" +
        "<td>" + esc(it.category || "—") + "</td>" +
        '<td><span class="badge rel-' + (rel || "none") + '">' + esc(REL_LABEL[rel] || "—") + "</span></td>" +
        '<td class="date-cell">' + esc(it.last_change || it.date || "—") + "</td>" +
        "</tr>";
    }).join("");
  }

  // ---------- Grafico trend ----------
  function renderChart(timeline) {
    var canvas = document.getElementById("timeline-chart");
    if (!canvas || typeof Chart === "undefined") return;
    var last = (timeline || []).slice(-30);
    var labels = last.map(function (d) { return d.date ? d.date.slice(5) : ""; });
    var css = getComputedStyle(document.documentElement);
    var accent = css.getPropertyValue("--accent").trim() || "#2f5bea";
    var changed = css.getPropertyValue("--st-changed").trim() || "#b7791f";
    var grid = css.getPropertyValue("--border").trim() || "#e3e8f0";
    var muted = css.getPropertyValue("--muted").trim() || "#667085";

    if (window._timelineChart) window._timelineChart.destroy();
    window._timelineChart = new Chart(canvas.getContext("2d"), {
      type: "bar",
      data: {
        labels: labels,
        datasets: [
          { label: "Nuovi", data: last.map(function (d) { return d.new || 0; }), backgroundColor: accent, borderRadius: 4 },
          { label: "Aggiornati", data: last.map(function (d) { return d.changed || 0; }), backgroundColor: changed, borderRadius: 4 }
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        scales: {
          x: { stacked: true, grid: { display: false }, ticks: { color: muted, maxRotation: 0, autoSkip: true } },
          y: { stacked: true, beginAtZero: true, grid: { color: grid }, ticks: { color: muted, precision: 0 } }
        },
        plugins: { legend: { labels: { color: muted, boxWidth: 12 } } }
      }
    });
  }

  // ---------- Avvio ----------
  initTheme();
  load();
})();
