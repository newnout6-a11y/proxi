// A stateful stand-in for the prompts admin API (plan 4.1). It runs in the page's routes, so nothing reaches a relay.
// Register it after mockAdmin: the last registered route is asked first, and it only takes /admin/api/prompts*.
import { API_KEY, NOW } from './fixtures.js';

export const SYSTEM_OPENCODE = [
  'You are opencode, an interactive CLI tool that helps users with software engineering tasks.',
  '',
  '# Tone and style',
  'You should be concise, direct, and to the point.',
  'Only use emojis if the user explicitly requests it.',
  '',
  '# Code comments',
  'IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked.',
  'Prefer self-explanatory code over comments.',
  '',
  '# Tool usage policy',
  'Use the specialized tools instead of bash when possible.',
  'Call multiple tools in parallel when they are independent.',
  '',
  '<env>',
  'Working directory: /home/dev/project',
  'Platform: linux',
  '</env>',
  '',
  '<available_skills>',
  '  <skill><name>git-release</name><description>Create release notes</description></skill>',
  '  <skill><name>pdf</name><description>Read and write PDF files</description></skill>',
  '</available_skills>',
].join('\n');

export const SYSTEM_CLAUDE_CODE = [
  'x-anthropic-billing-header: cc_version=1.0.3; cc_entrypoint=cli;',
  'You are Claude Code, Anthropic\'s official CLI for Claude.',
  '',
  '# Doing tasks',
  'Prefer editing existing files to creating new ones.',
].join('\n');

const BUILTIN_TEXTS = {
  // 329 characters, like the role the relay really has: legacy.role_chars is this length while no role of its own is set
  role: ('[ROLE]\nYou are a coding assistant behind a local OpenAI/Anthropic relay. Follow the user\'s task and language. '.repeat(4)).slice(0, 329),
  native_role: '[ROLE]\nYou are a coding assistant using the client\'s tools.',
  structured: '{{default}}',
  reminder: '<system-reminder>\n{{text}}\n</system-reminder>',
  codex_base: 'You are Codex, a coding agent. '.repeat(12).trim(),
};

export const PROTECTED_RULES = [
  { name: 'protected: service requests', if: { purpose: ['session-title', 'auto-mode-check', 'connection-probe', 'client-service'] }, use: 'passthrough' },
  { name: 'protected: Claude Code compaction', if: { request_class: ['compaction', 'auxiliary'] }, use: 'passthrough' },
];

const VARIABLES = [
  'client_system', 'client_system_raw', 'role', 'native_role', 'relay_task', 'local_exec', 'project_context', 'exec_reminder',
  'structured', 'model', 'client', 'agent', 'purpose',
];
const RELAY_ONLY = ['relay_task', 'local_exec', 'project_context', 'exec_reminder'];

// The shapes the relay answers with (P4): variables are names, conditions say their type and values, and so on.
export const BUILTIN = {
  relay_texts: BUILTIN_TEXTS,
  protected_rules: PROTECTED_RULES,
  variables: VARIABLES,
  edit_ops: {
    client_system: ['remove_section', 'keep_sections', 'remove_tag', 'keep_tag_inner', 'remove_between', 'remove_regex', 'replace_regex', 'replace_text', 'prepend', 'append'],
    user_blocks: ['drop_tag', 'remove_regex', 'replace_text'],
    tools: ['append', 'prepend', 'replace_text', 'replace_regex', 'set', 'hide'],
    mid_system: ['keep', 'wrap', 'plain', 'drop'],
  },
  conditions: {
    client: { type: 'string', glob: true, values: ['opencode', 'claude-code', 'claude-desktop', 'codex', 'harness', 'unknown'] },
    agent: { type: 'string', glob: true },
    purpose: { type: 'string', glob: true, values: ['client-tools', 'client-text', 'generation', 'session-title', 'auto-mode-check', 'connection-probe', 'client-service'] },
    request_class: { type: 'string', glob: true },
    model: { type: 'string', glob: true },
    tools: { type: 'boolean' },
    structured: { type: 'boolean' },
    has_client_system: { type: 'boolean' },
    dialect: { type: 'string', glob: true, values: ['chat', 'responses', 'anthropic'] },
    header_profile: { type: 'string', glob: true },
    virtual_profile: { type: 'string', glob: true },
  },
  profiles: ['passthrough'],
  structured_example: 'Reply with one JSON object that matches the schema "answer".\n{"type": "object", "properties": {"ok": {"type": "boolean"}}}',
  relay_task_variables: RELAY_ONLY,
  special_uses: ['$header', '$virtual', '$legacy'],
  profile_name: '[A-Za-z0-9][A-Za-z0-9._-]{0,63}',
  limits: { max_text_bytes: 262144, max_document_bytes: 2097152 },
};

