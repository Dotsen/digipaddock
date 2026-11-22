"""The dev cabinet's provider-key writer, confined to its state directory.

Tests enroll fixture public keys separately; no key is trusted from a browser request. This rootless
adapter runs the real signing protocol; only the filesystem writer and board connection are substituted.
"""

import os
import secrets
from pathlib import Path

from kd import llm_models, model_credentials, model_keys
from kd.cabinet import api


class DevModelKeys:
    def __init__(self, svc, state_dir):
        self.svc = svc
        self.root = Path(state_dir) / "model-keys"
        self.signers = self.root / "signers"
        self.env = self.root / "llm"
        self.signers.mkdir(parents=True, exist_ok=True)
        self.env.mkdir(parents=True, exist_ok=True)
        svc.ctx.llm_env_dir = str(self.env)
        svc.ctx.claude_env = str(self.root / "claude.env")  # the subscription models' «key» is this file
        self.service = model_keys.KeyService(
            svc.cfg,
            signers_dir=self.signers,
            write=self.write,
            remove=self.remove,
            audit=self.audit,
            status=lambda: model_credentials.subscription_status(self.root / "claude.env"),
        )

    def write(self, provider, value):
        path = self.root / "claude.env" if provider == model_credentials.SUBSCRIPTION else Path(
            llm_models.env_file(provider, env_dir=str(self.env)))
        temporary = path.with_name(".key-" + secrets.token_hex(8))
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                os.chmod(temporary, 0o600)
                handle.write(model_credentials.variable(provider) + "=" + value + "\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"restart": True} if provider == model_credentials.SUBSCRIPTION else {}

    def remove(self, provider):
        Path(llm_models.env_file(provider, env_dir=str(self.env))).unlink(
            missing_ok=True
        )

    def audit(self, user, provider, action):
        api.dispatch(
            self.svc.ctx,
            api.Principal("root", 0, "root"),
            {
                "op": "llm.key.changed",
                "user": user,
                "provider": provider,
                "action": action,
            },
        )

    async def call(self, request):
        return await self.svc.locked(lambda: self.service.handle(request))
