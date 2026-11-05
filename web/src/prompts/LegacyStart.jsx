import { useRef, useState } from 'react';
import { Plus } from 'lucide-react';
import { Button } from '../ui.jsx';
import { fmtInt } from '../format.js';
import { useFocusLater } from './hooks.js';
import NewProfile from './NewProfile.jsx';

const MODES = { standalone: 'Самостоятельные запросы', append: 'Дополнить клиента', replace: 'Заменить роль клиента' };

// legacy.role_chars is the length of the role in force: the built-in one has the length of builtin.relay_texts.role
function roleText(legacy, builtin) {
  if (!(legacy?.roleChars > 0)) return '—';
  const standard = builtin?.relayTexts?.role;
  const own = typeof standard === 'string' ? legacy.roleChars !== standard.length : null;
  return `${fmtInt(legacy.roleChars)} симв.${own === null ? '' : own ? ' (своя)' : ' (встроенная)'}`;
}

/** No prompts document in force: say which path is in force and how to start. `invalid`: a file exists but the relay refused it. */
export default function LegacyStart({ legacy, builtin, path, invalid = false, onCreate }) {
  const [starting, setStarting] = useState(false);
  const root = useRef(null);
  const focusLater = useFocusLater();
  const mode = legacy?.mode || '';
  return (
    <section className="panel p-legacy" aria-label={invalid ? 'Файл промптов не действует' : 'Файл промптов не создан'} ref={root}>
      <h2>{invalid ? 'Файл промптов не действует' : 'Файла промптов пока нет'}</h2>
      <p>
        {invalid ? 'Файл есть, но relay его не принял (причины указаны выше), поэтому ' : 'Сейчас '}
        relay работает в прежнем режиме (legacy): system клиента уходит к модели как есть, а роль relay задают настройки
        «Применение инструкций» и «Инструкции для модели». Профиль с <code>{'"use": "$legacy"'}</code> воспроизводит это поведение
      </p>
      <dl className="p-facts">
        <div><dt>Режим ролей</dt><dd className="mono">{mode ? `${MODES[mode] || mode} (${mode})` : '—'}</dd></div>
        <div><dt>Роль relay</dt><dd className="mono">{roleText(legacy, builtin)}</dd></div>
        <div><dt>Файл</dt><dd className="mono">{path || 'prompts.json'}</dd></div>
      </dl>
      <p className="p-hint">
        Эти настройки применяются сразу, без перезапуска, и к начатым диалогам тоже: смена роли посреди диалога перезаписывает кэш промпта
        активных диалогов. Закрепление за ревизией есть только у профилей из файла
      </p>
      <p>
        {invalid
          ? 'Исправьте файл на диске: relay подхватит его сам. Или начните документ здесь: сохранение заменит файл'
          : 'Чтобы начать, создайте первый профиль. Он сохранится в файле рядом с config.json и заработает без перезапуска, как только его выберет правило. Пока ни одно правило не подходит и профиля по умолчанию нет, запросы идут прежним путём'}
      </p>
      {starting ? (
        <NewProfile taken={['passthrough']} pattern={builtin?.profileName} title="Первый профиль" onCreate={onCreate}
          onCancel={() => { setStarting(false); focusLater(() => root.current?.querySelector('[data-act="start"]')); }} />
      ) : (
        <div><Button tone="primary" icon={Plus} data-act="start" onClick={() => setStarting(true)}>Создать первый профиль</Button></div>
      )}
    </section>
  );
}
