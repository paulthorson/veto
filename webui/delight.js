/* VETO delight — context-gated success feedback (P0 craft sprint, WS-G).
 *
 * GATE CONTRACT (roadmap Initiative 00 — enforced by tests, not convention):
 *  1. Delight renders ONLY when VetoDelight.success() is called explicitly
 *     from a user-initiated success handler. The module never observes the
 *     DOM, never auto-shows, and exposes no error/veto/rejection API.
 *  2. PROHIBITED states (rejection, veto, error, blocked, empty) must use the
 *     existing #banner / role="alert" patterns — never this module.
 *  3. Always dismissible: close button + Escape key. No timers that trap it.
 *  4. Inert under reduced motion: when matchMedia('(prefers-reduced-motion:
 *     reduce)') matches, the toast renders statically (no entrance
 *     animation). The global CSS kill switch is the backstop.
 *
 * Usage: VetoDelight.success("Setup complete — providers are healthy.");
 */
(function (global) {
  "use strict";

  var REDUCED_QUERY = "(prefers-reduced-motion: reduce)";

  function reducedMotion() {
    try {
      return global.matchMedia && global.matchMedia(REDUCED_QUERY).matches;
    } catch (e) {
      return false;
    }
  }

  function dismiss(toast) {
    if (toast && toast.parentNode) toast.parentNode.removeChild(toast);
    global.removeEventListener("keydown", onKey);
  }

  function onKey(e) {
    if (e.key === "Escape" || e.key === "Esc") {
      var t = document.querySelector(".delight-toast");
      dismiss(t);
    }
  }

  function success(message) {
    if (!message) return;
    dismiss(document.querySelector(".delight-toast")); // one at a time
    var toast = document.createElement("div");
    toast.className = "delight-toast" + (reducedMotion() ? " delight-static" : "");
    toast.setAttribute("role", "status");
    var mark = document.createElement("span");
    mark.className = "delight-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = "[✓]";
    var text = document.createElement("span");
    text.className = "delight-text";
    text.textContent = String(message);
    var close = document.createElement("button");
    close.className = "delight-close";
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", function () { dismiss(toast); });
    toast.appendChild(mark);
    toast.appendChild(text);
    toast.appendChild(close);
    document.body.appendChild(toast);
    global.addEventListener("keydown", onKey);
  }

  // The ONLY public API. There is intentionally no .error/.veto/.empty.
  global.VetoDelight = { success: success };
})(window);
