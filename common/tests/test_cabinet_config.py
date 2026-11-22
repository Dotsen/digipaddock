import pytest

from kd.cabinet import config


def test_loads_public_settings_and_secrets(tmp_path):
    (tmp_path / "cabinet.json").write_text(
        '{"schema_version": 1, "origin": "https://cab.example", "language": "ru", "timezone": "Etc/GMT-3"}',
        encoding="utf-8",
    )
    (tmp_path / "cabinet.env").write_text(
        "CABINET_SECRET="
        + "a" * 64
        + "\nVAPID_PRIVATE_KEY=priv\nVAPID_PUBLIC_KEY=pub\nVAPID_SUBJECT=mailto:x@example\n",
        encoding="utf-8",
    )
    cfg = config.load(str(tmp_path / "cabinet.json"), str(tmp_path / "cabinet.env"))
    assert cfg.origin == "https://cab.example" and cfg.rp_id == "cab.example"
    assert cfg.secret == bytes.fromhex("a" * 64) and cfg.vapid_public == "pub"
    # the private key is the push sender's (push.env); a cabinet.env from before the split still loads
    (tmp_path / "cabinet.env").write_text(
        "CABINET_SECRET=" + "a" * 64 + "\nVAPID_PUBLIC_KEY=pub\nVAPID_SUBJECT=mailto:x@example\n",
        encoding="utf-8",
    )
    assert config.load(str(tmp_path / "cabinet.json"), str(tmp_path / "cabinet.env")).vapid_private == ""
    assert config.card_url(cfg.origin, "KD-7") == "https://cab.example/cards/KD-7"
    assert config.decision_url(cfg.origin, "ab") == "https://cab.example/decisions/ab"
    assert (
        config.public(str(tmp_path / "cabinet.json"))["origin"] == "https://cab.example"
    )


@pytest.mark.parametrize(
    "env", ["", "CABINET_SECRET=short\n", "CABINET_SECRET=" + "a" * 64 + "\n"]
)
def test_refuses_a_short_or_missing_secret_and_missing_vapid(tmp_path, env):
    (tmp_path / "cabinet.json").write_text(
        '{"schema_version": 1, "origin": "https://cab.example"}', encoding="utf-8"
    )
    (tmp_path / "cabinet.env").write_text(env, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(str(tmp_path / "cabinet.json"), str(tmp_path / "cabinet.env"))


@pytest.mark.parametrize(
    "settings",
    [
        '{"origin": "ftp://cab.example"}',
        '{"origin": "http://cab.example"}',
        '{"origin": "https://cab.example/path"}',
        '{"origin": "https://cab.example", "language": "de"}',
        '{"origin": "https://cab.example", "timezone": "Mars/Base"}',
        "[]",
    ],
)
def test_refuses_bad_public_settings(tmp_path, settings):
    (tmp_path / "cabinet.json").write_text(settings, encoding="utf-8")
    (tmp_path / "cabinet.env").write_text(
        "CABINET_SECRET="
        + "a" * 64
        + "\nVAPID_PRIVATE_KEY=p\nVAPID_PUBLIC_KEY=q\nVAPID_SUBJECT=mailto:x@example\n",
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError):
        config.load(str(tmp_path / "cabinet.json"), str(tmp_path / "cabinet.env"))


def test_defaults_and_a_port_in_the_origin(tmp_path):
    (tmp_path / "cabinet.json").write_text(
        '{"origin": "https://cab.example:8443"}', encoding="utf-8"
    )
    (tmp_path / "cabinet.env").write_text(
        "CABINET_SECRET="
        + "b" * 64
        + "\nVAPID_PRIVATE_KEY=p\nVAPID_PUBLIC_KEY=q\nVAPID_SUBJECT=mailto:x@example\n",
        encoding="utf-8",
    )
    cfg = config.load(str(tmp_path / "cabinet.json"), str(tmp_path / "cabinet.env"))
    assert cfg.rp_id == "cab.example" and cfg.language == "ru" and cfg.timezone == "UTC"
    assert not cfg.dev


def test_push_env_holds_the_private_key_alone(tmp_path):
    """Global review 15.10.2026: the push sender (its own user) reads push.env — the VAPID private key and subject."""
    (tmp_path / "push.env").write_text(
        "VAPID_PRIVATE_KEY=priv\nVAPID_SUBJECT=mailto:x@example\n", encoding="utf-8"
    )
    push = config.load_push(str(tmp_path / "push.env"))
    assert (push.vapid_private, push.vapid_subject) == ("priv", "mailto:x@example")
    for text in ("", "VAPID_SUBJECT=mailto:x@example\n", "VAPID_PRIVATE_KEY=priv\n"):
        (tmp_path / "push.env").write_text(text, encoding="utf-8")
        with pytest.raises(config.ConfigError):
            config.load_push(str(tmp_path / "push.env"))
    with pytest.raises(config.ConfigError):
        config.load_push(str(tmp_path / "missing.env"))


def test_dev_mode_needs_no_files(tmp_path):
    cfg = config.load(str(tmp_path / "none.json"), str(tmp_path / "none.env"), dev=True)
    assert (
        cfg.dev and cfg.origin == "http://localhost:8765" and cfg.rp_id == "localhost"
    )
    assert len(cfg.secret) == 32 and cfg.vapid_public  # generated for the run
    other = config.load(
        str(tmp_path / "none.json"),
        str(tmp_path / "none.env"),
        dev=True,
        origin="http://localhost:9000",
    )
    assert other.origin == "http://localhost:9000"


def test_card_link_points_to_the_cabinet_or_falls_back(tmp_path):
    missing = tmp_path / "none.json"
    assert config.card_link(
        "KD-5", "https://old.example/board/", json_path=str(missing)
    ) == ("https://old.example/board/KD-5")
    present = tmp_path / "cabinet.json"
    present.write_text(
        '{"schema_version": 1, "origin": "https://cab.example"}', encoding="utf-8"
    )
    assert config.card_link(
        "KD-5", "https://old.example/board/", json_path=str(present)
    ) == ("https://cab.example/cards/KD-5")


def test_origin_of_takes_only_a_valid_origin(tmp_path):
    path = tmp_path / "cabinet.json"
    for bad in ('"javascript:alert(1)"', '"https://cab.example/x\\"><b>"', "5", '""'):
        path.write_text('{"origin": %s}' % bad, encoding="utf-8")
        assert config.origin_of(str(path)) is None, bad
