from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from app.journal.config import JournalSettings
from app.journal.domain import (
    ACCOUNT_HEADERS,
    FORMULA_COLUMNS,
    INPUT_COLUMNS,
    SHOT_HEADERS,
    TRADE_HEADERS,
    ConflictError,
    SyncError,
    sheet_inputs,
)
from app.journal.google import GoogleJournal
from journal_tests.test_domain import data


@pytest.mark.parametrize("include_type", [False, True])
async def test_oauth_flow_credentials_with_or_without_adc_type(tmp_path, include_type):
    import json

    info = {"token": "test-access", "refresh_token": "test-refresh",
            "client_id": "test-client", "client_secret": "test-secret",
            "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2099-01-01T00:00:00Z"}
    if include_type:
        info["type"] = "authorized_user"
    path = tmp_path / "oauth.json"
    path.write_text(json.dumps(info), encoding="utf-8")
    google = GoogleJournal(JournalSettings(_env_file=None, google_credentials=path))
    try:
        assert await google._token() == "test-access"
    finally:
        await google.close()


class SheetsServer:
    def __init__(self):
        self.grids = {
            "Сделки": [TRADE_HEADERS] + [[f"TR-{i:06}"] + [""] * 25 for i in range(1, 301)],
            "Скриншоты": [SHOT_HEADERS] + [[""] * 6 for _ in range(300)],
            "Счета": [ACCOUNT_HEADERS, ["1", "проп1", 200000, "USD", 0, 0, 200000, 0, 0, ""]],
        }
        for row in self.grids["Сделки"][1:]:
            for col in FORMULA_COLUMNS:
                row[col] = '=IF(B6="","",0)'
        self.writes = []
        self.timeout = False

    def metadata(self):
        return {"properties": {"timeZone": "Asia/Tomsk"}, "sheets": [
            {"properties": {"title": title, "sheetId": index},
             "tables": [{"range": {"startRowIndex": 4, "endRowIndex": 305 if title != "Счета" else 105,
                                    "startColumnIndex": 0, "endColumnIndex": len(grid[0])}}]}
            for index, (title, grid) in enumerate(self.grids.items())
        ]}

    def handle(self, request):
        path = unquote(request.url.path)
        if "/values/" in path:
            full_range = path.split("/values/", 1)[1]
            title, cells = full_range.rsplit("!", 1)
            title = title.strip("'").replace("''", "'")
            import re

            start, end = [int(x) for x in re.findall(r"\d+", cells)]
            rows = self.grids[title][start-5:end-4]
            return httpx.Response(200, json={"values": deepcopy(rows)})
        if path.endswith(":batchUpdate"):
            import json

            for update in json.loads(request.content)["requests"]:
                block = update["updateCells"]
                assert block["fields"] == "userEnteredValue"
                coord = block["range"]
                title = list(self.grids)[coord["sheetId"]]
                col = coord["startColumnIndex"]
                assert col not in FORMULA_COLUMNS if title == "Сделки" else col != 4
                typed = block["rows"][0]["values"][0]["userEnteredValue"]
                self.grids[title][coord["startRowIndex"]-4][col] = next(iter(typed.values()))
                self.writes.append((title, col, deepcopy(typed)))
            if self.timeout:
                self.timeout = False
                raise httpx.ReadTimeout("simulated lost response", request=request)
            return httpx.Response(200, json={"replies": []})
        return httpx.Response(200, json=self.metadata())


@pytest.fixture
def remote():
    server = SheetsServer()
    google = GoogleJournal(JournalSettings(_env_file=None),
                           client=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
                           credentials=SimpleNamespace(valid=True, token="test-only"))
    return google, server


async def test_schema_free_slots_and_formulas(remote):
    google, server = remote
    accounts, slots = await google.preflight()
    assert len(slots) == 300 and not any(s["blocked"] for s in slots)
    assert accounts[0]["initial"] == "200000"
    server.grids["Сделки"][1][6] = "manually overwritten formula"
    with pytest.raises(SyncError, match="формулы"):
        await google.preflight()
    assert server.writes == []
    await google.close()


async def test_atomic_inputs_only_and_text_not_formula(remote):
    google, server = remote
    baseline = {str(c): "" for c in INPUT_COLUMNS}
    payload = data(comment="=IMPORTXML(\"https://example.invalid\")", actual="0")
    await google.sync_trade("TR-000001", payload, baseline)
    assert any(col == 23 and "stringValue" in typed for _, col, typed in server.writes)
    assert server.grids["Сделки"][1][14] == 0
    assert server.grids["Сделки"][1][6].startswith("=")
    assert len(server.writes) == len(INPUT_COLUMNS)
    await google.close()


async def test_sorting_uses_id_not_row(remote):
    google, server = remote
    server.grids["Сделки"][1], server.grids["Сделки"][10] = server.grids["Сделки"][10], server.grids["Сделки"][1]
    await google.sync_trade("TR-000001", data(), {str(c): "" for c in INPUT_COLUMNS})
    assert server.grids["Сделки"][10][1] == "1"
    assert server.grids["Сделки"][1][1] == ""
    await google.close()


async def test_idempotent_timeout_and_manual_changes(remote):
    google, server = remote
    baseline = {str(c): "" for c in INPUT_COLUMNS}
    server.timeout = True
    with pytest.raises(SyncError) as exc:
        await google.sync_trade("TR-000001", data(), baseline)
    assert exc.value.retryable
    writes = len(server.writes)
    await google.sync_trade("TR-000001", data(), baseline)
    assert len(server.writes) == writes
    server.grids["Сделки"][1][23] = "manual"
    with pytest.raises(ConflictError):
        await google.sync_trade("TR-000001", data(exit="120"), sheet_inputs(data(), "Asia/Tomsk"))
    assert server.grids["Сделки"][1][23] == "manual"
    await google.close()


async def test_headers_duplicates_and_coverage(remote):
    google, server = remote
    server.grids["Сделки"][0] = list(TRADE_HEADERS)
    server.grids["Сделки"][0][0] = "renamed"
    with pytest.raises(SyncError, match="заголовки"):
        await google.preflight()
    server.grids["Сделки"][0][0] = TRADE_HEADERS[0]
    server.grids["Сделки"][2][0] = "TR-000001"
    with pytest.raises(SyncError, match="повторный ID"):
        await google.preflight()
    await google.close()


async def test_screenshot_empty_cells_preserve_image_slot_and_retry(remote):
    google, server = remote
    shot = SimpleNamespace(trade_id="TR-000001", kind="Вход", captured="2026-10-25T00:00:00+00:00",
                           drive_url="https://drive.example/image", comment="=literal", sheet_row=6)
    await google.sync_screenshot(shot)
    writes = len(server.writes)
    server.grids["Скриншоты"][1], server.grids["Скриншоты"][5] = server.grids["Скриншоты"][5], server.grids["Скриншоты"][1]
    await google.sync_screenshot(shot)
    assert shot.sheet_row == 10
    assert len(server.writes) == writes
    assert all(col != 4 for _, col, _ in server.writes)
    await google.close()
