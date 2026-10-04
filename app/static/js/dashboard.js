/* Мелкие улучшения кабинета без inline-скриптов (строгий CSP). */
(function () {
  document.addEventListener("submit", function (e) {
    var msg = e.target && e.target.dataset && e.target.dataset.confirm;
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var src = document.querySelector(b.dataset.copy);
    if (src && navigator.clipboard) {
      navigator.clipboard.writeText(src.value || src.textContent).then(function () {
        var old = b.textContent; b.textContent = "✓"; setTimeout(function () { b.textContent = old; }, 1200);
      });
    }
  });
})();
