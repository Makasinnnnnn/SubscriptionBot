from copy import deepcopy
from itertools import count

import pytest_asyncio

from app.journal.config import JournalSettings
from app.journal.domain import INPUT_COLUMNS, ConflictError, SyncError, sheet_inputs
from app.journal.models import Base, Mutex
from app.journal.service import JournalService
from app.journal.store import Store


class FakeGoogle:
    def __init__(self):
        self.rows = {}
        self.photos = {}
        self.uploads = {}
        self.writes = 0
        self.timeout_after_write = False
        self.deny = False
        self.next_id = count(1)

    async def sync_trade(self, ident, payload, baseline):
        if self.deny:
            raise SyncError("Google API: HTTP 403.")
        desired = sheet_inputs(payload, "Asia/Tomsk")
        current = self.rows.get(ident, {str(c): "" for c in INPUT_COLUMNS})
        if current == desired:
            return deepcopy(desired)
        if current != baseline:
            raise ConflictError("Ручные изменения.")
        self.rows[ident] = deepcopy(desired)
        self.writes += 1
        if self.timeout_after_write:
            self.timeout_after_write = False
            raise SyncError("Timeout", True)
        return desired

    async def current_inputs(self, ident):
        return deepcopy(self.rows[ident])

    async def reserve_file_id(self):
        return "drive-" + str(next(self.next_id))

    async def reserve_screenshot_row(self, occupied):
        return next(i for i in range(6, 306) if i not in occupied)

    async def upload(self, shot):
        self.uploads.setdefault(shot.drive_id, shot.content)
        return "https://drive.example/" + shot.drive_id

    async def sync_screenshot(self, shot):
        self.photos[shot.drive_id] = shot.trade_id

    async def summary(self, ident):
        return [ident, "проп1", 200000, "USD", 0, 0, 200000, 0, 0, ""]


@pytest_asyncio.fixture
async def journal(tmp_path):
    settings = JournalSettings(_env_file=None, admin_ids=[1], trader_accounts={"2": ["1"], "3": ["2"]},
                               database_url=f"sqlite+aiosqlite:///{tmp_path / 'journal.db'}")
    store = Store(settings.database_url)
    async with store.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with store.sessions() as s, s.begin():
        s.add(Mutex(id=1))
    baseline = {str(c): "" for c in INPUT_COLUMNS}
    await store.bootstrap([{"id": "1", "name": "проп1", "initial": "200000", "currency": "USD"}],
                          [{"id": f"TR-{i:06}", "blocked": False, "baseline": baseline}
                           for i in range(1, 301)])
    service = JournalService(store, settings, FakeGoogle())
    yield service
    await store.close()


class Client:
    def __init__(self, service, user=1):
        self.service, self.user, self.response = service, user, None
        self.ids = count(1)

    async def text(self, text):
        self.response = await self.service.handle(self.user, f"u{self.user}:e{next(self.ids)}", text=text)
        return self.response

    async def click(self, action, value="", event_id=None):
        self.response = await self.service.handle(
            self.user, event_id or f"u{self.user}:e{next(self.ids)}", action=action, value=value,
            nonce=self.response["nonce"],
        )
        return self.response

    async def image(self, index=1):
        self.response = await self.service.handle(
            self.user, f"u{self.user}:e{next(self.ids)}", image={
                "file_id": f"telegram-{index}", "filename": "image.png", "mime": "image/png",
                "content": b"\x89PNG\r\n\x1a\n-test",
            },
        )
        return self.response


async def opening(client, *, stop=None, risk=None):
    await client.text("/start")
    await client.click("open")
    await client.text("btcusdt")
    await client.click("value", "Long")
    await client.click("value", "now")
    await client.text("100")
    if stop:
        await client.text(stop)
    else:
        await client.click("skip")
    await client.click("value", "5")
    await client.click("value", "suggested")
    await client.click("value", "risk" if risk else "quantity")
    if risk:
        await client.text(risk)
        await client.click("value", "accept_qty")
    else:
        await client.text("10")
    await client.click("skip")  # comment
    return client


async def open_trade(client):
    await opening(client)
    await client.click("next")
    return await client.click("save")
