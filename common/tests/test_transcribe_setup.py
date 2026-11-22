"""transcribe_setup.sh, kd-transcribe's units, the socket client and the lock builder."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from kd import transcribe_client as client
from kd import transcribe_lock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "common" / "install" / "transcribe_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")
SYSTEMD = ROOT / "common" / "systemd"
BASH = shutil.which("bash")


def _mode(mode: str) -> str:
    cases = TEXT[TEXT.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def _lines(name: str) -> list[str]:
    return (SYSTEMD / name).read_text(encoding="utf-8").splitlines()


# --- units -------------------------------------------------------------------------------------------------------


def test_the_socket_starts_a_process_per_connection_for_the_cabinet_only():
    socket_unit = _lines("digipaddock-transcribe.socket")
    for line in ("ListenStream=/run/digipaddock-transcribe/transcribe.sock", "Accept=yes", "SocketUser=root",
                 "SocketGroup=@cabinet@", "SocketMode=0660", "DirectoryMode=0755", "WantedBy=sockets.target"):
        assert line in socket_unit, line


def test_the_instance_has_no_network_and_writes_nothing():
    service = _lines("digipaddock-transcribe@.service")
    for line in ("User=@transcribe@", "StandardInput=socket", "StandardOutput=socket", "StandardError=journal",
                 "PrivateNetwork=yes", "IPAddressDeny=any", "RestrictAddressFamilies=AF_UNIX", "ProtectSystem=strict",
                 "ProtectHome=yes", "PrivateTmp=yes", "NoNewPrivileges=yes", "CapabilityBoundingSet=",
                 "Environment=HOME=/nonexistent LANG=C.UTF-8 PATH=/usr/bin:/bin HF_HUB_OFFLINE=1"):
        assert line in service, line
    assert not [line for line in service if line.startswith(("ReadWritePaths", "Restart="))]
    start = next(line for line in service if line.startswith("ExecStart="))
    assert start.startswith("ExecStart=/var/lib/digipaddock/venv/transcribe/bin/python -I "
                            "/opt/digipaddock/common/bin/kd-transcribe ")
    assert "--model medium " in start  # the owner's decision of 22.10.2026
    assert "--models-dir /var/lib/digipaddock/transcribe/models" in start
    # the deployment's vocabulary: an overlay file under host/, optional
    assert "--synonyms /opt/digipaddock/host/transcribe-synonyms.json" in start


def test_the_cabinet_reaches_the_socket():
    cabinet = (SYSTEMD / "digipaddock-cabinet.service").read_text(encoding="utf-8").splitlines()
    assert "ReadWritePaths=-/run/digipaddock-transcribe" in cabinet  # ProtectSystem=strict hides /run otherwise


def test_render_host_rerenders_the_units_where_installed():
    render = (ROOT / "common" / "install" / "render_host.sh").read_text(encoding="utf-8")
    present = re.search(r'^PRESENT="([^"]*)"', render, re.M).group(1).split()
    assert "common/systemd/digipaddock-transcribe.socket" in present and "common/systemd/digipaddock-transcribe@.service" in present


# --- installer ---------------------------------------------------------------------------------------------------


def test_install_takes_only_the_hash_lock():
    assert 'transcribe=$(kd_user transcribe)' in TEXT and 'cabinet=$(kd_user cabinet)' in TEXT
    assert re.search(r'useradd --system [^\n]*--shell /usr/sbin/nologin "\$transcribe"', TEXT)
    install = TEXT.split("install_all() {", 1)[1].split("\n}", 1)[0]
    assert "grep -q -- '--hash=sha256:' \"$REQ\"" in install  # no lock, no install
    assert '--require-hashes --only-binary=:all: -r "$REQ"' in install
    assert 'install -d -o root -g "$transcribe" -m 0750 "$MODELS"' in install
    assert 'chmod -R u=rwX,g=rX,o= "$MODELS"' in install  # the executor reads the model, never writes it
    assert "systemctl enable digipaddock-transcribe.socket" in install
    assert "systemctl restart digipaddock-transcribe.socket" in install  # new socket settings take effect
    assert 'rm -rf "$VENV"' in install  # a new lock builds a fresh venv
    assert "install_all" in _mode("install") and "check" in _mode("install")


def test_check_proves_the_whole_path_without_network():
    body = TEXT.split("check() {", 1)[1].split("\n}", 1)[0]
    for needle in ('.requirements.sha256', 'load_model "$transcribe"', 'kd_render "$REPO/common/systemd/$unit"',
                   "systemctl is-enabled --quiet digipaddock-transcribe.socket",
                   'runuser -u "$cabinet" -- python3 -I "$CLIENT" smoke'):
        assert needle in body, needle
    assert "HF_HUB_OFFLINE=1" in TEXT.split("load_model() {", 1)[1].split("\n}", 1)[0]


def test_disable_keeps_the_user_and_the_model():
    disable = _mode("disable")
    assert "systemctl disable --now digipaddock-transcribe.socket" in disable
    assert "userdel" not in TEXT and "rm " not in disable


def test_lock_changes_nothing_on_the_host():
    body = TEXT.split("build_lock() {", 1)[1].split("\n}", 1)[0]
    assert "mktemp -d" in body and "trap \"rm -rf '$tmp'\" EXIT" in body  # expanded at once: the local dies
    assert "$VENV" not in body and "$UNITS" not in body and "useradd" not in body
    assert "transcribe_lock.py" in body and "--only-binary=:all:" in body
    assert "freeze --all" in body and "grep -v -i '^pip=='" in body  # setuptools/wheel locked if needed, not pip


def test_the_lock_holds_the_wheels_of_both_machines():
    """The empty-host run on aarch64: the lock had the x86_64 wheels only and pip refused av's hash. The
    builder downloads both machines' wheels (glibc of manylinux2014 up to Ubuntu 24.04's) into one lock."""
    body = TEXT.split("build_lock() {", 1)[1].split("\n}", 1)[0]
    assert "for arch in x86_64 aarch64; do" in body
    assert '--platform "manylinux2014_$arch"' in body and 'manylinux_2_${glibc}_$arch' in body and "seq 17 39" in body
    assert '--python-version "$py" --implementation cp --abi "cp$py"' in body
    lock = (ROOT / "common" / "transcribe" / "requirements.txt").read_text(encoding="utf-8")
    assert "x86_64 and aarch64" in lock.split("\n", 2)[1]
    # av 14.2.0, cp312, manylinux_2_17_aarch64 — the wheel pip refused on the aarch64 host
    assert "897be9a665c365dfcf0c10a257fe223521ed4d3b478e6b258f55f7cd13fdedd3" in lock


def test_the_lock_files():
    requirements_in = (ROOT / "common" / "transcribe" / "requirements.in").read_text(encoding="utf-8")
    assert "faster-whisper==1.2.1" in requirements_in and "av==14.2.0" in requirements_in
    lock = (ROOT / "common" / "transcribe" / "requirements.txt").read_text(encoding="utf-8")
    pins = [line for line in lock.splitlines() if line and not line.startswith(("#", " "))]
    # Either the placeholder (install refuses it) or a full lock: every pin with its hash.
    assert not pins or (all("==" in pin for pin in pins) and lock.count("--hash=sha256:") >= len(pins))


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.parametrize("args", [[], ["bogus"], ["install", "x"], ["lock", "--force"]])
def test_usage(args):
    proc = subprocess.run([BASH, SCRIPT.as_posix(), *args], capture_output=True, timeout=30)
    assert proc.returncode == 2 and b"usage" in proc.stderr


# --- the lock builder ----------------------------------------------------------------------------------------------


def test_lock_hashes_every_frozen_wheel(tmp_path):
    (tmp_path / "faster_whisper-1.2.1-py3-none-any.whl").write_bytes(b"fw")
    (tmp_path / "PyYAML-6.0.2-cp312-cp312-manylinux_2_17_x86_64.whl").write_bytes(b"yaml")
    text = transcribe_lock.lock("faster-whisper==1.2.1\n# comment\npyyaml==6.0.2\n", tmp_path)
    assert text == (
        f"faster-whisper==1.2.1 \\\n    --hash=sha256:{hashlib.sha256(b'fw').hexdigest()}\n"
        f"pyyaml==6.0.2 \\\n    --hash=sha256:{hashlib.sha256(b'yaml').hexdigest()}\n"
    )


@pytest.mark.parametrize("frozen", ["numpy==2.1.0\n", "numpy>=2\n", ""])
def test_lock_is_all_or_nothing(tmp_path, frozen):
    with pytest.raises(ValueError):
        transcribe_lock.lock(frozen, tmp_path)


# --- the socket client ---------------------------------------------------------------------------------------------


def _server(path: Path, reply: bytes):
    received = {}
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        with conn:
            data = b""
            while chunk := conn.recv(65536):  # to EOF: the client half-closes
                data += chunk
            received["data"] = data
            conn.sendall(reply)
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return received, thread


@pytest.fixture
def sock_path():
    # a short path: AF_UNIX paths are limited to ~100 bytes and pytest's tmp_path can be longer
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="kdt"))
    yield folder / "s"
    shutil.rmtree(folder, ignore_errors=True)


def test_ask_sends_the_header_and_the_recording_and_reads_one_line(sock_path):
    received, thread = _server(sock_path, b'{"status": "ok", "text": "\xd0\x9e\xd0\xba"}\n')
    answer = client.ask(b"RIFFdata", {"language": "ru"}, path=str(sock_path), timeout=5)
    thread.join(5)
    assert answer == {"status": "ok", "text": "Ок"}
    assert received["data"] == b'{"language": "ru"}\nRIFFdata'


@pytest.mark.parametrize("reply", [b"", b"not json\n", b"[1]\n", b'{"text": "no status"}\n'])
def test_ask_without_an_answer_line_is_an_error(sock_path, reply):
    _, thread = _server(sock_path, reply)
    with pytest.raises(client.TranscribeError):
        client.ask(b"x", {}, path=str(sock_path), timeout=5)
    thread.join(5)


def test_ask_without_the_executor_is_an_error(sock_path):
    with pytest.raises(client.TranscribeError):
        client.ask(b"x", {}, path=str(sock_path), timeout=1)


def test_smoke_is_a_probe_that_loads_the_model(sock_path, capsys):
    received, thread = _server(sock_path, b'{"status": "probe_ok", "text": ""}\n')
    assert client.main(["smoke", str(sock_path)]) == 0
    thread.join(5)
    header = json.loads(received["data"].split(b"\n", 1)[0])
    assert header == {"language": "ru", "probe": True}
    sock_path.unlink()
    _, thread = _server(sock_path, b'{"status": "empty", "text": ""}\n')  # VAD only: the model never loaded
    assert client.main(["smoke", str(sock_path)]) == 1
    thread.join(5)
    assert "expected probe_ok" in capsys.readouterr().err
    assert client.main(["bogus"]) == 2


def test_silence_is_a_wav_second():
    import wave
    import io

    with wave.open(io.BytesIO(client.silence(1.0)), "rb") as wav:
        assert (wav.getframerate(), wav.getnframes(), wav.getnchannels()) == (16000, 16000, 1)
