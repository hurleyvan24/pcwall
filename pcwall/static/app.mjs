import { burst } from "./confetti.mjs";
import { resolveDisplay, applyScale, autoFitScale, saveDisplay, loadDisplay, renderDisplayCard, deviceInfo } from "./display.mjs";

// Vanilla modules for Chromium 83. The service owns chores, stars and permissions.
const $ = (selector) => document.querySelector(selector);
const CACHE_KEY = "family-wall-state-v1";
const SOUND_KEY = "family-wall-sound";
const THEME_KEY = "family-wall-theme";
// Light from sunrise until half an hour after sunset, dark the rest of the night. "auto" is the
// default; "light" and "dark" pin the wall. The service sends Springfield's sun times with the weather.
const THEME_MODES = ["auto", "light", "dark"];
const THEME_LABELS = { auto: "Auto", light: "Light", dark: "Dark" };
const THEME_COLORS = { light: "#f3efea", dark: "#15191d" };   // mirrors --background in app.css
const SUN_FALLBACK = { sunrise: "06:30", sunset: "18:30" };
const DUSK_GRACE = 30;   // minutes after sunset that still count as day
// The morning banner: today's weather and Heritage uniform guidance from 6:00 until 9:00. Shorts
// from a 72° high; a sweater at a low of 60° or less, a warm jacket at 52° or less; a rain
// reminder from a 30% chance or any drizzle, rain or storm code (Open-Meteo WMO codes).
const MORNING_WINDOW = { from: "06:00", to: "09:00" };
const SHORTS_HIGH = 72;
const JACKET_LOW = 52;
const SWEATER_LOW = 60;
const RAIN_CHANCE = 30;
const RAIN_CODES = [51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99];
const RAIN_WARNING = "Might rain — bring a jacket";
// app.css is shared by every view; the banner carries its few rules itself so the stylesheet stays put.
const BANNER_STYLE = "flex:1 1 100%;display:flex;flex-wrap:wrap;align-items:center;margin:0 0 10px;padding:16px 22px;border:1px solid var(--line);border-radius:14px;font-size:1.35rem;line-height:1.5";
const BANNER_ITEM = "margin:2px 14px 2px 0";
// The what-to-wear pills read from across the kitchen: bigger than the stock .pill rule.
const BANNER_PILL = `${BANNER_ITEM};font-size:1.25rem;font-weight:600;padding:8px 16px`;
// Front-camera wake: a 32x24 luminance thumbnail every 600ms, compared with the one before it.
const CAMERA_FRAME_MS = 600;
const CAMERA_SETTLE_MS = 3000;   // after sleep() the screen's own light goes out; nothing in that window counts as motion
const CAMERA_WIDTH = 32;
const CAMERA_HEIGHT = 24;
const KIDS = ["riley", "casey", "taylor"];
const ROUTINE_SECTIONS = [
  { kind: "morning", title: "Morning", time: "Before 11 AM", pattern: /morning/i, icon: "☀" },
  { kind: "after_school", title: "After School", time: "11 AM – 6 PM", pattern: /after|school/i, icon: "⌂" },
  { kind: "bedtime", title: "Bedtime", time: "From 6 PM", pattern: /bed|night|evening/i, icon: "☾" },
];
// color and tint are the dark palette; ink and wash are their light-mode counterparts, deep
// enough to read as text on white and pale enough to carry charcoal text (both pass WCAG AA).
const PEOPLE = {
  riley: { name: "Riley", color: "#A855F7", tint: "#33263f", ink: "#7E22CE", wash: "#F3E8FF", emoji: "🌸" },
  casey: { name: "Casey", color: "#3B82F6", tint: "#23334b", ink: "#1D4ED8", wash: "#DBEAFE", emoji: "🚀" },
  taylor: { name: "Taylor", color: "#10B981", tint: "#1d3c33", ink: "#15803D", wash: "#DCFCE7", emoji: "🦁" },
  alex: { name: "Alex", color: "#9CA3AF", tint: "#363b42", ink: "#4B5563", wash: "#E5E7EB", emoji: "M" },
  jordan: { name: "Jordan", color: "#F59E0B", tint: "#443723", ink: "#B45309", wash: "#FEF3C7", emoji: "B" },
  family: { name: "Family", color: "#EAC586", tint: "#3c352a", ink: "#A16207", wash: "#FEF6D6", emoji: "☀" },
};
const VIEWS = {
  chores: ["LITTLE STEPS, BIG DAYS", "Today's chores", "Everyone has a part to play."],
  calendar: ["", "Calendar", ""],
  weather: ["Springfield FORECAST", "Weather", "7-day outlook for the family."],
  meals: ["AROUND THE TABLE", "What's for Dinner?", "One less thing to wonder about."],
  groceries: ["A LITTLE LIST FOR THE WEEK", "Groceries", "Tap it when it's in the cart."],
  photos: ["THE GOOD STUFF", "Our favorite moments", "A little reminder of what it's all for."],
  controls: ["", "Home Controls", "Lights, speakers and quick actions for home."],
  settings: ["MAKE YOURSELF AT HOME", "Settings", "Small adjustments for this screen."],
};
// Swipe order across the wall, left to right. Settings stays a deliberate tap away.
const SWIPE_VIEWS = ["chores", "calendar", "weather", "meals", "groceries", "photos"];
const SWIPE_IGNORE = "#dialog, #pairing, input, textarea, select, .routine-tabs";
const pending = new Map();
const pendingForms = new Set();
const drafts = { grocery: "", meal: "" };
let state = null;
let receivedAt = Date.now();
let currentView = "chores";
let calendarView = "week";
let calendarMonthOffset = 0;   // months from today's month that Month view shows; 0 is the current month
const routineSelections = new Map();
let routineWindow = null;
let online = false;
let pairing = false;
let source = null;
let refreshFlight = null;
let refreshTimer = null;
let lastTouch = Date.now();
let ignoreTapsUntil = 0;
let asleep = false;
let returnView = "chores";
let slide = 0;
let slideTimer = null;
let slideGeneration = 0;
let night = false;
let audioContext = null;
let soundEnabled = true;
let toastTimer = null;
let lastCelebration = 0;
let dialogMember = null;
let dialogTab = "store";
let dialogFocus = null;
let installPrompt = null;
let lastDayRefresh = 0;
let automaticSleep = false;
let sleptAt = 0;   // when the wall last fell asleep; the camera waits CAMERA_SETTLE_MS before trusting a change
let cameraRequest = null;
let cameraStream = null;
let cameraVideo = null;
let cameraCanvas = null;
let cameraTimer = null;
let cameraPrevLuma = null;
let swipeStart = null;
let themeMode = "auto";
let appliedTheme = "";
let display = null;   // the resolved UI scale from display.mjs: { preset, scale, source }
let autoFitWatched = false;
let displayRefitTimer = null;

try { soundEnabled = localStorage.getItem(SOUND_KEY) !== "off"; } catch (error) { /* Storage is optional. */ }
try {
  const savedTheme = localStorage.getItem(THEME_KEY);
  if (THEME_MODES.indexOf(savedTheme) !== -1) themeMode = savedTheme;
} catch (error) { /* Storage is optional. */ }

function escapeHtml(value) {
  const swaps = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value == null ? "" : value).replace(/[&<>"']/g, (character) => swaps[character]);
}

function list(value) { return Array.isArray(value) ? value : []; }
function number(value) { return Math.max(0, Number(value) || 0); }
function whole(value) { return Math.trunc(Number(value) || 0); }   // a star balance keeps its sign; number() is for counts
function member(id) {
  const fallback = PEOPLE[id] || PEOPLE.family;
  const actual = state && list(state.members).find((person) => person.id === id);
  return Object.assign({ id, name: fallback.name, emoji: fallback.emoji }, actual || {});
}
function colors(id) {
  const palette = PEOPLE[id] || PEOPLE.family;
  return `--member-color:${palette.color};--member-tint:${palette.tint};--member-ink:${palette.ink};--member-wash:${palette.wash}`;
}
function personDetail(person) {
  return [person.age != null ? `Age ${person.age}` : "", person.grade || ""].filter(Boolean).join(" · ");
}
function kidBoards() {
  return KIDS.map((id) => list(state.kids).find((kid) => kid.member === id)
    || { member: id, routines: [], chores: [], pending: [], available: 0 });
}
function minutesOf(value) {
  const match = /^(\d{1,2}):(\d{2})/.exec(value || "");
  return match ? Number(match[1]) * 60 + Number(match[2]) : 0;
}
function inWindow(minutes, from, to) {
  return from <= to ? minutes >= from && minutes < to : minutes >= from || minutes < to;
}
function activeRoutine(minutes) {
  return minutes < 660 ? "morning" : minutes < 1080 ? "after_school" : "bedtime";
}
function syncRoutineWindow(now) {
  const next = `${now.day}:${activeRoutine(now.minutes)}`;
  if (routineWindow === next) return false;
  routineWindow = next;
  routineSelections.clear();
  return true;
}
function clock() {
  const iso = state && state.now;
  const parsed = Date.parse(iso);
  if (!Number.isFinite(parsed)) {
    const now = new Date();
    return { day: `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`, minutes: now.getHours() * 60 + now.getMinutes() };
  }
  const offset = /([+-])(\d{2}):(\d{2})$/.exec(iso);
  const shift = offset ? (offset[1] === "-" ? -1 : 1) * (Number(offset[2]) * 60 + Number(offset[3])) : 0;
  const wall = new Date(parsed + Date.now() - receivedAt + shift * 60000);
  return { day: wall.toISOString().slice(0, 10), minutes: wall.getUTCHours() * 60 + wall.getUTCMinutes() };
}
function timeText(minutes) {
  const hours = Math.floor(minutes / 60) % 24;
  return `${hours % 12 || 12}:${String(minutes % 60).padStart(2, "0")} ${hours < 12 ? "AM" : "PM"}`;
}
// Sun times arrive as local "18:21" (or a local ISO stamp); anything unreadable uses the fallback.
function sunMinutes(value, fallback) {
  const match = /(?:^|T)(\d{1,2}):(\d{2})(?!\d)/.exec(String(value == null ? "" : value));
  const hours = match ? Number(match[1]) : 24;
  const minutes = match ? Number(match[2]) : 60;
  return hours < 24 && minutes < 60 ? hours * 60 + minutes : minutesOf(fallback);
}
function sunWindow() {
  const weather = state && state.weather || {};
  return {
    sunrise: sunMinutes(weather.sunrise, SUN_FALLBACK.sunrise),
    dusk: (sunMinutes(weather.sunset, SUN_FALLBACK.sunset) + DUSK_GRACE) % 1440,
  };
}
function daytime(minutes) {
  const sun = sunWindow();
  return inWindow(minutes, sun.sunrise, sun.dusk);
}
function themeFor(minutes) {
  if (themeMode === "light" || themeMode === "dark") return themeMode;
  return daytime(minutes) ? "light" : "dark";
}
// Runs on every clock tick and render; it only touches the document when the answer changes.
function applyTheme() {
  const next = themeFor(clock().minutes);
  if (next === appliedTheme) return;
  appliedTheme = next;
  const root = document.documentElement;
  if (!root || !root.classList) return;
  root.classList.remove("theme-light", "theme-dark");
  root.classList.add(`theme-${next}`);
  let meta = $('meta[name="theme-color"]');
  if (!meta && document.head && typeof document.createElement === "function") {
    meta = document.createElement("meta");
    meta.setAttribute("name", "theme-color");
    document.head.appendChild(meta);
  }
  if (meta && typeof meta.setAttribute === "function") meta.setAttribute("content", THEME_COLORS[next]);
}
// ── Display scale (display.mjs). A ?preset= or ?scale= query parameter, the choice saved on this screen and the
// server's display default resolve to one --ui-scale on <html>. Auto-Fit follows the viewport, so while it is the
// preset the wall listens for resize and orientation changes and refits a beat after the last one. ──
function queryDisplay() {
  try {
    const params = new URLSearchParams(location.search);
    const preset = params.get("preset") || params.get("display") || "";
    const scale = params.get("scale") || "";
    return preset || scale ? { preset: preset || undefined, scale: scale || undefined } : null;
  } catch (error) { return null; }
}
function serverDisplayDefault() {
  const config = state && state.display;
  if (!config) return null;
  if (typeof config === "string") return config;
  return typeof config === "object" ? { preset: config.preset || config.default_preset, scale: config.scale } : null;
}
function resolveWallDisplay() {
  return resolveDisplay({ query: queryDisplay(), stored: loadDisplay(), serverDefault: serverDisplayDefault(), viewport: deviceInfo() });
}
function sameDisplay(a, b) {
  return Boolean(a && b) && a.preset === b.preset && a.scale === b.scale && a.source === b.source;
}
function applyDisplay(resolved) {
  if (!resolved || sameDisplay(display, resolved)) return;
  display = resolved;
  applyScale(resolved.scale);
  watchAutoFit(resolved.preset === "auto");
}
function watchAutoFit(active) {
  if (active === autoFitWatched) return;
  autoFitWatched = active;
  const method = active ? "addEventListener" : "removeEventListener";
  window[method]("resize", scheduleAutoFit);
  window[method]("orientationchange", scheduleAutoFit);
  if (!active) clearTimeout(displayRefitTimer);
}
function scheduleAutoFit() {
  clearTimeout(displayRefitTimer);
  displayRefitTimer = setTimeout(refitDisplay, 150);
}
function refitDisplay() {
  if (!display || display.preset !== "auto") return;
  const viewport = deviceInfo();
  const scale = autoFitScale(viewport.width, viewport.height);
  if (scale === display.scale) return;
  display = { preset: "auto", scale, source: "auto" };
  applyScale(scale);
  if (currentView === "settings") render();   // the slider and device readout follow the new fit
}
// The display card's onSave: a preset tap or a slider stop. The choice is what gets saved, not the number it
// resolved to, so Auto-Fit keeps refitting on the next boot instead of freezing at today's viewport.
function saveWallDisplay(choice) {
  const resolved = resolveDisplay({ stored: choice, viewport: deviceInfo() });
  saveDisplay(choice);
  applyDisplay(resolved);
  render();
}
function mountDisplayCard() {
  const container = document.getElementById("display-settings");
  if (!container) return;
  renderDisplayCard(container, Object.assign({ device: deviceInfo() }, display || resolveWallDisplay()), saveWallDisplay);
}
function addDays(day, count) {
  const date = new Date(`${day}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + count);
  return date.toISOString().slice(0, 10);
}
function dateText(day, options) {
  return new Date(`${day}T12:00:00Z`).toLocaleDateString("en-US", Object.assign({ timeZone: "UTC" }, options));
}
// The first day of the month that Month view shows: today's month moved by calendarMonthOffset.
// Date.UTC carries a move past December or before January into the right year.
function viewedMonthFirstDay() {
  const today = String(state && state.today || "");
  const year = Number(today.slice(0, 4));
  const month = Number(today.slice(5, 7));
  if (!(year > 0 && month >= 1 && month <= 12)) return `${today.slice(0, 7)}-01`;
  return new Date(Date.UTC(year, month - 1 + calendarMonthOffset, 1, 12, 0, 0)).toISOString().slice(0, 10);
}
function dayLabel(day) {
  if (day === state.today) return "Today";
  if (day === addDays(state.today, 1)) return "Tomorrow";
  return dateText(day, { weekday: "long" });
}
function eventTime(event) { return event.all_day ? "All day" : timeText(minutesOf(String(event.start).slice(11, 16))); }
function agenda(startDay = state.today, count = 7) {
  return Array.from({ length: count }, (_, index) => {
    const day = addDays(startDay, index);
    const events = list(state.calendar && state.calendar.events).filter((event) => {
      if (!event) return false;
      const start = String(event.start || "");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(start.slice(0, 10)) || !Number.isFinite(Date.parse(start))) return false;
      return event.all_day ? start.slice(0, 10) <= day && day < String(event.end || addDays(start.slice(0, 10), 1)).slice(0, 10)
        : start.slice(0, 10) === day;
    }).sort((a, b) => Number(Boolean(b.all_day)) - Number(Boolean(a.all_day)) || String(a.start).localeCompare(String(b.start)));
    return { day, events };
  });
}
function itemKey(memberId, item) { return `${memberId}:${item}`; }
function effectiveDone(memberId, item) {
  const operation = pending.get(itemKey(memberId, item.item));
  return operation ? operation.value : Boolean(item.done);
}
// Automated steps are ticked by the Math app, never by a finger on the wall (ops.automated_step refuses a
// kiosk tick server-side too). The kitchen wall shows them locked; the parent phone keeps the tap so a parent
// can still correct one by hand. Anything that is not the parent phone counts as the wall, so a missing or
// unknown role can never hand a kid the Math step.
function autoStep(item) { return Boolean(item && (item.auto || item.id === "math" || item.item === "math")); }
function isKiosk() { return !(state && state.role === "parent"); }
function lockedStep(item) { return autoStep(item) && isKiosk(); }
// The progress ring, the "X of Y complete" caption and the day's Completion / On-Task Bonus row count only the
// steps and chores a kid ticks by hand. An automated step is extra stars: it never holds the day back and it
// never finishes the day on its own.
function progress(board) {
  const items = list(board.routines).reduce((all, routine) => all.concat(list(routine.steps)), []).concat(list(board.chores)).filter((item) => !autoStep(item));
  return { done: items.filter((item) => effectiveDone(board.member, item)).length, total: items.length };
}
function ring(done, total) {
  const circumference = 2 * Math.PI * 16;
  const filled = total ? circumference * Math.min(done / total, 1) : 0;
  return `<svg class="progress-ring" viewBox="0 0 40 40" aria-hidden="true"><circle class="track" cx="20" cy="20" r="16"/><circle class="bar" cx="20" cy="20" r="16" stroke-dasharray="${filled.toFixed(1)} ${circumference.toFixed(1)}"/></svg>`;
}
function empty(icon, title, detail) {
  return `<div class="empty-state"><span class="empty-icon" aria-hidden="true">${icon}</span><h2>${escapeHtml(title)}</h2><p>${escapeHtml(detail)}</p></div>`;
}
function photoUrl(photo) {
  try {
    const url = new URL(photo.url, window.location.href);
    return url.origin === window.location.origin && url.pathname.indexOf("/photos/") === 0 ? url.href : "";
  } catch (error) { return ""; }
}
function photos() { return list(state && state.photos).filter((photo) => photo && photoUrl(photo)); }

function weatherDays() {
  return list(state && state.weather && state.weather.days).filter((day) => day
    && /^\d{4}-\d{2}-\d{2}$/.test(day.date) && Number.isFinite(Date.parse(day.date)));
}
function weatherValue(value, unit = "") {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value))
    ? `${Math.round(Number(value))}${unit}` : "—";
}
function weatherSky(code, isDay = true) {
  if (code === null || code === undefined) return ["◇", "Conditions unavailable"];
  code = Number(code);
  if (code === 0) return [isDay ? "☀" : "☾", "Clear sky"];
  if (code === 1 || code === 2) return ["⛅", "Partly cloudy"];
  if (code === 3) return ["☁", "Overcast"];
  if (code === 45 || code === 48) return ["🌫", "Fog"];
  if (code >= 51 && code <= 57) return ["🌦", "Drizzle"];
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return ["🌧", "Rain"];
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return ["❄", "Snow"];
  if (code >= 95 && code <= 99) return ["⛈", "Thunderstorms"];
  return ["◇", "Conditions unavailable"];
}
function weatherPill(day) {
  const forecast = weatherDays().find((row) => row.date === day);
  if (!forecast) return "";
  const sky = weatherSky(forecast.code);
  const rain = Number(forecast.precip_prob) > 0 ? ` · ${weatherValue(forecast.precip_prob, "%")} rain` : "";
  const unit = state.weather.units && state.weather.units.temp || "degrees";
  return `<div class="day-weather" title="${escapeHtml(`${forecast.sky || sky[1]} · High / low in ${unit}`)}"><span aria-hidden="true">${sky[0]}</span> ${weatherValue(forecast.hi, "°")} / ${weatherValue(forecast.lo, "°")}${rain}</div>`;
}
function renderSidebarWeather() {
  const weather = state.weather || {};
  const current = weather.current;
  const today = weatherDays().find((row) => row.date === clock().day);
  const highLow = today ? `<span class="weather-high-low">H: ${weatherValue(today.hi, "°")} · L: ${weatherValue(today.lo, "°")}</span>` : "";
  const temperature = current?.temperature ?? weather.temp ?? weather.temperature;
  const emoji = current?.emoji ?? weather.condition_emoji ?? weather.emoji;
  const label = current?.label ?? weather.condition;
  const rawUnit = typeof weather.units === "string" ? weather.units : weather.units?.temp;
  const unit = ({ celsius: "°C", fahrenheit: "°F" })[rawUnit] || rawUnit || "°";
  if (weatherValue(temperature) !== "—") {
    return `<span><span aria-hidden="true">${escapeHtml(emoji || "◇")}</span> <strong>${escapeHtml(weatherValue(temperature, unit))}</strong> ${escapeHtml(label || "Conditions unavailable")}</span>${highLow}`;
  }
  if (current && String(current.time || "").slice(0, 10) === clock().day
      && weatherValue(current.temperature_2m) !== "—") {
    const sky = weatherSky(current.weather_code, current.is_day !== 0);
    return `<span title="${escapeHtml(`Springfield conditions at ${current.time}`)}"><span aria-hidden="true">${escapeHtml(emoji || sky[0])}</span> <strong>${escapeHtml(weatherValue(current.temperature_2m, unit))}</strong> ${escapeHtml(label || sky[1])}</span>${highLow}`;
  }
  if (!today) return "Weather unavailable";
  const sky = weatherSky(today.code);
  return `<span class="weather-label">Today's forecast</span><span aria-hidden="true">${sky[0]}</span> ${escapeHtml(today.sky || sky[1])}${highLow}`;
}
// Saturday or Sunday for a YYYY-MM-DD day; anything else (or no day at all) is a school day.
function isWeekend(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(day || ""))) return false;
  const dow = new Date(`${day}T12:00:00Z`).getUTCDay();
  return dow === 0 || dow === 6;
}
// What to wear to Heritage, from one day's forecast ({ hi, lo, precip_prob, code }) and the dress
// day a parent approved for it, if any. On a weekend there is no uniform, just shorts or pants.
// Pure, so the cutoffs can be checked without a screen.
function computeUniformAdvice(dayForecast, approvedDressDay, day) {
  const forecast = dayForecast || {};
  const hi = Number(forecast.hi);
  const lo = Number(forecast.lo);
  const weekend = isWeekend(day || forecast.date);
  const bottoms = weekend ? (hi >= SHORTS_HIGH ? "Shorts" : "Pants") : (hi >= SHORTS_HIGH ? "Uniform shorts" : "Uniform pants");
  const layer = lo <= JACKET_LOW ? "Warm jacket" : lo <= SWEATER_LOW ? "Sweater" : "No extra layer needed";
  const rain = Number(forecast.precip_prob) >= RAIN_CHANCE || RAIN_CODES.indexOf(Number(forecast.code)) !== -1 ? RAIN_WARNING : "";
  const dressDay = approvedDressDay && approvedDressDay.title
    ? { title: String(approvedDressDay.title), summary: String(approvedDressDay.summary || "") } : null;
  const summaryText = [bottoms, layer, rain, dressDay ? `Heritage Dress Day Today: ${dressDay.title}` : ""].filter(Boolean).join(" · ");
  return { bottoms, layer, rain, dressDay, summaryText };
}
function dressDays() {
  return list(state && state.dress_days).filter((item) => item && typeof item === "object" && item.id);
}
function approvedDressDay(day) {
  return dressDays().find((item) => item.status === "approved" && item.date === day) || null;
}
// From 6:00 to 9:00 the wall opens with the day's weather and what to wear to Heritage, with a
// dress day a parent approved for today beside it. A dress day found in school mail asks for a
// tap (Approve or Dismiss) at any hour, from the wall or a phone. Both draw inside #notes.
function renderMorningBanner() {
  const now = clock();
  const rows = [];
  if (inWindow(now.minutes, minutesOf(MORNING_WINDOW.from), minutesOf(MORNING_WINDOW.to))) {
    const today = weatherDays().find((row) => row.date === state.today);
    const approved = approvedDressDay(state.today);
    const sky = today ? weatherSky(today.code) : null;
    const advice = today ? computeUniformAdvice(today, approved, state.today) : null;
    const chance = today && Number(today.precip_prob) > 0 ? ` · ${weatherValue(today.precip_prob, "%")} rain` : "";
    const weather = today
      ? `<span class="morning-weather" style="${BANNER_ITEM}"><span aria-hidden="true">${sky[0]}</span> <strong>H: ${weatherValue(today.hi, "°")} · L: ${weatherValue(today.lo, "°")}</strong> ${escapeHtml(today.sky || sky[1])}${chance}</span>`
      : `<span class="morning-weather" style="${BANNER_ITEM}">Forecast unavailable</span>`;
    const outfit = advice
      ? `<span class="pill morning-outfit" style="${BANNER_PILL}">👕 ${escapeHtml(advice.bottoms)}</span><span class="pill morning-outfit" style="${BANNER_PILL}">🧥 ${escapeHtml(advice.layer)}</span>${advice.rain ? `<span class="pill morning-rain" style="${BANNER_PILL}">☔ ${escapeHtml(advice.rain)}</span>` : ""}`
      : "";
    const dressDay = approved
      ? `<span class="morning-dress-day" style="${BANNER_ITEM};font-weight:600">🎉 Heritage Dress Day Today: <strong>${escapeHtml(approved.title)}</strong>${approved.summary ? ` <small style="font-weight:400">${escapeHtml(approved.summary)}</small>` : ""}</span>`
      : "";
    rows.push(`<div class="morning-banner" role="status" aria-label="Morning weather and uniform" style="${BANNER_STYLE}"><span class="morning-title" style="${BANNER_ITEM};font-weight:600">☀ Good morning</span>${weather}${outfit}${dressDay}</div>`);
  }
  dressDays().filter((item) => item.status === "pending").forEach((item) => {
    const day = String(item.date || "");
    const when = /^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(Date.parse(day))
      ? `<time datetime="${day}">${escapeHtml(dateText(day, { weekday: "short", month: "short", day: "numeric" }))}</time>`
      : escapeHtml(day || "date to confirm");
    rows.push(`<div class="dress-day-pending" style="${BANNER_STYLE};border-left:3px solid var(--accent)"><span style="${BANNER_ITEM}">📧 Heritage Dress Day (${when}): <strong>${escapeHtml(item.title)}</strong>${item.summary ? ` <small>${escapeHtml(item.summary)}</small>` : ""}</span><span class="dress-day-actions"><button type="button" class="primary" data-action="approve-dress-day" data-id="${escapeHtml(item.id)}">Approve</button> <button type="button" data-action="dismiss-dress-day" data-id="${escapeHtml(item.id)}">Dismiss</button></span></div>`);
  });
  return rows.join("");
}
async function decideDressDay(op, button) {
  const id = String(button && button.dataset && button.dataset.id || "");
  if (!id) return;
  await act(op, { id });
  await refresh(true);
  toast(op === "approve_dress_day" ? "Dress day approved. It will be in the morning banner." : "Dress day dismissed.");
}

