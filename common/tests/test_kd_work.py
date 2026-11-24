import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kd import activity_journal, cap_client

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cabinet_env
from cabinet_env import CabinetEnv  # noqa: E402 — the cards live in the cabinet

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _flock(portable_flock):
    pass


class Env:
    def __init__(self, tmp_path, mod):
        self.mod = mod
        self.cab = CabinetEnv(tmp_path / "cabinet")
        self.registry = tmp_path / "agents.json"
        self.registry.write_text(
            json.dumps({"dev": {"user": "digi-dev"}, "sysadmin": {"user": "digi-sysadmin"},
                        "boss": {"user": "digi-boss"}}),
            encoding="utf-8",
        )
        self.journal = tmp_path / "activity.jsonl"
        self.env = {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-dev"}

    def card(self, card_id="KD-5", status="In Progress", assignee="@dev", body="text"):
        self.cab.put(card_id, status=status, assignee=assignee, description=body)

    def run(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        rc = self.mod.main(
            list(argv),
            env=self.env if env is None else env,
            now=lambda: NOW,
            journal=str(self.journal),
            client=self.cab.client("digi"),  # the executor runs as the base user
            registry=str(self.registry),
            out=out,
            err=err,
        )
        return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def env(tmp_path, kd_work):
    return Env(tmp_path, kd_work)


def test_progress_records_the_callers_claim_first(env):
    env.card()
    rc, out, _ = env.run("progress", "KD-5")
    assert (rc, out) == (0, "ok KD-5 claim\n")
    (record,) = activity_journal.read(str(env.journal))
    assert (record["worker"], record["action"]) == ("@dev", "claim")


def test_progress_without_a_card_change_is_fine_and_records_nothing(env):
    env.card()
    env.run("progress", "KD-5")
    rc, out, _ = env.run("progress", "KD-5")
    assert rc == 0 and out.startswith("unchanged KD-5")
    assert len(activity_journal.read(str(env.journal))) == 1


def test_progress_tells_the_cabinet_only_when_recorded(env):
    """the cabinet counts an agent's defers in a row from its last verified action."""
    env.card()
    seq = env.cab.store.last_seq()
    env.run("progress", "KD-5")
    env.run("progress", "KD-5")  # unchanged: nothing new to tell
    events = [e for e in env.cab.store.events_since(seq) if e["kind"] == "turn.progress"]
    assert [(e["actor"], e["card"]) for e in events] == [("agent:dev", "KD-5")]


def test_progress_after_review_is_a_review_submission(env):
    env.card()
    env.run("progress", "KD-5")
    env.card(status="Review", body="done")
    assert env.run("progress", "KD-5")[1] == "ok KD-5 review_submission\n"


@pytest.mark.parametrize(
    "argv, caller, message",
    [
        (["progress", "KD-6"], None, "card not found"),
        (["progress", "kd-5"], None, "invalid card"),
        (["progress", "KD-5", "extra"], None, "usage"),
        (["progress"], None, "usage"),
        (["claim", "KD-5"], None, "usage"),
        (["progress", "KD-5"], {"KD_CALLER_USER": "digi-dev"}, "no capability channel"),
        (["progress", "KD-5"], {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-boss"}, "not the worker's user"),
        (["progress", "KD-5"], {"KD_AGENT": "../dev", "KD_CALLER_USER": "digi-dev"}, "no capability channel"),
    ],
)
def test_refusals(env, argv, caller, message):
    env.card()
    rc, _, err = env.run(*argv, env=caller)
    assert rc == 2 and message in err
    assert not env.journal.exists()


def test_another_workers_card_is_refused(env):
    env.card(assignee="@sysadmin")
    rc, _, err = env.run("progress", "KD-5")
    assert rc == 2 and "single assignee" in err


def test_any_other_caller_reaches_the_executor_through_its_base_channel(kd_work):
    assert kd_work.reexec_argv(["progress", "KD-5"]) == [
        cap_client.CLIENT, "run", "base", kd_work.EXECUTOR, "progress", "KD-5"
    ]


def test_the_executor_path_reaches_usr_local_bin():
    # Seen on the factory host (11.10.2026): PATH=/usr/bin:/bin hid /usr/local/bin, so a reply never woke BOSS
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "bin" / "kd-work").read_text("utf-8")
    assert 'PATH="/usr/local/bin:/usr/bin:/bin"' in source


# --- a question to the people of the cabinet ---------------------------------------------


def _ask_env(env, tmp_path):
    (tmp_path / "cabinet.json").write_text('{"schema_version": 1, "origin": "https://cab.example"}', "utf-8")
    env.cab.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.card()


def run_cab(env, tmp_path, *argv, sleep=None):
    out, err = io.StringIO(), io.StringIO()
    rc = env.mod.main(list(argv), env=env.env, now=lambda: NOW, journal=str(env.journal),
                      client=env.cab.client("digi"), registry=str(env.registry), out=out, err=err,
                      cabinet_json=str(tmp_path / "cabinet.json"), sleep=sleep or (lambda s: None))
    return rc, out.getvalue(), err.getvalue()


def test_ask_opens_a_question_and_prints_its_link(env, tmp_path):
    _ask_env(env, tmp_path)
    rc, out, _ = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Выкатывать сегодня?")
    assert rc == 0 and out.startswith("ok question ")
    decision_id = out.split()[2]
    assert f"https://cab.example/decisions/{decision_id}" in out
    d = env.cab.call("root", {"op": "decision.get", "id": decision_id})["decision"]
    assert d["type"] == "question" and d["requested_by"] == "agent:dev" and d["payload"] == {"text": "Выкатывать сегодня?"}


def test_answer_waits_and_prints_the_reply(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Ок?")[1].split()[2]
    rc, out, err = run_cab(env, tmp_path, "answer", decision_id, "--wait", "0")
    assert rc == 3 and "no answer yet" in err
    env.cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "text": "Да, после 18:00"})
    rc, out, _ = run_cab(env, tmp_path, "answer", decision_id)
    assert rc == 0 and out == "approved: Да, после 18:00\n"


def test_answer_polls_until_the_wait_ends_and_reports_a_rejection(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Ок?")[1].split()[2]
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            env.cab.call("digi-cabinet", {"op": "decision.reject", "id": decision_id, "user": "owner", "text": "Нет"})

    rc, out, _ = run_cab(env, tmp_path, "answer", decision_id, "--wait", "60", sleep=sleep)
    assert rc == 1 and out == "rejected: Нет\n" and len(naps) == 2


@pytest.mark.parametrize("argv", [
    ["ask", "KD-5"], ["ask", "KD-5", "--text", ""], ["answer", "x" * 32], ["answer", "a" * 32, "--wait", "601"],
    ["answer", "a" * 32, "--wait", "-1"], ["ask", "KD-6", "--text", "?"],
])
def test_ask_and_answer_refusals(env, tmp_path, argv):
    _ask_env(env, tmp_path)
    rc, _, err = run_cab(env, tmp_path, *argv)
    assert rc in (1, 2) and err


def test_another_agent_cannot_read_the_answer(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Ок?")[1].split()[2]
    env.env = {"KD_AGENT": "sysadmin", "KD_CALLER_USER": "digi-sysadmin"}
    rc, _, err = run_cab(env, tmp_path, "answer", decision_id, "--wait", "0")
    assert rc == 2 and "no such decision" in err


# --- a question form on stdin, the chosen ids in the answer --------------------------------
def _ask_form(env, tmp_path, data: bytes):
    out, err = io.StringIO(), io.StringIO()
    rc = env.mod.main(["ask", "KD-5", "--form", "-"], env=env.env, now=lambda: NOW, journal=str(env.journal),
                      client=env.cab.client("digi"), registry=str(env.registry), out=out, err=err,
                      cabinet_json=str(tmp_path / "cabinet.json"), stdin=io.BytesIO(data))
    return rc, out.getvalue(), err.getvalue()


def _form_bytes(form=None):
    from question_samples import form as sample

    return json.dumps(form or sample(), ensure_ascii=False).encode("utf-8")


def test_ask_reads_a_form_from_stdin(env, tmp_path):
    from kd.cabinet import questions
    from question_samples import form

    _ask_env(env, tmp_path)
    rc, out, _ = _ask_form(env, tmp_path, _form_bytes())
    assert rc == 0 and out.startswith("ok question ")
    d = env.cab.call("root", {"op": "decision.get", "id": out.split()[2]})["decision"]
    assert d["payload"] == questions.validate_form(form())


def test_ask_form_errors_are_refused_with_the_cabinets_reason(env, tmp_path):
    from question_samples import form

    _ask_env(env, tmp_path)
    bad = form()
    bad["questions"][0]["options"] = bad["questions"][0]["options"][:1]
    rc, _, err = _ask_form(env, tmp_path, _form_bytes(bad))
    assert rc == 2 and "questions[0]: 2 to 8 options" in err


@pytest.mark.parametrize(
    "data",
    [b"", b"   ", b"not json", b"[1, 2]", b"\xff\xfe", b" " * (64 * 1024 + 1)],
    ids=["empty", "blank", "not-json", "a-list", "not-utf8", "too-large"],
)
def test_ask_form_needs_json_on_stdin(env, tmp_path, data):
    _ask_env(env, tmp_path)
    rc, out, err = _ask_form(env, tmp_path, data)
    assert rc == 2 and err and not out
    assert env.cab.call("root", {"op": "decision.list"})["decisions"] == []


def test_ask_takes_the_form_as_its_argument(env, tmp_path):
    """An agent without file writes (the analyst under dontAsk) passes the form as one argument (stage probe D)."""
    from kd.cabinet import questions
    from question_samples import form

    _ask_env(env, tmp_path)
    rc, out, _ = run_cab(env, tmp_path, "ask", "KD-5", "--form", " " + _form_bytes().decode("utf-8"))
    assert rc == 0 and out.startswith("ok question ")
    d = env.cab.call("root", {"op": "decision.get", "id": out.split()[2]})["decision"]
    assert d["payload"] == questions.validate_form(form())


@pytest.mark.parametrize("value", ["form.json", "/etc/passwd", "[1, 2]", "{not json", "{" + " " * (64 * 1024)],
                         ids=["relative-path", "absolute-path", "a-list", "not-json", "too-large"])
def test_ask_form_never_reads_a_path(env, tmp_path, value):
    """It runs as the base user: an argument that is not the form's JSON object is refused, never opened."""
    _ask_env(env, tmp_path)
    rc, out, err = run_cab(env, tmp_path, "ask", "KD-5", "--form", value)
    assert rc == 2 and err and not out
    assert env.cab.call("root", {"op": "decision.list"})["decisions"] == []


def test_answer_prints_the_structure_after_the_readable_line(env, tmp_path):
    from kd.cabinet import questions

    _ask_env(env, tmp_path)
    decision_id = _ask_form(env, tmp_path, _form_bytes())[1].split()[2]
    d = env.cab.call("root", {"op": "decision.get", "id": decision_id})["decision"]
    chosen = {"method": {"selected_option_ids": ["ssh"], "custom_text": "по ключу"}}
    env.cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "answers": chosen,
                                 "comment": "Срок — неделя", "form_sha256": questions.form_sha(d["payload"])})
    rc, out, _ = run_cab(env, tmp_path, "answer", decision_id)
    lines = out.splitlines()
    assert rc == 0 and lines[0] == "approved: 1. Как проверять?: Через SSH — по ключу"
    assert lines[-2] == "Срок — неделя"
    normal = questions.validate_answer(d["payload"], chosen, "")
    assert lines[-1].startswith('form {"') and "\\u" not in lines[-1] and ", " not in lines[-1]
    assert json.loads(lines[-1][len("form "):]) == {"origin": "agent", "answers": normal, "comment": "Срок — неделя"}


def test_answer_of_a_text_question_prints_one_line(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Ок?")[1].split()[2]
    env.cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "text": "Да"})
    assert run_cab(env, tmp_path, "answer", decision_id)[1] == "approved: Да\n"


# --- the outcome of the running turn ------------------------------------------------------
class _Recorder:
    def __init__(self, reply=None, error=None):
        self.requests, self.reply, self.error = [], reply, error

    def call(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.reply or {"outcome": {"outcome": request["outcome"], "state": "pending"}, "waits": []}


def _finish(env, *argv, client):
    out, err = io.StringIO(), io.StringIO()
    rc = env.mod.main(["finish", *argv], env=env.env, now=lambda: NOW, journal=str(env.journal), client=client,
                      registry=str(env.registry), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_finish_forms(env):
    client = _Recorder()
    rc, out, _ = _finish(env, "KD-5", "--outcome", "wait_user", "--note", "waiting", "--decision", "a" * 32,
                         "--decision", "b" * 32, "--card", "KD-7", "--any", client=client)
    assert rc == 0 and out.startswith("ok finish KD-5 wait_user — applies when this turn ends")
    assert client.requests == [{"op": "turn.finish", "agent": "dev", "card": "KD-5", "outcome": "wait_user",
                                "note": "waiting", "decisions": ["a" * 32, "b" * 32], "cards": ["KD-7"], "any": True}]
    rc, _, _ = _finish(env, "KD-5", "--outcome", "continue", "--note", "x", "--agent", "boss", client=_Recorder())
    assert rc == 2
    rc, _, err = _finish(env, "KD-5", "--outcome", "continue", client=_Recorder())
    assert rc == 2 and "usage" in err


def test_finish_operation_flag(env):
    """the turn waits for a factory operation."""
    client = _Recorder()
    rc, _, _ = _finish(env, "KD-5", "--outcome", "wait_dependency", "--note", "reinstall", "--operation", "e" * 32,
                       client=client)
    assert rc == 0 and client.requests[0]["operations"] == ["e" * 32]


def test_finish_contour_flag(env):
    """the turn waits for a contour in maintenance."""
    client = _Recorder()
    rc, _, _ = _finish(env, "KD-5", "--outcome", "wait_dependency", "--note", "maintenance", "--contour", "app-ci",
                       client=client)
    assert rc == 0 and client.requests[0]["contours"] == ["app-ci"]


def test_finish_deploy_flag(env):
    """the turn waits for a deploy of the card's commit, repeatable."""
    client = _Recorder()
    rc, _, _ = _finish(env, "KD-5", "--outcome", "wait_dependency", "--note", "deploy", "--deploy", "api:staging",
                       "--deploy", "alert:test@" + "a" * 40, client=client)
    assert rc == 0 and client.requests[0]["deploys"] == ["api:staging", "alert:test@" + "a" * 40]


def test_finish_prints_the_waits_and_the_refusal(env):
    from kd.cabinet import client as cabinet_client

    reply = {"outcome": {"outcome": "wait_user", "state": "pending"},
             "waits": [{"kind": "decision", "ref": "a" * 32, "state": "pending", "blocking": 1}]}
    rc, out, _ = _finish(env, "KD-5", "--outcome", "wait_user", "--note", "w", client=_Recorder(reply))
    assert rc == 0 and f"wait decision {'a' * 32} pending\n" in out
    rc, out, _ = _finish(env, "KD-5", "--outcome", "wait_user", "--note", "w",
                         client=_Recorder({**reply, "unchanged": True}))
    assert rc == 0 and out.startswith("unchanged finish KD-5")
    error = cabinet_client.CabinetError("refused", "no turn of yours is running on KD-5")
    rc, _, err = _finish(env, "KD-5", "--outcome", "continue", "--note", "n", client=_Recorder(error=error))
    assert rc == 2 and "no turn of yours is running" in err
    error = cabinet_client.CabinetError("unavailable", "socket")
    assert _finish(env, "KD-5", "--outcome", "continue", "--note", "n", client=_Recorder(error=error))[0] == 1


def test_finish_carries_the_callers_session_from_the_channel(env):
    """The channel names the session unit the call came from; finish passes it on, so the
    cabinet takes the outcome only for that session's own turn."""
    client = _Recorder()
    env.env["KD_CALLER_SESSION"] = "0123456789ab"
    rc, _, _ = _finish(env, "KD-5", "--outcome", "continue", "--note", "tests next", client=client)
    assert rc == 0 and client.requests[-1]["session"] == "0123456789ab"


# --- a question may ask for a file; an answer may carry files -------------------------------
SHOT = {"id": "f" * 32, "name": "shot.png", "mime": "image/png", "size": 40, "sha256": "a" * 64}


def test_ask_expect_file_payload(env, tmp_path):
    _ask_env(env, tmp_path)
    rc, out, _ = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Пришлите скриншот", "--expect", "file")
    d = env.cab.call("root", {"op": "decision.get", "id": out.split()[2]})["decision"]
    assert rc == 0 and d["payload"] == {"text": "Пришлите скриншот", "expect": "file"}


def test_ask_expect_default_text_payload_unchanged(env, tmp_path):
    _ask_env(env, tmp_path)
    rc, out, _ = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Ок?", "--expect", "text")
    d = env.cab.call("root", {"op": "decision.get", "id": out.split()[2]})["decision"]
    assert rc == 0 and d["payload"] == {"text": "Ок?"}


@pytest.mark.parametrize("argv", [
    ["ask", "KD-5", "--form", "-", "--expect", "file"], ["ask", "KD-5", "--text", "?", "--expect", "audio"],
    ["ask", "KD-5", "--text", "?", "--expect"],
])
def test_ask_expect_refusals(env, tmp_path, argv):
    _ask_env(env, tmp_path)
    rc, _, err = run_cab(env, tmp_path, *argv)
    assert rc != 0 and "usage" in err


def test_answer_prints_files(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Скриншот?", "--expect", "file")[1].split()[2]
    env.cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "text": "",
                                 "files": [SHOT]})
    rc, out, _ = run_cab(env, tmp_path, "answer", decision_id)
    assert rc == 0 and out == (f"approved: -\nfile {SHOT['id']} shot.png image/png 40 — read: kd-task file get "
                               f"{SHOT['id']}\n")


def test_answer_structure_without_form_keys(env, tmp_path):
    _ask_env(env, tmp_path)
    decision_id = run_cab(env, tmp_path, "ask", "KD-5", "--text", "Как назвать?")[1].split()[2]
    env.cab.call("digi-cabinet", {"op": "decision.reply", "id": decision_id, "user": "owner", "text": "Операции",
                                 "files": [SHOT]})
    rc, out, _ = run_cab(env, tmp_path, "answer", decision_id)
    assert rc == 0 and out.splitlines()[0] == "approved: Операции" and out.splitlines()[1].startswith("file ")
    assert "form " not in out


# --- help inside an approved task ------------------------------------------------------------------
AID = "c" * 32
ROW = {"id": AID, "state": "requested", "card": "KD-5", "lead": "dev", "kind": "platform_read", "role": "sysadmin",
       "helper": "", "step": "сверить номера", "ac": 2, "reason": "", "evidence": "", "proof_limit": ""}


def _main(env, *argv, client, extra_env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = env.mod.main(list(argv), env={**env.env, **(extra_env or {})}, now=lambda: NOW, journal=str(env.journal),
                      client=client, registry=str(env.registry), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_assist_request_forms(env):
    client = _Recorder({"assist": ROW, "unchanged": False})
    rc, out, _ = _main(env, "assist", "request", "KD-5", "--kind", "platform_read", "--to", "@sysadmin", "--step",
                       "сверить номера", "--ac", "2", "--key", "ac2", client=client,
                       extra_env={"KD_CALLER_SESSION": "0123456789ab"})
    assert rc == 0 and out == f"ok assist {AID} requested (@boss assigns it)\n"
    assert client.requests == [{"op": "assist.request", "agent": "dev", "card": "KD-5", "kind": "platform_read",
                                "to": "sysadmin", "step": "сверить номера", "ac": 2, "key": "ac2",
                                "session": "0123456789ab"}]
    client = _Recorder({"assist": {**ROW, "state": "assigned"}, "unchanged": True})
    rc, out, _ = _main(env, "assist", "request", "KD-5", "--kind", "platform_read", "--to", "sysadmin", "--step", "x",
                       "--key", "ac2", client=client)
    assert rc == 0 and out == f"unchanged assist {AID} assigned\n" and client.requests[0]["ac"] == 0


@pytest.mark.parametrize("argv", [
    ["assist", "request", "KD-5", "--kind", "k", "--to", "sysadmin", "--step", "x", "--key", "bad key"],
    ["assist", "request", "KD-5", "--kind", "k", "--to", "sysadmin", "--step", "x"],
    ["assist", "request", "KD-5", "--kind", "k", "--to", "sysadmin", "--step", "x", "--key", "k", "--ac", "two"],
    ["assist", "show"],
    ["assist", "reply", AID, "--state", "done"],
    ["assist", "launch", AID],
])
def test_assist_usage_refused(env, argv):
    client = _Recorder()
    rc, _, err = _main(env, *argv, client=client)
    assert rc == 2 and client.requests == []


def test_assist_show_prints_step_and_limits(env):
    reply = {"assist": {**ROW, "state": "accepted", "helper": "sysadmin"}, "card": {"id": "KD-5"},
             "ac_text": "Перенос сверен"}
    client = _Recorder(reply)
    rc, out, _ = _main(env, "assist", "show", AID, client=client, extra_env={"KD_AGENT": "sysadmin"})
    assert rc == 0 and client.requests == [{"op": "assist.show", "agent": "sysadmin", "id": AID}]
    lines = out.splitlines()
    assert lines[0] == f"assist {AID} accepted on KD-5 for @dev"
    assert lines[1] == "kind platform_read — read only: no new rights, no writes, no server changes"
    assert "step: сверить номера" in lines and "ac #2: Перенос сверен" in lines
    assert lines[-1].startswith(f"reply: kd-work assist reply {AID} --state done")
    assert len(out.encode("utf-8")) <= 8192


def test_assist_show_names_a_server_change_as_a_change(env):
    # the one kind that is not a read says so — and that the helper's own plan and the owner's signature
    # stand between the request and anything running
    reply = {"assist": {**ROW, "kind": "server_change", "state": "accepted", "helper": "sysadmin"},
             "card": {"id": "KD-5"}}
    rc, out, _ = _main(env, "assist", "show", AID, client=_Recorder(reply), extra_env={"KD_AGENT": "sysadmin"})
    assert rc == 0
    assert out.splitlines()[1] == ("kind server_change — a change on a server: open your own admin card with the "
                                   "plan; nothing runs without the owner's signature")
    assert "read only" not in out


def test_assist_reply_forms_and_late(env):
    client = _Recorder({"assist": {**ROW, "state": "failed"}, "unchanged": False, "late": False})
    rc, out, _ = _main(env, "assist", "reply", AID, "--state", "failed", "--reason", "capability_missing",
                       "--evidence", "нет доступа", client=client, extra_env={"KD_AGENT": "sysadmin"})
    assert rc == 0 and out == f"ok assist {AID} failed\n"
    assert client.requests == [{"op": "assist.reply", "agent": "sysadmin", "id": AID, "state": "failed",
                                "reason": "capability_missing", "evidence": "нет доступа"}]
    client = _Recorder({"assist": {**ROW, "state": "cancelled"}, "unchanged": False, "late": True})
    rc, out, _ = _main(env, "assist", "reply", AID, "--state", "done", "--evidence", "e", "--limit", "l",
                       client=client)
    assert rc == 0 and out == f"late assist {AID}: kept as evidence, the request was cancelled\n"


def test_assist_refusal_from_the_cabinet_is_exit_2(env):
    from kd.cabinet import client as cabinet_client

    client = _Recorder(error=cabinet_client.CabinetError("refused", "not yours"))
    rc, _, err = _main(env, "assist", "show", AID, client=client)
    assert rc == 2 and "not yours" in err


def test_finish_assist_flag(env):
    client = _Recorder()
    rc, _, _ = _finish(env, "KD-5", "--outcome", "wait_dependency", "--note", "жду", "--assist", AID, client=client)
    assert rc == 0 and client.requests[0]["assists"] == [AID]


# --- the council of models — reading it, answering its findings, waiting for its round ------------------
CID = "c" * 32


def test_finish_council_flag(env):
    client = _Recorder()
    rc, _, _ = _finish(env, "KD-5", "--outcome", "wait_dependency", "--note", "the council reads", "--council", CID,
                       client=client)
    assert rc == 0 and client.requests[0]["councils"] == [CID]


def test_council_prints_a_page(env):
    page = {"text": "council … round 1/3 running\nend (40 bytes)\n", "next": None}
    client = _Recorder(page)
    rc, out, _ = _main(env, "council", "KD-5", client=client)
    assert rc == 0 and out == page["text"]
    assert client.requests == [{"op": "council.view", "agent": "dev", "card": "KD-5"}]
    client = _Recorder(page)
    rc, _, _ = _main(env, "council", "KD-5", "--round", "2", "--cursor", "ab12:8100", client=client)
    assert rc == 0 and client.requests == [{"op": "council.view", "agent": "dev", "card": "KD-5", "round": 2,
                                            "cursor": "ab12:8100"}]


def test_council_reply_forms(env):
    client = _Recorder({"council": CID, "answered": ["r2.f1", "r2.f2", "r2.f3"], "unanswered": ["r2.f4"]})
    rc, out, _ = _main(env, "council-reply", "KD-5", "--finding", "r2.f1=fixed", "--finding", "r2.f3=rejected",
                       "--later", "r2.f2=KD-9", "--note", "r2.f3: by design; r2.f2: a card of its own",
                       client=client, extra_env={"KD_CALLER_SESSION": "0123456789ab"})
    assert rc == 0 and out == "ok council-reply KD-5 answered 3, unanswered 1: r2.f4\n"
    assert client.requests == [{
        "op": "council.reply", "agent": "dev", "card": "KD-5",
        "answers": [{"finding": "r2.f1", "answer": "fixed"}, {"finding": "r2.f3", "answer": "rejected"},
                    {"finding": "r2.f2", "answer": "later", "card": "KD-9"}],
        "note": "r2.f3: by design; r2.f2: a card of its own", "session": "0123456789ab"}]
    client = _Recorder({"council": CID, "answered": ["r1.f1"], "unanswered": []})
    rc, out, _ = _main(env, "council-reply", "KD-5", "--finding", "r1.f1=fixed", client=client)
    assert rc == 0 and out == "ok council-reply KD-5 answered 1, unanswered 0\n"
    assert client.requests[0]["note"] == ""
    # the minor findings still without an answer are named apart — they hold nothing, yet are answered too
    client = _Recorder({"council": CID, "answered": ["r1.f1"], "unanswered": [], "minor": ["r1.f2", "r1.f5"]})
    rc, out, _ = _main(env, "council-reply", "KD-5", "--finding", "r1.f1=fixed", client=client)
    assert rc == 0 and out == (
        "ok council-reply KD-5 answered 1, unanswered 0\n"
        "minor without an answer: r1.f2, r1.f5 — answer them too (fixed, or rejected with the reason);"
        " they do not hold the handoff\n"
    )


@pytest.mark.parametrize("argv", [
    ["council"],
    ["council", "nope"],
    ["council", "KD-5", "--round", "two"],
    ["council", "KD-5", "--round"],
    ["council", "KD-5", "--cursor", "c", "--cursor", "d"],
    ["council-reply", "KD-5"],
    ["council-reply", "KD-5", "--note", "r1.f1: why"],
    ["council-reply", "KD-5", "--finding", "r1.f1"],
    ["council-reply", "KD-5", "--finding", "r1.f1=fixed", "--finding", "r1.f1=rejected"],
    ["council-reply", "KD-5", "--finding", "f1=fixed"],
    ["council-reply", "KD-5", "--later", "r1.f1=nope"],
    ["council-reply", "KD-5", "--finding", "r1.f1=fixed", "--note", "a", "--note", "b"],
])
def test_council_usage_refusals(env, argv):
    client = _Recorder()
    rc, _, err = _main(env, *argv, client=client)
    assert rc == 2 and "usage" in err and client.requests == []


def test_council_reply_passes_the_cabinets_refusal_on(env):
    from kd.cabinet import client as cabinet_client

    error = cabinet_client.CabinetError("refused", "KD-5 has no council that waits for your answers")
    rc, _, err = _main(env, "council-reply", "KD-5", "--finding", "r1.f1=fixed", client=_Recorder(error=error))
    assert rc == 2 and "waits for your answers" in err
