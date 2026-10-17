"""Preview shell for the early-vote read API.

The FastAPI handler lands in a later change. This module only marks the
preview Worker as up. It does not read roster rows.
"""

from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response.json(
            {
                "service": "tx-turnout-ev-api",
                "environment": "preview",
                "status": "placeholder",
            }
        )
