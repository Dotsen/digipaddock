"""C04, common/bin/kd-worker, the coordinator's only way to worker jobs — the cabinet keeps them."""

import io
import json
import shutil
import sys
from pathlib import Path

import pytest

from kd import worker_jobs as wj
from kd import worker_packs, worker_result, worker_slots
from kd.cabinet import client as cabinet_client
from kd.cabinet import jobs as cabinet_jobs
from kd.specializations import SPECS_ROOT, load_registry

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_board
from admin_board import AdminBoard  # noqa: E402 — the board is the cabinet

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "common/worker/worker-slots.json"
FIXTURE_AGENTS = REPO / "common/tests/fixtures/policy/agents"
REG = load_registry(SPECS_ROOT, require_root_owner=False)
SKILL = sorted(REG.specs["implementer"].skills)[0]
AGENTS = {
    "boss": {"user": "digi-boss", "runtime": "generated", "telegram": True},
    "dev": {"user": "digi-dev", "runtime": "generated", "telegram": True},
    "analyst": {"user": "digi-analyst", "runtime": "generated", "telegram": True},
}
BOSS = ("boss", "digi-boss")


class Recording:
    """The executor's cabinet client that keeps every request it sends."""

    def __init__(self, board):
        self.board, self.requests = board, []

    def call(self, request):
        self.requests.append(json.loads(json.dumps(request)))
        return self.board.call(request)

    def sent(self, op):
        return [r for r in self.requests if r["op"] == op]


class Refusing(Recording):
    """A cabinet that refuses ``op`` with ``kind``."""

    def __init__(self, board, op, kind):
        super().__init__(board)
        self.op, self.kind = op, kind

    def call(self, request):
        if request["op"] == self.op:
            raise cabinet_client.CabinetError(self.kind, f"{self.kind}: refused")
        return super().call(request)


class Env:
    def __init__(self, tmp_path):
        self.board = AdminBoard(tmp_path / "cabinet", user="digi")  # kd-worker runs as the base user
        self.client = Recording(self.board)
        self.registry = tmp_path / "agents.json"
        self.registry.write_text(json.dumps(AGENTS), encoding="utf-8")
        self.enabled = tmp_path / "worker-slots.enabled"
        self.enabled.write_text("dev/worker1\ndev/worker2\n", encoding="utf-8")
        self.repos = tmp_path / "repos.json"
        self.repos.write_text(json.dumps({"digipaddock": {"repo": "o/r", "dir": "work/R"}}), encoding="utf-8")
        self.manifests = tmp_path / "agents"
        for name in ("analyst", "boss", "dev", "sysadmin"):
            (self.manifests / name).mkdir(parents=True)
            shutil.copyfile(FIXTURE_AGENTS / name / "agent.json", self.manifests / name / "agent.json")
        self.results = tmp_path / "results"
        self.results.mkdir()

    def card(self, task_id="KD-121", status="To Do", assignee="@dev", project="digipaddock"):
        text = (
            f"---\nid: {task_id}\ntitle: 'Card {task_id}'\nstatus: {status}\nassignee:\n"
            f"  - '{assignee}'\nlabels: []\ndependencies: []\nproject: {project}\n---\n\n## Description\n\ntext\n"
        )
        self.board.put(text)

    def jobs(self):
        return [r[0] for r in self.board.cab.store._conn.execute("SELECT id FROM jobs").fetchall()]

    def row(self, jid):
        return cabinet_jobs.row(self.board.cab.store, jid)

    def definition(self, jid):
        return self.row(jid)["data"]["definition"]

    def report(self, jid, **item):
        """What the session host reports of the job (its start or its end)."""
        cabinet_jobs.report_item(self.board.cab.ctx, {"type": "job", "job": jid, **item})

    def comments(self, task_id="KD-121"):
        return [(m["author"], m["body"]) for m in self.board.cab.store.messages(task_id) if m["kind"] == "text"]

    def main(self, kd_worker, argv, caller=BOSS):
        out, err = io.StringIO(), io.StringIO()
        code = kd_worker.main(
            argv,
            caller=caller,
            config_path=CONFIG,
            registry_path=str(self.registry),
            enabled_path=str(self.enabled),
            repos_path=str(self.repos),
            client=self.client,
            manifests_root=str(self.manifests),
            require_root_owner=False,
            results_root=str(self.results),
            out=out,
            err=err,
        )
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.card()
    return e


