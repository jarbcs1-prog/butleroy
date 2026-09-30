"""Tests for the policy gate.

These are the security-critical tests in the project. PLAN.md Phase 2 names
three specific gate claims, and each has a test here:

  * a T3 tool call from any channel prompts and executes nothing until
    confirmed at the PC;
  * an unattended session executes T0/T1 only;
  * and - implied by both - the assistant cannot touch its own code or policy.

Run with:  .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from assistant.policy import (  # noqa: E402
    CHANNEL_PC,
    CHANNEL_TELEGRAM,
    CHANNEL_VOICE,
    Decision,
    PolicyGate,
    Tier,
    ToolSpec,
)
from assistant.tools import ALL_TOOLS, default_gate  # noqa: E402


def make_gate(attended: bool = True,
              allowed_roots: list[str] | None = None) -> PolicyGate:
    """A gate with the full registry and one approved workspace."""
    gate = default_gate(allowed_roots=allowed_roots or [r"G:\workspace\project"])
    gate.attended = attended
    return gate


class TestUnknownToolsFailClosed(unittest.TestCase):
    def test_unknown_tool_is_denied(self):
        """A tool with no declaration is not a low-risk tool."""
        gate = make_gate()
        verdict = gate.evaluate("totally.made_up", channel=CHANNEL_PC)
        self.assertEqual(verdict.decision, Decision.DENY)
        self.assertIn("not registered", verdict.reason)

    def test_tier_comes_from_the_table_not_the_request(self):
        """A request cannot lower a tool's tier by asserting a lower one."""
        gate = make_gate()
        # No tier argument exists to abuse; verify via the lookup instead.
        self.assertEqual(gate.tier_of("file.delete"), Tier.IRREVERSIBLE)
        self.assertEqual(gate.tier_of("file.read"), Tier.READ)


class TestT3AlwaysConfirms(unittest.TestCase):
    """PLAN.md: 'a T3 tool call from any channel prompts and executes nothing'."""

    T3_TOOLS = [
        ("file.delete", {"path": r"G:\workspace\project\a.txt"}),
        ("file.empty_recycle_bin", {}),
        ("email.send", {"to": "someone@example.com", "subject": "hi"}),
        ("telegram.send", {"chat_id": 1, "text": "hi"}),
        ("package.install", {"name": "something"}),
        ("power.shutdown", {}),
    ]

    def test_t3_confirms_from_pc(self):
        gate = make_gate(attended=True)
        for tool, args in self.T3_TOOLS:
            with self.subTest(tool=tool, channel=CHANNEL_PC):
                v = gate.evaluate(tool, channel=CHANNEL_PC, args=args)
                self.assertEqual(v.decision, Decision.CONFIRM)
                self.assertEqual(v.requires_confirmation_at, CHANNEL_PC)
                self.assertIsNotNone(v.reason)

    def test_telegram_may_request_but_never_authorise_t3(self):
        """D13 rule 2. A hijacked Telegram session must not delete files."""
        gate = make_gate(attended=True)
        for tool, args in self.T3_TOOLS:
            with self.subTest(tool=tool, channel=CHANNEL_TELEGRAM):
                v = gate.evaluate(tool, channel=CHANNEL_TELEGRAM, args=args)
                self.assertNotEqual(v.decision, Decision.ALLOW)
                self.assertEqual(v.requires_confirmation_at, CHANNEL_PC)

    def test_t3_never_auto_executes_even_when_attended(self):
        """Being at the keyboard is not consent; confirmation is a separate act."""
        gate = make_gate(attended=True)
        v = gate.evaluate("file.delete", channel=CHANNEL_PC,
                          args={"path": r"G:\workspace\project\x"})
        self.assertEqual(v.decision, Decision.CONFIRM)


