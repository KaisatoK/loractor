// Talks the Streamlit component protocol directly, so no build step is needed.
const RENDER = "streamlit:render";
const state = { text: "", submitted: "", word: "", submitSeq: 0 };
const input = document.getElementById("q");
const list = document.getElementById("list");
let suggestions = [];
let focused = false;
let ready = false;
let debounce = null;

const SPACE = /\s/;

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

function renderList() {
  list.innerHTML = "";
  list.classList.toggle("open", focused && suggestions.length > 0);

  suggestions.forEach((s, i) => {
    const li = document.createElement("li");
    const term = document.createElement("span");
    term.className = "term";
    term.textContent = s.suggestion;
    const hits = document.createElement("span");
    hits.className = "hits";
    hits.textContent = s.count;
    li.append(term, hits);

    li.addEventListener("mousedown", (event) => {
      event.preventDefault();
      pick(i);
    });

    list.appendChild(li);
  });

  setHeight();
}

function pick(index) {
  const chosen = suggestions[index];

  if (!chosen) {
    return;
  }

  const value = input.value;
  const picked = String(chosen.suggestion);
  const { start, end } = currentWord();
  const draft = words(value);
  const suggested = words(picked);

  // A suggestion is a whole field value and can span several words -- an
  // event_template such as ``Removable base files: <*>`` -- while the caret only
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
  renderList();
  setValue();
  input.focus();
}

input.addEventListener("focus", () => {
  focused = true;
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
  clearTimeout(debounce);
  debounce = setTimeout(setValue, 180);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();

    const text = input.value.trim();

    if (!text) {
      return;
    }

    state.submitted = text;
    state.text = "";
    state.word = "";
    state.submitSeq += 1;
    input.value = "";
    suggestions = [];
    renderList();
    setValue();
  } else if (event.key === "Escape") {
    suggestions = [];
    renderList();
  }
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

  input.placeholder = args.placeholder || "";

  if (
    typeof args.text === "string" &&
    args.text !== input.value &&
    document.activeElement !== input
  ) {
    input.value = args.text;
  }

  suggestions = Array.isArray(args.suggestions) ? args.suggestions : [];
  renderList();
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
