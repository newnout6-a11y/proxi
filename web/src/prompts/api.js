// Every call of the prompts screen to the relay lives here, together with the code that reads what comes back,
// so a change in the admin API (plan 4.1) is fixed in one place. Shapes beyond 4.1 are read defensively.
import { api } from '../api.js';
import { normalizeConditions } from './conditions.js';
import { normalizeEditOps } from './ops.js';
import { normalizeParts } from './parts.js';
import { isObject } from './draft.js';

const isNum = (value) => typeof value === 'number' && Number.isFinite(value);
const list = (value) => (Array.isArray(value) ? value : []);
const text = (value) => (typeof value === 'string' ? value : '');

export const RELAY_TEXT_NAMES = ['role', 'native_role', 'structured', 'reminder', 'codex_base'];

// What the screen says about the variables when the relay names them without a description (plan 2.2).
export const VARIABLE_HINTS = {
  client_system: 'system клиента после правок, без служебной строки Claude Code',
  client_system_raw: 'system клиента без правок',
  role: 'роль relay: свой текст или встроенный',
  native_role: 'роль для клиента с tools без своих инструкций',
  relay_task: 'роль, локальные команды и контекст проекта; только для запросов relay',
  local_exec: 'блок локальных команд; только для запросов relay',
  project_context: 'блок контекста проекта; только для запросов relay',
  exec_reminder: 'напоминание о командах; только для запросов relay',
  structured: 'контракт структурированного ответа',
  model: 'модель запроса',
  client: 'клиент: opencode, claude-code, …',
  agent: 'агент клиента',
  purpose: 'назначение запроса',
};
export const DEFAULT_VARIABLES = Object.keys(VARIABLE_HINTS);

export function normalizeVariables(raw) {
  const items = Array.isArray(raw) ? raw : isObject(raw) ? Object.entries(raw).map(([name, info]) => (isObject(info) ? { name, ...info } : { name, description: info })) : [];
  const out = [];
  for (const item of items) {
    const name = typeof item === 'string' ? item : item?.name || item?.id || item?.key;
    if (typeof name !== 'string' || !name || out.some((entry) => entry.name === name)) continue;
    const description = typeof item === 'object' ? item.description || item.help || item.title : '';
    out.push({ name, description: text(description) || VARIABLE_HINTS[name] || '' });
  }
  return out.length ? out : DEFAULT_VARIABLES.map((name) => ({ name, description: VARIABLE_HINTS[name] }));
}

function normalizeRule(rule) {
  if (!isObject(rule)) return null;
  return { name: text(rule.name) || text(rule.label), if: isObject(rule.if) ? rule.if : {}, use: text(rule.use) || 'passthrough', description: text(rule.description) };
}

export function normalizeBuiltin(raw) {
  const data = isObject(raw) ? raw : {};
  const relayTexts = isObject(data.relay_texts) ? data.relay_texts : {};
  return {
    relayTexts,
    protectedRules: list(data.protected_rules).map(normalizeRule).filter(Boolean),
    variables: normalizeVariables(data.variables),
    editOps: normalizeEditOps(data.edit_ops),
    conditions: normalizeConditions(data.conditions),
    profiles: list(data.profiles).map((item) => (typeof item === 'string' ? item : item?.name)).filter(Boolean),
  };
}

export function normalizeErrors(raw) {
  return list(raw).map((item) => (typeof item === 'string' ? { message: item, param: '', ts: null }
    : { message: text(item?.message) || 'Ошибка', param: text(item?.param), ts: isNum(item?.ts) ? item.ts : null }));
}

export function normalizeLegacy(raw) {
  const data = isObject(raw) ? raw : {};
  return { mode: text(data.mode), roleChars: isNum(data.role_chars) ? data.role_chars : null };
}

/** GET /admin/api/prompts */
export function normalizePrompts(raw) {
  const data = isObject(raw) ? raw : {};
  return {
    document: isObject(data.document) ? data.document : null,
    rev: isNum(data.rev) ? data.rev : null,
    path: text(data.path),
    errors: normalizeErrors(data.errors),
    legacy: normalizeLegacy(data.legacy),
    builtin: normalizeBuiltin(data.builtin),
  };
}

/** POST /admin/api/prompts/preview */
export function normalizePreview(raw) {
  const data = isObject(raw) ? raw : {};
  return {
    rule: typeof data.rule === 'string' ? data.rule : null,
    profile: typeof data.profile === 'string' ? data.profile : null,
    rev: isNum(data.rev) ? data.rev : null,
    protected: data.protected === true,
    purpose: text(data.purpose),
    client: text(data.client),
    model: text(data.model),
    head: text(data.head),
    parts: normalizeParts(data.parts),
    history_changes: list(data.history_changes),
    tools_changes: list(data.tools_changes),
    edits_unmatched: list(data.edits_unmatched),
    warnings: list(data.warnings),
    head_bytes: isNum(data.head_bytes) ? data.head_bytes : null,
    tokens_estimate: isNum(data.tokens_estimate) ? data.tokens_estimate : null,
  };
}

