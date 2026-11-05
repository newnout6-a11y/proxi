import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_TEXT_BYTES, insertAt as insertText, templateProblems, tokenize, utf8Bytes, variableNames, problemText } from '../src/prompts/template.js';
import { diffLines, diffStats, foldDiff } from '../src/prompts/diff.js';
import { normalizeParts, segmentHead, sourceKind, sourceLabel, summarizeKinds } from '../src/prompts/parts.js';
import { describeEdit, flagsOf, joinFlags, newEdit, normalizeEditOps, opOf, setEditField, switchOp } from '../src/prompts/ops.js';
import { CONDITION_SPECS, conditionText, fromValues, normalizeConditions, setCondition, summarizeConditions, valuesOf } from '../src/prompts/conditions.js';
import {
  addProfile, blankDocument, catchAllIndex, changedPaths, equalJson, errorHits, idOf, insertAt, locate, moveItem, newRule, normalizePath, patchProfile,
  pathLabel, profileNameError, profileUsage, protectedEnabled, pruneProfile, removeAt, removeProfile, removeText, renameProfile, renameText,
  setProtected, setRelayText, setRuleField, STARTERS, starterProfile, textNameError, textUsage, uniqueName, updateAt,
} from '../src/prompts/draft.js';
import {
  BASELINE_DOCUMENT, DEFAULT_VARIABLES, MAX_DOCUMENT_BYTES, RELAY_TASK_VARIABLES, compileProfileName, detectDialect, failureLead, failureOf, forceProfile,
  normalizeBuiltin, normalizeLimits, normalizePins, normalizePreview, normalizePrompts, normalizeRecent, normalizeSaved, normalizeSpecialUses,
  normalizeVariables, parseBody, parseHeaders, previewRequest,
} from '../src/prompts/api.js';
import { describeChange } from '../src/prompts/changes.js';
import { emptySample, newestSample, recentLabel, resolveSample } from '../src/prompts/sample.js';

// --- template ------------------------------------------------------------------------------------------------

test('a template is split into text, escaped braces and variables with their offsets', () => {
  const template = 'Rules\n{{ text:mine }}\n\\{{literal}} {{client_system}}';
  const tokens = tokenize(template);
  assert.deepEqual(tokens.map((token) => token.type), ['text', 'var', 'text', 'escape', 'text', 'var']);
  assert.deepEqual(variableNames(template), ['text:mine', 'client_system']);
  const escape = tokens.find((token) => token.type === 'escape');
  assert.equal(template.slice(escape.start, escape.end), '\\{{');
  assert.equal(tokenize('').length, 0);
  // the pieces cover the template with no gaps
  assert.equal(tokens.map((token) => template.slice(token.start, token.end)).join(''), template);
});

test('unknown variables and missing named texts are the references the relay would refuse', () => {
  const known = ['client_system', 'role'];
  const problems = templateProblems('{{client_system}} {{typo}} {{text:mine}} {{text:gone}} {{text:}} \\{{typo2}}', known, ['mine']);
  assert.deepEqual(problems.map((problem) => [problem.kind, problem.name]), [['unknown', 'typo'], ['missing_text', 'text:gone'], ['unnamed', 'text:']]);
  assert.equal(problemText(problems[0]), 'Неизвестная переменная {{typo}}');
  assert.match(problemText(problems[1]), /Нет именованного текста «gone»/);
  assert.deepEqual(templateProblems('plain text, no references', known, []), []);
});

test('inserting a variable replaces the selection and puts the caret after it', () => {
  assert.deepEqual(insertText('ab', 1, 1, '{{x}}'), { value: 'a{{x}}b', caret: 6 });
  assert.deepEqual(insertText('abcd', 1, 3, '{{x}}'), { value: 'a{{x}}d', caret: 6 });
  assert.deepEqual(insertText('ab', undefined, undefined, '!'), { value: 'ab!', caret: 3 });
  assert.deepEqual(insertText('ab', 9, 9, '!'), { value: 'ab!', caret: 3 });
  assert.deepEqual(insertText('', 0, 0, '{{role}}'), { value: '{{role}}', caret: 8 });
});

test('the byte counter counts UTF-8 like the relay: Cyrillic takes two, emoji four', () => {
  assert.equal(utf8Bytes('abc'), 3);
  assert.equal(utf8Bytes('жж'), 4);
  assert.equal(utf8Bytes('😀'), 4);
  assert.equal(utf8Bytes(undefined), 0);
  assert.equal(MAX_TEXT_BYTES, 262144);
});

// --- diff ------------------------------------------------------------------------------------------------------

const kinds = (rows) => rows.map((row) => `${{ same: ' ', add: '+', del: '-' }[row.type]}${row.text}`);

test('identical texts and empty texts produce plain rows', () => {
  assert.deepEqual(kinds(diffLines('a\nb', 'a\nb')), [' a', ' b']);
  assert.deepEqual(diffLines('', ''), []);
  assert.deepEqual(kinds(diffLines('', 'a\nb')), ['+a', '+b']);
  assert.deepEqual(kinds(diffLines('a\nb', '')), ['-a', '-b']);
  assert.equal(diffStats(diffLines('same', 'same')).changed, false);
});

test('a removed section shows as deleted lines between unchanged ones', () => {
  const before = '# Role\nbe brief\n# Code comments\nno comments\nreally\n# Tools\nuse tools';
  const after = '# Role\nbe brief\n# Tools\nuse tools';
  const rows = diffLines(before, after);
  assert.deepEqual(kinds(rows), [' # Role', ' be brief', '-# Code comments', '-no comments', '-really', ' # Tools', ' use tools']);
  assert.deepEqual(diffStats(rows), { added: 0, removed: 3, same: 4, changed: true });
});

test('a changed line is a deletion followed by an addition, with line numbers on each side', () => {
  const rows = diffLines('one\ntwo\nthree', 'one\n2\nthree\nfour');
  assert.deepEqual(kinds(rows), [' one', '-two', '+2', ' three', '+four']);
  assert.deepEqual(rows.map((row) => [row.before, row.after]), [[1, 1], [2, null], [null, 2], [3, 3], [null, 4]]);
});

