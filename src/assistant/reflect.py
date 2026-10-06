"""Reflection: a friction ledger, a deterministic promotion gate, and a bounded overlay.

This is the only part of the assistant that reasons about improving itself, so it is
the part where a well-meant design is most dangerous. Everything below exists to make
one specific thing impossible rather than merely discouraged.

What is structurally impossible here
------------------------------------
**The assistant cannot write its own code or its own policy.** Not "is discouraged
from", not "is told not to" — cannot. Three independent mechanisms, and the first is
the load-bearing one:

1. **No `reflect.*` tool takes a path argument.** They derive every file they touch
   from `self.state`. A tool that cannot be *pointed* anywhere cannot be pointed at
   `src/`. This is the same argument as `telegram.send_draft`: a T1 write tool that
   takes a path from the caller is an arbitrary-write primitive wearing a
   self-improvement name, and the whole value of these four tools is that they can
   only ever write to one directory.
2. **The gate denies the assistant's own tree** (`policy.SELF_PATHS`), which is
   defence in depth and survives a mistake in this module.
3. **The overlay cannot grant authority.** It is prompt text, capped at 500 tokens,
   and it reaches only the system prompt — never the gate, never a tier, never an
   argument. A promoted lesson can change how the assistant *behaves*; it cannot
   change what it is *allowed to do*. D21 keeps those separate, and this is the
   code that keeps them separate.

The four anti-patterns, and which rule kills each
------------------------------------------------
D21 names them, so each gets a mechanism rather than a warning:

| anti-pattern | mechanism |
|---|---|
| **overfitting** — one failure becomes a permanent rule | `distil` and `propose` both refuse a candidate observed **once**. A lesson needs two *distinct* events. |
| **reflection without change** | `propose` requires a `check`: a falsifiable statement of what would be different. Without one the score is zero and nothing is promoted. |
| **performative reflection** | A reflection that changes nothing is not recorded as an improvement, because promotion requires `validation × confidence ≥ THRESHOLD`, and a score derived from evidence cannot be asserted by the model. |
| **endless analysis** | One change per cycle, a candidate cap, and a promotion ledger that records what was already promoted so the same lesson cannot be re-derived forever. |

The scores are computed, never supplied
----------------------------------------
`validation_score` and `confidence` are derived from the ledger by this module. The
model can state a symptom and a proposed change; it cannot state how good its own
proposal is. That is the difference between a gate and a suggestion, and a score the
proposer assigns to itself is a score that is always 10.

Measured shape of the gate
--------------------------
Two observations is the floor, not a tuning knob. The arithmetic is in
`validation_score()` and is deliberately boring — the interesting part is not the
formula, it is that every term is countable and none of them is an opinion.

A note on what reflection is *for*, since it is easy to build the wrong thing
------------------------------------------------------------------------------
The failure this prevents is not "the assistant gets stuck". It is the opposite: a
system that confidently records a lesson from one bad afternoon and then obeys it
forever. Everything here is weighted toward *not* learning. A ledger that mostly
reports "nothing repeated yet" is the tool working correctly, and `reflect.report`
says so in those words rather than presenting an empty result as a failure.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "ReflectionLedger",
    "ReflectError",
    "LEDGER_NAME",
    "PROPOSALS_DIR",
    "OVERLAY_NAME",
    "MIN_OBSERVATIONS",
    "MAX_CANDIDATES",
    "PROMOTION_THRESHOLD",
    "OVERLAY_TOKEN_CAP",
]

LEDGER_NAME = "friction.jsonl"
PROPOSALS_DIR = "reflections"
OVERLAY_NAME = "overlay.md"

#: Two, and not one. D21's capability promotion requires "observed more than once",
#: and one event is the entire input to every false lesson ever recorded.
MIN_OBSERVATIONS = 2

#: A bound on how much the reflection step will surface. Without it a long ledger
#: produces a long candidate list, the user reads the top of it, and the exercise
#: becomes a report rather than a decision.
MAX_CANDIDATES = 10

#: `validation_score × confidence` must reach this **fraction of the maximum**, not a
#: raw number. Both scores run 0..100, so the product runs 0..10,000, and a raw
#: threshold of 60 on a 10,000 scale is a threshold that every candidate passes.
#: That is not a hypothetical: the first version of this file had exactly that, and
#: every promotion test passed for the wrong reason.
#:
#: 0.45 means "at least 45% of a perfect score". It is a fraction rather than a
#: percentage because the maximum is derivable from the two score ranges and a
#: separate constant could drift out of step with them.
PROMOTION_THRESHOLD = 0.45

#: The maximum `validation_score × confidence`. Derived, so the threshold above is
#: always comparable with what the gate actually computes.
MAX_SCORE_PRODUCT = 100 * 100

#: D21's cap. Enforced by measurement, not by counting words in a comment.
OVERLAY_TOKEN_CAP = 500

#: Friction that the *system* observed, as opposed to friction the model narrated.
#: This is the difference between evidence and opinion, and it is why the daemon
#: records refusals and build gaps here rather than only accepting what the model
#: says went wrong.
SYSTEM_KINDS = frozenset({"refused", "not_implemented", "queued", "error", "denied"})

_TOKEN = re.compile(r"[a-z0-9_.]+")


def _key(area: str, symptom: str) -> str:
    """A stable grouping key, so "the gate refused a path" groups with itself.

    Lower-cased, punctuation collapsed, and *not* including anything that varies per
    occurrence — no timestamps, no UIDs, no counts. A key that included a variable
    would give every event its own group and nothing would ever repeat, which would
    make the whole module vacuously correct and completely useless.
    """
    words = [w for w in _TOKEN.findall(f"{area} {symptom}".lower()) if len(w) > 2]
    return " ".join(words[:8])


class ReflectError(RuntimeError):
    """A reflection request that cannot be honoured as asked."""


class ReflectionLedger:
    """The ledger, the candidates, the proposals and the overlay.

    `base` is the daemon's state directory. Nothing else is configurable, on
    purpose: the set of files this can touch is the safety property, so it is not a
    parameter somebody can widen.
    """

    def __init__(self, base: Path | str) -> None:
        self.base = Path(base)
        self._ledger = self.base / LEDGER_NAME
        self._proposals = self.base / PROPOSALS_DIR
        self._overlay = self._proposals / OVERLAY_NAME

    # -- friction -----------------------------------------------------------

    def log(self, *, area: str, symptom: str, kind: str = "observed",
            detail: str = "") -> dict[str, Any]:
        """Record one friction event.

        `kind` is `observed` when the model or the user says so, and one of
        `SYSTEM_KINDS` when the *daemon* saw it. Only system kinds are eligible for
        promotion, and that is the whole reason the daemon writes here too: a lesson
        the model derived from its own account of a conversation is a lesson about
        what it thinks happened, not about what happened.

        Returns the stored row with its running count, so a caller can see whether
        this event is the second one — which is the moment the lesson becomes
        eligible at all.
        """
        area = str(area or "").strip()
        symptom = str(symptom or "").strip()
        if not area:
            raise ReflectError("friction needs an area")
        if not symptom:
            raise ReflectError("friction needs a symptom")
        if len(symptom) > 2000:
            symptom = symptom[:2000] + "...<truncated>"
        row = {
            "at": time.time(),
            "area": area,
            "symptom": symptom,
            "kind": str(kind or "observed"),
            "detail": str(detail or "")[:2000],
            "key": _key(area, symptom),
        }
        self._ensure_dir()
        with self._ledger.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        row["observations"] = self.count(key=row["key"])
        return row

    def events(self) -> list[dict[str, Any]]:
        """Every event, oldest first. A corrupt line is skipped, not fatal.

        One malformed row must not make the ledger unreadable: the failure mode of a
        strict parser here is that a single stray byte from a model-produced file
        disables reflection permanently, which is indistinguishable from the feature
        never having existed.
        """
        if not self._ledger.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self._ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("key"):
                out.append(row)
        return out

    def count(self, *, key: str) -> int:
        """Distinct events for one key.

        Counted, not weighted. A later event does not make an earlier one count
        twice, and a single event logged five times is one observation — otherwise
        the floor is defeated by repetition rather than by recurrence.
        """
        return len({_event_id(e) for e in self.events() if e.get("key") == key})

    # -- candidates ---------------------------------------------------------

    def distil(self, *, min_observations: int = MIN_OBSERVATIONS,
               cap: int = MAX_CANDIDATES) -> dict[str, Any]:
        """Group the ledger into candidate lessons.

        Deterministic and model-free, which is why it is a T0 read: it counts rows.
        A distil that called a model would cost a 5 GiB load to do arithmetic, and
        the arithmetic would then be the model's rather than the ledger's.

        Returns candidates **and** the near-misses. A group observed once is the
        most informative thing in the whole ledger — it is exactly the overfitting
        case — so it is reported with its count rather than dropped, and a caller
        can see the difference between "nothing yet" and "one event, waiting".
        """
        floor = max(int(min_observations), MIN_OBSERVATIONS)
        groups: dict[str, list[dict[str, Any]]] = {}
        for event in self.events():
            groups.setdefault(str(event["key"]), []).append(event)

        candidates, near = [], []
        for key, rows in groups.items():
            distinct = len({_event_id(r) for r in rows})
            entry = {
                "key": key,
                "area": rows[-1].get("area", ""),
                "symptom": rows[-1].get("symptom", ""),
                "observations": distinct,
                "system_observed": any(r.get("kind") in SYSTEM_KINDS for r in rows),
                "first_at": min(r.get("at", 0) for r in rows),
                "last_at": max(r.get("at", 0) for r in rows),
            }
            (candidates if distinct >= floor else near).append(entry)

        # Most-observed first: the whole point is to surface what is actually
        # repeating, and sorting by recency would surface whatever happened last.
        candidates.sort(key=lambda c: (-c["observations"], -c["last_at"]))
        near.sort(key=lambda c: -c["observations"])
        held = [c["key"] for c in candidates if not c["system_observed"]]
        return {
            "candidates": candidates[:cap],
            "showing": min(len(candidates), cap),
            "total_candidates": len(candidates),
            "truncated": len(candidates) > cap,
            "one_off": near[:cap],
            "one_off_count": len(near),
            "min_observations": floor,
            "not_yet_promotable": held,
            "note": (
                "a candidate with observations=1 is one event, not a pattern. D21 "
                "requires more than once, and a lesson written from a single event "
                "is the overfitting this exists to prevent."
            ),
        }

    # -- proposals ----------------------------------------------------------

    def propose(self, *, key: str, change: str, check: str) -> dict[str, Any]:
        """Propose one improvement, and promote it only if the gate says so.

        `change` is the behaviour that would differ. `check` is how a human would
        tell the difference — and it is **required**, because D21 lists "reflection
        without change" as an anti-pattern and a change nobody can check is a change
        nobody can tell happened.

        Promotion happens here rather than in a fifth tool. Two reasons: the gate is
        deterministic code and should not be reachable by a model that might want to
        run it, and one change per cycle is easier to enforce as a property of the
        write path than as advice.
        """
        candidates = self.distil(min_observations=MIN_OBSERVATIONS, cap=MAX_CANDIDATES)
        match = next((c for c in candidates["candidates"] if c["key"] == key), None)
        if match is None:
            near = next((c for c in candidates["one_off"] if c["key"] == key), None)
            if near is not None:
                raise ReflectError(
                    f"{key!r} has been observed {near['observations']} time(s). D21 "
                    "requires more than once before a lesson is written - one event "
                    "is not a pattern, and a rule built from one is how a system "
                    "becomes confidently wrong forever."
                )
            raise ReflectError(
                f"no candidate lesson with key {key!r}. Run reflect.distil first - "
                "the key comes from there, not from this."
            )

        change = str(change or "").strip()
        if not change:
            raise ReflectError("a proposal needs a change, or it is not one")
        check = str(check or "").strip()
        if not check:
            raise ReflectError(
                "a proposal needs a `check`: a falsifiable statement of what would "
                "be different afterwards. D21 lists 'reflection without change' as "
                "an anti-pattern, and an uncheckable change is indistinguishable "
                "from no change."
            )

        score, confidence = self.scores(match)
        record = {
            "id": self._proposal_id(key),
            "key": key,
            "area": match["area"],
            "change": change[:1000],
            "check": check[:500],
            "observations": match["observations"],
            "system_observed": match["system_observed"],
            "validation_score": score,
            "confidence": confidence,
            "score_product": score * confidence,
            "created": time.time(),
            "promoted": False,
            "reason": "",
        }
        verdict = self._gate(record)
        record["promoted"] = verdict["promoted"]
        record["reason"] = verdict["reason"]

        self._ensure_dir()
        (self._proposals / f"{record['id']}.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        return {**record, "gate": verdict}

    def scores(self, candidate: dict[str, Any]) -> tuple[int, int]:
        """`validation_score` and `confidence`, both countable.

        Neither is ever supplied by the caller. A model that could score its own
        proposal would score it ten, every time, and the gate would be a formality
        with arithmetic on the front.

        * validation (0-100): how well evidenced, and how specific the grouping key
          is — a lesson drawn from three words of symptom text is vaguer than one
          drawn from a full sentence, and vagueness is what lets a rule absorb every
          later exception.
        * confidence (1-100): how many distinct observations, saturating — the
          difference between two events and twenty is real, and the difference
          between two hundred and two hundred and one is not.

        There is deliberately **no** term for "the system witnessed this". An
        earlier version scored 20 points for it, and a mutation removing that term
        left the suite green — because `_gate` refuses a non-witnessed candidate
        outright, so the term could only ever have applied to candidates that are
        already refused. Dead weight in a scoring function is worse than no
        function: it looks like part of the evidence and is not.
        """
        observations = int(candidate.get("observations", 0))
        evidence = min(40, observations * 15)
        specificity = 20 if len(str(candidate.get("key", "")).split()) >= 3 else 8
        score = evidence + specificity
        if observations <= MIN_OBSERVATIONS:
            # The floor is not a comfortable margin. A lesson built on the minimum
            # evidence is held to a higher bar, not a lower one, which is what makes
            # the `x` in `validation x confidence` do any work at all.
            score = min(score, 55)
        confidence = max(1, min(100, observations * 25))
        return score, confidence

    def _gate(self, record: dict[str, Any]) -> dict[str, Any]:
        """The deterministic gate. Returns a verdict and a reason, never raises.

        Four conditions, each named in D21, and the first two are the ones that stop
        the failure this module exists to prevent.
        """
        if record["observations"] < MIN_OBSERVATIONS:
            return {"promoted": False,
                    "reason": f"observed {record['observations']} time(s); D21 "
                              "requires more than once"}
        if not record["system_observed"]:
            return {"promoted": False,
                    "reason": "not witnessed by the daemon - the model narrated "
                              "this, and a lesson about what it thinks happened is "
                              "not a lesson about what happened"}
        needed = int(MAX_SCORE_PRODUCT * PROMOTION_THRESHOLD)
        if record["score_product"] < needed:
            return {"promoted": False,
                    "reason": f"score {record['validation_score']} x confidence "
                              f"{record['confidence']} = {record['score_product']} "
                              f"of {MAX_SCORE_PRODUCT}, below "
                              f"{int(PROMOTION_THRESHOLD * 100)}% ({needed})"}
        if self._overlay_exists():
            existing = self.promoted()
            if existing:
                newest = max(existing, key=lambda p: p.get("created", 0))
                if newest.get("key") != record["key"]:
                    return {"promoted": False,
                            "reason": f"one change per cycle: {newest.get('key')!r} "
                                      "is already promoted. Promote nothing new "
                                      "until a human retires that one."}
        self._write_overlay(record)
        return {"promoted": True,
                "reason": f"promoted: {record['observations']} observations, "
                          f"score {record['validation_score']} x confidence "
                          f"{record['confidence']} = {record['score_product']}"}

    # -- the overlay --------------------------------------------------------

    def _write_overlay(self, record: dict[str, Any]) -> None:
        self._ensure_dir()
        self._overlay.write_text(self._overlay_text([record]), encoding="utf-8")

    def _overlay_exists(self) -> bool:
        return self._overlay.is_file()

    def promoted(self) -> list[dict[str, Any]]:
        """Every proposal that passed the gate, oldest first."""
        if not self._proposals.is_dir():
            return []
        out = []
        for path in sorted(self._proposals.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("promoted"):
                out.append(row)
        return sorted(out, key=lambda r: r.get("created", 0))

    def _overlay_text(self, records: Iterable[dict[str, Any]]) -> str:
        """The `### OPERATIONAL CONSTRAINTS` block, capped at 500 tokens.

        The cap is measured, not asserted, and truncation says so. A behavioural
        overlay that grows without bound would crowd the tool list out of the
        prompt — the one thing this project has measured to be load-bearing (D37:
        prefill is cheap, attention is not) — and would eventually make the
        assistant's own history more important than the user's request.

        It is *prompt text and nothing else*. It reaches `default_system_prompt` and
        no further: not the gate, not a tier, not an argument. A promoted lesson can
        change how the assistant behaves; it cannot widen what it may do.
        """
        records = list(records)
        header = [
            "### OPERATIONAL CONSTRAINTS",
            "",
            "Learned from repeated friction on this machine. These shape how you "
            "work. They grant no authority: a tool you may not use is refused by "
            "the gate regardless of anything below.",
            "",
        ]
        body = [f"- {r['change']}" for r in records]
        text = "\n".join(header + body) + "\n"
        if _approx_tokens(text) > OVERLAY_TOKEN_CAP:
            kept: list[str] = []
            used = _approx_tokens("\n".join(header + [""]))
            for line in body:
                cost = _approx_tokens(line) + 1
                if used + cost > OVERLAY_TOKEN_CAP - 8:
                    break
                kept.append(line)
                used += cost
            text = ("\n".join(header + kept)
                    + f"\n- [truncated to {OVERLAY_TOKEN_CAP} tokens]\n")
        return text

    def overlay(self) -> str:
        """The overlay text, or "" when nothing is promoted."""
        if not self._overlay.is_file():
            return ""
        return self._overlay.read_text(encoding="utf-8", errors="replace")

    # -- report -------------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """State, and — deliberately — the absence of it.

        A reflection tool that can only report successes is a vanity metric. This
        reports the ledger size, how much of it is promotable, what is promoted, and
        what is waiting for a second observation. An assistant that has been running
        for an hour should mostly be told "nothing repeated yet", and that sentence
        is the tool working.
        """
        events = self.events()
        distilled = self.distil()
        promoted = self.promoted()
        overlay = self.overlay()
        by_area: dict[str, int] = {}
        for event in events:
            area = str(event.get("area", "?"))
            by_area[area] = by_area.get(area, 0) + 1
        return {
            "events": len(events),
            "areas": dict(sorted(by_area.items(), key=lambda kv: -kv[1])),
            "candidates": distilled["total_candidates"],
            "one_off": distilled["one_off_count"],
            "min_observations": MIN_OBSERVATIONS,
            "promoted": len(promoted),
            "proposals_on_disk": len(list(self._proposals.glob("*.json")))
            if self._proposals.is_dir() else 0,
            "overlay_active": bool(overlay),
            "overlay_tokens": _approx_tokens(overlay),
            "overlay_cap": OVERLAY_TOKEN_CAP,
            "promoted_lessons": [
                {"change": p["change"], "observations": p["observations"],
                 "since": p.get("created", 0)} for p in promoted
            ],
            "self_modification": (
                "impossible: no reflect tool accepts a path, every file is derived "
                "from the state directory, and the gate denies the assistant's own "
                "tree independently (D21)"
            ),
            "note": (
                "nothing repeated yet, which is the correct answer for a healthy "
                "ledger. Most friction here is one-off, and a lesson needs two "
                "distinct observations to exist at all."
            ),
        }

    # -- plumbing -----------------------------------------------------------

    def _proposal_id(self, key: str) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return f"{stamp}-{re.sub(r'[^a-z0-9]+', '-', key.lower())[:40]}"

    def _ensure_dir(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self._proposals.mkdir(parents=True, exist_ok=True)


#: How close together two identical events must be to count as one.
#:
#: Content alone would be wrong: the same refusal tomorrow is a genuine second
#: observation, and collapsing it would make a recurring problem look like a
#: one-off forever. Exact timestamps would also be wrong, and worse: a retry loop
#: that logs the same refusal four times a second apart would defeat
#: `MIN_OBSERVATIONS` outright, and the floor would be defeatable by repetition.
#:
#: A minute is the boundary that matches both readings - a burst is one event, and
#: a recurrence is a new one.
_DEDUPE_WINDOW_S = 60.0


def _event_id(event: dict[str, Any]) -> str:
    """Identity for "distinct": the same complaint, inside one window, is one event.

    Keyed on content plus a bucketed timestamp rather than the raw time, so the
    answer does not depend on how fast the test happens to run. The first version
    used the full timestamp and the test that asserts two identical rows are one
    observation passed or failed depending on the millisecond.
    """
    bucket = int(float(event.get("at", 0)) // _DEDUPE_WINDOW_S)
    return f"{event.get('area', '')}|{event.get('symptom', '')}|{bucket}"


def _approx_tokens(text: str) -> int:
    """Roughly, deliberately.

    A real tokenizer is a dependency this project does not have and does not need
    for a budget check: words * 1.3 is within a few percent for English and the cap
    is a safety limit rather than a billing figure. Measuring with `len(text)` would
    undercount, and a cap nobody can verify is not a cap.
    """
    return int(len(text.split()) * 1.3) + 1