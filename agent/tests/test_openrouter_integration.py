"""
Integration test: the OpenRouter adapter against a real HTTP server.

`test_vision_openrouter.py` injects a fake session (fast, no sockets). This one
spins the stand-in router from `tools/fake_openrouter.py` on an ephemeral port so
the wire format — URL, Bearer header, base64 image parts, JSON parsing — is
exercised for real.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

from src.data_layer import DataLayer
from src.sheets_client import XlsxSource
from src import config
from src.drive_client import MockDrive
from src.image_manager import ImageManager
from src.models import DownloadedImage, ResultStatus
from src.output_generator import OutputGenerator
from src.validator import Validator
from src.vision_checker import OpenRouterVision, VisionHints
from tools.fake_openrouter import Handler

pytestmark = pytest.mark.usefixtures("dataset_available")


@pytest.fixture()
def router():
    Handler.violate = False
    Handler.require_key = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.verbose = False  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/api/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_models_and_health_over_http(router):
    backend = OpenRouterVision(base_url=router, model="google/gemini-2.0-flash-001", api_key="test-key")
    assert "google/gemini-2.0-flash-001" in backend.list_models()
    ok, message = backend.health()
    assert ok
    assert "model vision" in message


def test_unknown_model_reported_over_http(router):
    backend = OpenRouterVision(base_url=router, model="does/not-exist", api_key="test-key")
    ok, message = backend.health()
    assert not ok
    assert "does/not-exist" in message


def test_smoke_test_reads_image_over_http(router, tmp_path):
    from PIL import Image

    path = tmp_path / "probe.png"
    Image.new("RGB", (120, 60), "white").save(path)

    backend = OpenRouterVision(base_url=router, model="google/gemini-2.0-flash-001", api_key="test-key")
    ok, detail, payload = backend.smoke_test(path)
    assert ok
    assert "MAMAZON TEST" in detail
    assert payload["confidence"] > 0.9


def test_analyze_returns_facts_over_http(router, tmp_path):
    from PIL import Image

    path = tmp_path / "evidence.png"
    Image.new("RGB", (32, 32), "white").save(path)
    image = DownloadedImage(file_id="a", path=str(path))

    hints = VisionHints(
        task_id="PP-03",
        nama="Budi",
        caption_reference="Caption resmi",
        required_hashtags=["#PPHorizon"],
        required_mentions=["@horizon_umn"],
        feed_required=True,
    )
    backend = OpenRouterVision(base_url=router, model="google/gemini-2.0-flash-001")
    facts = backend.analyze([image], hints)

    assert facts.provider == "openrouter"
    assert facts.caption_text == "Caption resmi"
    assert facts.hashtags == ["#PPHorizon"]
    assert facts.followers == 842
    assert facts.raw["usage"]["total_tokens"] == 1220


def test_full_validation_through_router(router, rulebase, tmp_path):
    """A OpenRouter backend drives the real pipeline end to end."""
    data = DataLayer(rulebase=rulebase, source=XlsxSource(config.DUMMY_DATA_PATH))
    outputs = OutputGenerator(
        output_dir=tmp_path,
        verification_path=tmp_path / "verification_report.csv",
        violations_path=tmp_path / "violations_log.csv",
        blast_draft_path=tmp_path / "blast_draft.md",
        audit_path=tmp_path / "audit_log.jsonl",
    )
    validator = Validator(
        data_layer=data,
        vision=OpenRouterVision(base_url=router, model="google/gemini-2.0-flash-001", api_key="test-key"),
        images=ImageManager(
            drive=MockDrive(),
            tmp_dir=tmp_path / "images",
            cache_path=tmp_path / "hashes.json",
        ),
        outputs=outputs,
        blast_context={},
    )
    try:
        batch = validator.validate("PP Feed 26 Oktober", limit=4, force=True, actor="test")
    finally:
        data.close()

    assert batch.total == 4
    assert batch.pending == [], [r.notes for r in batch.pending]
    # The stand-in model answers with the expected caption/tags, so nothing is fined.
    assert batch.flagged == []
    assert all(
        result.status in (ResultStatus.VERIFIED, ResultStatus.EXEMPT)
        for result in batch.results
    )
    assert any((result.facts.confidence if result.facts else 0) > 0.8 for result in batch.results)


def test_violating_router_produces_fines(router, rulebase, tmp_path):
    Handler.violate = True
    try:
        data = DataLayer(rulebase=rulebase, source=XlsxSource(config.DUMMY_DATA_PATH))
        outputs = OutputGenerator(
            output_dir=tmp_path,
            verification_path=tmp_path / "verification_report.csv",
            violations_path=tmp_path / "violations_log.csv",
            blast_draft_path=tmp_path / "blast_draft.md",
            audit_path=tmp_path / "audit_log.jsonl",
        )
        validator = Validator(
            data_layer=data,
            vision=OpenRouterVision(base_url=router, model="google/gemini-2.0-flash-001", api_key="test-key"),
            images=ImageManager(
            drive=MockDrive(),
            tmp_dir=tmp_path / "images",
            cache_path=tmp_path / "hashes.json",
        ),
            outputs=outputs,
            blast_context={},
        )
        try:
            batch = validator.validate("PP Feed 26 Oktober", limit=4, force=True, actor="test")
        finally:
            data.close()
    finally:
        Handler.violate = False

    assert batch.flagged, "dropping a hashtag must trigger C6"
    assert all("C6" in result.violation_ids for result in batch.flagged)
    assert batch.total_denda >= 5000
