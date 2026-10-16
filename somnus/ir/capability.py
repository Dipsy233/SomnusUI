"""Capability model: what an LLM is allowed to *do* to a node.

A capability has two levels:

* ``primitive`` - derived deterministically from native Patterns.
* ``semantic``  - provided by an App Pack (or a confident heuristic) and
  resolved to primitive steps via :attr:`Capability.via`.

Every capability may declare :class:`Effect` post-conditions, which is how the
verifier knows what "success" means (dev doc section 9.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CapLevel = Literal["primitive", "semantic"]
Risk = Literal["safe", "normal", "critical"]

#: The complete primitive capability vocabulary (dev doc section 4.5).
#:
#: Two of these are easy to confuse, and the difference matters:
#:
#: * ``select`` highlights/selects an item (SelectionItemPattern) - what a
#:   single click does.
#: * ``activate`` runs the element's *default action* (InvokePattern), which is
#:   a press for a button but an **open** for a file or list row - what a
#:   double click does. It is not a click.
#:
#: Measured in File Explorer: ``CALL li.select()`` highlights the file, while
#: ``CALL li.activate()`` launches it in the associated application.
PRIMITIVE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "activate",
        "focus",
        "read",
        "set_text",
        # Writes text through a Win32 message to the element's own HWND
        # (EM_REPLACESEL, else WM_SETTEXT). Used when a control has no usable
        # Value pattern but does map a real HWND. Like `type_text` it is a
        # routing detail, never a capability the LLM calls: `set_text` stays the
        # single name the model sees, and which primitive runs is the runtime's
        # decision (docs/interface-capability-report.md 3.2).
        "set_text_hwnd",
        # Writes text by putting it on the clipboard and sending Ctrl+V, then
        # restoring what the user had. The only route that reaches a self-drawn
        # editor (Chromium/Electron rich text, QQ's ProseMirror box) that
        # publishes no writable interface and maps no HWND, because the text is
        # consumed by the application's own WM_PASTE handler. Gated on
        # `--allow-clipboard`: it needs the foreground and it borrows the user's
        # clipboard. Like `type_text` it is a routing detail, never a capability
        # the LLM calls (docs/interface-capability-report.md 3.3).
        "paste_text",
        "toggle",
        "select",
        "expand",
        "collapse",
        "increment",
        "decrement",
        "scroll",
        "scroll_into_view",
        "open",
        "close",
        # Opens the element's context menu through UIA itself
        # (IUIAutomationElement3::ShowContextMenu). This is a semantic call, not
        # synthetic input: the pointer does not move, no keystroke is sent, and
        # it does not need the window to be foreground. It is how an application
        # whose default action is not enough (QQ's conversation rows, whose
        # Invoke merely highlights) is still driven - the menu's own items are
        # ordinary UIA menu items and their Invoke works.
        "context_menu",
        # Declarative keyboard fallback (v0.2): focus the target and type the
        # value. Never offered to the LLM as a capability of its own - it only
        # appears as a `via` step inside a pack capability (dev doc section 12.2).
        "type_text",    }
)

#: Capabilities that never change application state.
SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {"read", "focus", "expand", "collapse", "scroll", "context_menu"}
)

#: Capabilities considered `critical` when they originate from a pack.
CRITICAL_CAPABILITIES: frozenset[str] = frozenset(
    {"submit", "pay", "delete", "send", "close", "checkout", "confirm"}
)

#: Native pattern name (lowercase) -> primitive capability it grants.
PATTERN_TO_CAPABILITY: dict[str, str] = {
    "invoke": "activate",
    "toggle": "toggle",
    "selectionitem": "select",
    "griditem": "select",
    "value": "set_text",
    "rangevalue": "increment",
    "expandcollapse": "expand",
    "scroll": "scroll",
    "scrollitem": "scroll_into_view",
    "selection": "select",
    "text": "read",
    "legacyiaccessible": "activate",
    "window": "open",
}


@dataclass(slots=True, frozen=True)
class ParamSpec:
    """Declaration of one capability parameter."""

    type: str = "string"  # string | number | bool | enum
    required: bool = False
    enum: tuple[str, ...] = ()
    default: Any = None

    def accepts(self, value: Any) -> bool:
        if self.type == "string":
            return isinstance(value, str)
        if self.type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if self.type == "bool":
            return isinstance(value, bool)
        if self.type == "enum":
            return value in self.enum
        return True

    def render(self) -> str:
        if self.type == "enum":
            return "|".join(self.enum)
        return self.type


@dataclass(slots=True, frozen=True)
class PrimitiveStep:
    """One resolvable native step inside a semantic capability.

    ``target`` selects which native element the step applies to:

    * ``"$self"``   - the node's own native element
    * ``"$<param>"``- names a parameter that carries a member/native selector
    * any other string - a pack-declared member key (resolved at compile time)

    ``value`` may be ``None``, a literal, or ``"$<param>"`` to forward an
    argument supplied by the LLM.
    """

    primitive: str
    target: str = "$self"
    value: Any = None
    label: str = ""  # human readable, used in the audit log only
    #: Native pattern gate, evaluated when the capability is compiled. Forms:
    #: ``"has:<pattern>"`` (run only if the target exposes it) and
    #: ``"missing:<pattern>"`` (run only if it does not). This is how a pack
    #: declares a declarative keyboard fallback, e.g.
    #: ``set_text`` via ValuePattern, else focus + ``type_text`` (dev doc §12.2).
    condition: str | None = None


@dataclass(slots=True, frozen=True)
class Effect:
    """A verifiable post-condition.

    Grammar (minimal, deterministic)::

        <ref-selector> <field> <op> <value>

    ``ref-selector`` is either a ref (``c1``), ``$self``, or a pack alias such
    as ``cart``. ``field`` is one of ``count``, ``total``, ``value``, ``exists``,
    ``state``, ``rows``. ``op`` is one of ``+=``, ``-=``, ``=``, ``!=``,
    ``startswith``, ``contains``.
    """

    target: str
    field: str
    op: str
    value: Any
    raw: str = ""

    def render(self) -> str:
        return self.raw or f"{self.target}.{self.field} {self.op} {self.value}"


@dataclass(slots=True)
class Capability:
    """A callable ability exposed to the LLM."""

    name: str
    level: CapLevel = "primitive"
    params: dict[str, ParamSpec] = field(default_factory=dict)
    via: list[PrimitiveStep] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    risk: Risk = "safe"
    #: True when this capability must be confirmed by the host before execution.
    requires_confirmation: bool = False
    #: Free-form aliases resolving to this capability name.
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.risk == "safe" and self.name in CRITICAL_CAPABILITIES:
            self.risk = "critical"
        if self.risk == "critical":
            self.requires_confirmation = True

    # -- rendering ----------------------------------------------------------

    def render_params(self) -> str:
        if not self.params:
            return ""
        parts = []
        for pname, spec in self.params.items():
            token = spec.render()
            if spec.required:
                parts.append(pname)
            else:
                parts.append(f"{pname}:{token}" if token else pname)
        return ",".join(parts)

    def render(self) -> str:
        head = f"+{self.name}"
        params = self.render_params()
        if params:
            head += f"({params})"
        if self.requires_confirmation:
            head += "!"
        return head
