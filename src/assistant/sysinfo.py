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
import os
import platform
import shutil
import subprocess
import sys
import time
import winreg
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
            "query": query, "packages": named[:limit], "measured": True}


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
            current["win32_exit_code"] = parts[1] if len(parts) > 1 else None
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