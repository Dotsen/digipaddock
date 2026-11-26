"""C05, kd.worker_runner — one job of the session host: verify, prepare, launch, its end line."""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from kd import admin_task
from kd import worker_jobs as wj
from kd import worker_runner as wr
from kd import worker_slots as ws
from kd import worker_result as wrs
from kd.session_host import worker as host_worker
from kd.specializations import SPECS_ROOT, load_registry
from kd.worker_policy import Slot

REPO = Path(__file__).resolve().parents[2]
REG = load_registry(SPECS_ROOT, require_root_owner=False)
NOW = 1_800_000_000.0
FLOOR = REPO / "common/claude/managed-settings.template.json"
DEV_POLICY = REPO / "common/tests/fixtures/policy/dev.policy.json"
CONFIG = REPO / "common/worker/worker-slots.json"
AGENTS = "/srv/digipaddock-agents"
REGISTRY = {
    "boss": {"user": "digi-boss", "runtime": "generated", "telegram": True},
    "dev": {"user": "digi-dev", "runtime": "generated", "telegram": True},
}
# On Linux the tests run as an unprivileged user (stage): the agent's ids are its own, so the
# runner's fchown of what it hands the agent is allowed; on Windows (no fchown) any number.
PW = SimpleNamespace(
    pw_name="digi-dev",
    pw_uid=getattr(os, "getuid", lambda: 1234)(),
    pw_gid=getattr(os, "getgid", lambda: 1234)(),
    pw_dir="/home/digi-dev",
)
LINUX = pytest.mark.skipif(os.name == "nt", reason="symlinks and dir_fd: Linux")
OWN = lambda path: ws.Entry("dir", PW.pw_uid, 0o700, 0)  # noqa: E731 — work/ as the installer leaves it


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _card(task_id, status="To Do"):
    return admin_task.from_record(
        {
            "id": task_id,
            "title": f"Card {task_id}",
            "status": status,
            "project": "digipaddock",
            "assignee": "@dev",
            "labels": [],
            "servers": [],
            "revision": 1,
            "description": "text",
        }
    )


@pytest.fixture
def world(tmp_path):
    """A source copy with two commits, a job as the session host hands it to slot dev/worker1, a slot root."""
    source = tmp_path / "copies" / "digipaddock"
    source.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=source)
    git("config", "user.email", "t@t", cwd=source)
    git("config", "user.name", "t", cwd=source)
    (source / "README.md").write_text("one\n")
    git("add", "README.md", cwd=source)
    git("commit", "-qm", "one", cwd=source)
    (source / "README.md").write_text("two\n")
    git("commit", "-qam", "two", cwd=source)
    slots_root = tmp_path / "workers"
    config = ws.load_config(CONFIG)
    card = wj.Request("KD-121", "implementer", (), "Fix the parser.")
    admission = wj.admit(
        card,
        _card("KD-121"),
        repos=frozenset({"digipaddock"}),
        registry=REG,
        config=config,
        types={"dev": "developer"},
    )
    definition = wj.definition(
        card, admission, job_id=wj.new_job_id(), caller="digi-boss", requester="boss", config=config
    )
    job = wr.check_definition(definition, "dev/worker1")
    return dict(
        source=source,
        root=str(slots_root),
        definition=definition,
        job=job,
        config=config,
        tmp=tmp_path,
    )


def test_a_definition_is_the_job_with_its_slot(world):
    assert world["job"] == {**world["definition"], "slot": "dev/worker1"}


def test_a_definition_for_another_slot_is_refused(world):
    with pytest.raises(wr.RunnerError, match="slot analyst/worker1 is not a slot of dev"):
        wr.check_definition(world["definition"], "analyst/worker1")
    with pytest.raises(wr.RunnerError, match="slot"):
        wr.check_definition(world["definition"], "dev/worker0")


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.pop("brief"),
        lambda d: d.update(extra=1),
        lambda d: d.update(id="x" * 32),
        lambda d: d.update(timeout=59),
        lambda d: d.update(timeout=True),
        lambda d: d.update(agent=["dev"]),
    ],
)
def test_a_definition_of_another_shape_is_refused(world, change):
    bad = json.loads(json.dumps(world["definition"]))
    change(bad)
    with pytest.raises(wr.RunnerError):
        wr.check_definition(bad, "dev/worker1")
    with pytest.raises(wr.RunnerError):
        wr.check_definition(["not", "a", "job"], "dev/worker1")


def test_a_changed_registry_refuses_the_run(world):
    job = dict(world["job"])
    job["prompt"] = {**job["prompt"], "sha256": "0" * 64}
    with pytest.raises(wr.RunnerError, match="registry changed"):
        wr.verify_prompt(job, REG)
    assert wr.verify_prompt(world["job"], REG).spec == "implementer"


def test_prepare_repo_clones_the_source_on_its_head_without_a_remote(world):
    slot = Slot.parse("dev", "worker1")
    chowned = []
    base, branch = wr.prepare_repo(
        slot,
        str(world["source"]),
        job_id=world["job"]["id"],
        run=subprocess.run,
        chown=lambda path, uid, gid: chowned.append(path),
        root=world["root"],
    )
    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    assert (
        base == git("rev-parse", "HEAD", cwd=world["source"])
        and branch == f"worker/{world['job']['id']}"
    )
    assert (
        git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == branch
        and git("remote", cwd=repo) == ""
    )
    # the agent gets the clone in the root-owned staging folder, moved into work/ afterwards
    handed = repo.parent.parent / f"repo.{world['job']['id']}"
    assert (
        (repo / "README.md").read_text() == "two\n"
        and chowned
        and Path(chowned[-1]) == handed
        and not handed.exists()
    )
    # a second run on the same slot starts from a clean copy again
    (repo / "junk").write_text("x")
    wr.prepare_repo(
        slot,
        str(world["source"]),
        job_id=world["job"]["id"],
        run=subprocess.run,
        chown=lambda path, uid, gid: None,
        root=world["root"],
    )
    assert not (repo / "junk").exists() and (repo / "README.md").read_text() == "two\n"


def test_the_clone_trusts_the_source_through_a_global_config_file(world):
    # The source copy belongs to the repo user and root clones it: git's local transport strips
    # GIT_CONFIG_PARAMETERS from upload-pack, so `-c safe.directory` never reaches it (stage, 15.10);
    # a GIT_CONFIG_GLOBAL file naming the source and its .git does.
    # The clone is staged in the root-owned slot root and then handed to the agent: the file names the
    # staging folder, never the agent's work/repo — after the job git runs there as the agent (review 04.11.2026).
    seen = []

    def run(argv, **kwargs):
        if argv[:1] == ["git"]:
            seen.append(kwargs.get("env") or {})
        return subprocess.run(argv, **kwargs)

    slot = Slot.parse("dev", "worker1")
    source = str(world["source"])
    wr.prepare_repo(
        slot,
        source,
        job_id=world["job"]["id"],
        run=run,
        chown=lambda *a: None,
        root=world["root"],
    )
    assert len(seen) >= 5 and len({env.get("GIT_CONFIG_GLOBAL") for env in seen}) == 1
    env = seen[0]
    config = Path(env["GIT_CONFIG_GLOBAL"])
    assert config == Path(world["root"]) / "dev" / "worker1" / "gitconfig"
    text = config.read_text("utf-8")
    quoted = source.replace(
        "\\", "\\\\"
    )  # a quoted value: a Windows path keeps its backslashes
    assert (
        f'directory = "{quoted}"\n' in text and f'directory = "{quoted}/.git"\n' in text
    )
    assert "/work/repo" not in text
    assert (
        env["PATH"] == os.environ["PATH"]
    )  # the host's environment, one variable added


def test_a_failing_git_is_the_jobs_reason(world):
    slot = Slot.parse("dev", "worker1")
    with pytest.raises(wr.RunnerError, match="git:"):
        wr.prepare_repo(
            slot,
            str(world["tmp"] / "copies" / "missing"),
            job_id=world["job"]["id"],
            run=subprocess.run,
            chown=lambda *a: None,
            root=world["root"],
        )


