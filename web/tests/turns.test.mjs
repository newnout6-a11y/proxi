import assert from 'node:assert/strict';
import test from 'node:test';
import { STALE_AFTER, buildTraces, costOf, phasesOf, scaleOf, statusAt } from '../src/turns.js';
import { MIN_FORECAST_SECONDS, burnRate, coverageOf, coveredSeconds, fmtSince, fmtSpan, uncoveredBuckets } from '../src/coverage.js';

const ev = (ts, kind, data = {}) => ({ ts, kind, data, turn_id: 'test-turn' });
const finish = (ts, duration, reason = 'stop') => ev(ts, 'turn.end', { reason, duration_ms: duration });
const relayStart = (ts) => ({ ts, kind: 'relay.start', data: {} });
const forTurn = (id, events) => events.map((event) => ({ ...event, turn_id: id }));

test('unknown credits remain unknown and explicit zero remains known', () => {
  const [unknown] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: null }), finish(102, 2000)]);
  assert.equal(unknown.credits, null);
  assert.equal(unknown.creditsComplete, false);
  const [zero] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: 0 }), finish(102, 2000)]);
  assert.equal(zero.credits, 0);
  assert.equal(zero.creditsComplete, true);
});

test('a known debit and an unknown attempt are displayed as a lower bound', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: null, attempt: 1 }),
    ev(102, 'upstream.usage', { credits: 3.5, attempt: 2 }), finish(103, 3000)]);
  assert.equal(trace.credits, 3.5);
  assert.equal(trace.creditsComplete, false);
});

test('silent ongoing thinking grows with the clock without claiming output', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_start')]);
  assert.equal(phasesOf(trace, 301).think, 200000);
  assert.equal(phasesOf(trace, 301).out, 0);
  assert.equal(phasesOf(trace, 301).total, 201000);
  assert.equal(phasesOf(trace, 302).think, 201000);
});

test('late first token does not double count silent thinking as waiting', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_start'),
    ev(300, 'upstream.first_token', { ttft_ms: 200000, first: 'thinking' })]);
  const phases = phasesOf(trace, 301);
  assert.equal(phases.wait, 1000);
  assert.equal(phases.think, 200000);
  assert.equal(phases.total, 201000);
});

test('finished traces stop ticking and unclassified time is not labelled output', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(105, 'turn.end', { reason: 'stop', duration_ms: 5000 })]);
  assert.deepEqual(phasesOf(trace, 999), phasesOf(trace, 1000));
  assert.equal(phasesOf(trace, 999).out, 0);
});

for (const reverse of [false, true]) {
  test(`token snapshots do not create extra unknown charges (${reverse ? 'before' : 'after'} cost)`, () => {
    const usage = [{ credits: 1.5, attempt: 1 }, { credits: null, attempt: 1 }];
    if (reverse) usage.reverse();
    const [trace] = buildTraces([ev(100, 'turn.begin'), ...usage.map((data, i) => ev(101 + i, 'upstream.usage', data)), finish(103, 3000)]);
    assert.equal(trace.credits, 1.5);
    assert.equal(trace.creditsComplete, true);
  });
}

test('time between thinking and output stays unclassified', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.thinking_start'),
    ev(103, 'upstream.thinking_end', { thinking_ms: 3000 }), ev(110, 'upstream.text_start'),
    ev(111, 'turn.end', { reason: 'stop', duration_ms: 11000 })]);
  const phases = phasesOf(trace);
  assert.equal(phases.out, 1000);
  assert.equal(phases.other, 7000);
  assert.equal(phases.total, 11000);
});

test('missing boundaries never inflate elapsed duration', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_end', { thinking_ms: 1000 }),
    ev(102, 'tool.call', { args_ms: 1000 }), ev(104, 'turn.end', { reason: 'stop', duration_ms: 4000 })]);
  const p = phasesOf(trace);
  assert.equal(p.total, 4000);
  assert.equal(p.wait + p.think + p.tools + p.out + p.other, 4000);
});

