// Talks the Streamlit component protocol directly, so no build step is needed.
const RENDER = "streamlit:render";
const state = { text: "", submitted: "", word: "", submitSeq: 0, stopSeq: 0 };
const input = document.getElementById("q");
const list = document.getElementById("list");
const sendBtn = document.getElementById("send");
const box = document.getElementById("box");
let suggestions = [];
let focused = false;
let ready = false;
let debounce = null;
let active = -1; // suggestion the arrow keys have selected (Enter accepts it)

// An investigation is running for this chat: the field is locked and the send
// button is a stop button. ``stopping`` is set once a stop has been requested,
// until the backend reports the run has ended (it stops at its next event).
let running = false;
let stopping = false;
let basePlaceholder = "";

const SPACE = /\s/;
// ``@`` component/service, ``#`` event template, ``$`` message content. ``!`` is
// not a selector: typed right after one (``@!nova``) it only switches the
// backend to case-sensitive ranking, so it stays part of the queried word.
const SELECTORS = "@#$";

// The selector that scoped the lookup behind the suggestions on screen, if the
// queried word started with one. It is shown in the dropdown as a prefix but is
// never inserted: the backend strips it before matching, and picking a
// suggestion yields the bare field value.
function activeSelector() {
  const word = state.word || "";
  return SELECTORS.includes(word.charAt(0)) ? word.charAt(0) : "";
}

function currentWord() {
  const value = input.value;
  const caret =
    input.selectionStart == null ? value.length : input.selectionStart;
  let start = caret;

  while (start > 0 && !SPACE.test(value[start - 1])) {
    start -= 1;
  }

  let end = caret;

  while (end < value.length && !SPACE.test(value[end])) {
    end += 1;
  }

  return { start, end, word: value.slice(start, end) };
}

// Space-delimited words with their offsets, so a multi-word suggestion can be
// matched against the words around the caret.
function words(text) {
  const spans = [];
  let i = 0;

  while (i < text.length) {
    if (SPACE.test(text[i])) {
      i += 1;
      continue;
    }

    let end = i;

    while (end < text.length && !SPACE.test(text[end])) {
      end += 1;
    }

    spans.push({ start: i, end, text: text.slice(i, end) });
    i = end;
  }

  return spans;
}

function post(type, data) {
  window.parent.postMessage(
    Object.assign({ isStreamlitMessage: true, type }, data),
    "*"
  );
}

function setValue() {
  post("streamlit:setComponentValue", {
    value: Object.assign({}, state),
    dataType: "json",
  });
}

function setHeight() {
  post("streamlit:setFrameHeight", { height: document.body.scrollHeight });
}

// The send button is only actionable when there is something to send; while a
// run is active it is the stop button, actionable until a stop is requested.
function syncSend() {
  sendBtn.disabled = running ? stopping : !input.value.trim();
}

// Lock the field and swap the send icon for the stop icon while a run is active.
function syncMode() {
  input.disabled = running;
  box.classList.toggle("locked", running);
  sendBtn.classList.toggle("stop", running);

  const label = running ? (stopping ? "Stopping" : "Stop") : "Send";
  sendBtn.setAttribute("aria-label", label);
  sendBtn.title = label;

  input.placeholder = running
    ? stopping
      ? "Stopping…"
      : "Investigation running…"
    : basePlaceholder;

  if (running) {
    focused = false;
    suggestions = [];
    active = -1;
  }

  syncSend();
}

// Ask Streamlit to cancel the run. The button locks at once so a double click
// cannot send two requests; the next render confirms (or undoes) the state.
function stop() {
  if (!running || stopping) {
    return;
  }

  stopping = true;
  state.submitted = "";
  state.stopSeq += 1;
  syncMode();
  setValue();
}

// Hand the draft to Streamlit: clear the box, bump the submit counter and post
// the new state. Shared by Enter and the send button.
function submit() {
  const text = input.value.trim();

  if (running || !text) {
    return;
  }

  state.submitted = text;
  state.text = "";
  state.word = "";
  state.submitSeq += 1;
  input.value = "";
  suggestions = [];
  active = -1;
  renderList();
  syncSend();
  setValue();
}

