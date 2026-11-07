"""Google Sheets source: parsing, serial dates, error mapping, DataLayer integration."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest

from src.data_layer import DataLayer
from src.sheets_client import (
    GoogleSheetsSource,
    SheetAccessError,
    SheetRateLimitError,
    SheetSourceError,
    XlsxSource,
    col_to_letter,
    extract_spreadsheet_id,
    quote_sheet_title,
    serial_to_datetime,
    to_datetime,
    to_text,
)


# ─── Fake Google client ─────────────────────────────────────────────────────
class FakeRequest:
    def __init__(self, payload: dict, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.executions = 0

    def execute(self):
        self.executions += 1
        if self.error:
            raise self.error
        return self.payload


class FakeValues:
    def __init__(self, sheets: dict[str, list[list]]) -> None:
        self.sheets = sheets
        self.calls: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.requests: list[FakeRequest] = []

    def get(self, **kwargs):
        range_value = kwargs.get("range", "")
        title = range_value.rsplit("!", 1)[0].strip("'").replace("''", "'")
        self.calls.append(title)
        request = FakeRequest(
            {"values": self.sheets.get(title, [])}, error=self.errors.get(title)
        )
        self.requests.append(request)
        return request

    def append(self, **kwargs):
        range_value = kwargs.get("range", "")
        title = range_value.rsplit("!", 1)[0].strip("'").replace("''", "'")
        body = kwargs.get("body", {})
        values = body.get("values", [])
        if title not in self.sheets:
            self.sheets[title] = []
        for row in values:
            self.sheets[title].append(list(row))
        self.calls.append(f"append:{title}")
        request = FakeRequest({"updates": {"updatedRows": len(values)}}, error=self.errors.get("append"))
        self.requests.append(request)
        return request

    def update(self, **kwargs):
        range_value = kwargs.get("range", "")
        body = kwargs.get("body", {})
        self.calls.append(f"update:{range_value}")
        request = FakeRequest({"updatedCells": 1}, error=self.errors.get("update"))
        self.requests.append(request)
        return request


class FakeSpreadsheets:
    def __init__(self, titles: list[str], values: FakeValues, title: str = "Demo") -> None:
        self.titles = titles
        self._values = values
        self._title = title
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []
        self.requests: list[FakeRequest] = []

    def get(self, **kwargs):
        fields = kwargs.get("fields", "")
        self.calls.append(fields)
        request = FakeRequest(
            {
                "properties": {"title": self._title},
                "sheets": [{"properties": {"title": name}} for name in self.titles],
            },
            error=self.errors.get(fields),
        )
        self.requests.append(request)
        return request

    def values(self):
        return self._values


class FakeService:
    def __init__(self, titles: list[str], sheets: dict[str, list[list]] | None = None):
        self.values = FakeValues(sheets or {})
        self._spreadsheets = FakeSpreadsheets(titles, self.values)

    def spreadsheets(self):
        return self._spreadsheets


@pytest.fixture(autouse=True)
def mock_account_email(monkeypatch):
    monkeypatch.setattr(
        GoogleSheetsSource,
        "service_account_email",
        lambda self: "bot@project.iam.gserviceaccount.com",
    )


def http_error(status: int, reason: str | None = None, *, modern: bool = False):
    from googleapiclient.errors import HttpError
    from httplib2 import Response

    details = {"code": status, "message": "Mock Sheets error"}
    if reason:
        details["details" if modern else "errors"] = [{"reason": reason}]
    return HttpError(
        Response({"status": status}), json.dumps({"error": details}).encode()
    )


def fail_resource(service, resource, error):
    if resource.startswith("rows:"):
        service.values.errors[resource.removeprefix("rows:")] = error
    else:
        fields = (
            "sheets.properties.title" if resource == "sheet_names" else "properties.title"
        )
        service._spreadsheets.errors[fields] = error


def read_resource(source, resource):
    if resource.startswith("rows:"):
        return source.rows(resource.removeprefix("rows:"))
    return getattr(source, resource)()


# ─── Helpers ────────────────────────────────────────────────────────────────
class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
            (
                "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0",
                "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            ),
            ("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
        ],
    )
    def test_extract_id(self, value, expected):
        assert extract_spreadsheet_id(value) == expected

    def test_quote_title_escapes_apostrophes(self):
        assert quote_sheet_title("Buzzer vote 26 Oktober (joccely") == (
            "'Buzzer vote 26 Oktober (joccely'"
        )
        assert quote_sheet_title("O'Brien") == "'O''Brien'"

    def test_serial_to_datetime(self):
        serial = (datetime(2026, 10, 29) - datetime(1899, 12, 30)).days + (
            14 * 3600 + 23 * 60 + 11
        ) / 86400
        assert serial_to_datetime(serial) == datetime(2026, 10, 29, 14, 23, 11)

    def test_serial_out_of_range(self):
        assert serial_to_datetime(0) is None
        assert serial_to_datetime(99999999) is None
        assert serial_to_datetime("abc") is None

    def test_to_datetime_accepts_iso_string(self):
        assert to_datetime("2026-10-29 14:23:11") == datetime(2026, 10, 29, 14, 23, 11)

    def test_to_datetime_accepts_datetime(self):
        moment = datetime(2026, 10, 29, 9, 0)
        assert to_datetime(moment) == moment

    def test_to_text_avoids_float_artifacts(self):
        assert to_text(45997.0) == "45997"
        assert to_text(12.5) == "12.5"
        assert to_text(None) == ""
        assert to_text(True) == "TRUE"


# ─── Source ─────────────────────────────────────────────────────────────────
class TestGoogleSheetsSource:
    def test_sheet_names(self):
        source = GoogleSheetsSource("x" * 30, service=FakeService(["Roster", "PP 29 Oktober"]))
        assert source.sheet_names() == ["Roster", "PP 29 Oktober"]

    def test_rows_padded_and_fresh(self):
        service = FakeService(
            ["PP 29 Oktober"],
            {"PP 29 Oktober": [["Timestamp", "Nama"], ["2026-10-29", "Budi", "extra"]]},
        )
        source = GoogleSheetsSource("x" * 30, service=service)
        rows = source.rows("PP 29 Oktober")
        assert rows[0] == ["Timestamp", "Nama", None]
        assert rows[1] == ["2026-10-29", "Budi", "extra"]
        service.values.sheets["PP 29 Oktober"][1][1] = "Sari"
        service.values.sheets["PP 29 Oktober"].append(["2026-10-30", "Dewi"])
        fresh = source.rows("PP 29 Oktober")
        assert fresh[1][1] == "Sari"
        assert fresh[2] == ["2026-10-30", "Dewi", None]
        assert rows[1][1] == "Budi"
        assert service.values.calls == ["PP 29 Oktober", "PP 29 Oktober"]

    def test_invalidate_clears_cache(self):
        service = FakeService(["A"], {"A": [["x"]]})
        source = GoogleSheetsSource("x" * 30, service=service)
        source.rows("A")
        source.invalidate()
        source.rows("A")
        assert service.values.calls == ["A", "A"]

    def test_empty_spreadsheet_id(self):
        with pytest.raises(SheetSourceError):
            GoogleSheetsSource("")

    def test_403_becomes_share_instructions(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        error = HttpError(Response({"status": 403}), b'{"error": "forbidden"}')
        service = FakeService(["A"])
        service.values.get = lambda **kwargs: FakeRequest({}, error=error)

        source = GoogleSheetsSource("x" * 30, service=service)
        with pytest.raises(SheetAccessError) as excinfo:
            source.rows("A")
        assert "bot@project.iam.gserviceaccount.com" in str(excinfo.value)
        assert "Viewer" in str(excinfo.value)

    def test_404_mentions_spreadsheet_id(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response

        error = HttpError(Response({"status": 404}), b"{}")
        service = FakeService(["A"])
        service.values.get = lambda **kwargs: FakeRequest({}, error=error)
        source = GoogleSheetsSource("y" * 30, service=service)
        with pytest.raises(SheetAccessError) as excinfo:
            source.rows("A")
        assert "GOOGLE_SPREADSHEET_ID" in str(excinfo.value)


class TestFreshReadsAndFallback:
    def test_names_and_title_reflect_edits(self):
        service = FakeService(["A"])
        source = GoogleSheetsSource("x" * 30, service=service)
        names = source.sheet_names()
        assert source.spreadsheet_title() == "Demo"
        names.append("caller edit")
        service._spreadsheets.titles[:] = ["Renamed", "New tab"]
        service._spreadsheets._title = "Renamed workbook"
        assert source.sheet_names() == ["Renamed", "New tab"]
        assert source.spreadsheet_title() == "Renamed workbook"
        assert service._spreadsheets.calls == [
            "sheets.properties.title", "properties.title",
            "sheets.properties.title", "properties.title",
        ]
        assert source.warnings == []

    @pytest.mark.parametrize("resource", ["rows:A", "sheet_names", "spreadsheet_title"])
    @pytest.mark.parametrize(
        "status,reason,modern",
        [
            (429, None, False),
            (403, "rateLimitExceeded", False),
            (403, "userRateLimitExceeded", False),
            (403, "quotaExceeded", False),
            (403, "dailyLimitExceeded", False),
            (403, "dailyLimitExceededUnreg", False),
            (403, "RATE_LIMIT_EXCEEDED", True),
            (403, "QUOTA_EXCEEDED", True),
            (403, "RESOURCE_EXHAUSTED", True),
        ],
    )
    def test_quota_fallback_and_recovery(self, resource, status, reason, modern):
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        before = datetime.now(timezone.utc)
        original = read_resource(source, resource)
        after = datetime.now(timezone.utc)
        fail_resource(service, resource, http_error(status, reason, modern=modern))
        assert read_resource(source, resource) == original
        assert len(source.warnings) == 1
        warning = source.warnings[0]
        assert resource in warning
        assert source.label in warning
        assert "stale snapshot" in warning and "UTC" in warning
        read_at = datetime.fromisoformat(warning.split("read at ")[1].split(" (UTC)")[0])
        assert before <= read_at <= after
        source.warnings.clear()
        assert source.warnings == [warning]
        assert read_resource(source, resource) == original
        assert source.warnings == [warning]

        service.values.errors.clear()
        service._spreadsheets.errors.clear()
        service.values.sheets["A"] = [["new"]]
        service._spreadsheets.titles[:] = ["New tab"]
        service._spreadsheets._title = "New title"
        fresh = read_resource(source, resource)
        assert fresh != original
        assert source.warnings == []
        fail_resource(service, resource, http_error(429))
        assert read_resource(source, resource) == fresh

    def test_rows_snapshot_isolated_from_caller_and_api_mutations(self):
        service = FakeService(["A"], {"A": [["old"], ["second"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        rows = source.rows("A")
        rows[0][0] = "caller edit"
        rows.append(["caller row"])
        service.values.sheets["A"][1][0] = "API edit"
        service.values.errors["A"] = http_error(429)
        fallback = source.rows("A")
        assert fallback == [["old"], ["second"]]
        fallback[0][0] = "fallback edit"
        assert source.rows("A") == [["old"], ["second"]]

    def test_names_snapshot_isolated_from_fallback_mutation(self):
        service = FakeService(["A"])
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        source.sheet_names()
        fail_resource(service, "sheet_names", http_error(429))
        fallback = source.sheet_names()
        fallback.append("caller edit")
        assert source.sheet_names() == ["A"]

    def test_fallback_only_after_retry_exhaustion(self, monkeypatch):
        delays = []
        monkeypatch.setattr("src.sheets_client.time.sleep", delays.append)
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=3)
        source.rows("A")
        service.values.errors["A"] = http_error(403, "quotaExceeded")
        assert source.rows("A") == [["old"]]
        assert service.values.requests[-1].executions == 3
        assert delays == [1.0, 2.0]
        assert len(source.warnings) == 1

    def test_retry_success_does_not_warn_or_use_snapshot(self, monkeypatch):
        delays = []
        monkeypatch.setattr("src.sheets_client.time.sleep", delays.append)
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=3)
        source.rows("A")
        request = FakeRequest({"values": [["new"]]})

        def execute():
            request.executions += 1
            if request.executions == 1:
                raise http_error(429)
            return request.payload

        request.execute = execute
        service.values.get = lambda **kwargs: request
        assert source.rows("A") == [["new"]]
        assert request.executions == 2
        assert delays == [1.0]
        assert source.warnings == []

    @pytest.mark.parametrize("resource", ["rows:A", "sheet_names", "spreadsheet_title"])
    @pytest.mark.parametrize(
        "status,reason,error_type",
        [
            (403, "forbidden", SheetAccessError),
            (403, "accessNotConfigured", SheetAccessError),
            (403, None, SheetAccessError),
            (404, None, SheetAccessError),
            (500, None, SheetSourceError),
            (503, None, SheetSourceError),
        ],
    )
    def test_non_quota_failure_never_serves_snapshot(
        self, resource, status, reason, error_type
    ):
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        read_resource(source, resource)
        fail_resource(service, resource, http_error(429))
        read_resource(source, resource)
        assert source.warnings
        fail_resource(service, resource, http_error(status, reason))
        with pytest.raises(error_type) as excinfo:
            read_resource(source, resource)
        assert not isinstance(excinfo.value, SheetRateLimitError)
        assert source.warnings == []

    @pytest.mark.parametrize("resource", ["rows:A", "sheet_names", "spreadsheet_title"])
    @pytest.mark.parametrize("cache,prime", [(True, False), (False, False), (False, True)])
    def test_quota_without_snapshot_raises(self, resource, cache, prime, monkeypatch):
        monkeypatch.setattr(
            "src.sheets_client.time.sleep",
            lambda delay: pytest.fail("max_retries=0 must not sleep"),
        )
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, cache=cache, max_retries=0)
        if prime:
            read_resource(source, resource)
        fail_resource(service, resource, http_error(429))
        with pytest.raises(SheetRateLimitError):
            read_resource(source, resource)
        requests = service.values.requests + service._spreadsheets.requests
        assert requests[-1].executions == 1
        assert source._snapshots == {}
        assert source.warnings == []

    def test_cache_false_still_reads_fresh_names_and_rows(self):
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, cache=False)
        source.rows("A")
        source.sheet_names()
        service.values.sheets["A"] = [["new"]]
        service._spreadsheets.titles[:] = ["New tab"]
        assert source.rows("A") == [["new"]]
        assert source.sheet_names() == ["New tab"]
        assert source._snapshots == {}

    def test_empty_rows_and_names_are_valid_snapshots(self):
        service = FakeService([])
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        assert source.rows("A") == []
        assert source.sheet_names() == []
        fail_resource(service, "rows:A", http_error(429))
        fail_resource(service, "sheet_names", http_error(429))
        assert source.rows("A") == []
        assert source.sheet_names() == []
        assert len(source.warnings) == 2

    def test_warnings_are_resource_specific(self):
        service = FakeService(["A", "B"], {"A": [["a"]], "B": [["b"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        resources = ["rows:A", "rows:B", "sheet_names", "spreadsheet_title"]
        for resource in resources:
            read_resource(source, resource)
            fail_resource(service, resource, http_error(429))
            read_resource(source, resource)
        assert len(source.warnings) == 4
        service.values.errors.pop("A")
        source.rows("A")
        assert len(source.warnings) == 3
        assert not any("rows:A" in warning for warning in source.warnings)
        assert any("rows:B" in warning for warning in source.warnings)

    @pytest.mark.parametrize("method", ["invalidate", "close"])
    def test_invalidate_and_close_drop_snapshots_and_warnings(self, method):
        service = FakeService(["A"], {"A": [["old"]]})
        source = GoogleSheetsSource("x" * 30, service=service, max_retries=0)
        source.rows("A")
        service.values.errors["A"] = http_error(429)
        source.rows("A")
        assert source.warnings
        getattr(source, method)()
        assert source.warnings == []
        assert source._snapshots == {}
        source._service = service
        with pytest.raises(SheetRateLimitError):
            source.rows("A")

    def test_shared_client_construction_and_execution_hold_reentrant_lock(self):
        service = FakeService(["A"], {"A": [["a"]]})
        source = GoogleSheetsSource("x" * 30, service=service)
        get = service.values.get

        def checked_get(**kwargs):
            assert source._lock._is_owned()
            request = get(**kwargs)
            execute = request.execute

            def checked_execute():
                assert source._lock._is_owned()
                return execute()

            request.execute = checked_execute
            return request

        service.values.get = checked_get
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: source.rows("A"), range(8)))
        assert results == [[["a"]]] * 8
        assert service.values.calls == ["A"] * 8

    def test_shared_client_serializes_threads(self):
        service = FakeService(["A"], {"A": [["a"]]})
        source = GoogleSheetsSource("x" * 30, service=service)
        lock = source._lock
        first_executing, second_attempting, release_first = Event(), Event(), Event()

        class ObservedLock:
            def __enter__(self):
                if first_executing.is_set():
                    second_attempting.set()
                lock.acquire()

            def __exit__(self, *exc_info):
                lock.release()

        source._lock = ObservedLock()
        get = service.values.get

        def blocking_get(**kwargs):
            request = get(**kwargs)
            if len(service.values.calls) == 1:
                execute = request.execute

                def blocked_execute():
                    first_executing.set()
                    assert release_first.wait(5)
                    return execute()

                request.execute = blocked_execute
            return request

        service.values.get = blocking_get
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(source.rows, "A")
            try:
                assert first_executing.wait(5)
                second = pool.submit(source.rows, "A")
                assert second_attempting.wait(5)
                assert service.values.calls == ["A"]
            finally:
                release_first.set()
            assert first.result(timeout=5) == [["a"]]
            assert second.result(timeout=5) == [["a"]]
        assert service.values.calls == ["A", "A"]


# ─── DataLayer integration ──────────────────────────────────────────────────
class FakeSource:
    name = "fake"
    label = "fake-sheet"

    def __init__(self, sheets: dict[str, list[list]]) -> None:
        self.sheets = sheets

    def sheet_names(self):
        return list(self.sheets)

    def rows(self, name):
        return self.sheets[name]

    def close(self):
        pass


def serial(moment: datetime) -> float:
    return (moment - datetime(1899, 12, 30)).total_seconds() / 86400


class TestDataLayerWithSheets:
    def test_serial_timestamp_becomes_datetime(self, rulebase):
        rows = [
            [
                "Timestamp",
                "Nama Lengkap",
                "NIM",
                "Divisi",
                "Username Instagram",
                "Link Bukti (Google Drive)",
            ],
            [
                serial(datetime(2026, 10, 29, 14, 23, 11)),
                "Budi Santoso",
                446173,
                "Conan (Acara)",
                "budi_santoso",
                "https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA, "
                "https://drive.google.com/open?id=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            ],
        ]
        layer = DataLayer(rulebase=rulebase, source=FakeSource({"PP 29 Oktober": rows}))
        submissions = layer.read_submissions("PP 29 Oktober")

        assert len(submissions) == 1
        submission = submissions[0]
        assert submission.timestamp == datetime(2026, 10, 29, 14, 23, 11)
        assert submission.nim == "446173"  # no ".0", no scientific notation
        assert len(submission.links) == 2
        assert len(submission.file_ids) == 2

    def test_registry_date_column_uses_serial(self, rulebase):
        rows = [
            ["Nama Lengkap", "Divisi", "Status Angel Card", "Tanggal Pembelian"],
            ["Budi Santoso", "Conan (Acara)", "ACTIVE", serial(datetime(2026, 10, 2))],
        ]
        layer = DataLayer(
            rulebase=rulebase, source=FakeSource({"Angel Card Registry": rows})
        )
        registry = layer.load_angel_registry()
        assert len(registry) == 1
        assert registry[0].tanggal_beli.date() == datetime(2026, 10, 2).date()

    def test_label_and_close(self, rulebase):
        layer = DataLayer(rulebase=rulebase, source=FakeSource({"A": []}))
        assert layer.label == "fake-sheet"
        layer.close()

    def test_xlsx_source_still_works(self, rulebase, dataset_available):
        from src import config

        layer = DataLayer(rulebase=rulebase, source=XlsxSource(config.DUMMY_DATA_PATH))
        try:
            assert "PP 29 Oktober" in layer.sheet_names()
            assert len(layer.read_submissions("PP 29 Oktober")) == 42
        finally:
            layer.close()

    def test_col_to_letter_mapping(self):
        assert col_to_letter(1) == "A"
        assert col_to_letter(3) == "C"
        assert col_to_letter(26) == "Z"
        assert col_to_letter(27) == "AA"
        assert col_to_letter(28) == "AB"
        assert col_to_letter(52) == "AZ"
        assert col_to_letter(53) == "BA"

    def test_google_sheets_mutation_methods(self):
        service = FakeService(["Registry"], {"Registry": [["Name", "Status"]]})
        source = GoogleSheetsSource("x" * 30, service=service)
        # Prime cache
        assert source.rows("Registry") == [["Name", "Status"]]

        # Append row
        source.append_row("Registry", ["Alice", "ACTIVE"])
        assert "append:Registry" in service.values.calls
        assert source._snapshots == {}  # Invalidation verified

        # Fresh read includes appended row
        rows = source.rows("Registry")
        assert rows == [["Name", "Status"], ["Alice", "ACTIVE"]]

        # Update cell
        source.update_cell("Registry", 2, 2, "INACTIVE")
        assert any("update:'Registry'!B2" in c for c in service.values.calls)
        assert source._snapshots == {}

        # Update row
        source.update_row("Registry", 2, ["Alice", "REMOVED"])
        assert any("update:'Registry'!A2:B2" in c for c in service.values.calls)
        assert source._snapshots == {}

    def test_xlsx_source_mutation(self, tmp_path):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "TestTab"
        ws.append(["Col1", "Col2"])
        wb_path = tmp_path / "test_sheet.xlsx"
        wb.save(wb_path)
        wb.close()

        source = XlsxSource(wb_path)
        assert source.rows("TestTab") == [["Col1", "Col2"]]

        source.append_row("TestTab", ["Val1", "Val2"])
        assert source.rows("TestTab") == [["Col1", "Col2"], ["Val1", "Val2"]]

        source.update_cell("TestTab", 2, 2, "UpdatedVal")
        assert source.rows("TestTab") == [["Col1", "Col2"], ["Val1", "UpdatedVal"]]

        source.update_row("TestTab", 2, ["NewRow1", "NewRow2"])
        assert source.rows("TestTab") == [["Col1", "Col2"], ["NewRow1", "NewRow2"]]
        source.close()

