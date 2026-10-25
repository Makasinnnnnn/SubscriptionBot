import asyncio

from app.journal.config import JournalSettings
from app.journal.domain import SyncError
from app.journal.google import GoogleJournal


async def check():
    google = GoogleJournal(JournalSettings())
    try:
        accounts, slots = await google.preflight()
        print(f"Google schema OK: {len(accounts)} accounts, {len(slots)} IDs, "
              f"{sum(not s['blocked'] for s in slots)} unoccupied rows. No writes performed.")
    finally:
        await google.close()


if __name__ == "__main__":
    try:
        asyncio.run(check())
    except SyncError as exc:
        raise SystemExit(str(exc)) from None
