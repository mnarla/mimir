"""Small, strict local configuration; never echo submitted values in errors."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Configuration cannot be used safely."""


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    busy_timeout_ms: int = 3000
    cache_kib: int = 8192


def load_config(config: Path | None = None, data_dir: Path | None = None) -> Settings:
    storage: dict[str, object] = {}
    base = Path.cwd()
    if config is not None:
        try:
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError("Cannot read valid TOML configuration") from exc
        if set(parsed) - {"storage"}:
            raise ConfigError("Unknown configuration section")
        candidate = parsed.get("storage", {})
        if not isinstance(candidate, dict):
            raise ConfigError("Storage configuration must be a table")
        storage = candidate
        base = config.absolute().parent
    if set(storage) - {"data_dir", "busy_timeout_ms", "cache_kib"}:
        raise ConfigError("Unknown storage setting")
    raw_dir = storage.get("data_dir", "~/.local/share/brain")
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ConfigError("Storage path must be nonempty text")
    timeout = storage.get("busy_timeout_ms", 3000)
    cache = storage.get("cache_kib", 8192)
    if type(timeout) is not int or not 0 <= timeout <= 30000:
        raise ConfigError("Busy timeout must be an integer between 0 and 30000")
    if type(cache) is not int or not 256 <= cache <= 16384:
        raise ConfigError("Cache size must be an integer between 256 and 16384 KiB")
    chosen = Path(raw_dir).expanduser() if data_dir is None else data_dir.expanduser()
    if not chosen.is_absolute():
        chosen = (base if data_dir is None else Path.cwd()) / chosen
    return Settings(chosen.absolute(), timeout, cache)
