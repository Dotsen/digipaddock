"""A tree that may be published or released has no symlinks and no submodules.

A symlink's target and a submodule's commit are blobs the scanners never read as files, yet both reach the published
tree. kd-release and kd-mirror refuse such a tree through this one rule. Stdlib only.
"""

ODD_MODES = ("120000", "160000")  # a symlink, a submodule (gitlink)


def odd_entries(ls_tree):
    """The paths of the symlinks and submodules in ``git ls-tree -r <tree>`` output (``<mode> <type> <id>\t<path>``)."""
    return [
        line.split("\t", 1)[-1]
        for line in (ls_tree or "").splitlines()
        if line.split(" ", 1)[0] in ODD_MODES
    ]


def refusal(odd, limit=5):
    """The reason a tree with ``odd`` entries is refused ('' for none)."""
    if not odd:
        return ""
    return "a symlink or a submodule in the tree: " + ", ".join(odd[:limit])
