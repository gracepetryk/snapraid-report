#!/usr/bin/env python3
"""Render example reports from doctored copies of live snapraidd data.

Each scenario (all good, warning, error, still running, missed run) swaps the
daemon's task history for a fake one while keeping the real array and disk
data, so layout changes can be checked without waiting for a real failure.

    uv run preview/preview.py            # HTML + phone/desktop PNGs in preview/out/
    uv run preview/preview.py --no-png   # HTML only
    uv run preview/preview.py --send     # email them, subjects prefixed EXAMPLE
"""
import argparse
import copy
import importlib.util
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any

HERE = os.path.dirname(os.path.realpath(__file__))
OUT = os.path.join(HERE, "out")

# snapraid-report.py isn't an importable module name, so load it by path.
spec = importlib.util.spec_from_file_location("report", os.path.join(HERE, "..", "snapraid-report.py"))
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

Json = dict[str, Any]

NOW = datetime.now()
QUEUED_MIN_AGO = 240  # the nightly run was queued at 4am; the report goes out at 8am
VIEWPORTS = {"desktop": 720, "phone": 390}


@dataclass
class Scenario:
    name: str
    history: list[Json]  # finished tasks, oldest first
    array: Json
    disks: Json
    active: list[Json] = field(default_factory=list)  # the step still running, if any


def ts(min_ago: float) -> str:
    return (NOW - timedelta(minutes=min_ago)).isoformat(timespec="seconds")


def task(number: int, command: str, started_min_ago: float, finished_min_ago: float | None, **extra: Any) -> Json:
    """A maintenance step queued with the rest of the fake run at QUEUED_MIN_AGO."""
    return {
        "number": number, "command": command, "high_command": "maintenance", "health": "passed",
        "status": "terminated", "exit_code": 0, "scheduled_at": ts(QUEUED_MIN_AGO),
        "started_at": ts(started_min_ago),
        "finished_at": ts(finished_min_ago) if finished_min_ago is not None else None,
        "messages": [],
    } | extra


def disk(disks: Json, name: str) -> Json:
    return next(d for _, d in report.all_disks(disks) if d["name"] == name)


def scenarios(live: Json) -> Iterator[Scenario]:
    array = copy.deepcopy(live["array"])
    array.update(diff_added=6, diff_updated=1)
    array["diffs"] = [
        *({"change": "added", "disk": "d1", "path": f"tv/Andor/Season 02/Andor.S02E{e:02d}.mkv"} for e in range(1, 7)),
        {"change": "updated", "disk": "d3", "path": "docs/notes.txt"},
    ]
    yield Scenario("info", array=array, disks=live["disks"], history=[
        task(401, "up", 240, 240),
        task(402, "sync", 240, 236, size_done_bytes=2_140_000_000),
        task(403, "scrub", 236, 198, size_done_bytes=485_000_000_000),
        task(404, "report", 198, 198, report_output="HEALTH:  [passed]\nSTATUS: All nominal\nBAD BLOCKS: 0\n"),
    ])

    # Sync aborted by the deletion threshold, scrub skipped.
    array = copy.deepcopy(live["array"])
    array.update(diff_added=212, diff_removed=73, diff_updated=4, diff_moved=18, blocks_unsynced=41822)
    array["diffs"] = [
        *({"change": "removed", "disk": "d2", "path": f"tv/Severance/Season 01/Severance.S01E{e:02d}.mkv"}
          for e in range(1, 10)),
        *({"change": "added", "disk": "d1", "path": f"movies/Dune Part Two (2024)/Dune.Part.Two.{i}.mkv"}
          for i in range(3)),
    ]
    yield Scenario("warning", array=array, disks=live["disks"], history=[
        task(101, "up", 240, 240),
        task(102, "diff", 240, 235),
        task(103, "sync", 235, 235, status="canceled", exit_code=None,
             exit_msg="Sync suspended: 73 deleted files reached sync_threshold_deletes (50)."),
        task(104, "scrub", 235, 235, status="canceled", exit_code=None,
             exit_msg="Skipped because the previous sync did not complete."),
    ])

    # I/O errors on a data disk during sync, silent errors found in scrub.
    array = copy.deepcopy(live["array"])
    array.update(diff_added=38, diff_updated=2, blocks_bad=17, health="prefail")
    array["diffs"] = [{"change": "added", "disk": "d3", "path": f"music/Chappell Roan/Midwest Princess/{i:02d}.flac"}
                      for i in range(1, 12)]
    disks = copy.deepcopy(live["disks"])
    d2 = disk(disks, "d2")
    d2.update(health="prefail", error_io=9, error_data=8)
    d2["devices"][0].update(health="prefail", failure_probability=0.41)
    yield Scenario("error", array=array, disks=disks, history=[
        task(201, "up", 240, 240),
        task(202, "diff", 240, 236),
        task(203, "sync", 236, 181, health="prefail", exit_code=1, size_done_bytes=48_300_000_000,
             health_reason="Physical I/O errors on disk d2.",
             messages=[{"level": "error", "type": "hardware",
                        "text": "Read error at position 1843021 on disk 'd2' (/dev/sdd): Input/output error"}]),
        task(204, "scrub", 181, 54, health="corrupt", exit_code=1, size_done_bytes=505_700_000_000,
             health_reason="8 silent data errors found; run heal.",
             messages=[{"level": "error", "type": "hardware",
                        "text": "Data error in file 'd2:tv/The Bear/Season 02/The.Bear.S02E06.mkv' at position 412"}]),
    ])

    # A big sync still going when the report runs.
    yield Scenario(
        "running",
        array=live["array"],
        disks=live["disks"],
        history=[task(501, "up", 240, 240), task(502, "diff", 240, 232)],
        active=[task(503, "sync", 232, None, status="processing", exit_code=None, health="pending",
                     progress=62, elapsed_seconds=232 * 60, size_done_bytes=1_310_000_000_000)],
    )

    # The last run was two days ago.
    two_days = 60 * 52
    yield Scenario("stale", array=live["array"], disks=live["disks"],
                   history=[task(601, "report", two_days, two_days, scheduled_at=ts(two_days))])