class TestUnattendedCap(unittest.TestCase):
    """PLAN.md: 'an unattended session executes T0/T1 only'."""

    def test_t0_and_t1_execute_unattended(self):
        gate = make_gate(attended=False)
        cases = [
            ("file.list", {"path": r"G:\workspace\project"}),
            ("file.read", {"path": r"G:\workspace\project\a.txt"}),
            ("file.create", {"path": r"G:\workspace\project\scratch.md"}),
            ("system.info", {}),
            ("teach.explain", {"topic": "recursion"}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool):
                v = gate.evaluate(tool, channel="scheduled", args=args)
                self.assertEqual(v.decision, Decision.ALLOW, v.reason)

    def test_t2_and_t3_queue_unattended(self):
        gate = make_gate(attended=False)
        cases = [
            ("file.move", {"path": r"G:\workspace\project\a",
                           "dest_path": r"G:\workspace\project\b"}),
            ("delegate.assign", {"task": "build the thing"}),
            ("shell.run", {"command": "pytest"}),
        ]
        for tool, args in cases:
            with self.subTest(tool=tool):
                v = gate.evaluate(tool, channel="scheduled", args=args)
                self.assertIn(v.decision, (Decision.QUEUE, Decision.CONFIRM))
                self.assertNotEqual(v.decision, Decision.ALLOW)

    def test_t2_prompts_when_attended(self):
        gate = make_gate(attended=True)
        v = gate.evaluate("file.archive",
                          channel=CHANNEL_PC,
                          args={"path": r"G:\workspace\project", "dest_path":
                                r"G:\workspace\project\out.zip"})
        self.assertEqual(v.decision, Decision.CONFIRM)


class TestPathProtection(unittest.TestCase):
    """D10 isolation and D21 self-protection."""

    def test_paths_outside_approved_roots_are_denied(self):
        """D10: the assistant has no built-in knowledge of any project."""
        gate = make_gate()
        v = gate.evaluate("file.read",
                          channel=CHANNEL_PC,
                          args={"path": r"G:\some-other-project\secrets.txt"})
        self.assertEqual(v.decision, Decision.DENY)
        self.assertIn("outside every approved workspace root", v.reason)

    def test_assistant_cannot_read_its_own_source(self):
        gate = make_gate()
        v = gate.evaluate("file.read", channel=CHANNEL_PC,
                          args={"path": r"G:\localaized\src\assistant\policy.py"})
        self.assertEqual(v.decision, Decision.DENY)
        self.assertIn("D21", v.reason)

    def test_assistant_cannot_edit_its_own_decisions(self):
        gate = make_gate()
        v = gate.evaluate("file.edit", channel=CHANNEL_PC,
                          args={"path": r"G:\localaized\DECISIONS.md"})
        self.assertEqual(v.decision, Decision.DENY)

    def test_dotdot_cannot_escape_an_allowed_root(self):
        """'G:\\workspace\\project\\..\\..\\other' must not read as allowed."""
        gate = make_gate()
        v = gate.evaluate("file.read", channel=CHANNEL_PC,
                          args={"path": r"G:\workspace\project\..\..\Windows\System32\config\SAM"})
        self.assertEqual(v.decision, Decision.DENY)

    def test_absolute_deny_paths_are_protected(self):
        gate = make_gate(allowed_roots=[r"C:\Windows"])
        v = gate.evaluate("file.list", channel=CHANNEL_PC,
                          args={"path": r"C:\Windows\System32\config"})
        self.assertEqual(v.decision, Decision.DENY)

    def test_approved_workspace_is_reachable(self):
        gate = make_gate()
        v = gate.evaluate("file.read", channel=CHANNEL_PC,
                          args={"path": r"G:\workspace\project\src\main.py"})
        self.assertEqual(v.decision, Decision.ALLOW, v.reason)


class TestRegistryInvariants(unittest.TestCase):
    """The import-time assertions in tools.py, asserted again as tests."""

    def test_every_destructive_or_external_tool_is_t3(self):
        for spec in ALL_TOOLS:
            if spec.destructive or spec.external:
                with self.subTest(tool=spec.name):
                    self.assertIs(spec.tier, Tier.IRREVERSIBLE)

    def test_no_t3_tool_is_innocuous(self):
        for spec in ALL_TOOLS:
            if spec.tier is Tier.IRREVERSIBLE:
                with self.subTest(tool=spec.name):
                    self.assertTrue(spec.destructive or spec.external)

    def test_tool_names_are_unique(self):
        names = [s.name for s in ALL_TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_registry_covers_every_capability_in_the_brief(self):
        """D15-D17 must all have a tool, or the design is fiction."""
        for required in ("system.diagnose",       # tech support
                         "file.delete",           # administration
                         "email.send",            # mail
                         "telegram.send",         # messaging
                         "teach.explain",         # pedagogy
                         "delegate.assign"):      # orchestration
            self.assertIn(required, {s.name for s in ALL_TOOLS})

    def test_no_tool_edits_code_in_a_target_project(self):
        """D16 is structural: there is no tool with this capability at all."""
        forbidden = ("code.edit", "code.write", "refactor.apply", "code.patch")
        names = {s.name for s in ALL_TOOLS}
        for name in forbidden:
            self.assertNotIn(name, names)


class TestMisdeclarationIsCaught(unittest.TestCase):
    """A T1 tool that lies about being harmless must still prompt."""

    def test_destructive_flag_overrides_a_low_tier(self):
        gate = PolicyGate(allowed_roots=[])
        gate.register(ToolSpec("sneaky.delete", Tier.READ, destructive=True))
        v = gate.evaluate("sneaky.delete", channel=CHANNEL_PC)
        self.assertEqual(v.decision, Decision.CONFIRM)
        self.assertIn("stricter classification", v.reason)

    def test_external_flag_overrides_a_low_tier(self):
        gate = PolicyGate(allowed_roots=[])
        gate.register(ToolSpec("sneaky.post", Tier.READ, external=True))
        v = gate.evaluate("sneaky.post", channel=CHANNEL_PC)
        self.assertEqual(v.decision, Decision.CONFIRM)


if __name__ == "__main__":
    unittest.main()