// Video messages: a parent's clip waits on the wall for 24 hours. The service keeps
// state.video_messages trimmed to the live ones, so anything with an id and a URL is current.
function activeVideoMessages() {
  return list(state && state.video_messages).filter((item) => item && item.id && item.url);
}
function findVideoMessage(id) {
  return activeVideoMessages().find((item) => String(item.id) === String(id)) || null;
}
const VIDEO_REACTIONS = ["❤️ Love you!", "👍 Got it!", "😂 Haha!"];
// Each clip is one warm card in the notes strip; the whole card is the Watch button.
function renderVideoMessagesBanner() {
  return activeVideoMessages().map((item) => `<div class="note video-msg-banner"><button type="button" class="video-msg-play" data-action="play-video" data-id="${escapeHtml(item.id)}"><span class="video-msg-icon" aria-hidden="true">💌</span><span class="video-msg-copy"><strong>You have a video message from ${escapeHtml(item.sender || "Dad")}!</strong><small>${escapeHtml(item.caption || "Tap to watch · Stays for 24 hours")}${item.watched_at ? " · Watched ✓" : ""}</small></span><span class="video-msg-cta">▶ Watch</span></button></div>`).join("");
}
// The <video> inside the open player sheet, if there is one.
function videoPlayer() {
  const content = $("#dialog-content");
  if (content && typeof content.querySelector === "function") return content.querySelector(".wall-video-player");
  return $(".wall-video-player");
}
// Chrome's autoplay policy starts the clip silent unless the page has been tapped; the sheet always
// opens from a tap, so the player is unmuted and started here, and the Play with Sound button is the
// retry for a WebView that still kept it quiet.
function playWithSound(player) {
  if (!player) return false;
  player.muted = false;
  player.volume = 1;
  if (typeof player.play === "function") {
    const playPromise = player.play();
    if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
  }
  return true;
}
// The player sheet: the clip starts as soon as it opens, the wall records the watch once per
// opening, and three big reactions send a reply back to whoever recorded it.
function showVideoModal(videoId) {
  stopVideoReplyStream();
  const item = findVideoMessage(videoId);
  if (!item) { toast("That video message is gone from the wall."); return false; }
  unlockAudio();
  if ($("#dialog").hidden) dialogFocus = document.activeElement;
  dialogMember = null;
  dialogTab = "store";
  const sender = item.sender || "Dad";
  const reactions = VIDEO_REACTIONS.map((label) => `<button type="button" class="video-reaction" data-action="react-video" data-id="${escapeHtml(item.id)}" data-reaction="${escapeHtml(label)}">${escapeHtml(label)}</button>`).join("");
  $("#dialog-content").innerHTML = `<div class="sheet-heading video-sheet-heading"><div class="kid-name"><h2 id="dialog-title">💌 Video message from ${escapeHtml(sender)}</h2><small>Stays on the wall for 24 hours</small></div><button type="button" class="close-button" data-action="close-dialog" aria-label="Close">×</button></div><video class="wall-video-player" src="${escapeHtml(item.url)}" controls autoplay playsinline></video>${item.caption ? `<p class="video-caption">${escapeHtml(item.caption)}</p>` : ""}<div class="video-reactions" role="group" aria-label="Send ${escapeHtml(sender)} a reaction"><button type="button" class="video-reaction video-sound-btn" data-action="unmute-video">🔊 Play with Sound</button>${reactions}<button type="button" class="video-reaction video-reply-btn" data-action="start-video-reply" data-id="${escapeHtml(item.id)}">🎥 Record Video Reply</button></div><input type="file" id="video-reply-file" accept="video/*" capture="user" hidden><div id="video-reply-recorder" class="video-reply-recorder" hidden></div>`;
  $("#dialog").hidden = false;
  $("#app").setAttribute("aria-hidden", "true");
  playWithSound(videoPlayer());
  const close = $("#dialog-content .close-button");
  if (close) close.focus({ preventScroll: true });
  act("watch_video_message", { id: item.id }).then(() => refresh(true)).catch(() => {});
  return true;
}
// Play with Sound: unmute and restart the clip inside the tap, the one place Chrome lets sound through.
function unmuteVideo() {
  unlockAudio();
  const player = videoPlayer();
  playWithSound(player);
  toast("Sound is on! 🔊");
  return Boolean(player);
}
async function reactToVideo(button) {
  const id = String(button && button.dataset && button.dataset.id || "");
  const reaction = String(button && button.dataset && button.dataset.reaction || "");
  if (!id || !reaction) return;
  const item = findVideoMessage(id);
  const sender = item && item.sender || "Dad";
  const buttons = Array.from(document.querySelectorAll('#dialog [data-action="react-video"]'));
  buttons.forEach((entry) => { entry.disabled = true; });
  try {
    await act("react_video_message", { id, reaction });
    toast(`Sent to ${sender}! 💌`);
    if (button.classList && typeof button.classList.add === "function") button.classList.add("sent");
    await refresh(true);
  } finally { buttons.forEach((entry) => { entry.disabled = false; }); }
}

// ── Video replies: a kid records a short clip in the player sheet and it goes straight to the parent's
// Telegram chat. The front camera and MediaRecorder when the wall has them; otherwise the native camera
// app through a file picker. Nothing is kept on the wall. ──
const VIDEO_REPLY_MAX_SECONDS = 20;
const VIDEO_REPLY_MAX_BYTES = 16 * 1024 * 1024;   // Telegram's sendVideo cap; the service enforces the same limit
const VIDEO_REPLY_MIME_TYPES = ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4"];
let videoReplyStream = null;
let videoReplyRecorder = null;
let videoReplyChunks = [];
let videoReplyTimer = null;
let videoReplyId = "";
let videoReplySession = 0;   // bumped by every start and stop, so a camera prompt that resolves late is ignored
let videoReplyStopping = false;   // Stop & Send was tapped; the recorder's stop event is on its way
let videoReplyBusy = false;
let videoReplyStarting = false;   // startVideoReply() is clearing the last reply; the camera wake must not come back yet
// The motion-wake camera was switched off so the reply could have the front camera; it comes back when the reply ends.
let cameraWakePaused = false;

function videoReplySupported() {
  return typeof navigator !== "undefined" && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === "function" && typeof MediaRecorder === "function";
}
function videoReplyMimeType() {
  if (typeof MediaRecorder !== "function" || typeof MediaRecorder.isTypeSupported !== "function") return "";
  for (const candidate of VIDEO_REPLY_MIME_TYPES) {
    try { if (MediaRecorder.isTypeSupported(candidate)) return candidate; } catch (error) { /* let the recorder pick */ }
  }
  return "";
}
function setVideoReplyStatus(text) {
  const box = $("#video-reply-recorder");
  const status = box && typeof box.querySelector === "function" ? box.querySelector(".video-reply-status") : null;
  if (status) status.textContent = text;
}
// Releases the camera and microphone, drops any recording in progress and clears the auto-stop timer.
// Safe at any time: closing the sheet, cancelling, opening another clip, or after a finished send.
function stopVideoReplyStream() {
  videoReplySession += 1;   // any getUserMedia() still pending from an earlier start now resolves as stale
  if (videoReplyTimer !== null) { clearTimeout(videoReplyTimer); videoReplyTimer = null; }
  videoReplyId = "";
  videoReplyStopping = false;
  const recorder = videoReplyRecorder;
  videoReplyRecorder = null;
  videoReplyChunks = [];
  if (recorder) {
    recorder.ondataavailable = null;
    recorder.onstop = null;
    recorder.onerror = null;
    if (recorder.state !== "inactive") { try { recorder.stop(); } catch (error) { /* already stopped */ } }
  }
  const stream = videoReplyStream;
  videoReplyStream = null;
  if (stream && typeof stream.getTracks === "function") {
    stream.getTracks().forEach((track) => { try { track.stop(); } catch (error) { /* already stopped */ } });
  }
  const box = $("#video-reply-recorder");
  if (box) { box.innerHTML = ""; box.hidden = true; }
  const starter = $("#dialog-content .video-reply-btn");
  if (starter) starter.disabled = false;
  // The reply is over: hand the front camera back to the motion wake, if the reply took it.
  if (cameraWakePaused && videoReplyId === "" && !videoReplyStarting) { cameraWakePaused = false; startCameraWake(); }
}
// The file-picker fallback: a tablet without MediaRecorder, a denied camera or an http wall still
// records a reply with the native camera app and sends it the same way.
function pickVideoReplyFile(videoId) {
  const input = $("#video-reply-file");
  if (!input) { toast("This screen can't record video."); return false; }
  input.onchange = () => {
    const file = input.files && input.files[0];
    input.value = "";
    if (file) uploadVideoReply(videoId, file, file.type || "video/mp4");
  };
  if (typeof input.click === "function") input.click();
  return true;
}
function startVideoReply(button) {
  const videoId = String(button && button.dataset && button.dataset.id || "");
  if (!videoId || videoReplyBusy) return Promise.resolve(false);
  // Android Chrome refuses a second stream from the front camera while the motion wake holds it at 64x48,
  // and the fallback file picker then opens the rear camera. Let go of it first; the reply's end brings it back.
  if (cameraStream || cameraRequest) { cameraWakePaused = true; stopCameraWake(); }
  videoReplyStarting = true;
  try { stopVideoReplyStream(); } finally { videoReplyStarting = false; }
  const session = ++videoReplySession;   // this start's ticket: a later start, stop or cancel outdates it
  videoReplyId = videoId;
  if (!videoReplySupported()) { videoReplyId = ""; return Promise.resolve(pickVideoReplyFile(videoId)); }
  // Disabled before the permission prompt, so a double-tap can't open a second camera session while it waits.
  if (button) button.disabled = true;
  const starter = $("#dialog-content .video-reply-btn");
  if (starter) starter.disabled = true;
  let request;
  try {
    request = navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } }, audio: true });
  } catch (error) { request = Promise.reject(error); }
  return Promise.resolve(request).then((stream) => {
    const release = () => { if (stream && typeof stream.getTracks === "function") stream.getTracks().forEach((track) => { try { track.stop(); } catch (error) { /* gone */ } }); };
    // Stale if the sheet closed, another clip was opened, or a newer start/stop happened while the camera asked.
    if (session !== videoReplySession || videoReplyId !== videoId || $("#dialog").hidden) { release(); return false; }
    const mimeType = videoReplyMimeType();
    let recorder;
    try { recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream); }
    catch (error) { release(); stopVideoReplyStream(); return pickVideoReplyFile(videoId); }
    videoReplyStream = stream;
    videoReplyRecorder = recorder;
    videoReplyChunks = [];
    recorder.ondataavailable = (event) => { if (event && event.data && event.data.size > 0) videoReplyChunks.push(event.data); };
    recorder.onerror = () => { stopVideoReplyStream(); toast("The camera stopped. Try again!"); };
    recorder.onstop = () => {
      const chunks = videoReplyChunks;
      const type = String(recorder.mimeType || mimeType || "video/webm");
      stopVideoReplyStream();
      if (!chunks.length) { toast("Nothing was recorded. Try again!"); return; }
      uploadVideoReply(videoId, new Blob(chunks, { type }), type.split(";")[0].trim());
    };
    const box = $("#video-reply-recorder");
    if (box) {
      box.innerHTML = `<video class="video-reply-preview" autoplay muted playsinline></video><div class="video-reply-controls"><span class="video-reply-status">Recording… up to ${VIDEO_REPLY_MAX_SECONDS}s</span><button type="button" class="video-reaction video-reply-stop" data-action="stop-video-reply">⏹ Stop &amp; Send</button><button type="button" class="video-reaction video-reply-cancel" data-action="cancel-video-reply">Cancel</button></div>`;
      box.hidden = false;
      const preview = typeof box.querySelector === "function" ? box.querySelector("video") : null;
      if (preview) { try { preview.srcObject = stream; } catch (error) { /* no live preview on this browser */ } }
    }
    recorder.start();
    videoReplyTimer = setTimeout(finishVideoReply, VIDEO_REPLY_MAX_SECONDS * 1000);
    return true;
  }).catch(() => {
    // Cancelled, closed or restarted while the camera asked for permission: nothing to fall back to.
    if (session !== videoReplySession || videoReplyId !== videoId || $("#dialog").hidden) return false;
    stopVideoReplyStream();   // re-enables the Record button before the file picker takes over
    return pickVideoReplyFile(videoId);
  });
}
// Stop & Send: the recorder's stop event assembles the clip and uploads it. The browser delivers the
// last dataavailable and the stop event asynchronously, so a second tap while they are on their way
// is a no-op: firing onstop by hand here would upload before the final chunk arrived.
function finishVideoReply() {
  if (videoReplyTimer !== null) { clearTimeout(videoReplyTimer); videoReplyTimer = null; }
  if (!videoReplyRecorder || videoReplyStopping) return Boolean(videoReplyStopping);
  videoReplyStopping = true;
  setVideoReplyStatus("Sending…");
  const box = $("#video-reply-recorder");
  const stopButton = box && typeof box.querySelector === "function" ? box.querySelector(".video-reply-stop") : null;
  if (stopButton) stopButton.disabled = true;
  try { videoReplyRecorder.stop(); } catch (error) { stopVideoReplyStream(); toast("The camera stopped. Try again!"); return false; }
  return true;
}
function cancelVideoReply() {
  const active = videoReplyRecorder !== null || videoReplyStream !== null || videoReplyId !== "";
  stopVideoReplyStream();
  if (active) toast("Video reply cancelled.");
  return active;
}
async function uploadVideoReply(videoId, payload, mimeType) {
  if (!videoId || !payload) return false;
  if ((Number(payload.size) || 0) > VIDEO_REPLY_MAX_BYTES) { toast("That video is too big to send. Try a shorter one!"); return false; }
  const item = findVideoMessage(videoId);
  const sender = item && item.sender || "Dad";
  videoReplyBusy = true;
  toast("Sending your video reply… 🎥");
  try {
    const response = await fetch("/api/video-reply?id=" + encodeURIComponent(videoId), {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": mimeType || "video/webm", "X-Video-Id": videoId }, body: payload,
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401) { showPairing(); throw new Error(data.error || "Please pair this screen again."); }
    if (!response.ok || data.ok === false) throw new Error(data.error || `The wall couldn't send that (${response.status}).`);
    toast(`Video reply sent to ${sender}! 🎥`);
    refresh(true).catch(() => {});
    return true;
  } catch (error) {
    toast(error && error.message || "Couldn't send that video reply. Try again!");
    return false;
  } finally { videoReplyBusy = false; }
}

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500);
}
function setOnline(value) {
  online = value;
  $("#connection").textContent = value ? "All up to date" : "Reconnecting…";
  $("#connection").classList.toggle("online", value);
  $("#offline").hidden = value || pairing;
}
function cacheState() {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify({ state, receivedAt })); } catch (error) { /* A full cache must not interrupt a tap. */ }
}
async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== "/api/pair") {
      showPairing();
      throw new Error(data.error || "Please pair this screen again.");
    }
    if (!response.ok || data.ok === false) throw new Error(data.error || `The wall couldn't save that (${response.status}).`);
    return data;
  } catch (error) {
    if (error.name === "AbortError" || error instanceof TypeError) {
      setOnline(false);
      throw new Error("Can't reach the wall. Reconnect and try again.");
    }
    throw error;
  } finally { clearTimeout(timeout); }
}
function act(op, args) { return api("/api/do", { op, args }); }
async function refresh(afterWrite = false) {
  if (pairing) return;
  if (refreshFlight) {
    if (!afterWrite) return refreshFlight;
    await refreshFlight.catch(() => {});
    return refresh(true);
  }
  refreshFlight = (async () => {
    const next = await api("/api/state");
    if (!next.now || !next.today || !Array.isArray(next.members)) throw new Error("Waiting for the wall's next update.");
    if (state && Number(next.revision) < Number(state.revision)) return;
    const before = state;
    state = next;
    applyDisplay(resolveWallDisplay());   // a new server display default lands here; a saved choice still wins
    // A later reconnect must also retire local acknowledgements from earlier writes.
    homeAccepted.forEach((patch, id) => { if (homeReported(id, next)) homeAccepted.delete(id); });
    receivedAt = Date.now();
    setOnline(true);
    cacheState();
    render();
    celebrateChanges(before, next);
    listen();
  })();
  try { await refreshFlight; } catch (error) { setOnline(false); throw error; }
  finally { refreshFlight = null; }
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { refresh(true).catch(() => {}); }, 150);
}
function listen() {
  if (source || pairing || !("EventSource" in window)) return;
  source = new EventSource("/api/events");
  source.addEventListener("hello", (event) => {
    try {
      const hello = JSON.parse(event.data);
      if (!state || hello.revision !== state.revision || !online) scheduleRefresh();
      else setOnline(true);
    } catch (error) { scheduleRefresh(); }
  });
  source.addEventListener("change", (event) => {
    let data = null;
    try { data = event && event.data ? JSON.parse(event.data) : null; } catch (error) { data = null; }
    if (data && Array.isArray(data.kinds) && data.kinds.indexOf("reload") !== -1) {
      // A parent or PCAgent asked every screen to reload itself (the reload_browsers op).
      if (window.location && typeof window.location.reload === "function") window.location.reload();
      return;
    }
    scheduleRefresh();
  });
  source.onerror = () => setOnline(false); // EventSource owns reconnection and Last-Event-ID.
}