def request(card="KD-121", spec="implementer", skills=(SKILL,), brief="Fix the parser."):
    argv = ["request", "--card", card, "--spec", spec]
    for skill in skills:
        argv += ["--skill", skill]
    return argv + ["--brief", brief]


def job_id(out):
    assert out.startswith("ok job "), out
    return out.split()[2]


def test_request_status_and_result(kd_worker, env):
    code, out, err = env.main(kd_worker, request())
    assert code == 0, err
    jid = job_id(out)
    assert wj.JOB_ID.fullmatch(jid)
    assert out == f"ok job {jid} card KD-121 agent @dev spec implementer state queued\n"
    code, out, _ = env.main(kd_worker, ["status", "--job", jid])
    assert code == 0 and out.startswith(
        f"{jid} queued card KD-121 agent @dev spec implementer skills {SKILL} write repo slot -"
    )
    code, out, _ = env.main(kd_worker, ["result", "--job", jid])
    assert code == 0 and "result: none" in out


def test_request_submits_to_the_cabinet(kd_worker, env):
    jid = job_id(env.main(kd_worker, request())[1])
    (sent,) = env.client.sent("job.submit")
    config = worker_slots.load_config(CONFIG)
    assert sent["limits"] == {"jobs_per_day": config.jobs_per_day,
                              "attempts_per_card_spec": config.attempts_per_card_spec}
    job = sent["job"]
    prompt = worker_packs.build_prompt(REG, "implementer", (SKILL,), card="KD-121", brief="Fix the parser.",
                                       agent_type="developer")
    assert job["id"] == jid and job["prompt"] == json.loads(json.dumps(prompt.provenance()))
    assert (job["agent"], job["type"], job["repo"], job["writer"]) == ("dev", "developer", "digipaddock", True)
    assert (job["caller"], job["requester"]) == ("digi-boss", "boss")
    assert (job["timeout"], job["model"]) == (config.timeout_minutes * 60, config.model)
    assert env.definition(jid) == {**job, "effort": ""}  # the stored definition carries the card's effort
    assert "systemctl" not in (REPO / "common/bin/kd-worker").read_text("utf-8")


def test_a_ui_design_request_with_skill_frontend_carries_its_sha256_into_the_result(kd_worker, env):
    """the job and its result record name the frontend text it ran with."""
    code, out, err = env.main(kd_worker, request(spec="ui-design", skills=("frontend",), brief="Design the Inbox."))
    assert code == 0, err
    jid = job_id(out)
    frontend = {"version": REG.skills["frontend"].version, "sha256": REG.skills["frontend"].sha256}
    job = env.definition(jid)
    assert (job["spec"], job["skills"], job["writer"]) == ("ui-design", ["frontend"], True)
    assert job["prompt"]["skills"] == {"frontend": frontend}
    rec = _record(env, jid)
    assert rec["skills"] == ["frontend"] and rec["provenance"]["skills"] == {"frontend": frontend}
    assert rec["provenance"]["sha256"] == REG.specs["ui-design"].sha256


@pytest.mark.parametrize(
    "spec, skills, reason",
    [
        ("implementer", ("frontend", "python-backend", "security-review"), "at most 2 skills"),
        ("ui-design", ("python-backend",), "skill python-backend is not compatible with ui-design"),
        ("ui-design", ("frontend", "security-review"), "skill security-review is not compatible with ui-design"),
        ("ui-design", ("no-such",), "unknown skill no-such"),
    ],
)
def test_three_skills_or_a_skill_foreign_to_the_spec_are_refused_before_any_run(kd_worker, env, spec, skills, reason):
    """refused by kd-worker itself — nothing reaches the cabinet, no job exists."""
    code, out, err = env.main(kd_worker, request(spec=spec, skills=skills))
    assert code == wj.EXIT_REFUSED and reason in err and out == ""
    assert env.client.sent("job.submit") == [] and env.jobs() == []


