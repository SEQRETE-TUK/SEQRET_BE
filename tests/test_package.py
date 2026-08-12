"""Tests for distribution metadata consistency."""

from importlib.metadata import version

from app import __version__


def test_distribution_version_matches_runtime_version() -> None:
    assert version("seqret-backend") == __version__