function renderClock() {
  const now = clock();
  const date = dateText(now.day, { weekday: "long", month: "long", day: "numeric" });
  $("#clock").textContent = timeText(now.minutes);
  $("#date").textContent = date;
  $("#saver-time").textContent = timeText(now.minutes);
  $("#saver-date").textContent = date;
}
function renderHeader() {
  $("#sidebar-weather").innerHTML = renderSidebarWeather();
  $("#sidebar-kids").innerHTML = kidBoards().map((board) => {
    const person = member(board.member);
    const count = progress(board);
    return `<button type="button" class="member-chip" style="${colors(board.member)}" data-action="kid-sheet" data-member="${escapeHtml(board.member)}" aria-label="${escapeHtml(person.name)}: ${count.done} of ${count.total} chores complete"><span class="chip-avatar">${ring(count.done, count.total)}<span aria-hidden="true">${escapeHtml(person.emoji || person.name[0])}</span></span><span class="chip-label">${escapeHtml(person.name)}<small>${count.done} of ${count.total} done</small></span></button>`;
  }).join("");
  const banner = renderMorningBanner();
  const videos = renderVideoMessagesBanner();
  const notes = list(state.notes).map((note) => `<button type="button" class="note" data-action="copy-text" data-text="${escapeHtml(note.text)}" title="Tap to copy" style="user-select:text;cursor:pointer">${escapeHtml(note.text)} · <small>Tap to copy</small></button>`).join("");
  $("#notes").innerHTML = banner + videos + notes;
  $("#notes").hidden = !(banner || videos || notes);
  renderTicker();
}
// Keep feed documents and legacy headline lists readable. Preserve scroll position
// across refreshes unless the headlines or configured duration change.
function renderTicker() {
  const ticker = $("#news-ticker") || $(".news-ticker");
  if (!ticker) return;
  const headlines = Array.isArray(state.news) ? state.news : state.news?.headlines || state.news?.items || [];
  const items = list(headlines).map((entry) => typeof entry === "string" ? { text: entry }
    : { text: entry?.title || entry?.text, source: entry?.source }).filter((entry) => entry.text);
  const configuredSpeed = Number(state.settings?.news?.ticker_speed || 60);
  const seconds = Number.isFinite(configuredSpeed) && configuredSpeed > 0 ? configuredSpeed : 60;
  const signature = JSON.stringify([seconds, items.map((entry) => [entry.source || "", entry.text])]);
  ticker.hidden = !items.length;
  if (ticker.dataset.signature === signature) return;
  ticker.dataset.signature = signature;
  ticker.innerHTML = items.length ? `<div class="news-ticker__track" style="--ticker-duration:${seconds}s">${items.map((entry) => `<span class="news-ticker__item">${entry.source ? `<strong>${escapeHtml(entry.source)}</strong>` : ""}${escapeHtml(entry.text)}</span>`).join("")}</div>` : "";
}
function choreRow(board, item, extra) {
  const done = effectiveDone(board.member, item);
  const busy = pending.has(itemKey(board.member, item.item));
  const auto = autoStep(item);
  const locked = auto && isKiosk();
  // A locked row drops data-action, so the click handler never reaches tickChore and the wall never posts a
  // complete/uncomplete for it; it keeps data-member and data-item so the row stays addressable. A finished one
  // still shows its check and stars, only dimmed by app.css. Every other row renders exactly as it always has.
  const stars = extra || (auto && number(item.stars) > 0) ? `<small>${number(item.stars)} stars</small>` : "";
  const note = auto ? '<small class="auto-note">Completed in Math app</small>' : "";
  const lock = locked ? ' aria-disabled="true" disabled' : busy ? " disabled" : "";
  return `<button type="button" class="chore-row${locked ? " auto-step disabled" : ""}${done ? " done" : ""}"${locked ? "" : ' data-action="tick"'} data-member="${escapeHtml(board.member)}" data-item="${escapeHtml(item.item)}" aria-pressed="${done}" aria-busy="${busy}"${lock}><span class="chore-emoji" aria-hidden="true">${escapeHtml(item.emoji || (extra ? "⭐" : "✓"))}</span><span class="row-label"><span>${escapeHtml(item.title)}</span>${stars}${note}</span><span class="check" aria-hidden="true">✓</span></button>`;
}
// display.kid_cols from the server pins the kid grid to that many columns; otherwise app.css auto-fits them.
function kidColsAttr() {
  const config = state && state.display;
  const cols = config && typeof config === "object" ? whole(config.kid_cols) : 0;
  return cols >= 1 && cols <= 6 ? ` data-kid-cols="${cols}" style="--kid-cols:${cols}"` : "";
}
function renderChores() {
  const now = clock();
  syncRoutineWindow(now);
  const currentKind = activeRoutine(now.minutes);
  return `<div class="kids-grid"${kidColsAttr()}>${kidBoards().map((board) => {
    const person = member(board.member);
    const count = progress(board);
    const percent = count.total ? Math.round(count.done / count.total * 100) : 0;
    const routines = list(board.routines);
    const selected = routineSelections.get(board.member) || currentKind;
    const tabs = `<div class="routine-tabs" role="group" aria-label="${escapeHtml(person.name)}'s routines">${ROUTINE_SECTIONS.map((section) => `<button type="button" id="routine-${escapeHtml(board.member)}-${section.kind}" data-action="routine" data-member="${escapeHtml(board.member)}" data-routine="${section.kind}" aria-pressed="${selected === section.kind}">${section.title}</button>`).join("")}</div>`;
    const used = new Set();
    // The day's Completion / On-Task Bonus closes the Bedtime list. It is a summary row rather than a
    // button (no data-action, nothing to tap) and lights up once every step and chore of the day is
    // ticked, counting taps the wall has not confirmed yet so it answers the last tick at once.
    const allDone = count.total > 0 && count.done === count.total;
    const bonusRow = `<div class="chore-row bonus-row${allDone ? " done" : ""}" role="note" aria-label="Completion / On-Task Bonus: 2 stars for doing all chores for the day"><span class="chore-emoji" aria-hidden="true">🌟</span><span class="row-label"><span>Completion / On-Task Bonus</span><small>2 stars · All chores done for the day</small><span class="sr-only">${allDone ? "Earned today." : "Not earned yet."}</span></span><span class="check" aria-hidden="true">✓</span></div>`;
    function routineSection(routine, title, time, icon, current = false, trailer = "") {
      return `<section class="routine${current ? " current" : ""}"><div class="routine-title"><h3>${icon} ${escapeHtml(title)}</h3><time>${escapeHtml(time)}</time></div>${routine && list(routine.steps).length ? routine.steps.map((item) => choreRow(board, item, false)).join("") : '<p class="routine-empty">All clear for now.</p>'}${trailer}</section>`;
    }
    let groups = tabs + ROUTINE_SECTIONS.map((section) => {
      const routine = routines.find((candidate) => !used.has(candidate.id) && (candidate.kind === section.kind || (!candidate.kind && section.pattern.test(`${candidate.id} ${candidate.title}`))));
      if (routine) used.add(routine.id);
      return selected === section.kind ? routineSection(routine, section.title, section.time, section.icon, selected === currentKind, section.kind === "bedtime" ? bonusRow : "") : "";
    }).join("");
    groups += routines.filter((routine) => !used.has(routine.id)).map((routine) => routineSection(routine, routine.title, routine.start ? timeText(minutesOf(routine.start)) : "", "✧")).join("");
    if (list(board.chores).length) groups += `<section class="routine"><div class="routine-title"><h3>✧ A little extra</h3><time>Earn stars</time></div>${board.chores.map((item) => choreRow(board, item, true)).join("")}</section>`;
    const streak = Math.max(0, ...routines.map((routine) => number(routine.streak)));
    const detail = personDetail(person);
    const balance = whole(board.available);
    return `<article id="kid-${escapeHtml(board.member)}" class="kid-column" style="${colors(board.member)}"><header class="kid-head"><span class="avatar" aria-hidden="true">${escapeHtml(person.emoji || person.name[0])}</span><div class="kid-name"><h2><button type="button" class="kid-name-btn" data-action="kid-sheet" data-member="${escapeHtml(board.member)}" aria-label="${escapeHtml(person.name)}: stars and history">${escapeHtml(person.name)}</button></h2><small>${escapeHtml(detail || "Ready for a good day")}</small></div><button type="button" class="star-bank${balance < 0 ? " negative" : ""}" data-action="stars" data-member="${escapeHtml(board.member)}" aria-label="${escapeHtml(person.name)}: ${balance} stars and rewards">★ ${balance}</button></header><div class="kid-progress"><div class="progress-caption"><span>${count.done} of ${count.total} complete</span><span>${percent}%</span></div><div class="progress-track"><span class="progress-fill" style="width:${percent}%"></span></div></div>${groups}<footer class="board-footer"><span>${streak > 1 ? `🔥 ${streak}-day streak` : "A fresh start, every day"}</span><b>${count.total && count.done === count.total ? "All done! ★" : "You've got this"}</b></footer></article>`;
  }).join("")}</div>`;
}
function memberBadge(id) {
  return `<span class="member-badge" style="${colors(id)}">${escapeHtml(member(id).name)}</span>`;
}
function eventCard(event) {
  const ids = list(event.members);
  const title = String(event.title || "Untitled event");
  const minimum = /minimum\s*day|early\s*(dismissal|release)/i.test(title);
  const soccer = /soccer/i.test(title) && (ids.indexOf("taylor") !== -1 || /\bleo\b/i.test(title));
  return `<article class="event" style="${colors(ids[0] || "family")}"><time>${escapeHtml(eventTime(event))}</time><h3>${escapeHtml(event.title || "Untitled event")}</h3><div class="event-members">${(ids.length ? ids : ["family"]).map(memberBadge).join("")}</div>${minimum ? '<span class="event-badge">↘ Minimum day</span>' : ""}${soccer ? '<span class="event-badge">🩹 Broken arm</span>' : ""}${event.location ? `<span class="event-badge">${escapeHtml(event.location)}</span>` : ""}</article>`;
}
function renderCalendar() {
  const grid = calendarView === "month" ? renderMonth()
    : `<div class="week-grid">${agenda().map(({ day, events }) => `<section class="calendar-day${day === state.today ? " today" : ""}">${weatherPill(day)}<header class="day-head"><p>${escapeHtml(dayLabel(day))}</p><strong>${Number(day.slice(8))}</strong> <small>${escapeHtml(dateText(day, { month: "short" }))}</small></header><div class="day-events">${events.map(eventCard).join("") || '<p class="day-free">A little breathing room.</p>'}</div></section>`).join("")}</div>`;
  return `${grid}<div class="calendar-legend" aria-label="Family colors">${["riley", "casey", "taylor", "alex", "jordan", "family"].map(memberBadge).join("")}</div>`;
}
function renderMonth() {
  const firstDay = viewedMonthFirstDay();
  const first = new Date(`${firstDay}T12:00:00Z`);
  const dayCount = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  const leading = first.getUTCDay();
  const trailing = (7 - (leading + dayCount) % 7) % 7;
  const padding = '<div class="month-padding" aria-hidden="true"></div>';
  const weekdays = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].map((day) => `<div class="month-weekday">${day}</div>`).join("");
  const days = agenda(firstDay, dayCount).map(({ day, events }) => `<section class="month-day${day === state.today ? " today" : ""}" data-day="${day}" aria-label="${escapeHtml(dateText(day, { weekday: "long", month: "long", day: "numeric" }))}"><time class="month-number" datetime="${day}"${day === state.today ? ' aria-current="date"' : ""}>${Number(day.slice(8))}</time>${events.map((event) => {
    const ids = list(event.members);
    return `<article class="month-event" style="${colors(ids[0] || "family")}"><time>${escapeHtml(eventTime(event))}</time><h3>${escapeHtml(event.title || "Untitled event")}</h3><div class="event-members">${(ids.length ? ids : ["family"]).map(memberBadge).join("")}</div></article>`;
  }).join("")}</section>`).join("");
  return `<div class="month-scroll"><div class="month-grid" aria-label="${escapeHtml(dateText(firstDay, { month: "long", year: "numeric" }))}">${weekdays}${padding.repeat(leading)}${days}${padding.repeat(trailing)}</div></div>`;
}
function renderWeatherCards(days, units) {
  return `<div class="weather-grid">${days.map((day) => {
    const sky = weatherSky(day.code);
    return `<article class="weather-card${day.date === state.today ? " today" : ""}"><header><h2>${escapeHtml(dayLabel(day.date))}</h2><time datetime="${day.date}">${escapeHtml(dateText(day.date, { month: "short", day: "numeric" }))}</time></header><span class="weather-icon" aria-hidden="true">${sky[0]}</span><p class="weather-condition">${escapeHtml(day.sky || sky[1])}</p><div class="weather-temperatures"><span><small>High</small><strong>${weatherValue(day.hi, "°")}</strong></span><span><small>Low</small><strong>${weatherValue(day.lo, "°")}</strong></span></div><dl class="weather-details"><div><dt>Rain chance</dt><dd>${weatherValue(day.precip_prob, "%")}</dd></div><div><dt>Wind</dt><dd>${escapeHtml(weatherValue(day.wind, units.wind ? ` ${units.wind}` : ""))}</dd></div><div><dt>Humidity</dt><dd>${weatherValue(day.humidity, "%")}</dd></div></dl></article>`;
  }).join("")}</div>`;
}
function renderWeather() {
  const days = weatherDays().filter((day) => day.date >= state.today).slice(0, 7);
  const units = state.weather && state.weather.units || {};
  const home = days.length
    ? `<p class="forecast-location">Springfield, CA${units.temp ? ` · Temperatures in ${escapeHtml(units.temp)}` : ""}</p>${renderWeatherCards(days, units)}`
    : empty("☁", "Forecast unavailable", "Springfield weather will return when the forecast service reconnects.");
  const trips = list(state.weather && state.weather.trips).filter((trip) => trip);
  const cards = trips.map((trip) => {
    const tripDays = list(trip.days).filter((day) => day && /^\d{4}-\d{2}-\d{2}$/.test(day.date)
      && Number.isFinite(Date.parse(day.date)) && trip.start_date <= day.date && day.date <= trip.end_date);
    if (!tripDays.length) return "";
    const dates = `${dateText(trip.start_date, { month: "short", day: "numeric" })} – ${dateText(trip.end_date, { month: "short", day: "numeric" })}`;
    return `<article class="trip-weather-card"><header class="trip-weather-heading"><h3>${escapeHtml(trip.name)}</h3><p>${escapeHtml(dates)} · ${escapeHtml(trip.location)}</p></header>${renderWeatherCards(tripDays, units)}</article>`;
  }).join("");
  const note = trips.length ? "Forecasts will appear closer to departure."
    : "No upcoming trips planned. Trips added in PCAgent appear here with destination forecasts.";
  return `${home}<section class="trip-weather-section"><h2>Upcoming Trips</h2>${cards || `<p class="trip-weather-note">${note}</p>`}</section>`;
}
function renderMeals() {
  const form = state.role === "parent" ? `<form class="add-form" data-form="meal"><label class="sr-only" for="meal-title">Tonight's dinner</label><input id="meal-title" name="title" maxlength="200" placeholder="What's cooking tonight?" value="${escapeHtml(drafts.meal)}" required autocomplete="off"><button class="primary" type="submit">Set dinner</button></form>` : "";
  const days = Array.from({ length: 7 }, (_, index) => {
    const day = addDays(state.today, index);
    const indexed = list(state.meals)[index];
    const row = list(state.meals).find((meal) => meal.day === day || meal.date === day)
      || (indexed && !indexed.day && !indexed.date ? indexed : null);
    const meal = row && row.meal;
    return `<article class="meal-card${index === 0 ? " tonight" : ""}"><div class="meal-day"><span>${index === 0 ? "Tonight" : escapeHtml(dayLabel(day))}</span><span>${escapeHtml(dateText(day, { month: "short", day: "numeric" }))}</span></div><span class="meal-icon" aria-hidden="true">${escapeHtml(meal && meal.emoji || (index === 0 ? "🍽️" : "🥣"))}</span><h2>${meal ? escapeHtml(meal.title) : "Room for an idea"}</h2><p>${meal ? escapeHtml(meal.notes || "Something to look forward to.") : "Dinner isn't planned yet."}</p>${meal && meal.by ? `<p class="meal-by">Planned by ${escapeHtml(member(meal.by).name)}</p>` : ""}</article>`;
  }).join("");
  const ideas = list(state.meal_ideas).map((idea) => escapeHtml(typeof idea === "string" ? idea : idea.title)).join(" · ");
  return `${form}<div class="meals-grid">${days}</div>${ideas ? `<p class="ideas"><strong>Family favorites</strong><br>${ideas}</p>` : ""}`;
}
function groceryEmoji(aisle) {
  if (/produce|fruit|vegetable/i.test(aisle)) return "🥬";
  if (/dairy|milk|egg/i.test(aisle)) return "🥛";
  if (/bakery|bread/i.test(aisle)) return "🍞";
  if (/meat|seafood|protein/i.test(aisle)) return "🥩";
  if (/frozen/i.test(aisle)) return "🧊";
  if (/drink|beverage/i.test(aisle)) return "🧃";
  if (/house|clean/i.test(aisle)) return "🧽";
  if (/cart/i.test(aisle)) return "✓";
  return "🛒";
}
function renderGroceries() {
  const groups = new Map();
  list(state.groceries).forEach((item) => {
    const operation = pending.get(`grocery:${item.id}`);
    const checked = operation ? operation.value : Boolean(item.checked);
    const aisle = checked ? "In the cart" : item.aisle || item.category || "Other essentials";
    if (!groups.has(aisle)) groups.set(aisle, []);
    groups.get(aisle).push({ item, checked, busy: Boolean(operation) });
  });
  const aisles = Array.from(groups.keys()).sort((a, b) => a === b ? 0 : a === "In the cart" ? 1 : b === "In the cart" ? -1 : a.localeCompare(b));
  return `<div class="grocery-layout"><form class="add-form" data-form="grocery"><label class="sr-only" for="grocery-item">Add a grocery</label><input id="grocery-item" name="item" maxlength="200" placeholder="Add milk, apples, a little treat…" value="${escapeHtml(drafts.grocery)}" required autocomplete="off"><button class="primary" type="submit">+ Add item</button></form>${aisles.length ? `<div class="grocery-groups">${aisles.map((aisle) => `<section class="grocery-group"><h2>${groceryEmoji(aisle)} ${escapeHtml(aisle)}<small>${groups.get(aisle).length}</small></h2>${groups.get(aisle).map(({ item, checked, busy }) => `<button type="button" class="grocery-row${checked ? " done" : ""}" data-action="grocery" data-item="${escapeHtml(item.id)}" aria-pressed="${checked}"${busy ? ' disabled aria-busy="true"' : ""}><span class="check" aria-hidden="true">✓</span><span class="row-label"><span>${escapeHtml(item.emoji || "")} ${escapeHtml(item.name)}</span></span>${item.qty ? `<span class="quantity">× ${escapeHtml(item.qty)}</span>` : ""}</button>`).join("")}</section>`).join("")}</div>` : empty("🧺", "All stocked up?", "Add the first thing you need. Everyone's list stays in sync.")}</div>`;
}
function renderPhotos() {
  const collection = photos();
  if (!collection.length) return empty("☀", "Make this wall yours", "Ask PCAgent to “pick photos for the wall”. Your selected photos will appear here.");
  return `<div class="photo-grid">${collection.map((photo, index) => `<button type="button" class="photo-tile" data-action="slideshow" data-index="${index}" aria-label="View family photo ${index + 1}"><img src="${escapeHtml(photoUrl(photo))}" alt="${escapeHtml(photo.caption || photo.name || `Family memory ${index + 1}`)}" loading="lazy" decoding="async"></button>`).join("")}</div><p class="photo-help">${state.role === "kiosk" ? "Your photos take over after a few quiet minutes. " : ""}Tap a photo to start the slideshow. Touch anywhere to come back.</p>`;
}
function renderSettings() {
  const sun = sunWindow();
  const themeNote = themeMode === "light" ? "Always light, day and night." : themeMode === "dark" ? "Always dark, day and night."
    : `Following the sun: light from ${timeText(sun.sunrise)}, dark from ${timeText(sun.dusk)}.`;
  const themeRow = `<div class="settings-row"><span>Light and dark<small>${escapeHtml(themeNote)}</small></span><div class="theme-toggle" role="group" aria-label="Light and dark">${THEME_MODES.map((mode) => `<button type="button" data-action="theme" data-mode="${mode}" aria-pressed="${themeMode === mode}">${THEME_LABELS[mode]}</button>`).join("")}</div></div>`;
  const noteItems = list(state.notes).map((note) => note.text).filter(Boolean);
  const notesPanel = noteItems.length ? `<section class="panel" style="grid-column: 1 / -1"><h2>Notes</h2>${noteItems.map((text, index) => `<div class="settings-row" style="align-items:center"><div style="flex:1;min-width:0;margin-right:12px"><small style="display:block;margin-bottom:6px">Note ${index + 1}</small><input type="text" readonly value="${escapeHtml(text)}" onclick="this.select()" style="font-family:monospace;font-size:.9rem"></div><button type="button" data-action="copy-text" data-text="${escapeHtml(text)}">Copy</button></div>`).join("")}</section>` : "";
  return `<div class="settings-grid">${notesPanel}<section class="panel"><h2>On this screen</h2>${themeRow}<div class="settings-row"><span>Happy little sounds<small>A gentle chime for a finished chore.</small></span><button type="button" data-action="sound" aria-pressed="${soundEnabled}">${soundEnabled ? "Sound on" : "Sound off"}</button></div><div class="settings-row"><span>Fill the screen<small>Keep the family in focus.</small></span><button type="button" data-action="fullscreen">${document.fullscreenElement ? "Exit" : "Full screen"}</button></div><div class="settings-row"><span>Photo time<small>Crossfading memories, with a tap to wake.</small></span><button type="button" data-action="slideshow">Preview</button></div>${installPrompt ? '<div class="settings-row"><span>Keep us close<small>Add Family Wall to this device.</small></span><button type="button" data-action="install">Install</button></div>' : ""}</section><section class="panel" id="display-settings"></section><section class="panel"><h2>Our connection</h2><p class="settings-detail"><span>This screen</span><b>${state.role === "parent" ? "Parent phone" : "Kitchen wall"}</b></p><p class="settings-detail"><span>Last update</span><b>${escapeHtml(timeText(minutesOf(String(state.now).slice(11, 16))))}</b></p><p class="settings-detail"><span>Status</span><b>${online ? "Connected" : "Reconnecting"}</b></p><button type="button" data-action="refresh">Refresh the wall</button><p class="settings-note" style="margin-top:20px">Family plans, routines and photo selections are managed through PCAgent. Your sound, theme and display settings stay on this screen.</p></section></div>`;
}

