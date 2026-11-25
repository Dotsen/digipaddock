"""The «Deploys» page over HTTP (KD-9443): the environments' last runs and the journal, ``deploys.read`` only."""

import json

import pytest
from softauth import SoftAuthenticator, b64url, unb64url
from web_env import WebEnv

from kd import deploy_signature
from kd.cabinet import review_client

ENVS = [
    {
        "env": "staging",
        "workflow": "deploy-staging.yml",
        "branch": "main",
        "manual": False,
    },
    {"env": "prod", "workflow": "deploy-prod.yml", "branch": "main", "manual": True, "confirm": "",
     "skip_tests": True},
]
HEAD, HEAD2 = "c" * 40, "d" * 40


class FakeReview:
    """kd-review as the button sees it: the branch head, and a deploy it checks by the value it recomputes."""

    def __init__(self):
        self.keys, self.calls, self.fail, self.head = [], [], {}, HEAD

    async def call(self, request, timeout=40.0):
        self.calls.append(request)
        failure = self.fail.get(request["op"])
        if failure is not None:
            raise failure
        if request["op"] == "keys":
            return {"credential_ids": list(self.keys)}
        if request["op"] == "deploy_head":
            return {"sha": self.head, "branch": "main", "workflow": "deploy-prod.yml", "skip_tests": True}
        assert request["op"] == "deploy", request
        value = deploy_signature.challenge(request["launch"], request["payload"], request["user"],
                                           request["expires"], request["nonce"])
        client_data = json.loads(unb64url(request["assertion"]["response"]["clientDataJSON"]))
        assert unb64url(client_data["challenge"]) == value
        return {"state": "dispatched", "sha": request["payload"]["sha"], "inputs": {}}


def _run(run, env="staging", created="2026-10-14T09:00:00Z", **over):
    return {
        "run": run,
        "number": run,
        "env": env,
        "status": "success",
        "event": "push",
        "sha": "b" * 40,
        "ref": "main",
        "url": f"https://forge.example.test/o/api/actions/runs/{run}",
        "actor": "dev",
        "inputs": {},
        "title": "",
        "created": created,
        **over,
    }


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", "*")], actor="root")
    w.env.store._conn.execute(
        "INSERT INTO roles (id, perms) VALUES ('ops', '[\"deploys.read\"]')"
    )
    w.users.create("olga", "Ольга", roles=[("ops", "*")], actor="root")
    runs = [_run(n, created=f"2026-10-14T09:{n:02d}:00Z") for n in range(1, 56)]
    runs.append(
        _run(
            99,
            env="prod",
            created="2026-10-14T09:59:00Z",
            inputs={"skip_tests": "true"},
        )
    )
    w.env.call(
        "digi-forge",
        {
            "op": "deploy.report",
            "sources": [
                {
                    "project": "api",
                    "repo": "o/api",
                    "envs": ENVS,
                    "runs": runs,
                    "error": "",
                }
            ],
        },
    )
    w.review = FakeReview()
    w.svc.review = w.review
    return w


def test_the_owner_sees_each_environments_last_run(w):
    w.login("owner")
    found = w.client.get("/api/deploys").json()
    rows = {e["env"]: e for e in found["envs"]}
    assert rows["staging"]["run"]["run"] == 55 and rows["prod"]["run"]["inputs"] == {
        "skip_tests": "true"
    }
    assert (
        rows["prod"]["manual"] is True
        and rows["staging"]["stale"] is False
        and found["platform"] is None
    )


def test_the_journal_pages_fifty_runs_and_filters(w):
    w.login("owner")
    page = w.client.get("/api/deploys/runs").json()
    assert len(page["items"]) == 50 and page["items"][0]["run"] == 99 and page["next"]
    rest = w.client.get("/api/deploys/runs", params={"before": page["next"]}).json()
    assert len(rest["items"]) == 6 and rest["next"] is None
    prod = w.client.get(
        "/api/deploys/runs", params={"project": "api", "env": "prod"}
    ).json()["items"]
    assert [r["run"] for r in prod] == [99]
    assert w.client.get("/api/deploys/runs", params={"limit": 0}).status_code == 400


