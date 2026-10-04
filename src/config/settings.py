"""Operational settings — deliberately NOT model parameters.

The distinction is the one every spec repeats: these affect *timeliness, storage and numerical
guards*; they can never change a signal's content. Cadences, retention windows, epsilons and paths
live here. Anything that can change a published number lives in ``config/parameters.toml`` and is
counted against the cap.

Precedence: environment variables (prefix ``CT_``) override the TOML file, which overrides these
defaults. Secrets are NOT read here — ``src/cdx_http/signed.py`` is the only module that touches
credentials (specs/10 §9).
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Paths(BaseModel):
    data_dir: Path = REPO_ROOT / "data"
    raw_dir: Path = REPO_ROOT / "data" / "raw"
    backfill_dir: Path = REPO_ROOT / "data" / "backfill"
    parquet_dir: Path = REPO_ROOT / "data" / "parquet"
    holdout_dir: Path = REPO_ROOT / "data" / "holdout"
    state_db: Path = REPO_ROOT / "data" / "state.db"
    log_dir: Path = REPO_ROOT / "logs"
    parameters_file: Path = REPO_ROOT / "config" / "parameters.toml"


class Polling(BaseModel):
    """specs/11 §2.2 cadences, in seconds. Priorities: 0 highest, 3 dropped first."""

    futures_rt_s: float = 15
    spot_ticker_s: float = 30
    candles_1m_s: float = 60
    candles_1m_offset_s: float = 5
    candles_15m_offset_s: float = 10
    candles_1h_offset_s: float = 15
    candles_1d_offset_s: float = 30
    positioning_s: float = 900
    orderbook_s: float = 300
    instrument_meta_s: float = 21600
    #: bars re-fetched each poll for the two-fetch finalization rule (specs/11 §6.1)
    refetch_bars: int = 5

    @field_validator("refetch_bars")
    @classmethod
    def _at_least_two_bars(cls, v: int) -> int:
        if v < 2:
            raise ValueError("refetch_bars must be >= 2: the two-fetch finalization rule needs to compare two fetches")
        return v


class Freshness(BaseModel):
    """DQ gating windows (specs/11 §9). Informed by F-026: measured skew within +/-2 s."""

    futures_rt_max_age_s: float = 60
    spot_ticker_max_age_s: float = 120
    book_max_age_s: float = 600
    positioning_max_age_s: float = 2700
    clock_skew_tolerance_s: float = 3.0   # F-026 / D-10: no tighter than +/-3 s
    stale_after_s: float = 300            # UI turns a value red past this (specs/15 §9)


class Numerics(BaseModel):
    """Guard epsilons (specs/12 §4). These can turn a value into NUMERICAL_FAILURE; they can
    never change a successfully computed value, so they are not model parameters."""

    zero_variance_eps: float = 1e-12
    zero_stddev_eps: float = 1e-12
    zero_mad_eps: float = 1e-12
    tstat_se_rel_eps: float = 1e-9


class Retention(BaseModel):
    """specs/13 §9.2 and specs/11 §8.3."""

    rejection_full_detail_days: int = 30
    book_snapshot_days: int = 90
    quality_rollup_days: int = 400


class Backup(BaseModel):
    """Azure Blob backup (latest-updates.md AZ-4). Authentication is by VM managed identity —
    there is no key or connection string to store."""

    enabled: bool = False
    account_url: str = ""                  # e.g. https://<account>.blob.core.windows.net
    raw_container: str = "raw"
    state_container: str = "state"
    holdout_container: str = "holdout"
    nightly_utc_hour: int = 2

    @field_validator("nightly_utc_hour")
    @classmethod
    def _valid_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("nightly_utc_hour must be in 0..23")
        return v


class Server(BaseModel):
    host: str = "127.0.0.1"                # specs/10 §9 / specs/14 §11: localhost by default
    port: int = 8000
    auth_token: str = ""                   # required before any non-localhost bind is permitted


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CT_", env_nested_delimiter="__", extra="forbid")

    environment: str = "local"             # "local" | "azure"
    paths: Paths = Paths()
    polling: Polling = Polling()
    freshness: Freshness = Freshness()
    numerics: Numerics = Numerics()
    retention: Retention = Retention()
    backup: Backup = Backup()
    server: Server = Server()

    @field_validator("environment")
    @classmethod
    def _known_environment(cls, v: str) -> str:
        if v not in ("local", "azure"):
            raise ValueError("environment must be 'local' or 'azure'")
        return v

    def assert_bind_allowed(self) -> None:
        """specs/14 §11: refuse a non-localhost bind without a token. Exposure without
        authentication is not a supported configuration."""
        if self.server.host not in ("127.0.0.1", "localhost", "::1") and not self.server.auth_token:
            raise RuntimeError(
                f"refusing to bind {self.server.host}: a non-localhost bind requires CT_SERVER__AUTH_TOKEN "
                "(specs/14 §11). On Azure, keep the localhost bind and reach the dashboard over "
                "Tailscale/WireGuard or an SSH tunnel instead of opening a port."
            )


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings, optionally overlaying a TOML file. Env vars still win."""
    if path is None:
        return Settings()
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return Settings(**data)
