import { Stage } from './kit/stage.tsx';
import { Bar, Text, Window } from './kit/window.tsx';
import { useTheme } from '../theme.js';
import { STAGE_TOKENS } from './LoginHero.jsx';

// Card-size stills for empty states (stageshow static-card pattern): a window pinned
// top-left, running off the right and bottom, real words only on the focal row.

function Still({ children, label }) {
  const theme = useTheme();
  return (
    <div className="art" role="img" aria-label={label}>
      <Stage width={400} height={220} compact={{ below: 400, width: 320, height: 200 }}
        theme={theme === 'light' ? 'light' : 'dark'} tokens={STAGE_TOKENS} className="art-stage">
        {() => children}
      </Stage>
    </div>
  );
}

export function JournalArt() {
  return (
    <Still label="Так будет выглядеть строка журнала">
      <Window title="журнал" className="art-window">
        <div className="art-rows">
          <div className="art-row art-focal">
            <span className="art-time">22:41:07</span>
            <span className="art-msg">POST /v1/messages</span>
            <span className="art-tag">200</span>
          </div>
          {[120, 96, 132, 84].map((w, i) => (
            <div className="art-row" key={i}>
              <Bar w={48} h={5} tone={2} />
              <Bar w={w} h={6} tone={3} />
              <Bar w={18} h={5} tone={2} />
            </div>
          ))}
        </div>
      </Window>
    </Still>
  );
}

export function TurnsArt() {
  return (
    <Still label="Так будет выглядеть лента хода">
      <Window title="ходы" className="art-window">
        <div className="art-rows">
          <div className="art-row art-focal art-turn">
            <Text className="art-model">claude-opus-5-5</Text>
            <span className="art-tape"><i className="p1" /><i className="p2" /><i className="p3" /><i className="p4" /></span>
            <span className="art-time">21,8 с</span>
          </div>
          {[[90, 62], [70, 40], [84, 78]].map(([name, tape], i) => (
            <div className="art-row art-turn" key={i}>
              <Bar w={name} h={6} tone={3} />
              <span className="art-tape art-tape-filler" style={{ width: `${tape}%` }} />
              <Bar w={28} h={5} tone={2} />
            </div>
          ))}
        </div>
      </Window>
    </Still>
  );
}