def test_prepare_refuses_a_missing_or_foreign_source(world, tmp_path):
    with pytest.raises(wr.RunnerError, match="not a git work tree"):
        wr.source_copy("nope", copies_root=str(tmp_path / "copies"))
    (tmp_path / "copies" / "plain").mkdir()
    with pytest.raises(wr.RunnerError, match="not a git work tree"):
        wr.source_copy("plain", copies_root=str(tmp_path / "copies"))
    with pytest.raises(wr.RunnerError, match="project"):
        wr.source_copy("../etc", copies_root=str(tmp_path / "copies"))
    assert (
        Path(wr.source_copy("digipaddock", copies_root=str(tmp_path / "copies")))
        == tmp_path / "copies" / "digipaddock"
    )


def test_clear_empties_the_slot_folders_but_keeps_them(world):
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    (work / "repo" / "old").mkdir(parents=True)
    (work / "repo" / "old" / "f").write_text("x")
    (work / "tmp").mkdir()
    (work / "tmp" / "t").write_text("x")
    wr.clear(str(work / "repo"))
    wr.clear(str(work / "tmp"))
    assert (
        (work / "repo").is_dir()
        and not any((work / "repo").iterdir())
        and not any((work / "tmp").iterdir())
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks: Linux")
def test_clear_unlinks_a_link_instead_of_following_it(world):
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    outside = world["tmp"] / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    (work / "repo").mkdir(parents=True)
    os.symlink(outside, work / "repo" / "link")
    wr.clear(str(work / "repo"))
    assert (outside / "keep").exists() and not any((work / "repo").iterdir())


def test_write_slot_files_puts_the_merged_settings_and_the_system_text_in_the_slot_root(
    world,
):
    slot = Slot.parse("dev", "worker1")
    policy = {
        "settings": {"permissions": {"allow": []}, "hooks": {}},
        "launch_settings": {"sandbox": {"enabled": True}},
    }
    prompt = wr.verify_prompt(world["job"], REG)
    (Path(world["root"]) / "dev" / "worker1").mkdir(parents=True)
    settings, system = wr.write_slot_files(
        slot,
        policy,
        prompt,
        root=world["root"],
        write=lambda p, data, mode: Path(p).write_bytes(data),
    )
    assert Path(settings) == Path(world["root"]) / "dev" / "worker1" / "settings.json"
    assert json.loads(Path(settings).read_bytes()) == {
        "permissions": {"allow": []},
        "hooks": {},
        "sandbox": {"enabled": True},
    }
    assert Path(system).read_text("utf-8").startswith("# Worker session: implementer")


def _prepare(world, **override):
    agents_dir = world["tmp"] / "agents"
    (agents_dir / "dev" / ".claude").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEV_POLICY, agents_dir / "dev" / ".claude" / "policy.json")
    registry = world["tmp"] / "agents.json"
    registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
    kwargs = dict(
        config_path=CONFIG,
        registry_path=str(registry),
        packs_root=SPECS_ROOT,
        agents_root=AGENTS,
        floor_path=str(FLOOR),
        policy_root=str(agents_dir),
        copies_root=str(world["source"].parent),
        slots_root=world["root"],
        require_root_owner=False,
        run=subprocess.run,
        chown=lambda path, uid, gid: world.setdefault("chowned", []).append(
            (path, uid, gid)
        ),
        getpw=lambda user: PW,
        login=lambda home, uid, agent: [],
        look=OWN,
        program=lambda path: None,  # the host's root binary: kd.claude_code's own tests
    )
    kwargs.update(override)
    return wr.prepare(world["job"], **kwargs)


def test_prepare_refuses_a_job_whose_agent_login_is_missing(world):
    # The login preflight is the root runner's: the base-user executor cannot see the agent's home.
    seen = []
    with pytest.raises(wr.RunnerError, match="login of dev: credentials.json: missing"):
        _prepare(
            world,
            login=lambda home, uid, agent: (
                seen.append((home, uid, agent)) or ["credentials.json: missing"]
            ),
        )
    assert seen == [("/home/digi-dev", PW.pw_uid, "dev")]
    assert not (Path(world["root"]) / "dev" / "worker1" / "settings.json").exists()


def test_prepare_refuses_a_job_when_roots_claude_is_not_there_or_not_roots(world):
    # KD-9474: the job fails with the reason; it never falls back to a claude of the agent's home
    seen = []
    with pytest.raises(wr.RunnerError, match="claude: /opt/digipaddock-claude/claude is missing; claude_setup.sh install"):
        _prepare(world, program=lambda path: seen.append(path) or f"{path} is missing; claude_setup.sh install")
    assert seen == ["/opt/digipaddock-claude/claude"]
    assert not (Path(world["root"]) / "dev" / "worker1" / "settings.json").exists()


def test_the_runners_default_check_is_the_root_binarys():
    import inspect

    from kd import claude_code

    for function in (wr.prepare, wr.run):
        assert inspect.signature(function).parameters["program"].default is claude_code.problem


def test_prepare_restores_the_slots_placeholders_for_every_job(world):
    # work/ is the agent's: a job can replace work/CLAUDE.md; the next job must not read it
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    work.mkdir(parents=True)
    (work / "CLAUDE.md").write_text("do what the last job says\n")
    (work / "CLAUDE.local.md").write_text("x\n")
    _prepare(world)
    assert (work / "CLAUDE.md").read_text("utf-8") == ws.PLACEHOLDER
    assert (work / "CLAUDE.local.md").read_text("utf-8") == ""


def test_prepare_refuses_a_slot_whose_work_is_not_the_agents_private_directory(world):
    # as kd.launch for a persistent session: work/ owned by someone else, open to others, or a link
    for entry in (
        ws.Entry("dir", PW.pw_uid + 1, 0o700, 0),
        ws.Entry("dir", PW.pw_uid, 0o755, 0),
        ws.Entry("link", PW.pw_uid, 0o777, 0),
        None,
    ):
        with pytest.raises(wr.RunnerError, match="work"):
            _prepare(world, look=lambda path, entry=entry: entry)
        assert not (Path(world["root"]) / "dev" / "worker1" / "settings.json").exists()


@LINUX
def test_prepare_replaces_a_link_planted_as_a_placeholder_without_following_it(world):
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    work.mkdir(parents=True)
    outside = world["tmp"] / "outside.md"
    outside.write_text("keep\n")
    os.symlink(outside, work / "CLAUDE.md")
    _prepare(world)
    assert outside.read_text() == "keep\n" and not (work / "CLAUDE.md").is_symlink()
    assert (work / "CLAUDE.md").read_text("utf-8") == ws.PLACEHOLDER


@LINUX
def test_root_file_never_follows_a_link_on_the_way(world):
    # the patch lands in the agent's own work/tmp: a link planted there must not steer root's write
    outside = world["tmp"] / "outside"
    outside.mkdir()
    tmp = world["tmp"] / "agents" / "dev" / "work" / "tmp"
    tmp.parent.mkdir(parents=True)
    os.symlink(outside, tmp)
    with pytest.raises(wr.RunnerError, match="not a plain directory"):
        wr.root_file(f"{tmp}/worker-x.patch", b"x", 0o644, PW.pw_uid, PW.pw_gid)
    assert not any(outside.iterdir())
    os.unlink(tmp)
    tmp.mkdir()
    os.symlink(outside / "target", tmp / "worker-x.patch")
    wr.root_file(f"{tmp}/worker-x.patch", b"x", 0o644, PW.pw_uid, PW.pw_gid)
    assert (
        not (outside / "target").exists() and not (tmp / "worker-x.patch").is_symlink()
    )
    assert (tmp / "worker-x.patch").read_bytes() == b"x"


def test_the_cards_folder_is_never_made_through_a_link(world):
    # work/tmp/cards/<card> is made by root for the agent — a link in its place is refused, not followed
    outside = world["tmp"] / "outside"
    outside.mkdir()
    tmp = world["tmp"] / "agents" / "dev" / "work" / "tmp"
    tmp.mkdir(parents=True)
    os.symlink(outside, tmp / "cards")
    with pytest.raises(wr.RunnerError):
        wr.agent_dir(f"{tmp}/cards/KD-1", PW.pw_uid, PW.pw_gid)
    assert not any(outside.iterdir())


