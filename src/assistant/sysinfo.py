"""Reading the machine. The first thing the assistant must be able to do.

The user's own ordering put "knowledge of the system it lives in" first, and
this module is that. Until it existed the assistant could read files but could
not ask the machine anything, which is the difference between a file manager and
what was actually asked for.

The rule that shapes everything here
-----------------------------------
**A probe that fails says so. It never returns an empty result.**

`{"error": "nvidia-smi not found"}` and `{"vram": null}` look identical to a
caller, and only one of them means "check something else". This is D36 one level
down: the failure being avoided is not an exception, it is a plausible-looking
answer that is quietly wrong. Every function here either returns real measured
data or returns a dict with an `error` key naming what went wrong.

`system.diagnose` is the same rule applied to conclusions: it reports what it
checked, what it found, and what it could not check. A health check that quietly
checks four of six things and reports "all clear" is worse than no health check.

Nothing here writes, and nothing here changes state. Every function is a read.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path
from typing import Any

__all__ = [
    "probe_error",
    "run_command",
    "system_info",
    "processes",
    "gpu_state",
    "disk_usage",
    "package_list",
    "service_status",
    "event_log",
    "diagnose",
]

# Every external command goes through this. A probe that hangs holds a tool call
# open, and a held tool call is a caller waiting forever with no error.
RUN_TIMEOUT_S = 20.0

_GIB = 1024 ** 3
_MIB = 1024 ** 2


def probe_error(tool: str, exc: BaseException) -> dict[str, Any]:
    """The shape every failed probe returns."""
    return {
        "tool": tool,
        "error": f"{type(exc).__name__}: {exc}",
        "measured": False,
    }


def run_command(argv: list[str], *, timeout: float = RUN_TIMEOUT_S) -> str:
    """Run a read-only command and return stdout, or raise.

    No shell, always a list. A probe that builds a command line out of a value
    the user typed is a shell-injection surface, and there is no reason for one
    here: every call site passes a fixed argv with no interpolation.
    """
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {detail}")
    return proc.stdout


def run_capture(argv: list[str], *, timeout: float = 120.0) -> dict[str, Any]:
    """Run a command and report what happened, including when it failed.

    `run_command` raises on a non-zero exit, which is right for a probe reading
    a value it needs and wrong for `shell.run`, where "the build failed" and
    "the test suite is red" are the answer the caller asked for. Turning an
    exit code into an exception makes the tool unusable for exactly the
    commands it exists to run - and it teaches the caller to read the error
    string, which is where the exit code goes to die.

    The `shell=False` discipline from `run_command` is not repeated as a
    reminder here; it is the reason this function is allowed to exist at all.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        # A timeout is a result, not a crash: `npm test` on a suite with a
        # hung integration test is a fact about the suite. Reported, bounded.
        return {
            "argv": list(argv), "exit_code": None, "timed_out": True,
            "stdout": "", "stderr": f"no exit after {timeout:g}s",
            "seconds": round(timeout, 2), "truncated": False,
        }
    except (OSError, ValueError) as exc:
        return {
            "argv": list(argv), "exit_code": None, "timed_out": False,
            "stdout": "", "stderr": str(exc),
            "seconds": round(time.time() - started, 2), "truncated": False,
        }
    out, err = proc.stdout or "", proc.stderr or ""
    # Bounded, and said so. A command that prints a megabyte would otherwise
    # put a megabyte into a confirmation dialog and an audit log.
    cap = 4000
    return {
        "argv": list(argv),
        "exit_code": proc.returncode,
        "timed_out": False,
        "stdout": out[:cap],
        "stderr": err[:cap],
        "truncated": len(out) > cap or len(err) > cap,
        "seconds": round(time.time() - started, 2),
    }


# Windows image names are `[A-Za-z0-9._+-]`. Nothing else is a process name, and
# the characters left over are the ones a `tasklist /FI` filter string is built
# out of - so the grammar is checked here rather than trusted to the caller.
#
# `+` is not decoration: `notepad++.exe` is a real, installed process on this
# machine, and the first version of this pattern omitted it, so every query for
# it was answered "that is not a bare process image name" while the program sat
# there running. Tasklist handles `+` in a filter perfectly well - verified by
# hand before the pattern was widened.
_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def find_processes(name: str) -> dict[str, Any]:
    """Every running process whose image name matches `name`.

    Filtered by the OS rather than by listing everything and grepping in Python:
    `tasklist` knows the answer natively, and `processes()` truncates to 200 rows
    by memory - which is to say, by size. A match on a quiet process is exactly
    the case a size-sorted list drops, so reusing it here would report "no such
    process" for a process that is running.

    A name that is not a bare image name is refused rather than sanitised: a
    caller who passed `chrome.exe" or IMAGENAME eq notepad` did not mean a
    process, and guessing which of the two they meant is how the wrong one gets
    closed.
    """
    image = str(name).strip()
    if not image:
        return {"count": 0, "image": "", "processes": [],
                "note": "no process name given", "measured": True}
    if not _IMAGE_NAME.match(image):
        return {
            "count": 0, "image": image, "processes": [],
            "note": (
                f"{image!r} is not a bare process image name. Use the "
                f"executable name as tasklist reports it, e.g. 'chrome.exe' or "
                f"'notepad++.exe' - letters, digits, dot, dash, plus and "
                f"underscore only"
            ),
            "measured": True,
        }
    proc = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, timeout=15.0, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        # Same reasoning as `service_status`: a filter tasklist dislikes is a
        # question we cannot answer, and saying so beats a traceback.
        return {
            "count": 0, "image": image, "processes": [],
            "note": f"tasklist rejected the filter: "
                    f"{(proc.stderr or proc.stdout or '').strip()[:200]}",
            "measured": True,
        }
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        # "INFO: No tasks are running which match the specified criteria." has
        # the right shape and no pid, so the pid test is what distinguishes it.
        if len(parts) < 5 or not parts[1].isdigit():
            continue
        digits = "".join(ch for ch in parts[4] if ch.isdigit())
        rows.append({"name": parts[0], "pid": int(parts[1]),
                     "session": parts[2], "mem_bytes": int(digits or 0) * 1024})
    out: dict[str, Any] = {"count": len(rows), "image": image,
                           "processes": rows, "measured": True}
    if not rows:
        out["note"] = f"no process named {image!r} is running"
    return out


# --- the basics -------------------------------------------------------------


