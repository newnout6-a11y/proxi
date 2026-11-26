import assert from 'node:assert/strict';
import test from 'node:test';
import { opencodeSnippet, pingModelOf } from '../src/catalog.js';
import { spendAxisLabel, spendUnitOf } from '../src/spend.js';

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