test('a retry closes old thinking and waits for its own upstream signal', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(101, 'upstream.thinking_start'), ev(103, 'upstream.usage_start', { attempt: 2 })]);
  assert.equal(trace.phase, 'wait');
  assert.equal(phasesOf(trace, 203).think, 2000);
  assert.equal(phasesOf(trace, 204).think, 2000);
});

test('separate text segments close when thinking begins', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.text_start'),
    ev(102, 'upstream.thinking_start'), ev(103, 'upstream.thinking_end', { thinking_ms: 1000 }),
    ev(110, 'turn.end', { reason: 'stop', duration_ms: 10000 })]);
  assert.equal(phasesOf(trace).out, 1000);
  assert.equal(phasesOf(trace).other, 7000);
});

test('tool generation closes text at tool_start instead of its eventual call', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.text_start'),
    ev(102, 'upstream.tool_start'), ev(110, 'tool.call', { args_ms: 8000 }),
    ev(111, 'turn.end', { reason: 'stop', duration_ms: 11000 })]);
  assert.equal(phasesOf(trace).out, 1000);
  assert.equal(phasesOf(trace).other, 1000);
  assert.equal(phasesOf(trace).tools, 8000);
});

test('a turn that failed before reaching upstream costs nothing', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), { ...ev(101, 'turn.error'), level: 'error' }, finish(101, 1000, 'error')]);
  assert.equal(trace.credits, 0);
  assert.equal(trace.creditsComplete, true);
});

// The price rules of the relay's journal (tests/test_journal_costs.py on fix/pr24-wp03-usage, SCENARIOS and the turn
// traces), seen from one turn: [name, steps, credits, unknown attempts, cost is complete]. A turn that was never
// started with usage_start has no attempt: the first attempt is the one the journal opens, never an implicit 0.
const START = (n) => ['upstream.usage_start', { attempt: n }];
const USAGE = (n, credits, extra = {}) => ['upstream.usage', { attempt: n, credits, ...extra }];
const MARKER = { credits_reported: false };
const BEGIN = () => ['turn.begin', {}];
const END = (reason = 'stop') => ['turn.end', { reason, duration_ms: 1000 }];
const turnOf = (steps) => buildTraces([ev(99, 'turn.begin'), ...steps.map(([kind, data], i) => ev(100 + i, kind, data))])[0];
const COSTS = [
  ['normal', [START(1), USAGE(1, 0.01), END()], 0.01, 0, true],
  ['rejected before the upstream (413/402/503)', [END('error')], 0, 0, true],
  ['an attempt that was refused costs nothing (429/500)', [START(1), END('error')], 0, 0, true],
  ['an attempt that reported usage without a price is unknown', [START(1), USAGE(1, null, MARKER), END('error')], null, 1, false],
  ['exec step: two priced attempts', [START(1), USAGE(1, 0.01), START(2), USAGE(2, 0.02), END()], 0.03, 0, true],
  ['second attempt never reported anything', [START(1), USAGE(1, 0.01), START(2), END()], 0.01, 0, true],
  ['second attempt reported usage without a price', [START(1), USAGE(1, 0.01), START(2), USAGE(2, null, MARKER), END()], 0.01, 1, false],
  ['two turns sharing one key', [START(1), USAGE(1, 0.01), BEGIN(), START(1), USAGE(1, 0.02), END()], 0.03, 0, true],
  ['tokens-only update after the price', [START(1), USAGE(1, 0.01), USAGE(1, null, { credits_reported: true }), END()], 0.01, 0, true],
  ['price after a tokens-only snapshot', [START(1), USAGE(1, null), USAGE(1, 0.25), END()], 0.25, 0, true],
  ['a reported zero is a price', [START(1), USAGE(1, 0), END()], 0, 0, true],
  ['usage without an attempt number belongs to the last started attempt', [START(2), ['upstream.usage', { credits: 0.5 }], END()], 0.5, 0, true],
  ['usage before any attempt was started opens attempt 0', [['upstream.usage', { credits: 0.5 }], END()], 0.5, 0, true],
  ['usage without a number or a price before any attempt is unknown', [['upstream.usage', { credits: null }], END()], null, 1, false],
  ['cancelled while the attempt was silent: may have been billed', [START(1), END('cancelled')], null, 1, false],
  ['cancelled before any attempt started', [END('cancelled')], 0, 0, true],
  ['graceful cancel: the marker and the cancelled end count once', [START(1), USAGE(1, null, MARKER), END('cancelled')], null, 1, false],
  ['cancelled second attempt after a priced first one', [START(1), USAGE(1, 0.01), START(2), END('cancelled')], 0.01, 1, false],
  ['cancelled after the price arrived', [START(1), USAGE(1, 0.01), END('cancelled')], 0.01, 0, true],
  ['a silent last attempt of any other ending is not a missing price', [START(1), USAGE(1, 0.01), START(2), END('timeout')], 0.01, 0, true],
  ['an invalid price after a valid one changes nothing', [START(1), USAGE(1, 0.1), USAGE(1, -4, { credits_reported: true }), START(2), USAGE(2, 0.2), END()], 0.3, 0, true],
];
for (const [name, steps, credits, unknown, complete] of COSTS) {
  test(`cost of a finished turn: ${name}`, () => {
    const trace = turnOf(steps);
    const cost = costOf(trace);
    assert.equal(cost.running, false);
    assert.equal(cost.credits === null ? null : Math.round(cost.credits * 1e6) / 1e6, credits);
    assert.equal(cost.unknown, unknown);
    assert.equal(cost.complete, complete);
    assert.equal(trace.credits, cost.credits);
    assert.equal(trace.creditsComplete, complete);
  });
}

