import assert from 'node:assert/strict';
import test from 'node:test';
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
