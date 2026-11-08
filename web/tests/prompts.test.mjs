import assert from 'node:assert/strict';
import test from 'node:test';
import { MAX_TEXT_BYTES, insertAt as insertText, templateProblems, tokenize, utf8Bytes, variableNames, problemText } from '../src/prompts/template.js';
import { diffLines, diffStats, foldDiff } from '../src/prompts/diff.js';
import { normalizeParts, segmentHead, sourceKind, sourceLabel, summarizeKinds } from '../src/prompts/parts.js';
import { describeEdit, flagValue, flagsOf, joinFlags, newEdit, normalizeEditOps, opOf, setEditField, switchOp } from '../src/prompts/ops.js';
import { CONDITION_SPECS, conditionText, fromValues, normalizeConditions, setCondition, summarizeConditions, valuesOf } from '../src/prompts/conditions.js';
import {
  addProfile, blankDocument, catchAllIndex, changedPaths, equalJson, errorHits, idOf, insertAt, locate, moveItem, newRule, normalizePath, patchProfile,
  pathLabel, profileNameError, profileUsage, protectedEnabled, pruneProfile, removeAt, removeProfile, removeText, renameProfile, renameText,
  setProtected, setRelayText, setRuleField, STARTERS, starterProfile, systemEditsOf, textNameError, textNameHint, textUsage, uniqueName, updateAt,
} from '../src/prompts/draft.js';
import {
  BASELINE_DOCUMENT, DEFAULT_VARIABLES, MAX_DOCUMENT_BYTES, RELAY_TASK_VARIABLES, compileName, compileProfileName, compileTextName, detectDialect, failureLead, failureOf, forceProfile,
  normalizeBuiltin, normalizeEditReport, normalizeLimits, normalizePins, normalizePreview, normalizePrompts, normalizeRecent, normalizeSaved, normalizeSpecialUses,
  normalizeVariables, parseBody, parseHeaders, previewRequest, warningsToShow,
} from '../src/prompts/api.js';
import { describeChange, describeGroup, describeReport, describeUnmatched, editTarget, groupChanges, opLabel, scopeLabel, shortList } from '../src/prompts/changes.js';
import { fmtInt } from '../src/format.js';
import { emptySample, newestSample, recentLabel, resolveSample } from '../src/prompts/sample.js';
import { publishedModels } from '../src/prompts/publish.js';

test('publish masks match the full model id the way the relay does: case-insensitive fnmatch', () => {
  const catalog = ['claude-opus-5-5', 'claude-sonnet-5-5', 'zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash', 'gpt-5.5'];
  assert.equal(publishedModels(['glm-*'], catalog).length, 0);                       // the GLM ids start with their organisation
  assert.deepEqual(publishedModels(['*glm*'], catalog), ['zai-org/GLM-5.3', 'zai-org/GLM-5.3-Flash']);
  assert.deepEqual(publishedModels(['ZAI-ORG/glm-5.?'], catalog), ['zai-org/GLM-5.3']);
  assert.deepEqual(publishedModels(['claude-[os]*'], catalog), ['claude-opus-5-5', 'claude-sonnet-5-5']);
  assert.deepEqual(publishedModels(['claude-[!o]*'], catalog), ['claude-sonnet-5-5']);
  assert.deepEqual(publishedModels(['claude-[^s]*'], catalog), ['claude-sonnet-5-5']);
  assert.deepEqual(publishedModels(['[]]'], [']', 'a']), [']']);
  assert.deepEqual(publishedModels(['[!]]'], [']', 'a']), ['a']);
  assert.deepEqual(publishedModels(['[z-a]'], ['a', 'z']), []);
  assert.deepEqual(publishedModels(['[!z-a]'], ['a', 'z']), ['a', 'z']);
  assert.deepEqual(publishedModels(true, catalog), catalog);
  assert.deepEqual(publishedModels(false, catalog), []);
  assert.deepEqual(publishedModels(['gpt-5.5'], catalog), ['gpt-5.5']);              // a dot is a dot, not any character
  assert.deepEqual(publishedModels(['gpt-5x5'], catalog), []);
  assert.equal(publishedModels(['*'], null), null);                                     // the catalog is not loaded yet
});

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