def test_a_root_request_names_no_requester(kd_worker, env):
    jid = job_id(env.main(kd_worker, request(), caller=None)[1])
    assert (env.definition(jid)["caller"], env.definition(jid)["requester"]) == ("root", "")


@pytest.mark.parametrize("kind, code", [("conflict", 3), ("rate", 5), ("invalid", 2), ("unavailable", 1)])
def test_conflict_and_rate_map_to_exit_3_and_5(kd_worker, env, kind, code):
    env.client = Refusing(env.board, "job.submit", kind)
    got, _, err = env.main(kd_worker, request())
    assert got == code and f"{kind}: refused" in err
    assert env.jobs() == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--model", "opus"],
        ["--settings", "/tmp/x.json"],
        ["--prompt", "be root"],
        ["--system-prompt", "x"],
        ["--path", "/"],
        ["--cwd", "/"],
        ["--repo", "digipaddock"],
        ["--role", "implementer"],
        ["--agent", "dev"],
        ["--limit", "9"],
        ["--timeout", "1"],
        ["--max-turns", "99"],
        ["--dangerously-skip-permissions"],
        ["--mod", "opus"],
        ["extra"],
    ],
)
def test_no_option_takes_a_model_path_prompt_settings_agent_or_limit(kd_worker, env, extra):
    code, _, err = env.main(kd_worker, request() + extra)
    assert code == wj.EXIT_REFUSED and "usage:" in err
    assert env.jobs() == []


@pytest.mark.parametrize(
    "argv",
    [[], ["start", "--job", "0" * 32], ["run", "--job", "0" * 32], ["list"], ["status"], ["reconcile", "--all"]],
)
def test_unknown_operations_are_refused(kd_worker, env, argv):
    assert env.main(kd_worker, argv)[0] == wj.EXIT_REFUSED


def test_a_brief_is_data_never_an_option(kd_worker, env):
    # one option-shaped token is "expected one argument"; a sentence with options inside is data
    assert env.main(kd_worker, request(brief="--model"))[0] == wj.EXIT_REFUSED
    code, out, _ = env.main(kd_worker, request(brief="use --model opus and $(id)"))
    assert code == 0
    assert env.definition(job_id(out))["brief"] == "use --model opus and $(id)"


def test_only_the_coordinator_by_its_own_user_and_root_may_call(kd_worker, env):
    for caller in (("dev", "digi-dev"), ("boss", "digi-dev"), ("boss", "digi"), ("boss", ""), ("analyst", "digi-analyst")):
        code, _, err = env.main(kd_worker, request(), caller=caller)
        assert code == wj.EXIT_REFUSED and "only the coordinator" in err, caller
    assert env.main(kd_worker, request(), caller=None)[0] == 0


def test_an_unregistered_coordinator_authorises_nobody(kd_worker, env):
    env.registry.write_text(json.dumps({"dev": AGENTS["dev"]}), encoding="utf-8")
    for caller in (("boss", "digi"), ("boss", "digi-boss")):
        assert env.main(kd_worker, request(), caller=caller)[0] == wj.EXIT_REFUSED


def test_the_entry_scrubs_the_environment():
    # the same PATH as kd-work and kd-nudge
    tail = (REPO / "common/bin/kd-worker").read_text("utf-8").split('if __name__ == "__main__":', 1)[1]
    assert "os.environ.clear()" in tail
    assert 'PATH="/usr/local/bin:/usr/bin:/bin"' in tail