// Fixed device IDs form the client allowlist; names and commands never come from DOM text.
const HOME_LIGHTS = [
  { id: "family_room_lights", name: "Family Room Lights" },
  { id: "twins_lights", name: "Twins Lights" },
  { id: "bedroom_lights", name: "Bedroom Lights" },
];
const HOME_SPEAKERS = [
  { id: "kids_speaker", name: "Twins speaker" },
  { id: "kid_display", name: "Riley Display" },
  { id: "family_room_display", name: "Family Room Display" },
  { id: "playroom_display", name: "Playroom Display" },
  { id: "bedroom_display", name: "Bedroom Display" },
];
const HOME_SCENES = [
  { id: "weekday_silence", name: "Morning quiet", detail: "Silence Twins speaker and Riley Display." },
  { id: "all_lights_off", name: "All Lights Off", detail: "Switch off the family room, twins and bedroom lights." },
  { id: "family_evening", name: "Family evening", detail: "Run the family evening scene." },
];
const homePending = new Map();
const homeAccepted = new Map();
let homeNotice = null;

// Home metadata is optional on older walls. Until reported, device state stays unknown.
function homeSnapshot(snapshot = state) {
  const home = snapshot && (snapshot.home_control || snapshot.home);
  return Array.isArray(home) ? { devices: home } : home && typeof home === "object" ? home : null;
}
function homeReported(id, snapshot = state) {
  const home = homeSnapshot(snapshot);
  const devices = home && home.devices;
  const reported = Array.isArray(devices)
    ? devices.find((device) => device && typeof device === "object" && (device.device_id === id || device.id === id))
    : devices && typeof devices === "object" && Object.prototype.hasOwnProperty.call(devices, id) ? devices[id] : null;
  return reported && typeof reported === "object" && !Array.isArray(reported) ? reported : null;
}
function homeDevice(id) {
  const device = homeReported(id) || {};
  const observed = device.state && typeof device.state === "object" && !Array.isArray(device.state) ? device.state : device;
  const playerState = ["PLAYING", "PAUSED", "BUFFERING", "IDLE"].includes(observed.player_state) ? observed.player_state : "UNKNOWN";
  const result = {
    power: observed.power === true || observed.power === "on" ? true : observed.power === false || observed.power === "off" ? false : null,
    brightness: homeBrightness(observed.brightness),
    status: observed.muted === true ? "Muted" : playerState === "PAUSED" ? "Paused"
      : ["PLAYING", "BUFFERING"].includes(playerState) ? "Playing" : "Quiet",
    available: device.available !== false && !["offline", "unavailable", "disabled"].includes(device.availability),
    playerState,
    title: homeMediaText(observed.title),
    artist: homeMediaText(observed.artist),
    appName: homeMediaText(observed.app_name),
  };
  if (homeAccepted.has(id)) Object.assign(result, homeAccepted.get(id));
  homePending.forEach((command) => {
    if (command.changes.has(id)) Object.assign(result, command.changes.get(id));
  });
  // Offline observations take precedence over a pending or recently accepted command.
  if (!result.available) result.status = "Offline";
  else if (HOME_LIGHTS.some((light) => light.id === id)) {
    result.status = result.power === null ? "Unknown" : result.power ? "On" : "Off";
  }
  return result;
}
function homeMediaText(value) {
  return typeof value === "string" ? Array.from(value.trim()).slice(0, 128).join("").trim() || null : null;
}
function homeBrightness(value) {
  if (typeof value !== "number" && typeof value !== "string") return null;
  if (typeof value === "string" && !/^(?:0|[1-9]\d?|100)$/.test(value)) return null;
  const numeric = Number(value);
  return Number.isInteger(numeric) && numeric >= 0 && numeric <= 100 ? numeric : null;
}
function homeBusy(id) {
  return !state || pairing || asleep || !online || homePending.has("scene") || homePending.has(id);
}
function homeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = String(text);
  return element;
}
function homeButton(id, action, label, disabled) {
  const button = homeElement("button", "home-control-button", label);
  button.type = "button";
  button.id = id;
  button.dataset.action = action;
  button.disabled = disabled;
  return button;
}

// Receiver observations stay separate from slider previews and command acknowledgements.
const SPEAKER_HUB_ROOMS = [
  { id: "theater_receiver", name: "Theater", peer: "family_room_receiver" },
  { id: "family_room_receiver", name: "Family Room", peer: "theater_receiver" },
  { id: "speaker_hub_theater", name: "Theater", peer: "speaker_hub_family_room" },
  { id: "speaker_hub_family_room", name: "Family Room", peer: "speaker_hub_theater" },
];
const SPEAKER_HUB_INPUTS = ["tv", "hdmi1", "hdmi2", "hdmi3", "hdmi4", "hdmi5", "hdmi6", "hdmi7", "spotify", "bluetooth", "net_radio", "audio", "phono"];
const SPEAKER_HUB_INPUT_LABELS = { tv: "TV", spotify: "Spotify", bluetooth: "Bluetooth", net_radio: "Internet Radio", audio: "Audio", phono: "Phono" };
const SPEAKER_HUB_PLAYBACK = ["previous", "play", "pause", "stop", "next"];
const speaker_hubCards = new Map();
let speaker_hubVisibilityListening = false;
let speaker_hubDashboard = null;

