import assert from 'node:assert/strict';
import test from 'node:test';
import { STALE_AFTER, buildTraces, costOf, phasesOf, scaleOf, statusAt } from '../src/turns.js';

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

test('an attempt without a price is unknown once the turn is over, not while the turn runs', () => {
  const begun = [ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 })];
  const [running] = buildTraces(begun);
  assert.equal(running.creditsComplete, true);
  assert.equal(running.credits, null);
  assert.equal(costOf(running).pending, true);
  const [over] = buildTraces([...begun, finish(103, 3000, 'error')]);
  assert.equal(over.credits, null);
  assert.equal(over.creditsComplete, false);
  assert.equal(costOf(over).pending, false);
});

test('a priced attempt followed by an unpriced one is a lower bound only after the turn ends', () => {
  const attempts = [ev(100, 'turn.begin'), ev(100, 'upstream.usage_start', { attempt: 1 }),
    ev(101, 'upstream.usage', { credits: 2, attempt: 1 }), ev(102, 'upstream.usage_start', { attempt: 2 })];
  const [running] = buildTraces(attempts);
  assert.equal(running.credits, 2);
  assert.equal(running.creditsComplete, true);
  const [over] = buildTraces([...attempts, finish(104, 4000)]);
  assert.equal(over.credits, 2);
  assert.equal(over.creditsComplete, false);
});

test('a negative price is invalid and stays unknown, as the relay treats it', () => {
  for (const credits of [-3, NaN]) {
    const [trace] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits, attempt: 1 }), finish(102, 2000)]);
    assert.equal(trace.credits, null);
    assert.equal(trace.creditsComplete, false);
  }
  const [mixed] = buildTraces([ev(100, 'turn.begin'), ev(101, 'upstream.usage', { credits: 2, attempt: 1 }),
    ev(102, 'upstream.usage', { credits: -3, attempt: 2 }), finish(103, 3000)]);
  assert.equal(mixed.credits, 2);
  assert.equal(mixed.creditsComplete, false);
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