def test_a_reader_with_deploys_read_reads_and_a_partner_gets_403(w):
    olga = w.other_client()
    w.login("olga", client=olga)
    assert len(olga.get("/api/deploys").json()["envs"]) == 2
    anna = w.other_client()
    w.login("anna", client=anna)
    for path in ("/api/deploys", "/api/deploys/runs", "/api/deploys/alerts"):
        assert anna.get(path).status_code == 403, path


def test_failed_deploy_alerts_are_read_and_seen_over_http(w):
    w.env.store._conn.execute(
        "INSERT INTO deploy_alerts (project, run, cards, created) VALUES (?,?,?,?)",
        ("api", 99, '["KD-1"]', "2026-10-14T10:00:00Z"),
    )
    w.login("owner")
    [alert] = w.client.get("/api/deploys/alerts").json()["items"]
    assert (alert["run"], alert["env"], alert["cards"]) == (99, "prod", ["KD-1"])
    r = w.client.post("/api/deploys/alerts/seen", json={"project": "api", "run": 99}, headers=w.headers())
    assert r.status_code == 200 and r.json() == {"seen": True}
    assert w.client.get("/api/deploys/alerts").json()["items"] == []


# --- the button «Deploy to prod» (KD-9447) ---------------------------------------------------------------------
def _h(w, client):
    """The headers of a browser: the main one's, or another's own token (``client.csrf``)."""
    return w.headers(getattr(client, "csrf", None))


def _as(w, user):
    client = w.other_client()
    client.csrf = w.login(user, client=client)["csrf"]
    return client


def _signer(w, client=None):
    client = client or w.client
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = client.post("/api/me/passkeys/options", headers=_h(w, client), json={}).json()
    r = client.post("/api/me/passkeys", headers=_h(w, client),
                    json={"credential": auth.register(opts["options"]), "name": "Ноутбук"})
    assert r.status_code == 200, r.text
    w.review.keys.append(b64url(auth.credential_id))
    return auth


def _challenge(w, client=None, **over):
    client = client or w.client
    body = {"project": "api", "env": "prod", "skip_tests": False, **over}
    return client.post("/api/deploys/launch/challenge", headers=_h(w, client), json=body)


def _launch(w, auth, client=None, uv=False, **over):
    client = client or w.client
    ch = _challenge(w, client, **over)
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    return client.post("/api/deploys/launch", headers=_h(w, client),
                       json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=uv)})


def _deploys(w):
    return [c for c in w.review.calls if c["op"] == "deploy"]


def test_the_owner_launches_prod_with_a_passkey_and_no_user_verification(w):
    w.login("owner")
    auth = _signer(w)
    ch = _challenge(w, skip_tests=True).json()
    assert ch["options"]["userVerification"] == "discouraged"
    assert ch["launch"] == {"project": "api", "env": "prod", "branch": "main", "sha": HEAD, "skip_tests": True}
    r = w.client.post("/api/deploys/launch", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=False)})
    assert r.status_code == 200, r.text
    assert r.json() == {"state": "dispatched", "sha": HEAD, "project": "api", "env": "prod", "skip_tests": True}
    [call] = _deploys(w)
    assert call["payload"] == {"kind": "deploy_prod", "project": "api", "repo": "o/api", "env": "prod",
                               "workflow": "deploy-prod.yml", "branch": "main", "sha": HEAD, "skip_tests": True}
    assert call["user"] == "owner"
    # the journal: the launch with its commit and skip_tests, on the environment's row
    rows = {e["env"]: e for e in w.client.get("/api/deploys").json()["envs"]}
    assert rows["prod"]["launch"] is True and rows["staging"]["launch"] is False
    launched = rows["prod"]["launched"]
    assert (launched["user"], launched["sha"], launched["skip_tests"], launched["outcome"]) == (
        "owner", HEAD, True, "dispatched")