test('every source belongs to a family that sets its tint, and is named for people', () => {
  assert.deepEqual(['client', 'client_edited', 'text:mine', 'text:other', 'relay:role', 'relay:task', 'literal', 'billing', 'weird'].map(sourceKind),
    ['client', 'client', 'text', 'text', 'relay', 'relay', 'literal', 'billing', 'other']);
  assert.equal(sourceLabel('text:my_rules'), 'текст «my_rules»');
  assert.equal(sourceLabel('relay:native_role'), 'роль для клиента с tools');
  assert.equal(sourceLabel('relay:future'), 'relay: future');
  assert.equal(sourceLabel('client'), 'system клиента');
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

test('the client system as sent and after the edits is one kind in the legend, and two parts in the list', () => {
  const segments = segmentHead('aabbbcc', [{ source: 'client_edited', start: 0, end: 2 }, { source: 'text:x', start: 2, end: 5 }, { source: 'client', start: 5, end: 7 }]);
  assert.deepEqual(summarizeKinds(segments), [
    { kind: 'client', label: 'system клиента', chars: 4, parts: 2 },
    { kind: 'text', label: 'именованный текст', chars: 3, parts: 1 },
  ]);
  assert.deepEqual(segments.map((segment) => [segment.source, segment.kind]), [['client_edited', 'client'], ['text:x', 'text'], ['client', 'client']]);
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
  assert.deepEqual(setEditField({ op: 'remove_tag' }, 'tag', 'env'), { op: 'remove_tag', tag: 'env' });
  assert.deepEqual(setEditField({ tool: 'bash', op: 'append' }, 'tool', ''), { tool: '', op: 'append' });
  assert.deepEqual(setEditField({ op: 'keep_sections', headings: ['# A'] }, 'headings', []), { op: 'keep_sections' });
});

test('"all" of replace_text is on unless the edit says all: false, and the form writes exactly what the user chose', () => {
  const fresh = newEdit('client_system', 'replace_text');
  const edit = { op: 'replace_text', find: 'a', with: 'b' };
  assert.deepEqual(fresh, { op: 'replace_text', with: '' });
  // the relay replaces every occurrence while `all` is missing, so a box with no key behind it is ticked
  assert.equal(flagValue('all', fresh.all), true);
  assert.equal(flagValue('all', null), true);
  assert.equal(flagValue('all', true), true);
  assert.equal(flagValue('all', false), false);
  // unticking is the explicit false, ticking is the default and leaves no key behind
  const unticked = setEditField(edit, 'all', false);
  assert.deepEqual(unticked, { ...edit, all: false });
  assert.deepEqual(setEditField(unticked, 'all', true), edit);
  assert.deepEqual(setEditField({ ...edit, all: true }, 'all', false), { ...edit, all: false });
  assert.deepEqual(setEditField({ ...edit, all: true }, 'all', true), edit);
  // a file with all: false, ticked and unticked again (a double click), is the same file
  const file = { ...edit, all: false };
  assert.equal(flagValue('all', file.all), false);
  assert.equal(equalJson(setEditField(setEditField(file, 'all', true), 'all', false), file), true);
  // a box whose default is no keeps being written only when ticked
  const between = { op: 'remove_between', start: '<a>', end: '</a>' };
  assert.equal(flagValue('inclusive', between.inclusive), false);
  assert.deepEqual(setEditField(between, 'inclusive', true), { ...between, inclusive: true });
  assert.deepEqual(setEditField({ ...between, inclusive: true }, 'inclusive', false), between);
  // the operation can change under an edit that said all: false, and it keeps what both forms share
  assert.deepEqual(switchOp('tools', { tool: 'bash', op: 'replace_text', find: 'a', with: 'b', all: false }, 'replace_text'), { tool: 'bash', op: 'replace_text', find: 'a', with: 'b', all: false });
  assert.deepEqual(switchOp('user_blocks', { op: 'replace_text', find: 'a', with: 'b', all: false }, 'remove_regex'), { op: 'remove_regex' });
});

test('an edit that replaces always carries its replacement, empty or not, because the relay requires the key and an empty one deletes', () => {
  assert.deepEqual(newEdit('client_system', 'replace_text'), { op: 'replace_text', with: '' });
  assert.deepEqual(newEdit('client_system', 'replace_regex'), { op: 'replace_regex', with: '' });
  assert.deepEqual(newEdit('user_blocks', 'replace_text'), { op: 'replace_text', with: '' });
  assert.deepEqual(newEdit('tools', 'replace_text'), { tool: '', op: 'replace_text', with: '' });
  assert.deepEqual(newEdit('tools', 'replace_regex'), { tool: '', op: 'replace_regex', with: '' });
  assert.deepEqual(newEdit('client_system', 'remove_regex'), { op: 'remove_regex' });
  assert.deepEqual(newEdit('client_system', 'append'), { op: 'append' });
  // typed and emptied again, it stays an empty string; the other fields that are emptied still leave the edit
  const typed = setEditField(newEdit('client_system', 'replace_text'), 'with', 'x');
  assert.deepEqual(typed, { op: 'replace_text', with: 'x' });
  assert.deepEqual(setEditField(typed, 'with', ''), { op: 'replace_text', with: '' });
  assert.deepEqual(setEditField({ op: 'replace_text', find: 'a', with: '' }, 'find', ''), { op: 'replace_text', with: '' });
  assert.deepEqual(setEditField({ op: 'remove_regex', pattern: 'x', flags: 'i' }, 'flags', ''), { op: 'remove_regex', pattern: 'x' });
  assert.deepEqual(setEditField({ op: 'replace_text', with: 'x' }, 'with', undefined), { op: 'replace_text' });
  // switching starts a replacing edit with an empty replacement, and keeps the one the form it comes from had
  assert.deepEqual(switchOp('client_system', { op: 'prepend', text: 'hi' }, 'replace_text'), { op: 'replace_text', with: '' });
  assert.deepEqual(switchOp('client_system', { op: 'replace_text', find: 'a', with: 'b' }, 'replace_regex'), { op: 'replace_regex', with: 'b' });
  assert.deepEqual(switchOp('client_system', { op: 'replace_regex', pattern: 'a', with: 'b' }, 'remove_regex'), { op: 'remove_regex', pattern: 'a' });
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
  assert.deepEqual([preview.warnings, preview.edits_unmatched, preview.history_changes, preview.tools_changes, preview.edits], [[], [], [], [], []]);
  assert.equal(preview.head_bytes, 3);
  assert.equal(normalizePreview(null).head, '');
  assert.equal(normalizePreview({ protected: true }).protected, true);
});

test('the report on the edits of the client system keeps the position of each edit and says only what the relay said about a match', () => {
  assert.deepEqual(normalizeEditReport([{ index: 0, op: 'remove_tag', matched: true, target: 'x' }, { op: 'append', matched: false }, { op: 'prepend' }, 'junk']), [
    { index: 0, op: 'remove_tag', matched: true, target: 'x' },
    { index: 1, op: 'append', matched: false },
    { index: 2, op: 'prepend', matched: null },
  ]);
  assert.deepEqual(normalizeEditReport(undefined), []);
  const preview = normalizePreview({ edits: [{ index: 0, op: 'remove_section', matched: true, removed_chars: 5 }], warnings: ['one', { message: 'two' }] });
  assert.deepEqual(preview.edits, [{ index: 0, op: 'remove_section', matched: true, removed_chars: 5 }]);
  assert.deepEqual(preview.warnings, ['one', 'two']);
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

test('the warnings about the document are the relay\'s list with it and, while that save stands, what the save said besides, each once', () => {
  const list = (rev, warnings) => ({ rev, warnings });
  assert.deepEqual(warningsToShow(list(4, ['a', 'b']), { rev: 4, warnings: ['a', 'b', 'c'] }), ['a', 'b', 'c']);
  assert.deepEqual(warningsToShow(list(4, ['a']), { rev: 4, warnings: ['b', 'a'] }), ['a', 'b']);
  assert.deepEqual(warningsToShow(list(4, ['a', 'a']), { rev: 4, warnings: ['a'] }), ['a']);
  // the reading has not caught up with the save yet: the list that came with the previous document is not about this one
  assert.deepEqual(warningsToShow(list(3, ['old']), { rev: 4, warnings: ['new', 'new'] }), ['new']);
  // the first save of a relay with no file: there was no revision to read, and the save made the document
  assert.deepEqual(warningsToShow(list(null, []), { rev: 1, warnings: ['first'] }), ['first']);
  // somebody saved after that, there was no save: the relay's own list is all there is
  assert.deepEqual(warningsToShow(list(5, ['x']), { rev: 4, warnings: ['y'] }), ['x']);
  assert.deepEqual(warningsToShow(list(4, ['x']), { rev: null, warnings: [] }), ['x']);
  assert.deepEqual(warningsToShow(list(4, ['x']), undefined), ['x']);
  assert.deepEqual(warningsToShow(list(null, ['x']), undefined), ['x']);
  assert.deepEqual(warningsToShow(null, { rev: 4, warnings: ['y'] }), []);
  assert.deepEqual(warningsToShow(list(4, []), { rev: 4, warnings: [] }), []);
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

test('a text name is judged by the pattern the relay publishes, read the way Python reads it, and by the documented bans without one', () => {
  // the rule of the relay today: a word character, then up to 63 of word characters, dots and hyphens; \w is Unicode in Python
  const pattern = compileName('\\w[\\w.-]{0,63}');
  for (const name of ['my_rules', 'a', '_x', 'a.b-c', 'правила', 'Текст2', 'x'.repeat(64), '٣x']) assert.equal(pattern.test(name), true, name);
  for (const name of ['', '-x', '.x', 'a b', 'a:b', 'a{b}', 'a/b', 'a\nb', 'x'.repeat(65), 'a😀']) assert.equal(pattern.test(name), false, JSON.stringify(name));
  const rule = compileTextName('\\w[\\w.-]{0,63}');
  assert.equal(rule.source, '\\w[\\w.-]{0,63}');
  assert.equal(textNameError('my_rules', [], rule), null);
  assert.equal(textNameError('правила', ['other'], rule), null);
  assert.match(textNameError('-x', [], rule), /^Имя текста: буквы, цифры, «_», «\.» и «-», до 64 знаков; начинается с буквы, цифры или «_»$/);
  assert.match(textNameError('a b', [], rule), /^Имя текста:/);
  assert.match(textNameError('x'.repeat(65), [], rule), /до 64 знаков/);
  assert.match(textNameError('', [], rule), /Введите имя/);
  assert.match(textNameError('x', ['x'], rule), /уже есть/);
  assert.equal(textNameHint(rule), 'Буквы, цифры, «_», «.» и «-», до 64 знаков; начинается с буквы, цифры или «_»');
  // no rule published: the screen keeps to what would break {{text:name}}, as before
  assert.equal(textNameError('-x', []), null);
  assert.equal(textNameError('-x', [], null), null);
  assert.match(textNameError('a b', [], null), /нет пробелов/);
  assert.match(textNameError('a{b}', []), /нет пробелов/);
  assert.equal(textNameHint(null), 'Имя без пробелов, фигурных скобок и двоеточий');
  // a rule other than today's is quoted, not described: the words would not be true of it
  const other = compileTextName('[a-z]{1,8}');
  assert.equal(textNameError('abc', [], other), null);
  assert.equal(textNameError('Abc', [], other), 'Имя текста не подходит под правило relay: [a-z]{1,8}');
  assert.equal(textNameError('toolongname', [], other), 'Имя текста не подходит под правило relay: [a-z]{1,8}');
  assert.equal(textNameHint(other), 'Имя должно подходить под правило relay: [a-z]{1,8}');
  // a rule that does not read here is no rule
  assert.equal(compileName('\\Afoo\\Z'), null);
  assert.equal(compileName('\\bfoo'), null);
  assert.equal(compileName('(?P<n>x)'), null);
  assert.equal(compileName('[a'), null);
  assert.equal(compileName(undefined), null);
  assert.equal(compileTextName('(?P<n>x)'), null);
  assert.equal(compileTextName(''), null);
  assert.equal(compileName('\\d+')?.test('123'), true);
  assert.equal(compileName('\\d+')?.test('12a'), false);
  assert.equal(compileName('[\\d.]+')?.test('1.5'), true);
  assert.equal(compileName('a\\.b')?.test('a.b'), true);
  assert.equal(compileName('a\\.b')?.test('axb'), false);
  assert.equal(compileProfileName('[A-Za-z0-9][A-Za-z0-9._-]{0,63}')?.test('mine-first'), true);
  assert.equal(compileProfileName('[A-Za-z0-9][A-Za-z0-9._-]{0,63}')?.test('мой'), false);
  // the builtin answer carries it next to the profile name rule
  const builtin = normalizeBuiltin({ profile_name: '[a-z]+', text_name: '\\w[\\w.-]{0,63}' });
  assert.equal(builtin.textName.pattern.test('имя.v2'), true);
  assert.equal(builtin.textName.pattern.test('имя v2'), false);
  assert.equal(builtin.textName.source, '\\w[\\w.-]{0,63}');
  assert.equal(normalizeBuiltin({}).textName, null);
  assert.equal(normalizeBuiltin({ text_name: '' }).textName, null);
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
  // it keeps the system of the client and the tasks of the relay; it does not promise the role the old path gives a client with tools and no system
  assert.match(STARTERS[0].hint, /System клиента и задачи relay как сейчас/);
  assert.match(STARTERS[0].hint, /без system получит пустой head/);
  assert.match(STARTERS[0].hint, /\{\{native_role\}\}/);
  assert.doesNotMatch(STARTERS[0].hint, /без изменений/);
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

test('the sample to start from is the newest request with a kept body that is not a service request', () => {
  const entry = (request_id, extra) => ({ request_id, has_request: true, protected: false, ...extra });
  assert.equal(newestSample([entry('title', { protected: true }), entry('work'), entry('older')]).request_id, 'work');
  assert.equal(newestSample([entry('title', { protected: true }), entry('gone', { has_request: false }), entry('work')]).request_id, 'work');
  assert.equal(newestSample([entry('title', { protected: true }), entry('compaction', { protected: true })]), null);
  // a relay that says nothing about protection keeps the order it had
  assert.equal(newestSample([{ request_id: 'x', has_request: true }]).request_id, 'x');
  assert.equal(newestSample([]), null);
  assert.deepEqual(normalizeRecent({ items: [{ request_id: 'a', protected: true, has_request: true }, { request_id: 'b', has_request: true }, { request_id: 'c', protected: 'yes' }] }).map((item) => item.protected),
    [true, false, false]);
  // a service request can still be chosen on purpose
  const recent = [entry('title', { protected: true, ts: 1700000100 }), entry('work', { ts: 1700000000 })];
  assert.equal(resolveSample({ ...emptySample(), requestId: 'title' }, recent).sample.request_id, 'title');
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

test('scopes and operations are named by where they belong', () => {
  assert.deepEqual(['client_system', 'history', 'tools', 'future'].map(scopeLabel), ['system клиента', 'история', 'инструменты', 'future']);
  assert.equal(scopeLabel(''), '');
  assert.equal(opLabel('tools', 'append'), 'Добавить в конец описания');
  assert.equal(opLabel('client_system', 'append'), 'Добавить в конец');
  assert.equal(opLabel('history', 'replace_text'), 'Заменить текст');
  assert.equal(opLabel('tools', 'replace_text'), 'Заменить текст в описании');
  assert.equal(opLabel('history', 'mid_system', 'wrap'), 'Системные сообщения посреди диалога: обернуть как напоминание');
  assert.equal(opLabel('history', 'mid_system'), 'Системные сообщения посреди диалога');
  assert.equal(opLabel('', 'hide'), 'Скрыть инструмент');
  assert.equal(opLabel('tools', ''), '');
  assert.equal(opLabel('tools', 'future_op'), 'future_op');
  assert.equal(shortList(['a', 'b', 'c', 'd', 'e'], 3), 'a, b, c и ещё 2');
  assert.equal(shortList(['a', 'b'], 3), 'a, b');
});

test('the entries of a large body are folded by scope, operation and edit, and counted; skipped edits stay apart with their reason', () => {
  const items = [
    ...Array.from({ length: 12 }, (_, n) => ({ scope: 'history', op: 'drop_tag', edit: 0, index: n, target: 'system-reminder', removed_chars: 50 + n })),
    { scope: 'history', op: 'drop_tag', edit: 1, index: 3, target: 'other', removed_chars: 5 },
    { scope: 'history', op: 'mid_system', mode: 'drop', index: 20, deleted: true, removed_chars: 7 },
    { scope: 'history', op: 'mid_system', mode: 'drop', index: 22, deleted: true, removed_chars: 9 },
    { scope: 'history', op: 'mid_system', mode: 'wrap', index: 24, kept: true },
    { scope: 'history', op: 'drop_tag', edit: 0, index: 99, skipped: true, reason: 'protected' },
    { scope: 'history', op: 'drop_tag', edit: 0, index: 98, skipped: true, reason: 'protected' },
    { scope: 'history', op: 'drop_tag', edit: 0, index: 97, skipped: true, reason: 'too late' },
    'plain note', 'plain note',
  ];
  const groups = groupChanges(items);
  assert.deepEqual(groups.map((group) => [group.op || group.message, group.mode, group.edit, group.skipped, group.reason, group.count]), [
    ['drop_tag', '', 0, false, '', 12],
    ['drop_tag', '', 1, false, '', 1],
    ['mid_system', 'drop', null, false, '', 2],
    ['mid_system', 'wrap', null, false, '', 1],
    ['drop_tag', '', 0, true, 'protected', 2],
    ['drop_tag', '', 0, true, 'too late', 1],
    ['plain note', '', null, false, '', 2],
  ]);
  const folded = describeGroup(groups[0]);
  assert.deepEqual([folded.title, folded.where, folded.count, folded.reason], ['Убрать блок тега', 'история · правка 1', 12, '']);
  assert.deepEqual(folded.details, [['цель', 'system-reminder'], ['позиции в теле', '0, 1, 2, 3, 4, 5 и ещё 6'], ['убрано симв.', fmtInt(666)]]);
  const dropped = describeGroup(groups[2]);
  assert.deepEqual([dropped.title, dropped.where, dropped.count], ['Системные сообщения посреди диалога: удалить', 'история', 2]);
  assert.deepEqual(dropped.details, [['позиции в теле', '20, 22'], ['убрано симв.', '16'], ['удалено', '2']]);
  assert.deepEqual(describeGroup(groups[3]).details, [['позиции в теле', '24'], ['оставлено', '1']]);
  const skipped = describeGroup(groups[4]);
  assert.deepEqual([skipped.reason, skipped.count, groups[4].skipped], ['protected', 2, true]);
  assert.deepEqual(describeGroup(groups[6]), { title: 'plain note', where: '', count: 2, details: [], reason: '' });
  assert.deepEqual(groupChanges(undefined), []);
});

test('tool changes name the tool and what was added, and a hidden tool counts as deleted', () => {
  const groups = groupChanges([
    { scope: 'tools', op: 'append', edit: 0, index: 0, tool: 'bash', added_chars: 10 },
    { scope: 'tools', op: 'append', edit: 0, index: 4, tool: 'mcp_a', added_chars: 10 },
    { scope: 'tools', op: 'hide', edit: 2, index: 2, tool: 'webfetch', deleted: true },
    { scope: 'tools', op: 'future', edit: 3, index: 5, tool: 'x', brand_new: 'seen', nested: { a: 1 } },
  ]);
  assert.deepEqual(groups.map((group) => group.count), [2, 1, 1]);
  assert.deepEqual(describeGroup(groups[0]), { title: 'Добавить в конец описания', where: 'инструменты · правка 1', count: 2, reason: '',
    details: [['инструменты', 'bash, mcp_a'], ['позиции в теле', '0, 4'], ['добавлено симв.', '20']] });
  assert.deepEqual(describeGroup(groups[1]).details, [['инструмент', 'webfetch'], ['позиции в теле', '2'], ['удалено', '1']]);
  // a field the screen has no word for is still shown, under its own name
  assert.deepEqual(describeGroup(groups[2]).details, [['инструмент', 'x'], ['позиции в теле', '5'], ['brand_new', 'seen']]);
});

test('an edit that matched nothing is named by its place and says what it looked for', () => {
  assert.deepEqual(describeUnmatched({ scope: 'client_system', index: 1, op: 'remove_tag', target: 'available_skills' }),
    { title: 'Убрать блок тега', where: 'system клиента · правка 2', details: [['искали', 'available_skills']], note: '' });
  assert.deepEqual(describeUnmatched({ scope: 'history', op: 'drop_tag', edit: 0 }),
    { title: 'Убрать блок тега', where: 'история · правка 1', details: [], note: '' });
  assert.deepEqual(describeUnmatched({ scope: 'history', op: 'mid_system', mode: 'drop' }),
    { title: 'Системные сообщения посреди диалога: удалить', where: 'история', details: [], note: '' });
  assert.deepEqual(describeUnmatched({ scope: 'tools', op: 'append', edit: 3 }),
    { title: 'Добавить в конец описания', where: 'инструменты · правка 4', details: [], note: '' });
  assert.deepEqual(describeUnmatched('что-то'), { title: 'что-то', where: '', details: [], note: '' });
  assert.equal(describeUnmatched({ message: 'x' }).title, 'x');
});

test('the report on the edits of the client system is read edit by edit, with what an unmatched edit looked for', () => {
  const unmatched = [{ scope: 'client_system', index: 1, op: 'remove_tag', target: 'available_skills' }];
  assert.deepEqual(describeReport({ index: 0, op: 'remove_section', matched: true, target: '# Code comments', removed_chars: 612 }, unmatched),
    { number: 1, title: 'Убрать раздел', matched: true, details: [['цель', '# Code comments'], ['убрано симв.', fmtInt(612)]] });
  assert.deepEqual(describeReport({ index: 1, op: 'remove_tag', matched: false }, unmatched),
    { number: 2, title: 'Убрать блок тега', matched: false, details: [['искали', 'available_skills']] });
  assert.deepEqual(describeReport({ index: 2, op: 'prepend', matched: null, count: 1 }, []).details, [['совпадений', '1']]);
  assert.equal(describeReport({ index: 0, op: 'brand_new', matched: true }).title, 'brand_new');
});

test('the report as the relay sends it, with the lengths before and after, says what was cut and finds the target in the edit of the profile', () => {
  const edits = [
    { op: 'remove_section', heading: '# Code comments' },
    { op: 'remove_tag', tag: 'available_skills' },
    { op: 'remove_regex', pattern: "^[ \\t]*Today's date:[^\\n]*\\n", flags: 'm' },
    { op: 'replace_text', find: 'NOT PRESENT ANYWHERE', with: 'x' },
    { op: 'append', text: 'Всегда отвечай по-русски.' },
    { op: 'remove_between', start: '<!--', end: '-->' },
    { op: 'keep_sections', headings: ['# Tone', '# Tools'] },
  ];
  const unmatched = [{ scope: 'client_system', index: 3, op: 'replace_text', target: 'NOT PRESENT ANYWHERE' }];
  const row = (index, op, matched, before, after) => ({ index, op, matched, chars_before: before, chars_after: after });
  const read = (item, specs = edits) => describeReport(item, unmatched, specs);

  assert.deepEqual(read(row(0, 'remove_section', true, 11243, 10535)),
    { number: 1, title: 'Убрать раздел', matched: true, details: [['цель', '# Code comments'], ['', `вырезано ${fmtInt(708)} символов`]] });
  assert.deepEqual(read(row(1, 'remove_tag', true, 10535, 7570)).details, [['цель', 'available_skills'], ['', `вырезано ${fmtInt(2965)} символов`]]);
  assert.deepEqual(read(row(2, 'remove_regex', true, 7570, 7538)).details, [['цель', "^[ \\t]*Today's date:[^\\n]*\\n"], ['', 'вырезано 32 символа']]);
  // an edit that found nothing says what it looked for and nothing about lengths
  assert.deepEqual(read(row(3, 'replace_text', false, 7538, 7538)),
    { number: 4, title: 'Заменить текст', matched: false, details: [['искали', 'NOT PRESENT ANYWHERE']] });
  // an edit that adds text names the text, and what it added is told as such
  assert.deepEqual(read(row(4, 'append', true, 100, 126)).details, [['текст', 'Всегда отвечай по-русски.'], ['', 'добавлено 26 символов']]);
  assert.deepEqual(read(row(5, 'remove_between', true, 50, 49)).details, [['цель', '<!-- … -->'], ['', 'вырезано 1 символ']]);
  assert.deepEqual(read(row(6, 'keep_sections', true, 90, 60)).details, [['цель', '# Tone, # Tools'], ['', 'вырезано 30 символов']]);
  // an edit that matched and changed nothing is said to
  assert.deepEqual(read(row(0, 'remove_section', true, 40, 40)).details, [['цель', '# Code comments'], ['', 'текст не изменился']]);
  // the lengths are never listed raw
  assert.doesNotMatch(JSON.stringify(read(row(0, 'remove_section', true, 11243, 10535))), /chars_(before|after)|11243|10535/);

  // without the edits of the profile the miss still has its target from the list of misses, and a hit has none to name
  assert.deepEqual(read(row(3, 'replace_text', false, 5, 5), []).details, [['искали', 'NOT PRESENT ANYWHERE']]);
  assert.deepEqual(read(row(0, 'remove_section', true, 11243, 10535), []).details, [['', `вырезано ${fmtInt(708)} символов`]]);
  assert.deepEqual(describeReport(row(0, 'remove_section', true, 20, 10)).details, [['', 'вырезано 10 символов']]);
  // the edit at that place is another one now (the form has changed since the answer): its target is not borrowed
  assert.deepEqual(read(row(0, 'remove_tag', true, 20, 10)).details, [['', 'вырезано 10 символов']]);
  // what the relay itself says about the target comes first
  assert.deepEqual(read({ ...row(0, 'remove_section', true, 20, 10), target: '# Own' }).details, [['цель', '# Own'], ['', 'вырезано 10 символов']]);
  // a relay that sends no lengths and no target leaves the line with the name of the edit and its place
  assert.deepEqual(describeReport({ index: 4, op: 'append', matched: true }, [], []), { number: 5, title: 'Добавить в конец', matched: true, details: [] });
  // a target the relay left empty is no target: the edit of the profile is asked
  assert.deepEqual(describeReport({ index: 0, op: 'append', matched: true }, [{ scope: 'client_system', index: 0, op: 'append', target: '' }], [{ op: 'append', text: 'hi' }]).details, [['текст', 'hi']]);
});

test('what an edit of the client system looks for is the first thing its form names, in the order the relay uses', () => {
  assert.equal(editTarget({ op: 'remove_section', heading: '# A' }), '# A');
  assert.equal(editTarget({ op: 'keep_tag_inner', tag: 'env' }), 'env');
  assert.equal(editTarget({ op: 'remove_between', start: 'a', end: 'b' }), 'a … b');
  assert.equal(editTarget({ op: 'remove_between', start: 'a' }), 'a');
  assert.equal(editTarget({ op: 'replace_text', find: 'old', with: 'new' }), 'old');
  assert.equal(editTarget({ op: 'replace_regex', pattern: '^x', with: 'y' }), '^x');
  assert.equal(editTarget({ op: 'prepend', text: 'hello' }), 'hello');
  assert.equal(editTarget({ op: 'prepend', text: { file: 'prompts/hello.md' } }), 'файл prompts/hello.md');
  assert.equal(editTarget({ op: 'keep_sections', headings: ['# A', '', '# B'] }), '# A, # B');
  assert.equal(editTarget({ op: 'remove_tag' }), '');
  assert.equal(editTarget({ op: 'future', other: 1 }), '');
  assert.equal(editTarget(null), '');
  assert.deepEqual(systemEditsOf({ profiles: { mine: { client_system: { edits: [{ op: 'remove_tag', tag: 'x' }] } } } }, 'mine'), [{ op: 'remove_tag', tag: 'x' }]);
  assert.deepEqual(systemEditsOf({ profiles: { mine: { head: 'h' } } }, 'mine'), []);
  assert.deepEqual(systemEditsOf({ profiles: { mine: { client_system: { edits: 'junk' } } } }, 'mine'), []);
  assert.deepEqual(systemEditsOf({ profiles: {} }, 'passthrough'), []);
  assert.deepEqual(systemEditsOf(null, 'mine'), []);
});