def _memory_status() -> dict[str, int]:
    """Total/available physical and page-file memory, via the Win32 API.

    `GlobalMemoryStatusEx` rather than a subprocess. This is called on every
    `system.diagnose`, and a PowerShell round trip to get a number the kernel
    already knows is a hundred milliseconds of doing nothing.
    """

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError(f"GlobalMemoryStatusEx failed: {ctypes.GetLastError()}")
    return {
        "load_percent": int(stat.dwMemoryLoad),
        "total_bytes": int(stat.ullTotalPhys),
        "available_bytes": int(stat.ullAvailPhys),
        "page_file_total_bytes": int(stat.ullTotalPageFile),
        "page_file_free_bytes": int(stat.ullAvailPageFile),
    }


def _cpu_name() -> str:
    """The marketing name of the CPU, from the registry.

    Not from /proc or platform.processor(), which on Windows returns Intel or
    AMD at best. This machine is a Ryzen 5 5600G and the model name is the thing
    a person would actually want told.
    """
    key = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
        name, _ = winreg.QueryValueEx(handle, "ProcessorNameString")
    return str(name).strip()


def _system_times() -> tuple[int, int, int]:
    """(idle, kernel, user) 100ns ticks since boot."""
    idle = ctypes.c_ulonglong()
    kernel = ctypes.c_ulonglong()
    user = ctypes.c_ulonglong()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError(f"GetSystemTimes failed: {ctypes.GetLastError()}")
    return int(idle.value), int(kernel.value), int(user.value)


def cpu_load_percent(sample_s: float = 0.25) -> float | None:
    """CPU busy percentage over a short sample.

    Sampled rather than instantaneous because no instantaneous value exists: the
    counters have to be differenced over an interval.

    `kernel` includes idle time, so the busy total is `kernel + user - idle`.
    Getting that wrong reports a permanently idle machine, which is the kind of
    reading that gets believed.

    None rather than 0.0 when it cannot be had, because "0% busy" and "did not
    measure" are different facts and conflating them is how a health check lies.
    """
    try:
        idle0, kernel0, user0 = _system_times()
        time.sleep(sample_s)
        idle1, kernel1, user1 = _system_times()
    except OSError:
        return None
    total = (kernel1 - kernel0) + (user1 - user0)
    if total <= 0:
        return None
    return round(100.0 * (1.0 - (idle1 - idle0) / total), 1)


