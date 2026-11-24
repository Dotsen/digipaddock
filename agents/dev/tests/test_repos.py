import json
import pytest
from dev_agent import repos


def _write(tmp_path, data):
    p = tmp_path / "repos.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_load_and_get(tmp_path):
    p = _write(tmp_path, {"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock"}})
    r = repos.get("digipaddock", p)
    assert r.repo == "example/digipaddock" and r.dir == "/srv/digipaddock-agents/dev/work/DigiPaddock"
    assert r.release == "digipaddock"


def test_release_optional_and_unknown_project(tmp_path):
    p = _write(tmp_path, {"api": {"repo": "example-owner/ExampleApi", "dir": "work/ExampleApi"}})
    assert repos.get("api", p).release is None
    with pytest.raises(repos.RepoError, match="не подключён"):
        repos.get("digipaddock", p)


@pytest.mark.parametrize("data", [
    {"Bad Name": {"repo": "a/b", "dir": "work/x"}},
    {"x": {"repo": "a/b", "dir": "../x"}},
    {"x": {"repo": "a/b", "dir": "/abs"}},
    {"x": {"repo": "bad", "dir": "work/x"}},
    {"x": {"repo": "a/b", "dir": "work/x", "release": "other"}},
    {"x": {"repo": "a/b", "dir": "work/x", "release": ["digipaddock"]}},  # not a string — a RepoError, not a TypeError
    {"x": {"repo": "a/b", "dir": "work/.."}},  # the agent's folder itself
    {"x": {"repo": "a/b", "dir": "work/."}},   # work/ itself
    {"x": "work/x"},
    [],
])
def test_invalid_registry_rejected(tmp_path, data):
    with pytest.raises(repos.RepoError):
        repos.load(_write(tmp_path, data))


def test_missing_file_is_error(tmp_path):
    with pytest.raises(repos.RepoError, match="repos.json"):
        repos.load(tmp_path / "none.json")


def test_get_rejects_bad_project_name_before_reading(tmp_path):
    with pytest.raises(repos.RepoError, match="неверное имя проекта"):
        repos.get("../etc", tmp_path / "none.json")


def test_role_defaults_to_code_and_overlay_is_one_per_release(tmp_path):
    """a release is its code repository and, optionally, its deployment overlay."""
    p = _write(tmp_path, {
        "digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock"},
        "digipaddock-deploy": {"repo": "example/deploy", "dir": "work/deploy", "release": "digipaddock", "role": "overlay"},
        "api": {"repo": "example/api", "dir": "work/api"}})
    found = repos.load(p)
    assert (found["digipaddock"].role, found["digipaddock-deploy"].role, found["api"].role) == ("code", "overlay", "code")


@pytest.mark.parametrize("data", [
    {"a": {"repo": "a/b", "dir": "work/a", "release": "digipaddock"},
     "b": {"repo": "a/c", "dir": "work/b", "release": "digipaddock", "role": "code"}},
    {"a": {"repo": "a/b", "dir": "work/a", "release": "digipaddock", "role": "overlay"},
     "b": {"repo": "a/c", "dir": "work/b", "release": "digipaddock", "role": "overlay"}},
    {"a": {"repo": "a/b", "dir": "work/a", "release": "digipaddock", "role": "deploy"}},
    {"a": {"repo": "a/b", "dir": "work/a", "role": "overlay"}},  # an overlay belongs to a release
])
def test_roles_are_checked(tmp_path, data):
    with pytest.raises(repos.RepoError, match="role"):
        repos.load(_write(tmp_path, data))


def test_old_entry_gets_the_passport_defaults(tmp_path):
    """today's entries (and a schema-2 file an older release never wrote) load as before."""
    r = repos.get("digipaddock", _write(tmp_path, {"digipaddock": {"repo": "example/digipaddock", "dir": "work/DigiPaddock",
                                                       "release": "digipaddock"}}))
    assert (r.base, r.targets, r.require_up_to_date, r.up_to_date_paths) == ("main", ("main",), True, ())
    assert repos.required(r) == [] and repos.effect(r, "main") == "" and r.timeout_s == 1800


PASSPORT = {
    "repo": "example/alert", "dir": "work/alert", "base": "master", "targets": ["master"],
    "checks": {"contour": "dev-a", "profiles": {"ready": {}, "unit": {"required": True}, "static": {"required": True},
                                                "integration": {"required": False}}, "timeout_s": 1200},
    "merge_effects": {"master": "Forgejo Actions: деплой на stage приёмки"},
    "merge": {"require_up_to_date": False, "up_to_date_paths": ["common/kd/cabinet/schema.py", "migrations/*"]},
    "release": None,
}


def test_schema_2_entry(tmp_path):
    r = repos.get("alert", _write(tmp_path, {"alert": PASSPORT}))
    assert (r.base, r.targets, r.contour, r.timeout_s) == ("master", ("master",), "dev-a", 1200)
    assert repos.required(r) == ["unit", "static"]
    assert repos.effect(r, "master") == "Forgejo Actions: деплой на stage приёмки" and repos.effect(r, "dev") == ""
    assert r.release is None and r.require_up_to_date is False


