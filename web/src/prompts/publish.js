// Which catalog models a profile's publish_as_model publishes, matched the way the relay matches them
// (models._publishes: fnmatch on the lower-cased model id, so * ? and [seq] work and case does not matter).

function globRegex(glob) {
  let source = '';
  for (let index = 0; index < glob.length; index += 1) {
    const char = glob[index];
    if (char === '*') source += '.*';
    else if (char === '?') source += '.';
    else if (char === '[') {
      const end = glob.indexOf(']', index + 2);
      if (end < 0) { source += '\\['; continue; }
      let inner = glob.slice(index + 1, end).replace(/\\/g, '\\\\');
      if (inner.startsWith('!')) inner = '^' + inner.slice(1);
      source += `[${inner}]`;
      index = end;
    } else source += char.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${source}$`, 's');
}

/** The ids of `catalog` (routable model ids) that `setting` (false | true | glob list) publishes; null while the catalog is unknown. */
export function publishedModels(setting, catalog) {
  if (!Array.isArray(catalog)) return null;
  if (setting === true) return catalog;
  const globs = Array.isArray(setting) ? setting.filter((glob) => typeof glob === 'string' && glob) : [];
  const patterns = globs.map((glob) => { try { return globRegex(glob.toLowerCase()); } catch { return null; } }).filter(Boolean);
  return catalog.filter((id) => patterns.some((pattern) => pattern.test(id.toLowerCase())));
}