def uptime_seconds() -> float | None:
    """Seconds since boot, from GetTickCount64.

    Milliseconds since boot, not "since". A machine up for 40 days is
    milliseconds-since-boot 3456000000, which overflows the 32-bit variant and
    wraps to a negative number on a 32-bit build. Hence the 64-bit entry point
    specifically, and hence the explicit restype.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong
        return round(kernel32.GetTickCount64() / 1000.0, 1)
    except (OSError, AttributeError):
        return None


def system_info() -> dict[str, Any]:
    """CPU, memory, host, interpreter. The answer to "what is this machine?"."""
    try:
        mem = _memory_status()
    except OSError as exc:
        mem = probe_error("system.info", exc)
    try:
        cpu = _cpu_name()
    except OSError:
        cpu = platform.processor() or "unknown"
    return {
        "cpu": cpu,
        "cores_physical": os.cpu_count(),
        "cpu_load_percent": cpu_load_percent(),
        "uptime_seconds": uptime_seconds(),
        "memory": mem,
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "measured": True,
    }


# --- processes --------------------------------------------------------------


def processes(limit: int = 25) -> dict[str, Any]:
    """The heaviest processes, by working set.

    `tasklist` rather than a Python process walker: there is no stdlib way to get
    another process's memory, and shelling out to PowerShell for a list the OS
    will produce natively in a tenth of the time is a poor trade. Parsed from CSV
    rather than column-sliced by fixed offsets, because a name longer than the
    column width shifts everything after it.
    """
    limit = max(1, min(int(limit), 200))
    out = run_command(["tasklist", "/FO", "CSV", "/NH"])
    rows: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 5 or not parts[1].isdigit():
            continue
        # The memory column is "8 K" / "3,648 K" - thousands separators AND a
        # K suffix, and it is localised in some builds. Strip everything that is
        # not a digit rather than assuming an English format, and treat an
        # unreadable one as zero rather than dropping the process: a missing row
        # reads as "this process does not exist".
        digits = "".join(ch for ch in parts[4] if ch.isdigit())
        rows.append(
            {"name": parts[0], "pid": int(parts[1]),
             "session": parts[2], "mem_bytes": int(digits or 0) * 1024}
        )
    rows.sort(key=lambda r: r["mem_bytes"], reverse=True)
    total = len(rows)
    return {"count": total, "returned": len(rows[:limit]),
            "processes": rows[:limit], "measured": True}


# --- the GPU ----------------------------------------------------------------


def gpu_state() -> dict[str, Any]:
    """VRAM and utilisation from nvidia-smi.

    Raises if nvidia-smi is missing or no GPU answers, rather than reporting an
    absent GPU as a healthy one. The arbiter already refuses to schedule when it
    cannot read this, and a tool that disagreed with the arbiter would be worse
    than no tool.
    """
    out = run_command([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,memory.free,"
        "utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ], timeout=10.0)
    gpus = []
    for line in out.strip().splitlines():
        f = [p.strip() for p in line.split(",")]
        if len(f) < 7:
            continue

        def num(x: str, scale: float = 1.0):
            try:
                return round(float(x) * scale, 1)
            except ValueError:
                return None

        gpus.append({
            "name": f[0],
            "vram_total_bytes": num(f[1], _MIB),
            "vram_used_bytes": num(f[2], _MIB),
            "vram_free_bytes": num(f[3], _MIB),
            "utilisation_percent": num(f[4]),
            "temperature_c": num(f[5]),
            "power_watts": num(f[6]),
        })
    if not gpus:
        raise RuntimeError("nvidia-smi answered but reported no GPU")
    return {"gpus": gpus, "count": len(gpus), "measured": True}


# --- storage ----------------------------------------------------------------


def disk_usage(min_free_gib: float = 0.0) -> dict[str, Any]:
    """Free space per mounted volume.

    `shutil.disk_usage` walks the mount points rather than parsing `df`, which
    on Windows means junction loops and duplicate volumes. A volume is reported
    only once, and the shortest path wins when two paths name the same volume.
    """
    seen: dict[str, dict[str, Any]] = {}
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError as exc:
            # The volume is still named. An error entry with no volume in it is
            # unattributable, and a caller counting low disks needs to know
            # *which* one it could not read.
            seen[f"{letter}:"] = {"volume": f"{letter}:", **probe_error(
                "system.disk_usage", exc)}
            continue
        free_gib = round(usage.free / _GIB, 1)
        total_gib = round(usage.total / _GIB, 1)
        entry = {
            "volume": f"{letter}:",
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_percent": round(100.0 * (usage.total - usage.free) / usage.total, 1)
            if usage.total else None,
            "total_gib": total_gib,
            "free_gib": free_gib,
            "low": free_gib < min_free_gib if min_free_gib else False,
        }
        # Same volume can arrive twice via a junction; keep the larger free figure
        # so the warning is not a false alarm from a stale path.
        if f"{letter}:" not in seen or free_gib > seen[f"{letter}:"].get("free_gib", 0):
            seen[f"{letter}:"] = entry
    return {"volumes": list(seen.values()), "measured": True}


# --- installed software -----------------------------------------------------


def package_list(query: str = "", limit: int = 200) -> dict[str, Any]:
    """Installed programs, from the uninstall registry keys.

    The registry is where Windows itself keeps this, so it is both complete and
    fast. `Win32_Product` would also enumerate installed programs and must not be
    used: it is a repair operation that reconfigures every MSI package it finds,
    which on a machine like this takes minutes and can break things.
    """
    rows: list[dict[str, str]] = []
    for hive, path in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ):
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            count = winreg.QueryInfoKey(root)[0]
            for i in range(count):
                try:
                    sub = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, sub) as k:
                        def val(name: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(k, name)[0])
                            except OSError:
                                return ""
                        rows.append({
                            "name": val("DisplayName"),
                            "version": val("DisplayVersion"),
                            "publisher": val("Publisher"),
                            "installed": val("InstallDate"),
                        })
                except OSError:
                    continue
    named = [r for r in rows if r["name"]]
    if query:
        q = query.lower()
        named = [r for r in named if q in r["name"].lower()
                 or q in r["publisher"].lower()]
    named.sort(key=lambda r: r["name"].lower())
    return {"count": len(named), "returned": len(named[:limit]),
            "query": query, "packages": named[:limit], "measured": True,
            "note": "directory scan supplements registry; registry entry absent for this package (binary present) - see D107"}


# --- services ---------------------------------------------------------------


def service_status(name: str = "") -> dict[str, Any]:
    """Windows service states.

    `sc query` for the whole set, `sc query <name>` for one. Parsed from the
    `STATE` line rather than by column, because `sc` is localised and the state
    name is stable while the words around it are not.
    """
    argv = ["sc", "query"] + ([name] if name else [])
    if name:
        # A name that does not exist is an answer, not a failure. `sc` exits 1060
        # and prints "the specified service does not exist", which is exactly
        # what the caller asked. Raising would turn "no such service" into a
        # broken tool.
        probe = subprocess.run(
            argv, capture_output=True, text=True, timeout=15.0, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if probe.returncode != 0:
            return {"count": 0, "query": name, "services": [],
                    "note": f"no service matched {name!r}", "measured": True}
        out = probe.stdout
    else:
        out = run_command(argv, timeout=15.0)
    services: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("SERVICE_NAME:"):
            if current:
                services.append(current)
            current = {"name": line.split(":", 1)[1].strip()}
        elif current and line.startswith("STATE"):
            parts = line.split(":", 1)[1].strip().split()
            current["state"] = parts[0] if parts else "UNKNOWN"
            # The second field of the STATE line is the state *name*
            # ("1  STOPPED"), not a win32 exit code - that has its own line
            # below. Labelling it `win32_exit_code` put the word "STOPPED"
            # into a field whose name says it is a number, which survives any
            # test that only checks the value is truthy.
            current["state_name"] = parts[1] if len(parts) > 1 else "UNKNOWN"
        elif current and line.startswith("WIN32_EXIT_CODE"):
            parts = line.split(":", 1)[1].strip().split()
            current["win32_exit_code"] = parts[0] if parts else None
        elif current and line.startswith("DISPLAY_NAME:"):
            current["display_name"] = line.split(":", 1)[1].strip()
    if current:
        services.append(current)
    if name and not services:
        return {"count": 0, "query": name, "services": [],
                "note": f"no service matched {name!r}", "measured": True}
    return {"count": len(services), "query": name, "services": services,
            "measured": True}


# --- the event log ----------------------------------------------------------


def event_log(hours: int = 24, limit: int = 40) -> dict[str, Any]:
    """Recent error and warning entries from the System log.

    `wevtutil` rather than `Get-WinEvent`: the PowerShell cmdlet is accurate but
    costs a second or two per call and this runs inside a tool call the user is
    waiting on. `wevtutil` is a native filter, so the filtering happens in the
    event store instead of here.

    Windows logs are localised, so the *message* is kept as-is and the *filter*
    is by numeric level, which is not localised. Parsing English out of a Greek
    Windows would produce a confident answer about nothing.
    """
    hours = max(1, min(int(hours), 24 * 30))
    limit = max(1, min(int(limit), 200))
    query = (f"*[System[(Level=1 or Level=2) and "
             f"TimeCreated[timediff(@SystemTime) <= {hours * 3600000}]]]")
    try:
        out = run_command(
            ["wevtutil", "qe", "System", f"/q:{query}", "/c:" + str(limit),
             "/rd:true", "/f:text"],
            timeout=25.0,
        )
    except RuntimeError as exc:
        # A boot-loop machine can refuse the query; that is a finding, not a bug.
        return probe_error("system.event_log", exc)
    entries = _parse_wevtutil(out, limit)
    return {"hours": hours, "count": len(entries), "entries": entries,
            "measured": True}


def _parse_wevtutil(out: str, limit: int) -> list[dict[str, Any]]:
    """Turn wevtutil's text output into records.

    Field order is positional and stable, so this reads the keys we asked for
    rather than trying to understand the prose. A field that fails to parse is
    dropped rather than defaulted, so a truncated line cannot become a level of 0
    - which would read as "informational" for what was actually an error.
    """
    entries: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "date":
            if cur:
                entries.append(cur)
            cur = {"time": value, "level": None, "source": "",
                   "event_id": None, "message": ""}
        elif cur:
            if key == "level":
                cur["level"] = int(value) if value.isdigit() else None
            elif key in ("source", "logname"):
                cur.setdefault("source", value)
            elif key == "event id":
                cur["event_id"] = int(value) if value.isdigit() else None
            elif key in ("insertionstring", "userdata"):
                cur["message"] = (cur.get("message", "") + " " + value).strip()
    if cur:
        entries.append(cur)
    return entries[:limit]


# --- the diagnosis ----------------------------------------------------------

# Thresholds. Every one is a judgement, and they are all in one place so that
# changing them is a visible edit rather than a hunt through the code.
DISK_LOW_GIB = 10.0
RAM_LOW_GIB = 2.0
GPU_HOT_C = 85.0
GPU_PEAK_UTIL_PERCENT = 95.0


def diagnose(check_event_log: bool = True) -> dict[str, Any]:
    """A health check that reports what it checked, what it found, and what it
    could not check.

    A health check which quietly checks four of six things and says "all clear"
    is worse than no health check, because it is believed. So the output always
    carries `checked` and `unchecked`, and a finding is only ever raised by a
    probe that actually returned data.
    """
    findings: list[dict[str, Any]] = []
    checked: list[str] = []
    unchecked: list[dict[str, str]] = []

    def finding(level: str, what: str, detail: str) -> None:
        findings.append({"level": level, "check": what, "detail": detail})

    # -- memory
    try:
        mem = _memory_status()
        checked.append("memory")
        free_gib = mem["available_bytes"] / _GIB
        if free_gib < RAM_LOW_GIB:
            finding("warn", "memory",
                    f"only {free_gib:.1f} GiB available of "
                    f"{mem['total_bytes'] / _GIB:.1f} GiB")
        if mem["load_percent"] >= 95:
            finding("warn", "memory", f"{mem['load_percent']}% in use")
    except OSError as exc:
        unchecked.append({"check": "memory", "error": str(exc)})

    # -- disk
    try:
        disks = disk_usage()
        checked.append("disk")
        for vol in disks["volumes"]:
            if vol.get("error"):
                unchecked.append({"check": f"disk {vol['volume']}",
                                  "error": vol["error"]})
                continue
            if vol["free_gib"] < DISK_LOW_GIB:
                finding("warn", f"disk {vol['volume']}",
                        f"{vol['free_gib']} GiB free of {vol['total_gib']} GiB")
    except Exception as exc:
        unchecked.append({"check": "disk", "error": str(exc)})

    # -- gpu
    try:
        gpu = gpu_state()
        checked.append("gpu")
        for card in gpu["gpus"]:
            if card["temperature_c"] and card["temperature_c"] >= GPU_HOT_C:
                finding("warn", "gpu",
                        f"{card['name']} at {card['temperature_c']} C")
            if (card["vram_total_bytes"] and card["vram_used_bytes"]
                    and card["vram_used_bytes"] / card["vram_total_bytes"] > 0.95):
                finding("info", "gpu",
                        f"{card['name']} VRAM "
                        f"{round(100 * card['vram_used_bytes'] / card['vram_total_bytes'])}% "
                        "used")
    except Exception as exc:
        # No GPU, or nvidia-smi absent. On this machine there is a GPU, so this
        # is a real gap and is reported as one rather than as a healthy machine.
        unchecked.append({"check": "gpu", "error": str(exc)})

    # -- event log
    if check_event_log:
        try:
            events = event_log(hours=24, limit=40)
            checked.append("event_log")
            if events.get("error"):
                unchecked.append({"check": "event_log",
                                  "error": events["error"]})
            else:
                errors = [e for e in events["entries"] if e.get("level") == 1]
                if len(errors) >= 5:
                    finding("warn", "event_log",
                            f"{len(errors)} errors in the last 24h; "
                            f"latest: {errors[0].get('message', '')[:120]}")
        except Exception as exc:
            unchecked.append({"check": "event_log", "error": str(exc)})
    else:
        unchecked.append({"check": "event_log",
                          "error": "skipped by the caller"})

    order = {"error": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["level"], 3))
    worst = findings[0]["level"] if findings else "ok"
    return {
        "verdict": worst,
        "findings": findings,
        "checked": checked,
        "unchecked": unchecked,
        # Spelled out, because "all clear" from a check that skipped the GPU is
        # a lie and the reader cannot tell without this.
        "complete": not unchecked,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "measured": True,
    }


# --- Tier 3 administration: the grammars, and what is behind them ------------

# Every one of these is refused as a shape, not sanitised as a string.

#: Registry hives this module will name. `HKU` is deliberately absent: its
#: subkeys are other people's user profiles, and writing to another account's
#: hive is not administration of *this* machine.
REG_HIVES: dict[str, Any] = {
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKCR": winreg.HKEY_CLASSES_ROOT,
}

#: Keys that make something run again after a reboot. Writing here is how
#: software persists, and the distinction that matters is not "is this setting
#: dangerous" but "does it make the assistant harder to remove". A tool that
#: writes `Run` can be asked to write it by anything that can call the tool, and
#: the undo journal would faithfully record the inverse - which undoes the write
#: without undoing the persistence, because the persistence is now in a place
#: nobody is looking at.
REG_PERSISTENCE_KEYS: tuple[str, ...] = (
    r"Software\Microsoft\Windows\CurrentVersion\Run",
    r"Software\Microsoft\Windows\CurrentVersion\RunOnce",
    r"Software\Microsoft\Windows\CurrentVersion\RunServices",
    r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run",
    r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Shell",
    r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Userinit",
    r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon\Notify",
    r"Software\Microsoft\Windows NT\CurrentVersion\Windows\AppInit_DLLs",
    r"Software\Microsoft\Windows NT\CurrentVersion\Image File Execution Options",
)

#: The value types `reg add` accepts, mapped to the `winreg` constant. Kept as
#: an explicit allowlist rather than passing a caller-supplied string through to
#: a lookup: a lookup on an arbitrary key is how "REG_SZ" and "reg_sz" and
#: something else entirely all reach the same call.
REG_VALUE_TYPES: dict[str, int] = {
    "REG_SZ": winreg.REG_SZ,
    "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ,
    "REG_MULTI_SZ": winreg.REG_MULTI_SZ,
    "REG_DWORD": winreg.REG_DWORD,
    "REG_QWORD": winreg.REG_QWORD,
    "REG_BINARY": winreg.REG_BINARY,
}

#: A registry key: a hive, one backslash, then a path. Backslash-separated
#: segments, no empty segments, no `..`, because a key name is a literal name
#: and a path that walks upwards is a request to mean something else.
_KEY_SEGMENT = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]+$")

#: A registry value *name*. Spaces are fine - they are ordinary characters in a
#: value name, and `reg query` prints them - but a backslash is not, because it
#: would be read as a key separator.
_VALUE_NAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,163}$")


def registry_key_refusal(key: str) -> str | None:
    """Why this registry key may not be written, or None if it may.

    Three separate refusals, each with its own reason, because "invalid" is not
    an explanation and the three have different fixes:

    * not a hive we write - a typo, or an attempt at another account's hive;
    * not a key path - a malformed name, or a `..` that means a different key
      than it looks like;
    * a persistence key - well-formed, permitted in general, and refused
      because writing it makes software that runs at boot.

    Returns rather than raises because `daemon.py` calls this from a preview,
    and a preview that raises takes the whole confirmation down with it.
    """
    if not isinstance(key, str) or not key.strip():
        return ("key is required. Use hive\\subkey, for example "
                "HKCU\\Software\\Vendor\\Setting")
    hive, sep, rest = key.partition("\\")
    if not sep:
        return (f"{key!r} names no hive. Use hive\\subkey - one of "
                f"{', '.join(REG_HIVES)}")
    if hive.upper() not in REG_HIVES:
        return (f"hive {hive!r} is not writable here. Use one of "
                f"{', '.join(REG_HIVES)} - HKU is not included because its "
                f"subkeys are other people's profiles, not this machine")
    if not rest.strip():
        return f"{key!r} names a hive but no key under it"
    for segment in rest.split("\\"):
        if not segment.strip():
            return (f"{key!r} has an empty path segment (a doubled backslash). "
                    f"Windows treats that as a key with a blank name, which is "
                    f"never what was meant")
        if segment.strip() in (".", ".."):
            return (f"{key!r} contains {segment!r}. A registry key name is "
                    f"literal - there is no parent to walk up to")
        if not _KEY_SEGMENT.match(segment):
            return (f"the key segment {segment!r} contains a character Windows "
                    f"does not allow in a key name")
    folded = rest.lower().replace("/", "\\")
    for blocked in REG_PERSISTENCE_KEYS:
        low_blocked = blocked.lower()
        if folded == low_blocked or folded.startswith(low_blocked + "\\"):
            return (f"{key!r} is a persistence key ({blocked}). This tool can "
                    f"change a setting; it will not make software that runs "
                    f"again at boot, because that is the one change an undo "
                    f"journal cannot fully take back")
    return None


def registry_value_refusal(name: str) -> str | None:
    """Why this value name may not be written, or None if it may."""
    if not isinstance(name, str) or not name.strip():
        return ("name is required - the name of the value inside the key")
    if not _VALUE_NAME.match(name):
        return (f"the value name {name!r} contains a character a registry "
                f"value name cannot hold (a backslash is read as a key "
                f"separator; the rest are reserved)")
    return None


def registry_data_refusal(kind: str, data: Any) -> str | None:
    """Whether `data` is the right shape for `kind`, with the mismatch named.

    Checked here rather than at the `winreg` call because the failure there is
    a `TypeError` naming an internal type, which tells a human nothing about
    which of their two arguments was wrong.
    """
    if kind == "REG_MULTI_SZ":
        if not isinstance(data, (list, tuple)) or not all(
                isinstance(v, str) for v in data):
            return ("REG_MULTI_SZ takes a list of strings, one per line of the "
                    "value, e.g. [\"first\", \"second\"]")
        return None
    if kind == "REG_BINARY":
        if not isinstance(data, (bytes, bytearray)):
            return ("REG_BINARY takes bytes, not text. It is the only type here "
                    "whose data is not human-readable, which is also why it is "
                    "the one to be most careful with")
        return None
    if kind in ("REG_DWORD", "REG_QWORD"):
        number = data
        if isinstance(data, str):
            # A model writing a DWORD as "42" in JSON is writing the right
            # thing in the wrong type, and `registry_write` converts it. The
            # refusal and the conversion have to agree, or the conversion is
            # unreachable code and the refusal is a wall - which is what they
            # were: the refusal rejected every string, so the branch below it
            # could never run.
            try:
                number = int(data.strip(), 0)
            except ValueError:
                return (f"{kind} takes a whole number. Use 42, or the string "
                        f'"42" - not 4.2, and not "forty two"')
        elif isinstance(data, bool) or not isinstance(data, int):
            return (f"{kind} takes a whole number. Use 42, or the string "
                    f'"42" - not 4.2 and not true')
        if kind == "REG_DWORD" and not -(2 ** 31) <= number < 2 ** 32:
            return f"REG_DWORD holds 32 bits; {data} does not fit"
        if kind == "REG_QWORD" and not -(2 ** 63) <= number < 2 ** 64:
            return f"REG_QWORD holds 64 bits; {data} does not fit"
        return None
    if not isinstance(data, str):
        return (f"{kind} takes text. A number is accepted and converted, "
                f"because a model writing a DWORD as 42 in JSON is writing "
                f"the right thing in the wrong type - but nothing else is "
                f"converted")
    return None


def _registry_split(key: str) -> tuple[Any, str]:
    hive, _, rest = key.partition("\\")
    return REG_HIVES[hive.upper()], rest


def registry_read(key: str) -> dict[str, Any]:
    """Every value under one key, with its type, as Windows holds it.

    Reads through `winreg` rather than parsing `reg query`: the query output is
    a fixed-column table that spaces names to align them, so a value named
    "x" and one named "x    " are the same line. The structured read is also
    what the preview needs - it wants to show what is there *now*, before the
    write, not a summary.
    """
    refusal = registry_key_refusal(key)
    if refusal:
        return {"key": key, "exists": False, "values": {}, "error": refusal,
                "measured": True}
    hive, rest = _registry_split(key)
    try:
        handle = winreg.OpenKey(hive, rest)
    except FileNotFoundError:
        return {"key": key, "exists": False, "values": {}, "measured": True,
                "note": "the key does not exist yet; writing it will create it"}
    except PermissionError:
        return {"key": key, "exists": False, "values": {},
                "error": "access denied reading this key. On Windows this is "
                         "usually HKLM, which needs an elevated daemon",
                "measured": True}
    except OSError as exc:
        return {"key": key, "exists": False, "values": {},
                "error": str(exc), "measured": True}
    reverse = {v: k for k, v in REG_VALUE_TYPES.items()}
    values: dict[str, dict[str, Any]] = {}
    with handle:
        try:
            count = winreg.QueryInfoKey(handle)[1]
        except OSError as exc:
            return {"key": key, "exists": False, "values": {},
                    "error": str(exc), "measured": True}
        for i in range(count):
            try:
                name, value, kind = winreg.EnumValue(handle, i)
            except OSError:
                continue
            if isinstance(value, bytes):
                shown: Any = value.hex()
            elif isinstance(value, (list, tuple)):
                shown = list(value)
            else:
                shown = value
            values[name or "(Default)"] = {
                "type": reverse.get(kind, f"0x{kind:08x}"), "data": shown}
    return {"key": key, "exists": True, "values": values, "measured": True}


def registry_write(key: str, name: str, kind: str,
                   data: Any = None, *, delete: bool = False) -> dict[str, Any]:
    """Set or remove one value under one key. Refuses; never raises on refusal.

    All four refusals (key shape, value name, value type, data shape) run
    before anything is opened, so a rejected write cannot have half happened.

    `delete` removes the value, not the key. Deleting a key is not offered:
    `reg delete /f` on a key with subkeys takes them too, and there is no undo
    for that beyond a file backup the caller may not have made.
    """
    refusal = (registry_key_refusal(key) or registry_value_refusal(name)
               or (None if kind in REG_VALUE_TYPES else
                   f"type {kind!r} is not one of "
                   f"{', '.join(REG_VALUE_TYPES)}")
               or (None if delete else registry_data_refusal(kind, data)))
    if refusal:
        return {"ok": False, "error": refusal, "measured": True}
    hive, rest = _registry_split(key)
    coerced: Any = data
    if kind in ("REG_DWORD", "REG_QWORD") and isinstance(data, str):
        try:
            coerced = int(data.strip(), 0)
        except ValueError:
            return {"ok": False,
                    "error": f"{kind} takes a whole number; {data!r} is not one",
                    "measured": True}
    elif kind in ("REG_SZ", "REG_EXPAND_SZ") and not isinstance(data, str):
        coerced = str(data)
    elif kind == "REG_MULTI_SZ":
        coerced = list(data)
    try:
        with winreg.CreateKeyEx(hive, rest, 0, winreg.KEY_SET_VALUE) as handle:
            if delete:
                try:
                    winreg.DeleteValue(handle, name)
                except FileNotFoundError:
                    return {"ok": False,
                            "error": f"there is no value named {name!r} under "
                                     f"{key}, so there was nothing to remove",
                            "measured": True}
            else:
                winreg.SetValueEx(handle, name, 0, REG_VALUE_TYPES[kind], coerced)
    except PermissionError:
        return {"ok": False,
                "error": "access denied. On Windows this is usually HKLM, "
                         "which needs an elevated daemon - this one is not",
                "measured": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "measured": True}
    return {"ok": True, "key": key, "name": name,
            "removed": bool(delete),
            "type": None if delete else kind, "measured": True}


# --- package managers -------------------------------------------------------

#: Preference order for `package.install`, resolved by what is actually here.
#: winget first because it is the one Windows ships and the one that knows
#: about Store and MSIX packages a package manager cannot; choco second; scoop
#: last because it is per-user and its own package ids are the least portable.
PACKAGE_MANAGERS: tuple[str, ...] = ("winget", "choco", "scoop")

_MANAGER_ARGV: dict[str, list[str]] = {
    "winget": ["install", "--exact", "--accept-package-agreements",
               "--accept-source-agreements", "--disable-interactivity"],
    "choco": ["install", "-y", "--no-progress"],
    "scoop": ["install"],
}
_MANAGER_QUERY: dict[str, list[str]] = {
    "winget": ["list", "--exact", "--accept-source-agreements"],
    "choco": ["list", "--exact", "--limit-output"],
    "scoop": ["list"],
}


def _winget_path() -> Path | None:
    """Where winget lives when PATH does not know.

    Measured, not assumed: on this machine `shutil.which("winget")` is None
    while the binary answers `--version` with `v1.29.380`. winget is a Store
    app whose launcher is a per-user alias directory, and that directory is not
    on a PATH inherited from a service. The notepad++ slice already paid for
    the difference between "not on PATH" and "not installed".
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    candidate = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"
    return candidate if candidate.exists() else None


