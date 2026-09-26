#!/usr/bin/env python3
"""Email an HTML summary of the latest snapraidd run.

Meant to run from cron a few hours after the daemon's nightly run
(maintenance_schedule in /etc/snapraidd.conf); README.md has the crontab line.

Everything comes from the daemon's REST API: the latest run's steps, the
daemon's own report text, array stats and disk health. The email's level is
derived from those results. A missing, stale, or still-running run is
reported too, and any failure to build the report becomes a plain-text email
with the traceback.

Settings come from snapraid-report.conf next to this script (see
snapraid-report.conf.example), or the file given with --config. The email's
markup and styles live in templates/.
"""

import argparse
import configparser
import json
import os
import subprocess
import sys
import traceback
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Literal

import css_inline
import jinja2

HERE = os.path.dirname(os.path.realpath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "snapraid-report.conf")
TEMPLATES = os.path.join(HERE, "templates")

# Shapes of the daemon's JSON responses; see snapraidd.yaml for their fields.
Json = dict[str, Any]
Task = Json
Level = Literal["info", "warning", "error"]

# How far back to look for a run when snapraidd has no maintenance_schedule.
FALLBACK_WINDOW = timedelta(hours=24)
# A scheduled run is queued at its scheduled minute; allow for clock and queueing jitter.
SCHEDULE_SLACK = timedelta(minutes=5)
# History rows beyond the estimated background probes: the run's own steps and manual tasks.
HISTORY_HEADROOM = 100
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
RUN_COMMANDS = {"maintenance", "heal", "undelete"}
DISK_ROLES = ("data_disks", "parity_disks", "extra_disks")
BAD_HEALTH = {"corrupt", "prefail", "failing"}

TITLE = "SnapRAID Report"
# Subject-line icon per level; colors live in templates/report.css.
LEVEL_ICONS: dict[Level, str] = {"info": "✅", "warning": "⚠️", "error": "❌"}
STEP_LABELS = {
    "up": "Spin up", "down": "Spin down", "down_idle": "Spin down", "probe": "Probe",
    "smart": "SMART", "diff": "Diff", "sync": "Sync", "scrub": "Scrub",
    "check": "Check", "fix": "Fix", "report": "Report", "touch": "Touch",
}
# Steps only worth a timeline row when they go wrong.
ROUTINE_STEPS = {"up", "down", "down_idle", "probe", "smart", "report"}
DIFF_KINDS = ["added", "removed", "updated", "moved", "copied", "relocated", "restored"]

TEMP_WARM, TEMP_HOT = 45, 50  # °C
MAX_ISSUES = 15
MAX_CHANGE_GROUPS = 6
CHART_HEIGHT_PX = 48


# ---------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    mail_to: str
    mail_from: str
    sendmail: str = "/usr/sbin/sendmail"
    api_url: str = "http://127.0.0.1:7627/snapraid/v1"
    dashboard_url: str = ""


class ConfigError(Exception):
    pass


def load_config(path: str) -> Config:
    """Read an INI config file; see snapraid-report.conf.example for the keys."""
    parser = configparser.ConfigParser()
    if not parser.read(path):
        raise ConfigError(f"config file not found: {path} (start from snapraid-report.conf.example)")

    def get(section: str, key: str, required: bool = False) -> str:
        value = parser.get(section, key, fallback="").strip()
        if required and not value:
            raise ConfigError(f"{path}: [{section}] {key} is required")
        return value

    defaults = Config(mail_to="", mail_from="")
    return Config(
        mail_to=get("mail", "to", required=True),
        mail_from=get("mail", "from", required=True),
        sendmail=get("mail", "sendmail") or defaults.sendmail,
        api_url=(get("daemon", "api_url") or defaults.api_url).rstrip("/"),
        dashboard_url=get("daemon", "dashboard_url"),
    )


# ---------------------------------------------------------------- daemon API

@dataclass(frozen=True)
class State:
    run: list[Task]           # the latest run's tasks, oldest first; empty if none was found
    array: Json
    disks: Json
    expected_since: datetime  # a run should have been queued at or after this
    has_schedule: bool        # expected_since comes from maintenance_schedule, not FALLBACK_WINDOW