test('the diff keeps order when lines repeat and reproduces both sides', () => {
  const before = 'a\nb\na\nb\nc';
  const after = 'b\na\nb\nc\nc';
  const rows = diffLines(before, after);
  const rebuild = (side) => rows.filter((row) => row.type === 'same' || row.type === side).map((row) => row.text).join('\n');
  assert.equal(rebuild('del'), before);
  assert.equal(rebuild('add'), after);
});

test('a middle part too large for the table is shown as removed and added instead of guessed', () => {
  const a = Array.from({ length: 2100 }, (_, i) => `a${i}`).join('\n');
  const b = Array.from({ length: 2100 }, (_, i) => `b${i}`).join('\n');
  const rows = diffLines(`start\n${a}\nend`, `start\n${b}\nend`);
  assert.equal(rows[0].type, 'same');
  assert.equal(rows.at(-1).type, 'same');
  assert.deepEqual(diffStats(rows), { added: 2100, removed: 2100, same: 2, changed: true });
});

test('a large head with one changed line diffs quickly', () => {
  const lines = Array.from({ length: 20000 }, (_, i) => `line ${i}`);
  const changed = lines.slice();
  changed[10000] = 'edited';
  const started = performance.now();
  const rows = diffLines(lines.join('\n'), changed.join('\n'));
  assert.ok(performance.now() - started < 500);
  assert.deepEqual(diffStats(rows), { added: 1, removed: 1, same: 19999, changed: true });
});

test('unchanged stretches fold away except the context around a change', () => {
  const lines = Array.from({ length: 30 }, (_, i) => `l${i}`);
  const changed = lines.slice();
  changed[15] = 'x';
  const folded = foldDiff(diffLines(lines.join('\n'), changed.join('\n')), 2);
  assert.deepEqual(folded.map((row) => row.type), ['fold', 'same', 'same', 'del', 'add', 'same', 'same', 'fold']);
  assert.equal(folded[0].rows.length, 13);
  assert.equal(folded.at(-1).rows.length, 12);
  // two unchanged lines are not worth a fold, three are
  assert.deepEqual(foldDiff(diffLines('a\nb\nc\nd\ne\nf', 'x\nb\nc\nd\ne\ny'), 1).map((row) => row.type), ['del', 'add', 'same', 'same', 'same', 'same', 'del', 'add']);
  assert.deepEqual(foldDiff(diffLines('a\nb\nc\nd\ne\nf\ng', 'x\nb\nc\nd\ne\nf\ny'), 1).map((row) => row.type), ['del', 'add', 'same', 'fold', 'same', 'del', 'add']);
  assert.deepEqual(foldDiff([], 2), []);
});

// --- parts -----------------------------------------------------------------------------------------------------

test('every source is told apart by kind and named for people', () => {
  assert.deepEqual(['client', 'client_edited', 'text:mine', 'relay:role', 'literal', 'billing', 'weird'].map(sourceKind),
    ['client', 'edited', 'text', 'relay', 'literal', 'billing', 'other']);
  assert.equal(sourceLabel('text:my_rules'), 'текст «my_rules»');
  assert.equal(sourceLabel('relay:native_role'), 'роль для клиента с tools');
  assert.equal(sourceLabel('relay:future'), 'relay: future');
  assert.equal(sourceLabel('client_edited'), 'system клиента после правок');
});

test('parts come as spans, as [source, chars] pairs or with offsets missing', () => {
  assert.deepEqual(normalizeParts([{ source: 'literal', start: 0, end: 3, chars: 3, sha8: 'abc' }, { source: 'client', start: 3, end: 10 }]),
    [{ source: 'literal', start: 0, end: 3, chars: 3, sha8: 'abc' }, { source: 'client', start: 3, end: 10, chars: 7, sha8: '' }]);
  assert.deepEqual(normalizeParts([['literal', 3], ['client', 7]]).map((part) => [part.source, part.start, part.end]), [['literal', 0, 3], ['client', 3, 10]]);
  assert.deepEqual(normalizeParts([{ source: 'text:x', chars: 2 }, { source: 'client', chars: 4 }]).map((part) => [part.start, part.end]), [[0, 2], [2, 6]]);
  assert.deepEqual(normalizeParts(null), []);
  assert.deepEqual(normalizeParts([{ nope: 1 }, 'x']), []);
});

test('the head is cut where the relay cut it, counting code points rather than UTF-16 units', () => {
  const head = '😀 rules\n\nsystem ж';
  const segments = segmentHead(head, [{ source: 'literal', start: 0, end: 8 }, { source: 'client', start: 8, end: 17 }]);
  assert.deepEqual(segments.map((segment) => [segment.source, segment.text]), [['literal', '😀 rules\n'], ['client', '\nsystem ж']]);
  assert.equal(segments.map((segment) => segment.text).join(''), head);
});

test('text no part covers stays in the head as "other", and overlaps never repeat text', () => {
  const head = 'abcdefghij';
  assert.deepEqual(segmentHead(head, [{ source: 'client', start: 2, end: 5 }]).map((s) => [s.source, s.text]),
    [['other', 'ab'], ['client', 'cde'], ['other', 'fghij']]);
  assert.deepEqual(segmentHead(head, [{ source: 'client', start: 0, end: 6 }, { source: 'literal', start: 4, end: 10 }]).map((s) => [s.source, s.text]),
    [['client', 'abcdef'], ['literal', 'ghij']]);
  assert.deepEqual(segmentHead(head, []).map((s) => [s.source, s.text]), [['other', head]]);
  assert.deepEqual(segmentHead('', [{ source: 'client', start: 0, end: 3 }]), []);
  assert.equal(segmentHead(head, [{ source: 'client', start: 0, end: 99 }]).map((s) => s.text).join(''), head);
});

test('the legend counts characters per kind in the order the kinds appear', () => {
  const segments = segmentHead('aabbbc', [{ source: 'literal', start: 0, end: 2 }, { source: 'client', start: 2, end: 5 }, { source: 'literal', start: 5, end: 6 }]);
  assert.deepEqual(summarizeKinds(segments), [
    { kind: 'literal', label: 'текст шаблона', chars: 3, parts: 2 },
    { kind: 'client', label: 'system клиента', chars: 3, parts: 1 },
  ]);
});

// --- edit operations -------------------------------------------------------------------------------------------