def manager_exe(name: str) -> str | None:
    """The absolute path of a package manager, or None if it is not installed."""
    if name == "winget":
        return str(_winget_path()) if _winget_path() else None
    return shutil.which(name)


def package_managers() -> dict[str, Any]:
    """Which package managers exist here, and at what version.

    Reports the resolved path rather than the name alone, because "winget" in a
    preview means nothing to someone who has just learned that this machine has
    two of them and only one is on PATH.
    """
    found: list[dict[str, Any]] = []
    for name in PACKAGE_MANAGERS:
        exe = manager_exe(name)
        if not exe:
            continue
        row: dict[str, Any] = {"name": name, "exe": exe, "on_path": bool(
            shutil.which(name))}
        try:
            probe = run_capture([exe, "--version"], timeout=60.0)
            row["version"] = (probe.get("stdout") or "").strip()[:40]
            row["usable"] = probe.get("exit_code") == 0
        except Exception as exc:                   # noqa: BLE001 - a read
            row["error"] = str(exc)
            row["usable"] = False
        found.append(row)
    return {"count": len(found), "managers": found,
            "note": "" if found else
            "no package manager was found on this machine, so "
            "package.install cannot install anything",
            "measured": True}


def manager_install_argv(name: str, package: str,
                         version: str = "") -> list[str]:
    """The exact argv `package.install` would run, for the preview to show.

    Built in one place so the preview and the executor cannot disagree about
    what was blessed - a preview that shows one command and an executor that
    runs another is D32 with the confirmation attached to the wrong payload.
    """
    exe = manager_exe(name)
    if not exe:
        raise FileNotFoundError(name)
    argv = [exe] + _MANAGER_ARGV[name] + [package]
    if version:
        argv += ["--version", version] if name == "winget" else \
                ["--version=" + version]
    return argv