/** What the relay warns about in a document: protection off, rules that override it (plan I6). */
export function warningsFor(doc) {
  if (!doc) return [];
  const warnings = [];
  if (doc.protected?.enabled === false) warnings.push('protected.enabled: false: служебные запросы IDE (заголовки, сжатие, проба) получат ваш профиль вместо system клиента');
  (doc.rules || []).forEach((rule, index) => {
    if (rule.override_protected === true) warnings.push(`rules[${index}] «${rule.name}»: override_protected: правило применяется и к служебным запросам`);
  });
  return warnings;
}

export const initialDocument = () => ({
  version: 1,
  default_profile: null,
  texts: {
    my_rules: 'Отвечай по-русски. Не трогай файлы без явной просьбы.',
    opus_role: { file: 'prompts/opus.md' },
  },
  relay_texts: { role: null, native_role: null, structured: null, reminder: null, codex_base: null },
  profiles: {
    'mine-first': {
      description: 'мои правила поверх OpenCode, в начале',
      head: '{{text:my_rules}}\n\n{{client_system}}',
      client_system: { edits: [{ op: 'remove_section', heading: '# Code comments' }, { op: 'remove_tag', tag: 'available_skills' }] },
      history: { mid_system: 'keep', user_blocks: [{ op: 'drop_tag', tag: 'system-reminder', contains: 'Plan mode' }] },
      tools: { edits: [{ tool: 'bash', op: 'append', text: 'Не запускай команды, меняющие систему, без подтверждения.' }] },
      generation: { effort: null, verbosity: null },
      publish_as_model: false,
    },
    'exact-opus': { head: '{{text:opus_role}}' },
  },
  rules: [
    { name: 'opus свой', if: { model: 'claude-opus-*', client: 'opencode', tools: true }, use: 'exact-opus' },
    { name: 'по заголовку', if: { header_profile: '*' }, use: '$header' },
    { name: 'opencode', if: { client: 'opencode' }, use: 'mine-first' },
  ],
});

// --- a small model of the relay: enough to answer previews the way the relay would -------------------------------

const points = (text) => Array.from(text).length;
const sha8 = (text) => {
  let hash = 2166136261;
  for (const char of text) hash = Math.imul(hash ^ char.codePointAt(0), 16777619) >>> 0;
  return hash.toString(16).padStart(8, '0');
};
const bytes = (text) => new TextEncoder().encode(text).length;

const globToRegExp = (glob) => new RegExp(`^${String(glob).replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.')}$`, 'i');
const matchesValue = (value, wanted) => (Array.isArray(wanted) ? wanted : [wanted]).some((glob) => globToRegExp(glob).test(String(value ?? '')));

function ruleMatches(rule, request) {
  const conditions = rule.if || {};
  return Object.entries(conditions).every(([name, wanted]) => {
    if (['tools', 'structured', 'has_client_system'].includes(name)) return Boolean(request[name]) === wanted;
    if (name === 'header_profile') return request.header_profile ? matchesValue(request.header_profile, wanted) : false;
    if (name === 'virtual_profile') return false;
    return matchesValue(request[name], wanted);
  });
}