function speaker_hubObject(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
function speaker_hubText(value) { return typeof value === "string" ? Array.from(value).slice(0, 512).join("") : ""; }
function speaker_hubError(value) { return speaker_hubText(typeof value === "string" ? value : speaker_hubObject(value).message); }
function speaker_hubRoom(id) { return SPEAKER_HUB_ROOMS.find((room) => room.id === id); }
function speaker_hubSnapshot(device) {
  if (!device || typeof device !== "object" || Array.isArray(device) || device.enabled === false) return null;
  const rawState = speaker_hubObject(device.state);
  const record = Object.assign({}, device, rawState);
  delete record.state;
  const id = record.id || record.device_id;
  record.id = id;
  const rawPower = record.power !== undefined ? record.power : rawState.power;
  record.power = typeof rawPower === "boolean" ? (rawPower ? "on" : "standby") : rawPower;
  record.available = record.available !== undefined ? record.available :
    ![false, "offline", "unavailable", "disabled", "unsupported"].includes(record.availability);
  record.stale = Boolean(record.stale);
  record.revision = Number.isInteger(record.revision) ? record.revision : (Number.isInteger(rawState.revision) ? rawState.revision : 1);
  record.capabilities = Object.assign({}, speaker_hubObject(device.capabilities), speaker_hubObject(rawState.capabilities));
  record.link = Object.assign({}, speaker_hubObject(device.link), speaker_hubObject(rawState.link));
  if (record.kind !== "speaker_hub_audio_cast" || !speaker_hubRoom(record.id)) return null;
  return record;
}
function speaker_hubCapabilities(card) {
  const rawCaps = card.observed.capabilities;
  const caps = speaker_hubObject(rawCaps);
  const empty = rawCaps && typeof rawCaps === "object" && Object.keys(rawCaps).length === 0;
  const grid = speaker_hubObject(caps.volume);
  const vMin = caps.volume_min !== undefined ? caps.volume_min : (grid.min !== undefined ? grid.min : 0);
  const vMax = caps.volume_max !== undefined ? caps.volume_max : (grid.max !== undefined ? grid.max : (typeof card.observed.max_volume === "number" ? card.observed.max_volume : 161));
  const vStep = caps.volume_step !== undefined ? caps.volume_step : (grid.step !== undefined ? grid.step : 1);
  return {
    functions: list(caps.functions || caps.zone_functions || (empty ? [] : ["power", "volume", "mute", "input", "playback"])),
    inputs: list(caps.inputs || caps.input_ids || caps.input_list || (empty ? [] : (card.observed.inputs || SPEAKER_HUB_INPUTS))),
    distributable_inputs: list(caps.distributable_inputs || ["tv", "hdmi1", "hdmi2", "hdmi3", "spotify", "net_radio", "audio", "phono", "bluetooth"]),
    volume: { min: vMin, max: vMax, step: vStep },
    volume_min: vMin,
    volume_max: vMax,
    volume_step: vStep,
    netusb: caps.netusb !== undefined ? caps.netusb : !empty,
    netusb_available: caps.netusb_available !== undefined ? caps.netusb_available : !empty,
    distribution: caps.distribution || { server_zone: ["main"], client_zone: ["main"] },
  };
}
function speaker_hubFunctions(card) {
  const caps = speaker_hubCapabilities(card);
  return list(caps.functions || caps.zone_functions);
}
function speaker_hubInputs(card) {
  const caps = speaker_hubCapabilities(card);
  const inputs = list(caps.inputs || caps.input_ids || caps.input_list);
  return SPEAKER_HUB_INPUTS.filter((id) => inputs.some((input) => input === id ||
    (speaker_hubObject(input).id === id && input.enabled !== false)));
}
function speaker_hubVolumeGrid(card) {
  const caps = speaker_hubCapabilities(card), grid = speaker_hubObject(caps.volume);
  const min = caps.volume_min === undefined ? grid.min : caps.volume_min;
  let max = caps.volume_max === undefined ? grid.max : caps.volume_max;
  const step = caps.volume_step === undefined ? grid.step : caps.volume_step;
  if (![min, max, step].every((value) => typeof value === "number" && Number.isFinite(value)) || step <= 0 || max < min) return null;
  // The serialized capability maximum is observation-specific. Honor an explicit cap too.
  if (Object.prototype.hasOwnProperty.call(card.observed, "max_volume")) {
    const cap = card.observed.max_volume;
    if (typeof cap !== "number" || !Number.isFinite(cap)) return null;
    max = Math.min(max, cap);
  }
  return max >= min ? { min, max, step } : null;
}
function speaker_hubVolumeValid(card, value) {
  const grid = speaker_hubVolumeGrid(card);
  if (!grid || !Number.isInteger(value) || value < grid.min || value > grid.max) return false;
  const steps = (value - grid.min) / grid.step;
  return Math.abs(steps - Math.round(steps)) < 1e-8;
}
function speaker_hubPendingLink(card) {
  const peer = speaker_hubCards.get(card.room.peer);
  return Boolean(card.linkPending || (peer && peer.linkPending) ||
    [card, peer].some((item) => item && ["building", "deleting"].includes(speaker_hubObject(item.observed.link).phase)));
}
function speaker_hubGrouped(card) {
  const link = speaker_hubObject(card.observed.link);
  return Boolean((typeof link.group_id === "string" && link.group_id.replace(/0/g, "").length) ||
    ["server", "client", "unknown"].includes(link.role) || ["working", "partial", "unknown"].includes(link.phase));
}
function speaker_hubDistributable(card, input) {
  return list(speaker_hubCapabilities(card).distributable_inputs).includes(input);
}
function speaker_hubGate(card, action) {
  const seen = card.observed, link = speaker_hubObject(seen.link), caps = speaker_hubCapabilities(card);
  const busy = Boolean(card.command || card.reconciling);
  if (card.disposed || homeBusy(card.room.id) || seen.available !== true || seen.stale !== false ||
      !Number.isInteger(seen.revision) || seen.revision < 0 || speaker_hubPendingLink(card)) return false;
  if (busy && !(action === "volume" && card.activeAction === "volume" && !card.reconciling)) return false;
  if (action === "power") return speaker_hubFunctions(card).includes("power") && ["on", "standby"].includes(seen.power) &&
    (seen.power === "standby" || !speaker_hubGrouped(card));
  if (seen.power !== "on") return false;
  if (action === "volume") return speaker_hubFunctions(card).includes("volume") && Boolean(speaker_hubVolumeGrid(card));
  if (action === "mute") return speaker_hubFunctions(card).includes("mute") && typeof seen.mute === "boolean";
  if (action === "input") return speaker_hubFunctions(card).includes("input") && link.role !== "client" && link.role !== "unknown" &&
    !["partial", "unknown"].includes(link.phase) && speaker_hubInputs(card).length > 0;
  if (action === "playback") return link.role !== "client" && link.role !== "unknown" && !["partial", "unknown"].includes(link.phase) &&
    (caps.netusb === true || caps.netusb_available === true) && ["spotify", "net_radio"].includes(seen.input) && speaker_hubInputs(card).includes(seen.input);
  if (action === "unlink") return link.owned === true && speaker_hubGrouped(card) && link.phase !== "unknown";
  if (action === "link") {
    const peer = speaker_hubCards.get(card.room.peer), dist = speaker_hubObject(caps.distribution);
    const peerDist = peer && speaker_hubObject(speaker_hubCapabilities(peer).distribution);
    return Boolean(peer && !peer.command && !peer.reconciling && !homeBusy(peer.room.id) &&
      peer.observed.available === true && peer.observed.stale === false && peer.observed.power === "on" &&
      Number.isInteger(peer.observed.revision) && peer.observed.revision >= 0 &&
      !speaker_hubGrouped(card) && !speaker_hubGrouped(peer) && speaker_hubDistributable(card, seen.input) &&
      list(dist.server_zone || dist.server_zones).includes("main") && list(peerDist.client_zone || peerDist.client_zones).includes("main"));
  }
  return false;
}
function speaker_hubCommandValid(card, command) {
  if (!command || typeof command !== "object" || Array.isArray(command)) return false;
  const fields = { power: "power", volume: "volume", mute: "mute", input: "input", playback: "playback", link: "peer_id" };
  const field = Object.prototype.hasOwnProperty.call(fields, command.action) ? fields[command.action] : null;
  const keys = field ? (command.action === "link" ? ["action", "peer_id", "enabled"] : ["action", field]) : [];
  if (!field || Object.keys(command).length !== keys.length || !keys.every((key) => Object.prototype.hasOwnProperty.call(command, key))) return false;
  const action = command.action === "link" && command.enabled === false ? "unlink" : command.action;
  if (!speaker_hubGate(card, action)) return false;
  if (command.action === "power" || command.action === "mute") return typeof command[field] === "boolean" &&
    !(command.action === "power" && command.power === false && speaker_hubGrouped(card));
  if (command.action === "volume") return speaker_hubVolumeValid(card, command.volume);
  if (command.action === "input") return speaker_hubInputs(card).includes(command.input) &&
    (speaker_hubObject(card.observed.link).role !== "server" || speaker_hubDistributable(card, command.input));
  if (command.action === "playback") return SPEAKER_HUB_PLAYBACK.includes(command.playback);
  return typeof command.enabled === "boolean" && command.peer_id === card.room.peer;
}
function speaker_hubListen(card, node, name, fn) {
  node.addEventListener(name, fn);
  card.listeners.push(() => node.removeEventListener(name, fn));
}
function speaker_hubUpdate(card) {
  if (card.disposed) return;
  const seen = card.observed, nodes = card.nodes, link = speaker_hubObject(seen.link);
  const name = speaker_hubText(seen.name) || card.room.name;
  nodes.heading.textContent = name;
  nodes.model.textContent = speaker_hubText(seen.model);
  const badges = [seen.available === true ? "Available" : "Unavailable", seen.power === "standby" ? "Standby" : seen.power === "on" ? "On" : "Power unknown"];
  if (seen.stale === true) badges.push("Stale observation");
  if (["building", "deleting", "partial", "unknown"].includes(link.phase)) badges.push(`Audio_cast ${link.phase}`);
  if (link.role === "client") badges.push(`${speaker_hubRoom(link.peer_id) ? speaker_hubRoom(link.peer_id).name : "Other room"} is the source`);
  else if (link.role === "server") badges.push(`${name} is the source`);
  if (speaker_hubGrouped(card) && link.owned !== true) badges.push("External Audio_cast group");
  badges.push(card.notice || card.readNotice || speaker_hubError(seen.error) || speaker_hubError(link.error));
  nodes.status.textContent = badges.filter(Boolean).join(" · ");
  const track = speaker_hubObject(seen.track);
  nodes.track.textContent = [track.title, track.artist, track.album].map(speaker_hubText).filter(Boolean).join(" · ");
  nodes.currentInput.textContent = `Input: ${speaker_hubText(seen.input_label) || speaker_hubText(seen.input) || "Unknown"}`;
  nodes.power.textContent = seen.power === "on" ? "Standby" : "On";
  nodes.power.disabled = !speaker_hubGate(card, "power");
  nodes.power.setAttribute("aria-label", `${name} power`);
  nodes.power.setAttribute("aria-pressed", String(seen.power === "on"));
  nodes.mute.textContent = seen.mute === true ? "Unmute" : "Mute";
  nodes.mute.disabled = !speaker_hubGate(card, "mute");
  nodes.mute.setAttribute("aria-label", `${name} mute`);
  nodes.mute.setAttribute("aria-pressed", String(seen.mute === true));
  const grid = speaker_hubVolumeGrid(card);
  nodes.volume.min = String(grid ? grid.min : 0);
  nodes.volume.max = String(grid ? grid.max : 0);
  nodes.volume.step = String(grid ? grid.step : 1);
  nodes.volume.disabled = !speaker_hubGate(card, "volume");
  nodes.volume.setAttribute("aria-label", `${name} volume`);
  // Updating bounds may clamp a native range; restore the preview only while it is valid.
  const preview = card.preview !== null;
  if (!preview) nodes.volume.value = Number.isInteger(seen.volume) ? String(seen.volume) : nodes.volume.min;
  const raw = preview ? card.preview : seen.volume;
  const db = typeof seen.volume_db === "number" && Number.isFinite(seen.volume_db) ? `${seen.volume_db} dB · ` : "";
  nodes.output.textContent = preview ? `Preview: raw ${raw} · observed ${db}raw ${Number.isInteger(seen.volume) ? seen.volume : "unknown"}`
    : `${db}raw ${Number.isInteger(raw) ? raw : "unknown"}`;
  nodes.volume.setAttribute("aria-valuetext", nodes.output.textContent);
  const inputs = speaker_hubInputs(card).filter((id) => link.role !== "server" || speaker_hubDistributable(card, id));
  const inputKey = inputs.join("|");
  if (card.inputKey !== inputKey) {
    nodes.input.textContent = "";
    inputs.forEach((id) => {
      const option = homeElement("option", "", SPEAKER_HUB_INPUT_LABELS[id] || `HDMI ${id.slice(4)}`);
      option.value = id; nodes.input.appendChild(option);
    });
    card.inputKey = inputKey;
  }
  nodes.input.value = inputs.includes(seen.input) ? seen.input : "";
  nodes.input.disabled = !speaker_hubGate(card, "input");
  SPEAKER_HUB_PLAYBACK.forEach((action) => { nodes[action].disabled = !speaker_hubGate(card, "playback"); });
  nodes.sync.disabled = !speaker_hubGate(card, "link");
  nodes.unlink.disabled = !speaker_hubGate(card, "unlink");
  nodes.refresh.disabled = Boolean(card.refresh || card.command || card.disposed);
  card.element.setAttribute("aria-busy", String(Boolean(card.command || card.refresh || card.reconciling || speaker_hubPendingLink(card))));
}
function speaker_hubUpdateAll() { speaker_hubCards.forEach(speaker_hubUpdate); }
function speaker_hubObserve(card, snapshot, newerOnly = false) {
  const seen = speaker_hubSnapshot(snapshot);
  if (!seen || seen.id !== card.room.id) return false;
  const revision = seen.revision, previous = card.observed.revision;
  if (!Number.isInteger(revision) || revision < 0 || (Number.isInteger(previous) && revision < previous)) return false;
  if (newerOnly && revision === previous && !["power", "volume", "mute", "input", "playback", "available", "stale"]
    .some((field) => seen[field] !== card.observed[field])) return false;
  card.observed = seen;
  return true;
}
function renderSpeaker_hubCard(device) {
  const snapshot = speaker_hubSnapshot(device);
  if (!snapshot) return null;
  let card = speaker_hubCards.get(snapshot.id);
  if (card) {
    // A failed direct read stays stale until a fresh read or a newer dashboard observation.
    if (!card.readNotice || snapshot.revision > card.observed.revision) speaker_hubObserve(card, snapshot, true);
    speaker_hubUpdateAll(); return card.element;
  }
  card = { room: speaker_hubRoom(snapshot.id), observed: snapshot, nodes: {}, listeners: [], controllers: new Set(),
    generation: 0, disposed: false, command: null, refresh: null, activeAction: null, preview: null,
    queued: null, ready: false, editing: false, debounce: null, poll: null, pollDelay: null, reconciling: false, notice: "" };
  speaker_hubCards.set(snapshot.id, card);
  const element = homeElement("article", "panel home-control-card speaker_hub-card");
  card.element = element; element.id = `speaker_hub-${snapshot.id}`;
  element.style.cssText = "min-width:0;overflow-wrap:anywhere";
  const add = (part, tag, text, parent = element) => {
    const node = homeElement(tag, tag === "button" ? "home-control-button" : "home-device-status", text);
    node.id = `speaker_hub-${snapshot.id}-${part}`; card.nodes[part] = node; parent.appendChild(node); return node;
  };
  add("heading", "h3"); element.setAttribute("aria-labelledby", card.nodes.heading.id);
  add("model", "p"); const status = add("status", "p"); status.className += " home-status-badge";
  status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); status.setAttribute("aria-atomic", "true");
  add("track", "p"); add("currentInput", "p");
  const row = homeElement("div", "home-speaker-actions"); row.style.cssText = "display:flex;flex-wrap:wrap;gap:8px";
  element.appendChild(row);
  const action = (part, text, fn, parent = row) => {
    const button = add(part, "button", text, parent); button.type = "button";
    button.style.cssText = "flex:1 1 auto;min-height:44px";
    // Native focus rings are also visible when the surrounding theme changes.
    speaker_hubListen(card, button, "focus", () => { button.style.outline = "2px solid currentColor"; button.style.outlineOffset = "2px"; });
    speaker_hubListen(card, button, "blur", () => { button.style.outline = ""; });
    speaker_hubListen(card, button, "click", () => button.disabled ? undefined : fn());
    return button;
  };
  action("power", "On", () => sendSpeaker_hubCommand(snapshot.id, { action: "power", power: card.observed.power !== "on" }));
  action("mute", "Mute", () => sendSpeaker_hubCommand(snapshot.id, { action: "mute", mute: card.observed.mute !== true }));
  action("refresh", "Refresh", () => refreshSpeaker_hubCard(snapshot.id));
  const label = homeElement("label", "home-brightness-label", "Volume"); label.setAttribute("for", `speaker_hub-${snapshot.id}-volume`); element.appendChild(label);
  const output = add("output", "output"); output.setAttribute("for", `speaker_hub-${snapshot.id}-volume`);
  const volume = add("volume", "input"); volume.type = "range"; volume.className = "home-brightness";
  volume.setAttribute("aria-describedby", output.id);
  speaker_hubListen(card, volume, "input", () => speaker_hubPreviewVolume(card));
  ["change", "pointerup", "keyup", "blur"].forEach((event) => speaker_hubListen(card, volume, event, () => speaker_hubFinishVolume(card)));
  const inputLabel = homeElement("label", "home-brightness-label", "Input"); inputLabel.setAttribute("for", `speaker_hub-${snapshot.id}-input`); element.appendChild(inputLabel);
  const input = add("input", "select"); input.style.cssText = "max-width:100%;min-height:44px";
  input.setAttribute("aria-label", `${speaker_hubText(snapshot.name) || card.room.name} input`);
  speaker_hubListen(card, input, "change", () => sendSpeaker_hubCommand(snapshot.id, { action: "input", input: input.value }));
  const playback = homeElement("div", "home-speaker-actions"); playback.style.cssText = "display:flex;flex-wrap:wrap;gap:8px"; element.appendChild(playback);
  SPEAKER_HUB_PLAYBACK.forEach((mode) => action(mode, mode[0].toUpperCase() + mode.slice(1),
    () => sendSpeaker_hubCommand(snapshot.id, { action: "playback", playback: mode }), playback));
  const linking = homeElement("div", "home-speaker-actions"); linking.style.cssText = "display:flex;flex-wrap:wrap;gap:8px"; element.appendChild(linking);
  action("sync", `Sync ${speaker_hubRoom(card.room.peer).name} to ${card.room.name}`,
    () => sendSpeaker_hubCommand(snapshot.id, { action: "link", peer_id: card.room.peer, enabled: true }), linking);
  action("unlink", "Unlink", () => sendSpeaker_hubCommand(snapshot.id, { action: "link", peer_id: card.room.peer, enabled: false }), linking);
  if (!speaker_hubVisibilityListening) { document.addEventListener("visibilitychange", speaker_hubVisibility); speaker_hubVisibilityListening = true; }
  speaker_hubUpdateAll(); speaker_hubSchedulePolls();
  return element;
}

async function speaker_hubRequest(card, suffix, body, requestId = card.room.id) {
  const controller = new AbortController();
  let timeout, cancel;
  const stopped = new Promise((resolve, reject) => {
    cancel = (message, uncertain) => { const error = new Error(message); error.uncertain = uncertain; reject(error); controller.abort(); };
    timeout = setTimeout(() => cancel(body ? "Outcome uncertain: receiver may have changed. Refreshing." : "Receiver refresh timed out.", Boolean(body)), 2500);
  });
  const owned = { controller, cancel }; card.controllers.add(owned);
  try {
    return await Promise.race([stopped, (async () => {
      const response = await fetch(`/api/home/speaker_hub/${requestId}${suffix}`, {
        method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : {},
        body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
      });
      const data = speaker_hubObject(await response.json());
      const result = data.result && typeof data.result === "object" ? speaker_hubObject(data.result) : data;
      if (!response.ok || data.ok === false || result.ok !== true) {
        const error = new Error(speaker_hubError(result.error) || speaker_hubError(data.error) || `Receiver request failed (${response.status}).`);
        error.result = result; error.uncertain = speaker_hubObject(result.error).uncertain === true;
        throw error;
      }
      return result;
    })()]);
  } catch (error) {
    if (body && !error.result && !error.uncertain && !card.disposed) {
      error.uncertain = true; error.message = "Outcome uncertain: receiver may have changed. Refreshing.";
    }
    throw error;
  } finally { clearTimeout(timeout); card.controllers.delete(owned); }
}
function speaker_hubApplyResult(card, result, generation, peerGeneration) {
  if (card.disposed || speaker_hubCards.get(card.room.id) !== card || card.generation !== generation) return false;
  const apply = (target, value, metadata = {}) => {
    if (!value || typeof value !== "object" || Array.isArray(value) || !Object.keys(value).length) return false;
    const previous = target.observed;
    const snapshot = Object.assign({ name: previous.name, model: previous.model, capabilities: previous.capabilities,
      revision: previous.revision, availability: previous.availability }, metadata, value);
    snapshot.id = snapshot.id || snapshot.device_id || target.room.id;
    snapshot.kind = "speaker_hub_audio_cast";
    if (!Number.isInteger(snapshot.revision)) snapshot.revision = previous.revision;
    snapshot.link = Object.assign({}, speaker_hubObject(previous.link), speaker_hubObject(metadata.link), speaker_hubObject(value.link));
    return speaker_hubObserve(target, snapshot);
  };
  const state = result.state || result.observed_state ||
    (["power", "volume", "mute", "input", "playback"].some((field) => Object.prototype.hasOwnProperty.call(result, field)) ? result : null);
  const applied = apply(card, state, result);
  if (peerGeneration !== undefined) {
    const peer = speaker_hubCards.get(card.room.peer);
    if (peer && peer.generation === peerGeneration) apply(peer, result.peer_state || result.peer_observed_state, {
      revision: result.peer_revision === undefined ? result.revision : result.peer_revision,
      availability: result.peer_availability === undefined ? result.availability : result.peer_availability,
      link: result.peer_link,
    });
  }
  return applied;
}
async function refreshSpeaker_hubCard(deviceId) {
  const card = speaker_hubCards.get(deviceId);
  if (!card || card.disposed) return false;
  if (card.refresh) return card.refresh;
  if (card.command) return false;
  const generation = card.generation;
  const task = (async () => {
    try {
      const result = await speaker_hubRequest(card, "");
      const applied = speaker_hubApplyResult(card, result, generation);
      if (applied) { card.reconciling = false; card.readNotice = ""; }
      return applied;
    } catch (error) {
      if (!card.disposed && generation === card.generation) {
        const applied = error.result && speaker_hubApplyResult(card, error.result, generation);
        if (!applied) card.observed = Object.assign({}, card.observed, { available: false, stale: true });
        card.readNotice = speaker_hubError(error) || "Receiver refresh failed.";
      }
      return false;
    } finally {
      if (!card.disposed) { card.refresh = null; speaker_hubUpdateAll(); speaker_hubSchedulePolls(); speaker_hubDrainVolume(card); }
    }
  })();
  card.refresh = task; speaker_hubUpdate(card);
  return task;
}
async function sendSpeaker_hubCommand(deviceId, command) {
  const card = speaker_hubCards.get(deviceId);
  if (!card || !speaker_hubCommandValid(card, command) || card.command || card.reconciling) return false;
  // An older read may complete, but cannot replace this command's readback.
  const generation = ++card.generation;
  const peer = command.action === "link" ? speaker_hubCards.get(card.room.peer) : null;
  if (peer) { ++peer.generation; peer.linkPending = true; }
  const peerGeneration = peer ? peer.generation : undefined;
  card.linkPending = command.action === "link";
  card.activeAction = command.action; card.notice = "Saving…";
  const listenerUnlink = command.action === "link" && command.enabled === false && speaker_hubObject(card.observed.link).role === "client";
  let failed = false;
  const task = (async () => {
    try {
      const payload = listenerUnlink ? { action: "link", peer_id: card.room.id, enabled: false } : command;
      const result = await speaker_hubRequest(card, command.action === "link" ? "/link" : "/command", payload,
        listenerUnlink ? card.room.peer : card.room.id);
      // Link envelopes follow source/listener order, even when Unlink was tapped on the listener.
      const observed = listenerUnlink ? Object.assign({}, result, { state: result.peer_state, peer_state: result.state }) : result;
      const applied = speaker_hubApplyResult(card, observed, generation, peerGeneration);
      if (!card.disposed && card.generation === generation) {
        if (applied) card.readNotice = "";
        card.notice = speaker_hubError(result.error) || (result.accepted === true && result.verified !== true ? "Accepted; waiting for observation." : "");
        if (command.action === "volume" && card.queued === null && !card.editing && applied) card.preview = null;
      }
      return true;
    } catch (error) {
      failed = true;
      if (!card.disposed && card.generation === generation) {
        if (error.result) {
          const result = listenerUnlink ? Object.assign({}, error.result, { state: error.result.peer_state, peer_state: error.result.state }) : error.result;
          speaker_hubApplyResult(card, result, generation, peerGeneration);
        }
        card.notice = speaker_hubError(error) || "Receiver action failed.";
        card.preview = null; card.editing = false;
        card.reconciling = command.action === "volume" || error.uncertain === true;
        if (peer && error.uncertain === true && !peer.disposed) peer.reconciling = true;
      }
      return false;
    } finally {
      if (!card.disposed) {
        card.command = null; card.activeAction = null; card.linkPending = false;
        if (peer && !peer.disposed) peer.linkPending = false;
        speaker_hubUpdateAll(); speaker_hubSchedulePolls();
        if (failed && card.reconciling) {
          // Wait for a pre-command refresh to retire, then start a new read. Never replay this command.
          if (card.refresh) await card.refresh;
          if (!card.disposed && speaker_hubCards.get(deviceId) === card) {
            await Promise.all([refreshSpeaker_hubCard(deviceId), peer && !peer.disposed ? refreshSpeaker_hubCard(peer.room.id) : false]);
          }
        }
        speaker_hubDrainVolume(card);
      }
    }
  })();
  card.command = task; speaker_hubUpdateAll();
  return task;
}
function speaker_hubPreviewVolume(card) {
  if (!speaker_hubGate(card, "volume")) return;
  const value = Number(card.nodes.volume.value);
  if (!speaker_hubVolumeValid(card, value)) return;
  card.preview = value; card.queued = value; card.editing = true; card.ready = false;
  clearTimeout(card.debounce);
  card.debounce = setTimeout(() => { card.debounce = null; card.ready = true; speaker_hubDrainVolume(card); }, 150);
  speaker_hubUpdate(card);
}
function speaker_hubFinishVolume(card) {
  if (card.disposed) return;
  card.editing = false; card.ready = true;
  clearTimeout(card.debounce); card.debounce = null;
  if (card.queued === null && !card.command) { card.preview = null; speaker_hubUpdate(card); }
  speaker_hubDrainVolume(card);
}
function speaker_hubDrainVolume(card) {
  if (card.disposed || card.command || card.reconciling || !card.ready || card.queued === null) return;
  const value = card.queued; card.queued = null; card.ready = false;
  if (!speaker_hubGate(card, "volume") || !speaker_hubVolumeValid(card, value)) {
    card.preview = null; card.editing = false; speaker_hubUpdate(card); return;
  }
  sendSpeaker_hubCommand(card.room.id, { action: "volume", volume: value });
}
function speaker_hubSchedulePolls() {
  speaker_hubCards.forEach((card) => {
    const delay = speaker_hubPendingLink(card) ? 2000 : 5000;
    if (document.hidden || card.disposed || card.command || card.refresh) {
      clearTimeout(card.poll); card.poll = null; card.pollDelay = null; return;
    }
    if (card.poll !== null && card.pollDelay === delay) return;
    clearTimeout(card.poll); card.pollDelay = delay;
    card.poll = setTimeout(() => { card.poll = null; refreshSpeaker_hubCard(card.room.id); }, delay);
  });
}
function speaker_hubVisibility() {
  speaker_hubSchedulePolls();
  if (!document.hidden) speaker_hubCards.forEach((card) => { refreshSpeaker_hubCard(card.room.id); });
}
function disposeSpeaker_hubCard(deviceId) {
  const card = speaker_hubCards.get(deviceId);
  if (!card) return;
  card.disposed = true; ++card.generation;
  clearTimeout(card.debounce); clearTimeout(card.poll);
  card.queued = null; card.preview = null;
  card.listeners.forEach((remove) => remove()); card.listeners = [];
  card.controllers.forEach((request) => request.cancel("Receiver card closed.", false));
  const peer = speaker_hubCards.get(card.room.peer);
  if (card.linkPending && peer) peer.linkPending = false;
  if (card.element.parentNode) card.element.parentNode.removeChild(card.element);
  speaker_hubCards.delete(deviceId);
  if (speaker_hubCards.size === 0 && speaker_hubVisibilityListening) {
    document.removeEventListener("visibilitychange", speaker_hubVisibility); speaker_hubVisibilityListening = false;
  }
  if (speaker_hubCards.size === 0) speaker_hubDashboard = null;
}
function disposeSpeaker_hubCards() {
  Array.from(speaker_hubCards.keys()).forEach(disposeSpeaker_hubCard);
  speaker_hubDashboard = null;
}

