"""Tests for reading the machine.

Half of these run against the *real* machine, on purpose. Every probe here
answers a question about the hardware it is standing on, and a test that mocks
`nvidia-smi` proves only that the mock returns what the mock returns. The
interesting failure - a field name that changed, a tasklist column that moved, a
locale that formats numbers differently - cannot be faked into existence.

The other half mocks deliberately, to reach the failures that are hard to cause
on demand: a GPU that has gone away, a disk that cannot be read, an event log
that refuses the query. A health check is only worth having if it is right when
things are fine *and* when they are not.

The rule these are all written against: a probe that fails says so. It never
returns an empty result, because an empty result and a healthy one look the same
to everything downstream.
"""

import pytest

from assistant import sysinfo as S
from assistant.grammar import build_tool_grammar
from assistant.tools import ALL_TOOLS

SYSTEM_NAMES = {
    "system.info", "system.processes", "system.service_status",
    "system.disk_usage", "system.event_log", "system.gpu_state",
    "system.package_list", "system.diagnose",
}


# --- against the real machine -----------------------------------------------


def test_system_info_reports_this_machine():
    r = S.system_info()
    assert r["measured"] is True
    assert r["cores_physical"] >= 1
    assert isinstance(r["cpu"], str) and r["cpu"]
    # The real registry name, not "AMD" from platform.processor().
    assert "ryzen" in r["cpu"].lower() or "intel" in r["cpu"].lower()
    mem = r["memory"]
    assert mem["total_bytes"] > 1024 ** 3
    assert 0 <= mem["load_percent"] <= 100


def test_uptime_is_plausible_and_does_not_overflow():
    """GetTickCount64, not GetTickCount.

    A 40-day uptime in milliseconds is 3.4 billion, which overflows the 32-bit
    variant and wraps negative. The test cannot force a 40-day machine, so it
    checks the shape: positive, and not absurd.
    """
    up = S.uptime_seconds()
    assert up is not None
    assert 0 < up < 100 * 365 * 86400


def test_cpu_load_is_between_zero_and_a_hundred():
    load = S.cpu_load_percent(0.05)
    assert load is None or 0.0 <= load <= 100.0


def test_the_gpu_probe_finds_the_rtx_3060():
    r = S.gpu_state()
    assert r["count"] >= 1
    card = r["gpus"][0]
    assert "3060" in card["name"] or "NVIDIA" in card["name"]
    assert 8 * 1024 ** 3 < card["vram_total_bytes"] < 16 * 1024 ** 3
    assert card["vram_used_bytes"] <= card["vram_total_bytes"]


def test_disk_usage_finds_c_and_reports_real_numbers():
    r = S.disk_usage()
    volumes = {v["volume"]: v for v in r["volumes"]}
    assert "C:" in volumes, "the system volume must be found"
    c = volumes["C:"]
    assert c["free_bytes"] < c["total_bytes"]
    assert 0 <= c["used_percent"] <= 100
    # free must not exceed total - a disk that reports 200% full is a parse bug
    assert c["free_gib"] <= c["total_gib"]


def test_processes_are_ranked_by_memory_and_parse_the_real_format():
    """This caught a real bug.

    `tasklist /FO CSV` emits `"8 K"` and `"3,648 K"` - thousands separators *and*
    a K suffix. Parsing it as a bare integer raised ValueError on the very first
    row, so the probe had never worked. The format is not documented anywhere
    worth reading, which is why this test reads the real output.
    """
    r = S.processes(limit=10)
    assert r["count"] > 10, "this machine is running something"
    assert r["returned"] <= 10
    mems = [p["mem_bytes"] for p in r["processes"]]
    assert mems == sorted(mems, reverse=True), "must be sorted by memory"
    assert all(isinstance(p["pid"], int) for p in r["processes"])
    # every row survived parsing: a dropped row reads as a missing process
    assert len(r["processes"]) == min(10, r["count"])


def test_the_spooler_service_is_found_by_name():
    r = S.service_status("Spooler")
    assert r["count"] == 1
    assert r["services"][0]["name"] == "Spooler"
    assert r["services"][0]["state"].isdigit()


def test_a_service_that_does_not_exist_says_so_rather_than_erroring():
    r = S.service_status("NoSuchServiceNameXyzzy")
    assert r["count"] == 0
    assert "no service matched" in r["note"]


def test_the_package_list_is_large_and_searchable():
    """721 entries on this machine, which is what makes the search useful."""
    everything = S.package_list(limit=5)
    assert everything["count"] > 100
    assert everything["returned"] == 5

    hit = S.package_list(query="nvidia", limit=10)
    assert hit["count"] >= 1
    # The query matches name OR publisher, so a hit may be an NVIDIA driver
    # published by someone else. Checking only names would fail for the right
    # reason on the wrong field.
    blob = " ".join(
        f"{p['name']} {p['publisher']}" for p in hit["packages"]
    ).lower()
    assert "nvidia" in blob