def test_another_person_gets_no_button_and_a_refusal(w):
    olga = _as(w, "olga")  # reads the deploys, not the owner
    assert {e["env"]: e["launch"] for e in olga.get("/api/deploys").json()["envs"]} == {
        "staging": False, "prod": False}
    assert _challenge(w, olga).status_code == 403
    assert _challenge(w, _as(w, "anna")).status_code == 403
    assert w.review.calls == []


def test_the_launch_asks_for_the_passkey_alone(w):
    """No typed word: the sheet shows what goes out, the passkey signs it (the owner's choice after KD-9447).
    A word a client still sends changes nothing; a broken skip_tests or a self-deploying environment asks nothing."""
    w.login("owner")
    _signer(w)
    assert _challenge(w).status_code == 200
    assert _challenge(w, confirm="whatever").status_code == 200
    assert _challenge(w, skip_tests="yes").status_code == 400
    assert _challenge(w, env="staging").status_code == 404  # deploys by itself: no button
    assert [c["op"] for c in w.review.calls if c["op"] != "keys"] == ["deploy_head", "deploy_head"]


def test_without_a_signature_nothing_starts(w):
    w.login("owner")
    _signer(w)
    ch = _challenge(w).json()
    r = w.client.post("/api/deploys/launch", headers=w.headers(), json={"challenge_id": ch["challenge_id"]})
    assert r.status_code == 403
    stranger = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)  # a key that is not the owner's
    ch = _challenge(w).json()
    r = w.client.post("/api/deploys/launch", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": stranger.assert_(ch["options"])})
    assert r.status_code == 403
    assert _deploys(w) == []


def test_a_challenge_is_used_once_and_by_its_session(w):
    w.login("owner")
    auth = _signer(w)
    ch = _challenge(w).json()
    body = {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=False)}
    assert w.client.post("/api/deploys/launch", headers=w.headers(), json=body).status_code == 200
    assert w.client.post("/api/deploys/launch", headers=w.headers(), json=body).status_code == 410
    other = _as(w, "owner")
    ch = _challenge(w).json()
    r = other.post("/api/deploys/launch", headers=_h(w, other),
                   json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=False)})
    assert r.status_code == 403
    assert len(_deploys(w)) == 1


def test_the_signature_lives_ten_minutes(w):
    """The brief's term of a deploy_prod signature, not the store's five minutes of other challenges."""
    from kd.cabinet import users

    w.login("owner")
    auth = _signer(w)
    moment = [0.0]
    w.svc.challenges.clock = lambda: moment[0]
    ch = _challenge(w).json()
    assert ch["expires"] == users.shift(w.svc.now(), seconds=600)
    moment[0] = 590.0  # past the store's 300 s
    r = w.client.post("/api/deploys/launch", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=False)})
    assert r.status_code == 200, r.text
    ch = _challenge(w).json()
    moment[0] = 590.0 + 601
    r = w.client.post("/api/deploys/launch", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"], uv=False)})
    assert r.status_code == 410


def test_kd_reviews_refusal_is_shown_and_recorded(w):
    w.login("owner")
    auth = _signer(w)
    w.review.fail["deploy"] = review_client.ReviewRefused("stale", "main moved on: head ddddddd, signed ccccccc")
    r = _launch(w, auth)
    assert r.status_code == 409 and r.json()["error"] == "stale"
    launched = {e["env"]: e for e in w.client.get("/api/deploys").json()["envs"]}["prod"]["launched"]
    assert launched["outcome"] == "stale" and "moved on" in launched["message"]
    w.review.fail["deploy"] = review_client.ReviewUnavailable("kd-review unavailable (ConnectionRefusedError)")
    assert _launch(w, auth).status_code == 503


def test_no_signing_key_known_to_kd_review_asks_for_setup(w):
    w.login("owner")
    _signer(w)
    w.review.keys = []
    r = _challenge(w)
    assert r.status_code == 403 and r.json()["error"] == "setup_required"
