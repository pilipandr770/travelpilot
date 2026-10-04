/* Чат-виджет агентства. Без innerHTML: весь текст выводится через textContent (защита от XSS). */
(function () {
  var root = document.getElementById("cw-root");
  if (!root) return;
  var d = root.dataset;
  var btn = document.getElementById("cw-open");
  var box = document.getElementById("cw");
  var log = document.getElementById("cw-log");
  var form = document.getElementById("cw-form");
  var input = document.getElementById("cw-input");
  var closeBtn = document.getElementById("cw-close");
  var key = "tp_chat_session";
  var session = null;
  try { session = sessionStorage.getItem(key); } catch (e) {}
  var busy = false;

  function add(role, text) {
    var el = document.createElement("div");
    el.className = "msg " + (role === "user" ? "u" : "a");
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }
  function cards(list) {
    if (!list || !list.length) return;
    var wrap = document.createElement("div");
    wrap.className = "cards";
    list.forEach(function (o) {
      var c = document.createElement("div");
      c.className = "ocard";
      var t = document.createElement("b");
      t.textContent = o.title || "";
      c.appendChild(t);
      var parts = [];
      if (o.price_from != null) parts.push(d.from + " " + Number(o.price_from).toLocaleString() + " " + (o.currency || "") + (o.price_note ? " (" + o.price_note + ")" : ""));
      if (o.nights) parts.push(o.nights + " " + d.nights);
      if (o.valid_until) parts.push(d.valid + " " + o.valid_until);
      if (o.operator) parts.push(o.operator);
      var line = document.createElement("span");
      line.textContent = parts.join(" · ");
      c.appendChild(line);
      wrap.appendChild(c);
    });
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
  }
  function open() {
    box.classList.add("open");
    btn.style.display = "none";
    if (!log.children.length) {
      if (session) {
        fetch("/chat/history/" + encodeURIComponent(session)).then(function (r) { return r.json(); }).then(function (j) {
          (j.messages || []).forEach(function (m) { add(m.role, m.content); });
          if (!log.children.length) add("assistant", d.greeting);
        }).catch(function () { add("assistant", d.greeting); });
      } else { add("assistant", d.greeting); }
    }
    input.focus();
  }
  function close() { box.classList.remove("open"); btn.style.display = ""; }
  btn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || busy) return;
    busy = true;
    input.value = "";
    add("user", text);
    var typing = add("assistant", "…");
    typing.classList.add("typing");
    fetch("/chat/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session: session })
    }).then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
      .then(function (res) {
        typing.remove();
        if (res.body && res.body.reply) {
          session = res.body.session;
          try { sessionStorage.setItem(key, session); } catch (e) {}
          add("assistant", res.body.reply);
          cards(res.body.offers);
        } else if (res.body && res.body.error === "limit") { add("assistant", d.limit); }
        else if (res.body && res.body.error === "unavailable") { add("assistant", d.unavailable); }
        else { add("assistant", d.error); }
      })
      .catch(function () { typing.remove(); add("assistant", d.error); })
      .then(function () { busy = false; });
  });
})();
