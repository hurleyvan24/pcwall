// Pure timer state machine for the kitchen tablet. No DOM here: Node tests import this file directly and
// kitchen.mjs owns the screen. Every function returns new arrays. The tablet owns timer state and keeps it in
// localStorage under STORAGE_KEY; the server is stateless about timers. Chromium 83 syntax only.
export const MAX_TIMERS = 8;
export const STORAGE_KEY = "fw_timers_v1";
const MAX_SECONDS = 24 * 60 * 60;
const RESTORE_WINDOW_MS = 10 * 60 * 1000;   // a timer that ended longer ago than this is forgotten on reload

function live(timers) { return timers.filter((t) => t.state === "running" || t.state === "ringing"); }
function clone(timers) { return timers.map((t) => Object.assign({}, t)); }
function newId() { return "t_" + Date.now().toString(36) + Math.floor(Math.random() * 0xffff).toString(16); }
function lower(value) { return String(value == null ? "" : value).toLowerCase(); }
// Dice coefficient over letter bigrams: "pastaa" still finds "pasta", "soup" does not.
function similarity(a, b) {
  if (a === b) return 1;
  if (a.length < 2 || b.length < 2) return 0;
  const grams = {};
  let hits = 0;
  for (let i = 0; i < a.length - 1; i++) { const gram = a.slice(i, i + 2); grams[gram] = (grams[gram] || 0) + 1; }
  for (let j = 0; j < b.length - 1; j++) { const gram = b.slice(j, j + 2); if (grams[gram]) { grams[gram] -= 1; hits += 1; } }
  return (2 * hits) / (a.length + b.length - 2);
}

// "Timer 1", "Timer 2"... for unnamed timers; a repeated name gets a counter ("pasta 2").
export function nextLabel(timers, label) {
  const used = {};
  live(timers).forEach((t) => { used[lower(t.label)] = true; });
  const wanted = String(label == null ? "" : label).trim();
  if (!wanted) {
    let n = 1;
    while (used["timer " + n]) n += 1;
    return "Timer " + n;
  }
  if (!used[lower(wanted)]) return wanted;
  let k = 2;
  while (used[lower(wanted) + " " + k]) k += 1;
  return wanted + " " + k;
}

export function createTimer(timers, label, seconds, now, id) {
  const duration = Math.min(MAX_SECONDS, Math.round(Number(seconds) || 0));
  if (duration <= 0 || live(timers).length >= MAX_TIMERS) return { timers: clone(timers), timer: null };
  const timer = { id: id || newId(), label: nextLabel(timers, label), created_at: now, ends_at: now + duration * 1000, state: "running", ring_started_at: null };
  return { timers: clone(timers).concat([timer]), timer };
}

// Same rules as the server: the id, then the exact label, then a prefix or word, then similarity >= 0.75.
export function resolveTimer(timers, label) {
  const candidates = live(timers);
  const wanted = lower(label).trim();
  if (!wanted) return candidates.length === 1 ? candidates[0] : null;
  const byId = candidates.filter((t) => t.id === label);
  if (byId.length) return byId[0];
  const exact = candidates.filter((t) => lower(t.label) === wanted);
  if (exact.length) return exact[0];
  const loose = candidates.filter((t) => { const name = lower(t.label); return name.indexOf(wanted) === 0 || name.split(" ").indexOf(wanted) !== -1; });
  if (loose.length === 1) return loose[0];
  let best = null;
  let bestScore = 0;
  candidates.forEach((t) => { const score = similarity(wanted, lower(t.label)); if (score > bestScore) { best = t; bestScore = score; } });
  return bestScore >= 0.75 ? best : null;
}

// Extends a running timer from its end, and a ringing one from now (it goes back to running).
export function addTime(timers, idOrLabel, seconds, now) {
  const target = resolveTimer(timers, idOrLabel);
  const extra = Math.max(0, Math.round(Number(seconds) || 0)) * 1000;
  return clone(timers).map((t) => {
    if (!target || t.id !== target.id) return t;
    const base = t.state === "ringing" ? now : t.ends_at;
    return Object.assign({}, t, { state: "running", ring_started_at: null, ends_at: base + extra });
  });
}

// target is a timer id, "ringing" (every ringing timer) or "all".
export function cancelTimers(timers, target) {
  return clone(timers).filter((t) => {
    if (target === "all") return false;
    if (target === "ringing") return t.state !== "ringing";
    return t.id !== target;
  });
}

// Moves timers that reached ends_at to ringing and reports each of them exactly once.
export function tick(timers, now) {
  const newlyRinging = [];
  const next = clone(timers).map((t) => {
    if (t.state === "running" && now >= t.ends_at) { newlyRinging.push(t.id); return Object.assign({}, t, { state: "ringing", ring_started_at: now }); }
    return t;
  });
  return { timers: next, newlyRinging };
}

// One voice op from the server (or the console): { op: "set", id, label, seconds }, { op: "add", id, seconds },
// { op: "cancel", id } where id may also be "all" or "ringing". A repeated set with a known id is ignored so a
// retried reply stays idempotent; anything else leaves the timers unchanged.
export function applyVoiceTimerAction(timers, action, now) {
  const op = action && (action.op || action.action);
  if (op === "set") {
    if (action.id && timers.some((t) => t.id === action.id)) return clone(timers);
    return createTimer(timers, action.label, action.seconds, now, action.id).timers;
  }
  if (op === "add") return addTime(timers, action.id || action.label, action.seconds, now);
  if (op === "cancel") {
    const target = action.id || action.label || "all";
    if (target === "all" || target === "ringing") return cancelTimers(timers, target);
    const found = resolveTimer(timers, target);
    return found ? cancelTimers(timers, found.id) : clone(timers);
  }
  return clone(timers);
}

