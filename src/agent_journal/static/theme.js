"use strict";

(() => {
  const key = "agent-journal:theme";
  const system = matchMedia("(prefers-color-scheme: dark)");
  let preference = "system";
  let buttons = [];

  function apply() {
    const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = theme;
    for (const button of buttons) {
      button.setAttribute("aria-pressed", String(button.dataset.preference === preference));
    }
  }

  function readPreference() {
    try {
      const saved = localStorage.getItem(key);
      preference = saved === "light" || saved === "dark" ? saved : "system";
    } catch { /* The current theme remains usable without browser storage. */ }
    apply();
  }

  readPreference();
  system.addEventListener("change", apply);
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) readPreference();
  });
  document.addEventListener("DOMContentLoaded", () => {
    buttons = document.querySelectorAll("#theme button");
    for (const button of buttons) {
      button.addEventListener("click", () => {
        preference = button.dataset.preference;
        apply();
        try {
          if (preference === "system") localStorage.removeItem(key);
          else localStorage.setItem(key, preference);
        } catch { /* Keep the selection active for this page. */ }
      });
    }
    apply();
  });
})();