def manager_uninstall_argv(name: str, package: str) -> list[str]:
    """The exact argv `package.uninstall` would run, for the preview to show."""
    exe = manager_exe(name)
    if not exe:
        raise FileNotFoundError(name)
    if name == "winget":
        return [exe, "uninstall", "--exact", "--disable-interactivity", package]
    return [exe, "uninstall", "-y", package]


#: A package id as a manager spells one. `+` is there because Notepad++ is the
#: one package on this machine whose id is not all lowercase letters, and a
#: grammar that refuses it would refuse the most obvious request on the desk.
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\- ]{0,127}$")
PACKAGE_NAME_OK = _PACKAGE_ID


def package_installed_version(manager: str, package: str) -> str:
    """The version a manager reports for a package, or "" if it will not say.

    A separate call from `package_owned_by` rather than a return value of it,
    because the two answer different questions and the second one is allowed to
    come back empty: a manager can claim a package and decline to name its
    version, and an uninstall that guessed the version would put back
    something that is not what came out.
    """
    exe = manager_exe(manager)
    if not exe:
        return ""
    try:
        probe = run_capture([exe] + _MANAGER_QUERY[manager] + [package],
                            timeout=120.0)
    except Exception:                             # noqa: BLE001 - a read
        return ""
    if probe.get("exit_code") != 0:
        return ""
    for raw in (probe.get("stdout") or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name "):
            continue
        if manager == "choco":
            parts = line.split("|", 1)
            if parts[0].strip().lower() == package.lower():
                return parts[1].split()[0] if len(parts) > 1 and parts[1].strip() \
                    else ""
        elif manager == "winget":
            fields = line.split()
            if fields and fields[0].lower() == package.lower():
                return fields[1] if len(fields) > 1 else ""
        elif line.lower() == package.lower():
            return ""
    return ""


def package_owned_by(manager: str, package: str) -> bool:
    """Whether *this* manager says it installed the package.

    Not by exit code. Measured on this machine: `choco list <name> --exact
    --limit-output` exits 0 with empty stdout both for a package that is
    installed and for one that is not, and `choco info` exits 0 with nothing at
    all. An exit code cannot distinguish those two, so the *contents* of the
    listing are the evidence, and the evidence is compared rather than the
    return value - which is the D55 shape one level up.
    """
    exe = manager_exe(manager)
    if not exe:
        return False
    try:
        probe = run_capture([exe] + _MANAGER_QUERY[manager] + [package],
                            timeout=120.0)
    except Exception:                             # noqa: BLE001 - a read
        return False
    if probe.get("exit_code") != 0:
        return False
    text = (probe.get("stdout") or "")
    # choco prints "name|version"; winget prints a table whose first column is
    # the id. Matching on the id as a whole line, or as the text before the
    # first pipe, keeps "note++" from matching "notepad++".
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("name "):
            continue
        if manager == "choco":
            if line.split("|", 1)[0].strip().lower() == package.lower():
                return True
        elif manager == "winget":
            if line.split()[0].lower() == package.lower():
                return True
        elif line.lower() == package.lower():
            return True
    return False


# --- services ---------------------------------------------------------------

#: A service name as the SCM registers one. Backslash is allowed because
#: services genuinely use it for their own grouping, and the dot because
#: several are named `Foo.Bar`. Everything that could mean "a different
#: service than the one you named" is refused: a space, a quote, a wildcard,
#: or a path separator that is not a backslash.
_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.\-\\]{1,64}$")
SERVICE_NAME_OK = _SERVICE_NAME