test('the operations the relay offers come from several plausible shapes and fall back to the full list', () => {
  const full = normalizeEditOps(undefined);
  assert.ok(full.client_system.includes('remove_section'));
  assert.deepEqual(full.mid_system, ['keep', 'wrap', 'plain', 'drop']);
  assert.deepEqual(normalizeEditOps({ client_system: ['remove_tag'], tools: [{ op: 'hide' }], user_blocks: { drop_tag: {} }, mid_system: ['keep', 'drop'] }),
    { ...full, client_system: ['remove_tag'], tools: ['hide'], user_blocks: ['drop_tag'], mid_system: ['keep', 'drop'] });
  assert.deepEqual(normalizeEditOps({ history: { mid_system: ['drop'], user_blocks: ['remove_regex'] } }),
    { ...full, mid_system: ['drop'], user_blocks: ['remove_regex'] });
  const flat = normalizeEditOps({ remove_section: {}, drop_tag: {}, set: {} });
  assert.deepEqual([flat.client_system, flat.user_blocks, flat.tools], [['remove_section'], ['drop_tag'], ['set']]);
  assert.deepEqual(normalizeEditOps([]), full);
  assert.deepEqual(normalizeEditOps({ client_system: [] }), full);
});

test('an edit is described in one line, with the tool first', () => {
  assert.equal(describeEdit('client_system', { op: 'remove_section', heading: '# Code comments' }), 'Убрать раздел: # Code comments');
  assert.equal(describeEdit('client_system', { op: 'replace_text', find: 'old', with: 'new', all: true }), 'Заменить текст: old → new');
  assert.equal(describeEdit('tools', { tool: 'bash', op: 'append', text: 'Ask first.' }), 'Добавить в конец описания: bash → Ask first.');
  assert.equal(describeEdit('tools', { tool: 'webfetch', hide: true }), 'Скрыть инструмент: webfetch');
  assert.equal(describeEdit('client_system', { op: 'keep_sections', headings: ['# A', '# B'] }), 'Оставить только разделы: # A, # B');
  assert.equal(describeEdit('client_system', { op: 'mystery' }), 'mystery');
  assert.equal(describeEdit('client_system', null), '');
  assert.match(describeEdit('client_system', { op: 'prepend', text: 'x'.repeat(200) }), /…$/);
});

test('a new edit starts empty, switching the operation keeps the shared fields, and emptied fields leave the edit', () => {
  assert.deepEqual(newEdit('client_system', 'remove_tag'), { op: 'remove_tag' });
  assert.deepEqual(newEdit('tools', 'append'), { tool: '', op: 'append' });
  assert.deepEqual(newEdit('tools', 'hide'), { tool: '', hide: true });
  assert.equal(opOf('tools', { tool: 'x', hide: true }), 'hide');
  assert.equal(opOf('tools', { tool: 'x', op: 'set' }), 'set');
  assert.deepEqual(switchOp('client_system', { op: 'remove_tag', tag: 'env' }, 'keep_tag_inner'), { op: 'keep_tag_inner', tag: 'env' });
  assert.deepEqual(switchOp('client_system', { op: 'prepend', text: 'hi' }, 'remove_tag'), { op: 'remove_tag' });
  assert.deepEqual(switchOp('tools', { tool: 'bash', op: 'append', text: 'hi' }, 'hide'), { tool: 'bash', hide: true });
  assert.deepEqual(setEditField({ op: 'replace_text', find: 'a', with: 'b', all: true }, 'all', false), { op: 'replace_text', find: 'a', with: 'b' });
  assert.deepEqual(setEditField({ op: 'remove_tag' }, 'tag', 'env'), { op: 'remove_tag', tag: 'env' });
  assert.deepEqual(setEditField({ tool: 'bash', op: 'append' }, 'tool', ''), { tool: '', op: 'append' });
  assert.deepEqual(setEditField({ op: 'keep_sections', headings: ['# A'] }, 'headings', []), { op: 'keep_sections' });
});

test('regex flags are a subset of i, m and s in a fixed order', () => {
  assert.deepEqual([...flagsOf('sx i')].sort(), ['i', 's']);
  assert.equal(joinFlags(new Set(['s', 'i'])), 'is');
  assert.equal(joinFlags(new Set()), '');
});

// --- conditions ------------------------------------------------------------------------------------------------

test('conditions fall back to the table of the plan and take values from the relay when it names them', () => {
  assert.deepEqual(normalizeConditions(undefined).map((spec) => spec.name), CONDITION_SPECS.map((spec) => spec.name));
  const merged = normalizeConditions({ client: { values: ['opencode', 'newclient'], description: 'кто прислал' }, tools: 'bool', custom_flag: { type: 'boolean' } });
  assert.deepEqual(merged.find((spec) => spec.name === 'client').values, ['opencode', 'newclient']);
  assert.equal(merged.find((spec) => spec.name === 'client').hint, 'кто прислал');
  assert.equal(merged.find((spec) => spec.name === 'tools').kind, 'bool');
  assert.equal(merged.find((spec) => spec.name === 'custom_flag').kind, 'bool');
  assert.deepEqual(normalizeConditions(['model', 'client']).slice(0, 2).map((spec) => spec.name), ['model', 'client']);
  assert.deepEqual(normalizeConditions({ dialect: ['chat', 'anthropic'] }).find((spec) => spec.name === 'dialect').values, ['chat', 'anthropic']);
});

test('one value is a string, several are a list meaning OR, none removes the condition', () => {
  assert.deepEqual(valuesOf('opencode'), ['opencode']);
  assert.deepEqual(valuesOf(['a', 'b']), ['a', 'b']);
  assert.deepEqual(valuesOf(undefined), []);
  assert.deepEqual(valuesOf(''), []);
  assert.equal(fromValues([]), undefined);
  assert.equal(fromValues(['a']), 'a');
  assert.deepEqual(fromValues(['a', 'b']), ['a', 'b']);
  assert.deepEqual(setCondition({ client: 'codex' }, 'tools', true), { client: 'codex', tools: true });
  assert.deepEqual(setCondition({ client: 'codex', tools: false }, 'tools', undefined), { client: 'codex' });
  assert.deepEqual(setCondition({ client: 'codex' }, 'client', []), {});
  assert.deepEqual(setCondition(undefined, 'client', 'x'), { client: 'x' });
});

