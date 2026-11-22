"""e2e_runtime_setup.sh: root builds kd-e2e's runtime from what the base user built."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "common" / "install" / "e2e_runtime_setup.sh").read_text(encoding="utf-8")
RELEASE = (ROOT / "common" / "bin" / "kd-release").read_text(encoding="utf-8")


def test_root_never_shapes_a_path_inside_the_base_users_build():
    """Fable review I-2: entries under the build directory are the base user's, made by the base user."""
    for line in SCRIPT.splitlines():
        if line.strip().startswith("install -d"):
            assert "$BUILD/" not in line and "$src" not in line and "$cache" not in line, line


def test_the_playwright_version_is_read_by_the_base_user_and_checked():
    body = SCRIPT[SCRIPT.index("playwright_version() {"):]
    body = body[:body.index("\n}\n")]
    assert 'runuser -u "$base"' in body and "open(" not in body
    assert "^[0-9][0-9A-Za-z.-]{0,63}$" in SCRIPT


def test_links_from_the_build_are_refused_and_root_files_are_written_fresh():
    assert re.search(r'find "\$NEW/browsers" "\$NEW/app/\$WEB/dist" -type l', SCRIPT)
    assert 'rm -f "$NEW/browsers/.playwright"' in SCRIPT


def test_the_lock_wait_fits_inside_kd_release_timeout():
    """Fable review M-1: a busy lock ends as the script's own message, not kd-release's timeout."""
    wait = int(re.search(r"flock -w (\d+) 9", SCRIPT).group(1))
    t_e2e = int(re.search(r"^T_E2E = (\d+)", RELEASE, re.M).group(1))
    assert wait < t_e2e - 120


def test_no_repository_or_project_name_is_hard_wired():
    """Fable review M-3: the mirror and the copy come from the repos registry, as kd-release finds them."""
    assert "DigiPaddock.git" not in SCRIPT and "/srv/digipaddock-repos/digipaddock" not in SCRIPT
    assert "/etc/digipaddock/repos.json" in SCRIPT


def test_root_copies_the_build_through_the_base_users_tar_and_makes_the_runs_folder():
    """Review 04.11.2026: root never walks the base user's build by path (cp -R follows a folder swapped for a link
    mid-copy); the base user's tar reads it. kd-e2e's runs folder is made here, the base user's own, and checked."""
    body = SCRIPT[SCRIPT.index("from_build() {"):]
    body = body[:body.index("\n}\n")]
    assert 'runuser -u "$base" -- tar -C "$src" -cf -' in body and "--no-same-owner" in body
    assert not re.search(r'cp -R "\$(BUILD|cache)', SCRIPT) and 'cp -R "$cache"' not in SCRIPT
    assert 'install -d -o "$base" -g "$base" -m 0700 "$E2E/runs"' in SCRIPT
    assert "$base 700" in SCRIPT[SCRIPT.index("check() {"):]
