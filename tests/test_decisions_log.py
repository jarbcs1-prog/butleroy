"""DECISIONS.md is a claim this project keeps making, and keeps not checking.

HANDOFF.md §3 says `decisions D1..D73, contiguous, none missing`. That line was
written by hand, updated by hand, and was **wrong** the moment D75 was appended with
D74 missing - which is how it was found: the heading list came back D70 D71 D72 D73
D75, with a gap, and no test failed.

This is the stale-test-count lesson again (see the D73 commit): a figure in a
document is a claim about a moment, and nothing tells you when the moment passed.
A file that is written by hand and read by eye is a file that drifts.

So the claim is checked. Cheaply, and by parsing the headings rather than counting
lines, because the numbers are what matter and the prose is not.

Two things are asserted:

  * the numbering is contiguous from D1, so a gap is a gap;
  * nothing is referenced in the source that does not exist, so a module cannot cite
    a decision number that was never written.

The second one is the more useful. `modelconfig.py` opened with "D74" and cited it as
its own provenance while no D74 existed - a source file pointing at a decision that
is not there is a reader sent to look for something that was never written.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "DECISIONS.md"

#: A decision heading, in either of the two forms the file actually uses.
#:
#: Both heading levels (D1-D44 are `###`, the recent ones are `##`) and both dashes
#: (some headings use an en dash `–` where others use a hyphen `-`). Both facts were
#: found by the test failing for the wrong reason: the first regex matched only `##`
#: and only `-`, so it found 31 of 75 decisions and reported D1 through D44 as
#: dangling citations - while *passing* the citation test, because `known` was
#: nearly empty and so nothing looked missing.
#:
#: Worth recording because a check that is too narrow does not fail loudly. It fails
#: *confidently*, about the wrong thing, and a green test is the most reassuring
#: possible wrong answer.
_HEADING = re.compile(r"^#{2,4}\s+(D(\d+))\s+[-\u2013\u2014]\s+(.+)$", re.MULTILINE)


def _headings() -> list[tuple[str, int, str]]:
    text = DECISIONS.read_text(encoding="utf-8")
    return [(m.group(1), int(m.group(2)), m.group(3).strip())
            for m in _HEADING.finditer(text)]


def test_the_decision_numbers_are_contiguous_and_start_at_one() -> None:
    """No gaps, no duplicates.

    A duplicate is nearly as bad as a gap: `## D75` appearing twice means the second
    one silently replaced the first, and everything that cited it is now citing
    something different from what its author read.
    """
    numbers = [n for _, n, _ in _headings()]

    assert numbers, "no decisions found; has the heading format changed?"
    assert numbers[0] == 1, f"the log starts at D{numbers[0]}, not D1"

    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"decision numbers used twice: D{duplicates}"

    missing = sorted(set(range(1, max(numbers) + 1)) - set(numbers))
    assert not missing, (
        f"D{missing} missing between D1 and D{max(numbers)}; the log claims to be "
        "contiguous in HANDOFF.md §3"
    )

    # NOT asserting ascending order, and the reason is worth recording because the
    # first version of this file did assert it and failed.
    #
    # The log is organised into **Parts** (`## Part IV - What Phase 0 actually
    # proved`), not into a flat numbered sequence. D29 - "D25 is confirmed, and the
    # build must never be reverted to a wheel" - is the *opening* decision of Part
    # IV and refers back to D25, which sits at the end of Part III. Its position is
    # therefore correct and the numbers are not ascending.
    #
    # Requiring ascending order would have been one of two bad outcomes: move D29
    # out of the section it introduces, or delete a test that had found something
    # real. The assertion was wrong about the document, not the document wrong about
    # the assertion. What matters is contiguity and completeness, both checked above.


def test_every_decision_has_a_title() -> None:
    """A heading with an empty title is a note that lost its name."""
    untitled = [label for label, _, title in _headings() if not title.strip()]
    assert not untitled, f"untitled: {untitled}"


def test_no_source_file_cites_a_decision_that_does_not_exist() -> None:
    """The finding that prompted this file.

    `src/assistant/modelconfig.py` opened with "D74" as its own provenance, and no
    D74 existed - a source file sending a reader to look for something that was never
    written. Citing a decision is how this project explains *why* something is the
    way it is, so a dangling citation is worse than none: it looks authoritative.

    Scoped to `src/` and `tests/` rather than the docs, because the docs discuss
    decisions retrospectively and legitimately refer to ones by name in prose.
    """
    known = {n for _, n, _ in _headings()}

    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"\bD(\d{1,3})\b", text):
            number = int(m.group(1))
            # Not a real decision number at all - a false positive from something
            # like a hex constant or a test index. Bounded by the highest decision
            # plus a wide margin so it can only ever be "cites one that isn't there".
            if number in known or number > max(known) + 50:
                continue
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: D{number}")

    assert not offenders, (
        "these cite a decision number that is not in DECISIONS.md:\n  "
        + "\n  ".join(offenders)
    )


RECENT = _headings()[-3:]


@pytest.mark.parametrize("label,number,title", RECENT, ids=[h[0] for h in RECENT])
def test_the_recent_decisions_have_substance(label, number, title) -> None:
    """The last three are longer than a headline.

    A decision record that is two sentences long is a note. It is not wrong to be
    brief, but it is worth noticing when the recent ones shrink, because that is what
    happens when someone starts recording findings instead of reasoning.

    Deliberately only the most recent three, and deliberately a length check rather
    than a content check: this cannot judge whether the reasoning is good, and
    pretending to would be the D36 shape.
    """
    text = DECISIONS.read_text(encoding="utf-8")
    # Located by the number and the title together, because the heading's dash
    # character varies between the two conventions in the file and reconstructing
    # the exact marker string would reintroduce the bug this file was written for.
    start = text.index(f"{label} -") if f"{label} -" in text else text.index(f"{label} –")
    start = text.index(title, start) + len(title)
    rest = text[start:]
    next_heading = re.search(r"\n#{2,4}\s+D\d+", rest)
    body = rest if next_heading is None else rest[:next_heading.start()]

    assert len(body.strip()) > 400, (
        f"{label} is {len(body.strip())} characters. That is a note, not a "
        "decision record."
    )