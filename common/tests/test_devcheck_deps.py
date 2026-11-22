"""dev.check's prepared dependencies by the hash of their locks — built once by the ci user (the
files fetched with network, the venv installed without, review 04.11.2026), sealed root's, reused while the locks stay; the checkout gets a copy of node_modules.
Fakes for systemd-run, the owners and the lock: nothing real starts."""

import os
import stat
import threading
import time

import pytest

from kd import devcheck_runner as dr
from kd.session_host import devcheck as host_devcheck

ID, ID2 = "a" * 32, "b" * 32
LOCKS = {"requirements-dev.txt": "pytest>=8\n", "common/cabinet/requirements-build.txt": "setuptools==84.0.0\n",
         "common/cabinet/requirements.txt": "fastapi==1.0\n",
         "common/cabinet/web/package-lock.json": '{"lockfileVersion": 3, "packages": {"": {"name": "web"}}}\n',
         "common/cabinet/web/package.json": '{"name": "web"}\n'}


def checkout(root, name="src", **changes):
    path = root / name
    for rel, text in {**LOCKS, **changes}.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8")
    return str(path)


class Units:
    """systemd-run: the venv gets bin/python, npm ci gets node_modules with a link in .bin."""

    def __init__(self, fail=None, delay=0.0):
        self.calls, self.fail, self.delay = [], fail, delay

    def __call__(self, argv):
        self.calls.append(list(argv))
        time.sleep(self.delay)
        command = argv[argv.index("--") + 1:]
        where = [a.split("=", 1)[1] for a in argv if a.startswith("WorkingDirectory=")][0]
        if self.fail and self.fail in " ".join(command):
            return 1, "ERROR: No matching distribution found for fastapi==1.0 (token=abc)"
        if command[1:3] == ["-m", "venv"]:
            os.makedirs(os.path.join(command[-1], "bin"), exist_ok=True)
            open(os.path.join(command[-1], "bin", "python"), "w").close()
        if command[:2] == ["npm", "ci"]:
            os.makedirs(os.path.join(where, "node_modules", "svelte"), exist_ok=True)
            open(os.path.join(where, "node_modules", "svelte", "index.js"), "w").close()
        return 0, "ok"


class Env:
    def __init__(self, tmp_path, units=None):
        self.cache = str(tmp_path / "cache")
        trusted = checkout(tmp_path, "deployed")  # the deployed tree's locks: the build backend, the source package
        self.owners, self.sealed, self.owned = {}, [], []
        self.units = units or Units()
        self.lock = threading.Lock()
        self.deps = dr.Deps(cache=self.cache, units=self.units, own=self.own, seal=self.seal, owner=self.owner,
                            locker=self.locker, ci_user="digi-ci", copy=self.copy, trusted=trusted)
        self.copies = []

    def own(self, path, *, user, recursive=False):
        self.owned.append((path, user))
        self.owners[os.path.normpath(path)] = user

    def seal(self, path):
        self.sealed.append(path)
        self.owners[os.path.normpath(path)] = "root"

    def owner(self, path):
        return self.owners.get(os.path.normpath(path), "root")

    def locker(self, path):
        assert path == os.path.join(self.cache, ".lock")
        return self.lock

    def copy(self, src, dst):
        self.copies.append((src, dst))
        import shutil

        shutil.copytree(src, dst, symlinks=True)

    def builds(self, kind):
        return [c for c in self.units.calls if (kind == "venv") == ("npm" not in c)]


def test_lock_hashes(tmp_path):
    src = checkout(tmp_path)
    hashes = dr.lock_hashes(src)
    assert set(hashes) == {"venv", "node"} and all(len(h) == 12 for h in hashes.values())
    assert dr.lock_hashes(checkout(tmp_path, "other")) == hashes
    assert dr.lock_hashes(checkout(tmp_path, "web", **{"common/cabinet/web/package-lock.json": "{}"}))["venv"] == \
        hashes["venv"]


def test_same_lock_reuses_the_cache(tmp_path):
    env = Env(tmp_path)
    first = env.deps(ID, "digipaddock", checkout(tmp_path), {})
    calls = len(env.units.calls)
    assert calls == 10  # the downloader venv, its backend, three fetches; the venv, three installs; npm ci
    second = env.deps(ID2, "digipaddock", checkout(tmp_path, "src2"), {})
    assert second == first and len(env.units.calls) == calls
    h = dr.lock_hashes(checkout(tmp_path))
    assert first == {"venv": os.path.join(env.cache, f"venv-{h['venv']}"),
                     "node": os.path.join(env.cache, f"node-{h['node']}")}