test('a turn that never reached upstream has no attempt, so it is neither pending nor unknown', () => {
  const trace = turnOf([END('error')]);
  assert.equal(trace.costAttempts.size, 0);
  assert.equal(costOf(trace).credits, 0);
});

// The relay marks the attempt a client cancelled while it was silent at the moment of that end (journal._Turn.end), once
// and for good: what arrives later neither repeats the mark nor undoes it. Found by replaying random event sequences
// through the relay's own journal.turns() and comparing every turn.
const AFTER_END = [
  ['a cancelled end before any attempt marks nothing, a later attempt is only pending', [END('cancelled'), START(3)], 0, 0, true],
  ['an attempt started after a cancelled end is pending, the earlier silent one is the gap', [START(1), END('cancelled'), START(2)], null, 1, false],
  ['every cancelled end marks the attempt that was silent then', [START(1), USAGE(1, 0.25), START(2), END('cancelled'), START(3), END('cancelled')], 0.25, 2, false],
  ['a price that arrives after the cancelled end makes the attempt known again', [START(1), END('cancelled'), USAGE(1, 0.25)], 0.25, 0, true],
  ['a marker that arrives after the cancelled end does not count the attempt twice', [START(1), END('cancelled'), USAGE(1, null, MARKER)], null, 1, false],
  ['a cancelled end after the attempt reported marks nothing', [START(1), USAGE(1, 0.01), END('timeout'), END('cancelled')], 0.01, 0, true],
  ['usage for another attempt does not clear the mark of the silent current one', [START(1), START(2), END('cancelled'), USAGE(1, 0.5)], 0.5, 1, false],
];
for (const [name, steps, credits, unknown, complete] of AFTER_END) {
  test(`cost when events follow the end: ${name}`, () => {
    const cost = costOf(turnOf(steps));
    assert.equal(cost.credits === null ? null : Math.round(cost.credits * 1e6) / 1e6, credits);
    assert.equal(cost.unknown, unknown);
    assert.equal(cost.complete, complete);
  });
}

test('a running turn is never complete: nothing reported yet has no price, a price so far is a lower bound', () => {
  assert.deepEqual(costOf(turnOf([])), { credits: null, unknown: 0, running: true, complete: false });
  assert.deepEqual(costOf(turnOf([START(1)])), { credits: null, unknown: 0, running: true, complete: false });
  assert.deepEqual(costOf(turnOf([START(1), USAGE(1, 0.1)])), { credits: 0.1, unknown: 0, running: true, complete: false });
  assert.deepEqual(costOf(turnOf([START(1), USAGE(1, 0.1), START(2)])), { credits: 0.1, unknown: 0, running: true, complete: false });
  // An attempt that reported usage without a price is a gap even before the turn ends; the relay counts it too
  assert.deepEqual(costOf(turnOf([START(1), USAGE(1, null, MARKER)])), { credits: null, unknown: 1, running: true, complete: false });
});