def test_prepare_builds_the_slot_for_a_developer_job(world):
    prepared = _prepare(world)
    jid = world["job"]["id"]
    assert (prepared.user, prepared.uid, prepared.gid, prepared.home) == (
        "digi-dev",
        PW.pw_uid,
        PW.pw_gid,
        "/home/digi-dev",
    )
    assert (
        prepared.model == "sonnet"
        and prepared.branch == f"worker/{jid}"
        and prepared.slot.ref == "dev/worker1"
    )
    assert prepared.base == git("rev-parse", "HEAD", cwd=world["source"])
    settings = json.loads(Path(prepared.settings_path).read_bytes())
    assert (
        settings["sandbox"]["network"]["allowedDomains"] == []
        and "Agent" in settings["permissions"]["deny"]
    )
    assert (
        Path(prepared.system_path)
        .read_text("utf-8")
        .startswith("# Worker session: implementer")
    )
    assert prepared.task.startswith("Card: KD-121")
    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    assert (repo / "README.md").read_text() == "two\n"
    handed = repo.parent.parent / f"repo.{jid}"  # the root-owned staging folder, moved into work/ afterwards
    assert (handed, PW.pw_uid, PW.pw_gid) in [
        (Path(p), u, g) for p, u, g in world["chowned"]
    ]
    assert (Path(world["root"]) / "dev" / "worker1" / "work" / "tmp").is_dir()


@LINUX
def test_prepare_never_follows_a_link_the_agent_planted_as_repo_or_tmp(world):
    """Global review 15.10.2026: work/ is the agent's; a link there must steer neither the clone nor the chown."""
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    work.mkdir(parents=True)
    outside = world["tmp"] / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    os.symlink(outside, work / "repo")
    os.symlink(outside, work / "tmp")
    chowned = []
    prepared = _prepare(world, chown=lambda path, uid, gid: chowned.append(path))
    assert (outside / "keep").read_text() == "x" and list(outside.iterdir()) == [outside / "keep"]
    assert not (work / "repo").is_symlink() and (work / "repo" / "README.md").read_text() == "two\n"
    assert not (work / "tmp").is_symlink() and (work / "tmp").is_dir() and not any((work / "tmp").iterdir())
    assert prepared.branch.startswith("worker/") and all(not Path(p).is_symlink() for p in chowned)
    assert str(outside) not in chowned
    # the real chown refuses a link where a folder should be, instead of walking into its target
    link = world["tmp"] / "link"
    os.symlink(outside, link)
    with pytest.raises(wr.RunnerError, match="hand over"):
        wr.chown_tree(str(link), os.getuid(), os.getgid())


def test_prepare_refuses_a_job_whose_registry_text_changed(world):
    world["job"]["prompt"]["sha256"] = "0" * 64
    with pytest.raises(wr.RunnerError, match="registry changed"):
        _prepare(world)
    assert not (Path(world["root"]) / "dev" / "worker1" / "settings.json").exists()


def test_prepare_refuses_a_policy_of_another_agent(world):
    agents_dir = world["tmp"] / "agents"
    (agents_dir / "dev" / ".claude").mkdir(parents=True)
    policy = json.loads(DEV_POLICY.read_text("utf-8"))
    policy["agent"], policy["user"] = "analyst", "digi-analyst"
    (agents_dir / "dev" / ".claude" / "policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    with pytest.raises(wr.RunnerError, match="policy"):
        wr.prepare(
            world["job"],
            config_path=CONFIG,
            registry_path=_registry(world),
            packs_root=SPECS_ROOT,
            agents_root=AGENTS,
            floor_path=str(FLOOR),
            policy_root=str(agents_dir),
            copies_root=str(world["source"].parent),
            slots_root=world["root"],
            require_root_owner=False,
            run=subprocess.run,
            chown=lambda *a: None,
            getpw=lambda user: PW,
            login=lambda *a: [],
            look=OWN,
            program=lambda path: None,
        )


def _registry(world):
    registry = world["tmp"] / "agents.json"
    registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
    return str(registry)


# --- part 2: launch, events, result record, patch handoff --------------------------------------------------

RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 1200,
    "num_turns": 3,
    "result": 'Done.\n```json\n{"status": "done", "commit": "abc", "tests": "pytest -q: 3 passed", "notes": "ok"}\n```\n',
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 5,
        "cache_read_input_tokens": 7,
    },
    "modelUsage": {
        "claude-sonnet-5-5": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 7,
                              "cacheCreationInputTokens": 5, "costUSD": 0.0123},
    },
}


def test_argv_and_env_isolate_the_session(world, monkeypatch):
    # never the host's real token file (a root run on a host with it would print the env dict on a failure)
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {})
    p = _prepare(world)
    a = wr.argv(p)
    # KD-9474: root's binary by its absolute path, never a copy in the agent's home
    assert a[0] == "/opt/digipaddock-claude/claude" and a[1:3] == ["-p", p.task]
    for flag in wr.ISOLATION_ARGS:
        assert flag in a
    assert (
        "--system-prompt-file" in a
        and a[a.index("--system-prompt-file") + 1] == p.system_path
    )
    assert (
        a[a.index("--settings") + 1] == p.settings_path
        and "--no-session-persistence" in a
    )
    assert (
        a[a.index("--output-format") + 1] == "stream-json"
        and "--verbose" in a
        and a[a.index("--model") + 1] == "sonnet"
    )
    e = wr.env(p)
    assert (
        e["KD_WORKER_SLOT"] == p.slot.root
        and e["KD_AGENT"] == "dev"
        and e["KD_WORKER_JOB"] == p.job["id"]
    )
    assert (
        e["HOME"] == "/home/digi-dev"
        and e["PATH"] == "/opt/digipaddock-claude:/usr/local/bin:/usr/bin:/bin"
        and e["DISABLE_AUTOUPDATER"] == "1"
        and e["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] == "false"
        and e["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    )
    # KD-9482: git finds no repository above the slot and reads no system config
    assert e["GIT_CEILING_DIRECTORIES"] == p.slot.root and e["GIT_CONFIG_NOSYSTEM"] == "1"
    assert not any(
        k.startswith("CLAUDE_CODE_OAUTH") or k == "ANTHROPIC_API_KEY" for k in e
    )
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in e  # placeholders in the slot; credentials deny


def test_env_carries_the_hosts_claude_token(world, monkeypatch):
    """the root runner reads the host's token into the job's environment — the only credential there."""
    p = _prepare(world)
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})
    e = wr.env(p)
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in e


# --- a job on a cabinet model runs on its upstream with the provider's key --------------------------
SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}


def _on_model(world, **extra):
    job = wr.check_definition({**world["definition"], "model_spec": SPEC, "effort": "high", **extra}, "dev/worker1")
    return _prepare({**world, "job": job})


def test_a_job_on_a_cabinet_model_runs_on_its_upstream_and_effort(world, monkeypatch):
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})
    monkeypatch.setattr(wr.llm_models, "read_secret", lambda provider, *a, **k: {"ANTHROPIC_AUTH_TOKEN": "k-" + provider})
    p = _on_model(world)
    assert (p.model, p.model_slug, p.model_spec, p.effort) == ("anthropic/claude-sonnet-4.5", "or-x", SPEC, "high")
    a = wr.argv(p)
    assert a[a.index("--model") + 1] == "anthropic/claude-sonnet-4.5" and a[a.index("--effort") + 1] == "high"
    e = wr.env(p)
    assert e["ANTHROPIC_AUTH_TOKEN"] == "k-openrouter" and e["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert e["ANTHROPIC_MODEL"] == "anthropic/claude-sonnet-4.5" and e["ANTHROPIC_API_KEY"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in e  # the host's Claude token never reaches a provider's process


def test_a_job_without_a_model_runs_on_the_pools_without_an_effort(world, monkeypatch):
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {})
    p = _prepare(world)
    assert (p.model, p.model_slug, p.model_spec, p.effort) == ("sonnet", "", None, "")
    assert "--effort" not in wr.argv(p) and "ANTHROPIC_BASE_URL" not in wr.env(p)


