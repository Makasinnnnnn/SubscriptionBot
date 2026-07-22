"""Application entrypoint.

Run mode is controlled by APP_MODE:
- api: FastAPI app with payment webhooks
- bot: Telegram bot polling
- scheduler: background subscription cleanup jobs
"""

import asyncio
import os

from app.api.main import app  # noqa: F401
from app.bot.runner import run_bot
from app.tasks.scheduler import run_scheduler


def main() -> None:
    mode = os.getenv("APP_MODE", "api").lower()
    if mode == "bot":
        asyncio.run(run_bot())
        return
    if mode == "scheduler":
        asyncio.run(run_scheduler())
        return
    raise RuntimeError("Use `uvicorn main:app` for APP_MODE=api")


if __name__ == "__main__":
    main()