function renderControls() {
  let root = homeElement("div", "home-controls");
  const notice = homeElement("p", "home-controls-notice", homeNotice ? homeNotice.text
    : !online ? "Reconnect to the wall to use home controls." : "Choose a scene or adjust a device.");
  notice.setAttribute("role", homeNotice && homeNotice.error ? "alert" : "status");
  root.appendChild(notice);

  const scenes = homeElement("section", "home-controls-section");
  scenes.setAttribute("aria-labelledby", "home-scenes-title");
  const sceneTitle = homeElement("h2", "", "Quick Action Scenes");
  sceneTitle.id = "home-scenes-title";
  scenes.appendChild(sceneTitle);
  const sceneGrid = homeElement("div", "home-controls-grid");
  HOME_SCENES.forEach((scene) => {
    const running = homePending.get("scene");
    const button = homeButton(`home-scene-${scene.id}`, "home-scene", null, homeBusy("scene") || homePending.size > 0);
    button.className += " home-scene";
    button.dataset.scene = scene.id;
    button.setAttribute("aria-busy", String(Boolean(running && running.args.scene_id === scene.id)));
    button.appendChild(homeElement("strong", "", scene.name));
    button.appendChild(homeElement("span", "", running && running.args.scene_id === scene.id ? "Running…" : scene.detail));
    sceneGrid.appendChild(button);
  });
  scenes.appendChild(sceneGrid);
  root.appendChild(scenes);

  const lights = homeElement("section", "home-controls-section");
  lights.setAttribute("aria-labelledby", "home-lights-title");
  const lightTitle = homeElement("h2", "", "Lights");
  lightTitle.id = "home-lights-title";
  lights.appendChild(lightTitle);
  const lightGrid = homeElement("div", "home-controls-grid");
  HOME_LIGHTS.forEach((light) => {
    const device = homeDevice(light.id);
    const disabled = homeBusy(light.id) || !device.available;
    const card = homeElement("article", "panel home-control-card");
    card.setAttribute("aria-busy", String(homePending.has(light.id) || homePending.has("scene")));
    const heading = homeElement("h3", "", light.name);
    heading.id = `home-name-${light.id}`;
    card.setAttribute("aria-labelledby", heading.id);
    card.appendChild(heading);
    card.appendChild(homeElement("p", "home-device-status", device.status));
    const toggle = homeButton(`home-power-${light.id}`, "home-light-toggle", device.power ? "Turn off" : "Turn on", disabled);
    toggle.dataset.device = light.id;
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(device.power === true));
    toggle.setAttribute("aria-label", `${light.name} power`);
    card.appendChild(toggle);
    const row = homeElement("div", "home-brightness-label");
    const label = homeElement("label", "", "Brightness");
    label.setAttribute("for", `home-brightness-${light.id}`);
    row.appendChild(label);
    const value = homeElement("output", "", device.brightness === null ? "Not reported" : `${device.brightness}%`);
    value.id = `home-value-${light.id}`;
    value.setAttribute("for", `home-brightness-${light.id}`);
    row.appendChild(value);
    card.appendChild(row);
    const slider = homeElement("input", "home-brightness");
    slider.type = "range";
    slider.id = `home-brightness-${light.id}`;
    slider.min = "0";
    slider.max = "100";
    slider.step = "1";
    slider.value = String(device.brightness === null ? 100 : device.brightness);
    slider.disabled = disabled;
    slider.dataset.homeBrightness = "";
    slider.dataset.device = light.id;
    slider.setAttribute("aria-label", `${light.name} brightness`);
    slider.setAttribute("aria-describedby", value.id);
    card.appendChild(slider);
    lightGrid.appendChild(card);
  });
  lights.appendChild(lightGrid);
  root.appendChild(lights);

  const speakers = homeElement("section", "home-controls-section");
  speakers.setAttribute("aria-labelledby", "home-speakers-title");
  const speakerTitle = homeElement("h2", "", "Speakers");
  speakerTitle.id = "home-speakers-title";
  speakers.appendChild(speakerTitle);
  const speakerGrid = homeElement("div", "home-controls-grid");
  HOME_SPEAKERS.forEach((speaker) => {
    const device = homeDevice(speaker.id);
    const card = homeElement("article", "panel home-control-card");
    const heading = homeElement("h3", "", speaker.name);
    heading.id = `home-name-${speaker.id}`;
    card.setAttribute("aria-labelledby", heading.id);
    card.setAttribute("aria-busy", String(homePending.has(speaker.id) || homePending.has("scene")));
    card.appendChild(heading);
    card.appendChild(homeElement("p", "home-device-status home-status-badge", device.status));
    const mediaText = [device.title, device.artist, device.appName].filter(Boolean).join(" • ");
    if (device.available && ["PLAYING", "PAUSED", "BUFFERING"].includes(device.playerState) && mediaText) {
      const media = homeElement("p", "home-device-status home-media-info", mediaText);
      media.id = `home-media-${speaker.id}`;
      media.setAttribute("role", "status");
      media.setAttribute("aria-live", "polite");
      media.setAttribute("aria-atomic", "true");
      media.style.cssText = "min-width:0;max-width:100%;overflow-wrap:anywhere";
      card.setAttribute("aria-describedby", media.id);
      card.appendChild(media);
    }
    const silence = homeButton(`home-silence-${speaker.id}`, "home-speaker-silence", "Silence", homeBusy(speaker.id) || !device.available);
    silence.dataset.device = speaker.id;
    silence.setAttribute("aria-label", `Silence ${speaker.name}`);
    card.appendChild(silence);
    const modes = homeElement("div", "home-speaker-actions");
    modes.style.cssText = "display:flex;flex-wrap:wrap;gap:8px";
    [["play", "Play"], ["stop", "Stop"], ["pause", "Pause"], ["mute", "Mute"]].forEach(([mode, label]) => {
      const button = homeButton(`home-${mode}-${speaker.id}`, "home-speaker-silence", label, homeBusy(speaker.id) || !device.available);
      button.className += " secondary";
      button.style.flex = "1";
      button.dataset.device = speaker.id;
      button.dataset.mode = mode;
      button.setAttribute("aria-label", `${label} ${speaker.name}`);
      modes.appendChild(button);
    });
    card.appendChild(modes);
    speakerGrid.appendChild(card);
  });
  speakers.appendChild(speakerGrid);
  root.appendChild(speakers);

  const receivers = SPEAKER_HUB_ROOMS.map((room) => speaker_hubSnapshot(homeReported(room.id))).filter(Boolean);
  speaker_hubCards.forEach((card, id) => { if (!receivers.some((device) => device.id === id)) disposeSpeaker_hubCard(id); });
  if (receivers.length) {
    if (!speaker_hubDashboard) {
      const section = homeElement("section", "home-controls-section");
      const heading = homeElement("h2", "", "Receivers"); heading.id = "home-receivers-title";
      section.setAttribute("aria-labelledby", heading.id); section.appendChild(heading);
      const grid = homeElement("div", "home-controls-grid");
      section.appendChild(grid);
      speaker_hubDashboard = { root, section, grid };
      root.appendChild(section);
    } else {
      // Refresh the legacy sections without ever detaching an editing receiver control.
      const children = Array.from(root.children);
      root = speaker_hubDashboard.root;
      Array.from(root.children).forEach((child) => { if (child !== speaker_hubDashboard.section) root.removeChild(child); });
      children.forEach((child) => root.insertBefore(child, speaker_hubDashboard.section));
    }
    const grid = speaker_hubDashboard.grid;
    Array.from(grid.children).forEach((child) => {
      if (!receivers.some((device) => child.id === `speaker_hub-${device.id}`)) grid.removeChild(child);
    });
    receivers.forEach((device) => {
      const card = renderSpeaker_hubCard(device);
      if (card.parentNode !== grid) grid.appendChild(card);
    });
    speaker_hubUpdateAll(); speaker_hubSchedulePolls();
  } else speaker_hubDashboard = null;
  return root;
}
async function runHomeAction(key, op, args, changes, name) {
  if (homeBusy(key) || (key === "scene" && homePending.size > 0)) return;
  homePending.set(key, { args, changes });
  homeNotice = { text: `${name}: saving…` };
  render();
  let accepted = false;
  try {
    const response = await act(op, args);
    const result = response.result && typeof response.result === "object" ? response.result : response;
    if (result.ok === false || result.success === false) throw new Error(result.error || "The device could not complete that action.");
    accepted = true;
    changes.forEach((patch, id) => homeAccepted.set(id, Object.assign({}, homeAccepted.get(id), patch.status === "Silencing…" ? { status: "Silenced" } : patch)));
    await refresh(true);
    homeNotice = { text: `${name}: saved.` };
  } catch (error) {
    homeNotice = accepted ? { text: "Saved. Waiting for the wall to reconnect." }
      : { text: error && error.message ? error.message : "The action failed. Try again.", error: true };
    if (!accepted) toast(homeNotice.text);
  } finally {
    homePending.delete(key);
    render();
  }
}
function setHomeLight(input, brightnessChange = false) {
  const light = HOME_LIGHTS.find((item) => item.id === input.dataset.device);
  if (!light || !homeDevice(light.id).available) return;
  const device = homeDevice(light.id);
  const brightness = brightnessChange ? homeBrightness(input.value) : device.brightness === null || device.brightness === 0 ? 100 : device.brightness;
  if (brightness === null) return;
  const power = brightnessChange ? brightness > 0 : device.power !== true;
  const values = power ? { power, brightness } : { power };
  const changes = brightnessChange ? { power, brightness } : values;
  return runHomeAction(light.id, "home_light_set", Object.assign({ device_id: light.id }, values), new Map([[light.id, changes]]), light.name);
}
function silenceHomeSpeaker(button) {
  const speaker = HOME_SPEAKERS.find((item) => item.id === button.dataset.device);
  if (!speaker || !homeDevice(speaker.id).available) return;
  const mode = button.dataset.mode === undefined ? "auto" : button.dataset.mode;
  if (["auto", "stop", "pause", "mute", "play"].indexOf(mode) === -1) return;
  const change = mode === "play" ? { status: "Playing", playerState: "PLAYING" }
    : mode === "pause" ? { status: "Paused", playerState: "PAUSED" } : { status: "Silencing…" };
  return runHomeAction(speaker.id, "home_speaker_silence", { device_id: speaker.id, mode }, new Map([[speaker.id, change]]), speaker.name);
}
function runHomeScene(button) {
  const scene = HOME_SCENES.find((item) => item.id === button.dataset.scene);
  if (!scene) return;
  const changes = new Map();
  if (scene.id === "all_lights_off") HOME_LIGHTS.forEach((light) => changes.set(light.id, { power: false }));
  if (scene.id === "weekday_silence") ["kids_speaker", "kid_display"].forEach((id) => changes.set(id, { status: "Silencing…" }));
  return runHomeAction("scene", "home_run_scene", { scene_id: scene.id }, changes, scene.name);
}
function previewHomeBrightness(input) {
  const light = HOME_LIGHTS.find((item) => item.id === input.dataset.device);
  const brightness = homeBrightness(input.value);
  if (!light || brightness === null || homeBusy(light.id) || !homeDevice(light.id).available) return;
  const output = document.getElementById(`home-value-${light.id}`);
  if (output) output.textContent = `${brightness}%`;
}
function render() {
  applyTheme();   // a state refresh may carry new sun times; the pairing screen follows the clock too
  if (!state || pairing || currentView !== "controls") disposeSpeaker_hubCards();
  if (!state || pairing) return;
  const focused = document.activeElement;
  const focusedId = focused && focused.id;
  const selection = focused && typeof focused.selectionStart === "number" ? [focused.selectionStart, focused.selectionEnd] : null;
  renderClock();
  renderHeader();
  const view = VIEWS[currentView];
  $("#app").dataset.activeView = currentView;
  $("#view-eyebrow").textContent = view[0];
  $("#view-title").textContent = view[1];
  $("#view-subtitle").textContent = view[2];
  $("#view-eyebrow").hidden = !view[0];
  $("#view-subtitle").hidden = currentView === "calendar";
  if (currentView === "calendar") $("#view-title").textContent = dateText(calendarView === "month" ? viewedMonthFirstDay() : state.today, { month: "long", year: "numeric" });
  const count = kidBoards().reduce((sum, board) => sum + progress(board).done, 0);
  $("#view-tools").innerHTML = currentView === "chores" ? `<span class="pill">✧ ${count} little wins today</span>`
    : currentView === "calendar" ? `<div class="calendar-tools"><div class="calendar-nav" role="group" aria-label="Change month"><button type="button" id="calendar-prev" data-action="calendar-month" data-delta="-1" aria-label="Previous month" title="Previous month">‹</button><button type="button" id="calendar-next" data-action="calendar-month" data-delta="1" aria-label="Next month" title="Next month">›</button></div><div class="calendar-toggle" role="group" aria-label="Calendar view">${["week", "month"].map((mode) => `<button type="button" id="calendar-${mode}" data-action="calendar-view" data-mode="${mode}" aria-pressed="${calendarView === mode}">${mode === "week" ? "Week" : "Month"}</button>`).join("")}</div></div>`
    : currentView === "photos" && photos().length ? '<button type="button" data-action="slideshow">Play slideshow</button>' : "";
  const renderers = { chores: renderChores, calendar: renderCalendar, weather: renderWeather, meals: renderMeals, groceries: renderGroceries, photos: renderPhotos, settings: renderSettings };
  if (currentView === "controls") {
    const controls = renderControls(), content = $("#content");
    if (content.firstElementChild !== controls) {
      content.textContent = "";
      content.appendChild(controls);
    }
  } else $("#content").innerHTML = renderers[currentView]();
  if (currentView === "settings") mountDisplayCard();
  document.querySelectorAll("#content form[data-form]").forEach((form) => {
    form.querySelector("button").disabled = pendingForms.has(form.dataset.form);
  });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === currentView;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  if (focusedId) {
    const replacement = document.getElementById(focusedId);
    if (replacement && (replacement !== focused || document.activeElement !== replacement)) {
      replacement.focus({ preventScroll: true });
      if (selection && replacement.setSelectionRange) replacement.setSelectionRange(selection[0], selection[1]);
    }
  }
  if (asleep) $("#saver-next").textContent = nextEvent();
}
function showView(name, updateHash = true, direction = null) {
  if (!Object.prototype.hasOwnProperty.call(VIEWS, name)) name = "chores";
  if (name === "calendar") calendarMonthOffset = 0;   // the calendar always opens on the current month
  const changed = currentView !== name;
  currentView = name;
  if (updateHash && location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
  render();
  if (changed) window.scrollTo(0, 0);
  if (direction) slideContent(direction);
}
// Slide the freshly rendered view in from the side the finger came from. Only transform and
// opacity animate, so WebView 83 keeps the whole move on the compositor.
function slideContent(direction) {
  const slideClass = direction === "from-right" ? "slide-from-right" : direction === "from-left" ? "slide-from-left" : "";
  const content = $("#content");
  if (!slideClass || !content) return;
  content.classList.remove("slide-from-right", "slide-from-left");
  void content.offsetWidth;   // Commit the removal so a repeated swipe restarts the animation.
  content.classList.add(slideClass);
}
function scrollsSideways(element, boundary) {
  for (let node = element; node && node !== boundary; node = node.parentNode) {
    if (node.scrollWidth > node.clientWidth + 1) {
      const overflow = getComputedStyle(node).overflowX;
      if (overflow === "auto" || overflow === "scroll") return true;
    }
  }
  return false;
}
// A quick horizontal flick moves between the main views. Form fields, modals, the routine tabs
// and anything that scrolls sideways keep their own horizontal gestures.
function beginSwipe(event) {
  swipeStart = null;
  if (!state || pairing || asleep) return;
  const touch = event.changedTouches && event.changedTouches[0];
  if (!touch || (event.touches && event.touches.length > 1)) return;
  const target = event.target;
  if (!target || !target.closest || target.closest(SWIPE_IGNORE)) return;
  if (scrollsSideways(target, event.currentTarget)) return;
  swipeStart = { x: touch.clientX, y: touch.clientY, at: Date.now() };
}
function endSwipe(event) {
  const start = swipeStart;
  swipeStart = null;
  if (!start || !state || pairing || asleep) return;
  const touch = event.changedTouches && event.changedTouches[0];
  if (!touch) return;
  const deltaX = touch.clientX - start.x;
  const deltaY = touch.clientY - start.y;
  const elapsed = Date.now() - start.at;
  if (Math.abs(deltaX) < 50 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.5 || elapsed > 600) return;
  const index = SWIPE_VIEWS.indexOf(currentView);
  if (index === -1) return;
  if (deltaX < 0 && index < SWIPE_VIEWS.length - 1) showView(SWIPE_VIEWS[index + 1], true, "from-right");
  else if (deltaX > 0 && index > 0) showView(SWIPE_VIEWS[index - 1], true, "from-left");
}

// Resume audio inside the original tap, before waiting for the server.
function unlockAudio() {
  if (!soundEnabled) return;
  try {
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (!Audio) return;
    if (!audioContext) audioContext = new Audio();
    if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
  } catch (error) { /* A silent WebView still completes the chore. */ }
}
function chime() {
  if (!soundEnabled || !audioContext || audioContext.state !== "running") return;
  try {
    [783.99, 1046.5].forEach((frequency, index) => {
      const at = audioContext.currentTime + index * .11;
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(.001, at);
      gain.gain.exponentialRampToValueAtTime(.12, at + .015);
      gain.gain.exponentialRampToValueAtTime(.001, at + .34);
      oscillator.connect(gain);
      gain.connect(audioContext.destination);
      oscillator.start(at);
      oscillator.stop(at + .36);
      oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
    });
  } catch (error) { /* Audio is a bonus, never a dependency. */ }
}
function celebrate(memberId, point) {
  lastCelebration = Date.now();
  chime();
  const x = point ? point.x : window.innerWidth / 2;
  const y = point ? point.y : window.innerHeight * .45;
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const star = document.createElement("span");
    star.className = "flying-star";
    star.textContent = "★";
    star.style.left = `${x}px`;
    star.style.top = `${y}px`;
    $("#star-layer").appendChild(star);
    setTimeout(() => star.remove(), 1000);
  }
  burst($("#confetti"), [(PEOPLE[memberId] || PEOPLE.family).color, "#EAC586", "#F5F2EB"], { x, y });
}
function celebrateChanges(before, after) {
  if (!before || before.today !== after.today) return;
  const completed = new Set();
  list(before.kids).forEach((board) => list(board.routines).forEach((routine) => {
    if (routine.complete) completed.add(routine.id);
  }));
  const finishes = [];
  list(after.kids).forEach((board) => list(board.routines).forEach((routine) => {
    if (routine.complete && !completed.has(routine.id)) finishes.push({ member: board.member, title: routine.title });
  }));
  const approved = new Set(list(before.decided).filter((reward) => reward.status === "approved").map((reward) => reward.id));
  list(after.decided).forEach((reward) => {
    if (reward.status === "approved" && !approved.has(reward.id)) finishes.push({ member: reward.member, title: `${reward.title} approved` });
  });
  if (finishes.length) {
    toast(finishes.map((finish) => `${member(finish.member).name}: ${finish.title}!`).join("  "));
    const localTap = Array.from(pending.keys()).some((key) => finishes.some((finish) => key.indexOf(`${finish.member}:`) === 0));
    if (!localTap && Date.now() - lastCelebration > 1500) celebrate(finishes[0].member);
  }
}
function findChore(memberId, itemId) {
  const board = state && list(state.kids).find((kid) => kid.member === memberId);
  if (!board) return null;
  return list(board.routines).reduce((all, routine) => all.concat(list(routine.steps)), []).concat(list(board.chores))
    .find((item) => item.item === itemId);
}
async function optimisticAction(key, value, op, args, applyAccepted, onAccepted) {
  if (pending.has(key)) return;
  pending.set(key, { value });
  render();
  let accepted = false;
  try {
    await act(op, args);
    accepted = true;
    if (onAccepted) onAccepted();
    await refresh(true);
  } catch (error) {
    if (accepted && state) {
      applyAccepted();
      cacheState();
      toast("Saved. Waiting for the wall to reconnect.");
    } else toast(error.message);
  } finally {
    pending.delete(key);
    render();
  }
}
function tickChore(button) {
  // The kiosk never ticks an automated step. Its row renders without data-action, but a stale row or a synthetic
  // click can still land here, so the lock holds twice: against the row's classes, then against the live state.
  const refusal = "This step is completed in the Math app, not on the wall.";
  const classes = button.classList;
  if (classes && (classes.contains("disabled") || classes.contains("auto-step"))) { toast(refusal); return; }
  const memberId = button.dataset.member;
  const itemId = button.dataset.item;
  const item = findChore(memberId, itemId);
  if (!item) return;
  if (lockedStep(item)) { toast(refusal); return; }
  const value = !effectiveDone(memberId, item);
  const rectangle = button.getBoundingClientRect();
  const point = { x: rectangle.right - 32, y: rectangle.top + rectangle.height / 2 };
  if (value) unlockAudio();
  return optimisticAction(itemKey(memberId, itemId), value, value ? "complete" : "uncomplete",
    { member: memberId, item: itemId }, () => {
      const current = findChore(memberId, itemId);
      if (current) current.done = value;
    }, value ? () => celebrate(memberId, point) : null);
}
function tickGrocery(button) {
  const itemId = button.dataset.item;
  const item = list(state.groceries).find((grocery) => grocery.id === itemId);
  if (!item) return;
  const value = !item.checked;
  return optimisticAction(`grocery:${itemId}`, value, "check_groceries", { items: [itemId], checked: value }, () => {
    const current = list(state.groceries).find((grocery) => grocery.id === itemId);
    if (current) current.checked = value;
  });
}

