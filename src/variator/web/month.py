"""POST /api/variator/cases/{case_id}/month: the month Monte Carlo on actors for a page state (WP18).

It computes from the learned actors and the history (env ``VARIATOR_FUTURE_ACTORS``,
``VARIATOR_FUTURE_HISTORY``) and writes nothing. The body is the page ``CaseState`` (plus ``forks``);
the answer is ``month.json`` (``variator.future.live.run_state``). n is capped at 2000. A run longer
than 15 s answers 504 (the thread finishes in the background). Same state and seed give the same answer.
"""

# ruff: noqa: E501 - the route description texts, one per line

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Route

from variator.future import live
from variator.future.params import ACTORS_DIR, HISTORY_DIR

TIMEOUT_S = 15.0
MAX_BODY = 256 * 1024
_slots: asyncio.Semaphore | None = None  # at most 2 runs at a time (CPU)
_warm = threading.Event()  # set when the pre-warm has started
log = logging.getLogger(__name__)


def start_prewarm() -> bool:
    """Simulate the default state of both cases in a background thread, once per process.

    Off with ``VARIATOR_FUTURE_PREWARM=0`` and under pytest (the tests call ``live.prewarm`` themselves)."""
    if _warm.is_set() or os.environ.get("VARIATOR_FUTURE_PREWARM", "1") == "0" or "pytest" in sys.modules:
        return False
    _warm.set()

    def work() -> None:
        try:
            log.info("future pre-warm: %s", live.prewarm(*_paths()))
        except Exception:  # noqa: BLE001 - a missing artifact set must not stop the API
            log.warning("future pre-warm failed", exc_info=True)

    threading.Thread(target=work, name="future-prewarm", daemon=True).start()
    return True


def _paths() -> tuple[str, str]:
    actors = os.environ.get("VARIATOR_FUTURE_ACTORS", "").strip() or str(ACTORS_DIR)
    history = os.environ.get("VARIATOR_FUTURE_HISTORY", "").strip() or str(HISTORY_DIR)
    return actors, history


def describe(case_id: str) -> dict:
    """The answer of GET: what the route takes (the availability probe of the page reads it)."""
    return {
        "case": case_id,
        "method": "POST",
        "answer": "month.json of the state (variator.future/1, source: actors)",
        "body": {
            "forks": "list of fork ids that are on (default: all)",
            "activePresets": "list of hypothesis ids",
            "edits": "path -> number: v|<variable>|<key>, o|<option>|<variable>|<key>, r|<risk>|p, "
            "rm|<risk>|<key>, w|<criterion>, w|__risk",
            "prefs": "{weights: {criterion: number}, riskAversion: 0..1}",
            "timeline": "{moves: {month variable: month}, events: [item], off: [item]}",
            "include": 'optional ["week"]: eventTrees (case B: week grid; case A: month grid)',
            "weekForks": f"at most {live.MAX_WEEK_FORKS} fork ids for eventTrees (default: the two best)",
            "weekN": f"paths per fork of the case B event trees, {live.MIN_N}..{live.MAX_WEEK_N}, default {live.WEEK_N}",
            "path drill-down": "GET /cases/{case}/week/path?fork=&path= (defaults) or POST {state..., fork, path}",
            "n": f"{live.MIN_N}..{live.MAX_N} (larger values are capped), default {live.DEFAULT_N}",
            "seed": f"integer, default {live.DEFAULT_SEED}",
        },
        "limits": {
            "timeoutSeconds": TIMEOUT_S,
            "maxN": live.MAX_N,
            "maxBodyBytes": MAX_BODY,
            "parallelRuns": 2,
        },
        "deterministic": "the same state and seed give the same answer (a cache of 32 states)",
    }


async def month(request: Request) -> JSONResponse:
    global _slots
    case_id = request.path_params["case_id"]
    if case_id not in live.CASES:
        raise HTTPException(404, f"unknown case {case_id!r}")
    start_prewarm()  # no-op after the start-up; a fallback when the start-up hook did not run
    if request.method == "GET":
        return JSONResponse(describe(case_id))
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise HTTPException(413, "the body is too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "the body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    actors, history = _paths()
    _slots = _slots or asyncio.Semaphore(2)
    try:
        async with _slots:
            doc = await asyncio.wait_for(
                run_in_threadpool(live.run_state, case_id, body, actors=actors, history=history), TIMEOUT_S
            )
    except live.StateError as exc:
        raise HTTPException(400, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, f"the run takes longer than {TIMEOUT_S:.0f} s; lower n") from None
    except FileNotFoundError as exc:
        raise HTTPException(503, f"the learned actors or the history are missing: {exc}") from None
    return JSONResponse(doc)


async def week_path(request: Request) -> JSONResponse:
    """GET ?fork=&path= (the default state) or POST {state..., fork, path}: one path of a fork's event tree."""
    global _slots
    case_id = request.path_params["case_id"]
    if case_id not in live.CASES:
        raise HTTPException(404, f"unknown case {case_id!r}")
    if request.method == "GET":
        body: dict = {}
        fork, path = request.query_params.get("fork", ""), request.query_params.get("path", "")
    else:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "the body is too large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "the body is not JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "the body must be a JSON object")
        fork, path = str(body.pop("fork", "")), str(body.pop("path", ""))
    if not fork or not path:
        raise HTTPException(400, "fork and path are required")
    actors, history = _paths()
    _slots = _slots or asyncio.Semaphore(2)
    try:
        async with _slots:
            doc = await asyncio.wait_for(
                run_in_threadpool(live.week_path, case_id, body, fork, path, actors=actors, history=history),
                TIMEOUT_S,
            )
    except live.StateError as exc:
        raise HTTPException(400, str(exc)) from None
    except TimeoutError:
        raise HTTPException(504, f"the run takes longer than {TIMEOUT_S:.0f} s") from None
    return JSONResponse(doc)


def routes() -> list[BaseRoute]:
    """Routes relative to the API prefix. Building them (at app start-up) starts the pre-warm."""
    start_prewarm()
    return [
        Route("/cases/{case_id}/month", month, methods=["GET", "POST"]),
        Route("/cases/{case_id}/week/path", week_path, methods=["GET", "POST"]),
    ]