def test_changed_lock_builds_a_new_cache(tmp_path):
    env = Env(tmp_path)
    first = env.deps(ID, "digipaddock", checkout(tmp_path), {})
    second = env.deps(ID2, "digipaddock", checkout(tmp_path, "src2", **{"requirements-dev.txt": "pytest>=9\n"}), {})
    assert second["venv"] != first["venv"] and second["node"] == first["node"]
    assert len(env.builds("venv")) == 18 and len(env.builds("node")) == 1
    assert os.path.isdir(first["venv"])  # the other lock's cache stays


def _network(argv):
    return not any(a == "-p" and argv[i + 1] == "PrivateNetwork=yes" for i, a in enumerate(argv[:-1]))


def test_the_venv_is_fetched_with_network_and_installed_without(tmp_path):
    # review 04.11.2026: nothing of the candidate's runs while the network is open — a downloader venv only fetches
    # (no candidate package is installed in it), the venv is installed from the fetched files in a unit with no network
    env = Env(tmp_path)
    env.deps(ID, "digipaddock", checkout(tmp_path), {})
    builds = env.builds("venv")
    steps = [c[c.index("--") + 1:] for c in builds]
    dl_venv, backend, f_dev, f_build, f_cab, venv, i_dev, i_build, i_cab = steps
    new = venv[-1]
    assert os.path.basename(new).startswith(".new-venv-") and new.endswith(ID[:12])
    locks, dl, wheels = (os.path.join(new, n) for n in (".locks", ".dl", ".wheels"))
    quiet = ["--quiet", "--disable-pip-version-check", "--no-input"]
    assert dl_venv == ["/usr/bin/python3", "-m", "venv", dl]
    dl_py, py = os.path.join(dl, "bin", "python"), os.path.join(new, "bin", "python")
    assert backend == [dl_py, "-m", "pip", "install", *quiet, "--isolated", "--index-url", dr.INDEX, "--require-hashes",
                       "--only-binary=:all:", "-r", os.path.join(locks, "requirements-build.txt")]
    fetch = [dl_py, "-m", "pip", "download", *quiet, "--isolated", "--index-url", dr.INDEX, "--dest", wheels]
    assert f_dev == fetch + ["--only-binary=:all:", "-r", os.path.join(locks, "requirements-dev.txt")]
    assert f_build == fetch + ["--require-hashes", "--only-binary=:all:", "-r", os.path.join(locks, "requirements-build.txt")]
    assert f_cab == fetch + ["--require-hashes", "--only-binary=:all:", "--no-binary=http-ece", "--no-build-isolation",
                             "-r", os.path.join(locks, "requirements.txt")]
    assert venv == ["/usr/bin/python3", "-m", "venv", "--system-site-packages", new]
    install = [py, "-m", "pip", "install", *quiet, "--isolated", "--no-index", "--find-links", wheels]
    assert i_dev == install + ["--upgrade", "--only-binary=:all:", "-r", os.path.join(locks, "requirements-dev.txt")]
    assert i_build == install + ["--require-hashes", "--only-binary=:all:", "-r",
                                 os.path.join(locks, "requirements-build.txt")]
    assert i_cab == install + ["--require-hashes", "--only-binary=:all:", "--no-binary=http-ece", "--no-build-isolation",
                               "-r", os.path.join(locks, "requirements.txt")]
    assert [_network(c) for c in builds] == [True] * 5 + [False] * 4
    (npm,) = env.builds("node")
    assert npm[npm.index("--") + 1:] == ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"] and _network(npm)


@pytest.mark.parametrize("rel, text, why", [
    ("requirements-dev.txt", "--index-url https://evil.example/simple\npytest>=8\n", "only --hash"),
    ("requirements-dev.txt", "-r other.txt\n", "only --hash"),
    ("requirements-dev.txt", "pytest @ https://evil.example/pytest.whl\n", "not a plain requirement"),
    ("requirements-dev.txt", "https://evil.example/pkg.tar.gz\n", "not a plain requirement"),
    ("requirements-dev.txt", "./local\n", "not a plain requirement"),
    ("common/cabinet/requirements.txt", "fastapi==1.0 --trusted-host evil.example\n", "only --hash"),
    ("common/cabinet/requirements-build.txt", "setuptools==1.0\n", "differs from the deployed one"),
    ("common/cabinet/requirements.txt", "fastapi==1.0\nhttp-ece==9.9\n", "http-ece differs"),
    ("common/cabinet/web/package-lock.json",
     '{"lockfileVersion": 3, "packages": {"node_modules/x": {"resolved": "git+https://evil.example/x.git"}}}',
     "not https://registry.npmjs.org/"),
    ("common/cabinet/web/package-lock.json", '{"v": 1}', "not an npm lock"),
])
def test_a_lock_that_would_choose_what_the_network_fetches_builds_nothing(tmp_path, rel, text, why):
    env = Env(tmp_path)
    with pytest.raises(dr.DepsFailed, match=why):
        env.deps(ID, "digipaddock", checkout(tmp_path, **{rel: text}), {})
    assert env.units.calls == []


