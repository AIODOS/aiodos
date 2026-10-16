"""Le projet s'appelait Cœur IA : ses données sont reprises sous le nouveau nom."""
from aiodos import paths


def test_old_database_and_its_journal_are_adopted(data_dir):
    (data_dir / "coeur.db").write_bytes(b"base")
    (data_dir / "coeur.db-wal").write_bytes(b"dernieres ecritures")
    path = paths.db_path()
    assert path.name == "aiodos.db" and path.read_bytes() == b"base"
    assert (data_dir / "aiodos.db-wal").read_bytes() == b"dernieres ecritures"
    assert not (data_dir / "coeur.db").exists()


def test_an_existing_aiodos_database_is_never_replaced(data_dir):
    (data_dir / "aiodos.db").write_bytes(b"nouvelle")
    (data_dir / "coeur.db").write_bytes(b"ancienne")
    assert paths.db_path().read_bytes() == b"nouvelle"
    assert (data_dir / "coeur.db").read_bytes() == b"ancienne"
