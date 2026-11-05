import { useState } from 'react';
import { Plus } from 'lucide-react';
import { Button } from '../ui.jsx';
import { fmtInt } from '../format.js';
import NewProfile from './NewProfile.jsx';

const MODES = { standalone: 'Самостоятельные запросы', append: 'Дополнить клиента', replace: 'Заменить роль клиента' };

/** No prompts file yet: say which path is in force and how to start. */
export default function LegacyStart({ legacy, path, taken = [], onCreate }) {
  const [starting, setStarting] = useState(false);
  const mode = legacy?.mode || '';
  return (
    <section className="panel p-legacy" aria-label="Файл промптов не создан">
      <h2>Файла промптов пока нет</h2>
      <p>
        Сейчас relay работает в прежнем режиме (legacy): system клиента уходит к модели как есть, а роль relay задают настройки
        «Применение инструкций» и «Инструкции для модели». Профиль с <code>{'"use": "$legacy"'}</code> воспроизводит это поведение
      </p>
      <dl className="p-facts">
        <div><dt>Режим ролей</dt><dd className="mono">{mode ? `${MODES[mode] || mode} (${mode})` : '—'}</dd></div>
        <div><dt>Своя роль relay</dt><dd className="mono">{legacy?.roleChars > 0 ? `${fmtInt(legacy.roleChars)} симв.` : 'нет: действует встроенная'}</dd></div>
        <div><dt>Файл</dt><dd className="mono">{path || 'prompts.json'}</dd></div>
      </dl>
      <p>
        Чтобы начать, создайте первый профиль. Он сохранится в файле рядом с config.json и заработает без перезапуска, как только его выберет правило.
        Пока ни одно правило не подходит и профиля по умолчанию нет, запросы идут прежним путём
      </p>
      {starting ? (
        <NewProfile taken={['passthrough', ...taken]} title="Первый профиль" onCreate={onCreate} onCancel={() => setStarting(false)} />
      ) : (
        <div><Button tone="primary" icon={Plus} onClick={() => setStarting(true)}>Создать первый профиль</Button></div>
      )}
    </section>
  );
}