export function validateDocument(doc) {
  const known = new Set(VARIABLES);
  const texts = new Set(Object.keys(doc.texts || {}));
  const names = new Set(['passthrough', ...Object.keys(doc.profiles || {})]);
  for (const [name, profile] of Object.entries(doc.profiles || {})) {
    if (name === 'passthrough') return { message: 'profiles.passthrough: the built-in profile cannot be overridden', param: 'profiles.passthrough' };
    const head = typeof profile.head === 'string' ? profile.head : '';
    for (const match of head.matchAll(/(?<!\\)\{\{\s*([^{}]*?)\s*\}\}/g)) {
      const variable = match[1];
      if (variable.startsWith('text:')) {
        if (!texts.has(variable.slice(5))) return { message: `profiles.${name}.head: unknown text "${variable.slice(5)}"`, param: `profiles.${name}.head` };
      } else if (!known.has(variable)) {
        return { message: `profiles.${name}.head: unknown variable {{${variable}}}`, param: `profiles.${name}.head` };
      }
    }
    const edits = profile.client_system?.edits || [];
    for (let i = 0; i < edits.length; i += 1) {
      if (edits[i].op === 'remove_section' && !edits[i].heading) {
        return { message: `profiles.${name}.client_system.edits[${i}].heading: required`, param: `profiles.${name}.client_system.edits[${i}].heading` };
      }
    }
  }
  const rules = doc.rules || [];
  for (let i = 0; i < rules.length; i += 1) {
    const use = rules[i].use;
    if (!use || (!use.startsWith('$') && !names.has(use))) return { message: `rules[${i}].use: unknown profile "${use}"`, param: `rules[${i}].use` };
  }
  return null;
}