test('a pending attempt is not unknown until it reports something', () => {
  const pending = turnOf([START(1), USAGE(1, 0.2), START(2)]);
  assert.equal(costOf(pending).unknown, 0);
  const observed = turnOf([START(1), USAGE(1, 0.2), START(2), USAGE(2, null, MARKER)]);
  assert.equal(costOf(observed).unknown, 1);
  assert.equal(costOf(observed).credits, 0.2);
});

test('a price that is not a finite non-negative number is invalid, even when it claims to be reported', () => {
  for (const credits of [-1, -0.5, NaN, Infinity, -Infinity, true, '0.5', [0.5]]) {
    for (const reported of [true, undefined]) {
      const cost = costOf(turnOf([START(1), USAGE(1, credits, { credits_reported: reported }), END()]));
      assert.equal(cost.credits, null);
      assert.equal(cost.unknown, 1);
      assert.equal(cost.complete, false);
    }
  }
});

test('the reported flag alone never creates a price but keeps an earlier one', () => {
  assert.equal(costOf(turnOf([START(1), USAGE(1, null, { credits_reported: true }), END()])).unknown, 1);
  const kept = costOf(turnOf([START(1), USAGE(1, 0.1), USAGE(1, -1, { credits_reported: true }),
    USAGE(1, NaN, { credits_reported: true }), END()]));
  assert.equal(kept.credits, 0.1);
  assert.equal(kept.unknown, 0);
});

test('a turn whose end the relay never recorded is not complete and has no final price', () => {
  // The relay turns a silent attempt into a missing price only on a cancelled end, so a lost turn is no gap by itself
  assert.deepEqual(costOf(turnOf([]), 'interrupted'), { credits: null, unknown: 0, running: false, complete: false });
  assert.deepEqual(costOf(turnOf([START(1)]), 'interrupted'), { credits: null, unknown: 0, running: false, complete: false });
  assert.deepEqual(costOf(turnOf([START(1), USAGE(1, 0.25)]), 'interrupted'), { credits: 0.25, unknown: 0, running: false, complete: false });
  assert.deepEqual(costOf(turnOf([START(1), USAGE(1, null, MARKER)]), 'interrupted'), { credits: null, unknown: 1, running: false, complete: false });
});

test('a turn from before a relay restart is interrupted and ends at its last event', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_start')].map((event) => ({ ...event, restored: true })));
  assert.equal(trace.status, 'interrupted');
  const later = phasesOf(trace, 101 + 3 * 86400);
  assert.equal(later.open, false);
  assert.equal(later.status, 'interrupted');
  assert.equal(later.total, 1000);
  assert.deepEqual(phasesOf(trace, 101 + 4 * 86400), later);
});

test('a relay.start after a running turn interrupts it; a turn begun after the restart keeps running', () => {
  const [next, lost] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_start'), relayStart(500),
    ...forTurn('next-turn', [ev(501, 'turn.begin')])]);
  assert.equal(lost.status, 'interrupted');
  assert.equal(phasesOf(lost, 1e9).total, 1000);
  assert.equal(next.status, 'running');
  assert.equal(phasesOf(next, 502).open, true);
});

test('a relay restart does not change a turn that had already ended', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), finish(102, 2000, 'cancelled'), relayStart(500)]);
  assert.equal(trace.status, 'cancelled');
});

test('a running turn silent past the stale limit is interrupted by the clock and stops at its last event', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.thinking_start')]);
  assert.equal(statusAt(trace, 101 + STALE_AFTER - 1), 'running');
  assert.equal(phasesOf(trace, 101 + STALE_AFTER - 1).open, true);
  assert.equal(statusAt(trace, 101 + STALE_AFTER + 1), 'interrupted');
  const late = phasesOf(trace, 101 + STALE_AFTER + 1);
  assert.equal(late.open, false);
  assert.equal(late.total, 1000);
  assert.deepEqual(phasesOf(trace, 101 + 3 * 86400), late);
  // The relay never recorded its end: it is not a complete cost, and no price was reported
  assert.deepEqual(costOf(trace, statusAt(trace, 101 + STALE_AFTER + 1)), { credits: null, unknown: 0, running: false, complete: false });
});