/** GET /admin/api/prompts/recent */
export function normalizeRecent(raw) {
  return list(isObject(raw) ? raw.items : raw).filter(isObject).map((item) => ({
    ts: isNum(item.ts) ? item.ts : null,
    request_id: text(item.request_id),
    client: text(item.client),
    agent: text(item.agent),
    model: text(item.model),
    purpose: text(item.purpose),
    rule: typeof item.rule === 'string' ? item.rule : null,
    profile: typeof item.profile === 'string' ? item.profile : null,
    rev: isNum(item.rev) ? item.rev : null,
    pinned: item.pinned === true,
    head: text(item.head),
    parts: normalizeParts(item.parts),
    history_changes: list(item.history_changes),
    tools_changes: list(item.tools_changes),
    has_request: item.has_request === true,
  }));
}

/** GET /admin/api/prompts/pins */
export function normalizePins(raw) {
  return list(isObject(raw) ? raw.items : raw).filter(isObject).map((item) => ({
    conversation: text(item.conversation),
    purpose: text(item.purpose),
    rev: isNum(item.rev) ? item.rev : null,
    profile: typeof item.profile === 'string' ? item.profile : null,
    age_s: isNum(item.age_s) ? item.age_s : null,
  }));
}

// --- what the user pastes ------------------------------------------------------------------------------------------

/** A request body typed or pasted by the user: {body} or {error}. */
export function parseBody(source) {
  const raw = String(source ?? '').trim();
  if (!raw) return { error: 'Вставьте тело запроса' };
  try {
    const body = JSON.parse(raw);
    return isObject(body) ? { body } : { error: 'Тело запроса — JSON-объект' };
  } catch (failure) {
    return { error: `Не JSON: ${failure.message}` };
  }
}

/** Headers as "Name: value" lines or a JSON object: {headers} or {error}. */
export function parseHeaders(source) {
  const raw = String(source ?? '').trim();
  if (!raw) return { headers: {} };
  if (raw.startsWith('{')) {
    try {
      const parsed = JSON.parse(raw);
      if (!isObject(parsed)) throw new Error('нужен объект');
      return { headers: Object.fromEntries(Object.entries(parsed).map(([key, value]) => [key, String(value)])) };
    } catch (failure) {
      return { error: `Заголовки: ${failure.message}` };
    }
  }
  const headers = {};
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const colon = line.indexOf(':');
    if (colon <= 0) return { error: `Заголовок без двоеточия: «${line.trim().slice(0, 40)}»` };
    headers[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  return { headers };
}

/** The dialect a pasted body most likely belongs to; the user can still choose. */
export function detectDialect(body) {
  if (!isObject(body)) return 'chat';
  if (body.input !== undefined && body.messages === undefined) return 'responses';
  if (body.system !== undefined || body.anthropic_version !== undefined) return 'anthropic';
  const blocks = list(body.messages).flatMap((message) => list(message?.content));
  if (blocks.some((block) => ['tool_use', 'tool_result'].includes(block?.type))) return 'anthropic';
  if (list(body.tools).some((tool) => isObject(tool) && tool.input_schema !== undefined)) return 'anthropic';
  return 'chat';
}

/**
 * The body of a preview request. `sample` is {kind: 'recent', request_id} or {kind: 'paste', request, dialect, headers};
 * `document` is the draft to judge it by, left out when the saved revision should answer.
 */
export function previewRequest(sample, document) {
  if (!sample) return null;
  const body = sample.kind === 'recent'
    ? { request_id: sample.request_id }
    : { request: sample.request, dialect: sample.dialect, headers: sample.headers || {} };
  if (document !== undefined) body.document = document;
  return body;
}

/**
 * A document that sends every request, protected ones excepted, to `profile`: the profile on its own, whatever
 * the rules say. Rules are dropped because the preview is about the profile, not about which rule picks it.
 */
export function forceProfile(document, profile) {
  return { ...document, default_profile: profile, rules: [] };
}

// A document that leaves the client's system prompt as it is: the baseline for "what was cut".
export const BASELINE_DOCUMENT = { version: 1, default_profile: 'passthrough', profiles: {}, rules: [] };

// --- calls -----------------------------------------------------------------------------------------------------

export const loadPrompts = (key, signal) => api(key, 'prompts', { signal }).then(normalizePrompts);

export const savePrompts = (key, document, baseRev) => api(key, 'prompts', { method: 'PUT', body: { document, base_rev: baseRev } });

export const previewPrompts = (key, body, signal) => api(key, 'prompts/preview', { method: 'POST', body, signal }).then(normalizePreview);

export const loadRecent = (key, limit = 50, signal) => api(key, `prompts/recent?limit=${limit}`, { signal }).then(normalizeRecent);

export const loadPins = (key, signal) => api(key, 'prompts/pins', { signal }).then(normalizePins);

export const changePins = (key, action, conversation) => api(key, 'prompts/pins', {
  method: 'POST', body: conversation ? { action, conversation } : { action },
});

/** What a failed call says: {status, message, param} from the relay's {"error": {"message", "param"}}. */
export function failureOf(failure) {
  const error = failure?.body?.error;
  return { status: failure?.status ?? 0, message: text(error?.message) || failure?.message || 'Ошибка запроса', param: text(error?.param) };
}
