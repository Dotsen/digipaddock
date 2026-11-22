"""After a write the service tells every consumer, each on its own."""

import asyncio

from cabinet_env import CabinetEnv

from kd.cabinet import config, decisions, service


class Hub:
    def __init__(self):
        self.published = []

    def publish(self, events):
        self.published.extend(events)


class Boom:
    def catch_up(self, decisions):
        raise RuntimeError("the push queue is broken")


class Quiet:
    def catch_up(self, decisions):
        pass


def _service(tmp_path, notifier):
    env = CabinetEnv(tmp_path)
    changed = []
    svc = service.Service(
        env.ctx,
        config.load("/none", "/none", dev=True),
        users=env.users,
        decisions=decisions.Decisions(env.store),
        notifier=notifier,
        now=lambda: env.now,
        on_change=lambda: changed.append(1),
    )
    svc.hub = Hub()
    return env, svc, changed


def test_a_failing_notifier_does_not_silence_the_pages_or_sync(tmp_path, capsys):
    env, svc, changed = _service(tmp_path, Boom())
    card = env.card(project="web")
    asyncio.run(
        svc.locked(lambda: env.store.add_message(card.id, author="user:owner", kind="text", body="x"))
    )
    assert [e["kind"] for e in svc.hub.published] == ["message.added"]
    assert changed == [1]
    assert "the push queue is broken" in capsys.readouterr().err


def test_more_than_one_page_of_new_events_is_published(tmp_path):
    env, svc, changed = _service(tmp_path, Quiet())
    card = env.card(project="web")

    def burst():
        for n in range(501):
            env.store.add_message(card.id, author="agent:dev", kind="text", body=str(n))

    asyncio.run(svc.locked(burst))
    assert len(svc.hub.published) == 501
    assert svc.hub.published[-1]["seq"] == env.store.last_seq()
    assert changed == [1]
