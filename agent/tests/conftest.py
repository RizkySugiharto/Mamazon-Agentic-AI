"""Shared pytest fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_ROOT))

from src import config  # noqa: E402
from src.data_layer import DataLayer  # noqa: E402
from src.drive_client import MockDrive  # noqa: E402
from src.drive_client import MockDrive  # noqa: E402
from src.sheets_client import XlsxSource  # noqa: E402
from src.image_manager import ImageManager  # noqa: E402
from src.output_generator import BlastStore, OutputGenerator  # noqa: E402
from src.validator import TaskEngineAdapter, Validator  # noqa: E402
from src.vision_checker import MockVision  # noqa: E402


@pytest.fixture()
def validator(rulebase, outputs, images) -> Validator:
    """A Validator wired to the dummy dataset and a mocked vision backend."""
    data = DataLayer(source=XlsxSource(config.DUMMY_DATA_PATH))
    instance = Validator(
        data_layer=data, vision=MockVision(), images=images, outputs=outputs, blast_context={}
    )
    yield instance
    data.close()


@pytest.fixture(scope="session")
def engine() -> TaskEngineAdapter:
    return TaskEngineAdapter()


@pytest.fixture(scope="session")
def rulebase(engine) -> dict:
    return engine.rulebase


@pytest.fixture()
def data() -> DataLayer:
    layer = DataLayer(source=XlsxSource(config.DUMMY_DATA_PATH))
    yield layer
    layer.close()


@pytest.fixture()
def outputs(tmp_path) -> OutputGenerator:
    return OutputGenerator(
        output_dir=tmp_path,
        verification_path=tmp_path / "verification_report.csv",
        violations_path=tmp_path / "violations_log.csv",
        blast_draft_path=tmp_path / "blast_draft.md",
        audit_path=tmp_path / "audit_log.jsonl",
    )


@pytest.fixture()
def images(tmp_path) -> ImageManager:
    # Always mock the drive: tests must never depend on .env or the network.
    return ImageManager(
        drive=MockDrive(),
        tmp_dir=tmp_path / "images",
        cache_path=tmp_path / "hashes.json",
    )


@pytest.fixture()
def blast_store(tmp_path) -> BlastStore:
    return BlastStore(directory=tmp_path / "blasts")


@pytest.fixture()
def dataset_available() -> None:
    if not config.DUMMY_DATA_PATH.exists():
        pytest.skip("dummy dataset missing — run _refs/build_dummy.py")