test('conditions are summarised for the rule list; a rule without any matches every request', () => {
  assert.equal(summarizeConditions({}), 'любой запрос');
  assert.equal(summarizeConditions(undefined), 'любой запрос');
  assert.equal(summarizeConditions({ client: ['opencode', 'codex'], tools: true }), 'Клиент: opencode или codex · Есть инструменты: да');
  assert.equal(summarizeConditions({ structured: false }), 'Структурированный ответ: нет');
  assert.equal(summarizeConditions({ future: 'x' }), 'future: x');
  assert.equal(conditionText({ kind: 'bool' }, true), 'да');
});

// --- the document ---------------------------------------------------------------------------------------------

test('list helpers return new lists, keep the rest untouched and ignore impossible moves', () => {
  assert.deepEqual(moveItem(['a', 'b', 'c'], 0, 2), ['b', 'c', 'a']);
  assert.deepEqual(moveItem(['a', 'b', 'c'], 2, 1), ['a', 'c', 'b']);
  const same = ['a', 'b'];
  assert.equal(moveItem(same, 0, 0), same);
  assert.equal(moveItem(same, 0, 5), same);
  assert.equal(moveItem(same, -1, 0), same);
  assert.deepEqual(removeAt(['a', 'b', 'c'], 1), ['a', 'c']);
  assert.deepEqual(insertAt(['a', 'c'], 1, 'b'), ['a', 'b', 'c']);
  assert.deepEqual(insertAt([], 0, 'x'), ['x']);
});

test('an item keeps its key through edits and moves, a copy gets its own', () => {
  const rules = [newRule('a'), newRule('b'), newRule('c')];
  const keys = rules.map(idOf);
  assert.equal(new Set(keys).size, 3);
  const edited = updateAt(rules, 1, { name: 'renamed' });
  assert.equal(edited[1].name, 'renamed');
  assert.equal(idOf(edited[1]), keys[1]);
  assert.equal(edited[0], rules[0]);
  const moved = moveItem(edited, 1, 0);
  assert.deepEqual(moved.map(idOf), [keys[1], keys[0], keys[2]]);
  assert.equal(idOf(updateAt(rules, 0, (rule) => ({ ...rule, use: 'x' }))[0]), keys[0]);
  assert.notEqual(idOf({ ...rules[0] }), keys[0]);
  assert.equal(idOf('plain'), 'plain');
});

test('profile names follow the relay rule and the built-in name is taken', () => {
  assert.equal(profileNameError('mine-first', []), null);
  assert.equal(profileNameError('a.b_c-1', []), null);
  assert.match(profileNameError('', []), /Введите имя/);
  assert.match(profileNameError('passthrough', []), /встроенный/);
  assert.match(profileNameError('-bad', []), /латинские буквы/);
  assert.match(profileNameError('has space', []), /латинские буквы/);
  assert.match(profileNameError('мой', []), /латинские буквы/);
  assert.match(profileNameError('a'.repeat(65), []), /латинские буквы/);
  assert.equal(profileNameError('a'.repeat(64), []), null);
  assert.match(profileNameError('mine', ['mine']), /уже есть/);
  assert.match(profileNameError('$header', []), /латинские буквы/);
  assert.equal(textNameError('my_rules', []), null);
  assert.match(textNameError('my rules', []), /нет пробелов/);
  assert.match(textNameError('a:b', []), /нет пробелов/);
  assert.match(textNameError('x', ['x']), /уже есть/);
});

test('a profile section that says nothing is pruned, everything else is kept', () => {
  const profile = {
    head: '{{client_system}}', description: '', publish_as_model: false,
    client_system: { edits: [] }, history: { mid_system: 'keep', user_blocks: [] }, tools: { edits: [] },
    generation: { effort: null, verbosity: '' }, future_field: { keep: true },
  };
  assert.deepEqual(pruneProfile(profile), { head: '{{client_system}}', future_field: { keep: true } });
  const edits = [{ op: 'remove_tag', tag: 'env' }];
  assert.deepEqual(pruneProfile({ head: 'h', client_system: { edits }, history: { mid_system: 'drop' }, generation: { effort: 'high', verbosity: null }, publish_as_model: ['gpt-*'] }),
    { head: 'h', client_system: { edits }, history: { mid_system: 'drop' }, generation: { effort: 'high' }, publish_as_model: ['gpt-*'] });
  assert.equal(pruneProfile({ head: '' }).head, '');
  const doc = addProfile(blankDocument(), 'mine', { head: 'x' });
  assert.deepEqual(patchProfile(doc, 'mine', { generation: { effort: 'low' } }).profiles.mine, { head: 'x', generation: { effort: 'low' } });
  assert.deepEqual(patchProfile(doc, 'mine', { description: '' }).profiles.mine, { head: 'x' });
});

test('renaming a profile changes the rules and the default that name it and keeps the order of the profiles', () => {
  const rule = { name: 'r', if: {}, use: 'mine' };
  const doc = { version: 1, default_profile: 'mine', profiles: { a: { head: '1' }, mine: { head: '2' }, z: { head: '3' } }, rules: [rule, { name: 's', if: {}, use: 'a' }] };
  const keyBefore = idOf(rule);
  const next = renameProfile(doc, 'mine', 'mine2');
  assert.deepEqual(Object.keys(next.profiles), ['a', 'mine2', 'z']);
  assert.equal(next.default_profile, 'mine2');
  assert.deepEqual(next.rules.map((r) => r.use), ['mine2', 'a']);
  assert.equal(idOf(next.rules[0]), keyBefore);
  assert.equal(next.rules[1], doc.rules[1]);
  assert.deepEqual(doc.profiles.mine, { head: '2' });
  assert.deepEqual(profileUsage(doc, 'mine'), { rules: [{ rule, index: 0 }], isDefault: true });
  assert.deepEqual(profileUsage(doc, 'z'), { rules: [], isDefault: false });
  assert.deepEqual(Object.keys(removeProfile(doc, 'mine').profiles), ['a', 'z']);
});

test('renaming a named text follows its references in the profiles, and removal reports who used it', () => {
  const doc = { texts: { rules: 'be brief' }, profiles: { a: { head: '{{text:rules}}\n\n{{client_system}}', client_system: { edits: [{ op: 'append', text: '{{text:rules}}' }] } }, b: { head: 'x' } } };
  assert.deepEqual(textUsage(doc, 'rules'), ['a']);
  const next = renameText(doc, 'rules', 'brief');
  assert.deepEqual(Object.keys(next.texts), ['brief']);
  assert.equal(next.profiles.a.head, '{{text:brief}}\n\n{{client_system}}');
  assert.equal(next.profiles.a.client_system.edits[0].text, '{{text:brief}}');
  assert.equal(next.profiles.b, doc.profiles.b);
  assert.deepEqual(removeText(doc, 'rules').texts, {});
});