def api_get(api_url: str, endpoint: str) -> Json:
    with urllib.request.urlopen(f"{api_url}/{endpoint}", timeout=15) as resp:
        return json.load(resp)


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def last_scheduled(schedule: str, now: datetime) -> datetime | None:
    """The latest time at or before `now` matching a maintenance_schedule, or None if it's empty.

    The schedule is a comma-separated list of "HH:MM" (daily) or "Ddd HH:MM" (weekly) entries.
    """
    latest = None
    for entry in filter(None, (e.strip() for e in schedule.split(","))):
        *day, clock = entry.split()
        hour, minute = (int(x) for x in clock.split(":"))
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if day:
            moment -= timedelta(days=(moment.weekday() - DAYS.index(day[0].title())) % 7)
        if moment > now:
            moment -= timedelta(days=7 if day else 1)
        latest = max(latest or moment, moment)
    return latest


def history_limit(window: timedelta, probe_interval_minutes: int) -> int:
    """How many history rows to request to reach back over `window`.

    Background SMART probes make up most of the daemon's tasks: one per interval while
    disks spin. That's doubled to cover the spin-ups and spin-downs around them.
    """
    probes = window.total_seconds() / 60 / probe_interval_minutes if probe_interval_minutes else 0
    return int(probes * 2) + HISTORY_HEADROOM


def latest_run(history: list[Task]) -> list[Task]:
    """Return the newest run's tasks, oldest first, from tasks listed newest first.

    A run is the set of tasks queued together (same `scheduled_at`) by one high-level
    command (maintenance, heal, undelete). It isn't anchored on the trailing `report`
    task because the daemon only appends that when the run succeeds. Unrelated tasks
    interleaved with the run, like background probes and spin-downs, are skipped; an
    earlier run ends the search.
    """
    start = next((i for i, t in enumerate(history) if t.get("high_command") in RUN_COMMANDS), None)
    if start is None:
        return []
    key = (history[start]["high_command"], history[start].get("scheduled_at"))
    run = []
    for task in history[start:]:
        task_key = (task.get("high_command"), task.get("scheduled_at"))
        if task_key == key:
            run.append(task)
        elif task_key[0] in RUN_COMMANDS:
            break
    return list(reversed(run))


def fetch_state(api_url: str) -> State:
    """Read the latest run, array and disks. The run includes any of its steps still active or queued."""
    now = datetime.now()
    daemon_config = api_get(api_url, "config")
    scheduled_at = last_scheduled(daemon_config.get("maintenance_schedule") or "", now)
    since = scheduled_at or now - FALLBACK_WINDOW
    limit = history_limit(now - since + SCHEDULE_SLACK, daemon_config.get("probe_interval_minutes") or 0)

    tasks = api_get(api_url, f"tasks?limit_history={limit}")
    newest_first = [*reversed(tasks.get("pending") or []), *(tasks.get("active") or []),
                    *(tasks.get("history") or [])]
    return State(
        run=latest_run(newest_first),
        array=api_get(api_url, "array"),
        disks=api_get(api_url, "disks"),
        expected_since=since,
        has_schedule=scheduled_at is not None,
    )


# ---------------------------------------------------------------- assessing a run

def task_ok(task: Task) -> bool:
    return task.get("status") == "terminated" and task.get("exit_code") == 0


def task_failed(task: Task) -> bool:
    """The step finished unsuccessfully."""
    return task.get("status") in ("terminated", "signaled") and not task_ok(task)


def task_unfinished(task: Task) -> bool:
    """The step is queued or still running."""
    return task.get("status") not in ("terminated", "signaled", "canceled")


def task_problem(task: Task) -> str:
    """The daemon's explanation of what went wrong with a step, if any."""
    return task.get("health_reason") or task.get("exit_msg") or ""


def step_label(command: str) -> str:
    return STEP_LABELS.get(command, command)


def all_disks(disks: Json) -> Iterator[tuple[str, Json]]:
    """Yield (role, disk) for every data, parity and extra disk."""
    for role in DISK_ROLES:
        for disk in disks.get(role) or []:
            yield role, disk


def disk_errors(disk: Json) -> int:
    return (disk.get("error_io") or 0) + (disk.get("error_data") or 0)


def disk_healthy(disk: Json) -> bool:
    return disk.get("health") == "passed" and not disk_errors(disk)