def test_needs_up_to_date(tmp_path):
    r = repos.get("alert", _write(tmp_path, {"alert": PASSPORT}))
    assert repos.needs_up_to_date(r, {"a.py"}) is False
    assert repos.needs_up_to_date(r, {"a.py", "common/kd/cabinet/schema.py"}) is True
    assert repos.needs_up_to_date(r, ["migrations/0001.sql"]) is True
    strict = repos.get("alert", _write(tmp_path, {"alert": {**PASSPORT, "merge": {"require_up_to_date": True}}}))
    assert repos.needs_up_to_date(strict, ()) is True


@pytest.mark.parametrize("change", [
    {"targets": []},
    {"targets": ["master", "master"]},
    {"targets": [1]},
    {"targets": "master"},
    {"base": "../x"},
    {"base": "a..b"},
    {"base": "x.lock"},
    {"base": "-x"},
    {"base": ""},
    {"checks": {"profiles": {"Unit": {"required": True}}}},
    {"checks": {"profiles": {"unit": {"required": "yes"}}}},
    {"checks": {"profiles": {"unit": True}}},
    {"checks": {"profiles": {}, "timeout_s": 10}},
    {"checks": {"profiles": {}, "timeout_s": True}},
    {"checks": {"contour": "Bad Contour"}},
    {"checks": []},
    {"merge_effects": {"master": "x" * 301}},
    {"merge_effects": {"dev": "a branch not in targets"}},
    {"merge_effects": {"master": 1}},
    {"merge": {"require_up_to_date": "no"}},
    {"merge": {"up_to_date_paths": ["/abs"]}},
    {"merge": {"up_to_date_paths": ["a/../b"]}},
    {"merge": {"up_to_date_paths": "a.py"}},
])
def test_invalid_passport_rejected(tmp_path, change):
    with pytest.raises(repos.RepoError, match="alert"):
        repos.load(_write(tmp_path, {"alert": {**PASSPORT, **change}}))


DEPLOYS = {"test": {"workflow": "deploy-test.yml", "branch": "master"},
           "stage": {"workflow": "deploy-stage.yml", "branch": "dev"},
           "prod": {"workflow": "deploy-prod.yml", "branch": "master", "manual": True}}


def test_the_passports_deploys_keep_their_order(tmp_path):
    """KD-9443: the environments of «Deploys» — the workflow of each, its branch, whether it runs by hand."""
    r = repos.get("alert", _write(tmp_path, {"alert": {**PASSPORT, "deploys": DEPLOYS}}))
    assert r.deploys == (repos.Deploy("test", "deploy-test.yml", "master", False),
                         repos.Deploy("stage", "deploy-stage.yml", "dev", False),
                         repos.Deploy("prod", "deploy-prod.yml", "master", True))
    assert repos.get("alert", _write(tmp_path, {"alert": PASSPORT})).deploys == ()


def test_a_manual_deploy_names_the_buttons_inputs(tmp_path):
    """KD-9447: alert's prod takes the typed word in its input confirm, api's prod the boolean skip_tests."""
    deploys = {"prod": {"workflow": "deploy-prod.yml", "branch": "master", "manual": True, "confirm": "confirm",
                        "skip_tests": True}}
    r = repos.get("alert", _write(tmp_path, {"alert": {**PASSPORT, "deploys": deploys}}))
    assert r.deploys == (repos.Deploy("prod", "deploy-prod.yml", "master", True, "confirm", True),)


@pytest.mark.parametrize("deploys", [
    [],
    {"Test": {"workflow": "a.yml", "branch": "main"}},
    {"test": {"workflow": "deploy.sh", "branch": "main"}},
    {"test": {"workflow": "../a.yml", "branch": "main"}},
    {"test": {"workflow": "a.yml", "branch": "a..b"}},
    {"test": {"workflow": "a.yml"}},
    {"test": {"workflow": "a.yml", "branch": "main", "manual": "yes"}},
    {"test": {"workflow": "a.yml", "branch": "main", "inputs": {}}},
    {"test": {"workflow": "a.yml", "branch": "main"}, "prod": {"workflow": "a.yml", "branch": "main"}},
    {f"e{n}": {"workflow": f"d{n}.yml", "branch": "main"} for n in range(9)},
    # the owner's button (KD-9447): its inputs only on an environment started by hand
    {"prod": {"workflow": "a.yml", "branch": "main", "confirm": "confirm"}},
    {"prod": {"workflow": "a.yml", "branch": "main", "skip_tests": True}},
    {"prod": {"workflow": "a.yml", "branch": "main", "manual": True, "confirm": "a b"}},
    {"prod": {"workflow": "a.yml", "branch": "main", "manual": True, "confirm": 1}},
    {"prod": {"workflow": "a.yml", "branch": "main", "manual": True, "skip_tests": "true"}},
    {"prod": {"workflow": "a.yml", "branch": "main", "manual": True, "confirm": "skip_tests", "skip_tests": True}},
])
def test_invalid_deploys_rejected(tmp_path, deploys):
    with pytest.raises(repos.RepoError, match="alert: deploys"):
        repos.load(_write(tmp_path, {"alert": {**PASSPORT, "deploys": deploys}}))


@pytest.mark.parametrize("change", [{"base": "dev", "targets": ["dev"]}, {"targets": ["main", "dev"]}])
def test_a_released_project_merges_into_main_only(tmp_path, change):
    """kd-release releases main (spec §6.1): a released project's passport cannot point elsewhere."""
    entry = {"repo": "example/digipaddock", "dir": "work/DigiPaddock", "release": "digipaddock", **change}
    with pytest.raises(repos.RepoError, match="release"):
        repos.load(_write(tmp_path, {"digipaddock": entry}))
