"""Optional local-only embeddings with explicit artifact and generation identity."""

import hashlib
import importlib
import math
import re
import sqlite3
import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mimir.ledger import _hash


class EmbeddingError(ValueError):
    """Bounded failure without model paths, source content or provider error text."""


@dataclass(frozen=True)
class Generation:
    id: str
    model: str
    artifact_digest: str
    dimensions: int
    normalization: str
    tokenizer_digest: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", self.id)
            or not self.model
            or len(self.model) > 160
            or self.normalization != "unit"
            or type(self.dimensions) is not int
            or not 1 <= self.dimensions <= 4096
            or not re.fullmatch(r"[a-f0-9]{64}", self.artifact_digest)
            or not re.fullmatch(r"[a-f0-9]{64}", self.tokenizer_digest)
        ):
            raise EmbeddingError("Invalid embedding generation")

    def values(self) -> tuple[str, str, str, int, str, str]:
        return (
            self.id,
            self.model,
            self.artifact_digest,
            self.dimensions,
            self.normalization,
            self.tokenizer_digest,
        )


class Embedder(Protocol):
    generation: Generation

    def embed(self, text: str, *, query: bool = False) -> Sequence[float]: ...


def vector_blob(values: Sequence[float], dimensions: int) -> bytes:
    if type(dimensions) is not int or not 1 <= dimensions <= 4096 or len(values) != dimensions:
        raise EmbeddingError("Invalid vector dimensions")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise EmbeddingError("Invalid vector values")
    try:
        blob = struct.pack("<" + "f" * dimensions, *values)
        packed = struct.unpack("<" + "f" * dimensions, blob)
    except (OverflowError, struct.error):
        raise EmbeddingError("Invalid float32 vector") from None
    norm = math.sqrt(sum(value * value for value in packed))
    if not all(math.isfinite(value) for value in packed) or norm == 0 or abs(norm - 1) > 0.001:
        raise EmbeddingError("Expected unit-normalized vector")
    return blob


def load_vec(db: sqlite3.Connection) -> bool:
    try:
        module = importlib.import_module("sqlite_vec")
        db.enable_load_extension(True)
        try:
            module.load(db)
        finally:
            db.enable_load_extension(False)
        return bool(db.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9")
    except (ImportError, AttributeError, sqlite3.Error):
        return False


class LocalEmbedder:
    """One lazy process-wide FastEmbed session, shared by query and document calls.

    Manifest paths and SHA256 digests are supplied by an explicit provisioning
    step; no cache discovery or downloads are performed. A different generation
    requires restarting the owning process.
    """

    _lock = threading.Lock()
    _session: Any = None
    _session_key: tuple[Any, ...] | None = None

    def __init__(self, generation: Generation, directory: Path, manifest: dict[str, str]):
        self.generation = generation
        self.directory = directory
        self.manifest = dict(manifest)

    def _verify(self) -> None:
        generation = self.generation
        required = {
            "model_optimized.onnx",
            "tokenizer.json",
            "config.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
        }
        if (
            generation.model != "BAAI/bge-small-en-v1.5"
            or generation.dimensions != 384
            or generation.normalization != "unit"
            or not required <= self.manifest.keys()
            or _hash(self.manifest) != generation.artifact_digest
            or self.manifest.get("tokenizer.json") != generation.tokenizer_digest
            or self.directory.is_symlink()
            or not self.directory.is_dir()
        ):
            raise EmbeddingError("Invalid offline model manifest")
        for filename, expected in self.manifest.items():
            path = Path(filename)
            if path.is_absolute() or ".." in path.parts:
                raise EmbeddingError("Invalid offline model manifest")
            target = self.directory
            for part in path.parts:
                target = target / part
                if target.is_symlink():
                    raise EmbeddingError("Invalid offline model artifact")
            if not target.is_file() or target.stat().st_size > 512 * 1024 * 1024:
                raise EmbeddingError("Invalid offline model artifact")
            digest = hashlib.sha256()
            with target.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise EmbeddingError("Offline model digest mismatch")
        for target in self.directory.rglob("*"):
            if target.is_symlink() or (
                target.is_file()
                and target.relative_to(self.directory).as_posix() not in self.manifest
            ):
                raise EmbeddingError("Unverified offline model artifact")

    def embed(self, text: str, *, query: bool = False) -> Sequence[float]:
        if type(text) is not str or not text or len(text.encode("utf-8")) > 16384:
            raise EmbeddingError("Invalid embedding text")
        with self._lock:
            key = (*self.generation.values(), str(self.directory.absolute()), _hash(self.manifest))
            if self._session_key is not None and self._session_key != key:
                raise EmbeddingError("Another embedding generation owns the session")
            try:
                if self._session is None:
                    self._verify()
                    module = importlib.import_module("fastembed")
                    session = module.TextEmbedding(
                        model_name=self.generation.model,
                        specific_model_path=str(self.directory.absolute()),
                        local_files_only=True,
                        threads=1,
                        providers=["CPUExecutionProvider"],
                    )
                    type(self)._session = session
                    type(self)._session_key = key
                method = self._session.query_embed if query else self._session.embed
                result = next(iter(method([text], batch_size=1, parallel=None)))
                values = [float(value) for value in result]
                vector_blob(values, self.generation.dimensions)
                return values
            except EmbeddingError:
                raise
            except Exception:
                raise EmbeddingError("Local embedding unavailable") from None
