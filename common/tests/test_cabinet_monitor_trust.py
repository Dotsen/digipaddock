"""The monitor's accept, baseline and trust only by a monitor_trust decision the owner signs."""

import sqlite3
import sys
from pathlib import Path

import pytest

from kd.cabinet import api, decisions, schema, store

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

AT = "2026-10-14T13:00:00+03:00"
SIGNATURE = {"id": "cred", "response": {}}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    e.users.create("partner", "Партнёр", roles=[("partner", "*")], actor="root")
    return e


def user(env, name, request):
    return api.dispatch(env.ctx, api.Principal("user", -1, name), request)


def push(env, *items):
    return env.call("digi-sysadmin", {"op": "monitor.push", "items": list(items)})["results"]


def opened(env, key="node-c:logins:ubuntu@key SHA256:abc", n_key="o1"):
    server, check, subject = key.split(":", 2)
    problem = {"key": key, "server": server, "check": check, "subject": subject, "level": "crit", "max_level": "crit",
               "value": "198.51.100.2", "text": "вход", "opened_at": AT, "repeats": 0}
    return push(env, {"key": n_key, "kind": "open", "problem": problem})[0]["incident"]


def servers(env, *aliases):
    push(env, {"key": "sync-" + "-".join(aliases), "kind": "sync", "at": AT,
               "servers": [{"alias": a, "host": "", "unreachable_count": 0} for a in aliases]})


def propose(env, **request):
    return env.call("digi-sysadmin", {"op": "monitor_trust.propose", **request})


def approve(env, d, signature=SIGNATURE, revisions=None):
    revisions = revisions if revisions is not None else {
        decisions.basis_key(d): api.decision_basis(env.ctx, d)}
    return env.call("digi-cabinet", {"op": "decision.approve", "ids": [d["id"]], "user": "owner",
                                    "signature": signature, "credential": 1, "revisions": revisions})


def kinds(env, n):
    return [e["kind"] for e in env.call("digi-cabinet", {"op": "incident.get", "incident": n})["timeline"]]


# --- the migration ------------------------------------------------------------------------------------------
def test_a_v26_base_gets_the_apply_columns_and_the_removals(tmp_path):
    path = str(tmp_path / "v26.db")
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript(schema.DDL + "".join(schema.MIGRATIONS[v] for v in range(2, 27)))
    conn.execute("INSERT INTO meta VALUES ('schema_version', '26')")
    conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
    conn.close()
    db = store.Store.open(path)
    assert db.schema_version() == schema.SCHEMA_VERSION
    columns = {r[1] for r in db._conn.execute("PRAGMA table_info(decisions)")}
    assert {"apply_state", "apply_detail", "applied_at"} <= columns
    assert db._conn.execute("SELECT COUNT(*) FROM trust_removals").fetchone()[0] == 0


# --- AC1: the sysadmin only proposes ---------------------------------------------------------------------------
def test_the_sysadmins_accept_opens_a_decision_without_a_card_in_its_monitor_thread(env):
    n = opened(env)
    out = propose(env, kind="accept", incident=f"P-{n}", text="ключ владельца с ноутбука")
    d = out["decision"]
    assert out["repeat"] is False and d["type"] == "monitor_trust" and d["card"] == "" and d["state"] == "open"
    assert d["step_up"] is True and d["requested_by"] == "agent:sysadmin"
    assert d["payload"] == {"kind": "accept", "incident": n, "server": "node-c", "check": "logins",
                            "subject": "ubuntu@key SHA256:abc", "value": "198.51.100.2", "proposed_by": "agent:sysadmin",
                            "text": "ключ владельца с ноутбука"}
    assert d["thread"] == env.store.agent_thread("sysadmin", "monitor")
    assert "monitor_trust.proposed" in kinds(env, n)