def test_plain_hashed_requirements_with_markers_and_comments_pass(tmp_path):
    lock = ("# a header\nfastapi==1.0 \\\n    --hash=sha256:" + "a" * 64 + " \\\n    --hash=sha256:" + "b" * 64 +
            "\n    # via\n    #   x\ntzdata==2026.4; sys_platform == \"win32\"\n")
    env = Env(tmp_path)
    env.deps(ID, "digipaddock", checkout(tmp_path, **{"common/cabinet/requirements.txt": lock}), {})
    assert len(env.builds("venv")) == 9


def test_a_half_built_cache_is_never_used(tmp_path):
    env = Env(tmp_path)
    h = dr.lock_hashes(checkout(tmp_path))
    leftover = os.path.join(env.cache, f".new-venv-{h['venv']}-{'c' * 12}")
    os.makedirs(os.path.join(leftover, "bin"))
    planted = os.path.join(env.cache, f"node-{h['node']}")
    os.makedirs(planted)
    env.owners[os.path.normpath(planted)] = "digi-ci"  # not the root's: not a finished build
    out = env.deps(ID, "digipaddock", checkout(tmp_path), {})
    assert not os.path.exists(leftover) and len(env.builds("venv")) == 9 and len(env.builds("node")) == 1
    assert os.path.basename(env.sealed[-1]).startswith(".new-node-") and os.path.isdir(
        os.path.join(out["node"], "node_modules"))
    assert not [n for n in os.listdir(env.cache) if n.startswith(".new-")]


