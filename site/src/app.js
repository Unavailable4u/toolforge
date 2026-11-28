/* ToolForge landing site (P19). Progressive enhancement only: the page reads fully without this file.
   It adds a theme toggle, a collapsible menu on narrow screens and copy buttons. No network, no cookies, no analytics. */
(function () {
  "use strict";

  var root = document.documentElement;
  var KEY = "toolforge-theme";

  // Runs while the head is parsed, so a saved choice is in place before the first paint.
  try {
    var saved = window.localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") {
      root.setAttribute("data-theme", saved);
    }
  } catch (err) {
    /* storage can be blocked; the system setting then applies */
  }
  root.classList.add("js");

  function effectiveTheme() {
    var set = root.getAttribute("data-theme");
    if (set === "light" || set === "dark") {
      return set;
    }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function setupTheme() {
    var button = document.getElementById("theme-toggle");
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        window.localStorage.setItem(KEY, next);
      } catch (err) {
        /* not saved; the choice still applies to this visit */
      }
    });
  }

  function setupMenu() {
    var button = document.getElementById("menu-toggle");
    var nav = document.getElementById("site-nav");
    if (!button || !nav) {
      return;
    }
    function setOpen(open) {
      nav.classList.toggle("open", open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
    button.addEventListener("click", function () {
      setOpen(button.getAttribute("aria-expanded") !== "true");
    });
    nav.addEventListener("click", function (event) {
      if (event.target && event.target.tagName === "A") {
        setOpen(false);
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && button.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        button.focus();
      }
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "sr-only";
      document.body.appendChild(area);
      area.select();
      var done = false;
      try {
        done = document.execCommand("copy");
      } catch (err) {
        done = false;
      }
      document.body.removeChild(area);
      if (done) {
        resolve();
      } else {
        reject(new Error("copy failed"));
      }
    });
  }

  function setupCopy() {
    var buttons = document.querySelectorAll("button.copy[data-copy-target]");
    Array.prototype.forEach.call(buttons, function (button) {
      var original = button.textContent;
      button.addEventListener("click", function () {
        var target = document.getElementById(button.getAttribute("data-copy-target"));
        if (!target) {
          return;
        }
        copyText(target.textContent.replace(/\s+$/, "")).then(
          function () {
            button.textContent = "Copied";
            window.setTimeout(function () {
              button.textContent = original;
            }, 1800);
          },
          function () {
            button.textContent = "Select and copy";
            window.setTimeout(function () {
              button.textContent = original;
            }, 2500);
          }
        );
      });
    });
  }

  function start() {
    setupTheme();
    setupMenu();
    setupCopy();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