def test_the_same_proposal_again_answers_the_same_decision(env):
    n = opened(env)
    first = propose(env, kind="accept", incident=n)["decision"]
    again = propose(env, kind="accept", incident=f"P-{n}", text="ещё раз")
    assert again["repeat"] is True and again["decision"]["id"] == first["id"]
    # one open accept per P-n whatever its label: two signed would close it twice
    owners = user(env, "owner", {"op": "monitor_trust.propose", "kind": "accept", "incident": n, "label": "ноутбук"})
    assert owners["repeat"] is True and owners["decision"]["id"] == first["id"]
    servers(env, "node-c")
    b = propose(env, kind="baseline", server="node-c")["decision"]
    assert propose(env, kind="baseline", server="node-c")["decision"]["id"] == b["id"]
    t = propose(env, kind="trust", cidr="10.1.2.3/32", label="офис")["decision"]
    assert propose(env, kind="trust", cidr="10.1.2.3", label="офис")["decision"]["id"] == t["id"]
    assert t["payload"]["cidr"] == "10.1.2.3"
    assert propose(env, kind="trust", cidr="10.1.2.3", label="дом")["decision"]["id"] != t["id"]


def test_accept_of_a_closed_incident_or_a_check_without_a_baseline_is_refused(env):
    disk = opened(env, key="node-c:disk:/", n_key="d")
    with pytest.raises(api.ApiError) as exc:
        propose(env, kind="accept", incident=disk)
    assert exc.value.kind == "refused" and "ack" in str(exc.value)
    gap = opened(env, key="node-c:logins:gap", n_key="g")
    with pytest.raises(api.ApiError) as exc:
        propose(env, kind="accept", incident=gap)
    assert exc.value.kind == "refused"
    n = opened(env)
    push(env, {"key": "r", "kind": "resolve", "problem_key": "node-c:logins:ubuntu@key SHA256:abc", "opened_at": AT,
               "at": AT})
    with pytest.raises(api.ApiError) as exc:
        propose(env, kind="accept", incident=n)
    assert exc.value.kind == "refused" and "закрыта" in str(exc.value)


def test_a_wide_subnet_an_unknown_server_and_a_trust_without_label_are_invalid(env):
    for request in ({"kind": "trust", "cidr": "10.0.0.0/8", "label": "x"}, {"kind": "trust", "cidr": "nope",
                    "label": "x"}, {"kind": "trust", "cidr": "10.0.0.1", "label": " "},
                    {"kind": "baseline", "server": "ghost"}, {"kind": "who"}):
        with pytest.raises(api.ApiError) as exc:
            propose(env, **request)
        assert exc.value.kind == "invalid"


def test_another_agent_and_a_partner_may_not_propose(env):
    with pytest.raises(api.ApiError):
        env.call("digi-dev", {"op": "monitor_trust.propose", "kind": "baseline", "server": "all"})
    with pytest.raises(api.ApiError) as exc:
        user(env, "partner", {"op": "monitor_trust.propose", "kind": "baseline", "server": "all"})
    assert exc.value.kind == "refused"


# --- AC2: the owner's button and the signature ------------------------------------------------------------------
def test_the_owner_proposes_and_signs_it_once_with_the_incident_state_in_the_signature(env):
    n = opened(env)
    d = user(env, "owner", {"op": "monitor_trust.propose", "kind": "accept", "incident": n, "label": "ноутбук"})[
        "decision"]
    assert d["requested_by"] == "user:owner" and d["payload"]["label"] == "ноутбук"
    basis = api.decision_basis(env.ctx, d)
    assert basis.startswith("open|")
    assert decisions.Decisions(env.store).challenge([d["id"]], user="owner", expires="x",
                                                    revisions={f"decision:{d['id']}": basis})
    out = approve(env, d)["decisions"][0]
    assert out["state"] == "approved" and out["apply_state"] == "pending"
    assert "monitor_trust.signed" in kinds(env, n)


def test_no_signature_or_a_changed_incident_has_no_effect(env):
    n = opened(env)
    d = propose(env, kind="accept", incident=n)["decision"]
    with pytest.raises(api.ApiError) as exc:
        approve(env, d, signature=None)
    assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        approve(env, d, revisions={f"decision:{d['id']}": "open|2026-01-01T00:00:00Z"})
    assert exc.value.kind == "conflict"
    assert env.call("digi-sysadmin", {"op": "monitor_trust.take"})["decisions"] == []
    assert decisions.Decisions(env.store).get(d["id"])["state"] == "open"


