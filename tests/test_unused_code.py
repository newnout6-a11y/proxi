"""Dead code hides mistakes: no unused imports or write-only locals in the modules these fixes touch."""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = ["adapters", "apierr", "direct", "generation", "journal", "proxy", "session", "sse", "structured"]
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def parsed(name):
    return ast.parse((ROOT / (name + ".py")).read_text(encoding="utf-8"))


def loaded_names(tree):
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}


def own_nodes(function):
    """The nodes of one function body, without the functions and classes nested in it."""
    todo = list(function.body)
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, SCOPES):
            todo.extend(ast.iter_child_nodes(node))


@pytest.mark.parametrize("name", MODULES)
def test_no_unused_imports(name):
    tree = parsed(name)
    loaded = loaded_names(tree)
    unused = [(alias.asname or alias.name).split(".")[0] for node in tree.body
              if isinstance(node, (ast.Import, ast.ImportFrom)) for alias in node.names
              if (alias.asname or alias.name).split(".")[0] not in loaded]
    assert not unused, "%s.py imports names it never uses: %s" % (name, ", ".join(unused))


@pytest.mark.parametrize("name", MODULES)
def test_no_write_only_locals(name):
    found = []
    for function in ast.walk(parsed(name)):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        read = loaded_names(function)  # a closure may read what the function assigns
        shared = {item for node in own_nodes(function) if isinstance(node, (ast.Nonlocal, ast.Global))
                  for item in node.names}
        found += ["%s:%d %s" % (function.name, node.lineno, target.id)
                  for node in own_nodes(function) if isinstance(node, ast.Assign)
                  for target in node.targets
                  if isinstance(target, ast.Name) and target.id not in read | shared]
    assert not found, "%s.py assigns locals it never reads: %s" % (name, "; ".join(found))
