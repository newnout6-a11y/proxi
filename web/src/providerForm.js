// The provider form of the gateway panel (GatewayPanels.jsx) as plain functions: what the form shows of a saved provider
// and what a save sends. No React here, so node --test checks it (web/tests/providerForm.test.mjs).

export const EMPTY_PROVIDER = {
  preset: 'openrouter',
  name: 'OpenRouter',
  id: 'openrouter',
  driver: 'openai-chat',
  base_url: 'https://openrouter.ai/api/v1',
  auth_mode: 'bearer',
  auth_header_name: '',
  api_keys_text: '',
  api_key_env: '',
  routing_strategy: 'fill-first',
  cache_policy: 'auto',
  proxy_url: '',
  auto_discover: true,
  models_text: '',
  whitelist_text: '',
  blacklist_text: '',
  headers_text: '',
  body_overrides_text: '',
  saved_headers: [],
};

function parseJson(text, what) {
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Поле «${what}» должно быть корректным JSON`);
  }
}

/** A list typed in a field: a JSON array, or ids separated by commas or lines; null when the field is empty. */
export function parseListInput(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('[')) {
    const parsed = parseJson(trimmed, 'список');
    if (!Array.isArray(parsed)) throw new Error('Ожидается JSON-массив');
    return parsed;
  }
  return trimmed.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
}

/**
 * The manual models as typed: an OpenCode object keyed by model id ({"id": {"name": ...}}), a JSON list (ids or
 * {"id": ...} objects) or plain ids separated by commas or lines; null when the field is empty. An object was split at
 * its commas and its pieces were saved as model ids, every model of the provider then answered 404 (audit A14-01).
 */
export function parseModelsInput(text) {
  const trimmed = (text || '').trim();
  if (!trimmed) return null;
  if (trimmed.startsWith('{')) {
    const parsed = parseJson(trimmed, 'Ручные модели');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Ожидается JSON-объект моделей');
    return parsed;
  }
  return parseListInput(trimmed);
}

function parseJsonObject(text, fieldName) {
  const trimmed = (text || '').trim();
  if (!trimmed) return undefined;
  const parsed = parseJson(trimmed, fieldName);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`Поле «${fieldName}» должно быть JSON-объектом`);
  }
  return parsed;
}

/** The models of a saved provider as the field shows them: ids joined by commas, anything richer as JSON. */
export function modelsText(models) {
  if (Array.isArray(models)) {
    if (!models.length) return '';
    return models.every((m) => typeof m === 'string') ? models.join(', ') : JSON.stringify(models);
  }
  return models && typeof models === 'object' && Object.keys(models).length ? JSON.stringify(models) : '';
}

const hostOf = (url) => {
  try {
    return new URL(String(url || '').trim()).host.toLowerCase();
  } catch {
    return null;
  }
};

/**
 * Whether a base URL is on the preset's own host. A preset brings its balance endpoint, and with it the provider's key
 * went to that endpoint: a Z.ai base URL left on the OpenRouter preset sent the Z.ai key to openrouter.ai (audit A11-01).
 */
export function presetMatchesUrl(preset, baseUrl) {
  const own = hostOf(preset?.base_url);
  return own !== null && own === hostOf(baseUrl);
}

/** A saved provider (GET /admin/api/providers) as the edit form starts. */
export function providerToForm(prov) {
  const envFromCred = prov.credentials?.length === 1 ? prov.credentials[0].api_key_env || '' : '';
  const models = modelsText(prov.models);
  const overrides = prov.body_overrides && Object.keys(prov.body_overrides).length
    ? JSON.stringify(prov.body_overrides, null, 2) : '';
  return {
    preset: prov.preset && prov.preset !== 'custom' ? prov.preset : '',
    name: prov.name || prov.id,
    id: prov.id,
    driver: prov.driver || 'openai-chat',
    base_url: prov.base_url || '',
    auth_mode: prov.auth_mode || 'bearer',
    auth_header_name: prov.auth_header_name || '',
    api_keys_text: '',
    api_key_env: envFromCred,
    routing_strategy: prov.routing_strategy || 'fill-first',
    cache_policy: prov.cache_policy || 'auto',
    proxy_url: prov.proxy_url || '',
    auto_discover: prov.auto_discover !== false,
    models_text: models,
    whitelist_text: (prov.model_whitelist || []).join(', '),
    blacklist_text: (prov.model_blacklist || []).join(', '),
    // The saved headers stay on the server (their values are never shown): the field adds to them by name.
    headers_text: '',
    saved_headers: [...(prov.headers_keys || [])],
    body_overrides_text: overrides,
    // What the form started with: a field left as it was is not sent.
    initial: { models_text: models, proxy_url: prov.proxy_url || '', body_overrides_text: overrides },
  };
}

/**
 * What a save sends. A new provider sends every field; an edit sends a field it shows as the server keeps it only when
 * it was changed: the models (any shape), the proxy (shown without its password) and the body overrides (cleared: {}).
 * Headers go only when typed, and the server adds them by name to the saved ones (null removes one).
 */
export function buildProviderPayload(form, editingId, presets = []) {
  const keys = form.api_keys_text.split('\n').map((s) => s.trim()).filter(Boolean);
  const env = form.api_key_env.trim();
  const initial = editingId ? form.initial || {} : {};
  const payload = {
    name: form.name.trim() || form.id.trim(),
    driver: form.driver,
    base_url: form.base_url.trim(),
    auth_mode: form.auth_mode,
    routing_strategy: form.routing_strategy,
    cache_policy: form.cache_policy,
    auto_discover: !!form.auto_discover,
  };
  if (!editingId || form.proxy_url !== initial.proxy_url) payload.proxy_url = form.proxy_url.trim() || null;
  if (!editingId && form.id.trim()) payload.id = form.id.trim().toLowerCase();
  const preset = presets.find((p) => p.id === form.preset);
  if (form.preset && (!preset || presetMatchesUrl(preset, payload.base_url))) payload.preset = form.preset;
  if (form.auth_mode === 'custom-header') payload.auth_header_name = form.auth_header_name.trim();

  if (form.auth_mode !== 'none') {
    if (keys.length > 1) {
      payload.credentials = keys.map((k) => ({ api_key: k }));
    } else if (keys.length === 1) {
      payload.api_key = keys[0];
    } else if (env) {
      payload.api_key_env = env;
    } else if (!editingId) {
      throw new Error('Укажите хотя бы один API-ключ или имя переменной окружения');
    }
  }

  if (!editingId || form.models_text !== initial.models_text) payload.models = parseModelsInput(form.models_text) || [];
  payload.model_whitelist = parseListInput(form.whitelist_text);
  payload.model_blacklist = parseListInput(form.blacklist_text);

  const headers = parseJsonObject(form.headers_text, 'Доп. заголовки');
  if (headers !== undefined) payload.headers = headers;
  if (!editingId || form.body_overrides_text !== initial.body_overrides_text) {
    const overrides = parseJsonObject(form.body_overrides_text, 'Body overrides');
    // A cleared field removes the saved overrides: sent as nothing, they stayed (audit A14-03).
    if (overrides !== undefined) payload.body_overrides = overrides;
    else if (editingId) payload.body_overrides = {};
  }
  return payload;
}
