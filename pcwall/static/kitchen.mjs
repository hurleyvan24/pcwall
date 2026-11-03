// Timer bar, screensaver strip, ringing overlay, fact card and the typed fallback for Dot, the kitchen companion.
// Chromium 83 WebView syntax only (gate: tests/test_pcwall_frontend.py). Every node is built with
// createElement and textContent; no markup string is ever assigned. The DOM is touched only inside functions so
// Node tests can import the module, and one requestAnimationFrame loop runs while timers exist.
import * as T from "./timers.mjs";

export const IDS = {
  bar: "timer-bar", saver: "ss-timers", ring: "timer-ring-modal", ringLabel: "timer-ring-label", card: "fact-card-overlay",
  typed: "voice-text", form: "kitchen-ask-form", input: "voice-input", pill: "mic-pill",
};
const CHIME_EVERY_MS = 1500;
const CHIME_STOP_MS = 5 * 60 * 1000;   // the chip keeps ringing until it is dismissed; the chime gives up first
const WARNING_MS = 60000;
const SOURCES = { "simple.wikipedia.org": "Simple English Wikipedia", "en.wikipedia.org": "Wikipedia" };
const OFFLINE_TEXT = "Dot can't answer right now. Try again in a moment.";

const k = { store: null, deps: null, raf: 0, nodes: {}, ringingCount: 0, chimeTimer: 0, chimeStarted: 0, speaking: false, cardTimer: 0 };

