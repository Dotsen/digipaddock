"""Shared samples of the admin tests: an admin card, an operations catalog and a proposal."""


def md_card(text=None, revision=1):
    """The admin card of an md sample as the cabinet holds it after the cut-over import."""
    import legacy_md
    from kd import admin_task

    record, _, _ = legacy_md.parse_card((CARD if text is None else text).encode("utf-8"))
    return admin_task.from_record({**record, "revision": revision})


CARD = """---
id: KD-123
title: 'node-c: restart the app workers'
status: To Do
assignee:
  - '@sysadmin'
created_date: '2026-10-09 16:26'
updated_date: '2026-10-09 16:26'
labels:
  - 'server:node-c'
  - 'server:node-e'
  - P-35
dependencies: []
project: admin
ordinal: 183000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Restart the app workers after the config change.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 workers are active
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 checked with ssh-ro
<!-- DOD:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
[sysadmin] started
<!-- SECTION:NOTES:END -->
"""

CATALOG = {
    "schema_version": 1,
    "verbs": ["package.refresh", "service.restart", "service.start", "package.install"],
    "servers": {
        "node-c": {"packages": "apt", "install": True, "units": ["nginx.service", "app-worker@alpha.service"]},
        "node-g": {"packages": "dnf", "install": True, "units": []},
        "node-e": {"packages": "apt", "install": False, "units": []},
    },
}
INVENTORY = {"node-c", "node-g", "node-e", "example-prod", "node-l"}
ENABLED = {"node-c", "node-g", "node-e", "example-prod"}


def explain(op, **over):
    """A plan step with the owner's explanations (schema 2)."""
    data = {
        **op,
        "what": "Перезапустить веб-сервер",
        "effect": "Сайт недоступен пару секунд",
        "risk": {"level": "low", "why": "Конфигурация не меняется"},
        "rollback": "Не нужен",
    }
    data.update(over)
    return data


def proposal(**over):
    data = {
        "schema_version": 2,
        "task_id": "KD-123",
        "intent": "Обновить индексы пакетов и перезапустить nginx",
        "basis": "ssh-ro: nginx отдаёт 502",
        "operations": [
            explain({"server": "node-c", "verb": "package.refresh"}, what="Обновить индексы пакетов"),
            explain({"server": "node-c", "verb": "service.restart", "unit": "nginx.service"}),
        ],
    }
    data.update(over)
    return data


def action(**over):
    """A critical action (schema 2) on node-c."""
    data = {
        "schema_version": 2,
        "task_id": "KD-123",
        "intent": "Убрать пир",
        "basis": "wg show: пир не отвечает",
        "servers": ["node-c"],
        "command": "wg set wg0 peer 'abc=' remove",
        "effect": "Пир отключён",
        "risk": {"level": "high", "why": "Связь с пиром пропадёт"},
        "rollback": "Вернуть пир из резервной копии",
    }
    data.update(over)
    return data


# a catalog with a test environment on node-c and a plan whose every step belongs to it
ZONE_CATALOG = {
    **CATALOG,
    "verbs": CATALOG["verbs"] + ["service.reload", "app.tool", "app.flag"],
    "apps": {
        "shop-test": {
            "server": "node-c", "user": "www-data", "dir": "/var/www/shop.example.test", "php": "php",
            "spark": ["migrate", "migrate:status"],
            "flags": {"file": ".env", "format": "env", "keys": {"CI_ENVIRONMENT": ["development", "production"]}},
            "test": True, "reload": ["nginx.service"],
        },
    },
}
ZONE_STEPS = [
    explain({"server": "node-c", "verb": "app.tool", "app": "shop-test", "tool": "spark",
             "args": ["migrate:status"]}, what="Показать миграции стенда"),
    explain({"server": "node-c", "verb": "service.reload", "unit": "nginx.service"}, what="Перечитать конфиг"),
]


def zone_proposal(**over):
    """A plan of the test zone: every step is of the test environment shop-test."""
    return proposal(**{"intent": "Миграции тестового стенда", "basis": "просьба dev",
                       "operations": list(ZONE_STEPS), **over})