test('relay texts: null means built-in and leaves no key behind', () => {
  const doc = { version: 1, profiles: {} };
  const custom = setRelayText(doc, 'role', 'my role');
  assert.deepEqual(custom.relay_texts, { role: 'my role' });
  assert.equal('relay_texts' in setRelayText(custom, 'role', null), false);
  assert.deepEqual(setRelayText(custom, 'reminder', 'x').relay_texts, { role: 'my role', reminder: 'x' });
  assert.deepEqual(setRelayText(setRelayText(custom, 'reminder', 'x'), 'role', null).relay_texts, { reminder: 'x' });
});

test('protection is on unless the document says otherwise', () => {
  assert.equal(protectedEnabled({}), true);
  assert.equal(protectedEnabled(null), true);
  assert.equal(protectedEnabled({ protected: { enabled: true } }), true);
  const off = setProtected({ version: 1 }, false);
  assert.deepEqual(off.protected, { enabled: false });
  assert.equal(protectedEnabled(off), false);
  assert.equal('protected' in setProtected(off, true), false);
  assert.deepEqual(setProtected({ protected: { enabled: false, note: 1 } }, true).protected, { note: 1 });
});

test('changes are counted per section, profile and text, and the version is not a change', () => {
  const saved = { version: 1, default_profile: null, texts: { a: '1' }, profiles: { p: { head: 'x' }, q: { head: 'y' } }, rules: [{ name: 'r' }] };
  assert.deepEqual(changedPaths(saved, saved), []);
  assert.deepEqual(changedPaths(saved, JSON.parse(JSON.stringify(saved))), []);
  const draft = { ...saved, version: 2, profiles: { p: { head: 'x2' }, r: { head: 'new' } }, texts: { a: '1', b: '2' }, rules: [{ name: 'r' }, { name: 's' }], default_profile: 'p' };
  assert.deepEqual(changedPaths(saved, draft).sort(), ['default_profile', 'profiles.p', 'profiles.q', 'profiles.r', 'rules', 'texts.b']);
  assert.deepEqual(changedPaths(null, null), []);
  assert.deepEqual(changedPaths(null, blankDocument()), []);
  assert.deepEqual(changedPaths({ version: 1, default_profile: null, rules: [] }, { version: 1, rules: [], texts: {} }), []);
  // a built-in relay text is null or missing, the same thing; an empty named text is not nothing
  assert.deepEqual(changedPaths({ relay_texts: { role: null, reminder: 'x' } }, { relay_texts: { reminder: 'x' } }), []);
  assert.deepEqual(changedPaths({ texts: { a: 'x' } }, { texts: { a: 'x', b: '' } }), ['texts.b']);
  assert.deepEqual(changedPaths(null, { version: 1, profiles: { mine: { head: 'h' } }, rules: [{ name: 'r' }] }).sort(), ['profiles.mine', 'rules']);
  assert.equal(pathLabel('profiles.mine'), 'профиль mine');
  assert.equal(pathLabel('relay_texts.role'), 'текст relay: role');
  assert.equal(pathLabel('rules'), 'правила');
  assert.equal(pathLabel('texts.x'), 'текст x');
  assert.equal(pathLabel('odd'), 'odd');
});

test('documents are equal when their contents are, whatever the key order', () => {
  assert.equal(equalJson({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1 }), true);
  assert.equal(equalJson({ a: 1 }, { a: 1, b: undefined }), false);
  assert.equal(equalJson([1, 2], [2, 1]), false);
  assert.equal(equalJson(null, {}), false);
  assert.equal(equalJson([], {}), false);
});

test('errors from the relay point into the screen by path, however the list index is written', () => {
  assert.equal(normalizePath('profiles.mine.client_system.edits[0].heading'), 'profiles.mine.client_system.edits.0.heading');
  assert.equal(errorHits('profiles.mine.head', 'profiles.mine.head'), true);
  assert.equal(errorHits('profiles.mine.client_system.edits[1].tag', 'profiles.mine.client_system.edits.1'), true);
  assert.equal(errorHits('profiles.mine.client_system.edits[1].tag', 'profiles.mine.client_system'), true);
  assert.equal(errorHits('profiles.mine2.head', 'profiles.mine'), false);
  assert.equal(errorHits('', 'profiles.mine'), false);
  assert.deepEqual(locate('profiles.mine.head'), { tab: 'profiles', profile: 'mine' });
  assert.deepEqual(locate('rules[2].use'), { tab: 'rules', rule: 2 });
  assert.deepEqual(locate('rules'), { tab: 'rules', rule: null });
  assert.deepEqual(locate('default_profile'), { tab: 'rules', rule: null });
  assert.deepEqual(locate('texts.my_rules'), { tab: 'texts', text: 'my_rules', index: undefined });
  assert.deepEqual(locate('relay_texts.role'), { tab: 'texts', text: 'role', index: undefined });
  assert.deepEqual(locate('x-proxi-profile'), { tab: null });
});

// --- the API ---------------------------------------------------------------------------------------------------

test('the document answer is read with defaults for everything the relay leaves out', () => {
  const empty = normalizePrompts({});
  assert.equal(empty.document, null);
  assert.equal(empty.rev, null);
  assert.deepEqual(empty.errors, []);
  assert.deepEqual(empty.legacy, { mode: '', roleChars: null });
  assert.ok(empty.builtin.variables.some((variable) => variable.name === 'client_system'));
  assert.ok(empty.builtin.conditions.length >= 11);
  assert.deepEqual(normalizePrompts(null).document, null);
  const full = normalizePrompts({
    document: { version: 1, profiles: {} }, rev: 4, path: 'C:/relay/prompts.json',
    errors: [{ message: 'bad', param: 'profiles.x.head', ts: 5 }, 'plain'],
    legacy: { mode: 'standalone', role_chars: 120 },
    builtin: { relay_texts: { role: 'ROLE' }, protected_rules: [{ name: 'p', if: { purpose: ['x'] }, use: 'passthrough' }, 'junk'], profiles: ['passthrough'] },
  });
  assert.equal(full.rev, 4);
  assert.equal(full.path, 'C:/relay/prompts.json');
  assert.deepEqual(full.errors, [{ message: 'bad', param: 'profiles.x.head', ts: 5 }, { message: 'plain', param: '', ts: null }]);
  assert.deepEqual(full.legacy, { mode: 'standalone', roleChars: 120 });
  assert.deepEqual(full.builtin.relayTexts, { role: 'ROLE' });
  assert.deepEqual(full.builtin.protectedRules, [{ name: 'p', if: { purpose: ['x'] }, use: 'passthrough', description: '' }]);
  assert.deepEqual(full.builtin.profiles, ['passthrough']);
});

