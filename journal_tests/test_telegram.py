from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, GetFile, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User
from sqlalchemy import func, select

from app.journal.bot import build_router
from app.journal.models import Screenshot, Trade
from journal_tests.test_workflow import drain


class TelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, SendMessage):
            message = Message(message_id=len(self.sent) + 1000, date=datetime.now(UTC),
                              chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
                              reply_markup=method.reply_markup)
            self.sent.append(message)
            return message
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="unique", file_size=20,
                        file_path="images/" + method.file_id)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Journal", username="journal_bot")
        raise AssertionError(type(method).__name__)

    async def stream_content(self, url, **kwargs):
        yield b"\x89PNG\r\n\x1a\nvalid-test-content"


class TelegramClient:
    def __init__(self, service):
        self.session = TelegramSession()
        self.bot = Bot("42:TEST_ONLY_TOKEN_NOT_A_REAL_CREDENTIAL", session=self.session)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(build_router(service))
        self.sequence = 0

    async def send(self, text=None, document=None):
        self.sequence += 1
        message = Message(message_id=self.sequence, date=datetime.now(UTC),
                          chat=Chat(id=1, type="private"), from_user=User(id=1, is_bot=False, first_name="Test"),
                          text=text, document=document)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, message=message))
        return self.session.sent[-1]

    async def click(self, action, value=""):
        message = self.session.sent[-1]
        button = next(b for row in message.reply_markup.inline_keyboard for b in row
                      if b.callback_data.split("|", 3)[2:] == [action, value])
        self.sequence += 1
        query = CallbackQuery(id=str(self.sequence), from_user=User(id=1, is_bot=False, first_name="Test"),
                              chat_instance="private-test", message=message, data=button.callback_data)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.sequence, callback_query=query))
        return self.session.sent[-1]


async def test_real_aiogram_router_open_upload_close(journal):
    client = TelegramClient(journal)
    await client.send("/start")
    await client.click("open")
    await client.send("BTCUSDT")
    await client.click("value", "Long")
    await client.click("value", "now")
    await client.send("100")
    await client.click("skip")
    await client.click("value", "5")
    await client.click("value", "suggested")
    await client.click("value", "quantity")
    await client.send("10")
    await client.click("skip")
    for i in range(2):
        await client.send(document=Document(file_id=f"photo-{i}", file_unique_id=f"unique-{i}",
                                           file_name="screenshot.png", mime_type="image/png", file_size=26))
    await client.click("next")
    response = await client.click("save")
    assert "TR-000001" in response.text
    await drain(journal)
    await client.click("view", "TR-000001")
    await client.click("close", "TR-000001")
    await client.click("value", "now")
    await client.send("110")
    await client.click("value", "calculate")
    await client.click("value", "0")
    await client.click("skip")
    await client.click("next")
    await client.click("save")
    await drain(journal)
    async with journal.store.sessions() as s:
        trade = await s.get(Trade, "TR-000001")
        assert trade.payload["closed"]
        assert trade.payload["costs"] == "0"
        assert await s.scalar(select(func.count()).select_from(Screenshot)) == 2
    await client.bot.session.close()


async def test_router_rejects_unknown_document_mime(journal):
    client = TelegramClient(journal)
    response = await client.send(document=Document(file_id="pdf", file_unique_id="pdf-id",
                                                  mime_type="application/pdf", file_size=123))
    assert "JPG, PNG и WebP" in response.text
    await client.bot.session.close()
