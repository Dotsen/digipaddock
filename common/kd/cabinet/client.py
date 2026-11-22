"""Clients of the cabinet socket: the executors, root timers and installer talk to the board here."""

import json
import socket
import sys

from kd import admin_task, boss_board
from kd.cabinet import api, server


class CabinetError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def exit_code(error: CabinetError) -> int:
    return api.ERROR_CODES.get(error.kind, 1)


def encode(request: dict) -> bytes:
    """A request as one line on the socket (the server takes up to ``server.MAX_REQUEST`` bytes)."""
    return json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"


class Client:
    def __init__(self, socket_path=server.SOCKET, timeout=server.TIMEOUT):
        self.path, self.timeout = socket_path, timeout

    def call(self, request: dict) -> dict:
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            raise CabinetError(
                "unavailable", "board unavailable (no unix sockets on this system)"
            )
        conn = socket.socket(family, socket.SOCK_STREAM)
        try:
            conn.settimeout(self.timeout)
            conn.connect(self.path)
            conn.sendall(encode(request))
            line = conn.makefile("rb").readline()
        except (OSError, socket.timeout) as exc:
            raise CabinetError(
                "unavailable", f"board unavailable ({exc.__class__.__name__})"
            ) from exc
        finally:
            conn.close()
        return _unwrap(line)


def _unwrap(line: bytes) -> dict:
    try:
        response = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CabinetError("unavailable", "board answered nonsense") from exc
    if not isinstance(response, dict):
        raise CabinetError("unavailable", "board answered nonsense")
    if not response.get("ok"):
        raise CabinetError(
            response.get("error", "unavailable"), response.get("message", "board error")
        )
    return {k: v for k, v in response.items() if k != "ok"}


class LocalClient:
    """In-process dispatch for tests and for the service itself; JSON in and out, as on the socket."""

    def __init__(self, ctx, principal):
        self.ctx, self.principal = ctx, principal

    def call(self, request: dict) -> dict:
        try:
            response = api.dispatch(
                self.ctx, self.principal, json.loads(json.dumps(request))
            )
        except api.ApiError as exc:
            raise CabinetError(exc.kind, str(exc)) from exc
        return {k: v for k, v in json.loads(json.dumps(response)).items() if k != "ok"}


def boss_card(client, card_id: str) -> boss_board.Card:
    return boss_board.card_from(
        client.call({"op": "card.get", "card": card_id})["card"]
    )


def admin_card(client, card_id: str) -> admin_task.Card:
    """The sysadmin's card as the executors check it; CardError keeps its meaning for callers."""
    try:
        record = client.call(
            {"op": "card.get", "card": admin_task.check_task_id(card_id)}
        )["card"]
    except CabinetError as exc:
        raise admin_task.CardError(f"{card_id}: {exc}") from exc
    return admin_task.from_record(record)


def post_notice(source: str, level: str, key: str, params: dict, card: str = "", client=None) -> bool:
    """A notice in the cabinet; never raises: a refusal or an absent cabinet is one stderr line and False,
    so a failed notice never fails the work it reports."""
    request = {"op": "notice.post", "source": source, "level": level, "key": key, "params": params}
    if card:
        request["card"] = card
    try:
        (client or Client()).call(request)
        return True
    except (CabinetError, OSError, ValueError) as exc:
        kind = getattr(exc, "kind", exc.__class__.__name__)
        print(f"kd-notice: {source} {key}{' on ' + card if card else ''} not posted ({kind}: {exc})", file=sys.stderr)
        return False


def publish_pr(pr: dict, client=None) -> dict:
    """The forge executor's PR on the board: ``{pr, decision, council}`` — ``decision`` None while the
    card's council of the code reads this head; raises ``CabinetError``."""
    return (client or Client()).call({"op": "pr.publish", "pr": pr})


def devcheck_request(request: dict, client=None) -> dict:
    """kd-forge check's dev.check: ``{operation, created}``; raises ``CabinetError``."""
    return (client or Client()).call(request)


def pr_state(repo: str, number: int, state: str, client=None) -> dict:
    """A PR merged or closed: ``{pr}``; raises ``CabinetError`` (``not_found`` for a PR never published)."""
    return (client or Client()).call({"op": "pr.state", "repo": repo, "number": number, "state": state})


def projects(client) -> list:
    """The board's projects the caller sees (the cabinet's table is the only source): ``{id, name, route,
    repo}`` by id; raises ``CabinetError`` — an absent cabinet is never an empty board."""
    return client.call({"op": "project.list"})["items"]


def routes(client) -> dict:
    """project -> route (``@agent`` or ``@owner``) of the projects the caller sees; raises ``CabinetError``."""
    return {p["id"]: p["route"] for p in projects(client)}


def records(client, **filters) -> list:
    return client.call(
        {"op": "card.list", **{k: v for k, v in filters.items() if v is not None}}
    )["cards"]
