"""The publication UI uses the server's fnmatch semantics, including unusual classes."""
import fnmatch
import itertools
import json
from pathlib import Path
import subprocess


def test_publication_masks_match_python_fnmatch():
    atoms = ['^', '!', ']', '[', '-', 'a', 'z', '\\', '&', '|', '~']
    masks = ['claude-[^s]*', '[]]', '[!]]', '[z-a]', '[!z-a]', '[a-z]', '[a--z]', '[', '[]', '[!]']
    masks += ['[' + ''.join(chars) + ']' for size in (1, 2, 3)
              for chars in itertools.product(atoms, repeat=size)]
    catalog = atoms + ['s', 'b', '0', '', 'aa', '\n', 'claude-sonnet-5-5', 'claude-opus-5-5']
    script = """
import {publishedModels} from './web/src/prompts/publish.js';
let raw = ''; for await (const chunk of process.stdin) raw += chunk;
const {masks, catalog} = JSON.parse(raw);
console.log(JSON.stringify(masks.map(mask => publishedModels([mask], catalog))));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script],
                            cwd=Path(__file__).resolve().parents[1],
                            input=json.dumps({'masks': masks, 'catalog': catalog}),
                            text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    for mask, found in zip(masks, actual, strict=True):
        assert found == [name for name in catalog if fnmatch.fnmatchcase(name.lower(), mask.lower())], mask
