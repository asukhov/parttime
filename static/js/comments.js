/* Anonymous comments - front-end for the optional Cloudflare Worker bridge
   (site.toml: comments.provider = "cf").
   The worker keeps the thread in a GitHub Discussion and shields writes with
   Cloudflare Turnstile; this file only talks to the worker. */
(function () {
  "use strict";

  var cfg = window.PM_COMMENTS;
  if (!cfg || cfg.provider !== "cf") return;
  var root = document.getElementById("cf-comments");
  if (!root) return;

  var api = (cfg.api || "").replace(/\/+$/, "");
  var page = window.location.pathname;
  var token = "";
  var loading = false;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* plain text -> minimal safe html (autolinks, paragraphs, line breaks) */
  function textToHtml(text) {
    return esc(text)
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" rel="nofollow">$1</a>')
      .split(/\n{2,}/)
      .map(function (p) { return "<p>" + p.replace(/\n/g, "<br />") + "</p>"; })
      .join("");
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("ru-RU", {
        day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit"
      });
    } catch (e) { return iso; }
  }

  function renderThread(comments) {
    if (!comments || !comments.length) {
      return '<p class="comments-note">Комментариев пока нет - будьте первым.</p>';
    }
    var items = comments.map(function (c) {
      return (
        '<li class="comment" id="cf-' + esc(c.id) + '">' +
        '<div class="comment-header"><cite class="user">' + esc(c.author) + "</cite>" +
        '<span class="comment-sep"> · </span><span class="datetime">' +
        esc(fmtDate(c.created)) + "</span></div>" +
        '<div class="comment-content">' + textToHtml(c.body) + "</div></li>"
      );
    }).join("");
    var n = comments.length;
    return '<h3>Комментарии <span class="count">(' + n + ")</span></h3>" +
      '<ol class="comment-list">' + items + "</ol>";
  }

  function formHtml() {
    return (
      '<form id="cf-form">' +
      '<label for="cf-name">Имя</label>' +
      '<input type="text" id="cf-name" name="name" maxlength="60" required>' +
      '<label for="cf-body">Комментарий</label>' +
      '<textarea id="cf-body" name="body" maxlength="4000" required></textarea>' +
      '<div class="hp"><label>Website <input type="text" name="website" tabindex="-1" autocomplete="off"></label></div>' +
      (cfg.sitekey ? '<div id="cf-turnstile"></div>' : "") +
      '<button type="submit">Отправить</button>' +
      '<p class="comments-note" id="cf-status"></p>' +
      "</form>"
    );
  }

  function boot() {
    if (!api) {
      root.innerHTML =
        '<p class="comments-note">Комментарии не настроены: укажите <code>cf_api</code> ' +
        "в site.toml (см. extras/cloudflare-comments/README.md).</p>";
      return;
    }
    root.innerHTML = '<p class="comments-note">Загрузка комментариев…</p>';

    fetch(api + "/api/comments?page=" + encodeURIComponent(page))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        root.innerHTML = renderThread(data.comments) + formHtml();
        wireForm();
      })
      .catch(function () {
        root.innerHTML =
          '<p class="comments-note">Комментарии временно недоступны.</p>' + formHtml();
        wireForm();
      });
  }

  function setupTurnstile() {
    if (!cfg.sitekey) return;
    if (window.turnstile) {
      window.turnstile.render("#cf-turnstile", {
        sitekey: cfg.sitekey,
        callback: function (t) { token = t; },
        "expired-callback": function () { token = ""; }
      });
    } else {
      window.addEventListener("load", setupTurnstile); // script may arrive late
    }
  }

  function wireForm() {
    var form = document.getElementById("cf-form");
    var nameInput = document.getElementById("cf-name");
    var bodyInput = document.getElementById("cf-body");
    var trapInput = form.elements.website;
    var status = document.getElementById("cf-status");

    setupTurnstile();

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (loading) return;
      var name = nameInput.value.trim();
      var body = bodyInput.value.trim();
      if (!name || !body) return;
      if (cfg.sitekey && !token) {
        status.textContent = "Подтвердите, что вы не робот.";
        return;
      }
      loading = true;
      status.textContent = "Отправка…";
      fetch(api + "/api/comments", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          page: page,
          name: name,
          body: body,
          website: trapInput.value,
          token: token
        })
      })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) {
          loading = false;
          if (res.ok) {
            bodyInput.value = "";
            status.textContent = "Комментарий опубликован.";
            boot();
          } else {
            status.innerHTML = '<span class="cf-error">' +
              esc(res.j.error || "Не получилось отправить.") + "</span>";
            if (window.turnstile) window.turnstile.reset();
            token = "";
          }
        })
        .catch(function () {
          loading = false;
          status.innerHTML = '<span class="cf-error">Сеть недоступна, попробуйте ещё раз.</span>';
        });
    });
  }

  boot();
})();
