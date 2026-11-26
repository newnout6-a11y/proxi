import assert from 'node:assert/strict';
import test from 'node:test';
import {
  EMPTY_CLIENT_KEY, buildClientKeyPayload, buildProviderPayload, clientKeyToForm,
  parseModelsInput, presetMatchesUrl, providerToForm,
} from '../src/providerForm.js';

const PRESETS = [
  { id: 'openrouter', name: 'OpenRouter', driver: 'openai-chat', base_url: 'https://openrouter.ai/api/v1', auth_mode: 'bearer', cache_policy: 'auto' },
  { id: 'deepseek', name: 'DeepSeek', driver: 'openai-chat', base_url: 'https://api.deepseek.com', auth_mode: 'bearer', cache_policy: 'auto' },
];

// What GET /admin/api/providers answers for one provider (catalog/admin_ops._public_provider).
const saved = (extra = {}) => ({
  id: 'deepseek', name: 'DeepSeek', preset: 'deepseek', driver: 'openai-chat', base_url: 'https://api.deepseek.com',
  auth_mode: 'bearer', routing_strategy: 'fill-first', cache_policy: 'auto', auto_discover: true, proxy_url: null,
  headers_keys: [], body_overrides: {}, models: [], model_whitelist: null, model_blacklist: null,
  credentials: [{ index: 1, api_key_set: true, api_key_env: null }], ...extra,
});

const OPENCODE_MODELS = {
  'deepseek-chat': { name: 'DeepSeek Chat', limit: { context: 128000 } },
  'deepseek-reasoner': { name: 'DeepSeek R1' },
};

// A14-01: the form showed an OpenCode models object as JSON, split it at its commas and sent the pieces as model ids:
// '{"deepseek-chat":{"name":"DeepSeek Chat"' and so on were saved, and every model of the provider answered 404.
test('an edit that leaves the models alone does not send them, whatever their shape', () => {
  for (const models of [OPENCODE_MODELS, ['deepseek-chat', 'deepseek-reasoner'], [{ id: 'deepseek-chat', name: 'Chat' }], []]) {
    const form = providerToForm(saved({ models }));
    const payload = buildProviderPayload({ ...form, name: 'Renamed' }, 'deepseek', PRESETS);
    assert.equal('models' in payload, false, JSON.stringify(models));
    assert.equal(payload.name, 'Renamed');
  }
});

test('models typed as an object, a JSON list or plain ids are sent as such', () => {
  assert.deepEqual(parseModelsInput(JSON.stringify(OPENCODE_MODELS)), OPENCODE_MODELS);
  assert.deepEqual(parseModelsInput('[{"id": "a"}, "b"]'), [{ id: 'a' }, 'b']);
  assert.deepEqual(parseModelsInput('a, b\nc'), ['a', 'b', 'c']);
  assert.equal(parseModelsInput('  '), null);
  assert.throws(() => parseModelsInput('{"a": 1'), /JSON/);
  assert.throws(() => parseModelsInput('["a"'), /JSON/);
  const form = providerToForm(saved({ models: OPENCODE_MODELS }));
  const edited = { ...OPENCODE_MODELS, 'deepseek-v4': { name: 'V4' } };
  assert.deepEqual(buildProviderPayload({ ...form, models_text: JSON.stringify(edited) }, 'deepseek', PRESETS).models, edited);
  assert.deepEqual(buildProviderPayload({ ...form, models_text: '' }, 'deepseek', PRESETS).models, []);
});

// A14-03: the saved headers never reach the form (only their names), and the object replaced them all on the server.
test('headers go only when typed, as additions to the saved ones, which the form names', () => {
  const form = providerToForm(saved({ headers_keys: ['HTTP-Referer', 'X-Title'] }));
  assert.deepEqual(form.saved_headers, ['HTTP-Referer', 'X-Title']);
  assert.equal('headers' in buildProviderPayload(form, 'deepseek', PRESETS), false);
  const payload = buildProviderPayload({ ...form, headers_text: '{"X-New": "1", "X-Title": null}' }, 'deepseek', PRESETS);
  assert.deepEqual(payload.headers, { 'X-New': '1', 'X-Title': null });
});

test('cleared body overrides are sent as an empty object, untouched ones not at all', () => {
  const form = providerToForm(saved({ body_overrides: { service_tier: 'auto' } }));
  assert.equal('body_overrides' in buildProviderPayload(form, 'deepseek', PRESETS), false);
  assert.deepEqual(buildProviderPayload({ ...form, body_overrides_text: '' }, 'deepseek', PRESETS).body_overrides, {});
  assert.deepEqual(buildProviderPayload({ ...form, body_overrides_text: '{"top_k": 5}' }, 'deepseek', PRESETS).body_overrides, { top_k: 5 });
  assert.equal('body_overrides' in buildProviderPayload({ ...providerToForm(saved()), name: 'x' }, 'deepseek', PRESETS), false);
});

