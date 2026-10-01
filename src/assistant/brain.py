"""
Plane B: the local brain, as a disposable child process.

Everything about the shape of this module follows from three decisions that were
measured rather than assumed:

**D3 - one child process per model, killed on an idle TTL.** VRAM is only reliably
returned on Windows when a process dies. So this module is a *program*, not a
library that Plane A calls into. Plane A spawns it, it serves turns, and after 90 s
of silence it exits and the memory goes back. Nothing here holds a lease open or
caches a model between turns; that is the whole design.

**D1 - the lease is taken BEFORE the load, not after.** This ordering is the single
most important line in the file. If we load first and ask afterwards, we have
already taken the memory by the time we discover we were not allowed to, and the
only remedy left is to unload - which on a 4.9 GiB model is slow, visible, and
racy against the user's own CUDA work. A denial that happens *before* the load
costs nothing at all.

**D34 - the assistant is grammar-first.** Measured on this exact model: under
`tool_choice="auto"` it narrates the tool call in prose and emits nothing
callable; pinned, it calls perfectly; under a GBNF constraint it commits every
time. So the decision and the call are two separate constrained passes:

    pass 1  root ::= "tool" | "answer"          <- a two-way choice, no room to ramble
    pass 2  if "tool"    -> the generated per-tool grammar
             if "answer" -> unconstrained prose

That is the whole trick. A grammar-constrained decode does not make the model
better at *deciding*; it removes the option of not committing, which is the only
thing that was broken.

Two things this module deliberately does NOT do. It does not import
`assistant.policy` to re-derive tiers - authority belongs to Plane A and this
process may not widen it. And it does not execute anything: it emits a proposed
call and stops. The gate disposes.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .grammar import (
    Grammar,
    GrammarError,
    build_named_decision_grammar,
    build_tool_call_grammar,
    build_tool_grammar,
)
from .tools import ALL_TOOLS

# The tier ceiling a brain turn may be *reasoned about*. It is a reminder in
# code, not a control: this process cannot execute, and Plane A re-derives the
# real tier from its own registry at the moment of action.
MAX_TIER_FOR_TOOLS = "T3"


def default_system_prompt(tools: Sequence[Any] = ALL_TOOLS,
                          roots: Sequence[str] = ()) -> str:
    """The prompt a brain turn runs under.

    This is not decoration. Measured on this machine with the 4B fast tier:
    asked to list a directory using a tool, with the two-word decision grammar
    and NO system prompt, it answered `answer` and then narrated the tool call
    in prose -- the exact failure D34 recorded for `tool_choice="auto"`. With a
    system prompt naming the tools, the same grammar returned `tool` in 0.15s.

    So the decision pass is not broken and the two-pass design is sound. What
    was missing was the model being told it has tools. `system_prompt` defaulted
    to the empty string, which meant the assistant was asked to choose between
    "tool" and "answer" while never having been introduced to a tool at all, and
    "answer" was the honest answer to that question.

    The tool list is generated from the live registry rather than written out
    here, for the reason D34 gives for generating the grammar from it: a second
    hand-maintained list of tools is a list that will be wrong, and it will be
    wrong silently.

    Kept short deliberately. Prefill runs at ~1540 tok/s against generation's
    ~58 tok/s (D37), so a long prompt is comparatively cheap -- but every token
    here is also a token the 4B has to spend attention on, and the shortest
    prompt that states the contract is the one least likely to distract it.
    """
    lines = [
        "You are the assistant on the user's own Windows machine. You run "
        "entirely locally; there is no cloud service and no internet.",
        "",
        "You act by calling exactly one tool. Calling a tool is not something "
        "you describe afterwards or alongside - the call IS the action. Never "
        "write prose that describes, explains or narrates a tool call, and never "
        "write out the shape of a call by hand.",
        "",
        "A separate, always-on process decides whether your call is permitted "
        "and executes it. You propose; you do not execute. You may reason about "
        f"tools up to {MAX_TIER_FOR_TOOLS} (T3 is the most consequential). If an "
        "action needs the user's confirmation you will be told, so propose it "
        "normally and do not hedge.",
        "",
        "If the request is ordinary conversation, teaching or explanation, and no "
        "tool is called for, answer in plain prose instead.",
        "",
    ]
    if roots:
        # Without this the model cannot form a usable call at all. Measured with
        # it absent: asked to read a file it produced a *relative* path, which
        # resolves against the daemon's working directory rather than the
        # workspace, and asked to archive a folder it produced
        # `C:/Users/YourUsername/Documents` - a placeholder straight out of
        # training data. The gate refused every one of them, which is the system
        # working exactly as designed, but a refusal is not assistance.
        #
        # And naming them was not sufficient on its own. Asked to "copy notes.txt
        # to a backup copy", with the roots listed, the model still reached for
        # `C:/Users/<name>/Desktop` - a folder it remembered rather than one it
        # was given. So it is told the rule explicitly: a file the user mentions
        # without a path is somewhere under a root, and a root beats a memory.
        lines += [
            "You may only act inside these approved roots, and you must use "
            "absolute paths - a bare file name is refused because it means "
            "something different to every process:",
            *(f"  {r}" for r in roots),
            "",
            "When the user names a file without giving a path, it is under one "
            "of those roots - not in a folder you remember from elsewhere. Put "
            "it under the root rather than reaching for somewhere familiar.",
            "",
        ]
    lines += [
        # This block is here because a rule the model cannot express is not a
        # rule. The previous prompt said "if you cannot tell which root it
        # belongs to, say so" - and both the 4B and the 9B ignored it, every
        # time, for a question where they plainly could not tell. Measured: asked
        # to read a file that did not exist, both produced `file.read` against a
        # path they had invented; asked about `F:/theseas/config.toml`, which is
        # outside every approved root, both went straight at it.
        #
        # The reason is not that the models are obstinate. It is that the prompt
        # never said how to comply. The decision grammar admits exactly two
        # outcomes - `answer`, or `tool:<name>` - and the old text described only
        # the tool half. A model told to "say so" with no channel for saying so
        # will pick the only option it has been shown, and "read the thing the
        # user named" looks obligatory when the user names a thing.
        #
        # So: say plainly that both outcomes exist, name the vocabulary the
        # grammar actually uses, and say that one of them is a legitimate answer
        # rather than a failure to act.
        "You have two ways to respond, and only two.",
        "",
        "1. Call a tool. Choose it and give its arguments. Use this whenever you "
        "can actually do what was asked.",
        "",
        "2. Answer in plain words, with no tool. Use this to answer a question, to "
        "ask the user something you need answered, or to say plainly that you do "
        "not know something or cannot find it. Saying you do not know IS a "
        "complete answer here. It is never the wrong choice, and you are not "
        "expected to act on every request.",
        "",
        "Never invent a path, a file, a folder, or a value in order to make a "
        "tool call. A wrong path that looks real is worse than saying you did not "
        "find it. If you are unsure where something is, or whether it exists at "
        "all, option 2 exists for exactly that.",
        "",
    ]
    lines += [
        "Tools available:",
    ]
    for tool in tools:
        if tool.parameters is None:
            # Not described, therefore not callable. Listing it would advertise a
            # tool that the grammar will refuse to emit.
            continue
        args = []
        for key, spec in tool.parameters.items():
            flag = "" if spec.get("required", True) else ", optional"
            args.append(f"{key}: {spec.get('type', 'str')}{flag}")
        shape = f" ({', '.join(args)})" if args else " (takes no arguments)"
        lines.append(f"- {tool.name}{shape} - {tool.description}")
    return "\n".join(lines)


class BrainError(RuntimeError):
    """Anything that stops a turn from being answered."""


class LeaseDenied(BrainError):
    """The arbiter would not grant VRAM. The model was never loaded."""


class BrainNotLoaded(BrainError):
    """A turn was requested before the lease was taken and the model loaded."""


@dataclass(frozen=True)
class BrainConfig:
    """Everything needed to load a model, in one place.

    Defaults are the measured ones from D37 and Phase 1: 4.92 GiB of VRAM at 4K
    context, 2.5 s to load, 1540 tok/s prefill. `n_ctx` is deliberately modest
    because prefill is 27x generation on this card - the workload is long context,
    not long answers, and a bigger window costs prefill on every single turn.
    """

    model_path: str
    n_ctx: int = 4096
    n_batch: int = 512
    n_gpu_layers: int = -1
    n_threads: int | None = None
    max_tokens: int = 512
    temperature: float = 0.2
    # D26: the CUDA runtime must be on PATH and version-matched to the build.
    # The parent sets it; we refuse to guess, because a wrong guess fails at DLL
    # load time with a message that does not mention CUDA at all.
    cuda_bin: str | None = None
    idle_ttl_s: float = 90.0

    def path(self) -> Path:
        p = Path(self.model_path)
        if not p.is_file():
            raise BrainError(f"model not found: {p}")
        return p

    def exists(self) -> bool:
        return Path(self.model_path).is_file()


@dataclass
class Decision:
    """The result of pass 1.

    `kind` is exactly one of "tool" or "answer" - the grammar admits nothing else,
    which is the point. If the model somehow returns something else we treat it as
    "answer", because guessing "tool" and executing nothing is worse than
    answering when it should have acted.

    `tool` is the name pass 1 settled on, and it is what makes pass 2 exact. Left
    empty only if pass 1 failed to name one, in which case pass 2 falls back to
    the flat grammar and `parse_action` validates the result instead.
    """

    kind: str
    raw: str
    latency_s: float
    tool: str = ""

    @property
    def is_tool(self) -> bool:
        return self.kind == "tool"


@dataclass
class Action:
    """A proposed tool call, or an answer. Never an execution."""

    kind: str
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    latency_s: float = 0.0
    # Set when the model emitted something we could not parse. Deliberately not
    # an exception: an unparseable call is a normal failure of a small model, and
    # turning it into an exception would make the caller treat it as a crash.
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "text": self.text,
            # Not rounded to milliseconds. A latency that reads 0.0 is
            # indistinguishable from "not measured", and a caller that branches
            # on slowness would treat a fast turn as a missing one. Three
            # decimals is below the resolution of anything we can measure on
            # Windows and still cannot hide a sub-millisecond value.
            "latency_s": round(self.latency_s, 6),
        }
        if self.kind == "tool":
            out["tool"] = self.tool
            out["arguments"] = self.arguments
        if self.parse_error:
            out["parse_error"] = self.parse_error
        return out


def parse_action(
    content: str, *, tools: Sequence[Any] | None = None
) -> tuple[str | None, dict[str, Any], str | None]:
    """Parse and VALIDATE the JSON body a grammar-constrained call arrives in.

    D34 recorded the detail that matters here: under grammar constraint the call
    arrives in `content`, NOT in llama-cpp's `tool_calls` field. Anything written
    against native `tool_calls` sees nothing at all and concludes the model
    failed. This function is the only place that knows the shape.

    The validation is not decoration, and it was added because a claim made
    about it turned out to be false. The grammar's `args` rule is a flat
    alternation of every tool's argument shape (a forward CFG cannot match the
    name `toolname` just emitted), so the model is free to pair any tool with any
    shape. Measured: asked to run a health check, it answered

        {"tool": "system.diagnose",
         "arguments": {"content": "...", "path": ""}}

    which is the `file.create` shape. Nothing complained, the executor ignored
    both keys, and the call "succeeded" while doing something other than what the
    model said. A silently wrong call is worse than a refused one.

    So the registry is the authority, which is the check the grammar could never
    be. An unknown tool, an argument the tool does not take, a missing required
    one, or a value of the wrong type is an error here - reported, not executed.

    `tools=None` skips validation, which is only for a caller that has no
    registry at all. The default is the live one.
    """
    text = (content or "").strip()
    if not text:
        return None, {}, "empty response"
    # A grammar-constrained decode should never wrap the body in a fence, but a
    # model that has been loaded with a chat template sometimes adds one, and
    # refusing to strip it would turn a formatting artefact into a parse failure.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, {}, f"not JSON: {exc}"
    if not isinstance(body, dict):
        return None, {}, "response is not a JSON object"
    tool = body.get("tool")
    if not isinstance(tool, str) or not tool:
        return None, {}, "no 'tool' field"
    raw_args = body.get("arguments", body.get("parameters"))
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return tool, {}, "'arguments' is not an object"

    if tools is None:
        return tool, raw_args, None

    spec = next((t for t in tools if getattr(t, "name", None) == tool), None)
    if spec is None:
        return None, {}, (
            f"{tool!r} is not a registered tool, so it will not be run. "
            "It is either invented or misspelled."
        )
    schema = getattr(spec, "parameters", None)
    if schema is None:
        # Undescribed: excluded from the grammar precisely because nobody knows
        # its arguments. Running it would mean guessing.
        return None, {}, (
            f"{tool} has no described arguments, so no call can be checked "
            "against it; it is not callable yet"
        )

    unknown = sorted(set(raw_args) - set(schema))
    if unknown:
        known = ", ".join(sorted(schema)) or "none"
        return None, {}, (
            f"{tool} does not take {', '.join(unknown)}. It takes: {known}. "
            "The model paired this tool with another tool's argument shape."
        )
    missing = sorted(
        k for k, v in schema.items() if v.get("required", True) and k not in raw_args
    )
    if missing:
        return None, {}, f"{tool} is missing required argument(s): {', '.join(missing)}"

    for key, value in raw_args.items():
        want = str(schema[key].get("type", "str")).lower()
        if want in ("int", "integer") and not isinstance(value, int):
            return None, {}, f"{tool}.{key} must be a whole number, got {type(value).__name__}"
        if want in ("str", "string") and not isinstance(value, str):
            return None, {}, f"{tool}.{key} must be text, got {type(value).__name__}"
        if want in ("bool", "boolean") and not isinstance(value, bool):
            return None, {}, f"{tool}.{key} must be true or false"
    return tool, raw_args, None


class Brain:
    """One loaded model, answering turns, for as long as its lease lasts.

    The lease-then-load ordering is enforced structurally rather than by
    convention: `_loaded` cannot be set without `_lease` already being set, and
    `load()` is the only thing that sets either.
    """

    def __init__(
        self,
        config: BrainConfig,
        *,
        lease: Callable[[], Any],
        llm_factory: Callable[[BrainConfig], Any] | None = None,
        system_prompt: str | None = None,
        tools: Sequence[Any] = ALL_TOOLS,
        roots: Callable[[], Sequence[str]] | None = None,
    ):
        self.config = config
        # Default to the real prompt rather than an empty string. An empty
        # system prompt is not a neutral one - it is a model with no instructions
        # that will confidently produce anything, and it fails by being refused
        # rather than by erroring, which is the hardest kind of failure to notice.
        # Found by a test that constructed a brain without a roots reader and got
        # back `""`; whether or not production always supplies one, a constructor
        # that produces an empty prompt is a trap for whoever comes next.
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else default_system_prompt(tools, ())
        )
        # Approved roots, re-read every turn rather than captured at load. They
        # can be granted or revoked while the model is resident, and a prompt
        # that still advertises a revoked root is an assistant inviting itself
        # to be refused - or, worse, describing somewhere it no longer belongs.
        self._roots = roots
        self._prompt_cache: tuple[str, ...] | None = None
        self.tools = tools
        self._acquire = lease
        self._lease: Any = None
        self._llm: Any = None
        self._llm_factory = llm_factory or _make_llama
        self._grammar: Any = None
        self._decision_grammar: Any = None
        # Compiled per-tool grammars, keyed by name. Built on demand and reused,
        # because a turn is a thing the user is waiting on.
        self._per_tool: dict[str, Any] = {}
        self._tool_grammar: Grammar | None = None
        self.loaded_at: float | None = None
        self.last_used: float | None = None
        self.turns_served = 0

    # ------------------------------------------------------------------ lifecycle
    def load(self) -> dict[str, Any]:
        """Take the lease, then load. In that order, always.

        Returns a summary the caller can audit. A denial here means nothing was
        loaded and nothing was taken from the user, which is the entire point.
        """
        if self._llm is not None:
            return self.status()

        # Before the lease, deliberately. A lease is a claim on the user's VRAM
        # and it costs the caller nothing to give back; a 4.9 GiB load costs
        # 2.5 s and has to be unloaded again. Refusing the cheap thing first is
        # the whole discipline of D1.
        self.config.path()

        # ---- the ordering that matters (D1) -------------------------------
        lease = self._acquire()
        if lease is None:
            raise LeaseDenied(
                "the VRAM arbiter refused a lease, so the model was not loaded. "
                "Nothing was taken from your GPU."
            )
        if getattr(lease, "denied", False):
            raise LeaseDenied(str(lease))
        self._lease = lease
        # -------------------------------------------------------------------

        if self.config.cuda_bin:
            os.environ["PATH"] = self.config.cuda_bin + os.pathsep + os.environ.get("PATH", "")

        t0 = time.time()
        self._llm = self._llm_factory(self.config)
        load_s = time.time() - t0

        self._decision_grammar = self._make(self._decision_gbnf())
        gen = build_tool_grammar(self.tools)
        self._tool_grammar = gen
        self._grammar = self._make(gen.gbnf)

        self.loaded_at = time.time()
        self.last_used = self.loaded_at
        return {
            "loaded": True,
            "load_s": round(load_s, 2),
            "model": Path(self.config.model_path).name,
            "n_ctx": self.config.n_ctx,
            "tools_in_grammar": len(gen.tools),
            "tools_undescribed": len(gen.gaps),
            "grammar_rules": gen.n_rules,
        }

    def _make(self, gbnf: str) -> Any:
        """Compile a GBNF string into whatever the installed binding wants.

        Compiling eagerly, at load time, is still the right call - a grammar that
        cannot even be built should stop the child immediately rather than at the
        first turn.

        But compiling is NOT proof, and this docstring used to claim it was. It is
        not. Measured against llama-cpp-python 0.3.35 on this machine:

          * an undefined rule reference makes `LlamaGrammar.from_string` print a
            parse error to stderr and **return a broken object** - no exception;
          * sampling with that broken object then dies with an access violation
            inside `llama_sampler_sample`.

        And the converse also bites: a grammar that compiles perfectly can still
        segfault the sampler at sample time if it uses a bare unquoted `-` or a
        multi-line alternation. All three were found by running the sampler, not
        by compiling the text.

        So the protection is two layers and neither is sufficient alone:
        `grammar.py` refuses the shapes that crash the sampler, and this compile
        is the second layer. A grammar that survives both still has to be proved
        against the real sampler before anyone relies on it.
        """
        from llama_cpp import LlamaGrammar

        return LlamaGrammar.from_string(gbnf, verbose=False)

    def _decision_gbnf(self) -> str:
        """Pass 1 decides the tool AND names it, so pass 2 has one shape to pick.

        The flat tool-call grammar accepts every argument shape at once, which
        measured as the model pairing `system.diagnose` with the `file.create`
        arguments. Naming the tool first is what lets pass 2 be exact.
        """
        return build_named_decision_grammar(self.tools).gbnf

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._llm is not None,
            "turns_served": self.turns_served,
            "uptime_s": round(time.time() - self.loaded_at, 1) if self.loaded_at else 0.0,
            "idle_s": round(time.time() - self.last_used, 1) if self.last_used else None,
            "ttl_s": self.config.idle_ttl_s,
            "lease": self._lease is not None,
        }

    def expired(self, now: float | None = None) -> bool:
        """Whether the 90 s idle TTL has elapsed.

        Checked by the *parent*, which is what actually kills us. The child
        reporting "I think I should die" is not a shutdown mechanism; it is a
        suggestion. The parent's clock is the one that counts.
        """
        if self.last_used is None:
            return False
        return (now or time.time()) - self.last_used > self.config.idle_ttl_s

    def unload(self) -> None:
        """Drop the model. VRAM comes back when this process exits (D3)."""
        self._llm = None
        self._grammar = None
        self._decision_grammar = None
        self._per_tool = {}
        self._tool_grammar = None
        self._lease = None
        self.loaded_at = None
        self.last_used = None

    def describe(self) -> dict[str, Any]:
        """What the grammar actually admits, and what it is missing.

        Surfaced rather than logged quietly, because a tool silently missing from
        the grammar is a tool the model can never call, and that gap has to be
        visible to whoever is debugging "the assistant forgot how to do X".

        Answers without a model loaded, because the grammar and the prompt are
        pure and the question is worth asking *before* spending gigabytes on it.
        The two facts are reported separately rather than folded into one:
        `loaded` says whether six gigabytes are currently resident, and the rest
        says what would be reachable if they were.
        """
        gen = self._tool_grammar or build_tool_grammar(self.tools)
        if gen is None:
            return {"loaded": False, "tools": [], "undescribed": []}
        return {
            # The real load state, not "we can describe this". Reporting True
            # here because the grammar builds would tell a user watching the card
            # that six gigabytes are resident when nothing is loaded at all - and
            # the one thing this field exists for is answering that.
            "loaded": self._llm is not None,
            "tools": sorted(gen.tools),
            "undescribed": sorted(gen.gaps),
            "rules": gen.n_rules,
            # The prompt itself, served rather than logged quietly. The grammar
            # above says what the model *can* call; this says what it was *told*,
            # and a brain told about a different set of roots than the gate
            # enforces is the shape of "it kept reaching for Desktop". Answering
            # "which roots do you think you have" should not require attaching a
            # debugger to a process holding six gigabytes.
            "prompt": self.current_prompt(),
            "roots": sorted(self._current_roots()),
        }

    # -------------------------------------------------------------------- prompts
    def _current_roots(self) -> tuple[str, ...]:
        """The approved roots, read fresh.

        Read rather than cached because the point of the whole mechanism is that
        the roots can change while the brain is loaded, and a stale copy would be
        the assistant describing a permission set that no longer exists.
        """
        if self._roots is None:
            return ()
        return tuple(self._roots() or ())

    def current_prompt(self) -> str:
        """The system prompt, rebuilt when the approved roots change.

        A static prompt would name a workspace that has since been revoked. The
        gate would refuse anyway - but a prompt advertising somewhere the user
        took away is an assistant describing itself inaccurately, which is the
        failure this project keeps paying for in other places.
        """
        if self._roots is None:
            return self.system_prompt
        roots = self._current_roots()
        if roots == self._prompt_cache:
            return self.system_prompt
        self._prompt_cache = roots
        self.system_prompt = default_system_prompt(self.tools, roots)
        return self.system_prompt

    def _messages(self, prompt: str) -> list[dict[str, str]]:
        msgs = []
        system = self.current_prompt()
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return msgs

    # --------------------------------------------------------------------- turns
    def _generate(self, prompt: str, grammar: Any, max_tokens: int) -> tuple[str, float]:
        t0 = time.time()
        out = self._llm.create_chat_completion(
            messages=self._messages(prompt),
            grammar=grammar,
            max_tokens=max_tokens,
            temperature=self.config.temperature,
        )
        return (out["choices"][0]["message"]["content"] or ""), time.time() - t0

    def decide(self, prompt: str) -> Decision:
        """Pass 1. A two-way choice, made under constraint.

        This is the pass that fails under `auto`. There is no third option to
        fall into, so the model cannot spend its turn describing what it is about
        to do instead of doing it.
        """
        if self._llm is None:
            raise BrainNotLoaded("no model loaded - call load() first")
        raw, latency = self._generate(prompt, self._decision_grammar, max_tokens=48)
        token = (raw or "").strip().strip('"').strip()
        kind = "tool" if token.lower().startswith("tool") else "answer"
        name = ""
        if kind == "tool":
            # "tool:<name>" - the name pass 2 will be constrained to.
            name = token.partition(":")[2].strip().strip('"').strip()
        self.last_used = time.time()
        decision = Decision(kind=kind, raw=raw or "", latency_s=latency)
        decision.tool = name
        return decision

    def act(self, prompt: str, decision: Decision) -> Action:
        """Pass 2. Either a constrained tool call, or prose."""
        if self._llm is None:
            raise BrainNotLoaded("no model loaded - call load() first")
        t0 = time.time()
        if decision.is_tool:
            grammar = self._grammar
            name = getattr(decision, "tool", "")
            if name:
                # Exactly this tool's shape, so the model cannot pair the name
                # with another tool's arguments. Falls back to the flat grammar
                # only if pass 1 named nothing.
                compiled = self._per_tool.get(name)
                if compiled is None:
                    try:
                        compiled = self._make(
                            build_tool_call_grammar(name, self.tools).gbnf
                        )
                    except GrammarError:
                        compiled = self._grammar
                    # Cached: compiling a grammar is not free, and a turn is a
                    # thing the user is waiting on.
                    self._per_tool[name] = compiled
                grammar = compiled
            raw, _ = self._generate(prompt, grammar, self.config.max_tokens)
            tool, args, err = parse_action(raw, tools=self.tools)
            self.last_used = time.time()
            self.turns_served += 1
            return Action(
                kind="tool",
                tool=tool,
                arguments=args,
                text=raw or "",
                latency_s=time.time() - t0,
                parse_error=err,
            )
        raw, _ = self._generate(prompt, None, self.config.max_tokens)
        self.last_used = time.time()
        self.turns_served += 1
        return Action(kind="answer", text=raw or "", latency_s=time.time() - t0)

    def turn(self, prompt: str) -> Action:
        """A whole turn: decide, then act. Never executes anything."""
        decision = self.decide(prompt)
        action = self.act(prompt, decision)
        action.latency_s = decision.latency_s + action.latency_s
        return action

    def stream(self, prompt: str) -> Iterator[str]:
        """Stream an answer token by token.

        0.3.35 has no `stream_callback` on `create_completion`, so streaming means
        consuming the chunk generator. A seam left unwired would mean writing a
        mock stream and then discovering it chunks differently than the real one;
        this is the real one.
        """
        if self._llm is None:
            raise BrainNotLoaded("no model loaded - call load() first")
        chunks = self._llm.create_chat_completion(
            messages=self._messages(prompt),
            stream=True,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        for chunk in chunks:
            delta = chunk["choices"][0].get("delta", {})
            piece = delta.get("content")
            if piece:
                yield piece
        self.last_used = time.time()
        self.turns_served += 1


def _make_llama(config: BrainConfig) -> Any:
    """Construct the real llama.cpp handle.

    D25/D29: this must be OUR build. The prebuilt wheels ship a CPU backend
    compiled with AVX-512, which this Ryzen 5 5600G cannot execute, and they die
    with STATUS_ILLEGAL_INSTRUCTION on the CPU path. There is no runtime check
    that can detect that cheaply, so `probe_isa.py` is the standing smoke test and
    the install is pinned to `--no-binary :all:`.
    """
    from llama_cpp import Llama

    kwargs: dict[str, Any] = {
        "model_path": str(config.path()),
        "n_ctx": config.n_ctx,
        "n_batch": config.n_batch,
        "n_gpu_layers": config.n_gpu_layers,
        "verbose": False,
    }
    if config.n_threads:
        kwargs["n_threads"] = config.n_threads
    return Llama(**kwargs)
