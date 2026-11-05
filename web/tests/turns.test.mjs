import assert from 'node:assert/strict';
import test from 'node:test';
import { STALE_AFTER, buildTraces, costOf, phasesOf, scaleOf, statusAt } from '../src/turns.js';

const ev = (ts, kind, data = {}) => ({ ts, kind, data, turn_id: 'test-turn' });
const finish = (ts, duration, reason = 'stop') => ev(ts, 'turn.end', { reason, duration_ms: duration });
const relayStart = (ts) => ({ ts, kind: 'relay.start', data: {} });
const forTurn = (id, events) => events.map((event) => ({ ...event, turn_id: id }));

test('unknown credits remain unknown and explicit zero remains known', () => {
  const [unknown] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: null })]);
  assert.equal(unknown.credits, null);
  assert.equal(unknown.creditsComplete, false);
  const [zero] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: 0 })]);
  assert.equal(zero.credits, 0);
  assert.equal(zero.creditsComplete, true);
});

test('a known debit and an unknown attempt are displayed as a lower bound', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: null, attempt: 1 }),
    ev(102, 'upstream.usage', { credits: 3.5, attempt: 2 })]);
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
    const [trace] = buildTraces([ev(100, 'turn.begin'), ...usage.map((data, i) => ev(101 + i, 'upstream.usage', data))]);
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

// The price rules of the relay's journal (tests/test_journal_costs.py, SCENARIOS), seen from one turn:
// [name, steps, credits, unknown attempts, cost is complete]. Each ended turn is closed by its own turn.end.
const START = (n) => ['upstream.usage_start', { attempt: n }];
const USAGE = (n, credits, extra = {}) => ['upstream.usage', { attempt: n, credits, ...extra }];
const MARKER = { credits_reported: false };
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
  ['tokens-only update after the price', [START(1), USAGE(1, 0.01), USAGE(1, null, { credits_reported: true }), END()], 0.01, 0, true],
  ['price after a tokens-only snapshot', [START(1), USAGE(1, null), USAGE(1, 0.25), END()], 0.25, 0, true],
  ['a reported zero is a price', [START(1), USAGE(1, 0), END()], 0, 0, true],
  ['cancelled while the attempt was silent: may have been billed', [START(1), END('cancelled')], null, 1, false],
  ['cancelled before any attempt started', [END('cancelled')], 0, 0, true],
  ['graceful cancel: the marker and the cancelled end count once', [START(1), USAGE(1, null, MARKER), END('cancelled')], null, 1, false],
  ['cancelled second attempt after a priced first one', [START(1), USAGE(1, 0.01), START(2), END('cancelled')], 0.01, 1, false],
  ['cancelled after the price arrived', [START(1), USAGE(1, 0.01), END('cancelled')], 0.01, 0, true],
  ['an invalid price after a valid one changes nothing', [START(1), USAGE(1, 0.1), USAGE(1, -4, { credits_reported: true }), START(2), USAGE(2, 0.2), END()], 0.3, 0, true],
];
for (const [name, steps, credits, unknown, complete] of COSTS) {
  test(`cost of a finished turn: ${name}`, () => {
    const trace = turnOf(steps);
    const cost = costOf(trace);
    assert.equal(cost.final, true);
    assert.equal(cost.credits === null ? null : Math.round(cost.credits * 1e6) / 1e6, credits);
    assert.equal(cost.unknown, unknown);
    assert.equal(cost.complete, complete);
    assert.equal(trace.credits, cost.credits);
    assert.equal(trace.creditsComplete, complete);
  });
}

test('a running turn is never a finished unknown: nothing reported yet is not a gap, a price so far is a lower bound', () => {
  const waiting = costOf(turnOf([START(1)]));
  assert.deepEqual(waiting, { credits: null, unknown: 0, final: false, complete: true });
  const between = costOf(turnOf([START(1), USAGE(1, 0.1)]));
  assert.deepEqual(between, { credits: 0.1, unknown: 0, final: false, complete: true });
  assert.deepEqual(costOf(turnOf([])), { credits: null, unknown: 0, final: false, complete: true });
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

test('a turn cut off by a relay restart or by silence while its attempt was silent may have been billed', () => {
  const silent = turnOf([START(1)]);
  assert.deepEqual(costOf(silent, 'interrupted'), { credits: null, unknown: 1, final: true, complete: false });
  const priced = turnOf([START(1), USAGE(1, 0.25)]);
  assert.deepEqual(costOf(priced, 'interrupted'), { credits: 0.25, unknown: 0, final: true, complete: true });
  assert.deepEqual(costOf(turnOf([]), 'interrupted'), { credits: 0, unknown: 0, final: true, complete: true });
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
  assert.equal(costOf(trace, statusAt(trace, 101 + STALE_AFTER + 1)).complete, true);
});

test('an interrupted turn whose attempt never got a price has an unknown cost', () => {
  const [trace] = buildTraces([ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }), relayStart(500)]);
  assert.equal(trace.status, 'interrupted');
  assert.equal(trace.credits, null);
  assert.equal(trace.creditsComplete, false);
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