// A ``$`` content suggestion is a whole log line: it is shown with its line id
// and inserts a "Line ID: n (dataset)" reference rather than the message. The
// suggestions are merged across corpora and a line id is only unique within
// one, so the dataset rides along (omitted only if the backend sent none).
// Every other suggestion is a field value carrying a result count.
function isLineRef(s) {
  return s != null && s.line_id != null;
}

function lineRef(s) {
  return s.dataset ? `Line ID: ${s.line_id} (${s.dataset})` : `Line ID: ${s.line_id}`;
}

// The right-hand meta text for a suggestion.
function metaLabel(s) {
  return isLineRef(s) ? lineRef(s) : `Count: ${s.count}`;
}

// The text a suggestion inserts into the box when it is accepted.
function insertText(s) {
  return isLineRef(s) ? lineRef(s) : String(s.suggestion);
}

// Paint the arrow-key selection so Enter knows which suggestion it would accept.
function highlight() {
  Array.from(list.children).forEach((li, i) => {
    const on = i === active;
    li.classList.toggle("active", on);

    if (on) {
      li.scrollIntoView({ block: "nearest" });
    }
  });
}

// Move the keyboard selection by one, wrapping at either end. With nothing
// selected, Down picks the first suggestion and Up the last.
function moveActive(delta) {
  const count = suggestions.length;

  if (count === 0) {
    active = -1;
    highlight();
    return;
  }

  if (active === -1) {
    active = delta > 0 ? 0 : count - 1;
  } else {
    active = (active + delta + count) % count;
  }

  highlight();
}

function renderList() {
  list.innerHTML = "";
  list.classList.toggle("open", focused && suggestions.length > 0);
  const selector = activeSelector();

  suggestions.forEach((s, i) => {
    const li = document.createElement("li");
    const term = document.createElement("span");
    term.className = "term";

    // The selector is display-only -- it marks which column the suggestions came
    // from but is not part of the value pick() inserts.
    if (selector) {
      const scope = document.createElement("span");
      scope.className = "selector";
      scope.textContent = selector;
      term.appendChild(scope);
    }

    term.appendChild(document.createTextNode(String(s.suggestion)));
    const hits = document.createElement("span");
    hits.className = "hits";
    hits.textContent = metaLabel(s);
    li.append(term, hits);

    li.addEventListener("mousedown", (event) => {
      event.preventDefault();
      pick(i);
    });

    // Keep the keyboard selection in step with the pointer.
    li.addEventListener("mouseenter", () => {
      active = i;
      highlight();
    });

    list.appendChild(li);
  });

  highlight();
  setHeight();
}

function pick(index) {
  const chosen = suggestions[index];

  if (!chosen) {
    return;
  }

  const value = input.value;
  const picked = insertText(chosen);
  const { start, end } = currentWord();
  const draft = words(value);
  const suggested = words(picked);

  // A suggestion is a whole field value and can span several words -- a
  // content line such as ``Removable base files: <*>`` -- while the caret only
  // marks one of them. Line the two up on the word under the caret and replace
  // every draft word the suggestion covers, so accepting it does not duplicate
  // the surrounding words the draft already shares with it.
  const at = draft.findIndex((w) => w.start === start && w.end === end);
  const target = at >= 0 ? draft[at].text.toLowerCase() : "";
  const first = target
    ? suggested.findIndex((w) => w.text.toLowerCase().includes(target))
    : -1;

  let from = start;
  let to = end;

  if (first >= 0) {
    let lo = at;
    let hi = at;
    let slo = first;
    let shi = first;

    while (
      lo > 0 &&
      slo > 0 &&
      draft[lo - 1].text.toLowerCase() === suggested[slo - 1].text.toLowerCase()
    ) {
      lo -= 1;
      slo -= 1;
    }

    while (
      hi + 1 < draft.length &&
      shi + 1 < suggested.length &&
      draft[hi + 1].text.toLowerCase() ===
      suggested[shi + 1].text.toLowerCase()
    ) {
      hi += 1;
      shi += 1;
    }

    from = draft[lo].start;
    to = draft[hi].end;
  }

  input.value = value.slice(0, from) + picked + value.slice(to);
  const caret = from + picked.length;
  input.setSelectionRange(caret, caret);
  state.text = input.value;
  state.word = "";
  state.submitted = "";
  suggestions = [];
  active = -1;
  renderList();
  setValue();
  input.focus();
}

