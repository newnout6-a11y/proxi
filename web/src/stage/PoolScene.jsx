// Parts are imported from their own files, so only their CSS is bundled.
import { Stage } from './kit/stage.tsx';
import { Bar, Text, Window } from './kit/window.tsx';
import { Collapse } from './kit/motion/Collapse.tsx';
import { Count } from './kit/motion/Count.tsx';
import { Fly } from './kit/motion/Fly.tsx';
import { States } from './kit/motion/States.tsx';
import { Toggle } from './kit/controls/Toggle.tsx';
import { useTheme } from '../theme.js';
import { stageTokens } from './LoginHero.jsx';

// How the pool routes, as a system story on the stageshow `subscriptions` gold:
// no cursor. A turn leaves the IDE and its cost rides into the account with the
// lowest priority; that account's balance rolls under the reserve threshold and
// it steps back. The next turn goes to the spare account. The switched-off
// account never receives anything. The terminal and the pool window are the two
// places; the cost chip is the only thing that travels between them.

const COST = 4.2;
const START = [24.1, 1479.89];
const fmt = (n) => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);

const initial = { lines: 0, flown: 0, landed: [false, false], bal: START, reserve: false, serving: 0, fly: null, fade: false };
const POSTER = { lines: 2, flown: 2, landed: [true, true], bal: [START[0] - COST, START[1] - COST], reserve: true, serving: 1, fly: null, fade: false };

async function send(a, line, row) {
  // A turn finishes in the IDE: its line opens, then the cost buds off and travels
  await a.set({ lines: line });
  await a.wait(600);
  const from = a.rect(`src-${line}`, { layout: true });
  const to = a.rect(`pill-${row}`, { layout: true });
  // The chip buds off the IDE line in the same frame the line's own pill hides
  await a.set({ fly: { from, to, go: false }, flown: line });
  await a.wait(200);
  await a.set((s) => ({ fly: { ...s.fly, go: true } }));
  await a.wait(900);
  // Landed: the row's own pill takes over in the frame the chip goes, then the balance rolls
  await a.set((s) => ({ fly: null, landed: s.landed.map((v, i) => v || i === row),
    bal: s.bal.map((v, i) => (i === row ? v - COST : v)) }));
}

async function script(a) {
  await a.wait(800);
  await send(a, 1, 0);
  await a.wait(900);
  // Under the threshold: the main account steps back into the reserve
  await a.set({ reserve: true });
  await a.wait(900);
  // New work now goes to the next account by priority
  await a.set({ serving: 1 });
  await a.wait(700);
  await send(a, 2, 1);
  a.mark('payoff');
  await a.wait(3400);
  // The IDE clears first, then the figures reset while faded, so nothing rolls back in view
  await a.set({ lines: 0 });
  await a.wait(500);
  await a.set({ fade: true });
  await a.wait(500);
  await a.snap({ ...initial, fade: true });
  await a.wait(100);
  await a.set({ fade: false });
  await a.wait(500);
}

const ROWS = [
  { name: 'основной', meta: 'приоритет 0' },
  { name: 'запасной', meta: 'приоритет 1' },
  { name: 'третий', meta: 'выключен', off: true, balance: 812.4 },
];

function status(s, i) {
  if (i === 2) return 'off';
  if (i === 0 && s.reserve) return 'reserve';
  return 'active';
}

export function PoolScene() {
  const theme = useTheme();
  return (
    <div className="pool-scene" role="img"
      aria-label="Как пул выбирает аккаунт: ход уходит на включённый аккаунт с меньшим приоритетом; когда его баланс ниже порога, он уходит в резерв и следующий ход получает запасной; выключенный аккаунт ходов не получает">
      <Stage width={720} height={300} compact={{ below: 600, width: 358, height: 352 }} initial={initial} poster={POSTER}
        script={script} theme={theme === 'light' ? 'light' : 'dark'} tokens={stageTokens(theme)} className="ps">
        {(s) => (
          <>
            <Window title="relay · аккаунты" className="ps-window">
              <div className="ps-body">
                <aside className="ps-side">
                  {[52, 60, 44, 56].map((w, i) => (
                    <div key={i} className="ps-nav" data-on={i === 1 || undefined}>
                      <Bar w={10} h={10} r={3} tone={i === 1 ? 4 : 2} />
                      {i === 1 ? <Text className="ps-nav-label">Аккаунты</Text> : <Bar w={w} h={6} tone={2} />}
                    </div>
                  ))}
                </aside>
                <div className="ps-main">
                  <div className="ps-head">
                    <Text className="ps-title">Пул</Text>
                    <span className="ps-meta">резерв ниже 20</span>
                  </div>
                  <div className="ps-rows" data-hidden={s.fade || undefined}>
                    {ROWS.map((row, i) => (
                      <div key={row.name} className="ps-row" data-serving={s.serving === i || undefined}
                        data-dim={status(s, i) !== 'active' || undefined}>
                        <span className="ps-name">
                          <span className="ps-strong">{row.name}</span>
                          <span className="ps-sub">{row.meta}</span>
                        </span>
                        <span className="ps-balance">
                          {row.off ? fmt(row.balance) : <Count value={s.bal[i]} format={fmt} duration={900} />}
                        </span>
                        <States value={status(s, i)} align="start" className="ps-status"
                          states={{ active: <span className="ps-active">в работе</span>, reserve: <span className="ps-reserve">резерв</span>, off: <span>выключен</span> }} />
                        <span className="ps-slot">
                          {i < 2 && <span className="ps-pill" data-target={`pill-${i}`} data-landed={s.landed[i] || undefined}>−{fmt(COST)}</span>}
                        </span>
                        <Toggle on={!row.off} className="ps-toggle" />
                      </div>
                    ))}
                    {[64, 48].map((w, i) => (
                      <div key={i} className="ps-row ps-filler">
                        <span className="ps-name"><Bar w={w} h={6} tone={3} /><Bar w={44} h={5} tone={2} /></span>
                        <Bar w={52} h={6} tone={2} />
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </Window>

            <Window chrome="terminal" title="IDE" className="ps-term" lights="grey">
              {[1, 2].map((n) => (
                <Collapse key={n} open={s.lines >= n}>
                  <div className="ps-line">
                    <span>ход {n} · opus 5.5</span>
                    <span className="ps-pill ps-src" data-target={`src-${n}`} data-gone={s.flown >= n || undefined}>−{fmt(COST)}</span>
                  </div>
                </Collapse>
              ))}
              <Collapse open={s.lines === 0}>
                <div className="ps-line ps-wait">ждёт ход</div>
              </Collapse>
            </Window>

            {s.fly && (
              <Fly from={s.fly.from} to={s.fly.to} go={s.fly.go} held duration={900}>
                <span className="ps-pill ps-chip">−{fmt(COST)}</span>
              </Fly>
            )}
          </>
        )}
      </Stage>
    </div>
  );
}