def test_a_rejected_proposal_is_in_the_incidents_timeline_and_nothing_is_applied(env):
    n = opened(env)
    d = propose(env, kind="accept", incident=n)["decision"]
    out = env.call("digi-cabinet", {"op": "decision.reject", "id": d["id"], "user": "owner", "text": "это не мой ключ"})
    assert out["decision"]["state"] == "rejected" and out["decision"]["apply_state"] == ""
    assert kinds(env, n)[-1] == "monitor_trust.rejected"
    assert env.call("digi-sysadmin", {"op": "monitor_trust.take"})["decisions"] == []


def test_a_partner_neither_sees_nor_signs_the_monitors_trust(env):
    servers(env, "node-c")
    d = propose(env, kind="baseline", server="node-c")["decision"]
    assert user(env, "partner", {"op": "decision.list", "state": "open"})["decisions"] == []
    with pytest.raises(api.ApiError):
        env.call("digi-cabinet", {"op": "decision.approve", "ids": [d["id"]], "user": "partner",
                                 "signature": SIGNATURE, "revisions": {f"decision:{d['id']}": ""}})
    assert [x["id"] for x in user(env, "owner", {"op": "decision.list", "state": "open"})["decisions"]] == [d["id"]]


# --- AC3, AC4: the monitor applies, once ----------------------------------------------------------------------
def test_the_monitor_takes_signed_decisions_and_reports_each_once(env):
    servers(env, "node-c")
    t = propose(env, kind="trust", cidr="10.1.2.3", label="офис")["decision"]
    b = propose(env, kind="baseline", server="node-c")["decision"]
    approve(env, t)
    approve(env, b)
    taken = env.call("digi-sysadmin", {"op": "monitor_trust.take", "kinds": ["accept", "trust"]})
    assert [d["id"] for d in taken["decisions"]] == [t["id"]]
    assert [d["id"] for d in env.call("digi-sysadmin", {"op": "monitor_trust.take", "kinds": ["baseline"]})[
        "decisions"]] == [b["id"]]
    first = env.call("digi-sysadmin", {"op": "monitor_trust.report", "id": t["id"], "ok": True})
    assert first["repeat"] is False and first["decision"]["apply_state"] == "applied"
    again = env.call("digi-sysadmin", {"op": "monitor_trust.report", "id": t["id"], "ok": False, "detail": "x"})
    assert again["repeat"] is True and again["decision"]["apply_state"] == "applied"
    assert env.call("digi-sysadmin", {"op": "monitor_trust.take", "kinds": ["trust"]})["decisions"] == []
    failed = env.call("digi-sysadmin", {"op": "monitor_trust.report", "id": b["id"], "ok": False,
                                       "detail": "сервер недоступен: таймаут"})["decision"]
    assert failed["apply_state"] == "error" and failed["apply_detail"] == "сервер недоступен: таймаут"
    events = [e["kind"] for e in env.store.events_since(0, 500)]
    assert "monitor_trust.applied" in events and "monitor_trust.failed" in events
    thread = env.store.agent_thread("sysadmin", "monitor")
    keys = [m["payload"].get("key") for m in env.store.thread_messages(thread)]
    assert "sys.monitor_trust.applied" in keys and "sys.monitor_trust.failed" in keys


def test_only_the_monitor_takes_and_reports(env):
    with pytest.raises(api.ApiError):
        env.call("digi-dev", {"op": "monitor_trust.take"})
    with pytest.raises(api.ApiError):
        user(env, "owner", {"op": "monitor_trust.report", "id": "x" * 32, "ok": True})


