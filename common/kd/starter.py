"""The optional install step ``bootstrap.sh starter``: a minimal start on an empty board.

``common/install/starter.json`` holds a project of the owner's (``start``, route ``@owner``) and a few cards in it,
each in English and Russian: a tour of the cabinet, the first task for the analyst, the first server for the
sysadmin, a partner. A card that ``requires`` a role comes only on a host whose install.json has that role. The
cards are the owner's own To Do with acceptance items to tick; no agent is asked to do anything, and none of them
runs a command or grants a right.

The language is the first owner's in the cabinet, else the deployment's (host.json owner_language). Each card carries the labels
``starter`` and ``starter:<key>``: ``check`` exits 1 while one of the host's cards is missing, ``apply`` makes the
project when it is not there and the missing cards, so a run again adds nothing and a run cut short is finished
by the next. Whatever the owner did with the cards since (Done, edited, moved) counts as there.
"""

import json
import os
import sys

from kd import install_spec, naming

DATA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "install",
    "starter.json",
)
LABEL = "starter"
LANGUAGES = ("en", "ru")
def default_language() -> str:
    """The deployment's owner language (host.json): the cabinet's language for a person who chose none."""
    return naming.load_host()["owner_language"]


def load(path=DATA) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema_version") != 1 or not isinstance(data.get("cards"), list):
        raise ValueError(
            "{}: expected {{schema_version: 1, project, cards}}".format(path)
        )
    return data


def wanted(data: dict, roles) -> list:
    """The cards this host gets: those without ``requires`` and those whose role it has."""
    return [c for c in data["cards"] if not c.get("requires") or c["requires"] in roles]


def language(client) -> str:
    """The first owner's language (by id), else the deployment's owner language."""
    people = client.call({"op": "user.list"})["users"]
    for person in sorted(people, key=lambda p: p["id"]):
        if (
            any(role == "owner" for role, _ in person.get("roles", ()))
            and person.get("language") in LANGUAGES
        ):
            return person["language"]
    return default_language()


def missing(client, cards: list) -> list:
    have = {
        label
        for card in client.call({"op": "card.list", "label": LABEL})["cards"]
        for label in card.get("labels", ())
    }
    return [c for c in cards if "{}:{}".format(LABEL, c["key"]) not in have]


def apply(client, data: dict, cards: list, lang: str) -> list:
    """The project when absent, then each missing card in To Do with its acceptance items; their ids."""
    project = data["project"]
    if project["id"] not in {
        p["id"] for p in client.call({"op": "project.list"})["items"]
    }:
        client.call(
            {
                "op": "project.create",
                "id": project["id"],
                "name": project["name"][lang],
                "route": project["route"],
            }
        )
    # card.create takes no labels: a run stopped between create and edit left an Inbox card without them
    unlabelled = [
        c
        for c in client.call({"op": "card.list", "project": project["id"], "status": "Inbox"})["cards"]
        if LABEL not in c.get("labels", ())
    ]
    made = []
    for card in missing(client, cards):
        text = lambda field: naming.rewrite(card[field][lang])  # noqa: E731 — the host's command and card names
        titles = {naming.rewrite(card["title"][other]) for other in LANGUAGES}
        created = next((c for c in unlabelled if c["title"] in titles), None)
        if created is not None:
            unlabelled.remove(created)
        else:
            created = client.call(
                {
                    "op": "card.create",
                    "project": project["id"],
                    "assignee": project["route"],
                    "title": text("title"),
                    "description": text("description"),
                }
            )["card"]
        client.call(
            {
                "op": "card.edit",
                "card": created["id"],
                "revision": created["revision"],
                "changes": {
                    "status": "To Do",
                    "labels": [LABEL, "{}:{}".format(LABEL, card["key"])],
                    "acceptance": [
                        {"text": naming.rewrite(a[lang]), "checked": False}
                        for a in card.get("acceptance", ())
                    ],
                },
            }
        )
        made.append(created["id"])
    return made


def main(
    action, install_json, *, client=None, out=None, err=None, data_path=DATA
) -> int:
    """``check`` or ``apply`` for the roles of ``install_json`` (the release tree's); 1 — a card is missing (check)
    or the cabinet refused (apply)."""
    from kd.cabinet import client as cabinet_client  # noqa: PLC0415 — the cabinet's client only on the host

    out, err = out or sys.stdout, err or sys.stderr
    data = load(data_path)
    roles = install_spec.load_install(install_json).roles
    cards = wanted(data, roles)
    client = client or cabinet_client.Client()
    try:
        if action == "check":
            absent = missing(client, cards)
            for card in absent:
                err.write("starter card {}: not on the board\n".format(card["key"]))
            return 1 if absent else 0
        for card_id in apply(client, data, cards, language(client)):
            out.write("starter card {}\n".format(card_id))
    except cabinet_client.CabinetError as exc:
        err.write("cabinet: {}\n".format(exc))
        return 1
    return 0