def test_the_event_log_probe_answers_even_when_it_is_quiet():
    """Zero entries is a real answer; a failure is a different one."""
    r = S.event_log(hours=1, limit=5)
    assert r.get("error") is None or r["measured"] is not True
    if "error" not in r:
        assert r["count"] == len(r["entries"])


def test_diagnose_reports_what_it_checked_and_what_it_could_not():
    """The load-bearing assertion: a health check that skipped something must
    say it skipped it.

    A check which quietly checks four of six things and reports "all clear" is
    worse than no health check, because it is believed.
    """
    r = S.diagnose()
    assert r["measured"] is True
    assert set(r["checked"]) <= {"memory", "disk", "gpu", "event_log"}
    assert r["verdict"] in ("ok", "info", "warn", "error")
    assert isinstance(r["findings"], list)
    if r["unchecked"]:
        assert r["complete"] is False, (
            "it reported an incomplete check and still called itself complete"
        )
    else:
        assert r["complete"] is True
    # every skipped check must say why
    for gap in r["unchecked"]:
        assert gap.get("error")


def test_diagnose_runs_fast_enough_to_be_a_tool():
    """It sits inside a tool call the user is waiting on."""
    import time

    started = time.monotonic()
    S.diagnose(check_event_log=False)
    assert time.monotonic() - started < 8.0


# --- the failures, faked -----------------------------------------------------


def test_a_missing_gpu_is_reported_not_swallowed(monkeypatch):
    def boom(argv, timeout=None):
        raise RuntimeError("nvidia-smi exited 1: no devices found")

    monkeypatch.setattr(S, "run_command", boom)
    with pytest.raises(RuntimeError, match="no devices"):
        S.gpu_state()


def test_a_gpu_that_answers_but_reports_nothing_is_an_error(monkeypatch):
    """An empty GPU list is not a healthy machine."""
    monkeypatch.setattr(S, "run_command", lambda a, timeout=None: "\n\n")
    with pytest.raises(RuntimeError, match="no GPU"):
        S.gpu_state()


def test_a_non_numeric_gpu_field_becomes_null_not_zero(monkeypatch):
    """`[N/A]` for utilisation on some cards must not read as "0% utilised",
    which would tell the user their GPU is idle while it is on fire.

    The fake emits bare numbers because the real query asks for `nounits`; a
    fixture carrying "MiB" would fail the parse for the wrong reason and hide the
    case this is actually about.
    """
    monkeypatch.setattr(S, "run_command", lambda a, timeout=None:
                        "NVIDIA GeForce RTX 3060, 12288, 1024, 11264, "
                        "[N/A], 40, 50.00")
    card = S.gpu_state()["gpus"][0]
    assert card["utilisation_percent"] is None, (
        "[N/A] must not read as 0% - that would report an on-fire GPU as idle"
    )
    # nvidia-smi returns MiB even with nounits; the probe scales to bytes.
    assert card["vram_total_bytes"] == 12288 * 1024 * 1024
    assert card["temperature_c"] == 40.0


def test_diagnose_says_gpu_is_unchecked_when_the_gpu_is_gone(monkeypatch):
    """The whole point. A machine with no readable GPU must not be reported as
    healthy without qualification."""
    monkeypatch.setattr(S, "run_command",
                        lambda a, timeout=None: (_ for _ in ()).throw(
                            RuntimeError("nvidia-smi not found")))
    r = S.diagnose(check_event_log=False)
    assert r["verdict"] != "ok" or r["complete"] is False
    assert any(g["check"] == "gpu" for g in r["unchecked"])


def test_an_unreadable_volume_is_listed_as_a_gap_not_skipped(monkeypatch):
    real_usage = S.shutil.disk_usage

    def fake_usage(path):
            if path == "C:\\":
                raise OSError("access denied")
            return real_usage(path)

    monkeypatch.setattr(S.os.path, "exists", lambda p: p == "C:\\")
    monkeypatch.setattr(S.shutil, "disk_usage", fake_usage)
    r = S.disk_usage()
    c = [v for v in r["volumes"] if v["volume"] == "C:"][0]
    assert "error" in c, "an unreadable volume must say so"


def test_an_event_log_failure_is_a_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(S, "run_command", lambda a, timeout=None: (_ for _ in ())\
        .throw(RuntimeError("wevtutil exited 1: access denied")))
    r = S.event_log()
    assert r["measured"] is False
    assert "access denied" in r["error"]


