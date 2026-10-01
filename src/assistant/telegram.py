"""The Telegram bridge. Long-poll only, and the only thing here that talks to a network.

What this is
------------
The last piece of the "reachable by voice, PC prompt, or Telegram" requirement.
The daemon long-polls `getUpdates` and turns each allowed message into a
`daemon.handle()` call. That is the whole design: a transport that forwards text,
and no authority of its own.

Three rules, and why each is here rather than assumed
-----------------------------------------------------
**Nothing listens; exactly one component makes outbound calls, to one hardcoded
host.** D3 said "no TCP anywhere", and that is still true of how our components
talk to each other - named pipes, never sockets. Telegram is a different thing
and pretending otherwise would be dishonest: talking to a bot API requires an
outbound HTTPS request. So the rule is narrowed and stated precisely rather than
quietly broken. `_api()` refuses any host that is not the one constant, so a URL
assembled from a message can never send the token somewhere else.

**The allowlist is silent.** A chat id that is not in `state/telegram.json` gets
no reply at all - not a polite refusal, because that confirms the bot exists and
echoes back that the chat id was recognised. Silence is the whole of the answer.

**Telegram may ask; it may never authorise.** D13 rule 2 and D32 rule 5: a T3
arriving here becomes a *queued* confirmation, and the only thing that can approve
it is a foreground dialog at the PC. `/approve` exists and refuses, on purpose -
a command that was never implemented is indistinguishable from one that was
removed, and the user is entitled to know this one was considered and declined.

A note on presence
------------------
A message from Telegram does **not** assert that the user is at the keyboard. The
commonest reason to be away from the PC is not sleep - it is coffee, with the
phone in your pocket and the PC unattended. Treating a phone message as presence
would silently unlock T2 and T3 while nobody is watching the screen, which is the
exact failure D30 exists to prevent. So T0 and T1 still execute from the coffee
shop, and T2 and T3 queue until you are back.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

__all__ = ["TelegramConfig", "TelegramBridge", "TelegramError", "load_config"]

API_HOST = "api.telegram.org"
# Long-poll: 25 s server-side wait, 30 s client timeout. Under 60 s or Telegram
# treats the poller as gone.
LONG_POLL_S = 25
HTTP_TIMEOUT_S = 35.0

# Backoff for a network that is not there. The daemon is meant to run for weeks,
# so a failed poll must never end the thread - that is the difference between
# "the bot was offline for a minute" and "the bot silently died at 3am".
BACKOFF_START_S = 2.0
BACKOFF_MAX_S = 300.0

# The floor between polls, however quickly the server answered.
#
# Long-polling assumes the server holds the request for `timeout` seconds and
# that nothing comes back before then. That assumption holds for a working
# Telegram and for nothing else: a misbehaving proxy, a captive network or a
# rate limiter can return an empty result instantly, and then the loop is a
# `while True` with no sleep in it - one core, pegged, and the daemon competing
# with itself for the machine it is supposed to be serving.
#
# Found by the test suite hanging, which is the only symptom this has.
MIN_POLL_GAP_S = 1.0


class TelegramError(RuntimeError):
    """Configuration is missing or unusable."""


class TelegramConfig:
    """The token and the allowlist, read once and held as data.

    The token is deliberately not exposed by `describe()`, and `__repr__` is
    overridden so it cannot land in a traceback or a log line by accident. A
    token in a log is a token in a file somebody will later paste into a chat.
    """

    def __init__(self, token: str, allowed_chat_ids: tuple[int, ...]) -> None:
        if not token or ":" not in token:
            raise TelegramError("telegram_bot_token is missing or malformed")
        if not allowed_chat_ids:
            # An empty allowlist would mean "everyone", if the check were ever
            # skipped. Refuse it at construction instead.
            raise TelegramError(
                "telegram_allowed_chat_ids is empty - that would authorise nobody "
                "correctly and everybody if the check were ever bypassed"
            )
        self._token = token
        self.allowed_chat_ids = tuple(int(c) for c in allowed_chat_ids)

    def allows(self, chat_id: int | None) -> bool:
        return chat_id is not None and int(chat_id) in self.allowed_chat_ids

    def describe(self) -> dict[str, Any]:
        """Everything except the token. This is what `status` is allowed to show."""
        return {
            "api_host": API_HOST,
            "allowed_chat_ids": list(self.allowed_chat_ids),
            "long_poll_s": LONG_POLL_S,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"<TelegramConfig {len(self.allowed_chat_ids)} chat id(s), token hidden>"


def load_config(path: Path) -> TelegramConfig:
    """Read `state/telegram.json`. Raises rather than defaulting.

    A bridge that starts with no allowlist and quietly ignores everything looks
    identical to a working bridge with nobody talking to it, and the first is a
    bug while the second is a Tuesday.
    """
    if not path.exists():
        raise TelegramError(f"no telegram configuration at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        token = data["telegram_bot_token"]
        ids = data["telegram_allowed_chat_ids"]
    except KeyError as exc:
        raise TelegramError(f"{path} is missing {exc.args[0]!r}") from None
    return TelegramConfig(token, tuple(ids))


class TelegramBridge:
    """Long-polls Telegram and forwards allowed messages into the daemon.

    `call` is the daemon's `handle` shape - `call(op, **args) -> dict` returning
    `{ok, result}`. It is injected rather than constructed from a Daemon so the
    whole module is testable with no network and no daemon, and so that D31 is
    structural: there is no other attribute this class holds to reach anything.
    """

    def __init__(
        self,
        call: Callable[..., dict],
        config: TelegramConfig,
        *,
        transport: Callable[[str, dict | None, float], dict] | None = None,
    ) -> None:
        self._call = call
        self.config = config
        # The HTTP call is injectable so tests can drive every branch - 409, 401,
        # a timeout, a malformed body - without a network. A fake that only
        # handles the happy path would leave the interesting failures untested,
        # and the interesting failures are what happens at 3am.
        self._transport = transport or self._http_post
        self.offset: int | None = None
        self._offset_lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.polls = 0
        self.received = 0
        self.forwarded = 0
        self.refused_unknown = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._halt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="telegram", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "polls": self.polls,
            "received": self.received,
            "forwarded": self.forwarded,
            "refused_unknown_chat": self.refused_unknown,
            "offset": self.offset,
            "last_error": self.last_error,
            **self.config.describe(),
        }

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        """Poll forever, and never let an exception end the thread.

        The last part is the important one. A poller that dies on a transient
        network error is a bridge that works perfectly right up until the moment
        it is needed, and the user finds out from a person who needed something.
        """
        backoff = BACKOFF_START_S
        while not self._halt.is_set():
            started = time.monotonic()
            try:
                updates = self._poll_once()
                self.last_error = None
                backoff = BACKOFF_START_S
                for update in updates or []:
                    if self._halt.is_set():
                        return
                    self._handle_update(update)
                # Rate floor, on the success path too. See MIN_POLL_GAP_S.
                gap = MIN_POLL_GAP_S - (time.monotonic() - started)
                if gap > 0 and self._halt.wait(gap):
                    return
            except _RestartPoll:
                continue
            except Exception as exc:  # noqa: BLE001 - deliberate, see above
                self.last_error = f"{type(exc).__name__}: {exc}"
                # A daemon that cannot reach Telegram should still be up, and
                # still serving the PC and the hotkey.
                if self._halt.wait(backoff):
                    return
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _poll_once(self) -> list[dict]:
        self.polls += 1
        args: dict[str, Any] = {"timeout": LONG_POLL_S}
        with self._offset_lock:
            if self.offset is not None:
                args["offset"] = self.offset
        result = self._transport("getUpdates", args, HTTP_TIMEOUT_S)
        updates = result.get("result") or []
        if updates:
            with self._offset_lock:
                last = max(int(u.get("update_id", 0)) for u in updates)
                self.offset = last + 1
        return updates

    # -- message handling --------------------------------------------------

    def _handle_update(self, update: dict) -> None:
        self.received += 1
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return  # a reaction, a poll vote, a join notice
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not self.config.allows(chat_id):
            # Silent. See the module docstring.
            self.refused_unknown += 1
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        self.forwarded += 1
        self._on_text(int(chat_id), text, message.get("message_id"))

    def _on_text(self, chat_id: int, text: str, message_id: int | None) -> None:
        """Route one allowed message. Commands first, everything else is a prompt."""
        command = text.split()[0].lower() if text.startswith("/") else ""
        if command:
            reply = self._command(chat_id, command, text)
            if reply:
                self.say(chat_id, reply)
            return
        reply = self._ask_brain(chat_id, text)
        if reply:
            self.say(chat_id, reply)

    # -- commands ----------------------------------------------------------

    def _command(self, chat_id: int, command: str, text: str) -> str | None:
        # Telegram appends "@yourbotname" to commands in a group. Not relevant to
        # a private chat with an allowlist of one, but stripping it costs nothing.
        command = command.split("@", 1)[0]
        rest = text.partition(" ")[2].strip()

        if command in ("/start", "/help"):
            return (
                "Local assistant. Everything runs on your own machine.\n\n"
                "/status - is the daemon up\n"
                "/pending - actions waiting for you at the PC\n"
                "/deny <id> - refuse a waiting action\n"
                "/approve <id> - not available here, by design\n\n"
                "Anything else is sent to the assistant."
            )
        if command == "/status":
            res = self._call("status", channel="telegram", actor="telegram")
            body = (res or {}).get("result", {}) or {}
            return (
                f"daemon up {round(float(body.get('uptime_s', 0)))}s\n"
                f"attended: {body.get('attended')}\n"
                f"brain running: {(body.get('brain') or {}).get('running')}\n"
                f"vram: {body.get('vram')}"
            )
        if command == "/pending":
            res = self._call("confirm_list", channel="telegram", actor="telegram")
            pending = ((res or {}).get("result", {}) or {}).get("pending", [])
            if not pending:
                return "Nothing is waiting."
            lines = ["Waiting for you at the PC:"]
            for entry in pending:
                lines.append(
                    f"  {entry.get('id')}  {entry.get('tool')}"
                )
                reason = (entry.get("reason") or "").strip()
                if reason:
                    lines.append(f"      {reason}")
            lines.append("")
            lines.append("Refuse one with /deny <id>.")
            return "\n".join(lines)
        if command == "/deny":
            return self._deny(chat_id, rest)
        if command == "/approve":
            # Deliberate, and tested. See the module docstring.
            self._call(
                "audit_note", channel="telegram", actor="telegram",
                reason="telegram tried to approve a confirmation; refused by D32",
            )
            return (
                "I cannot approve that from here.\n\n"
                "An irreversible action is approved by a person looking at the "
                "dialog on the PC. Not me, not a message, not a link."
            )
        return "I do not know that command. /help for the list."

    def _deny(self, chat_id: int, arg: str) -> str:
        if not arg:
            return "Which one? /pending lists the ids."
        res = self._call(
            "confirm_deny", confirmation_id=arg.strip(),
            channel="telegram", actor="telegram",
        )
        body = (res or {}).get("result", {}) or {}
        if (res or {}).get("ok") and body.get("was_pending"):
            return f"Refused {arg.strip()}."
        return f"Nothing was pending as {arg.strip()!r}."

    # -- the assistant -----------------------------------------------------

    def _ask_brain(self, chat_id: int, text: str) -> str | None:
        res = self._call(
            "brain_turn", prompt=text, channel="telegram", actor="telegram"
        )
        if not (res or {}).get("ok"):
            return f"That did not work: {(res or {}).get('error')}"
        body = (res or {}).get("result", {}) or {}
        kind = body.get("kind")
        if kind == "answer":
            return body.get("text") or ""
        if kind == "tool":
            tool = body.get("tool")
            if body.get("parse_error"):
                return "I tried to call a tool and the call did not parse."
            confirmation = body.get("confirmation_id")
            if confirmation:
                return (
                    f"I want to run {tool}.\n\n"
                    f"It is waiting as {confirmation}. I will not run it from here - "
                    f"approve it at the PC, or /deny {confirmation}."
                )
            return (
                f"I would run {tool}, but it needs a confirmation that has to be "
                f"approved at the PC. Ask me again when you are there."
            )
        return "I did not understand that."

    # -- talking back ------------------------------------------------------

    def say(self, chat_id: int, text: str, *, reply_to: int | None = None) -> bool:
        """Send one message. A reply to the message you just sent.

        This is transport, not `telegram.send`. The registered tool is for a
        message the assistant *initiates* - a reminder, an alert, a result nobody
        asked for - which is why it is T3 and external. Answering a message that
        arrived a second ago has already been authorised by its sender.
        """
        if not text.strip():
            return False
        args: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4096],  # Telegram's own hard limit
        }
        if reply_to is not None:
            args["reply_to_message_id"] = reply_to
        try:
            self._transport("sendMessage", args, HTTP_TIMEOUT_S)
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"send failed: {type(exc).__name__}: {exc}"
            return False

    # -- the only network call in the project -------------------------------

    def _api(self, method: str) -> str:
        url = f"https://{API_HOST}/bot{self.config._token}/{method}"
        return url

    def _http_post(self, method: str, args: dict | None,
                   timeout: float) -> dict:
        body = json.dumps(args or {}).encode("utf-8")
        req = urllib.request.Request(
            self._api(method),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # No custom ssl context: the default verifies the certificate and the
        # hostname. A context that skipped verification would put the bot token
        # on the wire to anything that answered, and the token is the whole of
        # the access control.
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 409:
                # Another getUpdates is in flight. Not fatal: back off and retry,
                # and say so, because two pollers means two copies of every
                # message.
                raise _RestartPoll(
                    "another poller holds the cursor (409); retrying"
                ) from None
            if exc.code == 401:
                raise TelegramError(
                    "Telegram rejected the bot token (401). It is not usable."
                ) from None
            raise TelegramError(f"Telegram HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise ConnectionError(f"cannot reach Telegram: {exc.reason}") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise TelegramError(f"Telegram returned non-JSON: {raw[:120]}") from None
        if not payload.get("ok"):
            raise TelegramError(
                f"Telegram said not-ok: {payload.get('description', 'no reason')}"
            )
        return payload


class _RestartPoll(Exception):
    """409: someone else holds the cursor. Retry immediately, do not back off."""