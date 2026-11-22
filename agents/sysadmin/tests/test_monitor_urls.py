"""The monitor's links into the cabinet (/incidents/P-n)."""
from sysadmin.monitor import urls

BOARD = "https://cabinet.example.test"


def test_urls_lead_to_the_cabinet_and_fullmatch_only():
    assert urls.problem_url("P-35") == f"{BOARD}/incidents/P-35"
    for bad in ("P-", "p-35", "P-35/../x", "P-12345678", 'P-1"', "P-1\n", None, 35):
        assert urls.problem_url(bad) == "", bad
    assert not hasattr(urls, "MONITOR_URL")


def test_board_url_reads_the_cabinet_origin(tmp_path, monkeypatch):
    cabinet = tmp_path / "cabinet.json"
    cabinet.write_text('{"origin": "https://cabinet.example.test"}')
    monkeypatch.setattr(urls, "CABINET_JSON", str(cabinet))
    assert urls.board_url() == "https://cabinet.example.test"
    assert urls.problem_url("P-1") == "https://cabinet.example.test/incidents/P-1"


def test_board_url_without_cabinet_json_uses_host_json(tmp_path, monkeypatch):
    monkeypatch.setattr(urls, "CABINET_JSON", str(tmp_path / "absent.json"))
    assert urls.board_url() == "https://cabinet.example.test"