function closeDialog() {
  stopVideoReplyStream();
  $("#dialog").hidden = true;
  $("#dialog-content").innerHTML = "";
  dialogMember = null;
  dialogTab = "store";
  if (!pairing && !asleep) $("#app").removeAttribute("aria-hidden");
  if (dialogFocus && document.contains(dialogFocus)) dialogFocus.focus({ preventScroll: true });
  lastTouch = Date.now();
}
function deltaText(value) {
  const amount = Math.trunc(Number(value) || 0);
  return `${amount > 0 ? "+" : amount < 0 ? "-" : ""}${Math.abs(amount)} ★`;
}
function deltaClass(value) {
  const amount = Number(value) || 0;
  return amount > 0 ? "plus" : amount < 0 ? "minus" : "zero";
}
function actorName(id) {
  if (!id) return "";
  if (id === "kiosk") return "Kitchen wall";
  const person = list(state && state.members).find((candidate) => candidate.id === id) || PEOPLE[id];
  return person ? person.name : String(id);
}
function renderStoreTab(board, memberId) {
  const waiting = list(board.pending);
  const available = whole(board.available);   // below zero, "more stars to go" counts the climb back as well
  const rewards = list(state.rewards).filter((reward) => reward && reward.id).sort((a, b) => number(a.cost) - number(b.cost));
  const requests = waiting.map((reward) => `<div class="pending-reward">${escapeHtml(reward.emoji || "⭐")} ${escapeHtml(reward.title)}<br><small>Waiting for a parent</small><div><button type="button" data-action="decide" data-id="${escapeHtml(reward.id)}" data-approve="1">Approve</button><button type="button" data-action="decide" data-id="${escapeHtml(reward.id)}" data-approve="0">Not now</button></div></div>`).join("");
  const pin = waiting.length && state.role === "kiosk" ? '<label class="pin-label" for="parent-pin">Parent PIN to approve or decline</label><input id="parent-pin" type="password" inputmode="numeric" maxlength="8" autocomplete="off" placeholder="Parent PIN">' : "";
  const shelf = rewards.length ? `<div class="reward-list">${rewards.map((reward) => {
    const cost = number(reward.cost);
    const short = cost - available;
    return `<button type="button" class="reward" data-action="ask" data-member="${escapeHtml(memberId)}" data-reward="${escapeHtml(reward.id)}"${short > 0 ? " disabled" : ""}><span class="reward-emoji" aria-hidden="true">${escapeHtml(reward.emoji || "⭐")}</span><span class="reward-label">${escapeHtml(reward.title)}<small>${short > 0 ? `${short} more ${short === 1 ? "star" : "stars"} to go` : "Ask a parent"}</small></span><strong>${cost} ★</strong></button>`;
  }).join("")}</div>` : '<p class="history-empty">Rewards will appear here when your family adds them.</p>';
  return `${requests}${pin}${shelf}`;
}
function renderHistoryTab(board) {
  const days = list(board.daily_history).filter((day) => day && /^\d{4}-\d{2}-\d{2}$/.test(day.date));
  if (!days.length) return '<p class="history-empty">No stars yet. Finish a chore and it will show up here.</p>';
  return `<div class="history-list">${days.map((day, index) => {
    const open = index === 0;
    const entries = list(day.entries);
    const label = String(day.label || day.date);
    const dated = label === "Today" || label === "Yesterday";
    const rows = entries.length ? entries.map((entry) => `<div class="history-entry" data-kind="${escapeHtml(entry.kind || "")}"><time>${escapeHtml(entry.time || "")}</time><span class="entry-reason">${escapeHtml(entry.reason || "Stars")}${entry.by ? `<small>${escapeHtml(actorName(entry.by))}</small>` : ""}</span><span class="entry-delta ${deltaClass(entry.delta)}">${deltaText(entry.delta)}</span></div>`).join("")
      : `<p class="history-empty">${label === "Today" ? "Nothing yet today. Finish a chore to add a star." : "No stars this day."}</p>`;
    return `<div class="history-day"><button type="button" class="history-day-toggle" data-action="toggle-day" data-date="${day.date}" aria-expanded="${open}" aria-controls="day-details-${day.date}"><span class="history-day-label">${escapeHtml(label)}${dated ? `<small>${escapeHtml(dateText(day.date, { weekday: "short", month: "short", day: "numeric" }))}</small>` : ""}</span><span class="day-net ${deltaClass(day.net)}">${deltaText(day.net)}</span><span class="chevron" aria-hidden="true"></span></button><div class="history-day-details" id="day-details-${day.date}"${open ? "" : " hidden"}>${rows}</div></div>`;
  }).join("")}</div>`;
}
// The kid's own sheet: who they are, what they can spend, the store and the day-by-day story.
function showKidSheet(memberId, activeTab = "store") {
  const board = kidBoards().find((kid) => kid.member === memberId);
  if (!board) return;
  const wasOpen = !$("#dialog").hidden;
  if (!wasOpen) dialogFocus = document.activeElement;
  const focusedId = wasOpen && document.activeElement ? document.activeElement.id : "";
  dialogMember = memberId;
  dialogTab = activeTab === "history" ? "history" : "store";
  const person = member(memberId);
  const tab = (name, label) => `<button type="button" class="dialog-tab${dialogTab === name ? " active" : ""}" data-action="tab-${name}" data-member="${escapeHtml(memberId)}" id="kid-tab-${name}" role="tab" aria-selected="${dialogTab === name}" aria-controls="kid-panel">${label}</button>`;
  const balance = whole(board.available);
  // Below zero, the sheet says what it takes to climb back out instead of "stars to spend".
  const balanceText = balance < 0 ? `★ ${balance} stars. Earn ${-balance} to get back to zero.` : `★ ${balance} stars to spend`;
  $("#dialog-content").innerHTML = `<div class="sheet-heading kid-sheet-heading" style="${colors(memberId)}"><span class="avatar" aria-hidden="true">${escapeHtml(person.emoji || person.name[0])}</span><div class="kid-name"><h2 id="dialog-title">${escapeHtml(person.name)}</h2><small>${escapeHtml(personDetail(person) || "Stars and rewards")}</small></div><button type="button" class="close-button" data-action="close-dialog" aria-label="Close">×</button></div><p class="sheet-balance${balance < 0 ? " negative" : ""}">${balanceText}</p><div class="dialog-tabs" role="tablist" aria-label="${escapeHtml(person.name)}'s stars">${tab("store", "🎁 Store")}${tab("history", "📜 Daily History")}</div><div id="kid-panel" class="dialog-panel" role="tabpanel" aria-labelledby="kid-tab-${dialogTab}">${dialogTab === "history" ? renderHistoryTab(board) : renderStoreTab(board, memberId)}</div>`;
  $("#dialog").hidden = false;
  $("#app").setAttribute("aria-hidden", "true");
  // Keep focus on the tab that was just pressed; otherwise start at the close button.
  const target = (focusedId && $(`#dialog-content #${focusedId}`)) || $("#dialog-content button");
  if (target) target.focus({ preventScroll: true });
}
function showRewards(memberId) { return showKidSheet(memberId, "store"); }
async function rewardAction(button) {
  const memberId = dialogMember;
  const deciding = button.dataset.action === "decide";
  let args;
  if (deciding) {
    args = { id: button.dataset.id, approve: button.dataset.approve === "1" };
    if (state.role === "kiosk") {
      const pin = $("#parent-pin");
      if (!pin || !/^\d{4,8}$/.test(pin.value)) { toast("Enter the parent's 4–8 digit PIN."); if (pin) pin.focus(); return; }
      args.pin = pin.value;
      pin.value = "";
    }
  } else args = { member: button.dataset.member, reward: button.dataset.reward };
  document.querySelectorAll('#dialog [data-action="ask"], #dialog [data-action="decide"]').forEach((item) => { item.disabled = true; });
  let accepted = false;
  try {
    await act(deciding ? "decide_reward" : "request_reward", args);
    accepted = true;
    toast(deciding ? "Your answer is on the wall." : "Asked! A parent will answer soon.");
    await refresh(true);
  } catch (error) { toast(accepted ? "Saved. Waiting for the wall to reconnect." : error.message); }
  finally { if (state && dialogMember === memberId) showKidSheet(memberId, dialogTab); }
}

function nextEvent() {
  const now = clock();
  // A waiting video message leads the Home Screen line ahead of whatever is next on the calendar.
  const first = activeVideoMessages()[0];
  const notice = first ? `💌 You have a video message from ${first.sender || "Dad"}${first.caption ? ` — "${first.caption}"` : ""} · Tap screen to watch!  •  ` : "";
  for (const day of agenda()) {
    for (const event of day.events) {
      if (!event.all_day && (day.day > now.day || (day.day === now.day && minutesOf(String(event.start).slice(11, 16)) >= now.minutes))) {
        return `${notice}Up next · ${day.day === now.day ? "" : `${dayLabel(day.day)} · `}${eventTime(event)} · ${event.title}`;
      }
    }
  }
  return `${notice}A little time to just be together.`;
}
// A tap on the Home Screen wakes the wall; while an unwatched video message is waiting the same tap
// opens it, so "Tap screen to watch!" means exactly that. Once every clip has been watched, only a
// tap on the message line itself plays one again.
function wakeFromTap(target) {
  const waiting = activeVideoMessages();
  const unwatched = waiting.find((item) => !item.watched_at);
  const onNotice = Boolean(target && typeof target.closest === "function" && target.closest("#saver-next"));
  wake();
  const pick = unwatched || (onNotice ? waiting[0] : null);
  if (pick) showVideoModal(pick.id);
  return Boolean(pick);
}
let lastPhotoIndex = -1;
let cameraLit = false;
let lastCameraMotion = 0;
function setCameraLit(lit) {
  if (cameraLit === lit) return;
  cameraLit = lit;
  document.body.classList.toggle("camera-lit", lit);
  if (lit && asleep) showSlide();
}
function showSlide(explicitIndex) {
  if (!asleep || (night && !cameraLit)) return;
  const collection = photos();
  if (!collection.length) {
    $("#slide-a").classList.remove("shown");
    $("#slide-b").classList.remove("shown");
    return;
  }
  let pick;
  if (typeof explicitIndex === "number" && explicitIndex >= 0) {
    pick = explicitIndex % collection.length;
  } else if (collection.length === 1) {
    pick = 0;
  } else {
    pick = Math.floor(Math.random() * collection.length);
    if (pick === lastPhotoIndex) pick = (pick + 1) % collection.length;
  }
  lastPhotoIndex = pick;
  const frames = [$("#slide-a"), $("#slide-b")];
  const incoming = frames[slide % 2];
  const outgoing = frames[(slide + 1) % 2];
  const generation = ++slideGeneration;
  incoming.onload = () => {
    if (!asleep || generation !== slideGeneration) return;
    incoming.classList.add("shown");
    outgoing.classList.remove("shown");
  };
  incoming.onerror = () => { if (generation === slideGeneration) incoming.classList.remove("shown"); };
  incoming.src = photoUrl(collection[pick]);
  if (incoming.complete && incoming.naturalWidth) incoming.onload();
  slide += 1;
}
function sleep(automatic = false, index = 0) {
  if (!state || pairing || !$("#dialog").hidden) return;
  returnView = currentView;
  automaticSleep = automatic;
  asleep = true;
  sleptAt = Date.now();
  setCameraLit(false);
  slide = index;
  $("#slide-a").classList.remove("shown");
  $("#slide-b").classList.remove("shown");
  $("#screensaver").hidden = false;
  $("#app").setAttribute("aria-hidden", "true");
  $("#saver-next").textContent = nextEvent();
  renderClock();
  showSlide(automatic ? undefined : index);
  clearInterval(slideTimer);
  slideTimer = setInterval(showSlide, Math.max(5, number(state.screensaver && state.screensaver.slide_seconds) || 15) * 1000);
  $("#screensaver").focus({ preventScroll: true });
}
function wake() {
  asleep = false;
  automaticSleep = false;
  setCameraLit(false);
  lastTouch = Date.now();
  ignoreTapsUntil = Date.now() + 400;
  slideGeneration += 1;
  clearInterval(slideTimer);
  $("#screensaver").hidden = true;
  if (!pairing && $("#dialog").hidden) $("#app").removeAttribute("aria-hidden");
  showView(returnView);
  if (!pairing) $("#content").focus({ preventScroll: true });
}
function setNight(dark) {
  if (night === dark) return;
  night = dark;
  document.body.classList.toggle("night", dark);
  if (!dark) setCameraLit(false);
  try {
    if (window.fully && typeof window.fully.setScreenBrightness === "function") window.fully.setScreenBrightness(dark ? 8 : 180);
  } catch (error) { /* Fully's optional interface may be disabled. */ }
  if (!dark && asleep) showSlide();
}
function tick() {
  applyTheme();
  if (!state || pairing) return;
  renderClock();
  const now = clock();
  if (syncRoutineWindow(now) && currentView === "chores") render();
  if (now.day !== state.today && Date.now() - lastDayRefresh > 10000) {
    lastDayRefresh = Date.now();
    scheduleRefresh();
  }
  // Night and the idle screensaver apply to every screen: the kitchen wall and a parent's tablet alike.
  const nightSettings = state.night || {};
  setNight(inWindow(now.minutes, minutesOf(nightSettings.dim_from || "21:00"), minutesOf(nightSettings.dim_to || "06:30")));
  const idleSeconds = state.screensaver ? number(state.screensaver.idle_seconds) || 180 : 180;
  if (asleep) {
    if (cameraLit && (!night || Date.now() - lastCameraMotion >= idleSeconds * 1000)) setCameraLit(false);
    return;
  }
  if (!$("#dialog").hidden) return;
  const idle = Date.now() - lastTouch;
  if (idle >= 60000 && currentView !== "chores") showView("chores");
  if (idle >= idleSeconds * 1000) sleep(true);
}