test('an interrupted turn has no final price: it never ended, so even a silent attempt is no proven gap', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }), relayStart(500)]);
  assert.equal(trace.status, 'interrupted');
  assert.equal(trace.credits, null);
  assert.equal(trace.creditsComplete, false);
  const [priced] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(101, 'upstream.usage', { credits: 0.25, attempt: 1 }), relayStart(500)]);
  assert.equal(priced.status, 'interrupted');
  assert.equal(priced.credits, 0.25);
  assert.equal(priced.creditsComplete, false);
});

test('the shared scale is the longest finished turn: a running one never stretches it', () => {
  const traces = buildTraces([
    ...forTurn('done', [ev(100, 'turn.begin'), finish(105, 5000)]),
    ...forTurn('live', [ev(110, 'turn.begin'), ev(111, 'upstream.thinking_start')]),
  ]);
  assert.equal(traces[0].id, 'live');
  assert.equal(scaleOf(traces, 111 + 600), 5000);
  assert.equal(scaleOf(traces, 111 + STALE_AFTER + 1), 5000);
  assert.equal(scaleOf([traces[0]], 111 + 600), 1);
});

test('time spent in a local command is not counted as output', () => {
  const sequence = [ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }), ev(105, 'upstream.text_start'),
    ev(107, 'exec.start'), ev(137, 'exec.result', { duration_ms: 30000, exit: 0 })];
  const [live] = buildTraces(sequence.slice(0, 4));
  assert.equal(live.phase, 'exec');
  const running = phasesOf(live, 137);
  assert.equal(running.out, 2000);
  assert.equal(running.exec, 30000);
  const [trace] = buildTraces([...sequence, ev(137, 'upstream.usage_start', { attempt: 2 }), finish(140, 40000)]);
  const p = phasesOf(trace);
  assert.equal(p.out, 2000);
  assert.equal(p.exec, 30000);
  assert.equal(p.other, 30000);
  assert.equal(p.wait, 8000);
  assert.equal(p.total, 40000);
  assert.equal(trace.commands, 1);
});

test('a command also closes open thinking and tool arguments', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.thinking_start'), ev(102, 'exec.start'),
    ev(132, 'exec.result', { duration_ms: 30000 }), ev(133, 'upstream.tool_start', { call_id: 'a' }), ev(134, 'exec.start'),
    ev(164, 'exec.result', { duration_ms: 30000 }), finish(165, 65000)]);
  const p = phasesOf(trace);
  assert.equal(p.think, 2000);
  assert.equal(p.tools, 1000);
  assert.equal(p.exec, 60000);
  assert.equal(p.total, 65000);
});

test('parallel tool calls count the time they overlap once', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'),
    ev(101, 'upstream.tool_start', { call_id: 'a', name: 'Read' }), ev(102, 'upstream.tool_start', { call_id: 'b', name: 'Read' }),
    ev(103, 'tool.call', { call_id: 'a', name: 'Read', args_ms: 2000 }), ev(104, 'tool.call', { call_id: 'b', name: 'Read', args_ms: 2000 }),
    finish(105, 5000, 'tool_calls')]);
  assert.equal(trace.tools, 2);
  assert.equal(trace.args, 3000);
  assert.equal(phasesOf(trace).tools, 3000);
});

test('sequential tool calls add up, and a call still open overlaps the closed ones only once', () => {
  const sequence = [ev(100, 'turn.begin'),
    ev(101, 'upstream.tool_start', { call_id: 'a' }), ev(102, 'tool.call', { call_id: 'a', args_ms: 1000 }),
    ev(103, 'upstream.tool_start', { call_id: 'b' }), ev(105, 'tool.call', { call_id: 'b', args_ms: 2000 })];
  const [done] = buildTraces(sequence);
  assert.equal(done.args, 3000);
  const [open] = buildTraces([...sequence, ev(106, 'upstream.tool_start', { call_id: 'c' })]);
  assert.equal(phasesOf(open, 108).tools, 5000);
});

