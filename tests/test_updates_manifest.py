"""Manifestes signés : seule une clé de publication de confiance fait foi, et le
contenu doit être sûr avant de devenir des noms de fichiers."""
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aiodos.updates import keys, manifest
from aiodos.updates.manifest import UpdateError
from tests.updates_kit import new_key

SHA = "a" * 64


def make(version="2.0.1", **extra):
    data = {"format": 1, "app": "aiodos", "version": version, "published": 1, "commit": "abc",
            "min_python": "3.11", "notes": "",
            "files": [{"kind": "source", "name": f"aiodos-{version}.zip", "size": 10,
                       "sha256": SHA}]}
    data.update(extra)
    return data


def test_signed_manifest_is_verified_with_a_trusted_key():
    private, trusted = new_key()
    signed = manifest.sign(make(), private)
    assert manifest.verify(signed, trusted)["version"] == "2.0.1"


def test_tampered_manifest_is_refused():
    private, trusted = new_key()
    signed = manifest.sign(make(), private)
    signed["manifest"] = signed["manifest"].replace("2.0.1", "9.0.1")
    with pytest.raises(UpdateError, match="Signature invalide"):
        manifest.verify(signed, trusted)


def test_another_key_is_refused_even_under_a_trusted_identifier():
    _, trusted = new_key()
    signed = manifest.sign(make(), Ed25519PrivateKey.generate())
    with pytest.raises(UpdateError, match="clé inconnue"):
        manifest.verify(signed, trusted)
    signed["key"] = next(iter(trusted))
    with pytest.raises(UpdateError, match="Signature invalide"):
        manifest.verify(signed, trusted)


def test_by_default_only_the_publication_keys_are_trusted(monkeypatch):
    private, trusted = new_key()
    signed = manifest.sign(make(), private)
    monkeypatch.setattr(keys, "TRUSTED_KEYS", {})
    with pytest.raises(UpdateError):
        manifest.verify(signed)
    monkeypatch.setattr(keys, "TRUSTED_KEYS", trusted)
    assert manifest.verify(signed)["version"] == "2.0.1"


def entry(**change):
    return [{"kind": "source", "name": "x.zip", "size": 10, "sha256": SHA, **change}]


@pytest.mark.parametrize("change", [
    {"version": "../2.0.1"}, {"version": "2.0.1 "}, {"format": 2}, {"app": "autre"},
    {"files": []}, {"files": "x"},
    {"files": entry(name="../x.zip")}, {"files": entry(name="a/b.zip")},
    {"files": entry(name=".x.zip")}, {"files": entry(size=0)}, {"files": entry(size=True)},
    {"files": entry(size=10 ** 12)}, {"files": entry(sha256="A" * 64)},
    {"files": entry(kind="binaire")},                       # pas d'archive du code
    {"files": entry() + entry()},                           # deux fois le même nom
    {"files": entry() + entry(name="y.zip")},               # deux archives du code
])
def test_unsafe_or_malformed_manifests_are_refused(change):
    with pytest.raises(UpdateError):
        manifest.check(make(**change))


def test_files_of_an_unknown_kind_are_ignored():
    private, trusted = new_key()
    data = make()
    data["files"].append({"kind": "aiodos-net-android-arm64", "name": "aiodos-net", "size": 5,
                          "sha256": "b" * 64})
    found = manifest.verify(manifest.sign(data, private), trusted)
    assert [f["name"] for f in manifest.wanted(found)] == ["aiodos-2.0.1.zip"]


def test_each_machine_wants_only_the_companion_of_its_platform(monkeypatch):
    from aiodos.updates import platforms
    data = make()
    for where in ("windows-x86_64", "android-aarch64"):
        data["files"].append({"kind": f"aiodos-net-{where}", "name": f"aiodos-net-2.0.1-{where}",
                              "size": 5, "sha256": "b" * 64})
    manifest.check(data)
    for where in ("windows-x86_64", "android-aarch64"):
        monkeypatch.setattr(platforms, "tag", lambda where=where: where)
        assert [f["kind"] for f in manifest.wanted(data)] == ["source", f"aiodos-net-{where}"]
    for where in ("linux-x86_64", None):                    # pas construit pour elle
        monkeypatch.setattr(platforms, "tag", lambda where=where: where)
        assert [f["kind"] for f in manifest.wanted(data)] == ["source"]
        assert manifest.binary(data) is None


def test_garbage_is_refused():
    _, trusted = new_key()
    kid = next(iter(trusted))
    for junk in (None, [], {"manifest": 1}, {"manifest": "{}", "key": "x", "signature": "!!"},
                 {"manifest": "{}", "key": kid, "signature": "!!"},
                 {"manifest": "{}", "key": kid, "signature": "AAAA"}):
        with pytest.raises(UpdateError):
            manifest.verify(junk, trusted)