def test_an_incident_closed_before_the_signature_expires_its_accept(env):
    n = opened(env)
    d = propose(env, kind="accept", incident=n)["decision"]
    push(env, {"key": "r", "kind": "resolve", "problem_key": "node-c:logins:ubuntu@key SHA256:abc", "opened_at": AT,
               "at": AT})
    assert decisions.Decisions(env.store).get(d["id"])["state"] == "expired"
    assert kinds(env, n)[-1] == "monitor_trust.expired"
    with pytest.raises(api.ApiError):
        approve(env, d)


# --- AC5: taking an address off ------------------------------------------------------------------------------
def test_removal_needs_no_signature_and_is_journaled(env):
    sysadmin = env.call("digi-sysadmin", {"op": "monitor_trust.remove", "cidr": "10.1.2.3/32"})
    assert sysadmin["removal"]["state"] == "queued" and sysadmin["removal"]["item"] == "10.1.2.3"
    assert env.call("digi-sysadmin", {"op": "monitor_trust.remove", "cidr": "10.1.2.3"})["repeat"] is True
    owner = user(env, "owner", {"op": "monitor_trust.remove", "cidr": "10.9.9.0/24"})["removal"]
    taken = env.call("digi-sysadmin", {"op": "monitor_trust.take"})["removals"]
    assert [r["cidr"] for r in taken] == ["10.1.2.3", "10.9.9.0/24"]
    env.call("digi-sysadmin", {"op": "monitor_trust.report", "removal": sysadmin["removal"]["id"], "ok": True})
    env.call("digi-sysadmin", {"op": "monitor_trust.report", "removal": owner["id"], "ok": False,
                              "detail": "не в списке доверенных"})
    assert env.call("digi-sysadmin", {"op": "monitor_trust.take"})["removals"] == []
    events = [(e["kind"], e["actor"]) for e in env.store.events_since(0, 500) if e["kind"].startswith("monitor_trust")]
    assert ("monitor_trust.removal_requested", "agent:sysadmin") in events
    assert ("monitor_trust.removal_requested", "user:owner") in events
    assert ("monitor_trust.removed", "system:monitor") in events
    assert ("monitor_trust.removal_failed", "system:monitor") in events
    with pytest.raises(api.ApiError):
        user(env, "partner", {"op": "monitor_trust.remove", "cidr": "10.1.2.3"})


# --- AC6, AC7: what the pages show ------------------------------------------------------------------------------
def test_the_incident_and_the_server_pages_show_their_proposals_and_the_baseline_time(env):
    n = opened(env)
    d = propose(env, kind="accept", incident=n)["decision"]
    page = env.call("digi-cabinet", {"op": "incident.get", "incident": n})
    assert [x["id"] for x in page["trust"]] == [d["id"]] and page["acceptable"] is True
    push(env, {"key": "s2", "kind": "sync", "at": AT, "servers": [{"alias": "node-c", "host": "", "unreachable_count": 0}],
               "trust": {"trusted": {"10.1.2.3": "офис"}, "servers": {"node-c": {
                   "identities": {"ubuntu@key SHA256:abc": "ноутбук"}, "ports": {}, "wgpeers": [],
                   "taken_at": "2026-10-13T12:00:00+03:00"}}}})
    b = propose(env, kind="baseline", server="node-c")["decision"]
    env.call("digi-sysadmin", {"op": "monitor_trust.remove", "cidr": "10.1.2.3"})
    server = env.call("digi-cabinet", {"op": "server.get", "alias": "node-c"})
    assert server["trust"]["taken_at"] == "2026-10-13T09:00:00Z"
    assert server["trust"]["identities"] == [{"item": "ubuntu@key SHA256:abc", "label": "ноутбук"}]
    assert [x["id"] for x in server["proposals"]["decisions"]] == [b["id"]]
    assert server["proposals"]["removals"][0]["item"] == "10.1.2.3"


