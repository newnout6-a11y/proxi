// The prompts document as the editor holds it: plain JSON updated immutably, so unchanged parts keep their
// identity and comparing the draft with the saved document stays cheap. Unknown fields are never dropped.
const ids = new WeakMap();
let counter = 0;

/** A stable key for an item of a list (rule, edit): follows the object through moves and edits. */
export function idOf(item) {
  if (!item || typeof item !== 'object') return String(item);
  let id = ids.get(item);
  if (!id) {
    counter += 1;
    id = `i${counter}`;
    ids.set(item, id);
  }
  return id;
}

export function inherit(next, previous) {
  if (previous && typeof previous === 'object' && next && typeof next === 'object') ids.set(next, idOf(previous));
  return next;
}

export const PROFILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const BUILTIN_PROFILE = 'passthrough';
export const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export function equalJson(a, b) {
  if (a === b) return true;
  if (!a || !b || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  if (keys.length !== Object.keys(b).length) return false;
  return keys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && equalJson(a[key], b[key]));
}

// --- lists ---------------------------------------------------------------------------------------------------

export function moveItem(list, from, to) {
  if (from === to || from < 0 || to < 0 || from >= list.length || to >= list.length) return list;
  const next = list.slice();
  const [item] = next.splice(from, 1);
  next.splice(to, 0, item);
  return next;
}

export const removeAt = (list, index) => list.filter((_, i) => i !== index);

export const insertAt = (list, index, item) => [...list.slice(0, index), item, ...list.slice(index)];

/** Replace one item by `patch` (object, merged) or by what `patch(item)` returns, keeping its key. */
export const updateAt = (list, index, patch) => list.map((item, i) => {
  if (i !== index) return item;
  return inherit(typeof patch === 'function' ? patch(item) : { ...item, ...patch }, item);
});

// --- document ------------------------------------------------------------------------------------------------

export const blankDocument = () => ({ version: 1, profiles: {}, rules: [] });

export const profilesOf = (doc) => (isObject(doc?.profiles) ? doc.profiles : {});
export const rulesOf = (doc) => (Array.isArray(doc?.rules) ? doc.rules : []);
export const textsOf = (doc) => (isObject(doc?.texts) ? doc.texts : {});
export const protectedEnabled = (doc) => doc?.protected?.enabled !== false;

/** `pattern` is the relay's own rule when it published one (builtin.profile_name), else the documented one. */
export function profileNameError(name, taken = [], pattern = PROFILE_NAME) {
  const value = String(name ?? '');
  if (!value) return 'Введите имя профиля';
  if (value === BUILTIN_PROFILE) return 'Имя passthrough занято: это встроенный профиль';
  if (!pattern.test(value)) return 'Имя: латинские буквы, цифры, точка, дефис и подчёркивание, до 64 знаков, начинается с буквы или цифры';
  if (taken.includes(value)) return 'Профиль с таким именем уже есть';
  return null;
}

export function textNameError(name, taken = []) {
  const value = String(name ?? '');
  if (!value) return 'Введите имя текста';
  if (/[\s{}:]/.test(value)) return 'В имени нет пробелов, фигурных скобок и двоеточий';
  if (taken.includes(value)) return 'Текст с таким именем уже есть';
  return null;
}

// A section of a profile that says nothing is dropped, so the saved file holds only what the user chose.
export function pruneProfile(profile) {
  const next = { ...profile };
  const empty = (value) => value === undefined || value === null || value === '' || value === false
    || (Array.isArray(value) && !value.length) || (isObject(value) && !Object.keys(value).length);
  if (next.client_system) {
    const { edits, ...rest } = next.client_system;
    next.client_system = empty(edits) ? rest : { ...rest, edits };
  }
  if (next.history) {
    const { mid_system: mode, user_blocks: blocks, ...rest } = next.history;
    next.history = { ...rest, ...(mode && mode !== 'keep' ? { mid_system: mode } : {}), ...(empty(blocks) ? {} : { user_blocks: blocks }) };
  }
  if (next.tools) {
    const { edits, ...rest } = next.tools;
    next.tools = empty(edits) ? rest : { ...rest, edits };
  }
  if (next.generation) {
    next.generation = Object.fromEntries(Object.entries(next.generation).filter(([, value]) => value !== undefined && value !== null && value !== ''));
  }
  for (const key of ['client_system', 'history', 'tools', 'generation', 'description', 'publish_as_model']) if (empty(next[key])) delete next[key];
  return next;
}

