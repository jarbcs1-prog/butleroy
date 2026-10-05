"""Teaching: a ledger of what was taught, and the prompts that teach it.

Separate from `brain.py`, which knows how to talk to a model, and from
`daemon.py`, which owns the brain. This module knows three things and nothing
else: what counts as a topic, what has been taught so far, and what to say to a
model to make it teach. It never calls a model, never reads the network, and
never touches VRAM. The daemon streams; this decides what to stream.

**The ledger is the whole reason pedagogy is a toolset and not six prompts.**
`teach.progress` cannot report coverage from nothing, `teach.recall` has nothing
to test, and `teach.quiz` does not know what was taught. So every teaching turn
appends a row, and the reads answer from the rows rather than from the model.
That is also what makes the last section of this file possible.

**Coverage is not learning, and the ledger is where that difference is kept.**
`progress()` reports three counts per topic - explained, quizzed, recalled - and
then reports the topics that have been explained and never tested. A count of
explanations is a count of things said; a count of recalls answered is a count of
things retrieved. Only the second is evidence, and a tool that reports the first
under either name is measuring the wrong unit (D55 one level down).

**Five of the six teaching tools are T2, not T0, and the reason is in the code
that consumes them.** `tools.py` originally declared all six as `Tier.READ`,
which would have meant to a human that calling them is free. Four of them cannot
be free: the explanation, the quiz, the summary and the curriculum are *prose*,
and the brain produces prose only through `Brain.stream()`, which needs the child
model resident. A T0 label on a call that loads five gibibytes of VRAM is the
same lie as a wake word that scores zero and is reported as "not tested" - a
label that is right about the words and wrong about the cost (D50). `teach.progress`
is the only one that stays T0, because it is the only one that answers from the
ledger alone.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "LEDGER_NAME",
    "LEDGER_MAX_ROWS",
    "TEXT_CAP",
    "TOPIC_MAX",
    "KINDS",
    "STYLES",
    "KIND_EXPLAIN",
    "KIND_QUIZ",
    "KIND_RECALL",
    "KIND_SUMMARY",
    "KIND_CURRICULUM",
    "DEFAULT_STYLE",
    "topic_refusal",
    "question_refusal",
    "style_refusal",
    "source_refusal",
    "Ledger",
    "build_prompt",
]


# -- the shape of the subject -------------------------------------------------

LEDGER_NAME = "teaching.jsonl"
TEXT_CAP = 2000          # per row; a teaching turn is prose and prose is long
TOPIC_MAX = 200          # a topic, not an essay
LEDGER_MAX_ROWS = 500    # a running assistant is read daily and written often

KIND_EXPLAIN = "explain"
KIND_QUIZ = "quiz"
KIND_RECALL = "recall"
KIND_SUMMARY = "summarise"
KIND_CURRICULUM = "curriculum"

#: The kinds, in the order `progress` reports them.
KINDS: tuple[str, ...] = (KIND_EXPLAIN, KIND_QUIZ, KIND_RECALL,
                          KIND_SUMMARY, KIND_CURRICULUM)

#: Only the two styles, and Socratic is the default because the spec says so.
STYLES: tuple[str, ...] = ("socratic", "direct")
DEFAULT_STYLE = "socratic"

#: The prompt that makes "Socratic" mean something specific rather than a mood.
_SOCRATIC_RULES = (
    "Teach Socratically. Ask the user one question at a time and wait for the "
    "answer. Do not give the answer, not even as a hint, not even after the "
    "user says they do not know. If they are stuck for two turns, narrow the "
    "question rather than ending it. The only exception is a direct factual "
    "question, which you answer once and then offer to go deeper."
)


def _blank(raw: Any) -> bool:
    """Whether a value is missing, empty, or only whitespace.

    One definition, because three call sites each testing this differently is
    three chances for one of them to accept a string of spaces as a topic and
    hand it to a model as a subject.
    """
    return raw is None or not str(raw).strip()


def topic_refusal(raw: Any) -> str | None:
    """Why this cannot be a topic, or None if it can.

    Returns rather than raises, for the same reason every other refusal in this
    project returns: `_preview_for` has no exception handling, so a preview that
    raises takes the whole confirmation down with it (D59).
    """
    if _blank(raw):
        return ("a topic is required - the thing being taught, named in a few "
                "words. For example \"python generators\".")
    text = str(raw).strip()
    if len(text) > TOPIC_MAX:
        return (f"that topic is {len(text)} characters long, and the limit is "
                f"{TOPIC_MAX}. A topic is a name, not the material - the "
                "material goes in `source`, or the question goes in `question`.")
    return None


def question_refusal(raw: Any) -> str | None:
    """Why this cannot be a question to put to the user."""
    if _blank(raw):
        return ("a question is required here - this tool tests what the user "
                "can recall, and there is nothing to recall from.")
    text = str(raw).strip()
    if len(text) > TOPIC_MAX:
        return (f"that question is {len(text)} characters long, and the limit "
                f"is {TOPIC_MAX}.")
    return None


def style_refusal(raw: Any) -> str | None:
    """Why this is not a teaching style. `None` means take the default."""
    if _blank(raw):
        return None
    if str(raw).strip().lower() not in STYLES:
        return (f"{str(raw).strip()!r} is not a teaching style. Use "
                f"{' or '.join(repr(s) for s in STYLES)} - 'socratic' asks the "
                "user questions instead of handing over the answer, 'direct' "
                "does the latter.")
    return None


def source_refusal(raw: Any) -> str | None:
    """Why this cannot be the material to condense.

    The path itself is not judged here. That is the gate's job, and it is the
    gate's job because the gate holds the approved roots and this module holds
    no state about the machine; a second opinion about what may be read is a
    second thing that can be wrong (D61).
    """
    if _blank(raw):
        return ("point at something to condense - `source` for a file inside an "
                "approved workspace, or `text` for something pasted in.")
    return None


# -- the ledger ---------------------------------------------------------------


class Ledger:
    """What has been taught, as an append-only file of rows.

    Append-only on purpose. A rewrite would be tidier and would destroy the one
    property that makes this useful when something goes wrong: the file is
    readable by a person with notepad, in order, after the fact. The cap is
    enforced by refusing a new row rather than by deleting old ones - a tool
    that quietly drops the oldest teaching records to make room is a tool that
    can make `teach.progress` wrong without saying so.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # -- reading --

    def rows(self) -> list[dict[str, Any]]:
        """Every recorded teaching turn, oldest first.

        A malformed line is skipped rather than raised on. This file is written
        only by `record`, so a bad line means a crash mid-append or a person
        editing it; either way the reads that matter most - "what have I
        covered" - are better served by nine good rows than by an exception on
        the tenth.
        """
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def topics(self) -> list[str]:
        """Distinct topics taught, in first-seen order."""
        seen: list[str] = []
        for row in self.rows():
            topic = str(row.get("topic") or "").strip()
            if topic and topic not in seen:
                seen.append(topic)
        return seen

    def record(self, kind: str, topic: str, text: str, **extra: Any) -> dict[str, Any]:
        """Append one teaching turn. Returns the row as stored.

        The refusal is here rather than at the call site because the caller is a
        model turn that has already been paid for; finding out afterwards that
        the ledger was full means the work was done and thrown away.
        """
        body = str(text or "")
        row: dict[str, Any] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "topic": str(topic),
            "text": body[:TEXT_CAP],
            "chars": len(body),
            "truncated": len(body) > TEXT_CAP,
        }
        row.update(extra)
        existing = self.rows()
        if len(existing) >= LEDGER_MAX_ROWS:
            raise ValueError(
                f"the teaching ledger holds {LEDGER_MAX_ROWS} entries and will "
                f"not take another. It is {self.path}, in plain text, and it "
                "is yours to clear - nothing here deletes teaching history "
                "without being asked to.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # -- the read that is the point --

    def progress(self, *, limit: int = 50) -> dict[str, Any]:
        """What has been covered, and - the part that matters - what has not
        been tested.

        Three counts per topic, deliberately kept apart: `explained` is turns
        where something was said, `quizzed` and `recalled` are turns where
        something was asked for back. A topic that has only ever been explained
        is listed under `explained_never_tested`, because that is the state
        pedagogy exists to notice and a total count cannot show it.
        """
        rows = self.rows()
        by_topic: dict[str, dict[str, Any]] = {}
        for row in rows:
            topic = str(row.get("topic") or "").strip()
            if not topic:
                continue
            # The counter names come from KINDS, not from a literal list written
            # out here. They disagreed - this said `explained`, KINDS says
            # `explain` - and because a missing key reads as zero and a zero is
            # falsy, `explained_never_tested` came back empty for every topic,
            # every time. The one thing this report exists to say, silently
            # reporting the opposite. Two copies of a list of names, and only
            # one of them was true.
            entry = by_topic.setdefault(topic, dict.fromkeys(KINDS, 0))
            entry.update({"topic": topic, "kinds": [], "last_at": "",
                          "questions_asked": 0})
            kind = str(row.get("kind") or "")
            if kind in KINDS:
                entry[kind] = entry.get(kind, 0) + 1
                if kind not in entry["kinds"]:
                    entry["kinds"].append(kind)
            if row.get("question"):
                entry["questions_asked"] += 1
            at = str(row.get("at") or "")
            if at > entry["last_at"]:
                entry["last_at"] = at

        entries = list(by_topic.values())
        entries.sort(key=lambda e: e["last_at"], reverse=True)
        untested = [e["topic"] for e in entries
                    if e[KIND_EXPLAIN]
                    and not (e[KIND_QUIZ] or e[KIND_RECALL])]

        return {
            "recorded": len(rows),
            "topics": len(entries),
            "by_kind": {k: sum(1 for r in rows if r.get("kind") == k)
                        for k in KINDS},
            "shown": entries[:limit],
            "shown_count": len(entries[:limit]),
            "truncated": len(entries) > limit,
            "explained_never_tested": untested[:limit],
            "explained_never_tested_count": len(untested),
            "ledger": str(self.path),
            "note": (
                "Explained is not learned. A topic that has been explained and "
                "never quizzed or recalled appears under "
                "explained_never_tested, and asking about it is what this tool "
                "is for."
            ),
        }


# -- what to say to a model ---------------------------------------------------


def build_prompt(kind: str, *, topic: str, question: str = "",
                 style: str = DEFAULT_STYLE, ledger: Ledger | None = None,
                 source_text: str = "", limit: int = 6) -> str:
    """The prompt for one teaching turn.

    Built here rather than at each call site so that "Socratic" means one
    specific set of rules instead of five paraphrases of the same adjective. The
    ledger is consulted for the recent history so a model does not re-explain
    what was explained five minutes ago, which is the failure that makes a
    teacher feel like it is not listening.
    """
    style = (style or DEFAULT_STYLE).strip().lower()
    if style not in STYLES:
        style = DEFAULT_STYLE

    parts: list[str] = []
    if kind == KIND_EXPLAIN:
        parts.append(f"The user wants to understand: {topic}.")
        if style == "socratic":
            parts.append(_SOCRATIC_RULES)
        else:
            parts.append("Explain it plainly and completely, then ask one "
                         "question to check it landed.")
    elif kind == KIND_QUIZ:
        parts.append(f"Write up to {limit} questions that test understanding of "
                     f"{topic}. Ask one at a time and wait. Do not include the "
                     "answers in the question itself.")
    elif kind == KIND_RECALL:
        parts.append(f"Ask the user this question about {topic}, verbatim: "
                     f"{question}")
        parts.append("Wait for their answer. Judge it only when they give one, "
                     "and say what they got right before what they got wrong.")
    elif kind == KIND_SUMMARY:
        parts.append(f"Condense this material, which is about {topic}, into "
                     "what someone needs to remember. Keep the specifics; drop "
                     "the preamble.")
        if source_text:
            parts.append("Material:\n" + source_text.strip())
    elif kind == KIND_CURRICULUM:
        parts.append(f"Plan a learning sequence for {topic}. Order the steps so "
                     "each one uses what the step before it taught, name what "
                     "each step is for, and say what would show the step landed.")
    else:
        parts.append(f"Help the user with: {topic}.")

    if ledger is not None and kind in (KIND_EXPLAIN, KIND_QUIZ, KIND_RECALL,
                                       KIND_CURRICULUM):
        recent = [r for r in ledger.rows() if str(r.get("topic") or "") == topic]
        if recent:
            seen = ", ".join(f"{r.get('kind')} at {r.get('at')}" for r in recent[-limit:])
            parts.append(f"This has already come up for {topic}: {seen}. Do not "
                         "start from the beginning without saying why.")

    return "\n\n".join(parts)
