"""no Svelte component shadows a rune with a top-level binding of its name.

Svelte 5 reads ``$state(...)`` as a store subscription when the component's script declares ``state`` at its top
level with anything but that rune (``function state() {}``): the component then fails as it is created — /servers did
not open on stage. svelte-check only warns (store_rune_conflict) and the dev sandbox cannot run it, so this checks the
sources themselves (the components' scripts are indented two spaces at the top level).
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "cabinet" / "web" / "src"
RUNES = ("state", "derived", "effect", "props", "bindable", "inspect", "host")
TOP = re.compile(r"^  (?:export\s+)?(?:function\s+(\w+)\s*\(|(?:const|let|var)\s+(\w+)\s*=\s*(.*))")


def _conflicts(text: str):
    out = []
    for line in text.splitlines():
        found = TOP.match(line)
        if not found:
            continue
        name = found.group(1) or found.group(2)
        if name not in RUNES or f"${name}(" not in text and f"${name}<" not in text:
            continue
        init = found.group(3) or ""
        if found.group(2) and re.match(rf"\${name}\b", init.strip()):
            continue  # `let state = $state(…)` is the rune's own binding: valid
        out.append(line.strip())
    return out


def test_no_component_shadows_a_rune_it_uses():
    bad = {}
    for path in sorted(WEB.rglob("*.svelte")):
        found = _conflicts(path.read_text(encoding="utf-8"))
        if found:
            bad[str(path.relative_to(WEB))] = found
    assert bad == {}


def test_the_check_finds_the_shadowing_that_broke_the_servers_page():
    broken = '<script lang="ts">\n  let rows = $state(null);\n  function state(row) {\n    return row;\n  }\n</script>\n'
    assert _conflicts(broken) == ["function state(row) {"]
    assert _conflicts('<script>\n  let state = $state<string | null>(null);\n</script>\n') == []
    assert _conflicts("<script>\n  const state = (c) => c;\n</script>\n") == []  # no $state here: no conflict