def build(config: Any, daemon_config: Json, scenario: Scenario) -> EmailMessage:
    """Build the scenario's email with the module's api_get swapped for canned responses."""
    responses = {
        "tasks": {"pending": [], "active": scenario.active, "history": list(reversed(scenario.history))},
        "array": scenario.array,
        "disks": scenario.disks,
        "config": daemon_config,
    }
    # setattr: the module was loaded by path, so type checkers only see a plain ModuleType.
    setattr(report, "api_get", lambda _url, endpoint: responses[endpoint.split("?")[0]])  # noqa: B010
    return report.build_message(config)


def screenshot(paths: list[str]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("screenshots need Playwright from the dev dependencies: uv sync (or pass --no-png)")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for path in paths:
            for label, width in VIEWPORTS.items():
                page = browser.new_page(viewport={"width": width, "height": 800})
                page.goto(f"file://{path}")
                page.screenshot(path=path.replace(".html", f"-{label}.png"), full_page=True)
                page.close()
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", default=report.DEFAULT_CONFIG)
    parser.add_argument("--send", action="store_true", help="email the examples instead of writing files")
    parser.add_argument("--no-png", action="store_true", help="skip screenshots")
    args = parser.parse_args()

    config = report.load_config(args.config)
    live = {endpoint: report.api_get(config.api_url, endpoint) for endpoint in ("array", "disks", "config")}
    # Pretend the nightly run is scheduled for when the fake runs were queued.
    queued_at = (NOW - timedelta(minutes=QUEUED_MIN_AGO)).strftime("%H:%M")
    daemon_config = live["config"] | {"maintenance_schedule": queued_at}

    os.makedirs(OUT, exist_ok=True)
    written = []
    for scenario in scenarios(live):
        msg = build(config, daemon_config, scenario)
        if args.send:
            icon, rest = msg["Subject"].split(" ", 1)
            msg.replace_header("Subject", f"{icon} EXAMPLE {rest}")
            if report.send(msg, config.sendmail) != 0:
                return 1
            print("sent:", msg["Subject"])
            continue
        path = os.path.join(OUT, f"{scenario.name}.html")
        html = next(part for part in msg.walk() if part.get_content_type() == "text/html")
        with open(path, "w") as f:
            f.write(html.get_content())
        written.append(path)
        print(f"{scenario.name:8} {msg['Subject']}")

    if written and not args.no_png:
        screenshot(written)
        print(f"screenshots in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