export const patchProfile = (doc, name, patch) => ({
  ...doc,
  profiles: { ...profilesOf(doc), [name]: pruneProfile({ ...profilesOf(doc)[name], ...patch }) },
});

export function addProfile(doc, name, profile) {
  return { ...doc, profiles: { ...profilesOf(doc), [name]: profile } };
}

/** Where a profile is named: rules that use it and the default. */
export function profileUsage(doc, name) {
  const rules = rulesOf(doc).map((rule, index) => ({ rule, index })).filter(({ rule }) => rule?.use === name);
  return { rules, isDefault: doc?.default_profile === name };
}

export function removeProfile(doc, name) {
  const { [name]: _gone, ...rest } = profilesOf(doc);
  return { ...doc, profiles: rest };
}

function renameKey(object, from, to) {
  return Object.fromEntries(Object.entries(object).map(([key, value]) => [key === from ? to : key, value]));
}

/** Rename a profile everywhere the document names it; the position among the profiles stays. */
export function renameProfile(doc, from, to) {
  const next = { ...doc, profiles: renameKey(profilesOf(doc), from, to) };
  if (Array.isArray(doc.rules)) {
    next.rules = doc.rules.map((rule) => (rule?.use === from ? inherit({ ...rule, use: to }, rule) : rule));
  }
  if (doc.default_profile === from) next.default_profile = to;
  return next;
}

export function renameText(doc, from, to) {
  const next = { ...doc, texts: renameKey(textsOf(doc), from, to) };
  const needle = `{{text:${from}}}`;
  const swap = (value) => {
    if (typeof value === 'string') return value.includes(needle) ? value.split(needle).join(`{{text:${to}}}`) : value;
    if (Array.isArray(value)) {
      const mapped = value.map(swap);
      return mapped.every((item, i) => item === value[i]) ? value : mapped;
    }
    if (isObject(value)) {
      const entries = Object.entries(value);
      const mapped = entries.map(([key, item]) => [key, swap(item)]);
      return mapped.every(([, item], i) => item === entries[i][1]) ? value : inherit(Object.fromEntries(mapped), value);
    }
    return value;
  };
  next.profiles = swap(profilesOf(doc));
  return next;
}

/** Profiles whose head or edits mention {{text:name}}. */
export function textUsage(doc, name) {
  const needle = `{{text:${name}}}`;
  return Object.entries(profilesOf(doc)).filter(([, profile]) => JSON.stringify(profile).includes(needle)).map(([key]) => key);
}

export function removeText(doc, name) {
  const { [name]: _gone, ...rest } = textsOf(doc);
  return { ...doc, texts: rest };
}

/** relay_texts[name]: null means the built-in text; the key is dropped when every one is null. */
export function setRelayText(doc, name, value) {
  const next = { ...(isObject(doc.relay_texts) ? doc.relay_texts : {}), [name]: value };
  if (value === null) delete next[name];
  const out = { ...doc, relay_texts: next };
  if (!Object.keys(next).length) delete out.relay_texts;
  return out;
}

export function setProtected(doc, enabled) {
  const next = { ...doc };
  if (enabled) {
    const { enabled: _on, ...rest } = isObject(doc.protected) ? doc.protected : {};
    if (Object.keys(rest).length) next.protected = rest;
    else delete next.protected;
  } else {
    next.protected = { ...(isObject(doc.protected) ? doc.protected : {}), enabled: false };
  }
  return next;
}

export function newRule(use = '') {
  return { name: 'Новое правило', if: {}, use };
}

/** Set one field of a rule; `enabled: true` and `override_protected: false` are the defaults and are not written. */
export function setRuleField(rule, key, value) {
  const next = { ...rule };
  const drop = value === undefined || value === null || (key === 'enabled' && value === true) || (key === 'override_protected' && value === false);
  if (drop) delete next[key];
  else next[key] = value;
  return next;
}

/** The index of the first enabled rule that matches every request; whatever follows it can never be reached. */
export function catchAllIndex(rules) {
  const matchesAll = (rule) => !rule?.if || (isObject(rule.if) && !Object.keys(rule.if).length);
  return rules.findIndex((rule) => rule?.enabled !== false && matchesAll(rule));
}