#: Services this tool will not stop, with the reason attached. Not a blocklist
#: of "dangerous" names - two categories that are unsafe specifically because
#: the undo does not restore anything:
#:
#: * **boot-critical** - stopping these puts the machine into a state it wants
#:   to reboot out of, so the undo ("start it") is not available, because the
#:   machine is already on its way down;
#: * **security** - stopping these does not break anything visibly, it just
#:   stops protecting, and the user finds out later from something else.
SERVICE_NO_STOP: dict[str, str] = {
    "RpcSs": "RPC - boot-critical; stopping it makes Windows reboot",
    "DcomLaunch": "DCOM launcher - boot-critical, same",
    "RpcEptMapper": "RPC endpoint mapper - boot-critical, same",
    "LSM": "Local session manager - the console session itself",
    "Power": "the power service; the machine's shutdown depends on it",
    "PlugPlay": "plug and play; boot-critical on this class of machine",
    "BFE": "Base Filtering Engine - Windows Firewall depends on it, and a "
           "machine with no firewall does not know it has no firewall",
    "MpsSvc": "Windows Defender's firewall service - same",
    "WinDefend": "Microsoft Defender antivirus",
    "WdNisSvc": "Defender network inspection",
    "CryptSvc": "Cryptographic Services - signature checking, TLS, and the "
                "thing that decrypts the disk",
    "EventLog": "the event log - stopping it erases the record of what "
                "happened before the failure",
    "Schedule": "the Task Scheduler - other services depend on it being up",
    "Winmgmt": "WMI - a large part of the machine's own diagnostics go quiet",
    "Dhcp": "DHCP client - stopping it drops the network lease",
    "Dnscache": "DNS client - name resolution stops working",
    "Audiodg": "Windows Audio Device Graph - the sound stack",
    "Themes": "the theme service",
    "ProfSvc": "user profile service",
    "NlaSvc": "network location awareness - reported separately so it is a "
              "deliberate choice rather than a discovery",
}


