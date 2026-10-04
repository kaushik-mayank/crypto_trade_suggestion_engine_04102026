"""Load and validate ``recorder.toml``."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SPOT_INTERVAL_MS = {"1m": 60_000, "15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}
FUTURES_RESOLUTION_MS = {"1": 60_000, "5": 300_000, "60": 3_600_000, "1D": 86_400_000}

KNOWN_STREAMS = {"futures_rt", "positioning", "spot_ticker", "instrument_meta", "futures_book", "spot_book", "candles"}


@dataclass(frozen=True)
class SpotMarket:
    pair: str
    ticker_market: str


@dataclass
class StreamConfig:
    enabled: bool
    interval_s: float
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    data_dir: Path
    log_dir: Path
    env_file: Path
    console_log_level: str
    health_interval_s: float
    timeout_s: float
    host_rates: dict[str, float]
    futures_pairs: list[str]
    spot: list[SpotMarket]
    streams: dict[str, StreamConfig]

    def stream(self, name: str) -> StreamConfig:
        return self.streams.get(name, StreamConfig(False, 0))


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent

    def p(v: str) -> Path:
        q = Path(v)
        return q if q.is_absolute() else base / q

    streams: dict[str, StreamConfig] = {}
    for name, s in raw.get("streams", {}).items():
        if name not in KNOWN_STREAMS:
            raise ValueError(f"unknown stream {name!r}")
        s = dict(s)
        enabled = bool(s.pop("enabled", True))
        interval = float(s.pop("interval_s"))
        if interval <= 0:
            raise ValueError(f"{name}: interval_s must be > 0")
        streams[name] = StreamConfig(enabled, interval, s)

    c = streams.get("candles")
    if c:
        for iv in c.options.get("spot_intervals", []):
            if iv not in SPOT_INTERVAL_MS:
                raise ValueError(f"spot interval {iv!r} not served (served: {list(SPOT_INTERVAL_MS)})")
        for r in c.options.get("futures_resolutions", []):
            if r not in FUTURES_RESOLUTION_MS:
                raise ValueError(f"futures resolution {r!r} not served (served: {list(FUTURES_RESOLUTION_MS)})")
    pos = streams.get("positioning")
    if pos:
        if pos.options.get("method", "GET") not in ("GET", "POST"):
            raise ValueError("positioning.method must be GET or POST")
        if pos.options.get("timestamp_unit", "ms") not in ("ms", "s"):
            raise ValueError("positioning.timestamp_unit must be ms or s")
        if pos.options.get("auth", "none") not in ("none", "signed"):
            raise ValueError("positioning.auth must be none or signed")

    http = raw.get("http", {})
    return Config(
        data_dir=p(raw.get("data_dir", "data/raw")),
        log_dir=p(raw.get("log_dir", "logs")),
        env_file=p(raw.get("env_file", ".env")),
        console_log_level=str(raw.get("console_log_level", "INFO")).upper(),
        health_interval_s=float(raw.get("health_interval_s", 900)),
        timeout_s=float(http.get("timeout_s", 20)),
        host_rates={k: float(v) for k, v in http.get("host_rates", {}).items()},
        futures_pairs=list(raw.get("pairs", {}).get("futures", [])),
        spot=[SpotMarket(s["pair"], s["ticker_market"]) for s in raw.get("spot", [])],
        streams=streams,
    )
