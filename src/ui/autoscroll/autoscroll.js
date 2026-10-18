// Keeps the Streamlit app scrolled to the bottom while an answer streams, and
// steps out of the way the moment the reader scrolls up.
//
// The component is rendered inside the run's polling fragment, so every rerun
// reposts a ``streamlit:render`` message with the new content length; the
// component itself paints nothing, it only scrolls. It is same-origin with the
// app, so the parent document is reachable directly.
//
// The follow is a slow, bezier-eased glide toward the bottom. It is released as
// soon as the reader scrolls up (decided from the scroll position itself, so it
// never fights them) and re-engages when they come back near the bottom.
const RENDER = "streamlit:render";

// Distance from the bottom, in pixels, at which an active follow releases and a
// released follow re-engages. The gap is hysteresis, so it does not flap at one
// boundary: once following it holds until the reader is clearly above the
// bottom, but once released a small return to the bottom picks it up again.
const RELEASE_DISTANCE = 8;
const ENGAGE_DISTANCE = 48;

// Glide tuning. Duration grows with the distance to cover and is bounded at both
// ends; the easing shapes the motion inside that duration. These are deliberately
// slow: the point is a calm, readable glide, not a snap to the bottom.
const FOLLOW_PX_PER_MS = 0.5;
const FOLLOW_MIN_MS = 400;
const FOLLOW_MAX_MS = 1600;

// The glide's easing: cubic-bezier(0.4, 0, 0.2, 1) -- Material's standard
// curve. It eases in (no abrupt jump off the line) and out (settles softly).
const BEZIER = [0.4, 0, 0.2, 1];

// Per-host state, stored on the parent window so it survives this component's
// iframe being remounted (which re-runs this script from scratch). Keyed by the
// run id, so a new investigation starts fresh rather than inheriting the
// previous run's "reader scrolled up" state.
const STATE_KEY = "__streamlitAutoScroll";

// Identifies this execution of the script. If the iframe is remounted, a new
// script instance runs and the stale listeners (bound to the dead window) must
// be replaced; a plain re-render of the same iframe reuses them instead.
const INSTANCE_ID = Math.random().toString(36).slice(2);

// This component's own <iframe> in the parent document, matched by window.
function ownFrame(doc) {
  const frames = doc.querySelectorAll("iframe");

  for (const frame of frames) {
    if (frame.contentWindow === window) {
      return frame;
    }
  }

  return null;
}

// True when an element is a scroll container: its overflow is scrollable and
// its content is taller than its viewport. Content overflowing a
// ``overflow: hidden`` box (Streamlit's ``stAppViewContainer``) is not
// scrollable, so the content check alone is not enough.
function isScroller(doc, el) {
  if (!el) {
    return false;
  }

  const overflowY = doc.defaultView.getComputedStyle(el).overflowY;
  const scrolls =
    overflowY === "auto" || overflowY === "scroll" || overflowY === "overlay";

  return scrolls && el.scrollHeight > el.clientHeight;
}

// The element that actually scrolls the app. Newer Streamlit scrolls the
// ``stMain`` section; other builds scroll ``stAppViewContainer`` or the
// document. Prefer the first candidate that genuinely scrolls, then fall back
// to the nearest scrollable ancestor of this component's iframe.
function scrollContainer(doc) {
  const candidates = [
    doc.querySelector('section[data-testid="stMain"]'),
    doc.querySelector('[data-testid="stAppViewContainer"]'),
  ];

  for (const el of candidates) {
    if (isScroller(doc, el)) {
      return el;
    }
  }

  let node = ownFrame(doc);

  while (node) {
    if (isScroller(doc, node)) {
      return node;
    }

    node = node.parentElement;
  }

  return doc.scrollingElement || doc.documentElement;
}

function distanceFromBottom(container) {
  return container.scrollHeight - container.scrollTop - container.clientHeight;
}

// Sample a CSS-style cubic-bezier easing into a table and interpolate it. A
// table beats solving the curve on every animation frame, and the shape is
// exact enough at this resolution.
function makeEasing(x1, y1, x2, y2) {
  const cx = 3 * x1;
  const bx = 3 * (x2 - x1) - cx;
  const ax = 1 - cx - bx;
  const cy = 3 * y1;
  const by = 3 * (y2 - y1) - cy;
  const ay = 1 - cy - by;
  const steps = 64;
  const xs = new Float64Array(steps + 1);
  const ys = new Float64Array(steps + 1);

  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    xs[i] = ((ax * t + bx) * t + cx) * t;
    ys[i] = ((ay * t + by) * t + cy) * t;
  }

  return (x) => {
    if (x <= 0) {
      return 0;
    }

    if (x >= 1) {
      return 1;
    }

    let lo = 0;
    let hi = steps;

    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;

      if (xs[mid] <= x) {
        lo = mid;
      } else {
        hi = mid;
      }
    }

    const span = xs[hi] - xs[lo] || 1;
    const frac = (x - xs[lo]) / span;

    return ys[lo] + (ys[hi] - ys[lo]) * frac;
  };
}

const easeFollow = makeEasing(BEZIER[0], BEZIER[1], BEZIER[2], BEZIER[3]);

// The parent window, or ``null`` when cross-origin (nothing we can do).
function hostWindow() {
  try {
    return window.parent && window.parent.document ? window.parent : null;
  } catch (err) {
    return null;
  }
}