def test_reexec_goes_through_the_base_channel(kd_worker):
    # The agent's self channel runs the wrapper as the agent (sandbox exit only); like kd-nudge
    # and kd-task it then hops to the base channel, whose service is the base user (stage: the
    # BOSS call reached kd-worker as digi-boss and was refused «runs as digi»).
    assert kd_worker.reexec_argv(["status", "--job", "a" * 32]) == [
        "/opt/digipaddock/common/bin/kd-cap", "run", "base", "/opt/digipaddock/common/bin/kd-worker",
        "status", "--job", "a" * 32,
    ]
    tail = (REPO / "common/bin/kd-worker").read_text("utf-8").split('if __name__ == "__main__":', 1)[1]
    assert "os.execv(cap_client.CLIENT, reexec_argv(sys.argv[1:]))" in tail


def test_disabled_slots_start_nothing(kd_worker, env):
    # The agent's login is the root runner's preflight (kd.worker_runner): the base-user executor
    # cannot read the agent's home and does not try.
    env.enabled.write_text("", encoding="utf-8")
    code, _, err = env.main(kd_worker, request())
    assert code == wj.EXIT_REFUSED and "no enabled worker slots for dev" in err
    env.enabled.write_text("analyst/worker1\n", encoding="utf-8")
    assert env.main(kd_worker, request())[0] == wj.EXIT_REFUSED
    assert env.jobs() == []
    assert "login" not in (REPO / "common/bin/kd-worker").read_text("utf-8").split("def request", 1)[1].split("def ", 1)[0]


@pytest.mark.parametrize(
    "card, argv",
    [
        ({"status": "Inbox"}, request()),
        ({"assignee": "@sysadmin"}, request()),
        ({"assignee": "@analyst"}, request(spec="task-brief", skills=())),  # registered agent, no slots
        ({"project": "admin"}, request()),
        ({}, request(card="KD-999")),
        ({}, request(spec="task-brief", skills=())),
        ({}, request(spec="admin", skills=())),
        ({}, request(skills=(SKILL, SKILL))),
    ],
)
def test_card_spec_and_skills_are_strict(kd_worker, env, card, argv):
    env.card(**card)
    assert env.main(kd_worker, argv)[0] == wj.EXIT_REFUSED
    assert env.jobs() == []


def test_the_agents_type_comes_from_its_manifest(kd_worker, env):
    (env.manifests / "dev" / "agent.json").write_text("{", encoding="utf-8")
    code, _, err = env.main(kd_worker, request())
    assert code == wj.EXIT_ERROR and "manifest" in err


def test_a_second_writer_on_the_card_is_a_conflict(kd_worker, env):
    assert env.main(kd_worker, request())[0] == 0
    code, _, err = env.main(kd_worker, request())
    assert code == wj.EXIT_CONFLICT and "active writer" in err
    assert env.main(kd_worker, request(spec="reviewer", skills=()))[0] == 0


def test_another_callers_job_is_an_unknown_job(kd_worker, env):
    jid = job_id(env.main(kd_worker, request(), caller=None)[1])
    for op in (["status", "--job", jid], ["result", "--job", jid], ["cancel", "--job", jid, "--reason", "x"]):
        code, _, err = env.main(kd_worker, op)
        assert code == wj.EXIT_REFUSED and "unknown job" in err
    assert env.row(jid)["state"] == "queued"


