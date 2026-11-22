"""the provider catalog and the checks every runtime shares (cabinet, dispatcher, host, worker runner)."""

import pytest

from kd import llm_models

SPEC = {"id": "or-x", "provider": "openrouter", "upstream": "anthropic/claude-sonnet-4.5", "base_url": ""}
PRICES = {"price_in": 2, "price_out": 10, "price_cache_read": 0.1, "price_cache_write": 2.5}


def test_catalog_has_the_four_kinds():
    kinds = llm_models.providers()
    assert set(kinds) == {"anthropic", "openai", "openrouter", "fireworks", "deepseek", "custom"}
    assert all(k["secret"].startswith("llm-") for k in kinds.values())
    assert kinds["custom"]["base_url"] == "" and kinds["openrouter"]["base_url"].startswith("https://")


@pytest.mark.parametrize("bad", ["claude-sonnet-5-5", "sonnet", "opus", "haiku", "Or-GPT", "a" * 65, "", ".x", "a/b", 7])
def test_check_slug_refuses_claude_names_and_odd_text(bad):
    with pytest.raises(ValueError):
        llm_models.check_slug(bad)


@pytest.mark.parametrize("good", ["or-gpt5", "fw.deepseek_v4", "a"])
def test_check_slug_keeps_a_good_slug(good):
    assert llm_models.check_slug(good) == good


@pytest.mark.parametrize("good", ["openai/gpt-5", "accounts/fireworks/models/x", "deepseek-v4-pro[1m]", "x"])
def test_check_upstream_keeps_provider_ids(good):
    assert llm_models.check_upstream(good) == good


@pytest.mark.parametrize("bad", ["a b", "-x", "x\n", "x" * 129, "", "/x", None])
def test_check_upstream_refuses_odd_text(bad):
    with pytest.raises(ValueError):
        llm_models.check_upstream(bad)


def test_check_effort_is_a_level_or_empty():
    assert llm_models.check_effort("") == "" and llm_models.check_effort("xhigh") == "xhigh"
    for bad in ("ultra", 3, None, "High"):
        with pytest.raises(ValueError):
            llm_models.check_effort(bad)


def test_check_spec_base_url_only_for_custom():
    assert llm_models.check_spec(SPEC) == SPEC
    with pytest.raises(ValueError):
        llm_models.check_spec({**SPEC, "base_url": "https://x"})
    custom = {**SPEC, "provider": "custom", "base_url": "https://gw.local/v1"}
    assert llm_models.check_spec(custom) == custom
    for bad in ({**custom, "base_url": "http://x"}, {**custom, "base_url": ""}, {**SPEC, "extra": 1},
                {**SPEC, "provider": "together"}, {k: v for k, v in SPEC.items() if k != "upstream"}, "x"):
        with pytest.raises(ValueError):
            llm_models.check_spec(bad)


def test_environment_maps_every_alias_to_the_upstream():
    env = llm_models.environment(SPEC)
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_API_KEY"] == ""
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
        assert env[key] == "anthropic/claude-sonnet-4.5"
    custom = llm_models.environment({**SPEC, "provider": "custom", "base_url": "https://gw.local/v1"})
    assert custom["ANTHROPIC_BASE_URL"] == "https://gw.local/v1" and "ANTHROPIC_API_KEY" not in custom


def test_env_file_and_key_present(tmp_path):
    assert llm_models.env_file("openrouter") == "/etc/digipaddock/llm/openrouter.env"
    assert not llm_models.key_present("openrouter", env_dir=str(tmp_path))
    (tmp_path / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=sk-or-v1-abc\n", encoding="utf-8")
    assert llm_models.key_present("openrouter", env_dir=str(tmp_path))
    with pytest.raises(ValueError):
        llm_models.env_file("together")


@pytest.mark.parametrize("text, found", [
    ("ANTHROPIC_AUTH_TOKEN=sk-or-v1-abc\n", {"ANTHROPIC_AUTH_TOKEN": "sk-or-v1-abc"}),
    ("ANTHROPIC_AUTH_TOKEN=fw_Ab/c+d=\n", {"ANTHROPIC_AUTH_TOKEN": "fw_Ab/c+d="}),
    ('ANTHROPIC_AUTH_TOKEN="sk-or-v1-abc"\n', {}),
    ("ANTHROPIC_AUTH_TOKEN=sk-or-v1-abc\nexport X=1\n", {}),
    ("ANTHROPIC_AUTH_TOKEN=\n", {}),
    ("OTHER=x\n", {}),
    ("", {}),
])
def test_read_secret_accepts_one_bare_line_only(tmp_path, text, found):
    (tmp_path / "openrouter.env").write_text(text, encoding="utf-8")
    assert llm_models.read_secret("openrouter", env_dir=str(tmp_path)) == found
    assert bool(llm_models.secret_problem(text)) == (found == {})


def test_read_secret_without_a_file_is_empty(tmp_path):
    assert llm_models.read_secret("openrouter", env_dir=str(tmp_path)) == {}


def test_cost():
    fields = {"in": 1_000_000, "out": 100_000, "cache_read": 10_000_000, "cache_write": 0}
    assert llm_models.cost(PRICES, fields) == pytest.approx(4.0)
    assert llm_models.cost(PRICES, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 400_000}) == pytest.approx(1.0)


def test_experimental():
    assert not llm_models.experimental("anthropic/claude-sonnet-4.5")
    assert llm_models.experimental("openai/gpt-5")


# --- the key stays root's (spec §3.6) ---------------------------------------------------------------------------
REPO = llm_models.PROVIDERS_PATH.parents[2]