function cancelGlide(state) {
  if (state.raf) {
    window.cancelAnimationFrame(state.raf);
    state.raf = null;
  }
}

// Drop every listener and pending frame of a state (used when the run changes
// or the scroll container is swapped).
function detach(state) {
  if (state.scrollTargets) {
    for (const [target, fn] of state.scrollTargets) {
      target.removeEventListener("scroll", fn);
    }
  }

  cancelGlide(state);

  state.container = null;
  state.scrollTargets = null;
  state.lastSet = null;
}

function hostState(win, runId) {
  const previous = win[STATE_KEY];

  // A different run, or the first render: start sticking again.
  if (!previous || previous.runId !== runId) {
    if (previous) {
      detach(previous);
    }

    win[STATE_KEY] = {
      runId,
      instanceId: INSTANCE_ID,
      sticky: true,
      container: null,
      scrollTargets: null,
      raf: null,
      lastSet: null,
    };

    return win[STATE_KEY];
  }

  // Same run, but a fresh iframe: keep whether the reader had taken over and
  // only rebuild the listeners bound to the dead window.
  if (previous.instanceId !== INSTANCE_ID) {
    detach(previous);
    previous.instanceId = INSTANCE_ID;
  }

  return previous;
}

// Watch the reader. The follow's own writes are recognised by the value they
// set and ignored, so any other scroll is the reader's: moving clearly up
// releases the follow at once (and stops the glide), coming back near the
// bottom re-engages it.
function watchScroll(win, container, state) {
  if (state.container === container) {
    return;
  }

  detach(state);

  const onScroll = () => {
    const top = container.scrollTop;

    if (state.lastSet !== null && Math.abs(top - state.lastSet) <= 1) {
      return; // our own glide, not the reader
    }

    const distance = distanceFromBottom(container);

    if (state.sticky) {
      if (distance > RELEASE_DISTANCE) {
        state.sticky = false;
        cancelGlide(state);
        state.lastSet = null;
      }
    } else if (distance <= ENGAGE_DISTANCE) {
      state.sticky = true;
      followBottom(state, container);
    }
  };

  // A document-level scroll fires on the window, not the element, so listen on
  // both when the container is the document scroller.
  const doc = win.document;
  const targets = [container];

  if (container === doc.scrollingElement || container === doc.documentElement) {
    targets.push(win);
  }

  state.scrollTargets = targets.map((target) => [target, onScroll]);

  for (const [target, fn] of state.scrollTargets) {
    target.addEventListener("scroll", fn, { passive: true });
  }

  state.container = container;
}

// Glide the container toward its bottom. The distance and duration are fixed for
// one glide, so its path is a clean bezier at a steady, slow speed; anything
// that arrives during the glide is picked up by the next one, which starts from
// where this one finished, keeping the motion continuous. It never moves the
// view up, so it can never fight a reader scrolling up.
function followBottom(state, container) {
  if (state.raf || !state.sticky) {
    return;
  }

  const distance = distanceFromBottom(container);

  if (distance < 1) {
    return;
  }

  const from = container.scrollTop;
  const duration = Math.min(
    Math.max(distance / FOLLOW_PX_PER_MS, FOLLOW_MIN_MS),
    FOLLOW_MAX_MS
  );
  const start = performance.now();

  const step = () => {
    state.raf = null;

    if (!state.sticky || state.container !== container) {
      return;
    }

    // The view moved up since our last write, so the reader has taken over --
    // release before writing, so we never pull them back down.
    if (state.lastSet !== null && container.scrollTop < state.lastSet - 1) {
      state.sticky = false;
      state.lastSet = null;
      return;
    }

    const t = Math.min((performance.now() - start) / duration, 1);
    const value = Math.max(from + distance * easeFollow(t), container.scrollTop);

    state.lastSet = value;
    container.scrollTop = value;

    if (t < 1) {
      state.raf = window.requestAnimationFrame(step);
    } else {
      followBottom(state, container);
    }
  };

  state.raf = window.requestAnimationFrame(step);
}

function scrollToBottom(runId) {
  const win = hostWindow();

  if (!win) {
    return;
  }

  const state = hostState(win, runId);
  const container = scrollContainer(win.document);

  if (!container) {
    return;
  }

  watchScroll(win, container, state);

  if (!state.sticky) {
    return;
  }

  followBottom(state, container);
}

// The app paints the streamed text just after the render message, so start on
// the next frame and nudge once more after layout has settled (tables can grow).
function scheduleScroll(runId) {
  window.requestAnimationFrame(() => {
    scrollToBottom(runId);
    window.setTimeout(() => scrollToBottom(runId), 120);
  });
}

function post(type, data) {
  window.parent.postMessage(
    Object.assign({ isStreamlitMessage: true, type }, data),
    "*"
  );
}

let ready = false;

window.addEventListener("message", (event) => {
  const data = event.data || {};

  if (data.type === RENDER) {
    ready = true;
    scheduleScroll((data.args || {}).run_id || "");
  }
});

// Streamlit only sends a component its render message once it has announced
// readiness, so without this the frontend would never learn that the content
// changed. Re-announce until the first render arrives, in case an early one is
// lost (the same handshake the chat box uses).
function announce() {
  post("streamlit:componentReady", { apiVersion: 1 });
}

window.addEventListener("load", announce);
announce();

const retry = window.setInterval(() => {
  if (ready) {
    window.clearInterval(retry);
  } else {
    announce();
  }
}, 250);