function removeSection(text, heading) {
  const lines = text.split('\n');
  const start = lines.findIndex((line) => line === heading);
  if (start < 0) return text;
  const level = heading.match(/^#+/)?.[0].length || 1;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) {
    const found = lines[i].match(/^(#+)\s/);
    if (found && found[1].length <= level) { end = i; break; }
  }
  return [...lines.slice(0, start), ...lines.slice(end)].join('\n');
}

function applySystemEdits(system, edits) {
  let text = system;
  const unmatched = [];
  edits.forEach((edit, index) => {
    const before = text;
    if (edit.op === 'remove_section') text = removeSection(text, edit.heading || '');
    else if (edit.op === 'remove_tag') text = text.replace(new RegExp(`\\n?<${edit.tag}[^>]*>[\\s\\S]*?</${edit.tag}>\\n?`, 'g'), '\n');
    else if (edit.op === 'replace_text') text = edit.find ? text.split(edit.find).join(edit.with || '') : text;
    else if (edit.op === 'prepend') text = `${edit.text || ''}${text}`;
    else if (edit.op === 'append') text = `${text}${edit.text || ''}`;
    else if (edit.op === 'remove_regex') text = text.replace(new RegExp(edit.pattern, `g${edit.flags || ''}`), '');
    if (text === before) unmatched.push({ scope: 'client_system', index, op: edit.op, ...(edit.heading ? { heading: edit.heading } : {}), ...(edit.tag ? { tag: edit.tag } : {}) });
  });
  return { text: text.replace(/\n{3,}/g, '\n\n'), unmatched };
}

// {{...}} in a template become parts: text between them is "literal", a variable is its own part.
function composeHead(template, ctx) {
  const parts = [];
  const warnings = [];
  const push = (source, text) => { if (text) parts.push({ source, text }); };
  const pattern = /\\\{\{|\{\{\s*([^{}]*?)\s*\}\}/g;
  let last = 0;
  if (ctx.billing) push('billing', `${ctx.billing}\n`);
  for (const match of template.matchAll(pattern)) {
    push('literal', template.slice(last, match.index));
    last = match.index + match[0].length;
    if (match[0].startsWith('\\')) { push('literal', '{{'); continue; }
    const name = match[1];
    if (name === 'client_system') push(ctx.edited ? 'client_edited' : 'client', ctx.system);
    else if (name === 'client_system_raw') push('client', ctx.raw);
    else if (name.startsWith('text:')) {
      const value = ctx.texts[name.slice(5)];
      push(name, typeof value === 'string' ? value : value?.file ? `[содержимое файла ${value.file}]` : '');
    } else if (RELAY_ONLY.includes(name)) {
      warnings.push(`{{${name}}} пуст: запрос принадлежит клиенту`);
    } else if (['role', 'native_role'].includes(name)) push(`relay:${name}`, BUILTIN_TEXTS[name]);
    else if (['model', 'client', 'agent', 'purpose'].includes(name)) push('literal', ctx.request[name]);
  }
  push('literal', template.slice(last));
  let offset = 0;
  const spans = parts.map((part) => {
    const size = points(part.text);
    const span = { source: part.source, start: offset, end: offset + size, chars: size, sha8: sha8(part.text) };
    offset += size;
    return span;
  });
  return { head: parts.map((part) => part.text).join(''), parts: spans, warnings };
}

function requestOf(sample) {
  const headers = Object.fromEntries(Object.entries(sample.headers || {}).map(([key, value]) => [key.toLowerCase(), value]));
  const agent = headers['user-agent'] || '';
  const client = headers['x-proxi-client'] || (/opencode/i.test(agent) ? 'opencode' : /claude-cli|claude-code/i.test(agent) ? 'claude-code' : 'unknown');
  const body = sample.body || {};
  const system = typeof body.system === 'string' ? body.system
    : Array.isArray(body.system) ? body.system.map((block) => block.text || '').join('\n')
      : (body.messages || []).filter((m) => m.role === 'system').map((m) => m.content).join('\n');
  return {
    dialect: sample.dialect || 'chat', client, agent: '', model: body.model || '', purpose: headers['x-test-purpose'] || (body.tools?.length ? 'client-tools' : 'client-text'),
    request_class: headers['x-claude-code-request-class'] || '', tools: Boolean(body.tools?.length), structured: false, has_client_system: Boolean(system.trim()),
    header_profile: headers['x-proxi-profile'] || '', system, tool_names: (body.tools || []).map((tool) => tool.name || tool.function?.name),
    blocks: (body.messages || []).flatMap((m) => (typeof m.content === 'string' ? [m.content] : (m.content || []).map((b) => b.text || ''))),
  };
}

export function simulate(sample, doc) {
  const request = requestOf(sample);
  const protectedOn = doc ? doc.protected?.enabled !== false : true;
  let rule = null;
  let profile = null;
  let isProtected = false;
  if (protectedOn && PROTECTED_RULES.some((item) => ruleMatches(item, request))) {
    rule = PROTECTED_RULES.find((item) => ruleMatches(item, request)).name;
    profile = 'passthrough';
    isProtected = true;
  } else if (doc) {
    const found = (doc.rules || []).find((item) => item.enabled !== false && ruleMatches(item, request));
    if (found) { rule = found.name; profile = found.use === '$header' ? request.header_profile : found.use; }
    else if (doc.default_profile) profile = doc.default_profile;
  }
  const result = { rule, profile, protected: isProtected, purpose: request.purpose, client: request.client, model: request.model,
    history_changes: [], tools_changes: [], edits_unmatched: [], warnings: [] };
  if (!profile) return { ...result, head: '', parts: [], head_bytes: 0, tokens_estimate: 0 };
  const spec = profile === 'passthrough' ? { head: '{{client_system}}' } : doc.profiles[profile];
  if (!spec) return { ...result, profile, head: '', parts: [], head_bytes: 0, tokens_estimate: 0, warnings: [`profile ${profile} not found`] };
  let system = request.system;
  let billing = '';
  if (system.startsWith('x-anthropic-billing-header:')) {
    [billing, system] = [system.split('\n')[0], system.split('\n').slice(1).join('\n')];
  }
  const edits = profile === 'passthrough' ? [] : spec.client_system?.edits || [];
  const applied = applySystemEdits(system, edits);
  const composed = composeHead(spec.head && typeof spec.head === 'string' ? spec.head : '', {
    system: applied.text, raw: system, edited: applied.text !== system, billing, request, texts: doc?.texts || {},
  });
  const toolEdits = spec.tools?.edits || [];
  for (const edit of toolEdits) {
    if (request.tool_names.some((name) => matchesValue(name, edit.tool))) result.tools_changes.push({ tool: edit.tool, op: edit.hide ? 'hide' : edit.op, chars: edit.text?.length || 0 });
    else result.edits_unmatched.push({ scope: 'tools', tool: edit.tool, op: edit.hide ? 'hide' : edit.op });
  }
  for (const block of spec.history?.user_blocks || []) {
    const hits = request.blocks.filter((text) => text.includes(`<${block.tag}`) && (!block.contains || text.includes(block.contains))).length;
    if (hits) result.history_changes.push({ op: block.op, tag: block.tag, count: hits });
    else result.edits_unmatched.push({ scope: 'history.user_blocks', op: block.op, tag: block.tag });
  }
  result.edits_unmatched.unshift(...applied.unmatched);
  return { ...result, ...composed, warnings: [...composed.warnings], head_bytes: bytes(composed.head), tokens_estimate: Math.ceil(bytes(composed.head) / 4) };
}

// --- the recent requests the relay holds ---------------------------------------------------------------------------

const requestBody = (system, model, extra = {}) => ({ model, system, tools: [{ name: 'bash' }, { name: 'webfetch' }], max_tokens: 4096,
  messages: [{ role: 'user', content: [{ type: 'text', text: 'привет' }, { type: 'text', text: '<system-reminder>Plan mode is active</system-reminder>' }] }], ...extra });

export function recentRequests() {
  const opencode = { 'user-agent': 'opencode/1.4.2', 'x-session-affinity': 'ses_a1b2' };
  return [
    { request_id: 'req-open-2', ts: NOW - 20, conversation: 'c-7f2a', rev: 3, pinned: true,
      sample: { body: requestBody(SYSTEM_OPENCODE.replace('/home/dev/project', '/home/dev/other'), 'claude-sonnet-5-5'), dialect: 'anthropic', headers: opencode } },
    { request_id: 'req-open-1', ts: NOW - 140, conversation: 'c-7f2a', rev: 3, pinned: true,
      sample: { body: requestBody(SYSTEM_OPENCODE, 'claude-sonnet-5-5'), dialect: 'anthropic', headers: opencode } },
    { request_id: 'req-open-0', ts: NOW - 200, conversation: 'c-7f2a', rev: 3, pinned: true,
      sample: { body: requestBody(SYSTEM_OPENCODE, 'claude-sonnet-5-5'), dialect: 'anthropic', headers: opencode } },
    { request_id: 'req-cc-1', ts: NOW - 300, conversation: 'c-91d0', rev: 3, pinned: false,
      sample: { body: requestBody(SYSTEM_CLAUDE_CODE, 'claude-opus-5-5'), dialect: 'anthropic', headers: { 'user-agent': 'claude-cli/1.0.3' } } },
    { request_id: 'req-title-1', ts: NOW - 420, conversation: 'c-7f2a', rev: 3, pinned: false, keep: false,
      sample: { body: requestBody('Generate a title', 'claude-haiku-4-5', { tools: [] }), dialect: 'anthropic', headers: { 'user-agent': 'opencode/1.4.2', 'x-test-purpose': 'session-title' } } },
  ];
}

// --- the route --------------------------------------------------------------------------------------------------

export async function mockPrompts(page, options = {}) {
  const { legacy = false, errors = [], builtin = BUILTIN, previewDelay = 0, document: seed, recent, listWarnings = true } = options;
  const state = {
    doc: legacy ? null : (seed || initialDocument()), rev: legacy ? null : 3, path: 'C:\\relay\\prompts.json', errors, saves: [], previews: [],
    pinActions: [], requests: [], saveFails: null, previewDelay,
    recent: recent || recentRequests(),
    pins: [
      { conversation: 'c-7f2a', purpose: 'client-tools', rev: 2, profile: 'mine-first', age_s: 5400 },
      { conversation: 'c-91d0', purpose: 'client-tools', rev: 3, profile: 'mine-first', age_s: 240 },
    ],
  };
  const recentItems = () => state.recent.map((entry, index) => {
    const outcome = simulate(entry.sample, state.doc);
    return { ts: entry.ts, request_id: entry.request_id, client: outcome.client, agent: '', model: outcome.model, purpose: outcome.purpose, rule: outcome.rule,
      profile: outcome.profile, rev: entry.rev, pinned: entry.pinned, head: outcome.head, parts: outcome.parts,
      history_changes: outcome.history_changes, tools_changes: outcome.tools_changes, has_request: entry.keep !== false && index < 10 };
  });

  await page.route('**/admin/api/prompts**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const headers = request.headers();
    const reply = (status, json) => route.fulfill({ status, json }).catch(() => {});
    if (headers.authorization !== `Bearer ${API_KEY}`) return reply(401, { error: { message: 'invalid relay API key', code: 'unauthorized' } });
    state.requests.push({ path: url.pathname, method, headers });
    const path = url.pathname.replace('/admin/api/prompts', '') || '/';
    if (path === '/' && method === 'GET') {
      return reply(200, { document: state.doc, rev: state.rev, path: state.path, errors: state.errors, ...(listWarnings ? { warnings: warningsFor(state.doc) } : {}),
        legacy: { mode: 'standalone', role_chars: BUILTIN_TEXTS.role.length }, builtin });
    }
    if (path === '/' && method === 'PUT') {
      const body = request.postDataJSON();
      state.saves.push({ body, headers });
      if (state.saveFails) return reply(state.saveFails.status, state.saveFails.json);
      if (body.base_rev !== state.rev) return reply(409, { error: { message: 'prompts document changed: reload it', type: 'conflict', param: null } });
      const invalid = validateDocument(body.document);
      if (invalid) return reply(400, { error: { message: invalid.message, param: invalid.param, type: 'invalid_request_error' } });
      state.doc = body.document;
      state.rev = (state.rev ?? 0) + 1;
      return reply(200, { rev: state.rev, warnings: warningsFor(state.doc) });
    }
    if (path === '/preview' && method === 'POST') {
      const body = request.postDataJSON();
      state.previews.push({ body, at: Date.now() });
      const delay = typeof state.previewDelay === 'function' ? state.previewDelay(body) : state.previewDelay;
      if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
      const doc = body.document === undefined ? state.doc : body.document;
      if (doc) {
        const invalid = validateDocument(doc);
        if (invalid) return reply(400, { error: { message: invalid.message, param: invalid.param, type: 'invalid_request_error' } });
      }
      let sample;
      if (body.request_id) {
        const entry = state.recent.find((item) => item.request_id === body.request_id);
        if (!entry || entry.keep === false) return reply(404, { error: { message: 'request body is no longer kept', type: 'not_found_error' } });
        sample = entry.sample;
      } else {
        sample = { body: body.request, dialect: body.dialect, headers: body.headers };
      }
      const outcome = simulate(sample, doc);
      return reply(200, { ...outcome, rev: body.document === undefined ? state.rev : null });
    }
    if (path === '/recent' && method === 'GET') {
      const limit = Number(url.searchParams.get('limit')) || 50;
      return reply(200, { items: recentItems().slice(0, limit) });
    }
    if (path === '/pins' && method === 'GET') return reply(200, { items: state.pins });
    if (path === '/pins' && method === 'POST') {
      const body = request.postDataJSON();
      state.pinActions.push({ body, headers });
      // as the relay does: unpin names a conversation, apply_all names none
      if (body.action === 'unpin' && !body.conversation) return reply(400, { error: { message: 'unpin needs a conversation', param: 'conversation' } });
      if (body.action === 'apply_all' && body.conversation) return reply(400, { error: { message: 'apply_all takes no conversation', param: 'conversation' } });
      const before = state.pins.length;
      if (body.action === 'apply_all') state.pins = [];
      else state.pins = state.pins.filter((pin) => pin.conversation !== body.conversation);
      return reply(200, { unpinned: before - state.pins.length });
    }
    return reply(404, { error: { message: 'Unknown admin endpoint' } });
  });
  return state;
}
