"""Worker process: drains the outbox of every tenant through all registered consumers, forever."""
from __future__ import annotations

import logging
import signal
import time

from kb.db import connect, migrate
from kb.settings import get_settings
from kb.worker.framework import drain, load_consumers

log = logging.getLogger("kb.worker")
_stop = False


def _on_signal(*_: object) -> None:
    global _stop
    _stop = True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    s = get_settings()
    migrate()
    log.info("consumers: %s", ", ".join(sorted(load_consumers())))
    while not _stop:
        busy = False
        try:
            with connect() as conn:
                tenants = [r["tenant_id"] for r in conn.execute(
                    "SELECT tenant_id FROM tenant WHERE coalesce((settings->>'background')::boolean, true)").fetchall()]
                for t in tenants:
                    done = drain(conn, t, batch=s.worker_batch, raise_errors=False)
                    if any(v > 0 for v in done.values()):
                        busy = True
                        log.info("tenant %s: %s", t, done)
        except Exception:
            log.exception("drain failed; retrying")
            time.sleep(5)
        if not busy:
            time.sleep(s.worker_poll_s)


if __name__ == "__main__":
    main()