def service_state(name: str) -> dict[str, Any]:
    """One service's current state, by name.

    A thin, honest wrapper over `service_status`, which already answers "no
    such service" with a note rather than an exception - `sc` exits 1060 for
    that, and 1060 is the answer to the question that was asked.
    """
    found = service_status(name)
    rows = found.get("services") or []
    if not rows:
        return {"service": name, "exists": False, "measured": True,
                "note": found.get("note", f"no service matched {name!r}")}
    row = rows[0]
    return {"service": row.get("name", name), "exists": True,
            "state": row.get("state"), "state_name": row.get("state_name"),
            "running": row.get("state") == "4", "measured": True,
            "display_name": row.get("display_name", "")}


# --- power ------------------------------------------------------------------

#: The shortest delay this tool will schedule. Below it, `shutdown /s /t 0`
#: takes effect before the confirmation dialog has finished being read, which
#: means the confirmation is theatre: the thing the user blessed had already
#: happened by the time they blessed it.
POWER_MIN_DELAY_S = 30
POWER_MAX_DELAY_S = 3600
POWER_DEFAULT_DELAY_S = 60

#: What cancels a scheduled shutdown, in the exact words a human can type.
#: Printed in every preview and every result, because the delay is only a
#: kindness if the way to use it is written down.
POWER_CANCEL_HINT = r'stop it yourself with:  shutdown /a'

#: The flags for each power action. The program name is *not* here - see
#: `power_argv`, which is the only place an argv is built, because the flags
#: without the program are not a command.
_POWER_FLAGS: dict[str, list[str]] = {
    "shutdown": ["/s", "/t"],
    "restart": ["/r", "/t"],
    "logoff": ["/l"],
    "cancel": ["/a"],
}

#: The program itself, resolved once at import rather than at each call. This
#: is a Windows system binary; if it is not there the machine is in a state
#: where nothing this module does will work anyway.
POWER_EXE = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32" / "shutdown.exe")


def power_argv(action: str, delay_s: int | None = None) -> list[str]:
    """The exact argv for a power action - program first, then the flags.

    The program name was missing from this for one stretch, so every call
    returned a bare `['/r', '/t', '120']` and `run_capture` raised
    `FileNotFoundError` trying to execute `/r` as a program. It is the kind of
    bug that only appears when something actually runs the list, which is why
    the test asserts on the argv handed to the runner rather than on the
    command string shown in the preview.

    `cancel` is deliberately not given a delay: `/t` means nothing to `/a`, and
    passing it anyway would suggest the cancel is itself a scheduled thing.
    """
    if action not in _POWER_FLAGS:
        raise ValueError(action)
    flags = list(_POWER_FLAGS[action])
    if action in ("shutdown", "restart"):
        flags.append(str(delay_s if delay_s is not None
                         else POWER_DEFAULT_DELAY_S))
    return [POWER_EXE] + flags