def test_the_sysadmins_proposal_is_pushed_to_who_decides(env):
    from kd.cabinet import notify

    n = notify.Notifier(env.store, env.users, None)
    n.catch_up()  # the first call sets the cursor
    propose(env, kind="trust", cidr="10.4.4.4", label="дом", text="адрес офиса")
    user(env, "owner", {"op": "monitor_trust.propose", "kind": "trust", "cidr": "10.5.5.5", "label": "дача"})
    n.catch_up()
    rows = env.store._conn.execute("SELECT user, title, body FROM notifications").fetchall()
    assert [(r["user"], r["body"]) for r in rows] == [("owner", "адрес офиса")]  # the owner's own: no push
    assert rows[0]["title"] == "Сисадмин предлагает доверие монитора: доверенный адрес 10.4.4.4 («дом»)"


# --- many accepts as one bundle ------------------------------------------------------------------------
def units_of(env, *aliases):
    out = []
    for a in aliases:
        problem = {"key": f"{a}:sec:units:x.service", "server": a, "check": "sec:units", "subject": "x.service",
                   "level": "warn", "max_level": "warn", "value": "h1", "text": "юнит", "opened_at": AT, "repeats": 0}
        out.append(push(env, {"key": f"u-{a}", "kind": "open", "problem": problem})[0]["incident"])
    return out


def propose_many(env, incidents_, user="digi-sysadmin"):
    return env.call(user, {"op": "monitor_trust.propose_many", "incidents": incidents_})


def test_many_accepts_are_one_bundle(env):
    ns = units_of(env, "node-a", "node-b", "node-c")
    got = propose_many(env, [f"P-{n}" for n in ns])
    assert got["refused"] == [] and [d["payload"]["incident"] for d in got["decisions"]] == ns
    bundles = {d["payload"]["bundle"] for d in got["decisions"]}
    assert len(bundles) == 1 and len(bundles.pop()) == 16
    again = propose_many(env, ns)
    assert [d["id"] for d in again["decisions"]] == [d["id"] for d in got["decisions"]]


def test_a_closed_or_unacceptable_incident_is_refused_the_rest_proposed(env):
    ns = units_of(env, "node-a", "node-b")
    mem = opened(env, key="node-c:mem:ram", n_key="m")
    push(env, {"key": "r", "kind": "resolve", "problem_key": "node-b:sec:units:x.service", "how": "close", "at": AT})
    got = propose_many(env, ns + [mem, "P-999"])
    assert [d["payload"]["incident"] for d in got["decisions"]] == [ns[0]]
    assert [r["incident"] for r in got["refused"]] == [f"P-{ns[1]}", f"P-{mem}", "P-999"]
    assert "bundle" not in got["decisions"][0]["payload"] or got["decisions"][0]["payload"]["bundle"]


def test_a_bundle_is_signed_once(env):
    got = propose_many(env, units_of(env, "node-a", "node-b", "node-c"))["decisions"]
    revisions = {decisions.basis_key(d): api.decision_basis(env.ctx, d) for d in got}
    out = env.call("digi-cabinet", {"op": "decision.approve", "ids": [d["id"] for d in got], "user": "owner",
                                    "signature": SIGNATURE, "credential": 1, "revisions": revisions})["decisions"]
    assert [(d["id"], d["state"], d["apply_state"]) for d in out] == [(d["id"], "approved", "pending") for d in got]


def test_a_bundle_is_one_line_of_the_inbox_badge(env):
    """Stage acceptance: the inbox shows a bundle as one row, so its badge counts it once."""
    owner = api.Principal("user", 0, "owner")
    base = api.inbox_count(env.ctx, owner)
    propose_many(env, units_of(env, "node-a", "node-b", "node-c"))
    assert api.inbox_count(env.ctx, owner) == base + 1


def test_an_accept_whose_object_moved_on_before_the_signature_expires(env):
    """Stage acceptance: a unit's value moved on (its link glued in) while the bundle waited for the owner — the
    signature must not take the old value; the owner proposes again over what is there now."""
    ns = units_of(env, "node-a", "node-b")
    got = {d["payload"]["server"]: d["id"] for d in propose_many(env, ns)["decisions"]}
    now_value = '{"x.service":"h1","multi-user.target.wants/x.service":"symlink -> /etc/systemd/system/x.service"}'
    push(env, {"key": "t2", "kind": "sync", "at": "2026-10-14T13:05:00+03:00", "checks": [
        {"server": "node-a", "check": "sec:units", "rows": [
            {"subject": "x.service", "level": "warn", "value": now_value, "text": "юнит", "value_hash": "a" * 64}]}]})
    still = {d["id"] for d in env.call("digi-cabinet", {"op": "decision.list", "state": "open"})["decisions"]}
    assert got["node-a"] not in still and got["node-b"] in still
    again = propose_many(env, [ns[0]])["decisions"][0]
    assert again["id"] != got["node-a"] and again["payload"]["value"] == now_value