def run_is_stale(state: State) -> bool:
    """True when no run was queued since the last scheduled time."""
    if not state.run:
        return True
    queued = parse_time(state.run[0].get("scheduled_at"))
    return not queued or queued < state.expected_since - SCHEDULE_SLACK


def run_level(state: State) -> Level:
    """Error for failed steps or bad blocks; warning for anything unfinished, skipped or unhealthy."""
    run = state.run
    if any(task_failed(t) or t.get("health") in BAD_HEALTH for t in run) or state.array.get("blocks_bad"):
        return "error"
    unfinished_or_skipped = any(task_unfinished(t) or t.get("status") == "canceled" for t in run)
    if unfinished_or_skipped or not all(disk_healthy(d) for _, d in all_disks(state.disks)):
        return "warning"
    return "info"


def run_status(run: list[Task]) -> str:
    """One line for the subject and header: what went wrong, what's still going, or the daemon's summary."""
    problem = next(filter(None, map(task_problem, run)), "").rstrip(".")
    unfinished = next((t for t in run if task_unfinished(t)), None)
    running = ""
    if unfinished:
        pct = f", {unfinished['progress']}%" if unfinished.get("progress") is not None else ""
        running = f"still running: {step_label(unfinished['command']).lower()}{pct}"

    if problem and running:
        return f"{problem} ({running})"
    return status_line(run_report(run)) or problem or running.capitalize() or "Run finished"


def missed_run_status(state: State) -> str:
    if state.has_schedule:
        return f"Missed the scheduled run at {fmt_when(state.expected_since)}"
    return f"No run in the last {FALLBACK_WINDOW.total_seconds() / 3600:.0f} hours"


def assess(state: State, stale: bool) -> tuple[Level, str]:
    """Return (level, status line) summarising the run and array health."""
    if stale:
        return "error", missed_run_status(state)
    status = run_status(state.run)
    return run_level(state), " ".join(status.split()).replace("->", "→").rstrip(".")  # one line: it's the subject


def run_report(run: list[Task]) -> str:
    """The daemon's plain-text report for the run, if it produced one."""
    return next((t["report_output"] for t in run if t.get("report_output")), "")


def status_line(report: str) -> str:
    """The STATUS line of the daemon's report text."""
    for line in report.splitlines():
        if line.startswith("STATUS:"):
            return line.split(":", 1)[1].strip()
    return ""


# ---------------------------------------------------------------- formatting
# Also registered as template filters, so Python and Jinja format values the same way.

def fmt_when(moment: datetime) -> str:
    return moment.strftime("%a %b %-d, %-I:%M %p")


def fmt_number(n: int) -> str:
    return f"{n:,}"


def fmt_percent(fraction: float, digits: int = 0) -> str:
    return f"{fraction * 100:.{digits}f}%"


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


FILTERS = {"when": fmt_when, "number": fmt_number, "percent": fmt_percent,
           "bytes": fmt_bytes, "duration": fmt_duration}


# ---------------------------------------------------------------- template data
# Each section of templates/report.html.j2 gets its data from one builder below.

def fraction(part: float, whole: float) -> float:
    """part / whole clamped to 0–1; 0 when whole is empty."""
    return max(0.0, min(1.0, part / whole)) if whole else 0.0


def run_started(run: list[Task]) -> datetime | None:
    return parse_time(run[0].get("started_at")) or parse_time(run[0].get("scheduled_at"))


def run_duration(run: list[Task]) -> float | None:
    """Seconds from the run's start to its last finished step, or to now while it's still going."""
    started = parse_time(run[0].get("started_at"))
    if any(task_unfinished(t) for t in run):
        finished: datetime | None = datetime.now()
    else:
        finished = max(filter(None, (parse_time(t.get("finished_at")) for t in run)), default=None)
    return (finished - started).total_seconds() if started and finished else None


# ---- header

def run_meta(run: list[Task]) -> list[str]:
    """The header's small line for a run: its kind (unless routine), start and duration."""
    started = run_started(run)
    took = "running for" if any(task_unfinished(t) for t in run) else "took"
    meta = [fmt_when(started) if started else "start unknown", f"{took} {fmt_duration(run_duration(run))}"]
    if run[0]["high_command"] != "maintenance":
        meta.insert(0, run[0]["high_command"].capitalize())
    return meta


