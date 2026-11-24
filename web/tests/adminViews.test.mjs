import assert from 'node:assert/strict';
import test from 'node:test';
import { opencodeSnippet, pingModelOf } from '../src/catalog.js';
import { showSpendPeak, spendAxisLabel, spendUnitOf } from '../src/spend.js';
import { createJournalBuffer } from '../src/useJournal.js';

// A14-09: the unit picked with the "Кредиты / USD" switch stayed USD after the dollars left the window, and the switch is
// drawn only while there are dollars: the chart said "Расхода за окно не было" with Retool credits being spent, and
// nothing on the page brought the credits back.
test('the spend unit picked by hand holds only while the window has dollars to show', () => {
  const usd = { credits: 12.5, cost_usd: 0.42 };
  const creditsOnly = { credits: 12.5, cost_usd: 0 };
  const dollarsOnly = { credits: 0, cost_usd: 0.42 };
  assert.equal(spendUnitOf(null, usd), 'credits');
  assert.equal(spendUnitOf('usd', usd), 'usd');
  assert.equal(spendUnitOf('usd', creditsOnly), 'credits');
  assert.equal(spendUnitOf('usd', { credits: 3 }), 'credits');
  assert.equal(spendUnitOf(null, dollarsOnly), 'usd');
  assert.equal(spendUnitOf('credits', dollarsOnly), 'credits');
});

test('a dollar axis shows cents and their fractions, a credit axis keeps its two decimals', () => {
  assert.equal(spendAxisLabel(0.004, 'usd'), '$0,0040');
  assert.equal(spendAxisLabel(0.002, 'usd'), '$0,0020');
  assert.equal(spendAxisLabel(2.5, 'usd'), '$2,50');
  assert.equal(spendAxisLabel(0.5, 'credits'), '0,50');
  assert.equal(spendAxisLabel(25, 'credits'), '25,0');
});

// A14-11: the select lists only models with a route, while the test pinged the default model even when it had none: the
// select showed one model and the button tested another, which failed.
test('the model test pings the model the select shows', () => {
  const data = { default_model: 'claude-opus-5-5', data: [{ id: 'claude-opus-5-5', routable: false }, { id: 'gpt-6.1-sol', routable: true }] };
  assert.equal(pingModelOf(data, ''), 'gpt-6.1-sol');
  assert.equal(pingModelOf({ ...data, data: [{ id: 'claude-opus-5-5', routable: true }, { id: 'gpt-6.1-sol', routable: true }] }, ''), 'claude-opus-5-5');
  assert.equal(pingModelOf(data, 'gpt-6.1-sol'), 'gpt-6.1-sol');
  assert.equal(pingModelOf(data, 'claude-opus-5-5'), 'gpt-6.1-sol');
  assert.equal(pingModelOf({ default_model: 'x', data: [] }, ''), '');
  assert.equal(pingModelOf(null, ''), '');
});

// A14-06: the "Подключение IDE" snippet made an OpenCode provider "proxi" without models. The relay's own sync
// (scripts/sync_opencode_catalog.py, PROVIDER = 'retool-relay') fills only "retool-relay" and fails without it, so OpenCode
// set up from the snippet showed no models and "Синхр. OpenCode" answered with an error.
test('the OpenCode snippet sets up the provider the relay keeps in sync, with the default model', () => {
  const snippet = JSON.parse(opencodeSnippet('http://127.0.0.1:8787/v1', 'claude-opus-5-5'));
  assert.deepEqual(Object.keys(snippet.provider), ['retool-relay']);
  const provider = snippet.provider['retool-relay'];
  assert.equal(provider.npm, '@ai-sdk/openai-compatible');
  assert.equal(provider.options.baseURL, 'http://127.0.0.1:8787/v1');
  assert.equal(provider.options.apiKey, '{env:PROXI_API_KEY}');
  assert.deepEqual(Object.keys(provider.models), ['claude-opus-5-5']);
  assert.equal(snippet.model, 'retool-relay/claude-opus-5-5');
});

test('SpendChart peak visibility matches formatted axis tick precision for sub-cent USD peaks', () => {
  // Top tick $0.0050 vs peak $0.0042: must stay shown with $ and 4 decimals instead of collapsing both to "0,00".
  assert.equal(showSpendPeak(0.0042, 0.005, 'usd'), true);
  assert.equal(spendAxisLabel(0.0042, 'usd'), '$0,0042');
  // Peak matching the top tick exactly is omitted unless unsure.
  assert.equal(showSpendPeak(0.005, 0.005, 'usd'), false);
  assert.equal(showSpendPeak(0.005, 0.005, 'usd', true), true);
  assert.equal(showSpendPeak(1.48, 1.5, 'credits'), false);
  assert.equal(showSpendPeak(1.44, 2, 'credits'), true);
});

test('journal clear coordinates with SSE reset so seq=1 admin.journal_cleared is never wiped', () => {
  let latest = [];
  const buf = createJournalBuffer((events) => { latest = events; });
  buf.loadInitial([{ seq: 10, kind: 'old.event' }, { seq: 11, kind: 'old.other' }], 11);
  assert.equal(buf.lastSeq, 11);

  // Race case 1: SSE receives `reset` + `admin.journal_cleared` (seq=1) BEFORE POST events/clear resolves.
  const clearEpoch = buf.resetEpoch;
  buf.reset(); // SSE onReset
  buf.push({ seq: 1, kind: 'admin.journal_cleared' });
  buf.flushNow();
  assert.equal(buf.reset(clearEpoch), false);
  assert.deepEqual(latest, [{ seq: 1, kind: 'admin.journal_cleared' }]);
  assert.equal(buf.lastSeq, 1);

  // Case 2: POST resolves when no SSE reset arrived (e.g. offline/mocked stream) -> resets cleanly.
  const epoch2 = buf.resetEpoch;
  assert.equal(buf.reset(epoch2), true);
  assert.deepEqual(latest, []);
  assert.equal(buf.lastSeq, 0);
  buf.dispose();
});
