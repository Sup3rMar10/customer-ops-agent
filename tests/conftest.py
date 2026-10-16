import shutil
from datetime import datetime, timedelta, timezone

import pytest

from opsagent.backend import Backend
from opsagent.evals import Env
from opsagent.gateway import Gateway, GuardConfig
from opsagent.seed import build_database


@pytest.fixture(scope="session")
def template(tmp_path_factory):
    """One seeded database, built once. Tests copy it, so nothing leaks between them."""
    return build_database(tmp_path_factory.mktemp("seed") / "template.db")


@pytest.fixture
def env(template, tmp_path):
    e = Env(template, tmp_path, GuardConfig(mode="read_write"))
    yield e
    e.close()


@pytest.fixture
def make_env(template, tmp_path):
    made = []

    def factory(**cfg):
        sub = tmp_path / f"e{len(made)}"
        sub.mkdir()
        e = Env(template, sub, GuardConfig(**cfg))
        made.append(e)
        return e
    yield factory
    for e in made:
        e.close()
