// The spend chart's unit and axis labels, as plain functions (node --test checks them in web/tests/adminViews.test.mjs).
import { fmtFixed, isNum } from './format.js';

/** Whether the window holds a dollar spend (universal providers): the "Кредиты / USD" switch is drawn only then. */
export const hasUsdSpend = (totals) => isNum(totals?.cost_usd) && totals.cost_usd > 0;

/**
 * The unit the spend chart shows. A unit picked by hand holds only while the window has dollars: the switch is drawn
 * only then, and a "usd" kept after the dollars left the window showed "Расхода за окно не было" with credits being
 * spent, with nothing on the page to bring the credits back (audit A14-09). Without a pick, dollars lead only in a
 * window that has no credit spend.
 */
export function spendUnitOf(chosen, totals) {
  if (!hasUsdSpend(totals)) return 'credits';
  return chosen || (totals.credits > 0 ? 'credits' : 'usd');
}

/** An axis label: dollars with the cents and their fractions a small spend needs ($0,0040), credits as before. */
export function spendAxisLabel(value, unit) {
  return unit === 'usd' ? `$${fmtFixed(value, value < 0.1 ? 4 : 2)}` : fmtFixed(value, value < 1 ? 2 : 1);
}

/** Whether the spend chart's peak annotation differs from the top Y-axis tick (or is an unsure lower bound). */
export function showSpendPeak(peak, max, unit, unsure = false) {
  return Boolean(unsure) || spendAxisLabel(peak, unit) !== spendAxisLabel(max, unit);
}
