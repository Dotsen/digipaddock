import io
import json
import urllib.parse

from kd import pult


class Opener:
    def __init__(self, body=b'{"ok": true}', raise_exc=None):
        self.calls, self.body, self.raise_exc = [], body, raise_exc

    def __call__(self, url, data=None, timeout=None):
        self.calls.append((url, data, timeout))
        if self.raise_exc:
            raise self.raise_exc
        return io.BytesIO(self.body)


def env_file(tmp_path, token="123:abc", chat="42"):
    p = tmp_path / "pult.env"
    p.write_text(f"# токен\nPULT_BOT_TOKEN={token}\nPULT_CHAT_ID = {chat}\n", encoding="utf-8")
    return p


def test_default_env_path_is_the_products():
    assert pult.PULT_ENV.as_posix() == "/etc/digipaddock/pult.env"


def test_load_env_strips_and_skips_comments(tmp_path):
    assert pult.load_env(env_file(tmp_path)) == {"PULT_BOT_TOKEN": "123:abc", "PULT_CHAT_ID": "42"}


def test_send_posts_plain_text_to_chat(tmp_path):
    op = Opener()
    assert pult.send("привет <b>", env_path=env_file(tmp_path), opener=op) is True
    url, data, timeout = op.calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage" and timeout == 10
    form = urllib.parse.parse_qs(data.decode())
    assert form["chat_id"] == ["42"] and form["text"] == ["привет <b>"] and "parse_mode" not in form


def test_send_never_raises(tmp_path, capsys):
    assert pult.send("x", env_path=tmp_path / "нет", opener=Opener()) is False
    assert pult.send("x", env_path=env_file(tmp_path), opener=Opener(raise_exc=OSError("net"))) is False
    assert pult.send("x", env_path=env_file(tmp_path), opener=Opener(body=b'{"ok": false}')) is False
    assert "123:abc" not in capsys.readouterr().err  # the token is not printed to stderr


def test_safe_replaces_surrogates():
    assert pult._safe("a\udcffb") == "a�b"

