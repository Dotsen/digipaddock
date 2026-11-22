"""one wake facade — an agent on the session host gets a cabinet command; there is no other."""

import re
from pathlib import Path

from kd import session_config, wake
from kd.cabinet.client import CabinetError

ROOT = Path(__file__).resolve().parents[2]
HOSTED = session_config.HostConfig({"analyst": 1}, 600)


class Cabinet:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def call(self, request):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        return {"command": {"id": "c" * 32}}


def test_hosted_agent_enqueues_and_returns_sent():
    cabinet = Cabinet()
    out = wake.wake("analyst", "[board] KD-1 assigned to you", card="KD-1", config=HOSTED, client=cabinet)
    assert out == "sent"
    [request] = cabinet.calls
    assert request["op"] == "command.enqueue" and request["agent"] == "analyst" and request["card"] == "KD-1"
    assert request["text"] == "[board] KD-1 assigned to you" and re.fullmatch(r"[0-9a-f]{32}", request["key"])
    assert "space" not in request


def test_a_wake_into_the_monitor_thread_names_its_space():
    """The monitor's wake goes to the agent's "Monitor" thread."""
    cabinet = Cabinet()
    assert wake.wake("analyst", "[монитор] P-7", space="monitor", config=HOSTED, client=cabinet) == "sent"
    assert cabinet.calls[0]["space"] == "monitor" and cabinet.calls[0]["card"] == ""


def test_hosted_text_is_one_line_and_bounded():
    cabinet = Cabinet()
    wake.wake("analyst", "[monitor] two\nlines\x1b[31m" + "x" * 3000, config=HOSTED, client=cabinet)
    text = cabinet.calls[0]["text"]
    assert "\n" not in text and "\x1b" not in text and len(text) <= 2000 and text.startswith("[monitor] two lines")


def test_same_wake_twice_in_a_minute_has_one_key():
    cabinet = Cabinet()
    for _ in range(2):
        wake.wake("analyst", "[watch] x", config=HOSTED, client=cabinet, now=lambda: 1_000_000.0)
    assert cabinet.calls[0]["key"] == cabinet.calls[1]["key"]
    wake.wake("analyst", "[watch] x", config=HOSTED, client=cabinet, key="directive:abc")
    assert cabinet.calls[2]["key"] == "directive:abc"


def test_an_agent_off_the_host_is_absent(capsys):
    cabinet = Cabinet()
    assert wake.wake("dev", "[board] KD-2 assigned to you", urgent=True, config=HOSTED, client=cabinet) == "absent"
    assert cabinet.calls == [] and "kd-wake: dev is not on the session host" in capsys.readouterr().err


def test_cabinet_down_is_error():
    out = wake.wake("analyst", "x", config=HOSTED, client=Cabinet(CabinetError("unavailable", "down")))
    assert out == "error"


def test_broken_config_is_an_error(tmp_path, capsys):
    path = tmp_path / "session-host.json"
    path.write_text("{", encoding="utf-8")
    cabinet = Cabinet()
    assert wake.wake("analyst", "x", config_path=str(path), client=cabinet) == "error"
    assert cabinet.calls == [] and "session-host" in capsys.readouterr().err

