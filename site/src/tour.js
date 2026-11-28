/* ToolForge guided tour (P20). Progressive enhancement: without this file the section shows the twelve steps as a written list.
   It reads three files from the same site (data/tour.json, data/tour_captions.json, data/glossary.json), makes no other request and sets
   no cookie. It stores one thing, the Simple or Technical choice. Everything it writes into the page is built as text nodes: nothing from a
   data file is ever parsed as HTML. The copy buttons of the last step are handled by app.js.
   Proven by tests/test_tour_static.py and tests/browser/test_tour_browser.py. */
(function () {
  "use strict";

  var MODE_KEY = "toolforge-tour-mode";
  var STEP_COUNT = 12;
  /* The parts of the picture each step is about. The gate stations come from the step's own `gates` in tour.json. */
  var PARTS = {
    meet: ["agent"],
    find: ["agent", "shelf"],
    forge: ["agent", "server"],
    read_build: ["server"],
    test: ["server"],
    second_opinion: ["server"],
    trap: ["server", "room"],
    sign_off: ["server", "operator", "ledger"],
    use: ["agent", "room", "ledger"],
    reuse: ["agent", "shelf", "room"],
    caught: ["server", "room", "ledger"],
    your_turn: ["agent", "server", "shelf", "room", "operator", "ledger"]
  };
  var MARKUP = /\[\[([a-z_]+)(?:\|([^\]]+))?\]\]|`([^`]+)`/g;

  var app = null;
  var section = null;
  var state = {
    steps: [],
    captions: {},
    glossary: {},
    index: 0,
    mode: "simple",
    playing: false,
    timer: null,
    hovered: [],
    focused: []
  };

  function el(id) {
    return document.getElementById(id);
  }

  function each(list, callback) {
    Array.prototype.forEach.call(list, callback);
  }

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function clear(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function has(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  function readMode() {
    try {
      var saved = window.localStorage.getItem(MODE_KEY);
      return saved === "technical" ? "technical" : "simple";
    } catch (err) {
      return "simple";
    }
  }

  function saveMode(mode) {
    try {
      window.localStorage.setItem(MODE_KEY, mode);
    } catch (err) {
      /* not saved; the choice still applies to this visit */
    }
  }

  function load(url) {
    return window.fetch(url, { credentials: "omit" }).then(function (response) {
      if (!response.ok) {
        throw new Error(url + " answered " + response.status);
      }
      return response.json();
    });
  }

  /* --- Captions: text, `code` and glossary terms, as nodes --------------------------------------------------------------- */

  function renderCaption(target, text) {
    clear(target);
    var last = 0;
    var match;
    MARKUP.lastIndex = 0;
    while ((match = MARKUP.exec(text)) !== null) {
      if (match.index > last) {
        target.appendChild(document.createTextNode(text.slice(last, match.index)));
      }
      if (match[3] !== undefined) {
        target.appendChild(make("code", "", match[3]));
      } else if (has(state.glossary, match[1])) {
        var button = make("button", "tour-term-btn", match[2] || state.glossary[match[1]].term);
        button.type = "button";
        button.setAttribute("data-term", match[1]);
        button.setAttribute("aria-expanded", "false");
        button.setAttribute("aria-controls", "tour-term");
        target.appendChild(button);
      } else {
        target.appendChild(document.createTextNode(match[2] || match[1]));
      }
      last = MARKUP.lastIndex;
    }
    if (last < text.length) {
      target.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function hideTerm() {
    el("tour-term").hidden = true;
    each(el("tour-caption").querySelectorAll("button[data-term]"), function (button) {
      button.setAttribute("aria-expanded", "false");
    });
  }

  function toggleTerm(button) {
    var id = button.getAttribute("data-term");
    var panel = el("tour-term");
    if (!has(state.glossary, id)) {
      return;
    }
    var entry = state.glossary[id];
    var wasOpen = !panel.hidden && panel.getAttribute("data-term") === id;
    hideTerm();
    if (wasOpen) {
      return;
    }
    panel.setAttribute("data-term", id);
    el("tour-term-name").textContent = entry.term;
    el("tour-term-plain").textContent = entry.plain;
    el("tour-term-analogy").textContent = "In the shop: " + entry.analogy;
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
  }

  /* --- The live ledger ----------------------------------------------------------------------------------------------------- */

  function factsText(facts) {
    return Object.keys(facts || {})
      .sort()
      .map(function (key) {
        return key + "=" + String(facts[key]);
      })
      .join("  ");
  }

  function timeText(event) {
    if (event.source === "illustrative") {
      return "sample";
    }
    if (typeof event.t === "number") {
      return "+" + event.t.toFixed(3) + "s";
    }
    return "agent";
  }

  function renderLedger(step) {
    var box = el("tour-ledger");
    clear(box);
    box.scrollTop = 0;
    if (!step.events.length) {
      box.appendChild(make("p", "tl-empty", "No event recorded for this step."));
      return;
    }
    step.events.forEach(function (event) {
      var line = make("div", "tl-line");
      line.appendChild(make("span", "tl-t", timeText(event)));
      line.appendChild(make("span", "tl-type", event.type));
      line.appendChild(make("span", "tl-label", event.label));
      line.appendChild(make("span", "tl-tool", event.tool));
      var facts = factsText(event.facts);
      if (facts) {
        line.appendChild(make("span", "tl-facts", facts));
      }
      box.appendChild(line);
    });
  }

  /* --- The picture --------------------------------------------------------------------------------------------------------- */

  /* `{G1: "failed"}` from the step's gate events. The last result wins; a gate that only started has no state. */
  function gateStates(step) {
    var states = {};
    step.events.forEach(function (event) {
      var match = /^forge\.gate\.(passed|failed|skipped)$/.exec(event.type);
      if (match && event.facts && event.facts.gate) {
        states[event.facts.gate] = match[1];
      }
    });
    return states;
  }

  function renderStage(step) {
    var stage = el("tour-stage");
    var states = gateStates(step);
    var gates = step.gates || [];
    var parts = PARTS[step.id] || [];
    /* The last act shows what happened: a station with no result stays dark, and so does the room when nothing ran in it. */
    if (step.id === "caught" && Object.keys(states).length) {
      gates = Object.keys(states);
      var ran = step.events.some(function (event) {
        return /^(forge\.call\.|canary\.)/.test(event.type);
      });
      parts = parts.filter(function (name) {
        return ran || name !== "room";
      });
    }
    var active = parts.concat(
      gates.map(function (gate) {
        return String(gate).toLowerCase();
      })
    );
    stage.setAttribute("data-step", step.id);
    each(stage.querySelectorAll(".ts-part"), function (part) {
      var name = part.id.replace(/^ts-/, "");
      part.classList.toggle("is-active", active.indexOf(name) !== -1);
      if (has(states, name.toUpperCase())) {
        part.setAttribute("data-state", states[name.toUpperCase()]);
      } else {
        part.removeAttribute("data-state");
      }
    });
    stage.querySelector(".ts-token").classList.toggle("is-fail", step.id === "caught");

    /* Colour never carries a result alone: the same states are written out under the picture. */
    var words = Object.keys(states)
      .sort()
      .map(function (gate) {
        return gate + " " + states[gate];
      });
    var line = el("tour-gate-state");
    line.textContent = words.length ? "In this step: " + words.join(", ") + "." : "";
    line.hidden = !words.length;
  }

  /* --- One step on screen -------------------------------------------------------------------------------------------------- */

  /* Focus never jumps away: if it sits on something this step is about to remove or hide, it moves to the text of the step. */
  function keepFocus(step) {
    var active = document.activeElement;
    var leaving =
      el("tour-caption").contains(active) ||
      (step.id !== "your_turn" && el("tour-start").contains(active));
    if (leaving) {
      el("tour-text").focus({ preventScroll: true });
    }
  }

  function show() {
    var step = state.steps[state.index];
    var caption = state.captions[step.id];
    keepFocus(step);
    el("tour-count").textContent = "Step " + (state.index + 1) + " of " + state.steps.length;
    el("tour-step-title").textContent = caption.title;
    renderCaption(el("tour-caption"), caption[state.mode]);
    el("tour-note").textContent = step.note;
    hideTerm();
    renderLedger(step);
    renderStage(step);

    var doc = el("tour-doc");
    doc.textContent = caption.doc;
    doc.setAttribute("href", app.getAttribute("data-blob") + caption.doc);
    el("tour-start").hidden = step.id !== "your_turn";

    each(el("tour-progress").querySelectorAll("button[data-step]"), function (button, i) {
      if (i === state.index) {
        button.setAttribute("aria-current", "step");
      } else {
        button.removeAttribute("aria-current");
      }
      button.classList.toggle("is-done", i < state.index);
    });
    el("tour-back").setAttribute("aria-disabled", state.index === 0 ? "true" : "false");
    el("tour-next").setAttribute("aria-disabled", state.index === state.steps.length - 1 ? "true" : "false");
  }

  function go(index) {
    var next = Math.max(0, Math.min(state.steps.length - 1, index));
    if (next === state.index) {
      return;
    }
    state.index = next;
    show();
    if (state.playing) {
      if (state.index === state.steps.length - 1) {
        stop();
      } else {
        schedule();
      }
    }
  }

  function showMode() {
    each(app.querySelectorAll("button[data-mode]"), function (button) {
      button.setAttribute("aria-pressed", button.getAttribute("data-mode") === state.mode ? "true" : "false");
    });
  }

  function setMode(mode) {
    state.mode = mode;
    saveMode(mode);
    showMode();
    show();
  }

  /* --- Play: off by default, one step every few seconds, held while the visitor reads -------------------------------------- */

  function interval() {
    var milliseconds = parseInt(app.getAttribute("data-interval"), 10);
    return milliseconds > 0 ? milliseconds : 5000;
  }

  function holding() {
    return state.hovered.indexOf(true) !== -1 || state.focused.indexOf(true) !== -1;
  }

  /* Starts a full interval, or none at all while the tour is held. */
  function schedule() {
    window.clearTimeout(state.timer);
    el("tour-held").hidden = !(state.playing && holding());
    if (state.playing && !holding()) {
      state.timer = window.setTimeout(function () {
        go(state.index + 1); /* go() schedules the next one, or stops at the last step */
      }, interval());
    }
  }

  function start() {
    if (state.index === state.steps.length - 1) {
      state.index = 0;
      show();
    }
    state.playing = true;
    el("tour-play").textContent = "Pause";
    el("tour-play").setAttribute("aria-pressed", "true");
    schedule();
  }

  function stop() {
    state.playing = false;
    window.clearTimeout(state.timer);
    el("tour-play").textContent = "Play";
    el("tour-play").setAttribute("aria-pressed", "false");
    el("tour-held").hidden = true;
  }

  /* The timer waits while the pointer is over, or focus is in, a reading area. The play controls are not one, so pressing Play
     does not hold the tour. Pointer and focus are tracked apart: moving the pointer away does not release a focused area. */
  function watch(zone, i) {
    state.hovered[i] = false;
    state.focused[i] = false;
    function set(list, value) {
      if (list[i] !== value) {
        list[i] = value;
        schedule();
      }
    }
    zone.addEventListener("pointerenter", function (event) {
      if (event.pointerType !== "touch") {
        set(state.hovered, true);
      }
    });
    zone.addEventListener("pointerleave", function () {
      set(state.hovered, false);
    });
    zone.addEventListener("focusin", function () {
      set(state.focused, true);
    });
    zone.addEventListener("focusout", function (event) {
      set(state.focused, zone.contains(event.relatedTarget));
    });
  }

  /* --- Wiring -------------------------------------------------------------------------------------------------------------- */

  /* A control at the end of the tour stays focusable (focus never jumps); it only does nothing. */
  function enabled(button) {
    return button.getAttribute("aria-disabled") !== "true";
  }

  function onKey(event) {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
      return;
    }
    if (event.key === "Escape") {
      hideTerm();
      return;
    }
    /* A block that scrolls keeps its own keys. */
    if (event.target.closest("#tour-ledger, pre")) {
      return;
    }
    var moves = { ArrowRight: state.index + 1, ArrowLeft: state.index - 1, Home: 0, End: state.steps.length - 1 };
    if (has(moves, event.key)) {
      event.preventDefault();
      go(moves[event.key]);
    }
  }

  function wire() {
    el("tour-next").addEventListener("click", function (event) {
      if (enabled(event.currentTarget)) {
        go(state.index + 1);
      }
    });
    el("tour-back").addEventListener("click", function (event) {
      if (enabled(event.currentTarget)) {
        go(state.index - 1);
      }
    });
    el("tour-play").addEventListener("click", function () {
      if (state.playing) {
        stop();
      } else {
        start();
      }
    });
    each(app.querySelectorAll("button[data-mode]"), function (button) {
      button.addEventListener("click", function () {
        setMode(button.getAttribute("data-mode"));
      });
    });
    el("tour-progress").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-step]");
      if (button) {
        go(parseInt(button.getAttribute("data-step"), 10));
      }
    });
    el("tour-caption").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-term]");
      if (button) {
        toggleTerm(button);
      }
    });
    app.addEventListener("keydown", onKey);
    [app.querySelector(".tour-grid"), app.querySelector(".tour-ledger"), el("tour-progress")].forEach(watch);
  }

  function check(data, captions, glossary) {
    if (!data || !Array.isArray(data.steps) || data.steps.length !== STEP_COUNT) {
      throw new Error("tour.json does not hold " + STEP_COUNT + " steps");
    }
    data.steps.forEach(function (step) {
      if (!Array.isArray(step.events)) {
        throw new Error("step " + step.id + " has no list of events");
      }
      var caption = captions && captions[step.id];
      if (!caption || !caption.title || !caption.simple || !caption.technical || !caption.doc) {
        throw new Error("no caption for step " + step.id);
      }
    });
    if (!glossary || typeof glossary !== "object") {
      throw new Error("glossary.json is not an object");
    }
  }

  function begin(data, captions, glossary) {
    check(data, captions, glossary);
    state.steps = data.steps;
    state.captions = captions;
    state.glossary = glossary;
    state.mode = readMode();
    wire();
    showMode();
    show();
    section.classList.add("tour-live");
  }

  function failed() {
    el("tour-failed").hidden = false;
  }

  function init() {
    app = el("tour-app");
    section = el("tour");
    if (!app || !section) {
      return;
    }
    Promise.all([
      load(app.getAttribute("data-tour")),
      load(app.getAttribute("data-captions")),
      load(app.getAttribute("data-glossary"))
    ])
      .then(function (parts) {
        begin(parts[0], parts[1], parts[2]);
      })
      .catch(failed);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
