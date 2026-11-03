// display.mjs: UI scaling presets, auto-fit, persistence and the display settings card.
//
// Pure functions take their environment (document, localStorage, window) as
// parameters with browser defaults, so the module runs under Node for tests.

export const STORAGE_KEY = 'pcwall_display';

export const PRESETS = {
  standard_1080p: 1.0,
  tablet_15_6: 0.85,
  hd_720p: 0.75,
  glanceable: 1.15,
};

export const DEFAULT_PRESET = 'standard_1080p';
export const MIN_SCALE = 0.6;
export const MAX_SCALE = 1.4;

const PRESET_BUTTONS = [
  { preset: 'standard_1080p', label: '1080p' },
  { preset: 'tablet_15_6', label: '15.6" Tablet' },
  { preset: 'hd_720p', label: '720p' },
  { preset: 'auto', label: 'Auto-Fit' },
];

export function autoFitScale(width, height) {
  const w = Number(width) > 0 ? Number(width) : 1920;
  const h = Number(height) > 0 ? Number(height) : 1080;
  const raw = Math.min(w / 1920, h / 1080);
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(raw.toFixed(2))));
}

function hasScale(choice) {
  return choice.scale !== undefined && choice.scale !== null && choice.scale !== ''
    && Number.isFinite(Number(choice.scale));
}

function hasChoice(choice) {
  return Boolean(choice && typeof choice === 'object' && (choice.preset || hasScale(choice)));
}

function scaleFromChoice(choice) {
  if (hasScale(choice)) {
    const explicit = Number(choice.scale);
    return Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(explicit.toFixed(2))));
  }
  if (choice.preset && Object.prototype.hasOwnProperty.call(PRESETS, choice.preset)) {
    return PRESETS[choice.preset];
  }
  return PRESETS[DEFAULT_PRESET];
}

function presetFromChoice(choice) {
  if (choice.preset) return String(choice.preset);
  return 'custom';
}

export function resolveDisplay({ query, stored, serverDefault, viewport = {} } = {}) {
  let choice;
  let source;
  if (hasChoice(stored)) {
    choice = stored;
    source = 'stored';
  } else if (hasChoice(query)) {
    choice = query;
    source = 'query';
  } else if (hasChoice(serverDefault)) {
    choice = serverDefault;
    source = 'server';
  } else if (typeof serverDefault === 'string' && serverDefault) {
    choice = { preset: serverDefault };
    source = 'server';
  } else {
    choice = { preset: DEFAULT_PRESET };
    source = 'server';
  }

  const preset = presetFromChoice(choice);
  if (preset === 'auto') {
    const scale = autoFitScale(viewport.width || 1920, viewport.height || 1080);
    return { preset, scale, source: 'auto' };
  }
  return { preset, scale: scaleFromChoice(choice), source };
}

export function applyScale(scale, doc = (typeof document !== 'undefined' ? document : null)) {
  if (!doc || !doc.documentElement) return;
  const root = doc.documentElement;
  root.style.setProperty('--ui-scale', String(scale));
  root.style.fontSize = `calc(16px * ${scale})`;
}

export function saveDisplay(choice, storage = (typeof localStorage !== 'undefined' ? localStorage : null)) {
  if (!storage) return false;
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(choice));
    return true;
  } catch (e) {
    // QuotaExceededError or private-mode storage: the wall keeps working unsaved.
    return false;
  }
}

export function loadDisplay(storage = (typeof localStorage !== 'undefined' ? localStorage : null)) {
  if (!storage) return null;
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (e) {
    return null;
  }
}

export function deviceInfo(win = (typeof window !== 'undefined' ? window : null)) {
  if (!win) return { width: 1920, height: 1080, dpr: 1 };
  return {
    width: win.innerWidth || 1920,
    height: win.innerHeight || 1080,
    dpr: win.devicePixelRatio || 1,
  };
}

function el(doc, tag, attrs = {}, text = '') {
  const node = doc.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'className') node.className = value;
    else node.setAttribute(key, String(value));
  }
  if (text) node.textContent = text;
  return node;
}

export function renderDisplayCard(container, state = {}, onSave = () => {}) {
  if (!container) return null;
  const doc = container.ownerDocument || (typeof document !== 'undefined' ? document : null);
  if (!doc) return null;

  const current = {
    preset: state.preset || DEFAULT_PRESET,
    scale: Number.isFinite(Number(state.scale)) ? Number(state.scale) : PRESETS[DEFAULT_PRESET],
    source: state.source || 'server',
  };
  const device = state.device || deviceInfo();

  container.innerHTML = '';
  const card = el(doc, 'section', { className: 'display-card', 'data-preset': current.preset });
  card.appendChild(el(doc, 'h2', { className: 'display-card__title' }, 'Display'));

  const presets = el(doc, 'div', { className: 'display-card__presets', role: 'group', 'aria-label': 'Display presets' });
  for (const { preset, label } of PRESET_BUTTONS) {
    const active = preset === current.preset;
    const button = el(doc, 'button', {
      type: 'button',
      className: active ? 'preset-btn is-active' : 'preset-btn',
      'data-preset': preset,
      'aria-pressed': active ? 'true' : 'false',
    }, label);
    button.addEventListener('click', () => {
      const scale = preset === 'auto' ? autoFitScale(device.width, device.height) : PRESETS[preset];
      onSave({ preset, scale });
    });
    presets.appendChild(button);
  }
  card.appendChild(presets);

  const sliderRow = el(doc, 'label', { className: 'display-card__slider' });
  sliderRow.appendChild(el(doc, 'span', {}, 'Scale'));
  const slider = el(doc, 'input', {
    type: 'range',
    min: String(MIN_SCALE),
    max: String(MAX_SCALE),
    step: '0.05',
    value: String(current.scale),
    'data-role': 'scale-slider',
    'aria-label': 'UI scale',
  });
  slider.value = String(current.scale);
  const readout = el(doc, 'output', { 'data-role': 'scale-readout' }, `${Math.round(current.scale * 100)}%`);
  slider.addEventListener('input', () => {
    readout.textContent = `${Math.round(Number(slider.value) * 100)}%`;
  });
  slider.addEventListener('change', () => {
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, Number(Number(slider.value).toFixed(2))));
    onSave({ preset: 'custom', scale });
  });
  sliderRow.appendChild(slider);
  sliderRow.appendChild(readout);
  card.appendChild(sliderRow);

  const reset = el(doc, 'button', { type: 'button', className: 'display-card__reset', 'data-role': 'reset' }, 'Reset to 1080p');
  reset.addEventListener('click', () => {
    onSave({ preset: DEFAULT_PRESET, scale: PRESETS[DEFAULT_PRESET] });
  });
  card.appendChild(reset);

  const info = `${device.width}×${device.height} @ ${device.dpr}x · scale ${current.scale.toFixed(2)} (${current.source})`;
  card.appendChild(el(doc, 'p', { className: 'display-card__device', 'data-role': 'device-info' }, info));

  container.appendChild(card);
  return card;
}