def test_concurrent_builds_take_the_lock(tmp_path):
    env = Env(tmp_path, units=Units(delay=0.05))
    results = []
    sources = [checkout(tmp_path, "one"), checkout(tmp_path, "two")]
    threads = [threading.Thread(target=lambda s=s, i=i: results.append(env.deps(i, "digipaddock", s, {})))
               for s, i in zip(sources, (ID, ID2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and results[0] == results[1]
    assert len(env.units.calls) == 10  # one build, the second caller waited and reused it


def test_failed_build_is_dependency_update_required(tmp_path):
    env = Env(tmp_path, units=Units(fail="requirements.txt"))
    with pytest.raises(dr.DepsFailed) as failed:
        env.deps(ID, "digipaddock", checkout(tmp_path), {})
    assert "No matching distribution" in str(failed.value) and "venv" in str(failed.value)
    assert not [n for n in os.listdir(env.cache) if n.startswith((".new-", "venv-"))]


def test_the_deps_unit_has_network_and_writes_only_its_directory(tmp_path):
    env = Env(tmp_path)
    env.deps(ID, "digipaddock", checkout(tmp_path), {})
    argv = env.units.calls[0]
    new = [a.split("=", 1)[1] for a in argv if a.startswith("WorkingDirectory=")][0]
    assert argv[:7] == ["systemd-run", "--unit", host_devcheck.deps_unit(ID), "--wait", "--pipe", "--collect",
                        "--quiet"]
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert props == ["User=digi-ci", "Group=digi-ci", "PrivateTmp=yes", "ProtectSystem=strict", "ProtectHome=yes",
                     f"ReadWritePaths={new}", "NoNewPrivileges=yes", "CPUQuota=200%", "MemoryMax=3G",
                     f"RuntimeMaxSec={dr.DEPS_MAX_S}", "InaccessiblePaths=" + " ".join("-" + p for p in dr.INACCESSIBLE),
                     f"WorkingDirectory={new}"]
    assert not any(p.startswith(("PrivateNetwork", "IPAddressDeny")) for p in props)
    assert [a for a in argv if a.startswith("--setenv=")] == [f"--setenv=HOME={os.path.join(new, '.home')}"]
    assert (new, "digi-ci") in env.owned and len(env.sealed) == 2


def test_node_modules_is_copied_not_linked(tmp_path):
    env = Env(tmp_path)
    src = checkout(tmp_path)
    out = env.deps(ID, "digipaddock", src, {})
    target = os.path.join(src, "common", "cabinet", "web", "node_modules")
    assert env.copies == [(os.path.join(out["node"], "node_modules"), target)]
    assert os.path.isdir(target) and not os.path.islink(target)
    assert os.path.isfile(os.path.join(target, "svelte", "index.js"))
    assert (target, "digi-ci") in env.owned


def test_the_overlay_uses_the_code_checkouts_venv(tmp_path):
    env = Env(tmp_path)
    code = checkout(tmp_path, "code")
    overlay = str(tmp_path / "overlay")
    os.makedirs(overlay)
    out = env.deps(ID, "digipaddock-deploy", overlay, {"digipaddock": code})
    assert set(out) == {"venv"} and out["venv"].endswith(dr.lock_hashes(code)["venv"])
    assert env.builds("node") == []


def test_a_lock_outside_the_checkout_is_refused(tmp_path):
    src = checkout(tmp_path)
    outside = tmp_path / "secret"
    outside.write_text("root only\n", encoding="utf-8")
    os.remove(os.path.join(src, "requirements-dev.txt"))
    try:
        os.symlink(str(outside), os.path.join(src, "requirements-dev.txt"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    with pytest.raises(dr.DepsFailed):
        dr.lock_hashes(src)


def test_a_missing_lock_is_dependency_update_required(tmp_path):
    src = checkout(tmp_path)
    os.remove(os.path.join(src, "common", "cabinet", "requirements.txt"))
    with pytest.raises(dr.DepsFailed):
        dr.lock_hashes(src)


def test_old_caches_of_a_kind_are_pruned(tmp_path):
    env = Env(tmp_path)
    old = os.path.join(env.cache, "venv-000000000000")
    os.makedirs(old)
    week = time.time() - dr.PRUNE_S - 60
    os.utime(old, (week, week))
    fresh = os.path.join(env.cache, "venv-111111111111")
    os.makedirs(fresh)
    env.deps(ID, "digipaddock", checkout(tmp_path), {})
    assert not os.path.exists(old) and os.path.isdir(fresh)


# --- final review 28.10: the sealed cache is open to the ci user; the build fits the runner's own limit -------------
def test_the_sealed_modes_read_and_traverse_for_all_write_for_none():
    assert dr._sealed_mode(stat.S_IFDIR | 0o700) == 0o755
    assert dr._sealed_mode(stat.S_IFDIR | 0o777) == 0o755
    assert dr._sealed_mode(stat.S_IFREG | 0o600) == 0o644
    assert dr._sealed_mode(stat.S_IFREG | 0o775) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_cache_is_open_to_the_ci_user_under_roots_umask(tmp_path):
    env = Env(tmp_path)
    old = os.umask(0o077)  # kd-operation-run's
    try:
        out = env.deps(ID, "digipaddock", checkout(tmp_path), {})
    finally:
        os.umask(old)
    for path in (env.cache, out["venv"], out["node"]):
        assert stat.S_IMODE(os.stat(path).st_mode) & 0o055 == 0o055, path


class Timer:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


def test_each_build_command_gets_what_the_budget_leaves(tmp_path):
    env = Env(tmp_path)
    env.deps.timer = Timer()
    env.deps(ID, "digipaddock", checkout(tmp_path), {}, budget_s=300)
    limits = {a for c in env.units.calls for a in c if a.startswith("RuntimeMaxSec=")}
    assert limits == {"RuntimeMaxSec=300"}
    env2 = Env(tmp_path / "two")
    env2.deps(ID, "digipaddock", checkout(tmp_path, "src2"), {})
    assert {a for c in env2.units.calls for a in c if a.startswith("RuntimeMaxSec=")} == {
        f"RuntimeMaxSec={dr.DEPS_MAX_S}"}


def test_a_budget_too_small_is_slow_not_a_lock_problem(tmp_path):
    env = Env(tmp_path)
    env.deps.timer = Timer()
    with pytest.raises(dr.DepsSlow):
        env.deps(ID, "digipaddock", checkout(tmp_path), {}, budget_s=dr.MIN_DEPS_S - 1)
    assert env.units.calls == [] and issubclass(dr.DepsSlow, dr.DepsFailed)


def test_a_build_cut_at_its_limit_is_slow(tmp_path):
    timer = Timer()

    def units(argv):
        timer.now += 300
        return 1, "Terminated"

    env = Env(tmp_path, units=units)
    env.deps.timer = timer
    with pytest.raises(dr.DepsSlow):
        env.deps(ID, "digipaddock", checkout(tmp_path), {}, budget_s=300)
    assert not [n for n in os.listdir(env.cache) if n.startswith(".new-")]
