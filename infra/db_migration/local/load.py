"""Continuous write load through the fake Worker, then check the target holds every
successful write. Each thread owns its rows, so the expected final state is exact.

    python load.py <seconds> <rps>
"""

import os
import random
import sys
import threading
import time
import uuid

import httpx
import psycopg

URL = "http://localhost:8081/api/items"
DURATION_S = float(sys.argv[1]) if len(sys.argv) > 1 else 20
RPS = float(sys.argv[2]) if len(sys.argv) > 2 else 10
THREADS = 4

lock = threading.Lock()
expected: dict[uuid.UUID, tuple[str, str] | None] = {}
results = {"ok": 0, "failed": 0, "by_origin": {}, "max_ms": 0.0, "errors": []}


def worker(seed: int) -> None:
    rnd = random.Random(seed)
    mine: list[uuid.UUID] = []
    interval = THREADS / RPS
    end = time.monotonic() + DURATION_S
    with httpx.Client(timeout=40) as http:
        while time.monotonic() < end:
            tick = time.monotonic()
            if not mine or rnd.random() < 0.5:
                body = {"op": "insert", "id": str(uuid.uuid4()), "title": f"load {seed}", "status": "todo"}
            elif rnd.random() < 0.8:
                body = {"op": "update", "id": str(rnd.choice(mine)), "title": f"upd {tick:.3f}",
                        "status": rnd.choice(["todo", "in_progress", "done"])}
            else:
                victim = mine.pop(rnd.randrange(len(mine)))
                body = {"op": "delete", "id": str(victim)}
            try:
                resp = http.post(URL, json=body)
                elapsed = (time.monotonic() - tick) * 1000
                ok = resp.status_code == 200
            except httpx.HTTPError as exc:
                ok, elapsed, resp = False, 0.0, exc
            with lock:
                if not ok:
                    results["failed"] += 1
                    results["errors"].append(str(getattr(resp, "text", resp))[:120])
                    # Unknown outcome: drop the row from the exact check.
                    expected.pop(uuid.UUID(body["id"]), None)
                    continue
                results["ok"] += 1
                origin = resp.json()["served_by"]
                results["by_origin"][origin] = results["by_origin"].get(origin, 0) + 1
                results["max_ms"] = max(results["max_ms"], elapsed)
                rid = uuid.UUID(body["id"])
                if body["op"] == "insert":
                    mine.append(rid)
                    expected[rid] = (body["title"], body["status"])
                elif body["op"] == "update":
                    expected[rid] = (body["title"], body["status"])
                else:
                    expected[rid] = None
            time.sleep(max(0.0, interval - (time.monotonic() - tick)))


def main() -> None:
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    missing = wrong = 0
    with psycopg.connect(os.environ["TARGET_DSN"]) as conn:
        for rid, want in expected.items():
            row = conn.execute("SELECT title, status FROM work_items WHERE id = %s", (rid,)).fetchone()
            if want is None and row is not None:
                wrong += 1
            elif want is not None and row is None:
                missing += 1
            elif want is not None and tuple(row) != want:
                wrong += 1
    print({"requests_ok": results["ok"], "requests_failed": results["failed"],
           "by_origin": results["by_origin"], "max_request_ms": round(results["max_ms"], 1),
           "rows_checked": len(expected), "missing_on_target": missing, "wrong_on_target": wrong,
           "errors": results["errors"][:3]})


if __name__ == "__main__":
    main()
