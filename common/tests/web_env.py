"""The cabinet's HTTP app over a test store: ``WebEnv(tmp_path)`` with a logged-in helper."""

from cabinet_env import CabinetEnv
from fastapi.testclient import TestClient

from kd.cabinet import config, decisions, notify, service
from kd.cabinet.web import app as web_app
from kd.cabinet.web import ws


class WebEnv:
    def __init__(self, tmp_path, env=None):
        self.env = env or CabinetEnv(tmp_path)  # a test that set its cabinet up brings it
        self.cfg = config.load("/none", "/none", dev=True, files_dir=str(tmp_path / "files"))
        self.users = self.env.users
        self.svc = service.Service(
            self.env.ctx,
            self.cfg,
            users=self.users,
            decisions=decisions.Decisions(self.env.store),
            notifier=notify.Notifier(self.env.store, self.users, self.cfg),
            now=lambda: self.env.now,
        )
        self.svc.hub = ws.Hub(self.svc)
        # the page's requests carry its Origin — sign-in included (global review 15.10.2026)
        self.client = TestClient(
            web_app.create_app(self.svc), base_url=self.cfg.origin, headers={"Origin": self.cfg.origin}
        )
        self.csrf = ""

    def login(self, user="owner", client=None):
        client = client or self.client
        code = self.users.issue_code(user, self.env.now)
        r = client.post("/api/auth/code", json={"user": user, "code": code})
        assert r.status_code == 200, r.text
        # another browser keeps its own token (its ``me["csrf"]``)
        if client is self.client:
            self.csrf = r.json()["csrf"]
        return r.json()

    def headers(self, csrf=None):
        return {"X-CSRF-Token": csrf or self.csrf, "Origin": self.cfg.origin}

    def other_client(self):
        """A second browser (its own cookie jar) on the same app."""
        return TestClient(self.client.app, base_url=self.cfg.origin, headers={"Origin": self.cfg.origin})
