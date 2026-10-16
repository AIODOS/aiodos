"""Numéros de version : leur ordre, et seuls des numéros sûrs, qui deviennent des noms
de dossier."""
import pytest

from aiodos.updates import version


def test_development_versions_come_before_their_release():
    ordered = ["1.9.9", "2.0.0-dev", "2.0.0-dev2", "2.0.0", "2.0.1", "2.1.0-dev", "2.1.0",
               "10.0.0"]
    assert sorted(reversed(ordered), key=version.key) == ordered


def test_newer_and_newest():
    assert version.newer("2.0.1", "2.0.0")
    assert not version.newer("2.0.0", "2.0.0")
    assert version.newer("2.0.0", None)
    assert not version.newer(None, "2.0.0")
    assert not version.newer("../2.0.0", "1.0.0")
    assert version.newest("2.0.0", None, "2.0.1-dev", "x") == "2.0.1-dev"
    assert version.newest(None, "x") is None


@pytest.mark.parametrize("text", ["", "2", "2.0", "2.0.0.0", "v2.0.0", "2.0.0-beta", "../../x",
                                  "2.0.0/..", " 2.0.0", "2.0.0\n", None, 2])
def test_unsafe_or_unknown_numbers_are_refused(text):
    assert not version.valid(text)
    with pytest.raises(ValueError):
        version.key(text)
