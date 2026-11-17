import assert from 'node:assert/strict';
import test from 'node:test';
import { WARN_DAYS, describeResult, fmtDate, tokenExpiry, tokenLines } from '../src/minara.js';

const DAY = 86400;
const NBSP = ' ';
// A local moment, so the expected words do not depend on the time zone the tests run in.
const DEC_3 = new Date(2026, 11, 3, 14, 5).getTime() / 1000;

test('the tokens field gives one token per non-empty line, with the line it is on', () => {
  const text = ['', '  eyJa.b.c  ', 'eyJd.e.f\r', '', 'eyJg.h.i'].join('\n');
  assert.deepEqual(tokenLines(text), [{ line: 2, token: 'eyJa.b.c' }, { line: 3, token: 'eyJd.e.f' }, { line: 5, token: 'eyJg.h.i' }]);
  assert.deepEqual(tokenLines(''), []);
  assert.deepEqual(tokenLines(undefined), []);
});

test('a date is a day of the month, with the time when asked', () => {
  assert.equal(fmtDate(DEC_3), '3 декабря');
  assert.equal(fmtDate(DEC_3, true), '3 декабря, 14:05');
  assert.equal(fmtDate(null), '—');
});

test('a token is valid, expiring under three days before its end, expired at it, or of unknown term', () => {
  assert.equal(WARN_DAYS, 3);
  assert.deepEqual(tokenExpiry(null, DEC_3), { state: 'unknown', text: 'токен: срок неизвестен' });
  assert.deepEqual(tokenExpiry(DEC_3, DEC_3 - 24 * DAY), { state: 'valid', text: `токен до 3 декабря, ещё 24${NBSP}дн` });
  assert.deepEqual(tokenExpiry(DEC_3, DEC_3 - 3 * DAY), { state: 'valid', text: `токен до 3 декабря, ещё 3${NBSP}дн` });
  assert.deepEqual(tokenExpiry(DEC_3, DEC_3 - 2.5 * DAY),
    { state: 'expiring', text: `токен до 3 декабря, 14:05, осталось 2,5${NBSP}дн` });
  assert.equal(tokenExpiry(DEC_3, DEC_3 - 3600).text, `токен до 3 декабря, 14:05, осталось 1${NBSP}ч`);
  assert.deepEqual(tokenExpiry(DEC_3, DEC_3), { state: 'expired', text: 'токен истёк 3 декабря, нужен новый' });
});

test('every outcome of a bulk add is said by line, never by the token', () => {
  const lineOf = (position) => [3, 5, 9][position - 1];
  assert.deepEqual(describeResult({ token: 1, status: 'added', label: 'Minara 52', expires_at: DEC_3 }, lineOf),
    { tone: 'pos', tag: 'добавлен', text: 'Minara 52, токен до 3 декабря' });
  assert.deepEqual(describeResult({ token: 2, status: 'duplicate', reason: 'same_token', label: 'Minara 7', account: 'ab12cd34' }),
    { tone: 'muted', tag: 'уже в пуле', text: 'Minara 7' });
  assert.match(describeResult({ token: 2, status: 'duplicate', reason: 'same_user', account: 'ab12cd34', newer: true }).text,
    /^тот же пользователь Minara, что у ab12cd34; этот токен новее, но аккаунт берёт токен из переменной окружения$/);
  assert.deepEqual(describeResult({ token: 4, status: 'renewed', label: 'Minara 7', expires_at: DEC_3 }),
    { tone: 'pos', tag: 'токен обновлён', text: 'Minara 7, новый токен до 3 декабря' });
  assert.equal(describeResult({ token: 3, status: 'duplicate', reason: 'same_token', repeat_of: 1, label: 'Minara 52' }, lineOf).text,
    'повторяет строку 3');
  assert.equal(describeResult({ token: 3, status: 'duplicate', reason: 'same_user', repeat_of: 2 }, lineOf).text,
    'тот же пользователь Minara, что в строке 5');
  assert.deepEqual(describeResult({ token: 3, status: 'expired', expires_at: DEC_3 }),
    { tone: 'neg', tag: 'истёк', text: '3 декабря, 14:05' });
  assert.equal(describeResult({ token: 3, status: 'invalid', problem: 'cut', error: 'starts like a JWT' }).text,
    'начало JWT без остального: похоже, токен обрезан при копировании');
  assert.equal(describeResult({ token: 3, status: 'invalid', problem: 'rejected', error: 'token 3: base must be HTTPS' }).text,
    'token 3: base must be HTTPS');
});
