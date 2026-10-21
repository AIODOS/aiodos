"""Les octets d'un fichier produit pour une appli (gateway/files_api.py) : une réponse coupée
compte ce qui est parti, et la plage reprise ensuite rend le fichier récupéré (sa garde de
7 jours commence) ; une plage qui n'atteint pas la fin ne le rend jamais récupéré."""
import asyncio
import hashlib

from starlette.requests import Request

from aiodos.gateway import file_store, files_api

APP = "abcdefgh"


def _request(headers=None):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/aiodos/v1/files/x/content",
                    "headers": raw, "query_string": b""})


def _stored(tmp_path, size):
    data = bytes(range(256)) * (size // 256)
    source = tmp_path / "1-sortie.bin"
    source.write_bytes(data)
    public = file_store.add(APP, 1, "job_essai", 0, source, node="n",
                            kind="application/octet-stream", size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), machine=None,
                            place="here", engine="comfyui")
    return public["id"], data


async def _read(response, blocks=None):
    """Lit la réponse ; avec blocks, la coupe après autant de blocs (l'appli s'en va)."""
    got = b""
    iterator = response.body_iterator
    try:
        async for block in iterator:
            got += block
            if blocks is not None:
                blocks -= 1
                if not blocks:
                    break
    finally:
        await iterator.aclose()
    return got


def test_a_cut_download_counts_and_the_resumed_range_completes_it(tmp_path):
    file_id, data = _stored(tmp_path, 3 * files_api.CHUNK)
    cut = files_api.serve(file_store.get_row(file_id), _request())
    got = asyncio.run(_read(cut, blocks=1))
    assert got == data[:files_api.CHUNK]
    row = file_store.get_row(file_id)
    assert row["served"] == files_api.CHUNK and row["fetched_at"] is None
    rest = files_api.serve(row, _request({"Range": f"bytes={len(got)}-"}))
    assert rest.status_code == 206
    assert asyncio.run(_read(rest)) == data[len(got):]
    row = file_store.get_row(file_id)
    assert row["served"] == len(data) and row["fetched_at"] is not None


def test_a_range_short_of_the_end_never_fetches(tmp_path):
    file_id, data = _stored(tmp_path, 2 * files_api.CHUNK)
    for _ in range(3):                       # relue trois fois : plus d'octets que sa taille
        part = files_api.serve(file_store.get_row(file_id),
                               _request({"Range": f"bytes=0-{len(data) - 2}"}))
        assert asyncio.run(_read(part)) == data[:-1]
    row = file_store.get_row(file_id)
    assert row["served"] > len(data) and row["fetched_at"] is None
    whole = files_api.serve(row, _request())
    assert asyncio.run(_read(whole)) == data
    assert file_store.get_row(file_id)["fetched_at"] is not None
