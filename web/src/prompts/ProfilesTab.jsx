import { useState } from 'react';
import { Plus } from 'lucide-react';
import { Button, Tag } from '../ui.jsx';
import { Notice } from './controls.jsx';
import { addProfile, profileUsage, profilesOf, removeProfile, renameProfile, starterProfile, uniqueName } from './draft.js';
import LivePreview from './LivePreview.jsx';
import NewProfile from './NewProfile.jsx';
import ProfileEditor from './ProfileEditor.jsx';

export default function ProfilesTab({ apiKey, doc, setDoc, builtin, selected, select, onAddRule, live, onUnauthorized }) {
  const [creating, setCreating] = useState(false);
  const names = Object.keys(profilesOf(doc));
  const builtinNames = builtin.profiles.length ? builtin.profiles : ['passthrough'];
  const current = names.includes(selected) ? selected : builtinNames.includes(selected) ? selected : names[0] || null;
  const textNames = Object.keys(doc.texts || {});
  const isBuiltin = !!current && builtinNames.includes(current);

  const create = (name, starter) => {
    setDoc((d) => addProfile(d, name, starterProfile(starter)));
    select(name);
    setCreating(false);
  };
  const duplicate = () => {
    const name = uniqueName(`${current}-copy`, names);
    setDoc((d) => addProfile(d, name, structuredClone(d.profiles[current])));
    select(name);
  };
  const remove = () => {
    if (!window.confirm(`Убрать профиль ${current} из черновика? Изменения вступят в силу после сохранения`)) return;
    setDoc((d) => removeProfile(d, current));
    select(null);
  };
  const rename = (next) => {
    setDoc((d) => renameProfile(d, current, next));
    select(next);
  };

  return (
    <div className="p-profiles">
      <div className="p-profile-bar" role="group" aria-label="Профили">
        {[...builtinNames, ...names].map((name) => (
          <button type="button" key={name} className={`chip mono ${name === current ? 'on' : ''}`} aria-current={name === current ? 'true' : undefined} title={name}
            onClick={() => { select(name); setCreating(false); }}>
            <span className="p-chip-name">{name}</span>{builtinNames.includes(name) && <>{' '}<Tag>встроенный</Tag></>}
          </button>
        ))}
        <Button icon={Plus} onClick={() => setCreating(true)} disabled={creating}>Новый профиль</Button>
      </div>
      {creating && <NewProfile taken={[...names, ...builtinNames]} suggested={uniqueName('mine', names)} onCreate={create} onCancel={() => setCreating(false)} />}
      {!creating && isBuiltin && (
        <section className="panel">
          <h2 className="mono">{current}</h2>
          <p className="p-hint">Встроенный профиль: head — <code>{'{{client_system}}'}</code>, то есть system клиента без изменений. Его используют защитные правила для служебных запросов. Переопределить его нельзя</p>
        </section>
      )}
      {!creating && !isBuiltin && current && (
        <div className="p-profile-layout">
          <section className="panel p-profile-panel" aria-label={`Профиль ${current}`}>
            <ProfileEditor key={current} doc={doc} setDoc={setDoc} name={current} builtin={builtin} textNames={textNames}
              usage={profileUsage(doc, current)} onRename={rename} onDuplicate={duplicate} onDelete={remove} onAddRule={() => onAddRule(current)} />
          </section>
          <LivePreview apiKey={apiKey} doc={doc} profile={current} {...live} onUnauthorized={onUnauthorized} />
        </div>
      )}
      {!creating && !isBuiltin && !current && (
        <Notice tone="warn">Профилей пока нет. Создайте первый: он начнёт с system клиента без изменений</Notice>
      )}
    </div>
  );
}