test('variables come as names, objects or a map and always include a description', () => {
  assert.deepEqual(normalizeVariables(['a', { name: 'b', description: 'bee' }, { id: 'c' }, 'a', 5]).map((v) => v.name), ['a', 'b', 'c']);
  assert.equal(normalizeVariables([{ name: 'b', description: 'bee' }])[0].description, 'bee');
  assert.deepEqual(normalizeVariables({ role: 'роль', model: { description: 'модель' } }), [
    { name: 'role', description: 'роль' }, { name: 'model', description: 'модель' }]);
  assert.equal(normalizeVariables(['client_system'])[0].description.length > 0, true);
  assert.deepEqual(normalizeVariables([]).map((v) => v.name), DEFAULT_VARIABLES);
  assert.deepEqual(normalizeBuiltin(undefined).protectedRules, []);
});

test('a preview answer always has lists and parts as spans, and keeps null where the relay says null', () => {
  const preview = normalizePreview({
    rule: null, profile: 'mine', rev: null, protected: false, purpose: 'client-tools', client: 'opencode', model: 'claude-opus-5-5',
    head: 'abc', parts: [['literal', 1], ['client', 2]], head_bytes: 3, tokens_estimate: 1,
  });
  assert.equal(preview.rule, null);
  assert.equal(preview.rev, null);
  assert.deepEqual(preview.parts.map((part) => part.end), [1, 3]);
  assert.deepEqual([preview.warnings, preview.edits_unmatched, preview.history_changes, preview.tools_changes], [[], [], [], []]);
  assert.equal(preview.head_bytes, 3);
  assert.equal(normalizePreview(null).head, '');
  assert.equal(normalizePreview({ protected: true }).protected, true);
});

test('recent heads and pins are read for what the screen shows', () => {
  const [item] = normalizeRecent({ items: [{ ts: 5, request_id: 'r1', client: 'codex', model: 'm', purpose: 'client-tools', rule: 'r', profile: 'p', rev: 2, pinned: true,
    head: 'h', parts: [{ source: 'client', start: 0, end: 1 }], has_request: true }, 'junk'] });
  assert.deepEqual([item.request_id, item.rule, item.pinned, item.has_request, item.rev], ['r1', 'r', true, true, 2]);
  assert.equal(normalizeRecent({ items: [{ request_id: 'x' }] })[0].has_request, false);
  assert.deepEqual(normalizeRecent(undefined), []);
  assert.deepEqual(normalizePins({ items: [{ conversation: 'c-1a2b', purpose: 'client-tools', rev: 3, profile: 'p', age_s: 90 }] }),
    [{ conversation: 'c-1a2b', purpose: 'client-tools', rev: 3, profile: 'p', age_s: 90 }]);
  assert.deepEqual(normalizePins(null), []);
});

test('a pasted body must be a JSON object, and headers may be lines or JSON', () => {
  assert.deepEqual(parseBody('{"a":1}'), { body: { a: 1 } });
  assert.match(parseBody('').error, /Вставьте/);
  assert.match(parseBody('[1]').error, /объект/);
  assert.match(parseBody('{bad').error, /Не JSON/);
  assert.deepEqual(parseHeaders('User-Agent: opencode/1.0\nx-proxi-profile:  mine \n\n'), { headers: { 'User-Agent': 'opencode/1.0', 'x-proxi-profile': 'mine' } });
  assert.deepEqual(parseHeaders('{"x-a": 1}'), { headers: { 'x-a': '1' } });
  assert.deepEqual(parseHeaders(''), { headers: {} });
  assert.match(parseHeaders('no colon here').error, /без двоеточия/);
  assert.match(parseHeaders('{"a"').error, /Заголовки/);
  assert.match(parseHeaders('[1]').error, /Заголовки|двоеточия/);
});

test('the dialect of a pasted body is guessed from its shape', () => {
  assert.equal(detectDialect({ model: 'm', input: [{ role: 'user', content: 'hi' }] }), 'responses');
  assert.equal(detectDialect({ model: 'm', system: 'be brief', messages: [] }), 'anthropic');
  assert.equal(detectDialect({ model: 'm', messages: [{ role: 'assistant', content: [{ type: 'tool_use', id: '1', name: 'bash', input: {} }] }] }), 'anthropic');
  assert.equal(detectDialect({ model: 'm', tools: [{ name: 'bash', input_schema: {} }], messages: [] }), 'anthropic');
  assert.equal(detectDialect({ model: 'm', messages: [{ role: 'system', content: 'x' }] }), 'chat');
  assert.equal(detectDialect(null), 'chat');
});

test('a preview request carries the sample and only the draft when one is judged', () => {
  const paste = { kind: 'paste', request: { model: 'm' }, dialect: 'chat', headers: { 'x-a': '1' } };
  assert.deepEqual(previewRequest(paste), { request: { model: 'm' }, dialect: 'chat', headers: { 'x-a': '1' } });
  assert.deepEqual(previewRequest(paste, { version: 1 }), { request: { model: 'm' }, dialect: 'chat', headers: { 'x-a': '1' }, document: { version: 1 } });
  assert.deepEqual(previewRequest({ kind: 'recent', request_id: 'r1' }), { request_id: 'r1' });
  assert.deepEqual(previewRequest({ kind: 'recent', request_id: 'r1' }, null), { request_id: 'r1', document: null });
  assert.equal(previewRequest(null), null);
  assert.deepEqual(previewRequest({ kind: 'paste', request: {}, dialect: 'chat' }).headers, {});
});