# -- the recycle bin ----------------------------------------------------------
# `policy.DENY_PATHS` contains `C:\$Recycle.Bin`, so this module must not read
# that directory - not even by shelling out and naming it inside a script
# string, which would route around `_check_paths` entirely. That is the same
# hole recorded in tests/test_delegate_ops.py: "a path buried inside a `-c`
# source string is opened by python rather than by us."
#
# So everything here goes through two doors instead, and both were measured on
# this machine before anything was written:
#
#   * `Clear-RecycleBin -WhatIf` exits 0 and prints
#     `What if: Performing the operation "Clear-RecycleBin" on target
#     "All of the contents of the Recycle Bin".` - Windows naming its own
#     target is stronger evidence than a count we computed ourselves.
#   * `(New-Object -ComObject Shell.Application).NameSpace(0xA)` enumerates the
#     bin without the directory ever appearing in a command.
#
# The bin was empty when measured, so the COM `Size` column could not be
# verified. There is therefore no byte total anywhere in here: a number that
# was never seen is worse than no number, because it answers a question nobody
# asked with a figure nobody checked.

RECYCLE_PS = "powershell.exe"
RECYCLE_PS_CLEAR = "Clear-RecycleBin -Force -ErrorAction Stop"
RECYCLE_PS_WHATIF = "Clear-RecycleBin -WhatIf -ErrorAction Stop"

#: Named so the refusal to read it can say what it is refusing. The string is
#: documentation, not an operand - nothing passes it to anything.
RECYCLE_BIN_DIR = r"C:\$Recycle.Bin"

_RECYCLE_LIST_PS = (
    "$s = New-Object -ComObject Shell.Application;"
    "$b = $s.NameSpace(0xA);"
    "if ($null -eq $b) { Write-Output '{\"available\": false}'; exit 0 };"
    "$i = @($b.Items());"
    "$n = @($i | ForEach-Object { $_.Name });"
    "Write-Output ('{\"available\": true, \"count\": ' + $i.Count"
    " + ', \"names\": [' + (($n | ForEach-Object { '\"' + $_ + '\"' }) -join ',') + ']}')"
)


def _ps_argv(script: str) -> list[str]:
    """powershell.exe running one script, with the flags that keep it quiet.

    `-NoProfile` because a user's profile can print things of its own and we
    would be reading them as though Windows had said them; `-NonInteractive`
    so a prompt cannot appear and hang a tool call that has nobody to answer
    it.
    """
    return [RECYCLE_PS, "-NoProfile", "-NonInteractive", "-Command", script]


def _recycle_payload(out: str) -> dict[str, Any]:
    """Pull the one JSON line out of PowerShell's output.

    Takes the last line that starts with `{` and ends with `}`, because a
    profile or a warning can precede it and we want our own line, not theirs.
    Returns `{}` when there is no such line - and `{}` means "PowerShell did
    not answer", never "the bin is empty".
    """
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                got = json.loads(line)
            except ValueError:
                continue
            if isinstance(got, dict):
                return got
    return {}


def recycle_listing() -> dict[str, Any]:
    """What is in the recycle bin, asked through the shell namespace.

    Never reports a byte total, and never falls back to reading a directory.
    If the COM object cannot be reached the answer is `available: False` with
    the reason attached - because the alternative, a count of zero, reads as
    "there is nothing in there" when the truth is "we could not look".
    """
    got = run_capture(_ps_argv(_RECYCLE_LIST_PS))
    out = str(got.get("stdout") or "")
    payload = _recycle_payload(out)
    if not payload.get("available"):
        return {
            "available": False,
            "count": None,
            "items": [],
            "source": "shell namespace (COM NameSpace 0xA)",
            "denied_directory": RECYCLE_BIN_DIR,
            "note": (
                "the shell namespace could not be reached"
                + (f" (exit {got.get('exit_code')})" if got.get("exit_code")
                   else "")
                + ". The bin was not read by any other route: "
                + RECYCLE_BIN_DIR + " is on this project's deny list."
            ),
            "measured": True,
        }
    names = [str(n) for n in (payload.get("names") or [])]
    count = payload.get("count")
    if not isinstance(count, int):
        count = len(names)
    return {
        "available": True,
        "count": int(count),
        "items": [{"name": n} for n in names],
        "source": "shell namespace (COM NameSpace 0xA)",
        "denied_directory": RECYCLE_BIN_DIR,
        "note": (
            "count and names only - the shell API's size column could not be "
            "verified on this machine, so no byte total is reported rather "
            "than a number nobody checked"
        ),
        "measured": True,
    }


def recycle_whatif() -> dict[str, Any]:
    """Ask Windows what `Clear-RecycleBin` would do, without doing it.

    `names_target` is the judgement, and it comes from **stdout**, not from the
    exit code. An earlier reading would have taken exit 0 as consent, but exit
    0 with no "What if" line means Windows declined to answer - and a preview
    that reported that as confirmation would be claiming something it does not
    have.
    """
    got = run_capture(_ps_argv(RECYCLE_PS_WHATIF))
    out = str(got.get("stdout") or "")
    names_target = "What if:" in out and "Recycle Bin" in out
    return {
        "command": RECYCLE_PS_WHATIF,
        "exit_code": got.get("exit_code"),
        "stdout": out.strip(),
        "stderr": str(got.get("stderr") or "").strip(),
        "names_target": names_target,
        "note": (
            "Windows named its own target, so this is its answer and not ours"
            if names_target else
            "Windows did not name a target; treat this as no confirmation"
        ),
        "measured": True,
    }


def recycle_clear() -> dict[str, Any]:
    """Empty the bin. Returns the outcome; a non-zero exit is not a raise.

    Same rule as `run_capture`: for a probe reading a value, raising is right;
    for a tool whose whole job is to try something irreversible, "it failed and
    here is why" IS the answer.
    """
    got = run_capture(_ps_argv(RECYCLE_PS_CLEAR), timeout=180.0)
    return {
        "command": RECYCLE_PS_CLEAR,
        "exit_code": got.get("exit_code"),
        "ok": got.get("exit_code") == 0,
        "stdout": str(got.get("stdout") or "").strip(),
        "stderr": str(got.get("stderr") or "").strip(),
        "timed_out": bool(got.get("timed_out")),
        "seconds": got.get("seconds"),
        "measured": True,
    }
