"""Secret-free failures around the provider-key socket."""

from kd import model_keys
from kd.cabinet.review_client import ReviewClient, ReviewRefused, ReviewUnavailable


class ModelKeysClient:
    def __init__(self, path=model_keys.SOCKET):
        self.client = ReviewClient(path)

    async def call(self, request: dict) -> dict:
        try:
            return await self.client.call(request)
        except ReviewRefused as exc:
            # Root returns only fixed strings; never reflect a transport's exception/request body.
            kind = (
                exc.kind
                if exc.kind
                in ("invalid", "refused", "stepup_required", "setup_required")
                else "unavailable"
            )
            raise model_keys.Refused(
                kind, "provider key signature or operation was refused"
            ) from None
        except ReviewUnavailable:
            raise model_keys.Refused(
                "unavailable", "provider key service is unavailable"
            ) from None