test('the baseline and the forced profile leave only what the preview needs of the rules', () => {
  const doc = { version: 1, texts: { a: 'x' }, profiles: { mine: { head: '{{text:a}}' } }, rules: [{ name: 'r', if: {}, use: 'mine' }], default_profile: null };
  assert.deepEqual(forceProfile(doc, 'mine'), { ...doc, default_profile: 'mine', rules: [] });
  assert.equal(doc.rules.length, 1);
  assert.equal(BASELINE_DOCUMENT.default_profile, 'passthrough');
  assert.deepEqual(BASELINE_DOCUMENT.rules, []);
});

test('a failed call is read as status, message and the path the relay blames', () => {
  assert.deepEqual(failureOf({ status: 400, message: 'bad', body: { error: { message: 'profiles.x.head: unknown variable', param: 'profiles.x.head' } } }),
    { status: 400, message: 'profiles.x.head: unknown variable', param: 'profiles.x.head' });
  assert.deepEqual(failureOf({ status: 409, message: 'stale', body: null }), { status: 409, message: 'stale', param: '' });
  assert.deepEqual(failureOf(new Error('boom')), { status: 0, message: 'boom', param: '' });
  assert.deepEqual(failureOf(undefined), { status: 0, message: 'Ошибка запроса', param: '' });
});

// --- rules, starting points and samples ------------------------------------------------------------------------------

test('the document answer of the relay is read as the relay sends it: names, typed conditions, limits, special targets and warnings', () => {
  const answer = normalizePrompts({
    document: null, rev: null, path: 'prompts.json', errors: [], warnings: ['protected.enabled: false: …', { message: 'rules[1]: override_protected' }],
    legacy: { mode: 'standalone', role_chars: 329 },
    builtin: {
      relay_texts: { role: 'R', native_role: 'N', reminder: '<system-reminder>\n{{text}}\n</system-reminder>', structured: '{{default}}', codex_base: 'C' },
      protected_rules: [], variables: ['client_system', 'role'],
      edit_ops: { client_system: ['remove_tag'], mid_system: ['keep'], user_blocks: ['drop_tag'], tools: ['hide'] },
      conditions: { client: { type: 'string', glob: true, values: ['opencode', 'codex'] }, tools: { type: 'boolean' }, model: { type: 'string', glob: true } },
      profiles: ['passthrough'], structured_example: 'example', relay_task_variables: ['relay_task', 'local_exec'],
      special_uses: ['$header', '$legacy'], profile_name: '[a-z][a-z0-9-]{0,15}', limits: { max_text_bytes: 1000, max_document_bytes: 5000 },
    },
  });
  assert.deepEqual(answer.warnings, ['protected.enabled: false: …', 'rules[1]: override_protected']);
  assert.equal(answer.hasWarnings, true);
  assert.equal(normalizePrompts({}).hasWarnings, false);
  assert.deepEqual(normalizePrompts({}).warnings, []);
  const { builtin } = answer;
  assert.deepEqual(builtin.variables.map((variable) => variable.name), ['client_system', 'role']);
  assert.ok(builtin.variables.every((variable) => variable.description.length > 0));
  assert.deepEqual(builtin.editOps.client_system, ['remove_tag']);
  assert.equal(builtin.conditions.find((spec) => spec.name === 'tools').kind, 'bool');
  assert.deepEqual(builtin.conditions.find((spec) => spec.name === 'client').values, ['opencode', 'codex']);
  assert.equal(builtin.conditions.find((spec) => spec.name === 'model').kind, 'list');
  assert.deepEqual(builtin.relayTaskVariables, ['relay_task', 'local_exec']);
  assert.deepEqual(builtin.specialUses.map((item) => item.id), ['$header', '$legacy']);
  assert.equal(builtin.specialUses[0].label, 'Профиль из заголовка');
  assert.deepEqual(builtin.limits, { textBytes: 1000, documentBytes: 5000 });
  assert.equal(builtin.structuredExample, 'example');
  assert.equal(builtin.profileName.test('mine-1'), true);
  assert.equal(builtin.profileName.test('Mine'), false);
  assert.equal(builtin.profileName.test('a'.repeat(17)), false);
});

test('what the relay leaves out stays at the documented values', () => {
  const builtin = normalizeBuiltin({});
  assert.deepEqual(builtin.relayTaskVariables, RELAY_TASK_VARIABLES);
  assert.deepEqual(builtin.specialUses.map((item) => item.id), ['$header', '$virtual', '$legacy']);
  assert.deepEqual(builtin.limits, { textBytes: MAX_TEXT_BYTES, documentBytes: MAX_DOCUMENT_BYTES });
  assert.equal(builtin.profileName, null);
  assert.equal(builtin.structuredExample, '');
  assert.deepEqual(normalizeLimits({ max_text_bytes: 'big', max_document_bytes: -1 }), { textBytes: MAX_TEXT_BYTES, documentBytes: MAX_DOCUMENT_BYTES });
  assert.equal(normalizeLimits({ text: 77 }).textBytes, 77);
  assert.equal(normalizeBuiltin({ structured_example: { a: 1 } }).structuredExample, '{\n  "a": 1\n}');
  assert.equal(compileProfileName(''), null);
  assert.equal(compileProfileName('(?P<bad>x)'), null);
  assert.equal(compileProfileName('[a-z]+')?.test('abc'), true);
  assert.equal(compileProfileName('[a-z]+')?.test('abc1'), false);
});

test('special targets come as names or objects, keep the descriptions the screen knows, and take a newer one from the relay', () => {
  assert.deepEqual(normalizeSpecialUses(['$header', { id: '$virtual', description: 'своё описание' }, '$new', '$header', 5]).map((item) => [item.id, item.hint]),
    [['$header', 'Берётся из x-proxi-profile; если такого профиля нет, запрос получит ошибку 400'], ['$virtual', 'своё описание'], ['$new', '']]);
  assert.deepEqual(normalizeSpecialUses([]).map((item) => item.id), ['$header', '$virtual', '$legacy']);
  assert.deepEqual(normalizeSpecialUses(undefined).map((item) => item.id), ['$header', '$virtual', '$legacy']);
});

test('the answer of a save is the revision and the warnings, and a refusal opens its message by its status', () => {
  assert.deepEqual(normalizeSaved({ rev: 4, warnings: ['a', { message: 'b' }] }), { rev: 4, warnings: ['a', 'b'] });
  assert.deepEqual(normalizeSaved(null), { rev: null, warnings: [] });
  assert.equal(failureLead({ status: 400 }), 'Relay не принял документ');
  assert.match(failureLead({ status: 413 }), /2 МиБ/);
  assert.match(failureLead({ status: 503 }), /записать файл/);
  assert.equal(failureLead({ status: 0 }), 'Relay недоступен');
  assert.equal(failureLead({ status: 500 }), 'Ошибка');
});

