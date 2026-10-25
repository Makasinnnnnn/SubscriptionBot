import asyncio

import pytest
from sqlalchemy import func, select

from app.journal.bot import keyboard
from app.journal.models import Draft, Event, Job, Screenshot, Slot, Trade
from app.journal.service import JournalService, SyncWorker
from app.journal.store import Store
from journal_tests.conftest import Client, open_trade, opening


async def drain(service, limit=30):
    worker = SyncWorker(service)
    for _ in range(limit):
        if not await worker.once():
            return
    pytest.fail("worker did not become idle")


async def test_open_close_and_actual_zero(journal):
    client = Client(journal)
    result = await open_trade(client)
    assert "TR-000001" in result["text"]
    await drain(journal)
    await client.click("view", "TR-000001")
    await client.click("close", "TR-000001")
    await client.click("value", "now")
    await client.text("110")
    await client.click("value", "actual")
    await client.text("0")
    await client.click("skip")
    await client.click("next")
    assert "Чистый PnL: $0.00" in client.response["text"]
    await client.click("save")
    await drain(journal)
    row = journal.google.rows["TR-000001"]
    assert row["14"] == 0
    assert row["13"] == ""
    assert row["5"] != ""


async def test_quantity_by_risk_and_restart(journal):
    await opening(Client(journal), stop="90", risk="1")
    async with journal.store.sessions() as s:
        draft = await s.get(Draft, 1)
        assert draft.payload["data"]["quantity"] == "200"
    new_store = Store(journal.settings.database_url)
    try:
        restarted = JournalService(new_store, journal.settings, journal.google)
        response = await restarted.handle(1, "after-restart", text="/start")
        assert "сохранённый черновик" in response["text"]
        response = await restarted.handle(1, "resume", action="resume", nonce=response["nonce"])
        assert "скриншоты" in response["text"]
    finally:
        await new_store.close()


async def test_two_users_and_double_callbacks(journal):
    one, two = Client(journal, 1), Client(journal, 2)
    await asyncio.gather(opening(one), opening(two))
    await asyncio.gather(one.click("next"), two.click("next"))
    nonce = one.response["nonce"]
    first, _second = await asyncio.gather(one.click("save", event_id="first-save"), two.click("save"))
    duplicate = await journal.handle(1, "first-save", action="save", nonce=nonce)
    stale = await journal.handle(1, "second-press", action="save", nonce=nonce)
    assert duplicate == first
    assert "устарела" in stale["text"]
    async with journal.store.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Trade)) == 2
        ids = set((await s.scalars(select(Trade.id))).all())
        assert ids == {"TR-000001", "TR-000002"}


async def test_album_and_private_upload_stages(journal):
    client = await opening(Client(journal))
    await client.image(1)
    await client.image(2)
    await client.click("next")
    await client.click("save")
    await drain(journal)
    assert len(journal.google.photos) == 2
    assert len(journal.google.uploads) == 2
    async with journal.store.sessions() as s:
        shots = (await s.scalars(select(Screenshot))).all()
        assert all(shot.drive_url and shot.content is None for shot in shots)
        assert len({s.sheet_row for s in shots}) == 2
        assert journal.google.rows["TR-000001"]["25"].startswith("https://drive.example/")


async def test_timeout_after_remote_commit_and_retry(journal):
    await open_trade(Client(journal))
    journal.google.timeout_after_write = True
    worker = SyncWorker(journal)
    await worker.once()
    async with journal.store.locked() as s:
        job = await s.get(Job, "trade:TR-000001:1")
        assert job.status == "pending" and job.attempts == 1
        job.next_at = 0
    await drain(journal)
    assert journal.google.writes == 1


async def test_manual_conflict_requires_explicit_confirmation(journal):
    client = Client(journal)
    await open_trade(client)
    journal.google.rows["TR-000001"] = {str(c): "" for c in range(26)}
    journal.google.rows["TR-000001"]["23"] = "manual edit"
    await drain(journal)
    assert journal.google.rows["TR-000001"]["23"] == "manual edit"
    await client.click("sync")
    assert "Конфликт" in client.response["text"]
    await client.click("conflict", "trade:TR-000001:1")
    assert "manual edit" in client.response["text"]
    await client.click("resolve", "trade:TR-000001:1")
    await drain(journal)
    assert journal.google.rows["TR-000001"]["23"] == ""


async def test_permissions_and_foreign_trade_edit(journal):
    denied = await journal.handle(100, "unauthorized", text="/start")
    assert "Доступ закрыт" in denied["text"]
    async with journal.store.sessions() as s:
        assert await s.get(Event, "unauthorized") is None
    await open_trade(Client(journal, 1))
    trader = Client(journal, 2)
    await trader.text("/start")
    await trader.click("view", "TR-000001")
    await trader.click("close", "TR-000001")
    assert "чужую" in trader.response["text"]
    other = Client(journal, 3)
    await other.text("/start")
    await other.click("view", "TR-000001")
    assert "недоступна" in other.response["text"]


async def test_capacity_guard_and_nonretryable_error(journal):
    async with journal.store.locked() as s:
        for slot in (await s.scalars(select(Slot))).all():
            slot.blocked = True
    client = await opening(Client(journal))
    await client.click("next")
    result = await client.click("save")
    assert "Свободных" in result["text"]
    async with journal.store.locked() as s:
        (await s.get(Slot, "TR-000001")).blocked = False
    await client.click("save")
    journal.google.deny = True
    await drain(journal)
    async with journal.store.sessions() as s:
        assert (await s.get(Job, "trade:TR-000001:1")).status == "error"


async def test_all_keyboard_payloads_fit_telegram(journal):
    client = Client(journal)
    await open_trade(client)
    await client.click("sync")
    markup = keyboard(client.response)
    assert all(len(b.callback_data.encode()) <= 64 for row in markup.inline_keyboard for b in row)


async def test_cancel_discards_uncommitted_screenshots(journal):
    client = await opening(Client(journal))
    await client.image()
    await client.click("cancel")
    async with journal.store.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(Screenshot)) == 0
        assert await s.scalar(select(func.count()).select_from(Trade)) == 0


async def test_newer_success_supersedes_old_failed_job(journal):
    client = Client(journal)
    await open_trade(client)
    journal.google.deny = True
    await drain(journal)
    journal.google.deny = False
    await client.click("view", "TR-000001")
    await client.click("close", "TR-000001")
    await client.click("value", "now")
    await client.text("110")
    await client.click("value", "calculate")
    await client.click("value", "0")
    await client.click("skip")
    await client.click("next")
    await client.click("save")
    await drain(journal)
    async with journal.store.sessions() as s:
        assert all(j.status == "done" for j in (await s.scalars(select(Job))).all())
