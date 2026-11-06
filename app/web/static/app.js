// Busy state for every form: once a form is sent, the pressed button shows a spinner and every
// button on the page is blocked until the next page loads, so nothing is sent twice.
(function () {
  var busy = false;

  function setBusy(form, submitter) {
    busy = true;
    var shown = submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (shown) shown.classList.add("is-busy");
    document.querySelectorAll("button, input[type=submit]").forEach(function (b) {
      b.setAttribute("aria-disabled", "true");
      b.classList.add("is-blocked");
    });
    document.body.setAttribute("aria-busy", "true");
  }

  function clear() {
    busy = false;
    document.querySelectorAll(".is-busy, .is-blocked").forEach(function (b) {
      b.classList.remove("is-busy", "is-blocked");
      b.removeAttribute("aria-disabled");
    });
    document.body.removeAttribute("aria-busy");
  }

  document.addEventListener("submit", function (e) {
    if (e.defaultPrevented) return; // e.g. a "Are you sure?" dialog answered No
    if (busy) { e.preventDefault(); return; }
    setBusy(e.target, e.submitter);
  });

  // Coming back with the browser's Back button may restore the page as it was left.
  window.addEventListener("pageshow", function (e) { if (e.persisted) clear(); });
})();