input.addEventListener("focus", () => {
  focused = true;
  active = suggestions.length > 0 ? 0 : -1;
  renderList();
});

input.addEventListener("blur", () => {
  focused = false;
  renderList();
});

input.addEventListener("input", () => {
  state.text = input.value;
  state.word = currentWord().word;
  state.submitted = "";
  active = -1;
  highlight();
  syncSend();
  clearTimeout(debounce);
  debounce = setTimeout(setValue, 180);
});

input.addEventListener("keydown", (event) => {
  if (running) {
    return;
  }

  if (event.key === "Enter") {
    event.preventDefault();

    if (active >= 0 && suggestions[active]) {
      pick(active);
    } else {
      submit();
    }
  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!focused || suggestions.length === 0) {
      return;
    }

    event.preventDefault();
    moveActive(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Escape") {
    suggestions = [];
    active = -1;
    renderList();
  }
});

sendBtn.addEventListener("mousedown", (event) => {
  event.preventDefault();
});

sendBtn.addEventListener("click", () => {
  if (running) {
    stop();
    return;
  }

  submit();
  input.focus();
});

function applyTheme(theme) {
  const root = document.documentElement.style;

  if (theme.base === "dark") {
    root.setProperty("--ac-bg", "#262730");
    root.setProperty("--ac-fg", "#fafafa");
    root.setProperty("--ac-border", "rgba(250, 250, 250, 0.2)");
    root.setProperty("--ac-muted", "rgba(250, 250, 250, 0.6)");
  }

  if (theme.secondaryBackgroundColor) {
    root.setProperty("--ac-bg", theme.secondaryBackgroundColor);
  }

  if (theme.textColor) {
    root.setProperty("--ac-fg", theme.textColor);
  }
}

window.addEventListener("message", (event) => {
  const data = event.data || {};

  if (data.type !== RENDER) {
    return;
  }

  ready = true;
  const args = data.args || {};

  if (data.theme) {
    applyTheme(data.theme);
  }

  basePlaceholder = args.placeholder || "";

  // Resume counting above the backend's submit watermark: a remounted box (a new
  // chat unmounts it on the role picker) would otherwise restart at zero, and the
  // backend would drop its first submits as replays of the previous chat's.
  if (typeof args.submit_seq_base === "number") {
    state.submitSeq = Math.max(state.submitSeq, args.submit_seq_base);
  }

  // Same for the stop counter, so a remounted box does not replay old stops.
  if (typeof args.stop_seq_base === "number") {
    state.stopSeq = Math.max(state.stopSeq, args.stop_seq_base);
  }

  // The backend's view of the run is authoritative: it undoes an optimistic
  // "stopping" if the stop request failed.
  const wasRunning = running;
  running = args.running === true;
  stopping = running && args.stopping === true;

  if (
    typeof args.text === "string" &&
    args.text !== input.value &&
    document.activeElement !== input
  ) {
    input.value = args.text;
  }

  suggestions =
    !running && Array.isArray(args.suggestions) ? args.suggestions : [];
  active = suggestions.length > 0 ? 0 : -1;
  syncMode();
  renderList();

  // The run ended: hand the keyboard back so the next question can be typed.
  if (wasRunning && !running) {
    input.focus();
  }
});

function announce() {
  post("streamlit:componentReady", { apiVersion: 1 });
}

window.addEventListener("load", announce);
announce();

const retry = setInterval(() => {
  if (ready) {
    clearInterval(retry);
  }

  else announce();
}, 250);

new ResizeObserver(setHeight).observe(document.body);
setHeight();
