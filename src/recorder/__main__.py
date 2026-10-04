"""``python -m recorder [--config recorder.toml]`` - start the cold-start recorder."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
from pathlib import Path

from cdx_http.client import HttpClient
from cdx_http.signed import load_credentials

from .config import load_config
from .runner import Recorder
from .storage import JsonlStore, SingleInstanceLock
from .tasks import build_tasks


def setup_logging(log_dir: Path, console_level: str) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if sys.stdout is not None:          # None under pythonw / fully detached launch
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(console_level)
        console.setFormatter(fmt)
        root.addHandler(console)
    fileh = logging.handlers.RotatingFileHandler(log_dir / "recorder.log", maxBytes=5_000_000, backupCount=5,
                                                 encoding="utf-8")
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(fmt)

    root.addHandler(fileh)
    # urllib3 logs full URLs at DEBUG; harmless (no secrets in URLs) but noisy.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="recorder", description=__doc__)
    ap.add_argument("--config", default="recorder.toml")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg.log_dir, cfg.console_log_level)
    log = logging.getLogger("recorder")

    lock = SingleInstanceLock(cfg.data_dir / "_meta" / "recorder.lock")
    if not lock.acquire():
        log.error("another recorder is already writing to %s - exiting", cfg.data_dir)
        return 2

    pos = cfg.stream("positioning")
    creds = load_credentials(cfg.env_file) if pos.enabled and pos.options.get("auth", "none") == "signed" else None
    http = HttpClient(host_rates=cfg.host_rates, timeout_s=cfg.timeout_s)
    store = JsonlStore(cfg.data_dir)
    tasks = build_tasks(cfg, creds, log)
    if not tasks:
        log.error("no streams enabled - nothing to do")
        return 1
    rec = Recorder(tasks, http, store, health_interval_s=cfg.health_interval_s,
                   secrets=creds.secrets() if creds else ())

    def _stop(signum, _frame):
        log.info("signal %s received - shutting down", signum)
        rec.stop()

    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)
    log.info("data dir: %s", cfg.data_dir)
    try:
        rec.run_forever()
    except KeyboardInterrupt:
        # run_forever's finally has already flushed, closed and written the shutdown marker
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