def test_a_job_runs_with_its_roles_context_limit_as_claudes_auto_compact_window(world, monkeypatch):
    """the cabinet names the role's limit in the definition it sends; ``-p`` mode compacts at it too. A job
    without one (the role has no limit) sets nothing: Claude Code's own window."""
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {})
    job = wr.check_definition({**world["definition"], "context_limit": 400000}, "dev/worker1")
    p = _prepare({**world, "job": job})
    assert p.context_limit == 400000 and wr.env(p)["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "400000"
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in wr.env(_prepare(world))
    for bad in (99999, "400000", True):
        with pytest.raises(wr.RunnerError):
            wr.check_definition({**world["definition"], "context_limit": bad}, "dev/worker1")


def test_a_missing_provider_key_is_the_jobs_failure(world, monkeypatch):
    monkeypatch.setattr(wr.llm_models, "read_secret", lambda *a, **k: {})
    p = _on_model(world)
    with pytest.raises(wr.RunnerError, match="no_key: openrouter"):
        wr.env(p)


@pytest.mark.parametrize("bad", [{"model_spec": "or-x"}, {"model_spec": {**SPEC, "id": "Bad/Name"}},
                                 {"model_spec": {**SPEC, "provider": "nope"}}, {"effort": "ultra"}, {"effort": 1}])
def test_a_bad_model_spec_or_effort_refuses_the_job(world, bad):
    with pytest.raises(wr.RunnerError):
        wr.check_definition({**world["definition"], **bad}, "dev/worker1")


def test_end_line_sums_the_usage_under_the_cabinet_models_slug(world):
    facts = {"base": None, "branch": None, "slot": "dev/worker1", "started": NOW, "ended": NOW + 5}
    outcome = {"exit": 0, "timed_out": False, "result_event": RESULT_EVENT, "output_bytes": 100}
    rec = wrs.build(world["job"], facts, outcome, head=None, stat="", patch=None)
    used = wr.end_line(rec, RESULT_EVENT, "or-x")["usage"]
    assert used == {"models": {"or-x": {"in": 10, "out": 20, "cache_read": 7, "cache_write": 5, "cost": 0}},
                    "requests": 3}
    assert "or-x" not in wr.end_line(rec, RESULT_EVENT)["usage"]["models"]  # the pool's job: Claude's own names


class FakeProc:
    def __init__(self, lines, rc=0, hang=False):
        import io as _io

        self.stdout = _io.BytesIO(b"".join(line.encode() + b"\n" for line in lines))
        self.rc, self.hang, self.killed = rc, hang, False

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self.rc

    def kill(self):
        self.killed = True

    def poll(self):
        return self.rc


def test_launch_keeps_events_and_the_result_event(world, tmp_path, monkeypatch):
    # the host's own token file never decides the test: a token of its own, passed by a descriptor
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})
    p = _prepare(world)
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        "not json",
        json.dumps(RESULT_EVENT),
    ]
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)
        return FakeProc(lines)

    out = wr.launch(
        p,
        popen=popen,
        events_path=str(tmp_path / "ev.jsonl"),
        stderr_path=str(tmp_path / "ev.stderr"),
        timeout=60,
        now=lambda: NOW,
    )
    assert (
        out.exit == 0
        and out.result_event["total_cost_usd"] == RESULT_EVENT["total_cost_usd"]
        and not out.timed_out
    )
    assert out.output_bytes > 0 and (out.started, out.ended) == (NOW, NOW)
    kept = (tmp_path / "ev.jsonl").read_text().splitlines()
    assert len(kept) == 2 and json.loads(kept[0])["type"] == "system"
    # the descriptor's number is whatever the host had free: it is one of the passed ones, the rest is the job's env
    launched = dict(seen["env"])
    fd = launched.pop("CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR")
    expected = {k: v for k, v in wr.env(p).items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    assert int(fd) in seen["pass_fds"] and launched == expected
    assert Path(seen["cwd"]) == Path(p.work) and seen["argv"] == wr.argv(p)
    assert (seen["user"], seen["group"], seen["extra_groups"]) == (
        PW.pw_uid,
        PW.pw_gid,
        [],
    ) and seen["start_new_session"] is True


def _launched_env(p, tmp_path):
    """What claude starts with: its environment and what it reads from each passed descriptor."""
    seen = {}

    def popen(argv, **kwargs):
        seen.update(env=kwargs["env"], fds={fd: os.read(fd, 1000) for fd in kwargs["pass_fds"]})
        return FakeProc([json.dumps(RESULT_EVENT)])

    wr.launch(p, popen=popen, events_path=str(tmp_path / "ev.jsonl"), stderr_path=str(tmp_path / "ev.stderr"),
              timeout=60, now=lambda: NOW)
    return seen["env"], seen["fds"]


@pytest.mark.skipif(os.name != "posix", reason="pipes: POSIX")
def test_launch_passes_the_hosts_token_only_by_a_descriptor(world, tmp_path, monkeypatch):
    """The token is in no variable of claude's environment; its pipe is closed in the runner after."""
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})
    e, fds = _launched_env(_prepare(world), tmp_path)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in e and "sk-ant-oat01-test" not in "".join(e.values())
    fd = int(e["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"])
    assert fds == {fd: b"sk-ant-oat01-test"}
    with pytest.raises(OSError):
        os.fstat(fd)  # the runner's copy is closed once claude has started


@pytest.mark.skipif(os.name != "posix", reason="pipes: POSIX")
def test_launch_without_a_token_passes_no_descriptor(world, tmp_path, monkeypatch):
    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {})
    e, fds = _launched_env(_prepare(world), tmp_path)
    assert fds == {} and not [k for k in e if k.endswith("_FILE_DESCRIPTOR")]


@pytest.mark.skipif(os.name != "posix", reason="pipes: POSIX")
def test_launch_on_a_provider_api_key_passes_it_by_a_descriptor(world, tmp_path, monkeypatch):
    """Provider kind anthropic: ANTHROPIC_API_KEY has a descriptor in claude 2.1.289."""
    monkeypatch.setattr(wr.llm_models, "read_secret", lambda provider, *a, **k: {"ANTHROPIC_API_KEY": "k-api"})
    p = _on_model(world, model_spec={"id": "an-x", "provider": "anthropic", "upstream": "claude-sonnet-4-5",
                                     "base_url": ""})
    e, fds = _launched_env(p, tmp_path)
    assert "ANTHROPIC_API_KEY" not in e and list(fds.values()) == [b"k-api"]
    assert e["CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"] == str(next(iter(fds)))


