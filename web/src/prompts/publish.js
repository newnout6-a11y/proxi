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

/** The ids of `catalog` (routable model ids) that `setting` (false | true | glob list) publishes; null while the catalog is unknown. */
export function publishedModels(setting, catalog) {
  if (!Array.isArray(catalog)) return null;
  if (setting === true) return catalog;
  const globs = Array.isArray(setting) ? setting.filter((glob) => typeof glob === 'string' && glob) : [];
  const patterns = globs.map((glob) => { try { return globRegex(glob.toLowerCase()); } catch { return null; } }).filter(Boolean);
  return catalog.filter((id) => patterns.some((pattern) => pattern.test(id.toLowerCase())));
}
