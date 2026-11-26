from pathlib import Path


def bounded_log(path, maximum=8_000_000):
    path = Path(path)
    if path.exists() and path.stat().st_size > maximum:
        with path.open("rb") as source:
            source.seek(-maximum // 2, 2)
            tail = source.read()
        path.write_bytes(tail)
    return path
