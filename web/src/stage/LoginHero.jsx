// Parts are imported from their own files, so only their CSS is bundled.
import { Stage } from './kit/stage.tsx';
import { Bar, Caret, Prompt, Text, Window } from './kit/window.tsx';
import { Collapse } from './kit/motion/Collapse.tsx';
import { Count } from './kit/motion/Count.tsx';
import { States } from './kit/motion/States.tsx';
import { useTheme } from '../theme.js';

// Login hero, built on the stageshow hero-demo / payout-flow pattern: no cursor,
// one request travels from an IDE terminal into the relay journal. Real words only
// on the focal row, the command and the credits figure; filler everywhere else.

export const STAGE_TOKENS = {
  '--ss-accent': '#f5a524',
  '--ss-accent-soft': 'rgba(245, 165, 36, 0.12)',
  '--ss-accent-text': '#f5a524',
  '--ss-font': "'Manrope Variable', 'Segoe UI', sans-serif",
  '--ss-mono': "'IBM Plex Mono', Consolas, monospace",
};

const BALANCE = 1479.89;
const COST = 0.84;
const COMMAND = 'исправь падающий тест';

const initial = { typed: '', sent: false, row: false, phase: 0, credits: BALANCE, done: false, fade: false };
const POSTER = { typed: COMMAND, sent: true, row: true, phase: 4, credits: BALANCE - COST, done: true, fade: false };

async function script(a) {
  await a.wait(700);
  await a.type('typed', COMMAND, { interval: 55 });
  await a.wait(400);
  await a.set({ sent: true });
  await a.wait(500);
  // The request lands in the relay: a new row opens and pushes the older ones down
  await a.set({ row: true });
  await a.wait(700);
  for (const phase of [1, 2, 3, 4]) {
    await a.set({ phase });
    await a.wait(phase === 2 ? 900 : 600);
  }
  // Payoff: the turn is billed, the balance rolls down and holds
  await a.set({ credits: BALANCE - COST, done: true });
  a.mark('payoff');
  await a.wait(3200);
  // The next request starts: the terminal clears first, then the scene resets out of view
  await a.set({ typed: '', sent: false, done: false });
  await a.wait(500);
  await a.set({ row: false, phase: 0, fade: true });
  await a.wait(700);
  // The balance changes back only while it is faded out, so it never rolls up in view
  await a.snap({ ...initial, fade: true });
  await a.wait(100);
  await a.set({ fade: false });
  await a.wait(400);
}

const fmt = (n) => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);

function Tape({ phase }) {
  return (
    <span className="lh-tape" data-phase={phase}>
      <i className="p1" /><i className="p2" /><i className="p3" /><i className="p4" />
    </span>
  );
}

function FillerRow({ w }) {
  return (
    <div className="lh-row">
      <Bar w={44} h={5} tone={2} />
      <Bar w={w} h={6} tone={3} />
      <Bar w={22} h={6} tone={2} />
      <Bar w={30} h={5} tone={2} />
    </div>
  );
}

export function LoginHero() {
  const theme = useTheme();
  return (
    <Stage width={720} height={340} compact={{ below: 560, width: 358, height: 340 }} initial={initial} poster={POSTER}
      script={script} theme={theme === 'light' ? 'light' : 'dark'} tokens={STAGE_TOKENS} className="lh">
      {(s) => (
        <>
          <Window title="relay · журнал" className="lh-journal">
            <div className="lh-body">
              <aside className="lh-side">
                {[56, 44, 50, 38].map((w, i) => (
                  <div key={i} className="lh-nav" data-on={i === 1 || undefined}>
                    <Bar w={10} h={10} r={3} tone={i === 1 ? 4 : 2} />
                    {i === 1 ? <Text className="lh-nav-label">Журнал</Text> : <Bar w={w} h={6} tone={2} />}
                  </div>
                ))}
              </aside>
              <div className="lh-main">
                <div className="lh-head">
                  <Text className="lh-title">Журнал событий</Text>
                  <span className="lh-credits" data-hidden={s.fade || undefined}>
                    <span className="lh-credits-label">кредиты</span>
                    <Count value={s.credits} format={fmt} className="lh-credits-value" />
                  </span>
                </div>
                <div className="lh-rows">
                  <Collapse open={s.row}>
                    <div className="lh-row lh-focal" data-target="focal">
                      <span className="lh-time">22:41:07</span>
                      <span className="lh-model">claude-opus-5-5</span>
                      <States value={s.phase < 4 ? 'run' : 'ok'} align="start" className="lh-status"
                        states={{ run: <span className="lh-run">идёт</span>, ok: <span className="lh-ok">200</span> }} />
                      <States value={s.done ? 'dur' : 'none'} align="end" className="lh-dur"
                        states={{ none: <span>—</span>, dur: <span>21,8 с</span> }} />
                      <Tape phase={s.phase} />
                    </div>
                  </Collapse>
                  <FillerRow w={96} />
                  <FillerRow w={72} />
                  <FillerRow w={88} />
                  <FillerRow w={64} />
                </div>
              </div>
            </div>
          </Window>

          <Window chrome="terminal" title="IDE" className="lh-term" lights="grey">
            <Prompt path="~/app" data-typing>{s.typed}{!s.sent && <Caret still={s.typed.length > 0 && s.typed.length < COMMAND.length} />}</Prompt>
            <Collapse open={s.sent}>
              <div className="lh-term-line">→ relay 127.0.0.1:8787</div>
            </Collapse>
            <Collapse open={s.done}>
              <div className="lh-term-line lh-term-ok">ответ получен · {fmt(COST)} кредита</div>
            </Collapse>
          </Window>
        </>
      )}
    </Stage>
  );
}