def _record(env, jid, status="done"):
    """The runner's result record of the job, as it leaves it in the results folder."""
    block = json.dumps({"status": status, "commit": "abc", "tests": "t", "notes": "n"})
    event = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.0123,
             "result": f"```json\n{block}\n```", "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 1}}}
    rec = worker_result.build({**env.definition(jid), "slot": "dev/worker1"},
                              {"base": "b" * 40, "branch": "worker/" + jid, "slot": "dev/worker1", "started": 1.0,
                               "ended": 10.0},
                              {"exit": 0, "timed_out": False, "result_event": event, "output_bytes": 5},
                              head="c" * 40, stat="x | 1 +", patch=None)
    (env.results / f"{jid}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_status_and_result_read_the_cabinet(kd_worker, env):
    jid = job_id(env.main(kd_worker, request())[1])
    env.report(jid, state="starting", slot="dev/worker2")
    code, out, _ = env.main(kd_worker, ["status", "--job", jid])
    assert code == 0 and out.startswith(f"{jid} running card KD-121 agent @dev spec implementer") and " slot dev/worker2 " in out
    rec = _record(env, jid)
    env.report(jid, state="done", reason="", comment=worker_result.comment(rec), usage=None, result=rec["result"])
    code, out, _ = env.main(kd_worker, ["result", "--job", jid])
    assert code == 0 and f"{jid} done " in out
    assert "summary: done" in out and '"commit": "abc"' in out
    ((author, body),) = env.comments()
    assert author == "agent:dev" and "Рабочая сессия implementer" in body


def test_result_reads_a_record_of_before_the_auth_mark(kd_worker, env):
    """a record written before the auth_failed field reads as before, with its old reason."""
    jid = job_id(env.main(kd_worker, request())[1])
    env.report(jid, state="starting", slot="dev/worker1")
    rec = _record(env, jid)
    old = {k: v for k, v in rec.items() if k != "auth_failed"}
    old.update(is_error=True, subtype="success", result=None)
    (env.results / f"{jid}.json").write_text(json.dumps(old), encoding="utf-8")
    env.report(jid, state="failed", reason="claude reported success", comment="", usage=None, result=None)
    code, out, _ = env.main(kd_worker, ["result", "--job", jid])
    assert code == 0 and "summary: failed " in out and "claude reported success" in out and "auth_failed" not in out


def test_cancel_asks_the_cabinet(kd_worker, env):
    queued = job_id(env.main(kd_worker, request())[1])
    code, out, _ = env.main(kd_worker, ["cancel", "--job", queued, "--reason", "not needed"])
    assert (code, out) == (0, f"ok {queued} cancelled\n")
    assert env.main(kd_worker, ["cancel", "--job", queued, "--reason", "again"])[1] == f"ok {queued} already cancelled\n"
    out = env.main(kd_worker, ["status", "--job", queued])[1]
    assert f"{queued} cancelled " in out and "reason: not needed" in out
    # a job the host runs is asked to stop: cancelling until the host reports its end
    running = job_id(env.main(kd_worker, request())[1])
    env.report(running, state="starting", slot="dev/worker1")
    code, out, _ = env.main(kd_worker, ["cancel", "--job", running, "--reason", "stop"])
    assert code == 0 and out == f"ok {running} cancelling: the session host stops it; status shows its end\n"
    assert env.row(running)["state"] == "cancelling"
    env.report(running, state="cancelled", reason="stopped", comment="", usage=None, result=None)
    assert f"{running} cancelled " in env.main(kd_worker, ["status", "--job", running])[1]
    assert [r["caller"] for r in env.client.sent("job.cancel")] == ["digi-boss"] * 3


def test_cancel_of_a_finished_job_is_refused_and_its_result_kept(kd_worker, env):
    jid = job_id(env.main(kd_worker, request())[1])
    env.report(jid, state="starting", slot="dev/worker1")
    env.report(jid, state="done", reason="", comment="", usage=None, result={"status": "done", "summary": "kept"})
    code, _, err = env.main(kd_worker, ["cancel", "--job", jid, "--reason", "x"])
    assert code == wj.EXIT_REFUSED and "already done" in err
    assert '"summary": "kept"' in env.main(kd_worker, ["result", "--job", jid])[1]


@pytest.mark.parametrize("argv", [["finish", "--job", "0" * 32], ["reconcile"]])
@pytest.mark.parametrize("caller", [BOSS, None])
def test_finish_and_reconcile_are_gone(kd_worker, env, argv, caller):
    code, _, err = env.main(kd_worker, argv, caller=caller)
    assert code == wj.EXIT_REFUSED and "usage:" in err


def test_the_executor_names_no_user_literal_and_no_sudo():
    text = (REPO / "common/bin/kd-worker").read_text("utf-8")
    assert "digi-" not in text and "/home/" not in text and "sudo" not in text


def test_the_executor_runs_as_the_base_user_not_root():
    text = (REPO / "common/bin/kd-worker").read_text(encoding="utf-8")
    assert "naming.base_user()" in text and "geteuid() != 0" not in text