// A07-09 / A11-19: the admin shows a proxy_url without its user name and password; sent back unchanged it would be saved so.
test('a proxy address shown without its password is sent only when changed', () => {
  const form = providerToForm(saved({ proxy_url: 'socks5://•••@10.0.0.2:1080' }));
  assert.equal('proxy_url' in buildProviderPayload({ ...form, name: 'x' }, 'deepseek', PRESETS), false);
  assert.equal(buildProviderPayload({ ...form, proxy_url: 'socks5://10.0.0.3:1080' }, 'deepseek', PRESETS).proxy_url, 'socks5://10.0.0.3:1080');
  assert.equal(buildProviderPayload({ ...form, proxy_url: '' }, 'deepseek', PRESETS).proxy_url, null);
});

// A11-01: the new-provider form starts on the OpenRouter preset; a base URL on another host kept it, and with it the
// preset's balance endpoint: that provider's key went to openrouter.ai.
test('a preset is sent only while the base URL is on its own host', () => {
  const fresh = { ...providerToForm(saved()), preset: 'openrouter', base_url: 'https://openrouter.ai/api/v1', api_keys_text: 'sk-or-v1-x' };
  assert.equal(buildProviderPayload(fresh, null, PRESETS).preset, 'openrouter');
  const moved = { ...fresh, base_url: 'https://api.z.ai/api/paas/v4', id: 'zai' };
  assert.equal('preset' in buildProviderPayload(moved, null, PRESETS), false);
  assert.equal(presetMatchesUrl(PRESETS[0], 'https://openrouter.ai/api/v1/'), true);
  assert.equal(presetMatchesUrl(PRESETS[0], 'https://api.z.ai/api/paas/v4'), false);
  assert.equal(presetMatchesUrl(PRESETS[0], 'not a url'), false);
});

test('a new provider without a key or a variable is refused, an edit keeps the stored key', () => {
  const blank = { ...providerToForm(saved()), api_keys_text: '', api_key_env: '' };
  assert.throws(() => buildProviderPayload(blank, null, PRESETS), /ключ/);
  const edit = buildProviderPayload(blank, 'deepseek', PRESETS);
  assert.equal('api_key' in edit || 'credentials' in edit || 'api_key_env' in edit, false);
  assert.deepEqual(buildProviderPayload({ ...blank, api_keys_text: 'k1\nk2' }, 'deepseek', PRESETS).credentials, [{ api_key: 'k1' }, { api_key: 'k2' }]);
});

test('editing a preset provider to a custom base_url emits preset: "custom" so PATCH clears the old preset', () => {
  const form = providerToForm(saved({ preset: 'deepseek', base_url: 'https://api.deepseek.com' }));
  assert.equal(form.preset, 'deepseek');
  // Unchanged base_url keeps its preset
  assert.equal(buildProviderPayload(form, 'deepseek', PRESETS).preset, 'deepseek');
  // Moving base_url off the preset host (even after UI cleared form.preset to '') emits 'custom' on edit
  const clearedByUi = { ...form, preset: '', base_url: 'https://proxy.custom.invalid/v1' };
  assert.equal(buildProviderPayload(clearedByUi, 'deepseek', PRESETS).preset, 'custom');
  // Direct call with mismatched preset/url also resets to 'custom' on edit, while new providers omit preset
  const directMoved = { ...form, base_url: 'https://proxy.custom.invalid/v1' };
  assert.equal(buildProviderPayload(directMoved, 'deepseek', PRESETS).preset, 'custom');
  assert.equal('preset' in buildProviderPayload({ ...directMoved, api_keys_text: 'k1' }, null, PRESETS), false);
});

test('client key form guards non-numeric limits instead of turning NaN into null and includes Minara limits', () => {
  for (const badRpm of ['abc', '12abc', '1e5', '1.5', '0', '100001', '-5']) {
    assert.throws(() => buildClientKeyPayload({ ...EMPTY_CLIENT_KEY, label: 'IDE', rpm_limit: badRpm }), /RPM/);
  }
  for (const [field, pattern] of [
    ['max_budget_usd', /Бюджет в USD/],
    ['max_request_minara', /Лимит Minara за запрос/],
    ['max_day_minara', /Дневной лимит Minara/],
  ]) {
    for (const badVal of ['abc', '10abc', 'NaN', 'Infinity', '-1', '1e5']) {
      assert.throws(() => buildClientKeyPayload({ ...EMPTY_CLIENT_KEY, label: 'IDE', [field]: badVal }), pattern);
    }
  }
  const payload = buildClientKeyPayload({
    ...EMPTY_CLIENT_KEY,
    label: '  Cursor  ',
    allowed_text: 'minara/*, claude-opus-5-5',
    rpm_limit: '60',
    max_budget_usd: '12,5',
    max_request_minara: '25.5',
    max_day_minara: '250',
    allow_cost_confirm: true,
  });
  assert.deepEqual(payload, {
    label: 'Cursor',
    allowed_models: ['minara/*', 'claude-opus-5-5'],
    rpm_limit: 60,
    max_budget_usd: 12.5,
    max_request_minara: 25.5,
    max_day_minara: 250,
    allow_cost_confirm: true,
  });
  const roundtrip = clientKeyToForm({ id: 'ck-1', ...payload });
  assert.equal(roundtrip.editing, true);
  assert.equal(roundtrip.max_request_minara, '25.5');
  assert.equal(roundtrip.max_day_minara, '250');
  assert.equal(roundtrip.allow_cost_confirm, true);
});
