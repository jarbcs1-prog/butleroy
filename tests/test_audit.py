"""Tests for the audit chain.

The chain's value is that "the log says X" is checkable. These tests therefore
attack it: edit a record, delete a record, reorder two, and check that
`verify` catches each.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from assistant.audit import AuditLog  # noqa: E402
from assistant.policy import CHANNEL_PC, CHANNEL_TELEGRAM  # noqa: E402
from assistant.tools import default_gate  # noqa: E402


class TestChainIntegrity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "audit.log"
        self.log = AuditLog(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _fill(self, n: int = 5) -> None:
        for i in range(n):
            self.log.append(
                actor="user",
                channel=CHANNEL_PC,
                tool="file.read",
                tier="T0",
                decision="allow",
                reason=f"case {i}",
                args={"path": f"G:\\workspace\\project\\{i}.txt"},
            )

    def test_empty_chain_verifies(self):
        result = self.log.verify()
        self.assertTrue(result.ok)
        self.assertEqual(result.checked, 0)

    def test_appends_verify(self):
        self._fill(5)
        result = self.log.verify()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.checked, 5)

    def test_editing_a_record_is_detected(self):
        self._fill(5)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[2])
        record["decision"] = "allow-forever"          # the attack
        lines[2] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = self.log.verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.first_bad_seq, 3)
        self.assertIn("hash mismatch", result.detail)

    def test_deleting_a_record_is_detected(self):
        self._fill(5)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        del lines[2]                                  # the attack
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = self.log.verify()
        self.assertFalse(result.ok)
        # The break surfaces at the *next surviving* record, not at the gap:
        # record 3 no longer exists to fail, so record 4 is the first one whose
        # prev_hash points at a link that is no longer there.
        self.assertEqual(result.first_bad_seq, 4)
        self.assertIn("broken link", result.detail)

    def test_reordering_is_detected(self):
        self._fill(5)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines[1], lines[3] = lines[3], lines[1]       # the attack
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = self.log.verify()
        self.assertFalse(result.ok)

    def test_restarting_appends_to_the_same_chain(self):
        self._fill(3)
        reopened = AuditLog(self.path)
        reopened.append(actor="user", channel=CHANNEL_PC, tool="file.read",
                        tier="T0", decision="allow", reason="after restart")
        result = reopened.verify()
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.checked, 4)

    def test_records_are_immutable(self):
        self._fill(1)
        record = next(iter(self.log.read()))
        with self.assertRaises(Exception):
            record.decision = "allow"  # type: ignore[misc]


class TestGateIsAudited(unittest.TestCase):
    """Every verdict must land in the log, including denials."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log = AuditLog(Path(self._tmp.name) / "audit.log")

    def tearDown(self):
        self._tmp.cleanup()

    def test_denials_and_approvals_are_both_recorded(self):
        gate = default_gate(allowed_roots=[r"G:\workspace\project"])

        for tool, args in (
            ("file.read", {"path": r"G:\workspace\project\a.txt"}),
            ("file.delete", {"path": r"G:\workspace\project\a.txt"}),
            ("file.read", {"path": r"G:\elsewhere\secret.txt"}),
        ):
            verdict = gate.evaluate(tool, channel=CHANNEL_TELEGRAM, args=args)
            self.log.append(
                actor="user",
                channel=CHANNEL_TELEGRAM,
                tool=tool,
                tier=str(verdict.tier),
                decision=verdict.decision,
                reason=verdict.reason,
                args=args,
            )

        decisions = [r.decision for r in self.log.read()]
        self.assertEqual(decisions, ["allow", "confirm", "deny"])
        self.assertTrue(self.log.verify().ok)


if __name__ == "__main__":
    unittest.main()