def stale_meta(run: list[Task]) -> list[str]:
    """The header's small line when no recent run exists: when we looked, and the last run we saw."""
    meta = [f"checked {fmt_when(datetime.now())}"]
    last = run_started(run) if run else None
    if last:
        meta.append(f"last run {fmt_when(last)}")
    return meta


# ---- stat boxes

@dataclass(frozen=True)
class Tile:
    value: str
    label: str


def step_size_text(task: Task | None) -> str:
    """Bytes processed by a step, or why there's no number."""
    if not task:
        return "—"
    if task.get("status") == "canceled":
        return "skipped"
    return fmt_bytes(task.get("size_done_bytes") or 0)


def risk_tile(array: Json) -> Tile:
    """The daemon's estimate of at least one disk failing within a year."""
    return Tile(fmt_percent(array.get("failure_probability") or 0), "1-yr failure risk")


def run_tiles(run: list[Task], array: Json) -> list[Tile]:
    by_command = {t["command"]: t for t in run}
    changes = sum(array.get(f"diff_{kind}") or 0 for kind in DIFF_KINDS)
    return [
        Tile(fmt_number(changes), "Files changed"),
        Tile(step_size_text(by_command.get("sync")), "Synced"),
        Tile(step_size_text(by_command.get("scrub")), "Scrubbed"),
        risk_tile(array),
    ]


# ---- issues

@dataclass(frozen=True)
class Issue:
    step: str | None
    text: str


@dataclass(frozen=True)
class Issues:
    shown: list[Issue]  # at most MAX_ISSUES
    more: int           # how many were left out


def issues_view(run: list[Task], array: Json) -> Issues:
    issues = []
    for task in run:
        step = step_label(task["command"])
        if task_problem(task):
            issues.append(Issue(step, task_problem(task)))
        for msg in task.get("messages") or []:
            if msg.get("level") in ("fatal", "error"):
                issues.append(Issue(step, msg["text"]))
    if array.get("blocks_bad"):
        issues.append(Issue(None, f"{fmt_number(array['blocks_bad'])} bad blocks in the array — run a heal."))
    return Issues(issues[:MAX_ISSUES], max(0, len(issues) - MAX_ISSUES))


# ---- timeline

StepState = Literal["ok", "skipped", "queued", "failed", "running"]


@dataclass(frozen=True)
class Step:
    label: str
    state: StepState    # names the dot color in report.css
    state_text: str     # shown after the label; empty for steps that finished fine
    size: int | None    # bytes processed
    seconds: float | None


def step_state(task: Task) -> tuple[StepState, str]:
    if task_ok(task):
        return "ok", ""
    if task.get("status") == "canceled":
        return "skipped", "skipped"
    if task.get("status") == "queued":
        return "queued", "queued"
    if task_failed(task):
        return "failed", "failed"
    pct = f" {task['progress']}%" if task.get("progress") is not None else ""
    return "running", f"running{pct}"


def task_seconds(task: Task) -> float | None:
    start, end = parse_time(task.get("started_at")), parse_time(task.get("finished_at"))
    if start and end:
        return (end - start).total_seconds()
    return task.get("elapsed_seconds")  # only set while the step is processing


def timeline_view(run: list[Task]) -> list[Step]:
    steps = []
    for task in run:
        if task_ok(task) and task["command"] in ROUTINE_STEPS:
            continue
        state, state_text = step_state(task)
        steps.append(Step(
            label=step_label(task["command"]),
            state=state,
            state_text=state_text,
            size=task.get("size_done_bytes"),
            seconds=None if state == "skipped" else task_seconds(task),
        ))
    return steps


# ---- changes

@dataclass(frozen=True)
class ChangeCount:
    kind: str  # one of DIFF_KINDS
    count: int


@dataclass(frozen=True)
class ChangeGroup:
    change: str
    folder: str  # "disk:path/"
    count: int


@dataclass(frozen=True)
class Changes:
    counts: list[ChangeCount]
    groups: list[ChangeGroup]  # largest first, at most MAX_CHANGE_GROUPS
    more_groups: int           # how many were left out