def test_an_accept_whose_object_got_worse_with_a_new_value_expires(env):
    ns = units_of(env, "node-a")
    d = propose(env, kind="accept", incident=ns[0])["decision"]
    push(env, {"key": "w", "kind": "update", "problem_key": "node-a:sec:units:x.service", "opened_at": AT,
               "change": "worse", "level": "crit", "value": "h2", "value_hash": "b" * 64, "at": AT})
    still = {x["id"] for x in env.call("digi-cabinet", {"op": "decision.list", "state": "open"})["decisions"]}
    assert d["id"] not in still


def test_more_than_50_or_none_is_invalid(env):
    for incidents_ in ([], list(range(1, 52)), "P-1"):
        with pytest.raises(api.ApiError) as bad:
            propose_many(env, incidents_)
        assert bad.value.kind == "invalid"


def test_a_partner_may_not_propose_many(env):
    ns = units_of(env, "node-a")
    with pytest.raises(api.ApiError) as refused:
        api.dispatch(env.ctx, api.Principal("user", 0, "partner"),
                     {"op": "monitor_trust.propose_many", "incidents": ns})
    assert refused.value.kind == "refused"


# --- a signed accept is not proposed again ----------------------------------------------------------
def test_a_signed_accept_waiting_for_the_monitor_is_not_proposed_again(env):
    n = opened(env)
    d = user(env, "owner", {"op": "monitor_trust.propose", "kind": "accept", "incident": n})["decision"]
    approve(env, d)
    with pytest.raises(api.ApiError) as again:
        user(env, "owner", {"op": "monitor_trust.propose", "kind": "accept", "incident": n})
    assert again.value.kind == "refused" and f"P-{n}" in str(again.value)
    got = propose_many(env, [n])
    assert got["decisions"] == [] and [r["incident"] for r in got["refused"]] == [f"P-{n}"]
    assert [x["id"] for x in decisions.Decisions(env.store)._rows("WHERE d.type='monitor_trust'", ())] == [d["id"]]


def test_an_accept_the_monitor_failed_may_be_proposed_again(env):
    n = opened(env)
    d = propose(env, kind="accept", incident=n)["decision"]
    approve(env, d)
    env.call("digi-sysadmin", {"op": "monitor_trust.report", "id": d["id"], "ok": False, "detail": "эталон повреждён"})
    again = propose(env, kind="accept", incident=n)["decision"]
    assert again["id"] != d["id"] and again["state"] == "open"


def test_the_incident_shows_its_signed_accept_in_the_list_and_on_its_page(env):
    n = opened(env)
    assert env.call("digi-cabinet", {"op": "incident.query"})["items"][0]["accepted"] is None
    d = propose(env, kind="accept", incident=n)["decision"]
    assert env.call("digi-cabinet", {"op": "incident.query"})["items"][0]["accepted"] is None  # not signed yet
    approve(env, d)
    item = env.call("digi-cabinet", {"op": "incident.query"})["items"][0]
    assert item["accepted"] == {"decision": d["id"], "apply_state": "pending", "decided_at": item["accepted"][
        "decided_at"], "kind": "accept"} and item["accepted"]["decided_at"]
    page = env.call("digi-cabinet", {"op": "incident.get", "incident": n})
    assert page["incident"]["accepted"]["decision"] == d["id"] and page["acceptable"] is False


