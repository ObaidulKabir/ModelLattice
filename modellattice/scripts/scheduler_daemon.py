"""Long-running process: refreshes the catalog from every registered
connector, then relationships, on a fixed interval
(CONNECTOR_REFRESH_INTERVAL_HOURS, default 6). Deliberately a separate
process from the web app (app.main) - see app/scheduler.py's docstring
for why, and for why the evaluation runner is NOT scheduled here.

Usage:
    .venv/Scripts/python.exe -m scripts.scheduler_daemon

Runs until stopped (Ctrl+C). For production, run it as its own service
(systemd unit, Windows Task Scheduler entry, or a separate container) -
not built here, since this MVP runs everything from one dev machine.
"""
import logging
import signal
import sys

from app.scheduler import build_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    scheduler = build_scheduler()
    scheduler.start()
    logging.getLogger("modellattice.scheduler").info(
        "scheduler started; refreshing every %s", scheduler.get_job("refresh_all").trigger
    )

    def _shutdown(signum, frame):
        logging.getLogger("modellattice.scheduler").info("shutting down...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    signal.pause() if hasattr(signal, "pause") else _block_forever()


def _block_forever() -> None:
    import time

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
