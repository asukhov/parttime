/* Client-side search over /searchindex.json — a few hundred posts scan
   instantly, no server and no dependencies required. */
(function () {
  "use strict";

  var cfg = window.PM_SEARCH;
  var input = document.getElementById("q");
  var results = document.getElementById("results");
  if (!cfg || !input || !results) return;

  var index = null;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function mark(text, tokens) {
    var out = esc(text);
    tokens.forEach(function (t) {
      if (t.length < 2) return;
      out = out.replace(
        new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"),
        "<mark>$1</mark>"
      );
    });
    return out;
  }

  function render(query) {
    var tokens = query.toLowerCase().split(/\s+/).filter(Boolean);
    results.innerHTML = "";
    if (!tokens.length) return;

    var hits = [];
    for (var i = 0; i < index.length; i++) {
      var item = index[i];
      var title = item.t.toLowerCase();
      var body = item.d.toLowerCase();
      var score = 0;
      var ok = true;
      for (var j = 0; j < tokens.length; j++) {
        var t = tokens[j];
        var inTitle = title.indexOf(t) !== -1;
        var inBody = body.indexOf(t) !== -1;
        if (!inTitle && !inBody) { ok = false; break; }
        score += inTitle ? 3 : 1;
      }
      if (ok) hits.push({ item: item, score: score });
    }
    hits.sort(function (a, b) { return b.score - a.score; });

    if (!hits.length) {
      results.innerHTML = '<p class="search-hint">Ничего не найдено.</p>';
      return;
    }

    hits.slice(0, 40).forEach(function (hit) {
      var item = hit.item;
      var card = document.createElement("div");
      card.className = "search-result";
      card.innerHTML =
        '<h3><a href="' + esc(item.u) + '">' + mark(item.t, tokens) + "</a></h3>" +
        (item.dt ? '<div class="meta">' + esc(item.dt) + "</div>" : "") +
        '<p class="snippet">' + mark(item.d.slice(0, 240), tokens) + "…</p>";
      results.appendChild(card);
    });
  }

  function run() {
    if (!index) return;
    var q = input.value.trim();
    render(q);
    var url = new URL(window.location.href);
    if (q) url.searchParams.set("q", q);
    else url.searchParams.delete("q");
    history.replaceState(null, "", url);
  }

  fetch(cfg.index)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      index = data;
      var q = new URL(window.location.href).searchParams.get("q") || "";
      input.value = q;
      if (q) render(q);
    })
    .catch(function () {
      results.innerHTML = '<p class="search-hint">Не удалось загрузить индекс поиска.</p>';
    });

  input.addEventListener("input", run);
})();