# --- a login whose access is gone closes as "access revoked", never into the baseline -------------------
def test_revoke_opens_its_own_decision_for_a_login_by_a_key(env):
    n = opened(env)
    d = propose(env, kind="revoke", incident=f"P-{n}", text="пользователь удалён", label="ignored")["decision"]
    assert d["payload"] == {"kind": "revoke", "incident": n, "server": "node-c", "check": "logins",
                            "subject": "ubuntu@key SHA256:abc", "value": "198.51.100.2", "proposed_by": "agent:sysadmin",
                            "text": "пользователь удалён"}
    assert propose(env, kind="revoke", incident=n)["decision"]["id"] == d["id"]
    assert "monitor_trust.proposed" in kinds(env, n)
    page = env.call("digi-cabinet", {"op": "incident.get", "incident": n})
    assert page["revocable"] is True and page["acceptable"] is True


def test_revoke_only_for_a_login_by_a_key(env):
    for key, n_key in (("node-c:logins:gap", "g"), ("node-c:logins:root@password", "p"),
                       ("node-c:logins:root@key ?", "k"), ("node-c:sec:units:x.service", "u")):
        n = opened(env, key=key, n_key=n_key)
        with pytest.raises(api.ApiError) as exc:
            propose(env, kind="revoke", incident=n)
        assert exc.value.kind == "refused", key
        assert env.call("digi-cabinet", {"op": "incident.get", "incident": n})["revocable"] is False


def test_revoke_and_accept_of_one_incident_replace_each_other_until_one_is_signed(env):
    n = opened(env)
    a = propose(env, kind="accept", incident=n)["decision"]
    r = propose(env, kind="revoke", incident=n)["decision"]
    assert decisions.Decisions(env.store).get(a["id"])["state"] == "expired"
    approve(env, r)
    with pytest.raises(api.ApiError) as exc:
        propose(env, kind="accept", incident=n)
    assert exc.value.kind == "refused" and "доступ снят" in str(exc.value)
    page = env.call("digi-cabinet", {"op": "incident.get", "incident": n})
    assert page["incident"]["accepted"]["kind"] == "revoke" and page["revocable"] is False
    assert [d["id"] for d in env.call("digi-sysadmin", {"op": "monitor_trust.take", "kinds": ["revoke"]})[
        "decisions"]] == [r["id"]]
    assert env.call("digi-sysadmin", {"op": "monitor_trust.take", "kinds": ["accept", "trust"]})["decisions"] == []


def test_many_revokes_are_one_bundle_and_the_rest_refused(env):
    a = opened(env, key="node-a:logins:deploy@key SHA256:aaa", n_key="a")
    b = opened(env, key="node-b:logins:deploy@key SHA256:aaa", n_key="b")
    units = units_of(env, "node-c")
    got = env.call("digi-sysadmin", {"op": "monitor_trust.propose_many", "incidents": [a, b] + units,
                                     "kind": "revoke"})
    assert [(d["payload"]["kind"], d["payload"]["incident"]) for d in got["decisions"]] == [("revoke", a), ("revoke", b)]
    assert len({d["payload"]["bundle"] for d in got["decisions"]}) == 1
    assert [r["incident"] for r in got["refused"]] == [f"P-{units[0]}"]
    with pytest.raises(api.ApiError) as bad:
        env.call("digi-sysadmin", {"op": "monitor_trust.propose_many", "incidents": [a], "kind": "baseline"})
    assert bad.value.kind == "invalid"


def test_the_monitor_closes_a_revoked_incident_as_revoke(env):
    n = opened(env)
    push(env, {"key": "rv", "kind": "resolve", "problem_key": "node-c:logins:ubuntu@key SHA256:abc", "opened_at": AT,
               "how": "revoke", "value": "198.51.100.2", "at": AT})
    page = env.call("digi-cabinet", {"op": "incident.get", "incident": n})
    assert page["incident"]["status"] == "resolved" and page["incident"]["resolution"] == "revoke"
    resolved = [e for e in page["timeline"] if e["kind"] == "incident.resolved"]
    assert resolved and resolved[-1]["payload"]["how"] == "revoke"