def test_wevtutil_parsing_drops_an_unreadable_level_rather_than_defaulting(monkeypatch):
    """A level of 0 reads as "informational". A truncated error line must not
    become one."""
    text = (
        "Date: 2026-09-30 10:00:00\n"
        "Level: not-a-number\n"
        "Source: Disk\n"
        "Event ID: 7\n\n"
        "Date: 2026-09-30 11:00:00\n"
        "Level: 1\n"
        "Source: Disk\n"
        "Event ID: 7\n"
    )
    entries = S._parse_wevtutil(text, 10)
    assert len(entries) == 2
    assert entries[0]["level"] is None, "an unreadable level must be None"
    assert entries[1]["level"] == 1


def test_a_low_disk_is_raised_as_a_finding(monkeypatch):
    class FakeUsage:
        total = 100 * S._GIB
        free = 2 * S._GIB
        used = 98 * S._GIB

    monkeypatch.setattr(S.os.path, "exists", lambda p: p == "C:\\")
    monkeypatch.setattr(S.shutil, "disk_usage", lambda p: FakeUsage())
    monkeypatch.setattr(S, "gpu_state", lambda: {"gpus": []})
    r = S.diagnose(check_event_log=False)
    assert any(f["check"] == "disk C:" for f in r["findings"])
    assert r["verdict"] in ("warn", "error")


def test_a_hot_gpu_is_raised_as_a_finding(monkeypatch):
    monkeypatch.setattr(S, "gpu_state", lambda: {"gpus": [
        {"name": "NVIDIA GeForce RTX 3060", "vram_total_bytes": 12 * S._GIB,
         "vram_used_bytes": 1 * S._GIB, "vram_free_bytes": 11 * S._GIB,
         "utilisation_percent": 99.0, "temperature_c": 91.0, "power_watts": 200.0}
    ]})
    r = S.diagnose(check_event_log=False)
    assert any(f["check"] == "gpu" for f in r["findings"])
    assert r["verdict"] in ("warn", "error")


def test_a_skip_the_event_log_is_recorded_as_a_skip():
    r = S.diagnose(check_event_log=False)
    assert "event_log" not in r["checked"]
    assert any(g["check"] == "event_log" and "skipped" in g["error"]
               for g in r["unchecked"])


# --- limits are clamped, not obeyed ------------------------------------------


@pytest.mark.parametrize("bad,good", [(-5, 1), (0, 1), (10 ** 9, 200)])
def test_a_nonsense_process_limit_is_clamped_not_obeyed(bad, good):
    r = S.processes(limit=bad)
    assert r["returned"] <= good


def test_a_nonsense_event_window_is_clamped():
    r = S.event_log(hours=0, limit=1)
    assert r["hours"] == 1
    r = S.event_log(hours=10 ** 9, limit=1)
    assert r["hours"] <= 24 * 30


# --- the registry agrees with reality ----------------------------------------


def test_every_system_tool_has_an_executor():
    from assistant.daemon import Daemon

    missing = SYSTEM_NAMES - set(Daemon._EXECUTORS)
    assert not missing, f"registered but not executable: {sorted(missing)}"


def test_every_system_tool_is_described_and_in_the_grammar():
    """An undescribed tool is excluded from the grammar, which would make it
    uncallable - a capability the assistant has and cannot use."""
    g = build_tool_grammar(ALL_TOOLS)
    assert SYSTEM_NAMES <= set(g.tools), (
        f"not in the grammar: {sorted(SYSTEM_NAMES - set(g.tools))}"
    )


def test_the_package_query_is_in_the_grammar_because_the_executor_honours_it():
    """Caught a real gap.

    `system.package_list` was registered as taking no arguments while its
    executor honoured a `query` - so the grammar forbade the one argument that
    made the tool worth having, and the model could only ever dump the whole
    721-entry list.
    """
    g = build_tool_grammar(ALL_TOOLS)
    assert '"\\"query\\""' in g.gbnf
    assert '"\\"limit\\""' in g.gbnf


def test_the_probes_take_no_arguments_the_grammar_could_not_supply():
    """Every argument an executor reads must be describable.

    A wrapper reading `args.get("x")` for something the grammar cannot emit is a
    capability the model has no way to reach, which is the D31 problem with an
    extra step.
    """
    import inspect

    import assistant.daemon as D

    for tool in sorted(SYSTEM_NAMES):
        fn = D.Daemon._EXECUTORS[tool]
        src = inspect.getsource(fn)
        keys = set()
        for token in ("limit", "name", "hours", "query", "event_log"):
            if f'"{token}"' in src:
                keys.add(token)
        spec = next(t for t in ALL_TOOLS if t.name == tool)
        described = set(spec.parameters or {})
        assert keys <= described, (
            f"{tool} reads {keys - described} but the grammar cannot supply it"
        )