def group_diffs(diffs: list[Json]) -> list[ChangeGroup]:
    """Collapse individual file changes into one group per (change, folder), largest first."""
    counts: dict[tuple[str, str], int] = {}
    for diff in diffs:
        folder = os.path.dirname(diff["path"]) or "."
        key = (diff["change"], f"{diff['disk']}:{folder}/")
        counts[key] = counts.get(key, 0) + 1
    groups = [ChangeGroup(change, folder, n) for (change, folder), n in counts.items()]
    return sorted(groups, key=lambda g: -g.count)


def changes_view(array: Json) -> Changes | None:
    counts = [ChangeCount(kind, array.get(f"diff_{kind}") or 0) for kind in DIFF_KINDS]
    counts = [c for c in counts if c.count]
    if not counts:
        return None
    groups = group_diffs(array.get("diffs") or [])
    return Changes(counts, groups[:MAX_CHANGE_GROUPS], max(0, len(groups) - MAX_CHANGE_GROUPS))


# ---- array

@dataclass(frozen=True)
class ChartDay:
    days_ago: int
    scrubbed_pct: float  # % of the array verified that day
    scrub_px: int
    new_px: int


@dataclass(frozen=True)
class Chart:
    days: list[ChartDay]  # oldest first
    peak: float           # the busiest day's scrubbed + newly synced share of the array, 0–1
    height_px: int


@dataclass(frozen=True)
class ArrayView:
    used: int
    total: int
    used_fraction: float
    scrub_fraction: float
    files: int
    data_disks: int
    parity_disks: int
    chart: Chart | None


def chart_view(history: Json) -> Chart | None:
    """Per-day scrub bars, scaled so the busiest day fills the chart."""
    points = {p["ago"]: p for p in history.get("points") or []}
    if not points:
        return None
    peak_pct = max((p.get("scrubbed") or 0) + (p.get("new") or 0) for p in points.values())
    scale = CHART_HEIGHT_PX / (peak_pct or 1)
    days = []
    for ago in range(max(points), -1, -1):
        point = points.get(ago, {})
        scrubbed, new = point.get("scrubbed") or 0, point.get("new") or 0
        days.append(ChartDay(
            days_ago=ago,
            scrubbed_pct=scrubbed,
            scrub_px=round(scrubbed * scale),
            new_px=round(new * scale),
        ))
    return Chart(days, peak_pct / 100, CHART_HEIGHT_PX)


def array_view(array: Json) -> ArrayView:
    total, free = array.get("total_space_bytes") or 0, array.get("free_space_bytes") or 0
    blocks = array.get("blocks_count") or 0
    return ArrayView(
        used=total - free,
        total=total,
        used_fraction=fraction(total - free, total),
        scrub_fraction=fraction(blocks - (array.get("blocks_unscrubbed") or 0), blocks),
        files=array.get("files_count") or 0,
        data_disks=array.get("data_disks_count") or 0,
        parity_disks=array.get("parity_disks_count") or 0,
        chart=chart_view(array.get("scrub_history") or {}),
    )


# ---- disks

@dataclass(frozen=True)
class DiskRow:
    name: str
    role: str                    # data, parity or extra
    node: str                    # e.g. /dev/sdb
    drive: str                   # model family, or the model when there's no family
    model: str | None            # shown under the family
    temp: str
    temp_class: str              # temp-warm / temp-hot in report.css
    used: int | None
    used_fraction: float | None
    size: int | None
    risk: float | None           # chance of this disk failing within a year
    health: str
    errors: int
    healthy: bool


def last_temp(device: Json) -> int | None:
    temps = [t for t in device.get("temp_history_24h") or [] if t]
    return temps[-1] if temps else None


def disk_temp(device: Json) -> tuple[str, str]:
    """Return (text, css class) for a disk's latest temperature."""
    temp = last_temp(device)
    if not temp:
        return ("asleep" if device.get("power") == "standby" else "—"), ""
    if temp >= TEMP_HOT:
        return f"{temp}°C", "temp-hot"
    if temp >= TEMP_WARM:
        return f"{temp}°C", "temp-warm"
    return f"{temp}°C", ""


