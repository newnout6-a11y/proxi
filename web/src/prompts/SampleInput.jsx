import { useId } from 'react';
import { Area, Select } from './controls.jsx';
import { DIALECTS, recentLabel } from './sample.js';

/** Picks the request a preview is made for: a recent one the relay still holds, or a body pasted by hand. */
export default function SampleInput({ value, onChange, recent, error, detected, onRefresh }) {
  const group = useId();
  const usable = recent.filter((item) => item.has_request);
  const set = (patch) => onChange({ ...value, ...patch });
  return (
    <fieldset className="p-sample">
      <legend>Образец запроса</legend>
      <div className="p-modes">
        <label className="check">
          <input type="radio" name={group} checked={value.mode === 'recent'} onChange={() => set({ mode: 'recent' })} />
          <span>Из последних запросов</span>
        </label>
        <label className="check">
          <input type="radio" name={group} checked={value.mode === 'paste'} onChange={() => set({ mode: 'paste' })} />
          <span>Своё тело запроса</span>
        </label>
      </div>
      {value.mode === 'recent' ? (
        usable.length ? (
          <Select label="Запрос" value={usable.some((item) => item.request_id === value.requestId) ? value.requestId : ''}
            onChange={(requestId) => set({ requestId })}
            options={[{ value: '', label: 'Выберите запрос' }, ...usable.map((item) => ({ value: item.request_id, label: recentLabel(item) }))]}
            hint="Relay хранит тела только последних запросов. Предпросмотр прогонит выбранный запрос через документ" />
        ) : (
          <p className="p-hint">
            Relay пока не хранит тел запросов: отправьте запрос через relay или вставьте тело вручную.
            {onRefresh && <> <button type="button" className="link-button" onClick={onRefresh}>Проверить ещё раз</button></>}
          </p>
        )
      ) : (
        <>
          <Area label="Тело запроса (JSON)" value={value.text} onChange={(text) => set({ text })} rows={8}
            placeholder={'{"model": "claude-opus-5-5", "system": "…", "messages": [{"role": "user", "content": "привет"}]}'} />
          <div className="p-pair">
            <Select label="Диалект" value={value.dialect} onChange={(dialect) => set({ dialect })}
              options={[{ value: '', label: detected ? `Определить по телу (${detected})` : 'Определить по телу' }, ...DIALECTS]} />
            <Area label="Заголовки" value={value.headersText} onChange={(headersText) => set({ headersText })} rows={3}
              placeholder={'User-Agent: opencode/1.4\nx-proxi-profile: mine'} hint="Строки «Имя: значение» или JSON-объект. Нужны, чтобы определить клиента" />
          </div>
        </>
      )}
      {error && <p className="p-error">{error}</p>}
    </fieldset>
  );
}
