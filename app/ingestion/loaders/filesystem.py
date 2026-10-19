"""Filesystem loader.

Resolves the ``CatalogSource.location`` against the project's
``INGESTION_DATA_ROOT``, walks the resulting path, and yields one
``LoadedBlob`` per file whose extension matches the source's declared format.

Two failure modes are hard errors on purpose:

* **Missing location** — a catalog that points nowhere is silently wrong, the
  worst kind of broken, so it raises ``FileNotFoundError``.
* **Escaping the data root** — a ``location`` of ``../../etc`` (or an absolute
  path, or a symlink that resolves outside) raises ``ValueError``. The catalog
  is a file other people edit; the loader is the boundary that keeps "a
  catalog entry" from becoming "read any file on the host". The reference
  implementation had no such guard; this one does.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadedBlob:
    """A single file's bytes plus the path relative to the data root."""

    relative_path: str
    bytes_: bytes


class FileSystemLoader:
    def __init__(self, data_root: Path | str) -> None:
        self._data_root = Path(data_root).resolve()

    @property
    def data_root(self) -> Path:
        return self._data_root

    def resolve(self, location: str) -> Path:
        """Resolve ``location`` under the data root, refusing escapes.

        Raises:
            ValueError: the resolved path is not inside the data root.
        """
        if not isinstance(location, str) or not location.strip():
            raise ValueError("catalog location must be a non-empty string")
        candidate = (self._data_root / location).resolve()
        if candidate != self._data_root and self._data_root not in candidate.parents:
            # Do not echo the resolved path: it is host detail, not user input.
            raise ValueError(
                f"catalog location {location!r} escapes the configured data root"
            )
        return candidate

    def iter_blobs(self, location: str, formats: set[str]) -> Iterator[LoadedBlob]:
        """Yield every file under ``location`` whose extension is in ``formats``."""
        path = self.resolve(location)
        if not path.exists():
            raise FileNotFoundError(
                f"catalog location {location!r} does not resolve to a real path "
                "under the configured data root"
            )
        if path.is_file():
            candidates = [path]
        else:
            candidates = sorted(p for p in path.rglob("*") if p.is_file())
        wanted = {fmt.lower().lstrip(".") for fmt in formats}
        for candidate in candidates:
            ext = candidate.suffix.lower().lstrip(".")
            if ext not in wanted:
                continue
            yield LoadedBlob(
                relative_path=candidate.relative_to(self._data_root).as_posix(),
                bytes_=candidate.read_bytes(),
            )