@pytest.mark.skipif(os.name != "posix", reason="pipes: POSIX")
def test_launch_on_a_provider_bearer_key_passes_it_by_the_api_keys_descriptor(world, tmp_path, monkeypatch):
    """The owner's decision on the card: ANTHROPIC_AUTH_TOKEN goes by the API key's descriptor; OpenRouter's empty
    ANTHROPIC_API_KEY goes with it, and no key stays in the environment."""
    monkeypatch.setattr(wr.llm_models, "read_secret", lambda provider, *a, **k: {"ANTHROPIC_AUTH_TOKEN": "k-or"})
    e, fds = _launched_env(_on_model(world), tmp_path)
    assert "ANTHROPIC_AUTH_TOKEN" not in e and "ANTHROPIC_API_KEY" not in e and list(fds.values()) == [b"k-or"]
    assert e["CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"] == str(next(iter(fds)))
    assert e["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"


def test_launch_kills_on_timeout(world, tmp_path):
    p = _prepare(world)
    proc = FakeProc([], hang=True)
    out = wr.launch(
        p,
        popen=lambda *a, **k: proc,
        events_path=str(tmp_path / "ev.jsonl"),
        stderr_path=str(tmp_path / "ev.stderr"),
        timeout=1,
        now=lambda: NOW,
    )
    assert out.timed_out and proc.killed and out.result_event is None


def test_launch_caps_the_result_text_it_keeps(world, tmp_path):
    p = _prepare(world)
    big = {**RESULT_EVENT, "result": "x" * (wr.OUTPUT_CAP + 10)}
    out = wr.launch(
        p,
        popen=lambda *a, **k: FakeProc([json.dumps(big)]),
        events_path=str(tmp_path / "ev.jsonl"),
        stderr_path=str(tmp_path / "ev.stderr"),
        timeout=60,
        now=lambda: NOW,
    )
    assert len(out.result_event["result"]) == wr.OUTPUT_CAP


# Claude Code's stream when it refuses the credentials (the fixture of test_session_host_claude.py)
AUTH_TEXT = "Invalid API key · Please run /login"
AUTH_LINES = [
    json.dumps({"type": "assistant", "error": "authentication_failed",
                "message": {"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": AUTH_TEXT}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": AUTH_TEXT}),
]


def test_launch_marks_a_refused_sign_in(world, tmp_path):
    p = _prepare(world)
    out = wr.launch(p, popen=lambda *a, **k: FakeProc(AUTH_LINES, rc=1), events_path=str(tmp_path / "ev.jsonl"),
                    stderr_path=str(tmp_path / "ev.stderr"), timeout=60, now=lambda: NOW)
    assert out.auth_failed is True and out.result_event["is_error"] is True
    out = wr.launch(p, popen=lambda *a, **k: FakeProc([json.dumps(RESULT_EVENT)]),
                    events_path=str(tmp_path / "ok.jsonl"), stderr_path=str(tmp_path / "ok.stderr"), timeout=60,
                    now=lambda: NOW)
    assert out.auth_failed is False


def _options(world, copies=None):
    """The keyword arguments of run and main for this world (the host's paths under tmp)."""
    tmp_path = world["tmp"]
    agents_root = tmp_path / "agents"
    (agents_root / "dev" / ".claude").mkdir(parents=True, exist_ok=True)
    (agents_root / "dev" / ".claude" / "policy.json").write_bytes(
        DEV_POLICY.read_bytes()
    )
    (agents_root / "dev" / "work" / "tmp").mkdir(parents=True, exist_ok=True)
    (tmp_path / "agents.json").write_text(json.dumps(REGISTRY), encoding="utf-8")

    def run(argv, **kwargs):  # every git call: its global config and the user it runs as
        if argv[:1] == ["git"]:
            world.setdefault("git_calls", []).append(
                (argv, (kwargs.get("env") or {}).get("GIT_CONFIG_GLOBAL"), kwargs.get("user"))
            )
        return subprocess.run(argv, **kwargs)

    return dict(
        config_path=CONFIG,
        registry_path=str(tmp_path / "agents.json"),
        packs_root=SPECS_ROOT,
        agents_root=AGENTS,
        policy_root=str(agents_root),
        floor_path=str(FLOOR),
        copies_root=str(copies if copies is not None else world["source"].parent),
        slots_root=world["root"],
        results_root=str(tmp_path / "results"),
        events_root=str(tmp_path / "events"),
        agent_tmp=str(agents_root / "dev" / "work" / "tmp"),
        require_root_owner=False,
        run=run,
        chown=lambda *a: None,
        getpw=lambda user: PW,
        look=OWN,
        login=lambda *a: [],
        program=lambda path: None,
        now=lambda: NOW,
    )


def _record(world):
    return json.loads(
        (world["tmp"] / "results" / f"{world['job']['id']}.json").read_text("utf-8")
    )


def _run(world, popen, copies=None):
    code, end = wr.run(world["job"], popen=popen, **_options(world, copies))
    return code, end, _record(world)


def test_a_runner_failure_after_claude_is_a_failed_result(world):
    def popen(
        argv, **kwargs
    ):  # the session ends well, then the copy's HEAD is unreadable for git
        (Path(kwargs["cwd"]) / "repo" / ".git" / "HEAD").write_text(
            "ref: refs/heads/\n"
        )
        return FakeProc([json.dumps(RESULT_EVENT)])

    code, end, rec = _run(world, popen)
    assert code == 0 and end["state"] == "failed" and end["reason"] == rec["reason"]
    assert (
        rec["exit"] == 0 and rec["reason"].startswith("git:") and rec["patch"] is None
    )
    assert wrs.outcome_of(rec) == ("failed", rec["reason"])


def test_run_writes_the_record_and_hands_the_patch_over(world):
    def popen(
        argv, **kwargs
    ):  # "claude" commits one change in the slot's copy, then ends with the block
        repo = Path(kwargs["cwd"]) / "repo"
        (repo / "README.md").write_text("three\n")
        git("commit", "-qam", "three", cwd=repo)
        return FakeProc([json.dumps(RESULT_EVENT)])

    code, end, rec = _run(world, popen)
    assert code == 0 and end["state"] == "done", end
    assert rec["provenance"]["base"] == git("rev-parse", "HEAD", cwd=world["source"]), (
        rec.get("reason")
    )
    assert (
        rec["provenance"]["commit"] != rec["provenance"]["base"]
        and "README.md" in rec["diff_stat"]
    )
    patch = Path(rec["patch"])
    # into the temp folder of the job's card, not the agent's shared work/tmp
    card = world["tmp"] / "agents" / "dev" / "work" / "tmp" / "cards" / world["job"]["card"]
    assert patch == card / f"worker-{world['job']['id']}.patch"
    assert "three" in patch.read_text()
    assert card.stat().st_uid == PW.pw_uid and oct(card.stat().st_mode & 0o777) == oct(0o750)
    assert (
        rec["result"]["status"] == "done"
        and rec["usage"]["cost_usd"] == RESULT_EVENT["total_cost_usd"]
    )
    events = world["tmp"] / "events" / f"{world['job']['id']}.jsonl"
    assert json.loads(events.read_text().splitlines()[-1])["type"] == "result"
    # before the session root clones with the slot's trust file; after it git runs as the agent, no global config
    config = f"{world['root']}/dev/worker1/gitconfig"
    calls = world["git_calls"]
    before = [c for c in calls if c[2] is None]
    after = [c for c in calls if c[2] is not None]
    assert before and all(Path(c[1] or "") == Path(config) for c in before), calls
    assert [_subcommand(c[0]) for c in after] == ["rev-parse", "diff", "diff"], calls
    assert all(c[1] == "/dev/null" and c[2] == PW.pw_uid for c in after), calls
    assert all("--no-ext-diff" in c[0] and "--no-textconv" in c[0] for c in after if _subcommand(c[0]) == "diff"), calls
    assert all("protocol.allow=never" in c[0] for c in calls), calls


def test_end_line_has_outcome_comment_and_usage(world):
    code, end, rec = _run(world, lambda *a, **k: FakeProc([json.dumps(RESULT_EVENT)]))
    assert code == 0
    # the record on disk has sorted keys; the comment keeps the block's own order
    assert sorted(end.pop("comment").splitlines()) == sorted(wrs.comment(rec).splitlines())
    assert end == {
        "type": "job",
        "phase": "ended",
        "state": "done",
        "reason": "",
        "usage": {
            "models": {"claude-sonnet-5-5": {"in": 10, "out": 20, "cache_read": 7, "cache_write": 5, "cost": 0.0123}},
            "requests": 3,
        },
        "result": rec["result"],
    }
    # the session host takes it whole: the same parser it runs on the runner's stdout
    end["comment"] = wrs.comment(rec)
    assert host_worker.end_line(json.dumps(end, ensure_ascii=False).encode()) == {
        k: end[k] for k in ("state", "reason", "comment", "usage", "result")
    }


def test_a_refused_sign_in_ends_the_job_auth_failed(world):
    """the job whose Claude cannot sign in ends failed with auth_failed, not «claude reported success»."""
    code, end, rec = _run(world, lambda *a, **k: FakeProc(AUTH_LINES, rc=1))
    assert code == 0 and (end["state"], end["reason"]) == ("failed", "auth_failed") and rec["auth_failed"] is True
    assert "нет входа в Claude" in end["comment"] and "auth_failed" not in end["comment"]
    assert host_worker.end_line(json.dumps(end, ensure_ascii=False).encode())["reason"] == "auth_failed"


def test_a_final_block_over_the_limit_fails_the_job(world):
    block = json.dumps({"status": "done", "notes": "x" * (wr.RESULT_MAX + 1024)})
    event = {**RESULT_EVENT, "result": f"```json\n{block}\n```"}
    code, end, rec = _run(world, lambda *a, **k: FakeProc([json.dumps(event)]))
    assert code == 0 and (end["state"], end["result"]) == ("failed", None) and "64 KiB" in end["reason"]
    assert host_worker.end_line(json.dumps(end).encode())["state"] == "failed"
    first = end["comment"].splitlines()[0]  # final review: the comment says what the state says
    assert ": сбой" in first and "готово" not in first and "64 KiB" in first and "x" * 100 not in end["comment"]


def test_the_three_hops_hold_one_limit_of_the_final_block():
    from kd.cabinet import jobs

    assert wr.RESULT_MAX == host_worker.RESULT_MAX == jobs.RESULT_MAX == 64 * 1024
    # the text kept for the block search holds a block of that size with words before it; the end line holds it too
    assert wr.OUTPUT_CAP >= 2 * wr.RESULT_MAX and host_worker.LINE_MAX >= 2 * wr.RESULT_MAX


def test_end_line_keeps_a_thorough_critics_block(world):
    """thirty findings in Russian — some 40 KiB — are a review, not an overflow."""
    import council_env

    block = council_env.thorough()
    facts = {"base": None, "branch": None, "slot": "dev/worker1", "started": NOW, "ended": NOW + 5}
    outcome = {"exit": 0, "timed_out": False, "result_event": RESULT_EVENT, "output_bytes": 100}
    rec = {**wrs.build(world["job"], facts, outcome, head=None, stat="", patch=None), "result": block}

    end = wr.end_line(rec, RESULT_EVENT)

    assert end["result"] == block
    line = json.dumps(end, ensure_ascii=False).encode()
    assert len(line) <= host_worker.LINE_MAX and host_worker.end_line(line)["result"] == block


def test_a_cyrillic_final_block_is_measured_in_utf8_bytes(world):
    """Final review: 6 K Cyrillic characters are 12 KiB, not the 36 KiB of their ASCII escapes."""
    block = json.dumps({"status": "done", "notes": "ж" * (6 * 1024)}, ensure_ascii=False)
    event = {**RESULT_EVENT, "result": f"```json\n{block}\n```"}
    code, end, rec = _run(world, lambda *a, **k: FakeProc([json.dumps(event, ensure_ascii=False)]))
    assert code == 0 and end["state"] == "done" and end["result"]["notes"] == "ж" * (6 * 1024)
    assert host_worker.end_line(json.dumps(end, ensure_ascii=False).encode())["result"] == end["result"]


def test_a_job_without_commits_hands_no_patch_over(world):
    code, end, rec = _run(
        world, lambda *a, **k: FakeProc([json.dumps(RESULT_EVENT)])
    )
    assert (
        code == 0
        and rec["patch"] is None
        and rec["provenance"]["commit"] == rec["provenance"]["base"]
    )


def test_a_ui_design_worker_missing_an_input_is_blocked_without_a_commit(world):
    """ui-design with skill frontend; the brief names no widths — the worker designs
    nothing, ends "blocked" naming the input, and nothing is committed or handed over."""
    request = wj.Request("KD-121", "ui-design", ("frontend",), "Design the Inbox screen.")
    admission = wj.admit(request, _card("KD-121"), repos=frozenset({"digipaddock"}), registry=REG,
                         config=world["config"], types={"dev": "developer"})
    definition = wj.definition(request, admission, job_id=wj.new_job_id(), caller="digi-boss",
                               requester="boss", config=world["config"])
    world["job"] = wr.check_definition(definition, "dev/worker1")
    block = {"status": "blocked", "commit": None, "files": [], "tokens": [], "screens": [],
             "missing_inputs": ["widths: the card names none", "scenarios of the Inbox screen"]}
    event = {**RESULT_EVENT, "result": f"Blocked.\n```json\n{json.dumps(block)}\n```\n"}
    code, end, rec = _run(world, lambda *a, **k: FakeProc([json.dumps(event)]))
    assert code == 0 and rec["spec"] == "ui-design" and rec["skills"] == ["frontend"]
    assert rec["provenance"]["skills"] == {
        "frontend": {"version": REG.skills["frontend"].version, "sha256": REG.skills["frontend"].sha256}
    }
    assert rec["result"]["status"] == "blocked" and rec["result"]["missing_inputs"]
    assert rec["provenance"]["commit"] == rec["provenance"]["base"] and rec["patch"] is None
    assert not list((world["tmp"] / "agents" / "dev" / "work" / "tmp").iterdir())
    assert end["state"] == "done" and end["reason"].startswith("blocked: ")
    assert end["result"]["missing_inputs"] == block["missing_inputs"]


def test_a_runner_failure_before_claude_is_a_failed_result(world):
    code, end, rec = _run(
        world,
        popen=lambda *a, **k: pytest.fail("claude must not start"),
        copies=world["tmp"] / "nowhere",
    )
    assert code == 0 and end["state"] == "failed" and end["usage"] == {"models": {}, "requests": 0}
    assert (
        rec["result"] is None
        and rec["exit"] is None
        and "not a git work tree" in rec["reason"]
    )
    assert wrs.outcome_of(rec) == ("failed", rec["reason"])


def test_a_record_that_cannot_be_written_is_the_units_failure(world):
    (world["tmp"] / "blocker").write_text("not a directory")
    options = _options(world, copies=world["tmp"] / "nowhere")
    options["results_root"] = str(world["tmp"] / "blocker" / "results")
    code, end = wr.run(world["job"], popen=lambda *a, **k: None, **options)
    assert code == 1 and end["state"] == "failed"  # the outcome still reaches the host


def _main(world, argv, stdin, copies=None, popen=None):
    out = io.StringIO()
    code = wr.main(argv, stdin=io.BytesIO(stdin), out=out,
                   popen=popen or (lambda *a, **k: FakeProc([json.dumps(RESULT_EVENT)])),
                   **_options(world, copies))
    return code, out.getvalue()


def test_main_reads_the_job_from_stdin_and_prints_its_end(world):
    code, out = _main(world, ["--slot", "dev/worker1"], json.dumps(world["definition"]).encode() + b"\n")
    assert code == 0 and out.count("\n") == 1, out
    end = json.loads(out)
    assert (end["type"], end["phase"], end["state"]) == ("job", "ended", "done")
    assert _record(world)["slot"] == "dev/worker1"


def test_a_prepare_failure_still_prints_an_end_line(world):
    code, out = _main(world, ["--slot", "dev/worker1"], json.dumps(world["definition"]).encode(),
                      copies=world["tmp"] / "nowhere", popen=lambda *a, **k: pytest.fail("claude must not start"))
    end = json.loads(out)
    assert code == 0 and out.count("\n") == 1
    assert end["state"] == "failed" and "not a git work tree" in end["reason"] and end["comment"]


@pytest.mark.parametrize(
    "stdin",
    [b"", b"{", b"[]", b"x" * (wr.STDIN_MAX + 2)],
    ids=["empty", "broken", "not-a-job", "too-long"],
)
def test_a_bad_definition_prints_a_failed_end_line(world, stdin):
    code, out = _main(world, ["--slot", "dev/worker1"], stdin, popen=lambda *a, **k: pytest.fail("no claude"))
    end = json.loads(out)
    assert code == 1 and end["state"] == "failed" and end["reason"].startswith("bad job definition")
    assert host_worker.end_line(out.encode())["state"] == "failed"
    assert not (world["tmp"] / "results").exists()


def test_the_entry_point_takes_one_slot_only(world):
    for argv in ([], ["x"], ["a" * 32], ["--slot"], ["--slot", "dev/worker0"], ["--slot", "dev/worker1", "x"]):
        assert wr.main(argv, stdin=io.BytesIO(b""), out=io.StringIO()) == 2, argv
    assert not hasattr(wr, "load_job") and not hasattr(wr, "default_finish")


@pytest.mark.parametrize("euid, dropped", [(0, True), (1000, False)])
def test_git_as_the_agent_drops_other_groups_only_as_root(monkeypatch, euid, dropped):
    # setgroups needs root: a runner that is not root (the tests, dev.check) runs git as itself with no extra_groups
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(stdout="ok\n")

    monkeypatch.setattr(wr.os, "geteuid", lambda: euid)
    assert wr._git(run, ["status"], as_user=(1001, 1002)) == "ok"
    assert (seen["user"], seen["group"]) == (1001, 1002)
    assert ("extra_groups" in seen) is dropped and seen.get("extra_groups", []) == []


def test_the_jobs_git_config_runs_nothing_after_the_job(world, tmp_path):
    # review 04.11.2026: the job writes its copy's .git/config; an external diff or a textconv driver named there
    # must not run when the runner reads the copy after the session (it once ran as root)
    marker = tmp_path / "ran"
    tool = tmp_path / "tool.sh"
    tool.write_text(f"#!/bin/sh\necho ran >> {marker}\ncat \"$1\" 2>/dev/null\n")
    tool.chmod(0o755)

    def popen(argv, **kwargs):  # "claude" names the tool for diffs and text conversion, then commits a change
        repo = Path(kwargs["cwd"]) / "repo"
        git("config", "diff.external", str(tool), cwd=repo)
        git("config", "diff.conv.textconv", str(tool), cwd=repo)
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("* diff=conv\n")
        (repo / "README.md").write_text("three\n")
        git("commit", "-qam", "three", cwd=repo)
        return FakeProc([json.dumps(RESULT_EVENT)])

    code, end, rec = _run(world, popen)
    assert code == 0 and end["state"] == "done", end
    assert not marker.exists()
    assert "+three" in Path(rec["patch"]).read_text()


def _subcommand(argv):
    """git's subcommand past the leading ``-c name=value`` pairs."""
    at = 1
    while argv[at] == "-c":
        at += 2
    return argv[at]


def test_git_runs_with_no_program_or_transport_the_repository_names():
    # KD-9482: command-line config beats every config file, and GIT_NO_LAZY_FETCH stops a missing object's fetch
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, env=kwargs["env"])
        return SimpleNamespace(stdout="ok\n")

    assert wr._git(run, ["status"], env={"KEEP": "1"}) == "ok"
    assert seen["argv"] == ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                            "-c", "core.sshCommand=false", "-c", "protocol.allow=never", "status"]
    assert seen["env"] == {"KEEP": "1", "GIT_NO_LAZY_FETCH": "1"}


def test_the_clone_reaches_the_hosts_copy_by_its_local_path_only(world):
    # before the job root clones and fetches from the host's copy: the file transport and no other
    copy, head = _synced(world)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.run(argv, **kwargs)

    wr.prepare_repo(Slot.parse("dev", "worker1"), str(copy), job_id=world["job"]["id"], run=run,
                    chown=lambda *a: None, root=world["root"], pull={"number": 7, "head": head, "target": "main"})
    assert calls and all("protocol.allow=never" in argv for argv in calls), calls
    local = [_subcommand(argv) for argv in calls if "protocol.file.allow=always" in argv]
    assert local == ["clone", "fetch", "fetch"], calls


def _as_self(argv, **kwargs):
    """subprocess.run without the agent's ids: the test runs git as its own user (Windows has no setuid)."""
    return subprocess.run(argv, **{k: v for k, v in kwargs.items() if k not in ("user", "group", "extra_groups")})


def _job_copy(world, monkeypatch):
    """The slot's copy as prepare_repo leaves it and the job's Prepared, for the reads after the session."""
    monkeypatch.setattr(wr.os, "geteuid", lambda: 1000, raising=False)
    slot = Slot.parse("dev", "worker1")
    base, branch = wr.prepare_repo(slot, str(world["source"]), job_id=world["job"]["id"], run=subprocess.run,
                                   chown=lambda *a: None, root=world["root"])
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    prepared = wr.Prepared(job=world["job"], slot=slot, user=PW.pw_name, uid=PW.pw_uid, gid=PW.pw_gid,
                           home=PW.pw_dir, settings_path="", system_path="", task="", base=base, branch=branch,
                           model=None, work=str(work))
    return prepared, work / "repo"


def test_a_lazy_fetch_the_job_set_up_runs_nothing_after_the_job(world, tmp_path, monkeypatch):
    # KD-9482 (probed on the host): an object missing from a copy the job made a partial clone sent git to fetch it,
    # through the ssh program the job's config named — as the agent, outside the job's sandbox
    marker = tmp_path / "ran"
    tool = tmp_path / "tool.sh"
    tool.write_text(f"#!/bin/sh\necho ran >> {marker.as_posix()}\nexit 1\n")
    tool.chmod(0o755)
    p, repo = _job_copy(world, monkeypatch)
    for key, value in (("remote.origin.url", "ssh://example.test/x"), ("remote.origin.promisor", "true"),
                       ("extensions.partialClone", "origin"), ("core.sshCommand", tool.as_posix())):
        git("config", key, value, cwd=repo)
    (repo / "README.md").write_text("three\n")
    git("commit", "-qam", "three", cwd=repo)
    blob = git("rev-parse", "HEAD:README.md", cwd=repo)
    loose = repo / ".git" / "objects" / blob[:2] / blob[2:]
    loose.chmod(0o644)  # git writes objects read-only (Windows will not unlink such a file)
    loose.unlink()  # the job's commit names an object it removed

    with pytest.raises(wr.RunnerError, match="^git: "):
        wr.repo_facts(p, run=_as_self)
    assert not marker.exists()


@pytest.mark.parametrize("plant", [
    lambda git_dir: (git_dir / "commondir").write_text("/elsewhere/.git\n"),
    lambda git_dir: (git_dir / "config.worktree").write_text("[core]\n"),
    lambda git_dir: (git_dir.rename(git_dir.with_name("old")), git_dir.write_text("gitdir: /elsewhere/.git\n")),
    lambda git_dir: git_dir.rename(git_dir.with_name("old")),  # git would look for a repository above the copy
], ids=["commondir", "config.worktree", "gitfile", "missing"])
def test_a_job_copy_that_points_git_elsewhere_is_refused_before_git_runs(world, monkeypatch, plant):
    p, repo = _job_copy(world, monkeypatch)
    plant(repo / ".git")

    def run(*a, **k):
        pytest.fail("git must not run on a refused copy")

    for read in (wr.repo_facts, lambda p, run: wr.hand_over_patch(p, run=run, agent_tmp=str(world["tmp"]))):
        with pytest.raises(wr.RunnerError, match="the job's copy is refused"):
            read(p, run=run)


def test_prepare_leaves_nothing_but_its_own_entries_in_work(world):
    # KD-9482: work/ is the job's cwd and the agent's to write; what a job left there — a repository at its top, a
    # folder's instructions, settings — never reaches the next job
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    for path in (".git/config", "notes/CLAUDE.md", ".claude/rules/x.md", "stray.txt"):
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text("x\n")
    _prepare(world)
    assert sorted(p.name for p in work.iterdir()) == ["CLAUDE.local.md", "CLAUDE.md", "inputs", "repo", "tmp"]


# --- a job on a model of the Claude subscription ------------------------------------------------------
SUB = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}


