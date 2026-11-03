// Unit tests for pcwall/static/display.mjs. Run with: node --test tests/test_display.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  STORAGE_KEY,
  PRESETS,
  autoFitScale,
  resolveDisplay,
  applyScale,
  saveDisplay,
  loadDisplay,
  deviceInfo,
  renderDisplayCard,
} from '../pcwall/static/display.mjs';

// Minimal DOM stand-in: enough of Element for renderDisplayCard.
class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.textContent = '';
    this.value = '';
    this.style = {
      props: {},
      setProperty(name, value) { this.props[name] = value; },
    };
  }
  set innerHTML(_html) { this.children = []; }
  get innerHTML() { return ''; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name]; }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn(); }
  querySelectorAll(pred) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) { if (pred(child)) out.push(child); walk(child); }
    };
    walk(this);
    return out;
  }
}
const fakeDoc = { createElement: (tag) => new FakeElement(fakeDoc, tag) };
fakeDoc.documentElement = new FakeElement(fakeDoc, 'html');

class FakeStorage {
  constructor() { this.data = {}; }
  getItem(key) { return key in this.data ? this.data[key] : null; }
  setItem(key, value) { this.data[key] = value; }
}

test('PRESETS has the four named scales', () => {
  assert.deepEqual(PRESETS, { standard_1080p: 1.0, tablet_15_6: 0.85, hd_720p: 0.75, glanceable: 1.15 });
});

test('autoFitScale fits 1920x1080 and rounds 720p to 0.67', () => {
  assert.equal(autoFitScale(1920, 1080), 1.0);
  assert.equal(autoFitScale(1280, 720), 0.67);
});

test('autoFitScale clamps to the 0.60 floor and 1.40 cap', () => {
  assert.equal(autoFitScale(960, 540), 0.6);
  assert.equal(autoFitScale(3840, 2160), 1.4);
});

test('resolveDisplay seeds from the query parameter when storage is empty', () => {
  const r = resolveDisplay({ query: { preset: 'hd_720p' }, stored: null, serverDefault: null });
  assert.deepEqual(r, { preset: 'hd_720p', scale: 0.75, source: 'query' });
});

test('resolveDisplay prefers stored settings over the query parameter', () => {
  const r = resolveDisplay({ query: { preset: 'hd_720p' }, stored: { preset: 'tablet_15_6' } });
  assert.equal(r.preset, 'tablet_15_6');
  assert.equal(r.scale, 0.85);
  assert.equal(r.source, 'stored');
});

test('resolveDisplay falls back to the server default, then standard_1080p', () => {
  const server = resolveDisplay({ query: null, stored: null, serverDefault: { preset: 'glanceable' } });
  assert.deepEqual(server, { preset: 'glanceable', scale: 1.15, source: 'server' });
  const none = resolveDisplay({});
  assert.deepEqual(none, { preset: 'standard_1080p', scale: 1.0, source: 'server' });
});

test('resolveDisplay computes auto preset from the viewport', () => {
  const r = resolveDisplay({ stored: { preset: 'auto' }, viewport: { width: 1280, height: 720 } });
  assert.deepEqual(r, { preset: 'auto', scale: 0.67, source: 'auto' });
  const custom = resolveDisplay({ stored: { scale: 1.1 } });
  assert.deepEqual(custom, { preset: 'custom', scale: 1.1, source: 'stored' });
});

test('applyScale sets --ui-scale and the root font-size', () => {
  const root = new FakeElement(fakeDoc, 'html');
  applyScale(0.85, { documentElement: root });
  assert.equal(root.style.props['--ui-scale'], '0.85');
  assert.equal(root.style.fontSize, 'calc(16px * 0.85)');
  assert.doesNotThrow(() => applyScale(1, null));
});

test('saveDisplay writes JSON under STORAGE_KEY and loadDisplay reads it back', () => {
  const storage = new FakeStorage();
  assert.equal(saveDisplay({ preset: 'hd_720p', scale: 0.75 }, storage), true);
  assert.equal(storage.data[STORAGE_KEY], '{"preset":"hd_720p","scale":0.75}');
  assert.deepEqual(loadDisplay(storage), { preset: 'hd_720p', scale: 0.75 });
});

test('saveDisplay swallows QuotaExceededError', () => {
  const full = {
    setItem() { throw new DOMException('quota', 'QuotaExceededError'); },
  };
  assert.doesNotThrow(() => saveDisplay({ preset: 'auto' }, full));
  assert.equal(saveDisplay({ preset: 'auto' }, full), false);
  assert.equal(saveDisplay({ preset: 'auto' }, null), false);
});

test('deviceInfo reads window dimensions with fallbacks', () => {
  assert.deepEqual(deviceInfo({ innerWidth: 1280, innerHeight: 720, devicePixelRatio: 2 }), { width: 1280, height: 720, dpr: 2 });
  assert.deepEqual(deviceInfo({}), { width: 1920, height: 1080, dpr: 1 });
  assert.deepEqual(deviceInfo(null), { width: 1920, height: 1080, dpr: 1 });
});

test('renderDisplayCard renders presets, slider, reset and device line, and wires onSave', () => {
  const container = new FakeElement(fakeDoc, 'div');
  const saved = [];
  const state = { preset: 'tablet_15_6', scale: 0.85, source: 'stored', device: { width: 1366, height: 768, dpr: 1 } };
  const card = renderDisplayCard(container, state, (choice) => saved.push(choice));

  const buttons = card.querySelectorAll((n) => n.tagName === 'BUTTON' && n.getAttribute('data-preset'));
  assert.deepEqual(buttons.map((b) => b.getAttribute('data-preset')), ['standard_1080p', 'tablet_15_6', 'hd_720p', 'auto']);
  assert.equal(buttons[1].getAttribute('aria-pressed'), 'true');

  const info = card.querySelectorAll((n) => n.getAttribute('data-role') === 'device-info')[0];
  assert.match(info.textContent, /1366×768 @ 1x/);

  buttons[3].dispatch('click');
  assert.deepEqual(saved.at(-1), { preset: 'auto', scale: 0.71 });

  const slider = card.querySelectorAll((n) => n.getAttribute('data-role') === 'scale-slider')[0];
  slider.value = '1.2';
  slider.dispatch('change');
  assert.deepEqual(saved.at(-1), { preset: 'custom', scale: 1.2 });

  card.querySelectorAll((n) => n.getAttribute('data-role') === 'reset')[0].dispatch('click');
  assert.deepEqual(saved.at(-1), { preset: 'standard_1080p', scale: 1.0 });
});
