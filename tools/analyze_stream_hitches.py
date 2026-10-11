"""Correlate completed Sim monitor waits with matching collection operations.

Usage: python tools/analyze_stream_hitches.py path/to/skate3_034.log
All timings use log wall-clock timestamps; operations are async, not raw I/O.
"""
import argparse
import datetime as dt
import json
import re
from pathlib import Path


def analyze(path):
    rows = []
    pending = None
    operations = []
    kind = None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        stamp = re.match(r"\[([\d-]+ [\d:.]+)\]", line)
        if not stamp:
            continue
        now = dt.datetime.fromisoformat(stamp[1])
        monitor = re.search(r"world-stream-monitor: kind=(\d).*?wait=(\d)", line)
        if monitor:
            kind = int(monitor[1])
            if kind == 1:
                if monitor[2] == "1" and pending is None:
                    pending = {"start": now, "id": None}
                elif monitor[2] == "0" and pending:
                    pending["end"] = now
                    rows.append(pending)
                    pending = None
        cell = re.search(r"world-stream-cell: kind=1 id=([0-9A-F]+)", line)
        if cell and pending and pending["id"] is None:
            pending["id"] = cell[1]
        operation = re.search(
            r"world-stream-operation: id=([0-9A-F]+) operation=(\w+) phase=(\w+)", line
        )
        if operation:
            operations.append((now, *operation.groups()))

    result = []
    ms = lambda a, b: round((b - a).total_seconds() * 1000, 3)
    for row in rows:
        start, end, cell_id = row["start"], row["end"], row["id"]
        # Do not accidentally match another visit to the same cell.
        loads = [op for op in operations if start <= op[0] <= end
                 and op[1] == cell_id and op[2] == "load"]
        issued = next((op[0] for op in loads if op[3] == "start"), None)
        completed = next((op[0] for op in loads if op[3] == "complete"), None)
        unloads = [op[0] for op in operations if start <= op[0] <= (issued or end)
                   and op[2:] == ("unload", "complete")]
        result.append({
            "pause_start": start.isoformat(timespec="milliseconds"),
            "cell_id": cell_id,
            "wait_ms": ms(start, end),
            "before_load_ms": ms(start, issued) if issued else None,
            "load_ms": ms(issued, completed) if issued and completed else None,
            "after_load_ms": ms(completed, end) if completed else None,
            "last_unload_to_load_ms": ms(unloads[-1], issued) if unloads and issued else None,
        })
    return {"log": str(path), "completed_sim_waits": result,
            "unfinished_sim_wait": pending is not None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.log), indent=2))
