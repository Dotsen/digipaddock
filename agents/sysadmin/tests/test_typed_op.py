"""agent-typed-op — one root cron line, one sysctl/logrotate file, the target's script switch."""

import io

import pytest

from gate import agent_typed_op as op


@pytest.fixture
def roots(tmp_path, monkeypatch):
    sysctl, logrotate, copies = (
        tmp_path / "sysctl.d",
        tmp_path / "logrotate.d",
        tmp_path / "root",
    )
    for folder in (sysctl, logrotate, copies):
        folder.mkdir()
    monkeypatch.setattr(op, "DIRS", (str(sysctl), str(logrotate)))
    monkeypatch.setattr(op, "COPIES", str(copies))
    return sysctl, logrotate, copies


class Crontab:
    def __init__(self, data=b""):
        self.data, self.writes = data, []

    def read(self):
        return self.data

    def write(self, data):
        self.writes.append(data)
        self.data = data


def test_cron_set_adds_then_replaces_the_cards_one_line(roots):
    tab = Crontab(b"0 1 * * * /opt/other.sh\n")
    said = op.cron(["KD-7", "set", "*/5 * * * * /opt/a.sh"], tab.read, tab.write)
    assert "добавлена" in said
    assert tab.data == b"0 1 * * * /opt/other.sh\n*/5 * * * * /opt/a.sh # kd:KD-7\n"
    assert (
        roots[2] / "kd-7" / "crontab.before"
    ).read_bytes() == b"0 1 * * * /opt/other.sh\n"
    said = op.cron(["KD-7", "set", "*/10 * * * * /opt/a.sh"], tab.read, tab.write)
    assert "заменена" in said
    assert tab.data == b"0 1 * * * /opt/other.sh\n*/10 * * * * /opt/a.sh # kd:KD-7\n"
    # the copy is the state before the card's first change
    assert (
        roots[2] / "kd-7" / "crontab.before"
    ).read_bytes() == b"0 1 * * * /opt/other.sh\n"
    assert "без изменений" in op.cron(
        ["KD-7", "set", "*/10 * * * * /opt/a.sh"], tab.read, tab.write
    )
    assert len(tab.writes) == 2


def test_cron_remove_takes_only_the_cards_line(roots):
    tab = Crontab(b"# keep\n1 2 * * * /x # kd:KD-8\n3 4 * * * /y # kd:KD-9\n")
    assert "убрана" in op.cron(["KD-8", "remove"], tab.read, tab.write)
    assert tab.data == b"# keep\n3 4 * * * /y # kd:KD-9\n"
    assert "без изменений" in op.cron(["KD-8", "remove"], tab.read, tab.write)
    assert op.cron(["KD-9", "remove"], tab.read, tab.write) and tab.data == b"# keep\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["KD-7", "set"],
        ["KD-7", "remove", "x"],
        ["7", "set", "* * * * * a"],
        ["KD-7", "set", "a\nb"],
        ["KD-7", "set", "* * * * * a # kd:KD-1"],
        ["KD-7", "drop"],
    ],
)
def test_cron_refuses_a_malformed_step(roots, argv):
    tab = Crontab()
    with pytest.raises(op.Refuse):
        op.cron(argv, tab.read, tab.write)
    assert tab.writes == []


def test_cron_refuses_two_lines_of_one_card(roots):
    tab = Crontab(b"1 * * * * /a # kd:KD-7\n2 * * * * /b # kd:KD-7\n")
    with pytest.raises(op.Refuse, match="несколько"):
        op.cron(["KD-7", "set", "* * * * * /c"], tab.read, tab.write)


def test_file_set_writes_keeps_the_old_one_and_loads_sysctl(roots):
    sysctl = roots[0]
    loaded = []
    target = sysctl / "60-kd.conf"
    said = op.drop(
        [str(sysctl), "60-kd.conf", "KD-5", "set", "vm.swappiness = 10"],
        load=lambda argv: loaded.append(argv) or (0, b"", b""),
    )
    assert (
        target.read_text() == "vm.swappiness = 10\n"
        and oct(target.stat().st_mode & 0o777) == "0o644"
    )
    assert "записан" in said and loaded == [["sysctl", "-p", str(target)]]
    op.drop(
        [str(sysctl), "60-kd.conf", "KD-5", "set", "vm.swappiness = 20\n"],
        load=lambda argv: (0, b"", b""),
    )
    assert target.read_text() == "vm.swappiness = 20\n"
    assert (
        roots[2] / "kd-5" / "60-kd.conf.before"
    ).read_text() == "vm.swappiness = 10\n"
    assert not list(sysctl.glob(".*kd-new"))


