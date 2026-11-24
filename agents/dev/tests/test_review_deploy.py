"""kd-review starts a prod deploy (KD-9447, ``deploy_prod``) only after checking the owner's passkey signature over
the launch it is shown — the term, the nonce, the registered key, the environment in the passports and the branch's
live head; the one signature it takes without user verification."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common" / "tests"))
from softauth import SoftAuthenticator  # noqa: E402
from test_review import NOW, ORIGIN, RP, TOKEN, iso, refused, registry  # noqa: E402
from test_forge_read import make_opener  # noqa: E402

from dev_agent import forge, review  # noqa: E402
from kd import deploy_signature  # noqa: E402
from kd.cabinet import passkeys  # noqa: E402

REPO = "o/api"
SHA, SHA2 = "a" * 40, "b" * 40
LAUNCH = "2" * 32
PAYLOAD = {
    "kind": "deploy_prod",
    "project": "api",
    "repo": REPO,
    "env": "prod",
    "workflow": "deploy-prod.yml",
    "branch": "main",
    "sha": SHA,
    "skip_tests": True,
}


def project(name, repo, deploys):
    return {
        "name": name,
        "route": "@owner",
        "done_by": "",
        "memory_bank": "",
        "repo": {"repo": repo, "dir": f"work/{name}", "deploys": deploys},
    }


PROJECTS = {
    "schema_version": 1,
    "projects": {
        "api": project(
            "api",
            REPO,
            {
                "staging": {"workflow": "deploy-staging.yml", "branch": "main"},
                "prod": {
                    "workflow": "deploy-prod.yml",
                    "branch": "main",
                    "manual": True,
                    "skip_tests": True,
                },
            },
        ),
        "alert": project(
            "alert",
            "o/alert",
            {
                "prod": {
                    "workflow": "deploy-prod.yml",
                    "branch": "master",
                    "manual": True,
                    "confirm": "confirm",
                }
            },
        ),
    },
}


def forgejo(head=SHA, repo=REPO, branch="main", dispatch=None):
    return make_opener(
        {
            ("GET", f"/repos/{repo}/branches/{branch}"): {
                "name": branch,
                "commit": {"id": head},
            },
            ("POST", f"/repos/{repo}/actions/workflows/"): dispatch
            if dispatch is not None
            else {},
        }
    )


def reviewer(tmp_path, opener, nonces=None, projects=PROJECTS):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(projects), encoding="utf-8")
    return review.Reviewer(
        api=forge.Api(TOKEN, opener=opener),
        repos_path=str(tmp_path / "repos.json"),
        signers_dir=str(tmp_path / "signers"),
        origin=ORIGIN,
        clock=lambda: NOW,
        nonces_path=nonces,
        projects_path=str(path),
    )


def signed(
    auth,
    *,
    payload=PAYLOAD,
    sign_payload=None,
    user="owner",
    expires=None,
    nonce="ab" * 16,
    uv=False,
    launch=LAUNCH,
):
    """A deploy request as the cabinet sends it: an assertion without user verification, as the button asks it."""
    expires = expires or iso(NOW + 300)
    value = deploy_signature.challenge(
        launch, sign_payload or payload, user, expires, nonce
    )
    options, _ = passkeys.authentication_options(
        review.Rp(ORIGIN, RP),
        challenge=value,
        allow=[auth.credential_id],
        user_verification=False,
    )
    return {
        "op": "deploy",
        "launch": launch,
        "payload": payload,
        "user": user,
        "expires": expires,
        "nonce": nonce,
        "assertion": json.loads(auth.assert_(options, uv=uv)),
    }


def dispatches(opener):
    return [
        (c[1].split("/api/v1")[-1], json.loads(c[2]))
        for c in opener.seen
        if c[0] == "POST"
    ]


@pytest.fixture
def auth(tmp_path):
    a = SoftAuthenticator(RP, ORIGIN)
    registry(tmp_path, a)
    return a


def test_a_signed_launch_dispatches_the_workflow_with_skip_tests(tmp_path, auth):
    opener = forgejo()
    out = reviewer(tmp_path, opener).handle(signed(auth))
    assert out == {"state": "dispatched", "sha": SHA, "inputs": {"skip_tests": "true"}}
    assert dispatches(opener) == [
        (
            f"/repos/{REPO}/actions/workflows/deploy-prod.yml/dispatches",
            {"ref": "main", "inputs": {"skip_tests": "true"}},
        )
    ]


def test_alert_gets_the_typed_confirm_word(tmp_path, auth):
    payload = {
        **PAYLOAD,
        "project": "alert",
        "repo": "o/alert",
        "branch": "master",
        "skip_tests": False,
    }
    opener = forgejo(repo="o/alert", branch="master")
    reviewer(tmp_path, opener).handle(signed(auth, payload=payload))
    assert dispatches(opener)[0][1] == {
        "ref": "master",
        "inputs": {"confirm": "deploy"},
    }


def test_a_signature_with_user_verification_is_taken_too(tmp_path, auth):
    """Discouraged is not forbidden: an authenticator that verifies the person anyway signs as well."""
    opener = forgejo()
    assert (
        reviewer(tmp_path, opener).handle(signed(auth, uv=True))["state"]
        == "dispatched"
    )


def test_every_other_signature_still_needs_user_verification(tmp_path, auth):
    """A pull request's approval without a fingerprint or PIN is refused, as before: only deploy_prod goes without."""
    from test_review import forgejo as pr_forgejo, signed as pr_signed

    request = pr_signed(auth)
    options = passkeys.authentication_options(
        review.Rp(ORIGIN, RP), challenge=b"x" * 32, allow=[auth.credential_id]
    )[0]
    assert json.loads(options)["userVerification"] == "required"
    from kd import pr_signature

    value = pr_signature.challenge(
        request["decision"],
        request["payload"],
        request["card_revision"],
        "owner",
        request["expires"],
        request["nonce"],
        request["mode"],
    )
    options = passkeys.authentication_options(
        review.Rp(ORIGIN, RP), challenge=value, allow=[auth.credential_id]
    )[0]
    request["assertion"] = json.loads(auth.assert_(options, uv=False))
    opener = pr_forgejo()
    assert "does not match" in str(
        refused(
            review.Reviewer(
                api=forge.Api(TOKEN, opener=opener),
                repos_path=str(_repos(tmp_path)),
                signers_dir=str(tmp_path / "signers"),
                origin=ORIGIN,
                clock=lambda: NOW,
            ),
            request,
        )
    )
    assert dispatches(opener) == []
    with pytest.raises(
        passkeys.PasskeyError
    ):  # the cabinet's own check, as every ceremony but the deploy's
        passkeys.verify_authentication(
            review.Rp(ORIGIN, RP),
            request["assertion"],
            challenge=value,
            public_key=auth._cose_key(),
            sign_count=0,
        )
    assert (
        passkeys.verify_authentication(
            review.Rp(ORIGIN, RP),
            request["assertion"],
            challenge=value,
            public_key=auth._cose_key(),
            sign_count=0,
            user_verification=False,
        )
        >= 0
    )