def test_no_agent_reads_a_provider_key():
    """Like the host's Claude token: the rules floor, the resources registry and the host reader refuse
    the key directory."""
    import json

    settings = json.loads((REPO / "common" / "claude" / "managed-settings.template.json").read_text(encoding="utf-8"))
    assert "Read(//etc/digipaddock/llm/**)" in settings["permissions"]["deny"]
    resources = json.loads((REPO / "common" / "agent-resources.json").read_text(encoding="utf-8"))
    assert {"kind": "dir", "path": llm_models.ENV_DIR, "deny": ["read"]} in resources["protected"]
    never = (REPO / "common" / "install" / "host_read_setup.sh").read_text(encoding="utf-8").split("NEVER=(", 1)[1]
    assert llm_models.ENV_DIR in never.split(")", 1)[0]


def test_sandboxes_deny_the_provider_key_to_bash():
    import json

    from kd.registry import FAIL_CLOSED_SANDBOX

    want = [{"name": "CLAUDE_CODE_OAUTH_TOKEN", "mode": "deny"}, {"name": llm_models.SECRET_KEY, "mode": "deny"}, {"name": "ANTHROPIC_API_KEY", "mode": "deny"}]
    assert FAIL_CLOSED_SANDBOX["credentials"]["envVars"] == want
    for profile in ("analyst", "coordinator", "developer", "sysadmin"):
        sandbox = json.loads((REPO / "common" / "agent-sandboxes" / f"{profile}-v1.json").read_text(encoding="utf-8"))
        assert sandbox["settings"]["credentials"]["envVars"] == want, profile


def test_catalog_has_a_secret_per_kind():
    from kd import install_catalog

    from kd import naming

    host = naming.load_host(str(REPO / "host" / "host.example.json"))
    by = {s.name: s for s in install_catalog.secrets(host=host, prefix="digi")}
    for kind, entry in llm_models.providers().items():
        s = by[entry["secret"]]
        assert (s.path, s.owner, s.group, s.mode) == (llm_models.env_file(kind), "root", "root", 0o600)
        assert s.format == llm_models.secret_key(kind) + "=<key>" and s.wanted_by == () and s.restart == ()
        assert s.needed_by == "common/install/session_host_setup.sh"


# --- the models of the Claude subscription, by reference ``claude:<alias>`` ----------------------------
OPUS = {"id": "claude:opus", "provider": "subscription", "upstream": "opus", "base_url": ""}


def test_claude_catalog_lists_the_four_aliases():
    catalog = llm_models.claude_models()
    assert list(catalog) == ["claude:opus", "claude:sonnet", "claude:haiku", "claude:fable"]
    assert all(row["upstream"] == ref.partition(":")[2] and row["title"] and row["id"] == ref
               for ref, row in catalog.items())


def test_subscription_spec_and_check_spec():
    assert llm_models.subscription_spec("claude:opus") == OPUS
    assert llm_models.check_spec(OPUS) == OPUS
    for bad in ({**OPUS, "upstream": "sonnet"}, {**OPUS, "base_url": "https://x"}, {**OPUS, "id": "claude:gpt"},
                {**OPUS, "id": "or-x"}, {**OPUS, "extra": 1}):
        with pytest.raises(ValueError):
            llm_models.check_spec(bad)
    with pytest.raises(ValueError):
        llm_models.subscription_spec("claude:nope")
    with pytest.raises(ValueError):
        llm_models.subscription_spec("or-x")


def test_subscription_has_no_environment():
    assert llm_models.environment(llm_models.subscription_spec("claude:sonnet")) == {}
    assert llm_models.is_subscription(OPUS) is True
    assert llm_models.is_subscription(None) is False and llm_models.is_subscription(SPEC) is False


def test_a_slug_is_never_a_claude_ref():
    with pytest.raises(ValueError):
        llm_models.check_slug("claude:opus")
    assert llm_models.is_ref("claude:opus") and llm_models.is_ref("claude:claude-opus-5-5")
    assert not llm_models.is_ref("or-x") and not llm_models.is_ref("claude:") and not llm_models.is_ref(7)


@pytest.mark.parametrize("models", [
    [{"id": "claude:gpt", "upstream": "gpt-5", "title": "GPT"}],  # not a Claude model
    [{"id": "claude:opus", "upstream": "opus"}],  # no title
    [{"id": "opus", "upstream": "opus", "title": "Opus"}],  # not a reference
    [{"id": "claude:opus", "upstream": "opus", "title": "A"}, {"id": "claude:opus", "upstream": "opus", "title": "B"}],
    [],
])
def test_bad_claude_catalog_is_refused(tmp_path, models):
    import json

    path = tmp_path / "claude-models.json"
    path.write_text(json.dumps({"schema_version": 1, "models": models}), encoding="utf-8")
    with pytest.raises(ValueError):
        llm_models.claude_models(path)


# --- which of the names a run reported is the one the model's upstream names ------------------------------
@pytest.mark.parametrize("upstream, names, found", [
    ("opus", ["claude-haiku-4-5", "claude-opus-5-5"], "claude-opus-5-5"),
    ("opus", ["claude-opus-6-0", "claude-opus-5-5"], "claude-opus-6-0"),  # the first of the given order
    ("claude-opus-4-1", ["claude-opus-4-1-20250805"], "claude-opus-4-1-20250805"),
    ("opus", ["claude-haiku-4-5"], ""),
    ("opus", ["claude-opusplan-1"], ""),  # a token, not a substring
    ("opus", [], ""),
])
def test_served_is_the_name_the_upstream_names(upstream, names, found):
    assert llm_models.served(upstream, names) == found