// Camera wake. A luminance thumbnail of the front camera is compared with the one before it; enough
// pixels changing by enough means someone walked up. Pure, so the thresholds can be tested on arrays.
function detectCameraMotion(prevLuma, currLuma, diffThreshold = 22, minChangedPixels = 24) {
  if (!prevLuma || !currLuma || prevLuma.length !== currLuma.length) return false;
  let changed = 0;
  for (let index = 0; index < currLuma.length; index += 1) {
    if (Math.abs(currLuma[index] - prevLuma[index]) >= diffThreshold) {
      changed += 1;
      if (changed >= minChangedPixels) return true;
    }
  }
  return false;
}
// RGBA bytes to one luminance byte a pixel (Rec. 601 weights in integer arithmetic).
function cameraLuma(rgba) {
  const luma = new Uint8Array(rgba.length >> 2);
  for (let index = 0; index < luma.length; index += 1) {
    const offset = index * 4;
    luma[index] = (rgba[offset] * 77 + rgba[offset + 1] * 151 + rgba[offset + 2] * 28) >> 8;
  }
  return luma;
}
// One sample: draw the current camera frame at 32x24 and compare it with the last one. Only touching the
// screen opens Chores; camera motion keeps the Home Screen (clock with photos) on during the day and
// lights up the Home Screen at night when someone walks by.
function sampleCameraFrame() {
  if (!cameraVideo || !cameraCanvas || document.hidden) return false;
  if (typeof cameraVideo.readyState === "number" && cameraVideo.readyState < 2) return false;   // nothing decoded yet
  let luma;
  try {
    const context = cameraCanvas.getContext("2d");
    if (!context) return false;
    context.drawImage(cameraVideo, 0, 0, CAMERA_WIDTH, CAMERA_HEIGHT);
    luma = cameraLuma(context.getImageData(0, 0, CAMERA_WIDTH, CAMERA_HEIGHT).data);
  } catch (error) { return false; }   // the stream ended, or the frame is not readable yet
  const previous = cameraPrevLuma;
  cameraPrevLuma = luma;
  if (asleep && Date.now() - sleptAt < CAMERA_SETTLE_MS) return false;
  if (!detectCameraMotion(previous, luma)) return false;
  lastCameraMotion = Date.now();
  if (asleep && night) setCameraLit(true);
  return true;
}
// Ask for the front camera at the smallest size and rate Chromium offers, then sample it on a timer.
// Resolves true once sampling runs. Without a camera, a secure context or permission (getUserMedia is
// absent over plain HTTP) the wall quietly keeps waking on touch alone.
function startCameraWake() {
  if (cameraRequest) return cameraRequest;
  if (typeof navigator === "undefined" || !navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") return Promise.resolve(false);
  if (typeof document === "undefined" || typeof document.createElement !== "function") return Promise.resolve(false);
  const request = navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: 64, height: 48, frameRate: 5 }, audio: false })
    .then((stream) => {
      // stopCameraWake() ran while the camera asked (a video reply wanted it): let this stream go at once.
      if (cameraRequest !== request) {
        if (stream && typeof stream.getTracks === "function") stream.getTracks().forEach((track) => { try { track.stop(); } catch (error) { /* gone */ } });
        return false;
      }
      cameraStream = stream;
      cameraVideo = document.createElement("video");
      cameraVideo.setAttribute("muted", "");
      cameraVideo.setAttribute("playsinline", "");
      cameraVideo.muted = true;
      cameraVideo.playsInline = true;
      cameraVideo.srcObject = stream;   // never attached to the page: nothing is drawn, nothing glows
      cameraCanvas = document.createElement("canvas");
      cameraCanvas.width = CAMERA_WIDTH;
      cameraCanvas.height = CAMERA_HEIGHT;
      cameraPrevLuma = null;
      const playing = typeof cameraVideo.play === "function" ? cameraVideo.play() : null;
      if (playing && typeof playing.catch === "function") playing.catch(() => {});
      cameraTimer = setInterval(sampleCameraFrame, CAMERA_FRAME_MS);
      return true;
    })
    .catch(() => { if (cameraRequest === request) cameraRequest = null; return false; });   // refused or missing: a later call may try again
  cameraRequest = request;
  return request;
}
// Switch the motion-wake camera off and release it, so another getUserMedia() can have the front camera.
function stopCameraWake() {
  if (cameraTimer !== null) { clearInterval(cameraTimer); cameraTimer = null; }
  const stream = cameraStream;
  cameraStream = null;
  cameraVideo = null;
  cameraCanvas = null;
  cameraPrevLuma = null;
  cameraRequest = null;
  if (stream && typeof stream.getTracks === "function") {
    stream.getTracks().forEach((track) => { try { track.stop(); } catch (error) { /* already stopped */ } });
  }
}

function showPairing() {
  if (pairing) return;
  pairing = true;
  if (asleep) wake();
  closeDialog();
  state = null;
  pending.clear();
  if (source) source.close();
  source = null;
  clearTimeout(refreshTimer);
  try { localStorage.removeItem(CACHE_KEY); } catch (error) { /* Optional storage. */ }
  $("#content").innerHTML = "";
  $("#sidebar-kids").innerHTML = "";
  $("#notes").hidden = true;
  $("#pairing").hidden = false;
  $("#app").setAttribute("aria-hidden", "true");
  setNight(false);
  setOnline(false);
  $("#pair-code").focus();
}
async function pair() {
  const code = $("#pair-code").value;
  if (!/^\d{6}$/.test(code)) { $("#pair-error").textContent = "Enter all six digits from your pairing code."; return; }
  $("#pair-submit").disabled = true;
  $("#pair-error").textContent = "";
  try {
    await api("/api/pair", { code, name: window.matchMedia("(orientation: portrait)").matches ? "Phone" : "Kitchen panel" });
    window.location.reload();
  } catch (error) { $("#pair-error").textContent = error.message; $("#pair-submit").disabled = false; }
}
async function submitList(form) {
  const name = form.dataset.form;
  if ((name !== "meal" && name !== "grocery") || pendingForms.has(name)) return;
  const input = form.querySelector("input");
  const text = input.value.trim();
  if (!text) return;
  pendingForms.add(name);
  form.querySelector("button").disabled = true;
  let accepted = false;
  try {
    await act(name === "meal" ? "set_meal" : "add_groceries", name === "meal" ? { day: "today", title: text } : { items: [text] });
    accepted = true;
    // Keep any new draft entered while this save was in flight.
    if (drafts[name] === input.value && input.value.trim() === text) drafts[name] = "";
    await refresh(true);
  } catch (error) { toast(accepted ? "Saved. Waiting for the wall to reconnect." : error.message); }
  finally { pendingForms.delete(name); render(); }
}
// The sidebar toggle is static markup, so its label follows the real full-screen state.
function syncFullscreenButton() {
  const button = $(".fullscreen-btn");
  if (!button) return;
  const label = document.fullscreenElement ? "Exit full screen" : "Full screen";
  button.title = label;
  button.setAttribute("aria-label", label);
}
async function fullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
    else toast("Use your kiosk app's full-screen setting.");
  } catch (error) { toast("Full screen isn't available in this browser."); }
  syncFullscreenButton();
  render();
}
const actions = {
  "home-light-toggle": setHomeLight,
  "home-light-brightness"(input) { return setHomeLight(input, true); },
  "home-speaker-silence": silenceHomeSpeaker,
  "home-scene": runHomeScene,
  tick: tickChore,
  grocery: tickGrocery,
  routine(button) {
    if (KIDS.indexOf(button.dataset.member) === -1 || !ROUTINE_SECTIONS.some((section) => section.kind === button.dataset.routine)) return;
    syncRoutineWindow(clock());
    routineSelections.set(button.dataset.member, button.dataset.routine);
    render();
  },
  "calendar-view"(button) {
    if (button.dataset.mode !== "week" && button.dataset.mode !== "month") return;
    calendarView = button.dataset.mode;
    calendarMonthOffset = 0;   // Week and Month both start from today; pressing Month again comes back to it
    render();
  },
  // ‹ and › page Month view a month at a time (from Week view they open it); 0 is today's month.
  "calendar-month"(button) {
    const delta = Number(button.dataset.delta);
    if (delta !== -1 && delta !== 0 && delta !== 1) return;
    calendarMonthOffset = delta === 0 ? 0 : calendarMonthOffset + delta;
    calendarView = "month";
    render();
  },
  kid(button) {
    showView("chores");
    const column = document.getElementById(`kid-${button.dataset.member}`);
    if (column) column.scrollIntoView({ block: "nearest", behavior: "auto" });
  },
  "kid-sheet"(button) { showKidSheet(button.dataset.member, "store"); },
  stars(button) { showKidSheet(button.dataset.member, "store"); },
  "tab-store"(button) { showKidSheet(button.dataset.member || dialogMember, "store"); },
  "tab-history"(button) { showKidSheet(button.dataset.member || dialogMember, "history"); },
  "toggle-day"(button) {
    const day = String(button.dataset.date || "");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return;
    const details = $(`#day-details-${day}`);
    if (!details) return;
    const open = Boolean(details.hidden);
    details.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
  },
  "close-dialog": closeDialog,
  ask: rewardAction,
  decide: rewardAction,
  slideshow(button) { sleep(false, number(button.dataset.index)); },
  sound() {
    soundEnabled = !soundEnabled;
    try { localStorage.setItem(SOUND_KEY, soundEnabled ? "on" : "off"); } catch (error) { /* Optional storage. */ }
    if (soundEnabled) unlockAudio();
    render();
  },
  theme(button) {
    const mode = button.dataset.mode;
    if (THEME_MODES.indexOf(mode) === -1) return;
    themeMode = mode;
    try { localStorage.setItem(THEME_KEY, mode); } catch (error) { /* Optional storage. */ }
    render();   // applies the theme and redraws the pressed state
  },
  fullscreen,
  "copy-text"(button) {
    const text = String(button.dataset.text || "");
    if (!text) return;
    let copied = false;
    try {
      const box = document.createElement("textarea");
      box.value = text;
      box.style.position = "fixed";
      box.style.left = "-9999px";
      document.body.appendChild(box);
      box.focus();
      box.select();
      copied = document.execCommand("copy");
      document.body.removeChild(box);
    } catch (error) { /* Fallback to clipboard API below. */ }
    if (!copied && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(text).catch(() => {});
    }
    toast(`Copied: ${text}`);
  },
  "approve-dress-day"(button) { return decideDressDay("approve_dress_day", button); },
  "dismiss-dress-day"(button) { return decideDressDay("dismiss_dress_day", button); },
  "play-video"(button) { showVideoModal(button.dataset.id); },
  "react-video": reactToVideo,
  "unmute-video": unmuteVideo,
  "start-video-reply": startVideoReply,
  "stop-video-reply": finishVideoReply,
  "cancel-video-reply": cancelVideoReply,
  async refresh() { await refresh(true); toast("The wall is up to date."); },
  async install() {
    if (!installPrompt) return;
    const prompt = installPrompt;
    installPrompt = null;
    await prompt.prompt();
    await prompt.userChoice;
    render();
  },
};

// Screen Wake Lock keeps the wall lit while Family Wall is visible. Chromium releases the
// sentinel whenever the page hides, so it is requested again on return, and once more on the
// first touch (captured so a screensaver wake tap counts) for WebViews that wait for a gesture.
// Over plain HTTP on a LAN/Tailscale IP, navigator.wakeLock is omitted by Chromium, so a silent
// 1x1 looping video (NoSleep fallback) and Fully Kiosk's keepScreenOn bridge also hold the screen.
let screenWakeLock = null;
let wakeLockRequest = null;
let keepAwakeVideo = null;
let keepAwakeCanvas = null;
let keepAwakeTick = 0;
function keepScreenAwake() {
  try {
    if (window.fully && typeof window.fully.keepScreenOn === "function") window.fully.keepScreenOn(true);
  } catch (error) { /* Optional kiosk bridge. */ }
  if (screenWakeLock && !screenWakeLock.released) return;
  if (typeof document === "undefined" || typeof document.createElement !== "function" || !document.body) return;
  try {
    if (!keepAwakeVideo) {
      keepAwakeCanvas = document.createElement("canvas");
      keepAwakeCanvas.width = 2;
      keepAwakeCanvas.height = 2;
      const ctx = keepAwakeCanvas.getContext && keepAwakeCanvas.getContext("2d");
      if (ctx) { ctx.fillStyle = "#000001"; ctx.fillRect(0, 0, 2, 2); }
      keepAwakeVideo = document.createElement("video");
      keepAwakeVideo.setAttribute("muted", "");
      keepAwakeVideo.setAttribute("playsinline", "");
      keepAwakeVideo.setAttribute("loop", "");
      keepAwakeVideo.setAttribute("aria-hidden", "true");
      keepAwakeVideo.muted = true;
      keepAwakeVideo.loop = true;
      keepAwakeVideo.playsInline = true;
      const streamFn = keepAwakeCanvas.captureStream || keepAwakeCanvas.mozCaptureStream || keepAwakeCanvas.webkitCaptureStream;
      if (typeof streamFn === "function") keepAwakeVideo.srcObject = streamFn.call(keepAwakeCanvas, 1);
      keepAwakeVideo.style.cssText = "position:fixed;width:1px;height:1px;opacity:0.01;pointer-events:none;bottom:0;right:0;z-index:-1";
      document.body.appendChild(keepAwakeVideo);
      setInterval(() => {
        if (keepAwakeCanvas && keepAwakeCanvas.getContext) {
          const c = keepAwakeCanvas.getContext("2d");
          if (c) { c.fillStyle = (keepAwakeTick++ & 1) ? "#000001" : "#000002"; c.fillRect(0, 0, 2, 2); }
        }
      }, 2000);
    }
    if (keepAwakeVideo.paused) {
      const playPromise = keepAwakeVideo.play();
      if (playPromise && typeof playPromise.catch === "function") playPromise.catch(() => {});
    }
  } catch (error) { /* Ignore unsupported media playback in headless harnesses. */ }
}
async function requestScreenWakeLock() {
  if (wakeLockRequest || (screenWakeLock && !screenWakeLock.released)) return;
  if (typeof navigator === "undefined" || !("wakeLock" in navigator) || document.visibilityState !== "visible") return;
  try {
    wakeLockRequest = navigator.wakeLock.request("screen");
    screenWakeLock = await wakeLockRequest;
    screenWakeLock.addEventListener("release", () => { screenWakeLock = null; });
  } catch (error) { /* Denied (battery saver, insecure context) or unsupported: the kiosk keeps its own timeout. */ }
  finally { wakeLockRequest = null; }
}

$("#keypad").innerHTML = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Clear", "0", "⌫"]
  .map((digit) => `<button type="button" data-digit="${digit}"${digit === "⌫" ? ' aria-label="Delete last digit"' : ""}>${digit}</button>`).join("");

// Capture the waking gesture before it can reach an underlying chore or form.
document.addEventListener("pointerdown", (event) => {
  lastTouch = Date.now();
  if (!screenWakeLock) requestScreenWakeLock();
  keepScreenAwake();
  if (asleep) { event.preventDefault(); event.stopImmediatePropagation(); wakeFromTap(event.target); }
}, true);
const swipeSurface = $(".main-shell") || $("main");
if (swipeSurface) {
  swipeSurface.addEventListener("touchstart", beginSwipe, { passive: true });
  swipeSurface.addEventListener("touchend", endSwipe, { passive: true });
  swipeSurface.addEventListener("touchcancel", () => { swipeStart = null; }, { passive: true });
}
// Drop the slide class once it has played so will-change releases its compositor layer.
document.addEventListener("animationend", (event) => {
  if (event.target && event.target.id === "content") event.target.classList.remove("slide-from-right", "slide-from-left");
});
document.addEventListener("wheel", () => { lastTouch = Date.now(); }, { passive: true });
document.addEventListener("touchmove", () => { lastTouch = Date.now(); }, { passive: true });
document.addEventListener("keydown", (event) => {
  lastTouch = Date.now();
  if (asleep) { event.preventDefault(); event.stopImmediatePropagation(); wake(); return; }
  if (event.key === "Escape" && !$("#dialog").hidden) { closeDialog(); return; }
  const modal = pairing ? $("#pairing") : !$("#dialog").hidden ? $("#dialog") : null;
  if (event.key === "Tab" && modal) {
    const controls = Array.from(modal.querySelectorAll('button:not([disabled]), input:not([disabled]), [tabindex="0"]')).filter((element) => element.offsetParent !== null);
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (first && (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    }
  }
}, true);
document.addEventListener("click", (event) => {
  keepScreenAwake();
  if (asleep) { event.preventDefault(); event.stopImmediatePropagation(); wake(); return; }
  if (Date.now() < ignoreTapsUntil) { event.preventDefault(); event.stopImmediatePropagation(); return; }
  const digit = event.target.closest("[data-digit]");
  if (digit && pairing) {
    const input = $("#pair-code");
    input.value = digit.dataset.digit === "Clear" ? "" : digit.dataset.digit === "⌫" ? input.value.slice(0, -1) : (input.value + digit.dataset.digit).slice(0, 6);
    return;
  }
  if (!state || pairing) return;
  const button = event.target.closest("[data-action], [data-view]");
  if (!button || button.disabled) return;
  if (button.dataset.view) { showView(button.dataset.view); return; }
  const action = actions[button.dataset.action];
  if (action) Promise.resolve(action(button)).catch((error) => toast(error.message));
}, true);
document.addEventListener("input", (event) => {
  if (event.target.closest("[data-home-brightness]")) { previewHomeBrightness(event.target); return; }
  if (event.target.id === "pair-code") event.target.value = event.target.value.replace(/[^0-9]/g, "").slice(0, 6);
  const form = event.target.closest("form[data-form]");
  if (form) drafts[form.dataset.form] = event.target.value;
});

// Preview on input, commit once on release (or a keyboard change), never on every drag frame.
document.addEventListener("change", (event) => {
  const slider = event.target.closest("[data-home-brightness]");
  if (!slider || slider.disabled || Date.now() < ignoreTapsUntil) return;
  return Promise.resolve(actions["home-light-brightness"](slider)).catch((error) => toast(error.message));
});
document.addEventListener("submit", (event) => {
  event.preventDefault();
  if (Date.now() < ignoreTapsUntil) return;
  if (event.target.id === "pair-form") { if (!$("#pair-submit").disabled) pair(); return; }
  if (state && !pairing) submitList(event.target);
});
window.addEventListener("hashchange", () => showView(location.hash.slice(1), false));
window.addEventListener("online", scheduleRefresh);
window.addEventListener("pagehide", disposeSpeaker_hubCards);
window.addEventListener("offline", () => setOnline(false));
window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); installPrompt = event; if (currentView === "settings") render(); });
window.addEventListener("appinstalled", () => { installPrompt = null; if (currentView === "settings") render(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleRefresh(); });
document.addEventListener("fullscreenchange", () => { syncFullscreenButton(); if (currentView === "settings") render(); });

requestScreenWakeLock();
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") { requestScreenWakeLock(); keepScreenAwake(); }
});
window.addEventListener("pointerdown", () => {
  if (!screenWakeLock) requestScreenWakeLock();
}, { once: true, passive: true, capture: true });
// The front camera wakes a sleeping wall when someone walks up to it (see sampleCameraFrame).
startCameraWake();

try {
  const saved = JSON.parse(localStorage.getItem(CACHE_KEY) || "null");
  if (saved && saved.state && saved.state.now && saved.state.today && Array.isArray(saved.state.members)) {
    state = saved.state;
    receivedAt = Number(saved.receivedAt) || Date.now();
  }
} catch (error) { state = null; }
applyDisplay(resolveWallDisplay());   // scale the wall before its first view; a cached state may carry the server default
showView(location.hash.slice(1) || "chores", false);
setOnline(false);
refresh().catch((error) => {
  if (!state && !pairing) $("#content").innerHTML = empty("⌂", "We'll be right here", error.message);
});
setInterval(tick, 1000);
setInterval(() => {
  if (!pairing && (!online || !("EventSource" in window))) refresh().catch(() => {});
}, 15000);
setInterval(() => { if (!pairing) refresh().catch(() => {}); }, 900000);

// ── Dot: kitchen timers, fact card, typed input and voice. Dynamic imports so the wall never depends on them. ──
import("./kitchen.mjs").then((kitchen) => {
  const shared = { toast, chime, unlockAudio, getAudioContext: () => audioContext };
  kitchen.initKitchen(shared);
  window.fwKitchen = kitchen;
  window.__fwTimerStore = kitchen.timerStore();
  window.__fwShowFactCard = kitchen.showFactCard;
  window.__fwRunVoiceAction = kitchen.runVoiceAction;
  return import("./voice.mjs").then((voice) => {
    window.fwVoice = voice.initVoice(Object.assign({ applyVoiceReply: kitchen.applyVoiceReply, snapshotTimers: kitchen.snapshotTimers, setSpeaking: kitchen.setSpeaking }, shared));
  });
}).catch((error) => { console.warn("Dot modules unavailable", error); });