// What a new profile starts from; {{client_system}}{{relay_task}}{{exec_reminder}} keeps both kinds of request
// as they are today: the client's own system, or the task the relay builds when the client sends none.
const KEEP = '{{client_system}}{{relay_task}}{{exec_reminder}}';
export const STARTERS = [
  { id: 'same', label: 'Как сейчас', hint: 'System клиента и задачи relay без изменений: точка отсчёта для правок', head: KEEP },
  { id: 'rules-first', label: 'Мои правила в начале', hint: 'Свой текст перед system клиента', head: `Отвечай по-русски. Не трогай файлы без явной просьбы.\n\n${KEEP}` },
  { id: 'exact', label: 'Только мой текст', hint: 'System клиента выбрасывается, инструменты и заголовки продолжают работать', head: 'Ты — ассистент по программированию. Отвечай кратко и проверяй изменения.' },
];

export function starterProfile(id) {
  const starter = STARTERS.find((item) => item.id === id) || STARTERS[0];
  return { head: starter.head };
}

/** `base`, or `base-2`, `base-3` … when the name is taken. */
export function uniqueName(base, taken) {
  if (!taken.includes(base)) return base;
  let n = 2;
  while (taken.includes(`${base}-${n}`)) n += 1;
  return `${base}-${n}`;
}

// --- what changed ----------------------------------------------------------------------------------------------

const GROUPED = ['profiles', 'texts', 'relay_texts'];
const blank = (value) => value === undefined || value === null || (Array.isArray(value) && !value.length)
  || (isObject(value) && !Object.keys(value).length);
const same = (a, b) => equalJson(a, b) || (blank(a) && blank(b));

/** Paths of the edits that differ from the saved document: sections, and one entry per profile or text. */
export function changedPaths(saved, draft) {
  if (saved === draft) return [];
  const before = isObject(saved) ? saved : {};
  const after = isObject(draft) ? draft : {};
  const paths = [];
  for (const key of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (key === 'version' || same(before[key], after[key])) continue;
    if (GROUPED.includes(key)) {
      const was = isObject(before[key]) ? before[key] : {};
      const now = isObject(after[key]) ? after[key] : {};
      for (const name of new Set([...Object.keys(was), ...Object.keys(now)])) {
        if (!same(was[name], now[name])) paths.push(`${key}.${name}`);
      }
    } else {
      paths.push(key);
    }
  }
  return paths;
}

const SECTION_LABELS = { rules: 'правила', default_profile: 'профиль по умолчанию', protected: 'защита служебных запросов', codex: 'Codex' };

export function pathLabel(path) {
  if (SECTION_LABELS[path]) return SECTION_LABELS[path];
  const [group, ...rest] = path.split('.');
  const name = rest.join('.');
  if (group === 'profiles') return `профиль ${name}`;
  if (group === 'texts') return `текст ${name}`;
  if (group === 'relay_texts') return `текст relay: ${name}`;
  return path;
}

// --- errors from the relay -----------------------------------------------------------------------------------------

/** "profiles.mine.client_system.edits[0].heading" and "...edits.0.heading" are the same place. */
export const normalizePath = (param) => String(param ?? '').replace(/\[(\d+)\]/g, '.$1').replace(/^\./, '');

/** Does an error at `param` belong to the control at `path` (the control itself or something inside it)? */
export function errorHits(param, path) {
  const error = normalizePath(param);
  const own = normalizePath(path);
  return !!error && !!own && (error === own || error.startsWith(`${own}.`));
}

/** Where a path lives in the screen: the tab, and the profile, rule or text it points into. */
export function locate(param) {
  const parts = normalizePath(param).split('.');
  const [group, name, index] = parts;
  if (group === 'profiles' && name) return { tab: 'profiles', profile: name };
  if (group === 'rules') return { tab: 'rules', rule: /^\d+$/.test(name || '') ? Number(name) : null };
  if (group === 'default_profile' || group === 'protected') return { tab: 'rules', rule: null };
  if (group === 'texts' || group === 'relay_texts' || group === 'codex') return { tab: 'texts', text: name || null, index };
  return { tab: null };
}