test('a profile name is judged by the relay pattern when there is one', () => {
  const strict = compileProfileName('[a-z][a-z0-9-]{0,7}');
  assert.equal(profileNameError('mine', [], strict), null);
  assert.match(profileNameError('Mine', [], strict), /латинские буквы/);
  assert.match(profileNameError('toolongname', [], strict), /латинские буквы/);
  assert.equal(profileNameError('Mine', []), null);
  assert.match(profileNameError('passthrough', [], strict), /встроенный/);
  assert.match(profileNameError('mine', ['mine'], strict), /уже есть/);
});

test('a rule field is written only when it differs from the default', () => {
  const rule = { name: 'r', if: {}, use: 'p' };
  assert.deepEqual(setRuleField(rule, 'enabled', false), { ...rule, enabled: false });
  assert.deepEqual(setRuleField({ ...rule, enabled: false }, 'enabled', true), rule);
  assert.deepEqual(setRuleField(rule, 'override_protected', true), { ...rule, override_protected: true });
  assert.deepEqual(setRuleField({ ...rule, override_protected: true }, 'override_protected', false), rule);
  assert.deepEqual(setRuleField(rule, 'name', 'x'), { ...rule, name: 'x' });
  assert.deepEqual(setRuleField(rule, 'name', undefined), { if: {}, use: 'p' });
});

test('whatever follows the first enabled rule that matches everything can never be reached', () => {
  const rule = (extra) => ({ name: 'r', use: 'p', ...extra });
  assert.equal(catchAllIndex([rule({ if: { client: 'codex' } }), rule({ if: {} }), rule({ if: { client: 'x' } })]), 1);
  assert.equal(catchAllIndex([rule({}), rule({ if: {} })]), 0);
  assert.equal(catchAllIndex([rule({ if: {}, enabled: false }), rule({ if: { tools: true } })]), -1);
  assert.equal(catchAllIndex([]), -1);
});

test('the starting points keep both kinds of request as they are or start from the user\'s own text', () => {
  assert.deepEqual(STARTERS.map((item) => item.id), ['same', 'rules-first', 'exact']);
  assert.equal(starterProfile('same').head, '{{client_system}}{{relay_task}}{{exec_reminder}}');
  assert.match(starterProfile('rules-first').head, /^Отвечай по-русски.*\n\n\{\{client_system\}\}/s);
  assert.doesNotMatch(starterProfile('exact').head, /client_system/);
  assert.deepEqual(starterProfile('nope'), starterProfile('same'));
  for (const item of STARTERS) {
    const known = ['client_system', 'relay_task', 'exec_reminder'];
    assert.deepEqual(templateProblems(item.head, known, []), []);
  }
});

test('a free name is the base itself, or the base with the next number', () => {
  assert.equal(uniqueName('mine', ['a']), 'mine');
  assert.equal(uniqueName('mine', ['mine']), 'mine-2');
  assert.equal(uniqueName('mine', ['mine', 'mine-2', 'mine-3']), 'mine-4');
});

test('a sample is a recent request with a kept body, or a pasted body with its headers and dialect', () => {
  const recent = [
    { request_id: 'a', ts: 1700000000, client: 'opencode', model: 'claude-opus-5-5', purpose: 'client-tools', has_request: false },
    { request_id: 'b', ts: 1700000100, client: 'codex', model: 'gpt-5.5', purpose: '', has_request: true },
  ];
  assert.equal(newestSample(recent).request_id, 'b');
  assert.equal(newestSample([recent[0]]), null);
  assert.match(recentLabel(recent[1]), /codex · gpt-5\.5$/);
  assert.deepEqual(resolveSample(emptySample(), recent), { sample: null, error: '' });
  assert.deepEqual(resolveSample({ ...emptySample(), requestId: 'a' }, recent), { sample: null, error: '' });
  const picked = resolveSample({ ...emptySample(), requestId: 'b' }, recent);
  assert.deepEqual([picked.sample.kind, picked.sample.request_id], ['recent', 'b']);

  const paste = { mode: 'paste', text: '{"model": "m", "system": "s"}', dialect: '', headersText: 'User-Agent: x' };
  const pasted = resolveSample(paste, []);
  assert.deepEqual([pasted.sample.kind, pasted.sample.dialect, pasted.sample.headers, pasted.sample.request], ['paste', 'anthropic', { 'User-Agent': 'x' }, { model: 'm', system: 's' }]);
  assert.equal(resolveSample({ ...paste, dialect: 'responses' }, []).sample.dialect, 'responses');
  assert.deepEqual(resolveSample({ ...paste, text: '  ' }, []), { sample: null, error: '' });
  assert.match(resolveSample({ ...paste, text: '{oops' }, []).error, /Не JSON/);
  assert.match(resolveSample({ ...paste, headersText: 'nope' }, []).error, /без двоеточия/);
  assert.deepEqual(resolveSample(null), { sample: null, error: '' });
});

test('a change the relay reports is read for its operation, its place and its note, whatever fields it has', () => {
  assert.deepEqual(describeChange('plain text'), { title: 'plain text', details: [], note: '' });
  assert.deepEqual(describeChange({ op: 'drop_tag', tag: 'system-reminder', count: 3 }),
    { title: 'Убрать блок тега', details: [['тег', 'system-reminder'], ['совпадений', '3']], note: '' });
  assert.deepEqual(describeChange({ op: 'append', tool: 'bash', chars: 57, reason: 'tool matched' }),
    { title: 'Добавить в конец', details: [['инструмент', 'bash'], ['симв.', '57']], note: 'tool matched' });
  assert.equal(describeChange({ message: 'skipped: would break the roles' }).title, 'skipped: would break the roles');
  assert.equal(describeChange({ op: 'future_op' }).title, 'future_op');
  assert.deepEqual(describeChange({ op: 'x', mystery: 'seen', nested: { a: 1 } }).details, [['mystery', 'seen']]);
  assert.equal(describeChange(null).title, '');
  assert.equal(describeChange({ message: 'x'.repeat(200) }).title.length, 80);
});