test('every attempt waits for its own first signal', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(101, 'upstream.thinking_start'), ev(103, 'upstream.thinking_end', { thinking_ms: 2000 }),
    ev(104, 'upstream.usage_start', { attempt: 2 }), ev(112, 'upstream.first_token', { ttft_ms: 8000, first: 'text' }),
    ev(112, 'upstream.text_start'), finish(114, 14000)]);
  const p = phasesOf(trace);
  assert.equal(p.wait, 9000);
  assert.equal(p.think, 2000);
  assert.equal(p.out, 2000);
  assert.equal(p.other, 1000);
  assert.equal(p.total, 14000);
});

test('the wait of a retry grows with the clock until its first signal', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(101, 'upstream.thinking_start'), ev(103, 'upstream.thinking_end', { thinking_ms: 2000 }),
    ev(104, 'upstream.usage_start', { attempt: 2 })]);
  assert.equal(phasesOf(trace, 114).wait, 11000);
  assert.equal(phasesOf(trace, 114).think, 2000);
});

test('an attempt that gets no signal at all waited until the next one began', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(110, 'upstream.usage_start', { attempt: 2 }), ev(112, 'upstream.text_start'), finish(113, 13000)]);
  const p = phasesOf(trace);
  assert.equal(p.wait, 12000);
  assert.equal(p.out, 1000);
  assert.equal(p.other, 0);
});

test('only an explicit complete: false is reported as a gap in the metrics window', () => {
  assert.equal(coverageOf(undefined, 3600), null);
  assert.equal(coverageOf({}, 3600), null);
  assert.equal(coverageOf({ coverage: { complete: true, covered_from: 1000 } }, 3600), null);
  for (const complete of [undefined, null, 0, 'false', 'no']) {
    assert.equal(coverageOf({ coverage: { complete, covered_from: 1000 } }, 3600), null);
  }
  assert.deepEqual(coverageOf({ coverage: { complete: false, covered_from: 1000, covered_seconds: 600 } }, 3600),
    { from: 1000, seconds: 600 });
});

test('the coverage start falls back to the oldest event; invalid numbers are ignored and the span stays inside the window', () => {
  assert.equal(coverageOf({ coverage: { complete: false, oldest_event_ts: 900 } }, 3600).from, 900);
  assert.equal(coverageOf({ coverage: { complete: false, covered_from: '1000', oldest_event_ts: NaN } }, 3600).from, null);
  // The raw buffer's oldest event says nothing about what the aggregates cover
  assert.equal(coverageOf({ coverage: { complete: false, buffer_oldest_ts: 500 } }, 3600).from, null);
  const seconds = (covered_seconds) => coverageOf({ coverage: { complete: false, covered_seconds } }, 3600).seconds;
  assert.equal(seconds(-5), 0);
  assert.equal(seconds(99999), 3600);
  assert.equal(seconds('600'), null);
  assert.equal(seconds(undefined), null);
});

test('the covered seconds are what the relay reports within the window; only an explicit gap shortens the window', () => {
  assert.equal(coveredSeconds(undefined, 3600), 3600);
  assert.equal(coveredSeconds({}, 3600), 3600);
  assert.equal(coveredSeconds({ coverage: { complete: true, covered_seconds: 3600 } }, 3600), 3600);
  // Without an explicit complete: false the figure the relay sent is not a gap, whatever else it says
  assert.equal(coveredSeconds({ coverage: { covered_seconds: 100 } }, 3600), 3600);
  assert.equal(coveredSeconds({ coverage: { complete: true, covered_seconds: 100 } }, 3600), 3600);
  assert.equal(coveredSeconds({ coverage: { complete: false, covered_seconds: 600 } }, 3600), 600);
  assert.equal(coveredSeconds({ coverage: { complete: false, covered_seconds: 99999 } }, 3600), 3600);
  assert.equal(coveredSeconds({ coverage: { complete: false, covered_seconds: -4 } }, 3600), 0);
  assert.equal(coveredSeconds({ coverage: { complete: false } }, 3600), null);
  assert.equal(coveredSeconds({ coverage: { complete: false, covered_seconds: '600' } }, 3600), null);
});

