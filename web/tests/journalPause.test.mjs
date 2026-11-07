import assert from 'node:assert/strict';
import test from 'node:test';
import { pausedView } from '../src/journalPause.js';

const events = (...seqs) => seqs.map((seq) => ({ seq, kind: 'x' }));

test('without a pause the list shows the live events', () => {
  const live = events(1, 2, 3);
  assert.deepEqual(pausedView(live, null), { events: live, pending: 0 });
});

test('a paused list keeps the events it had and counts the ones that came since', () => {
  const frozen = events(1, 2, 3);
  const live = events(1, 2, 3, 4, 5);
  assert.deepEqual(pausedView(live, frozen), { events: frozen, pending: 2 });
});

test('the live stream may drop old events past its cap: the count is of the new ones only', () => {
  const frozen = events(1, 2, 3);
  const live = events(3, 4, 5, 6);
  assert.equal(pausedView(live, frozen).pending, 3);
});

test('a pause on an empty list counts every event that came', () => {
  assert.equal(pausedView(events(1, 2), []).pending, 2);
});