def test_a_job_on_a_subscription_model_runs_on_the_alias_with_the_hosts_token(world, monkeypatch):
    def no_secret(*a, **k):
        raise AssertionError("a subscription model reads no kind's key")

    monkeypatch.setattr(wr.claude_token, "env", lambda *a: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"})
    monkeypatch.setattr(wr.llm_models, "read_secret", no_secret)
    job = wr.check_definition({**world["definition"], "model_spec": SUB}, "dev/worker1")
    p = _prepare({**world, "job": job})
    assert (p.model, p.model_slug, p.model_spec) == ("opus", "", SUB)
    a = wr.argv(p)
    assert a[a.index("--model") + 1] == "opus"
    e = wr.env(p)
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-test"
    assert "ANTHROPIC_BASE_URL" not in e and "ANTHROPIC_MODEL" not in e and "ANTHROPIC_AUTH_TOKEN" not in e


def test_the_usage_slug_is_the_cabinet_models_only(world):
    """A subscription job's usage stays under the names Claude Code reports, like the pool's."""
    assert wr.usage_slug({**world["job"], "model_spec": SUB}) == ""
    assert wr.usage_slug({**world["job"], "model_spec": SPEC}) == "or-x"
    assert wr.usage_slug(world["job"]) == ""


# --- the inputs of a job of the cabinet, read-only files of the slot's work/inputs ------------------
INPUTS = {"brief.md": "# KD-121\n\nбриф\n", "thread.md": "#1 @owner text: go\n"}


def test_a_definition_may_carry_inputs(world):
    job = wr.check_definition({**world["definition"], "inputs": INPUTS}, "dev/worker1")
    assert job["inputs"] == INPUTS


@pytest.mark.parametrize("inputs", [{}, [], {"brief.txt": "x"}, {"../x.md": "x"}, {"brief.md": ""}, {"brief.md": 1}])
def test_bad_inputs_refuse_the_definition(world, inputs):
    with pytest.raises(wr.RunnerError, match="job definition"):
        wr.check_definition({**world["definition"], "inputs": inputs}, "dev/worker1")


def test_inputs_do_not_widen_the_definition_itself(world):
    fat = {**world["definition"], "brief": "x" * (wr.DEFINITION_MAX + 1), "inputs": INPUTS}
    with pytest.raises(wr.RunnerError, match="over"):
        wr.check_definition(fat, "dev/worker1")


def test_prepare_writes_the_inputs_into_the_slot(world):
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    (work / "inputs").mkdir(parents=True)
    (work / "inputs" / "old.md").write_text("the last job's card\n")
    world["job"] = wr.check_definition({**world["definition"], "inputs": INPUTS}, "dev/worker1")

    _prepare(world)

    assert sorted(p.name for p in (work / "inputs").iterdir()) == ["brief.md", "thread.md"]
    assert (work / "inputs" / "brief.md").read_text("utf-8") == INPUTS["brief.md"]


@pytest.mark.parametrize("left_closed", [False, True])
def test_the_worker_can_enter_its_inputs_under_the_runners_umask(world, left_closed):
    # kd-worker-run works under umask 077 and the folder is root's, so its mode is set, not left to the
    # umask — the worker's user must enter it; a folder an earlier job left closed is opened again
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    if left_closed:
        (work / "inputs").mkdir(parents=True)
        os.chmod(work / "inputs", 0o700)
    world["job"] = wr.check_definition({**world["definition"], "inputs": INPUTS}, "dev/worker1")
    old = os.umask(0o077)
    try:
        _prepare(world)
    finally:
        os.umask(old)

    assert os.stat(work / "inputs").st_mode & 0o777 == 0o755
    assert os.stat(work / "inputs" / "brief.md").st_mode & 0o777 == 0o644
    assert os.stat(work / "tmp").st_mode & 0o777 == 0o755


def test_a_job_without_inputs_leaves_none_of_the_last_job(world):
    work = Path(world["root"]) / "dev" / "worker1" / "work"
    (work / "inputs").mkdir(parents=True)
    (work / "inputs" / "old.md").write_text("the last job's card\n")

    _prepare(world)

    assert list((work / "inputs").iterdir()) == []


def test_main_reads_a_job_with_inputs_past_the_definitions_limit(world):
    big = {**world["definition"], "inputs": {"brief.md": "x" * (wr.DEFINITION_MAX * 2)}}
    # the job is read and taken — it fails later, where the slot is prepared (no source copy), not as a bad definition
    code, out = _main(world, ["--slot", "dev/worker1"], json.dumps(big).encode() + b"\n",
                      copies=world["tmp"] / "nowhere", popen=lambda *a, **k: pytest.fail("claude must not start"))
    end = json.loads(out)
    assert code == 0 and end["state"] == "failed" and "not a git work tree" in end["reason"], out


# --- a job on a pull request's head ---------------------------------------------------------------
def _synced(world):
    """A host copy as kd-repo-sync leaves it — a clone of the forge's repository with the heads of its pull
    requests — and the head of pull 7: one commit over ``main``; the forge also has a branch ``release``."""
    forge = world["source"]
    git("branch", "release", "HEAD~1", cwd=forge)
    git("checkout", "-q", "-b", "feature", cwd=forge)
    (forge / "feature.txt").write_text("new\n")
    git("add", "feature.txt", cwd=forge)
    git("commit", "-qm", "feature", cwd=forge)
    head = git("rev-parse", "HEAD", cwd=forge)
    git("update-ref", "refs/pull/7/head", head, cwd=forge)
    git("checkout", "-q", "main", cwd=forge)
    git("branch", "-qD", "feature", cwd=forge)
    copy = world["tmp"] / "synced" / "digipaddock"
    copy.parent.mkdir()
    git("clone", "-q", str(forge), str(copy), cwd=world["tmp"])
    git("fetch", "-q", "origin", "+refs/pull/*/head:refs/pull/*/head", cwd=copy)
    return copy, head


def _on_pull(world, copy, pull):
    return wr.prepare_repo(
        Slot.parse("dev", "worker1"),
        str(copy),
        job_id=world["job"]["id"],
        run=subprocess.run,
        chown=lambda path, uid, gid: None,
        root=world["root"],
        pull=pull,
    )


def test_a_pull_job_is_checked_out_on_the_pull_head(world):
    copy, head = _synced(world)

    base, branch = _on_pull(world, copy, {"number": 7, "head": head, "target": "main"})

    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    main = git("rev-parse", "main", cwd=world["source"])
    assert (base, branch) == (main, "pr-head")
    assert git("rev-parse", "HEAD", cwd=repo) == head
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "pr-head"
    assert git("rev-parse", "pr-base", cwd=repo) == main
    assert git("remote", cwd=repo) == ""
    assert git("diff", "--name-only", "pr-base...pr-head", cwd=repo) == "feature.txt"
    assert sorted(git("branch", "--format=%(refname:short)", cwd=repo).split()) == [
        "main",
        "pr-base",
        "pr-head",
    ]


def test_a_pull_whose_head_is_not_in_the_copy_is_head_missing(world):
    copy, head = _synced(world)

    with pytest.raises(wr.RunnerError, match="^head_missing: "):
        _on_pull(world, copy, {"number": 8, "head": head, "target": "main"})


def test_a_pull_whose_ref_moved_is_head_missing(world):
    copy, head = _synced(world)
    other = git("rev-parse", "main", cwd=world["source"])  # the copy holds another head of pull 7

    with pytest.raises(wr.RunnerError, match="^head_missing: pull 7 is at "):
        _on_pull(world, copy, {"number": 7, "head": other, "target": "main"})


def test_a_pull_into_another_target_branch(world):
    copy, head = _synced(world)

    base, _ = _on_pull(world, copy, {"number": 7, "head": head, "target": "release"})

    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    release = git("rev-parse", "release", cwd=world["source"])
    assert base == release != git("rev-parse", "main", cwd=world["source"])
    assert git("rev-parse", "pr-base", cwd=repo) == release
    with pytest.raises(wr.RunnerError, match="^head_missing: "):  # a target the copy has not got
        _on_pull(world, copy, {"number": 7, "head": head, "target": "gone"})


def test_a_job_without_pull_is_prepared_as_before(world):
    copy, _ = _synced(world)

    base, branch = _on_pull(world, copy, None)

    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    assert (base, branch) == (
        git("rev-parse", "main", cwd=world["source"]),
        f"worker/{world['job']['id']}",
    )
    assert sorted(git("branch", "--format=%(refname:short)", cwd=repo).split()) == ["main", branch]


PULL = {"number": 7, "head": "a" * 40, "target": "main"}


def _reader(world, **pull):
    """The definition of a developer job that writes nothing, on a pull request."""
    limits = {**world["definition"]["limits"], "write": "none"}
    return {**world["definition"], "limits": limits, "writer": False, "pull": {**PULL, **pull}}


def test_a_definition_carries_a_pull(world):
    job = wr.check_definition(_reader(world), "dev/worker1")

    assert job["pull"] == PULL


@pytest.mark.parametrize(
    "bad",
    [
        "7",
        {**PULL, "extra": 1},
        {"number": 7, "head": "a" * 40},
        {**PULL, "head": "a" * 39},
        {**PULL, "head": "A" * 40},
        {**PULL, "number": 0},
        {**PULL, "number": True},
        {**PULL, "number": "7"},
        {**PULL, "target": "a..b"},
        {**PULL, "target": "-x"},
        {**PULL, "target": "a b"},
        {**PULL, "target": "x.lock"},
        {**PULL, "target": "a//b"},
        {**PULL, "target": ""},
    ],
)
def test_definition_refuses_a_bad_pull(world, bad):
    with pytest.raises(ValueError):
        wj.check_pull(bad)
    job = _reader(world)
    job["pull"] = bad
    with pytest.raises(wr.RunnerError, match="job definition: "):
        wr.check_definition(job, "dev/worker1")


def test_a_job_that_writes_is_never_on_a_pull(world):
    writer = {**world["definition"], "pull": PULL}  # the implementer: write repo

    with pytest.raises(wr.RunnerError, match="writes nothing"):
        wr.check_definition(writer, "dev/worker1")


def test_prepare_puts_a_pull_job_on_its_head(world):
    copy, head = _synced(world)
    request = wj.Request("KD-121", "reviewer", (), "Read the pull request.")
    admission = wj.admit(
        request,
        _card("KD-121"),
        repos=frozenset({"digipaddock"}),
        registry=REG,
        config=world["config"],
        types={"dev": "developer"},
    )
    definition = wj.definition(
        request,
        admission,
        job_id=wj.new_job_id(),
        caller="cabinet",
        requester="",
        config=world["config"],
    )
    definition["pull"] = {"number": 7, "head": head, "target": "main"}
    world["job"] = wr.check_definition(definition, "dev/worker1")

    prepared = _prepare(world, copies_root=str(copy.parent))

    repo = Path(world["root"]) / "dev" / "worker1" / "work" / "repo"
    assert (prepared.base, prepared.branch) == (
        git("rev-parse", "main", cwd=world["source"]),
        "pr-head",
    )
    assert git("rev-parse", "HEAD", cwd=repo) == head