def _repos(tmp_path):
    from test_forge_read import REPO as PR_REPO

    path = tmp_path / "repos.json"
    path.write_text(
        json.dumps(
            {
                "digipaddock": {
                    "repo": PR_REPO,
                    "dir": "work/DigiPaddock",
                    "release": "digipaddock",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_head_asked_before_signing(tmp_path, auth):
    r = reviewer(tmp_path, forgejo(head=SHA2))
    assert r.handle(
        {"op": "deploy_head", "project": "api", "repo": REPO, "env": "prod"}
    ) == {
        "sha": SHA2,
        "branch": "main",
        "workflow": "deploy-prod.yml",
        "skip_tests": True,
    }
    for project, repo, env in (
        ("api", REPO, "staging"),
        ("api", "o/other", "prod"),
        ("nope", REPO, "prod"),
    ):
        refused(r, {"op": "deploy_head", "project": project, "repo": repo, "env": env})


def test_a_sha_other_than_the_branch_head_is_refused_without_dispatch(tmp_path, auth):
    opener = forgejo(head=SHA2)
    err = refused(reviewer(tmp_path, opener), signed(auth), "stale")
    assert "moved on" in str(err)
    assert dispatches(opener) == []


def test_a_forged_signature_is_refused_without_dispatch(tmp_path, auth):
    opener = forgejo()
    r = reviewer(tmp_path, opener)
    # signed over another commit, another project's test choice, by a key root never registered, as another person
    refused(r, signed(auth, sign_payload={**PAYLOAD, "sha": SHA2}))
    refused(
        r, signed(auth, sign_payload={**PAYLOAD, "skip_tests": False}, nonce="01" * 16)
    )
    refused(r, signed(SoftAuthenticator(RP, ORIGIN), nonce="02" * 16))
    refused(r, signed(auth, user="partner", nonce="03" * 16))
    request = signed(auth, nonce="04" * 16)
    request["assertion"]["response"]["signature"] = (
        request["assertion"]["response"]["signature"][:-4] + "AAAA"
    )
    refused(r, request)
    assert dispatches(opener) == []


def test_a_replayed_signature_is_refused_without_a_second_dispatch(tmp_path, auth):
    opener = forgejo()
    r = reviewer(tmp_path, opener, nonces=tmp_path / "used.json")
    request = signed(auth)
    r.handle(request)
    assert "used" in str(refused(r, request))
    # a restart keeps the spent nonce
    assert "used" in str(
        refused(reviewer(tmp_path, opener, nonces=tmp_path / "used.json"), request)
    )
    assert len(dispatches(opener)) == 1


def test_an_old_or_far_term_is_refused(tmp_path, auth):
    opener = forgejo()
    r = reviewer(tmp_path, opener)
    for i, expires in enumerate(
        (iso(NOW), iso(NOW - 60), iso(NOW + deploy_signature.TERM_SECONDS + 1))
    ):
        refused(r, signed(auth, expires=expires, nonce=f"1{i}" * 16))
    assert dispatches(opener) == []


def test_the_passport_decides_what_starts(tmp_path, auth):
    opener = forgejo()
    no_skip = json.loads(json.dumps(PROJECTS))
    del no_skip["projects"]["api"]["repo"]["deploys"]["prod"]["skip_tests"]
    refused(
        reviewer(tmp_path, opener, projects=no_skip), signed(auth)
    )  # no skip_tests input there
    moved = json.loads(json.dumps(PROJECTS))
    moved["projects"]["api"]["repo"]["deploys"]["prod"]["branch"] = "release"
    refused(
        reviewer(tmp_path, opener, projects=moved),
        signed(auth, nonce="21" * 16),
        "stale",
    )
    auto = {**PAYLOAD, "env": "staging", "workflow": "deploy-staging.yml"}
    refused(
        reviewer(tmp_path, opener), signed(auth, payload=auto, nonce="22" * 16)
    )  # deploys by itself
    assert dispatches(opener) == []


def test_a_payload_that_is_not_a_launch_is_invalid(tmp_path, auth):
    r = reviewer(tmp_path, forgejo())
    request = signed(auth)
    for payload in (
        {**PAYLOAD, "kind": "pr_approve"},
        {**PAYLOAD, "sha": "abc"},
        {**PAYLOAD, "extra": 1},
        {**PAYLOAD, "skip_tests": "yes"},
    ):
        refused(r, {**request, "payload": payload}, "invalid")


def test_the_journal_line_names_the_launch_without_the_assertion(
    tmp_path, auth, capsys
):
    request = signed(auth)
    review.answer(reviewer(tmp_path, forgejo()), json.dumps(request).encode())
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line == {
        "ts": line["ts"],
        "op": "deploy",
        "outcome": "dispatched",
        "user": "owner",
        "repo": REPO,
        "launch": LAUNCH,
        "project": "api",
        "env": "prod",
        "sha": SHA,
        "skip_tests": True,
    }
