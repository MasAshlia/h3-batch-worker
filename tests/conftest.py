from pathlib import Path

import pytest

from h3_worker.batch import compile_batch
from h3_worker.config import load_config
from h3_worker.file_io import read_json
from h3_worker.workflow_patcher import WorkflowPatcher

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def project_root():
    return ROOT


@pytest.fixture
def config():
    return load_config(ROOT / "config.example.yaml")


@pytest.fixture
def template(config):
    return read_json(config.template_path)


@pytest.fixture
def patcher(config, template):
    return WorkflowPatcher(template, config.mapping)


@pytest.fixture
def batch(config):
    return compile_batch(ROOT / "fixtures/manifests/episode_test.json", config)
