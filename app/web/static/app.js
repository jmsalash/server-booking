// Forms on every page:
// - data-confirm="Question?" (+ data-confirm-text, data-confirm-ok) asks in a modal first;
// - once a form is sent, the pressed button shows a spinner and every button on the page is
//   blocked until the next page loads, so nothing is sent twice.
(function () {
  var busy = false;

  function setBusy(form, submitter) {
    busy = true;
    var shown = submitter || form.querySelector('button[type="submit"], button:not([type])');
    if (shown) shown.classList.add("is-busy");
    document.querySelectorAll("button, input[type=submit]").forEach(function (b) {
      if (b.closest("dialog")) return;
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

  var dialog = null;
  function confirmDialog() {
    if (dialog) return dialog;
    dialog = document.createElement("dialog");
    dialog.className = "modal";
    dialog.setAttribute("aria-labelledby", "modal-title");
    dialog.innerHTML =
      '<form method="dialog" class="modal__body">' +
      '<h2 id="modal-title" class="modal__title"></h2>' +
      '<p class="modal__text"></p>' +
      '<div class="modal__actions">' +
      '<button value="no" class="btn modal__no">Keep it</button>' +
      '<button value="yes" class="btn btn--danger modal__yes"></button>' +
      "</div></form>";
    // A click on the dimmed backdrop (outside the box) closes it, like Esc.
    dialog.addEventListener("click", function (e) { if (e.target === dialog) dialog.close("no"); });
    document.body.appendChild(dialog);
    return dialog;
  }

  function ask(form, submitter) {
    var d = confirmDialog();
    d.querySelector(".modal__title").textContent = form.dataset.confirm;
    var text = d.querySelector(".modal__text");
    text.textContent = form.dataset.confirmText || "";
    text.hidden = !form.dataset.confirmText;
    d.querySelector(".modal__yes").textContent = form.dataset.confirmOk || "Yes";
    d.returnValue = "";
    d.onclose = function () {
      if (d.returnValue !== "yes") return;
      // The board refreshes its cards every minute: find the form again if it was replaced.
      if (!form.isConnected) form = document.querySelector('form[action="' + form.getAttribute("action") + '"]');
      if (!form) return;
      form.dataset.confirmed = "1";
      form.requestSubmit(submitter && submitter.isConnected ? submitter : undefined);
    };
    d.showModal();
    d.querySelector(".modal__no").focus(); // the safe choice is the default
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.method === "dialog") return;
    if (e.defaultPrevented) return;
    if (busy) { e.preventDefault(); return; }
    if (form.dataset.confirm && !form.dataset.confirmed) {
      e.preventDefault();
      ask(form, e.submitter);
      return;
    }
    delete form.dataset.confirmed;
    setBusy(form, e.submitter);
  });

  // "✓ Saved" messages come from ?saved=…: show it once, not again on reload.
  if (/[?&]saved=/.test(location.search)) {
    var u = new URL(location.href);
    u.searchParams.delete("saved");
    history.replaceState(null, "", u.pathname + u.search + u.hash);
  }

  // Coming back with the browser's Back button may restore the page as it was left.
  window.addEventListener("pageshow", function (e) { if (e.persisted) clear(); });
})();
