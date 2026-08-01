"""Load TOML config with a gitignored local override layered on top.

Shipped defaults live in trust/config/<name>.toml. A self-hoster's
personal overrides live in trust/config/<name>.local.toml (gitignored)
and are deep-merged over the defaults.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(__file__).parent / "config"

# Cache parsed config keyed on (name, dir) with an mtime signature, so the
# per-tool-call hot path stats two files instead of re-parsing them every call.
_CACHE: dict = {}


def _mtime(path: Path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, val in over.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(name: str, config_dir=None) -> dict:
    """Load <name>.toml, deep-merging <name>.local.toml over it when present.

    Result is cached on an (base, local) mtime signature and a fresh deep copy is
    returned each call, so callers may mutate freely without poisoning the cache.
    """
    d = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    base_p = d / f"{name}.toml"
    local_p = d / f"{name}.local.toml"
    sig = (_mtime(base_p), _mtime(local_p))
    key = (name, str(d))
    cached = _CACHE.get(key)
    if cached is None or cached[0] != sig:
        merged = _deep_merge(_read_toml(base_p), _read_toml(local_p))
        _CACHE[key] = (sig, merged)
        cached = _CACHE[key]
    return copy.deepcopy(cached[1])
