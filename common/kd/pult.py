"""The owner's Telegram bot, now only kd-alert's duplicate of critical alerts (root reads the token).
The token is /etc/digipaddock/pult.env (root 0600); without the file nothing is duplicated. Text is plain: no
markup, so a reason with «<» or «&» reaches Telegram as it is."""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from kd.envfile import load_env

PULT_ENV = Path("/etc/digipaddock/pult.env")


def _safe(text: str) -> str:
    """Bytes that are not UTF-8 come as surrogates: urlencode fails on them — they become «�»."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def send(text: str, env_path=PULT_ENV, opener=urllib.request.urlopen) -> bool:
    """Send to the Pult. Never raises and never prints the token: a Telegram failure does not stop the caller."""
    try:
        env = load_env(env_path)
        data = urllib.parse.urlencode({
            "chat_id": env["PULT_CHAT_ID"], "text": _safe(text), "disable_web_page_preview": "true",
        }).encode()
        url = f"https://api.telegram.org/bot{env['PULT_BOT_TOKEN']}/sendMessage"
        with opener(url, data=data, timeout=10) as response:
            return json.load(response).get("ok") is True
    except Exception as exc:  # noqa: BLE001 — any Pult failure must not break the caller
        print(f"pult: message not sent ({exc.__class__.__name__})", file=sys.stderr)
        return False