export function applyTimerOps(timers, ops, now) {
  return (Array.isArray(ops) ? ops : []).reduce((next, op) => applyVoiceTimerAction(next, op, now), clone(timers));
}

// Reads the persisted list back: unreadable JSON is an empty list, timers that ended more than ten minutes ago
// are dropped, and timers that ended while the tablet was away come back ringing.
export function restoreTimers(json, now) {
  let parsed;
  try { parsed = JSON.parse(json || "[]"); } catch (error) { parsed = []; }
  if (!Array.isArray(parsed)) return [];
  return parsed
    .filter((t) => t && typeof t.id === "string" && typeof t.ends_at === "number" && now - t.ends_at < RESTORE_WINDOW_MS)
    .map((t) => {
      const ended = now >= t.ends_at;
      return {
        id: t.id,
        label: String(t.label == null ? "" : t.label),
        created_at: typeof t.created_at === "number" ? t.created_at : t.ends_at,
        ends_at: t.ends_at,
        state: ended ? "ringing" : "running",
        ring_started_at: ended ? (typeof t.ring_started_at === "number" ? t.ring_started_at : t.ends_at) : null,
      };
    });
}

// "07:59" under an hour, "1:00:00" from an hour up; nothing below zero.
export function formatRemainingMs(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n) => (n < 10 ? "0" : "") + n;
  return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s);
}
export const formatRemaining = formatRemainingMs;

// A fact card stays for 20 seconds, or until 3 seconds after the spoken answer ends if that is later.
export function factCardDeadline(shownAt, ttsEndsAt) { return Math.max(shownAt + 20000, (ttsEndsAt || 0) + 3000); }

// Mic pill reducer: idle | listening | busy | muted. Events: tap, longpress, wake, endpoint, done, error.
export function pillNext(state, event) {
  if (event === "longpress") return state === "muted" ? "idle" : "muted";
  if (state === "muted") return "muted";
  if (event === "wake" || (event === "tap" && state === "idle")) return "listening";
  if (event === "tap") return "idle";
  if (event === "endpoint" && state === "listening") return "busy";
  if (event === "done" || event === "error") return "idle";
  return state;
}

function defaultStorage() {
  try { return typeof localStorage === "undefined" ? null : localStorage; } catch (error) { return null; }
}

// The tablet's one timer list with persistence and change notifications. Options: storage (anything with
// getItem/setItem, null for memory only; defaults to localStorage), key (defaults to STORAGE_KEY) and now (a clock,
// for tests). Subscribers receive (timers, change) after every commit; storage failures never block a change.
export function createTimerStore(options) {
  const opts = options || {};
  const key = opts.key || STORAGE_KEY;
  const now = typeof opts.now === "function" ? opts.now : () => Date.now();
  const storage = opts.storage === undefined ? defaultStorage() : opts.storage;
  const listeners = [];
  let timers = [];
  function persist() {
    if (!storage) return false;
    try { storage.setItem(key, JSON.stringify(timers)); return true; } catch (error) { return false; }
  }
  function commit(next, change) {
    timers = next;
    persist();
    listeners.slice().forEach((listener) => { listener(timers, change); });
    return timers;
  }
  return {
    key,
    get() { return timers; },
    snapshot() { return timers.map((t) => ({ id: t.id, label: t.label, ends_at: t.ends_at, state: t.state })); },
    ringing() { return timers.filter((t) => t.state === "ringing"); },
    subscribe(listener) {
      listeners.push(listener);
      return () => { const index = listeners.indexOf(listener); if (index !== -1) listeners.splice(index, 1); };
    },
    restore() {
      let raw = null;
      try { raw = storage ? storage.getItem(key) : null; } catch (error) { raw = null; }
      return commit(restoreTimers(raw, now()), { type: "restore" });
    },
    set(label, seconds, id) {
      const result = createTimer(timers, label, seconds, now(), id);
      if (result.timer) commit(result.timers, { type: "set", id: result.timer.id });
      return result.timer;
    },
    add(idOrLabel, seconds) {
      const target = resolveTimer(timers, idOrLabel);
      if (!target) return null;
      commit(addTime(timers, target.id, seconds, now()), { type: "add", id: target.id });
      return timers.filter((t) => t.id === target.id)[0] || null;
    },
    cancel(target) {
      const before = timers.length;
      commit(cancelTimers(timers, target), { type: "cancel", id: target });
      return before - timers.length;
    },
    apply(ops) { return commit(applyTimerOps(timers, ops, now()), { type: "ops", ops: Array.isArray(ops) ? ops : [] }); },
    tick() {
      const result = tick(timers, now());
      if (result.newlyRinging.length) commit(result.timers, { type: "ring", ids: result.newlyRinging });
      return result.newlyRinging;
    },
    replace(next) { return commit(Array.isArray(next) ? clone(next) : [], { type: "replace" }); },
  };
}