def disk_row(role: str, disk: Json) -> DiskRow:
    device = (disk.get("devices") or [{}])[0]
    total, free = disk.get("total_space_bytes"), disk.get("free_space_bytes")
    used = total - free if total and free is not None else None
    temp, temp_class = disk_temp(device)
    return DiskRow(
        name=disk["name"],
        role=role.removesuffix("_disks"),
        node=device.get("node", ""),
        drive=device.get("family") or device.get("model") or "—",
        model=device.get("model") if device.get("family") else None,
        temp=temp,
        temp_class=temp_class,
        used=used,
        used_fraction=fraction(used, total) if used is not None and total else None,
        size=total or device.get("size_bytes"),  # raw device size when the daemon has no filesystem totals
        risk=device.get("failure_probability"),
        health=disk.get("health") or "unknown",
        errors=disk_errors(disk),
        healthy=disk_healthy(disk),
    )


# ---- whole report

@dataclass(frozen=True)
class Report:
    """Everything templates/report.html.j2 renders."""
    title: str
    level: Level
    status: str
    meta: list[str]
    tiles: list[Tile]
    issues: Issues | None  # None when there's no run to report on
    timeline: list[Step]
    changes: Changes | None
    array: ArrayView
    disks: list[DiskRow]
    dashboard_url: str

    @property
    def tile_width_pct(self) -> int:
        return 100 // len(self.tiles)


def build_report(state: State, stale: bool, level: Level, status: str, dashboard_url: str) -> Report:
    run, array = state.run, state.array
    return Report(
        title=TITLE,
        level=level,
        status=status,
        meta=stale_meta(run) if stale else run_meta(run),
        tiles=[risk_tile(array)] if stale else run_tiles(run, array),
        issues=None if stale else issues_view(run, array),
        timeline=[] if stale else timeline_view(run),
        changes=None if stale else changes_view(array),
        array=array_view(array),
        disks=[disk_row(role, disk) for role, disk in all_disks(state.disks)],
        dashboard_url=dashboard_url,
    )


# ---------------------------------------------------------------- email

def render_html(report: Report) -> str:
    """Render the report template and inline its stylesheet, as email clients expect."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES),
        autoescape=jinja2.select_autoescape(["html.j2"]),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(FILTERS)
    context = {f.name: getattr(report, f.name) for f in fields(report)} | {"tile_width_pct": report.tile_width_pct}
    html = env.get_template("report.html.j2").render(context)
    return css_inline.CSSInliner(keep_at_rules=True).inline(html)


def build_message(config: Config) -> EmailMessage:
    """Build the report email. Any failure becomes a plain-text email with the traceback."""
    msg = EmailMessage()
    msg["From"] = f"SnapRAID on {os.uname().nodename} <{config.mail_from}>"
    msg["To"] = config.mail_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=config.mail_from.split("@")[-1])

    try:
        state = fetch_state(config.api_url)
        stale = run_is_stale(state)
        level, status = assess(state, stale)
        html = render_html(build_report(state, stale, level, status, config.dashboard_url))
    except Exception:  # any failure, from an unreachable daemon to a template bug, still sends an email
        traceback.print_exc()
        msg["Subject"] = f"{LEVEL_ICONS['error']} {TITLE} · Report failed"
        msg.set_content(f"Building the report from {config.api_url} failed:\n\n{traceback.format_exc()}")
        return msg

    msg["Subject"] = f"{LEVEL_ICONS[level]} {TITLE} · {status}"
    msg.set_content(f"{TITLE}: {status}\n\n{run_report(state.run)}".rstrip() + "\n")
    msg.add_alternative(html, subtype="html")
    return msg


def send(msg: EmailMessage, sendmail: str) -> int:
    result = subprocess.run([sendmail, "-t", "-oi"], input=msg.as_bytes(), capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        print(f"snapraid-report: sendmail exited {result.returncode}: {stderr}", file=sys.stderr)
    return result.returncode


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to the config file")
    parser.add_argument("--to", help="send to this address instead of the configured one")
    parser.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except (ConfigError, configparser.Error) as exc:
        print(f"snapraid-report: {exc}", file=sys.stderr)
        return 1
    if args.to:
        config = replace(config, mail_to=args.to)

    msg = build_message(config)
    if args.dry_run:
        sys.stdout.write(msg.as_string())
        return 0
    return send(msg, config.sendmail)


if __name__ == "__main__":
    sys.exit(main())