def test_file_remove_and_logrotate(roots):
    logrotate = roots[1]
    path = logrotate / "app"
    path.write_text("/var/log/app.log {\n daily\n}\n")
    said = op.drop([str(logrotate), "app", "KD-6", "remove"])
    assert "убран" in said and not path.exists()
    assert (
        roots[2] / "kd-6" / "app.before"
    ).read_text() == "/var/log/app.log {\n daily\n}\n"
    assert "без изменений" in op.drop([str(logrotate), "app", "KD-6", "remove"])
    assert "записан" in op.drop(
        [str(logrotate), "app", "KD-6", "set", "/var/log/b.log {\n weekly\n}\n"]
    )


def test_file_refuses_another_directory_a_path_or_a_link(roots, tmp_path):
    with pytest.raises(op.Refuse, match="каталог"):
        op.drop(["/etc/cron.d", "x", "KD-1", "set", "x"])
    with pytest.raises(op.Refuse, match="имя"):
        op.drop([str(roots[1]), "../x", "KD-1", "set", "x"])
    (tmp_path / "elsewhere").write_text("secret")
    (roots[1] / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(op.Refuse, match="не обычный"):
        op.drop([str(roots[1]), "link", "KD-1", "set", "x"])
    assert (tmp_path / "elsewhere").read_text() == "secret"


def test_a_failed_sysctl_load_is_a_refusal_with_what_was_written(roots):
    with pytest.raises(op.Refuse, match="sysctl -p"):
        op.drop(
            [str(roots[0]), "x.conf", "KD-1", "set", "a = 1"],
            load=lambda argv: (255, b"", b"unknown key"),
        )


def test_script_switch(tmp_path):
    flag = str(tmp_path / "script-off")
    assert "выключен" in op.script(["off"], flag) and (tmp_path / "script-off").exists()
    assert "без изменений" in op.script(["off"], flag)
    assert (
        "включён" in op.script(["on"], flag) and not (tmp_path / "script-off").exists()
    )
    assert "без изменений" in op.script(["on"], flag)
    with pytest.raises(op.Refuse):
        op.script(["maybe"], flag)


def test_reboot_writes_the_snapshot_then_reboots(roots):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[0] == "wg":
            return 127, b"", b"wg: not found"
        return 0, ("out of %s\n" % argv[0]).encode(), b""

    said = op.reboot(["KD-4"], run=run, stamp="20261117T120000Z")
    folder = roots[2] / "kd-4" / "before-reboot-20261117T120000Z"
    assert "перезагрузка запущена" in said and str(folder) in said
    assert (folder / "ss.txt").read_text() == "out of ss\n"
    assert (
        "exit 127" in (folder / "wg.txt").read_text()
    )  # a missing tool does not stop the reboot
    assert calls[-1] == ["systemctl", "reboot"] and len(calls) == len(op.SNAPSHOT) + 1
    with pytest.raises(OSError):  # one snapshot per moment: never over an earlier one
        op.reboot(["KD-4"], run=run, stamp="20261117T120000Z")


def test_a_reboot_that_systemctl_refuses_is_a_refusal(roots):
    def run(argv):
        return (1, b"", b"denied") if argv == ["systemctl", "reboot"] else (0, b"", b"")

    with pytest.raises(op.Refuse, match="systemctl reboot"):
        op.reboot(["KD-4"], run=run, stamp="x")
    with pytest.raises(op.Refuse):
        op.reboot([], run=run)


def test_main_reports_a_refusal_with_exit_2():
    out, err = io.StringIO(), io.StringIO()
    assert op.main(["reboot"], out, err) == 2 and "agent-typed-op" in err.getvalue()
