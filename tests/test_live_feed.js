#!/usr/bin/env node
/* Live Feed batching regression checks without a browser dependency. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const appPath = path.join(__dirname, '..', 'src', 'simplenms', 'static', 'app.js');
const source = fs.readFileSync(appPath, 'utf8');
const close = source.lastIndexOf('})();');
assert.notEqual(close, -1, 'app.js closing wrapper not found');
const hooks = `
  window.__nmsTest = {
    state,
    fetchEvents,
    flushLiveEvents,
    queueLiveEvent,
    getLiveEvents: () => liveEvents,
    getPendingCount: () => pendingLiveEvents.length,
    setLiveEvents: events => { liveEvents = events; },
    setSnapshotId: id => { eventSnapshotId = id; },
  };
`;

function element() {
  return {
    classList: { add() {}, remove() {}, toggle() {} },
    style: {}, dataset: {}, value: '', textContent: '', innerHTML: '',
    addEventListener() {}, setAttribute() {}, querySelector() { return element(); },
  };
}

const elements = new Map();
const getElement = selector => {
  if (!elements.has(selector)) elements.set(selector, element());
  return elements.get(selector);
};
const document = {
  readyState: 'loading',
  documentElement: { dataset: {} },
  querySelector: getElement,
  querySelectorAll: () => [],
  createElement: element,
  addEventListener() {},
};
let timerId = 0;
const timers = new Map();
const window = {
  localStorage: { getItem: () => null, setItem() {} },
};
const context = {
  window,
  document,
  localStorage: window.localStorage,
  console,
  Date,
  Intl,
  JSON,
  URLSearchParams,
  isNaN,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
  clearTimeout(id) { timers.delete(id); },
  setInterval() { return 0; },
  clearInterval() {},
};
vm.runInNewContext(source.slice(0, close) + hooks + source.slice(close), context, { filename: appPath });
const live = window.__nmsTest;

function event(id) {
  return {
    id,
    ts: `2026-01-01T00:00:${String(id).padStart(3, '0')}`,
    type: 'syslog', src_ip: '10.0.0.1', payload: `event-${id}`,
  };
}

async function main() {
  for (let id = 1; id <= 201; id++) live.queueLiveEvent(event(id));
  assert.equal(live.getPendingCount(), 200, 'pending events must be capped at 200');
  live.flushLiveEvents();
  assert.equal(live.getLiveEvents().length, 200, 'rendered events must be capped at 200');
  assert.equal(live.state.totalEvents, 200, 'dropped pending event must not affect the visual total');

  live.setLiveEvents([event(5)]);
  live.setSnapshotId(5);
  live.state.totalEvents = 10;
  live.queueLiveEvent(event(5));
  live.queueLiveEvent(event(6));
  live.queueLiveEvent(event(6));
  live.flushLiveEvents();
  assert.equal(live.getLiveEvents().filter(item => item.id === 6).length, 1, 'SSE IDs must deduplicate');
  assert.equal(live.state.totalEvents, 11, 'only a post-snapshot event changes the visual total');

  const requests = [];
  context.fetch = () => new Promise(resolve => requests.push(resolve));
  const first = live.fetchEvents();
  const second = live.fetchEvents();
  requests[1]({ json: async () => ({ total: 2, total_pages: 1, snapshot_id: 2, events: [event(2)] }) });
  await Promise.resolve();
  await Promise.resolve();
  requests[0]({ json: async () => ({ total: 1, total_pages: 1, snapshot_id: 1, events: [event(1)] }) });
  await Promise.all([first, second]);
  assert.equal(live.getLiveEvents()[0].id, 2, 'stale REST responses must not overwrite the latest feed');

  console.log('live feed batching checks passed');
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
