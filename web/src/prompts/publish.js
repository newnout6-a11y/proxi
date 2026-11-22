// Which catalog models a profile's publish_as_model publishes, matched the way the relay matches them
// (models._publishes: fnmatch on the lower-cased model id, so * ? and [seq] work and case does not matter).

function globRegex(glob) {
  let source = '';
  for (let index = 0; index < glob.length; index += 1) {
    const char = glob[index];
    if (char === '*') source += '.*';
    else if (char === '?') source += '.';
    else if (char === '[') {
      const start = index + 1;
      let end = start;
      if (glob[end] === '!') end += 1;
      if (glob[end] === ']') end += 1;
      while (end < glob.length && glob[end] !== ']') end += 1;
      if (end === glob.length) { source += '\\['; continue; }
      // Python fnmatch removes reversed ranges rather than rejecting the whole glob.
      // Keep range hyphens apart from literal hyphens, as fnmatch.translate does.
      let inner = glob.slice(start, end);
      if (inner.includes('-')) {
        const chunks = [];
        let at = start;
        let search = start + (glob[start] === '!' ? 2 : 1);
        while (true) {
          const dash = glob.indexOf('-', search);
          if (dash < 0 || dash >= end) break;
          chunks.push(glob.slice(at, dash));
          at = dash + 1;
          search = dash + 3;
        }
        const tail = glob.slice(at, end);
        if (tail) chunks.push(tail);
        else chunks[chunks.length - 1] += '-';
        for (let part = chunks.length - 1; part > 0; part -= 1) {
          if (chunks[part - 1].at(-1) > chunks[part][0]) {
            chunks[part - 1] = chunks[part - 1].slice(0, -1) + chunks[part].slice(1);
            chunks.splice(part, 1);
          }
        }
        inner = chunks.map((chunk) => chunk.replace(/\\/g, '\\\\').replace(/-/g, '\\-')).join('-');
      } else inner = inner.replace(/\\/g, '\\\\');
      if (!inner) source += '(?!)';
      else if (inner === '!') source += '.';
      else {
        if (inner.startsWith('!')) inner = '^' + inner.slice(1);
        else if (inner.startsWith('^')) inner = '\\' + inner;
        // A leading ] is a literal in Python, but closes a JavaScript class.
        source += `[${inner.replace(/\]/g, '\\]')}]`;
      }
      index = end;
    } else source += char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${source}$`, 's');
}

/** Non-empty model globs configured in `profile.publish_as_model`, or `[]` when off / `"*"` (`true`). */
export function publishMasksOf(profile) {
  return Array.isArray(profile?.publish_as_model)
    ? profile.publish_as_model.filter((glob) => typeof glob === 'string' && glob.trim())
    : [];
}

/** True when the profile is exposed as virtual `<model>@<profile>` entries in `/v1/models`. */
export function hasPublishAsModel(profile) {
  return profile?.publish_as_model === true || publishMasksOf(profile).length > 0;
}

/** True when `value` matches any case-insensitive fnmatch glob in `globs`. */
export function matchesGlobs(globs, value) {
  if (value === undefined || value === null || value === '') return false;
  const list = Array.isArray(globs) ? globs : [globs];
  const target = String(value).toLowerCase();
  return list.some((glob) => {
    if (typeof glob !== 'string' || !glob) return false;
    try { return globRegex(glob.toLowerCase()).test(target); } catch { return false; }
  });
}

/**
 * Summarize which models can reach `profileName` in production (via `default_profile`, `rules`, or `publish_as_model`).
 * Used both to pick a representative preview sample and to warn when forced preview hits a different model.
 */
export function profileModelScope(doc, profileName) {
  const profile = doc?.profiles?.[profileName] || {};
  const publishMasks = publishMasksOf(profile);
  const publishedAll = profile.publish_as_model === true;
  const isDefault = doc?.default_profile === profileName;
  const rules = (Array.isArray(doc?.rules) ? doc.rules : []).filter(
    (rule) => rule?.enabled !== false && rule?.use === profileName,
  );
  const openRule = rules.some((rule) => {
    const modelCond = rule?.if?.model;
    return modelCond === undefined || modelCond === null || modelCond === '' || (Array.isArray(modelCond) && !modelCond.length);
  });
  const ruleModelMasks = rules.flatMap((rule) => {
    const modelCond = rule?.if?.model;
    return Array.isArray(modelCond) ? modelCond : modelCond ? [String(modelCond)] : [];
  }).filter((glob) => typeof glob === 'string' && glob.trim());
  const modelMasks = [...new Set([...publishMasks, ...ruleModelMasks])];
  const unrestricted = isDefault || publishedAll || openRule;
  return {
    isDefault,
    publishedAll,
    publishMasks,
    rules,
    modelMasks,
    unrestricted,
    /** True if unrestricted or `modelId` matches `modelMasks`; false if restricted elsewhere; null if no scope is defined yet. */
    matchesModel(modelId) {
      if (unrestricted) return true;
      if (!modelMasks.length) return null;
      return matchesGlobs(modelMasks, modelId);
    },
  };
}

/** The ids of `catalog` (routable model ids) that `setting` (false | true | glob list) publishes; null while the catalog is unknown. */
export function publishedModels(setting, catalog) {
  if (!Array.isArray(catalog)) return null;
  if (setting === true) return catalog;
  const globs = Array.isArray(setting) ? setting.filter((glob) => typeof glob === 'string' && glob) : [];
  return catalog.filter((id) => matchesGlobs(globs, id));
}
