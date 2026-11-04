import { Stage } from './kit/stage.tsx';
import { Bar, Window } from './kit/window.tsx';
import { Badge } from './kit/controls/Badge.tsx';
import { useTheme } from '../theme.js';
import { stageTokens } from './LoginHero.jsx';

// Where the add-account values come from, as a still on the static-card gold: a
// browser on the Retool editor with DevTools on the request headers. Numbered
// accent marks pair each source with the form field of the same number; real
// words only on the address and the two headers the relay sends.

const UUID = '8f3a2c1e-…';

function Num({ n }) {
  return <Badge tone="accent" className="ag-num">{n}</Badge>;
}

export function AddGuide() {
  const theme = useTheme();
  return (
    <div className="add-guide" role="img"
      aria-label="Схема: значения полей 1 и 2 берутся из строки браузера в редакторе Retool, 3 и 4 — из заголовков запроса во вкладке Network">
      <Stage width={400} height={248} compact={{ below: 400, width: 320, height: 248 }}
        theme={theme === 'light' ? 'light' : 'dark'} tokens={stageTokens(theme)} className="ag">
        {() => (
          <Window className="ag-window" urlShape="rounded"
            url={(
              <span className="ag-url">
                <Num n={1} /><span className="ag-mark">tenant.retool.com</span>
                <span className="ag-path">/editor/</span>
                <Num n={2} /><span className="ag-mark">{UUID}</span>
              </span>
            )}>
            <div className="ag-tools">
              <div className="ag-tabs">
                <Bar w={44} h={5} tone={2} />
                <Bar w={40} h={5} tone={2} />
                <span className="ag-tab">Network</span>
                <Bar w={36} h={5} tone={2} />
              </div>
              <div className="ag-head">Request headers</div>
              <div className="ag-row ag-focal">
                <Num n={3} />
                <span className="ag-key">cookie</span>
                <span className="ag-val"><Bar w={176} h={6} tone={3} /></span>
              </div>
              <div className="ag-row ag-focal">
                <Num n={4} />
                <span className="ag-key">x-xsrf-token</span>
                <span className="ag-val"><Bar w={88} h={6} tone={3} /></span>
              </div>
              {[[52, 96], [40, 72]].map(([k, v], i) => (
                <div className="ag-row" key={i}>
                  <span />
                  <Bar w={k} h={5} tone={2} />
                  <span className="ag-val"><Bar w={v} h={6} tone={2} /></span>
                </div>
              ))}
            </div>
          </Window>
        )}
      </Stage>
    </div>
  );
}

export function GuideNum({ n }) {
  return <span className="guide-num" aria-hidden="true">{n}</span>;
}