const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} is not ${expected}`);
const rateOf = (credits, coverage, windowSec = 3600) => burnRate({ totals: { credits }, coverage }, windowSec);

test('the burn rate is credits * 86400 / min(window, covered seconds): the spend of the covered part, per day', () => {
  near(rateOf(3.6, undefined).perDay, 86.4);
  near(rateOf(3.6, { complete: true, covered_seconds: 3600 }).perDay, 86.4);
  // Half of the window is covered: the same spend is twice the rate, not half of it
  near(rateOf(3.6, { complete: false, covered_seconds: 1800 }).perDay, 172.8);
  assert.equal(rateOf(3.6, { complete: false, covered_seconds: 1800 }).seconds, 1800);
  // A longer covered stretch than the window is cut to the window, a shorter window to its own length
  assert.equal(rateOf(3.6, { complete: false, covered_seconds: 21600 }, 3600).seconds, 3600);
  assert.equal(rateOf(3.6, { complete: false, covered_seconds: 21600 }, 86400).seconds, 21600);
  near(rateOf(36, { complete: false, covered_seconds: 21600 }, 86400).perDay, 144);
});

test('no burn rate is told from a stretch shorter than the minimum, an unknown one, or no spend', () => {
  assert.equal(MIN_FORECAST_SECONDS, 300);
  const short = rateOf(3.6, { complete: false, covered_seconds: MIN_FORECAST_SECONDS - 1 });
  assert.deepEqual(short, { seconds: MIN_FORECAST_SECONDS - 1, short: true, perDay: null });
  const enough = rateOf(3.6, { complete: false, covered_seconds: MIN_FORECAST_SECONDS });
  assert.equal(enough.short, false);
  near(enough.perDay, 1036.8);
  assert.deepEqual(rateOf(3.6, { complete: false }), { seconds: null, short: false, perDay: null });
  for (const credits of [0, -1, NaN, Infinity, undefined, null, '3.6']) assert.equal(rateOf(credits, undefined).perDay, null);
  assert.equal(burnRate(null, 3600).perDay, null);
  assert.equal(burnRate(undefined, 3600).seconds, 3600);
});

test('buckets that end before the coverage begins are no data; the newest bucket always is data', () => {
  const buckets = Array.from({ length: 6 }, (_, i) => ({ t: 1000 + i * 60 }));
  assert.equal(uncoveredBuckets(buckets, 360, null), 0);
  assert.equal(uncoveredBuckets(buckets, 360, 1000), 0);
  assert.equal(uncoveredBuckets(buckets, 360, 1059), 0);
  assert.equal(uncoveredBuckets(buckets, 360, 1060), 1);
  assert.equal(uncoveredBuckets(buckets, 360, 1200), 3);
  assert.equal(uncoveredBuckets(buckets, 360, 99999), 5);
  assert.equal(uncoveredBuckets([{ t: 1000 }], 60, 99999), 0);
  assert.equal(uncoveredBuckets(buckets, 0, 1200), 0);
  assert.equal(uncoveredBuckets([{}, {}], 120, 1200), 0);
  assert.equal(uncoveredBuckets([], 3600, 1200), 0);
});

test('a time span and a moment are written for people', () => {
  assert.equal(fmtSpan(40), '40\u00a0с');
  assert.equal(fmtSpan(600), '10\u00a0мин');
  assert.equal(fmtSpan(5400), '90\u00a0мин');
  assert.equal(fmtSpan(7200), '2\u00a0ч');
  assert.equal(fmtSpan(9000), '2,5\u00a0ч');
  assert.equal(fmtSpan(NaN), '—');
  const now = new Date(2026, 10, 5, 12, 0, 0).getTime() / 1000;
  assert.match(fmtSince(now - 600, now), /^\d{2}:\d{2}$/);
  assert.match(fmtSince(now - 3 * 86400, now), /^\d{1,2} [а-яё]+, \d{2}:\d{2}$/);
  assert.equal(fmtSince(undefined, now), '—');
});
