import assert from 'node:assert/strict';
import test from 'node:test';
import { buildTraces, phasesOf } from '../src/turns.js';

const ev = (ts, kind, data = {}) => ({ ts, kind, data, turn_id: 'test-turn' });

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
