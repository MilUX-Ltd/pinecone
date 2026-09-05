"""Shared fixtures. The suites import the flat scripts by path."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def build_bundle() -> ModuleType:
    return load("build_bundle")


@pytest.fixture(scope="session")
def serve() -> ModuleType:
    return load("serve")


@pytest.fixture()
def env_no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("PINECONE_REPO", "MilUX-Ltd/this-repository-does-not-exist")
    os.environ.pop("PINECONE_GITHUB_TOKEN", None)