function noop() {}
function deps() { return k.deps || { toast: noop, chime: noop, unlockAudio: noop, getAudioContext: () => null }; }
function dom(override) { return override || document; }
function byId(id, doc) { return typeof doc.getElementById === "function" ? doc.getElementById(id) : null; }
function make(doc, tag, className, text) {
  const node = doc.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
function ringing(timer) { return timer.state === "ringing"; }
function sourceName(host) { return SOURCES[String(host == null ? "" : host).toLowerCase()] || "Encyclopedia"; }

// The tablet's timer list (window.__fwTimerStore). Created on first use; every change redraws the screen.
export function timerStore() {
  if (!k.store) {
    k.store = T.createTimerStore();
    k.store.subscribe(rerender);
  }
  return k.store;
}

function buildChip(doc, timer, now) {
  const chip = make(doc, "div", "timer-chip");
  chip.setAttribute("data-id", timer.id);
  chip.appendChild(make(doc, "span", "timer-label", timer.label));
  const time = make(doc, "span", "timer-time", "");
  chip.appendChild(time);
  const track = make(doc, "span", "timer-progress");
  const bar = make(doc, "span", "timer-progress-bar");
  track.appendChild(bar);
  chip.appendChild(track);
  const entry = { chip, time, bar, text: "", fraction: "" };
  paint(entry, timer, now);
  return entry;
}
// Per frame only what changed is written: the text once a second, the bar's transform when it has moved.
function paint(entry, timer, now) {
  const remaining = timer.ends_at - now;
  const text = T.formatRemainingMs(remaining);
  if (entry.text !== text) {
    entry.text = text;
    entry.time.textContent = text;
    entry.chip.classList.toggle("is-warning", timer.state === "running" && remaining > 0 && remaining < WARNING_MS);
    entry.chip.classList.toggle("is-ringing", timer.state === "ringing");
  }
  const span = Math.max(1, timer.ends_at - timer.created_at);
  const fraction = Math.max(0, Math.min(1, remaining / span)).toFixed(3);
  if (entry.fraction !== fraction) {
    entry.fraction = fraction;
    entry.bar.style.transform = "scaleX(" + fraction + ")";
  }
}

// Projects the timers onto the screen: chips in the main bar and the screensaver strip, plus the ringing
// overlay's visibility and label. Returns the chip nodes per timer id so the frame loop can update them in place.
export function renderKitchenView(timers, now, doc) {
  const d = dom(doc);
  const list = Array.isArray(timers) ? timers : [];
  const nodes = {};
  [IDS.bar, IDS.saver].forEach((id) => {
    const strip = byId(id, d);
    if (!strip) return;
    clear(strip);
    list.forEach((timer) => {
      const entry = buildChip(d, timer, now);
      strip.appendChild(entry.chip);
      nodes[timer.id] = (nodes[timer.id] || []).concat([entry]);
    });
    strip.hidden = !list.length;
  });
  const rung = list.filter(ringing);
  const ring = byId(IDS.ring, d);
  if (ring) {
    const label = byId(IDS.ringLabel, d);
    if (label) label.textContent = rung.map((timer) => timer.label).join(", ");
    ring.hidden = !rung.length;
  }
  return { nodes, ringing: rung.map((timer) => timer.id) };
}

function rerender() {
  const wasRinging = k.ringingCount;
  const view = renderKitchenView(timerStore().get(), Date.now());
  k.nodes = view.nodes;
  k.ringingCount = view.ringing.length;
  if (k.ringingCount) {
    startChime();
    if (!wasRinging) focusDismiss();
  } else stopChime();
  startFrame();
}
function focusDismiss() {
  const ring = byId(IDS.ring, dom());
  const button = ring && typeof ring.querySelector === "function" ? ring.querySelector('[data-action="dismiss"]') : null;
  if (!button || typeof button.focus !== "function") return;
  try { button.focus({ preventScroll: true }); } catch (error) { /* focus is a courtesy */ }
}
function startFrame() {
  if (k.raf || typeof requestAnimationFrame !== "function" || !timerStore().get().length) return;
  k.raf = requestAnimationFrame(frame);
}
function frame() {
  k.raf = 0;
  const store = timerStore();
  store.tick();   // a timer reaching ends_at commits "ringing" and the subscription redraws everything
  const now = Date.now();
  const timers = store.get();
  timers.forEach((timer) => { (k.nodes[timer.id] || []).forEach((entry) => { paint(entry, timer, now); }); });
  if (timers.length && !k.raf) k.raf = requestAnimationFrame(frame);
}

function startChime() {
  if (k.chimeTimer) return;
  k.chimeStarted = Date.now();
  const beat = () => {
    k.chimeTimer = 0;
    if (!timerStore().ringing().length || Date.now() - k.chimeStarted > CHIME_STOP_MS) return;
    if (!k.speaking) deps().chime();
    k.chimeTimer = setTimeout(beat, CHIME_EVERY_MS);
  };
  beat();
}
function stopChime() { clearTimeout(k.chimeTimer); k.chimeTimer = 0; }

// Builds the card's nodes: an https thumbnail when there is one, the title, the kid-safe bullets and the source.
export function renderFactCard(card, doc) {
  const d = dom(doc);
  const root = make(d, "div", "fact-inner");
  const url = String((card && card.thumbnail_url) || "");
  if (/^https:\/\//i.test(url)) {
    const image = make(d, "img", "fact-thumb");
    image.setAttribute("alt", "");
    image.setAttribute("referrerpolicy", "no-referrer");
    image.setAttribute("src", url);
    image.onerror = () => { image.hidden = true; };
    root.appendChild(image);
  }
  const body = make(d, "div", "fact-body");
  body.appendChild(make(d, "h3", "", String((card && card.title) || "")));
  const bullets = make(d, "ul", "");
  (card && Array.isArray(card.bullets) ? card.bullets : []).forEach((bullet) => { bullets.appendChild(make(d, "li", "", String(bullet))); });
  body.appendChild(bullets);
  body.appendChild(make(d, "p", "fact-source", sourceName(card && card.source)));
  root.appendChild(body);
  return root;
}

export function hideFactCard() {
  clearTimeout(k.cardTimer);
  k.cardTimer = 0;
  const overlay = byId(IDS.card, dom());
  if (!overlay) return;
  overlay.classList.remove("is-shown");
  overlay.hidden = true;
}

// Shows an encyclopedia card for 20 seconds, or until 3 seconds after the spoken answer ends if that is later.
// Tapping the card hides it early. Returns the delay so callers and tests can see the deadline.
export function showFactCard(card, ttsEndsAt) {
  const overlay = byId(IDS.card, dom());
  if (!overlay || !card) return 0;
  clear(overlay);
  overlay.appendChild(renderFactCard(card));
  overlay.hidden = false;
  void overlay.offsetWidth;   // commit the display change so the opacity transition can play
  overlay.classList.add("is-shown");
  const shownAt = Date.now();
  const delay = Math.max(0, T.factCardDeadline(shownAt, ttsEndsAt || shownAt) - shownAt);
  clearTimeout(k.cardTimer);
  k.cardTimer = setTimeout(hideFactCard, delay);
  return delay;
}

export function snapshotTimers() { return timerStore().snapshot(); }
export function setSpeaking(flag) { k.speaking = Boolean(flag); }

// One /api/voice reply: timer ops land in the store, a card is shown, refused store ops are explained, and the
// spoken text is toasted when there is no audio to play and no card carrying the answer.
export function applyVoiceReply(reply, ttsEndsAt) {
  if (!reply || typeof reply !== "object") return;
  if (Array.isArray(reply.timer_ops) && reply.timer_ops.length) timerStore().apply(reply.timer_ops);
  if (reply.card) showFactCard(reply.card, ttsEndsAt);
  (Array.isArray(reply.store_ops) ? reply.store_ops : []).forEach((op) => {
    if (op && op.ok === false && op.detail && op.detail !== "parents_only") deps().toast(String(op.detail));
  });
  if (!reply.audio && reply.spoken_text && !reply.card) deps().toast(String(reply.spoken_text));
}

// Bridge for voice.mjs and the console (window.__fwRunVoiceAction): a whole reply, or one timer op such as
// { op: "set", label: "pasta", seconds: 480 }. Returns the timers the server should see next.
export function runVoiceAction(action, ttsEndsAt) {
  if (action && typeof action === "object") {
    if (action.op || action.action) timerStore().apply([action]);
    else applyVoiceReply(action, ttsEndsAt);
  }
  return snapshotTimers();
}

function setSending(flag) {
  const form = byId(IDS.form, dom());
  if (!form) return;
  form.setAttribute("aria-busy", flag ? "true" : "false");
  const button = typeof form.querySelector === "function" ? form.querySelector("button") : null;
  if (button) button.disabled = Boolean(flag);
}

// The typed fallback: the same JSON form of POST /api/voice that voice.mjs will use for audio.
export function sendText(text) {
  const question = String(text == null ? "" : text).trim();
  if (!question) return Promise.resolve(null);
  deps().unlockAudio();
  setSending(true);
  const body = { text: question, meta: { client_now_ms: Date.now(), timers: snapshotTimers(), trigger: "typed" } };
  return fetch("/api/voice", { method: "POST", credentials: "same-origin", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
    .then((response) => response.json().catch(() => ({})).then((data) => {
      if (!response.ok || !data || data.ok === false) throw new Error((data && data.error) || "voice_failed");
      return data;
    }))
    .then((reply) => { applyVoiceReply(reply, Date.now()); return reply; })
    .catch(() => { deps().toast(OFFLINE_TEXT); return null; })
    .then((reply) => { setSending(false); return reply; });
}

function dismissRinging() { timerStore().cancel("ringing"); }
function extendRinging(seconds) {
  const ops = timerStore().ringing().map((timer) => ({ op: "add", id: timer.id, seconds }));
  if (ops.length) timerStore().apply(ops);
}
// The ring buttons and the fact card listen on window in the capture phase: app.mjs wakes the screensaver from a
// document-level capture listener and swallows that same tap, and a ringing timer must answer the first touch.
function onCaptureClick(event) {
  const target = event.target;
  if (!target || typeof target.closest !== "function") return;
  const button = target.closest("#" + IDS.ring + " [data-action]");
  if (button) {
    const action = button.getAttribute("data-action");
    if (action === "dismiss") dismissRinging();
    else if (action === "plus-one") extendRinging(60);
    return;
  }
  if (target.closest("#" + IDS.card)) hideFactCard();
}

// deps: { toast, chime, unlockAudio, getAudioContext } from app.mjs. Returns the timer store.
export function initKitchen(sharedDeps) {
  k.deps = sharedDeps || null;
  const doc = dom();
  const store = timerStore();
  store.restore();   // the subscription draws the chips, reopens the ringing overlay and restarts the chime
  if (typeof window !== "undefined" && window.addEventListener) {
    window.addEventListener("click", onCaptureClick, true);
    // Create the AudioContext inside the first gesture so a timer set by typing can chime later.
    window.addEventListener("pointerdown", () => { deps().unlockAudio(); }, { once: true, passive: true, capture: true });
  }
  const form = byId(IDS.form, doc);
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const input = byId(IDS.input, doc);
      const text = input ? String(input.value || "").trim() : "";
      if (!text) return;
      input.value = "";
      sendText(text);
    });
  }
  // Until voice.mjs takes the pill over, "Type to ask" puts the cursor in the field below it.
  const pill = byId(IDS.pill, doc);
  if (pill) {
    const focusInput = () => {
      const typed = byId(IDS.typed, doc);
      const input = byId(IDS.input, doc);
      if (input && !(typed && typed.hidden) && typeof input.focus === "function") input.focus();
    };
    pill.addEventListener("click", focusInput);
    pill.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); focusInput(); } });
  }
  doc.addEventListener("keydown", (event) => {
    const ring = byId(IDS.ring, doc);
    if (event.key === "Escape" && ring && !ring.hidden) dismissRinging();
  });
  doc.addEventListener("visibilitychange", () => { if (doc.visibilityState === "visible") startFrame(); });
  return store;
}
