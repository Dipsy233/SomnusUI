# SomnusUI

**Do language models dream of pixels?**

[English](README.md) | 中文

**Compile a native GUI into a pure-text, no-vision, low-token Semantic API** so
an LLM can operate software by *capability calls* instead of *pixel clicks*.

```
User Intent
   ↓
LLM (planner, 4 tools only)
   ↓
Somnus Runtime ─ observe / find / inspect / call
   ├─ Session Registry   (ref ↔ fingerprint ↔ native element)
   ├─ Resolver           (semantic capability → native primitives)
   ├─ Verifier           (settle + effects + delta)
   └─ Policy             (risk, confirmation, allow-list)
   ↓
Semantic Graph
   ↑
Semantic Compiler (deterministic: normalize → infer → match → project → compress)
   ↑
AccessibilityProvider (Windows UIA; CDP as a write channel; AX / AT-SPI later)
```

The LLM never sees pixels, coordinates, screenshots, handles, `AutomationId`s or
COM pointers. It sees entities, states, relations, capabilities and values.

## Principles

- **No Vision** – the accessibility tree is the only perception channel.
- **Low Token** – observation is projected and budgeted: homogeneous lists
  preview three items, anonymous leaves fold into one summary line; L0
  typically costs tens to a few hundred tokens.
- **Semantic First** – the LLM calls `CALL p1.add()`, never `click(312, 528)`.
- **Deterministic Compiler** – no LLM runs inside the runtime or compiler.
- **Fail Loud** – anything not expressible via the accessibility API is
  `UNKNOWN`, never guessed.

## Quick start

```bash
pip install -e ".[dev]"

# Replay the built-in restaurant demo (no Windows required):
somnus --demo observe
somnus --demo call 'CALL p1.add()'
somnus --demo bench --runs 100

# Against a real Windows app (requires the [windows] extra):
pip install -e ".[windows]"
somnus windows                     # list windows
somnus observe                     # what the target window offers
somnus observe w1                  # one window
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # multi-step session, no App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP server (stdio), 4 tools
```

### As an MCP server for any client

Claude Desktop, Cursor, pi and any other MCP client can connect:

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

The server exposes the `observe` / `find` / `inspect` / `call` tools; the
provider is picked automatically (real windows via UIA when the `[windows]`
extra is installed, mock fixtures otherwise).

## What the LLM sees

One real window, four tool calls. Nothing else is ever shown to the model.

**① `observe`** - the current screen as capabilities:

```
@rev 1
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← a text box: it can be written
  INPUT q2 "只读路径" readonly               ← an input with no +set_text: not writable
  BUTTON b1 "共享(S)..."                      ← clickable (activate is implicit)
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← currently unavailable
  TAB t3 "共享"
```

**② `find`** - locate by keyword, returning whole entity lines:

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

**③ `inspect`** - everything about one node (implicit caps and relations
included):

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**④ `call`** - act, and get a delta instead of a fresh observation:

```
CALL tx1.set_text("Hello SomnusUI")
→ OK_UNVERIFIED rev=2
  ~ tx1 value →Hello SomnusUI                  changed
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   added
  - t2                                          removed
```

Failures are one line with a recovery hint, never a stack trace:

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

The model never sees a handle, a coordinate, an `AutomationId`, a process id,
a COM pointer, a raw `control_type`, or the stacks of unnamed `Pane` wrappers a
UIA dump contains. Full walkthrough: [docs/what-the-llm-sees.md](docs/what-the-llm-sees.md).

## Generic mode

A pack is an optimisation, not a requirement. With `--no-pack` (or a window
that simply has no pack) the compiler derives everything from structure and
native patterns, so an application the runtime has never seen is still usable:

- **Controls** are defined by the platform - an element that exposes an
  interaction pattern (`invoke`, `selectionitem`, `toggle`, `value`, …). An
  inert label or container is offered nothing.
- **Capabilities are proven, never guessed from the role.** `activate` appears
  only when the platform really offers a default action (an `Invoke` pattern, or
  the `LegacyIAccessible` fallback). A button exposing only `ExpandCollapse`
  shows `+expand +collapse` and no `activate`, so the gesture that works is the
  one on the line; a control exposing nothing gets no action at all instead of
  one that can only fail.
- **`set_text`** is derived for every text-entry surface, and its route is
  chosen by the control's own interface, never by its class name. Three routes,
  in order:
  1. **a Chromium page's editor** - a self-drawn box (QQ, VS Code, Steam) is
     written through the DevTools Protocol, which is the page's own text
     insertion: the editor's framework sees it exactly as typed input. This is
     the only route that keeps such an editor's model in sync - UIA's
     `SetValue` puts the text in the DOM but runs none of the page's handlers;
  2. **a native control with a Value pattern** - written through it: the
     application's own write interface, atomic and independent of focus;
  3. **anything else** - focused and typed into, which is the last resort it
     has always been.
  So Notepad is written natively, and QQ's ProseMirror box is written through
  the page, both without a pack and without synthetic input.
- **`context_menu`** is offered where a menu is plausible (the element exposes
  something to operate on). It is the escape hatch for an application that
  ignores the native patterns: the menu is opened through UIA itself
  (`ShowContextMenu`), so the pointer never moves and no keystroke is sent, and
  the menu's own items are ordinary menu items whose `activate` works. An
  application whose default action only highlights a row (a chat list) is
  driven this way, without synthetic input. UIA cannot be asked whether an
  element *has* a menu, so the call reports the truth afterwards: a menu that
  appeared is `OK`, one that did not is `OK_UNVERIFIED` and the response names
  the gesture that does work.
- **Entities** are detected structurally: a titled row with a verb button, or
  three or more titled rows under one container (a list, a toolbar, a
  navigation pane).

```bash
somnus --no-pack repl          # drive anything
somnus --no-pack observe w4
```

Packs remain useful for encoding a domain vocabulary the generic path cannot
guess (`PRODUCT p1 +add`, `+submit!`), but they are never needed just to reach
and operate a control.

## Providers

The runtime talks to one `AccessibilityProvider`. Two exist, and the compiler,
projection and runtime are identical for all of them.

| Provider | Source | Status |
| --- | --- | --- |
| `uia` | Windows UI Automation (Win32 / WPF / UWP) | available |
| `cdp` | Chrome DevTools Protocol (a browser page, or the write channel for an Electron app) | optional (`[cdp]`) |
| `mock` | recorded `RawTree` fixtures (any platform) | available |

```bash
somnus --provider uia windows          # native windows
somnus --provider cdp observe          # a browser page, driven directly
```

### Writing into a self-drawn editor (CDP)

An Electron application (QQ, VS Code, Steam) draws its own text box: a
`contenteditable` that UIA cannot usefully write into. UIA's `SetValue` on one
puts the text in the DOM but runs none of the page's handlers, so the editor's
own model never sees it and the application sends the wrong thing - a silent
desync, which is worse than a failure. With the `[cdp]` extra installed and the
application reachable on a loopback debug port, `set_text` goes through the
page's own text insertion instead (`Input.insertText`), the same path a
keystroke takes inside the page. No pointer moves, no keystroke is posted to a
window, and the window does not need to be foreground.

Discovery is automatic: the conventional ports are probed, plus any recorded in
a browser's `DevToolsActivePort` file, plus anything named in `SOMNUS_CDP_PORTS`.
Nothing else changes - observation still comes from UIA, and the page channel is
only consulted for elements inside page content, never for a browser's own
chrome (which has its own working native interface).

## Background workspace (non-intrusive operation)

The agent's work does not require the window to be foreground and never
steals the user's focus. Verified live:

- A **visible but covered** window exposes its full tree and accepts every
  UIA primitive - being foreground is not a requirement of anything the
  runtime does;
- a **minimized** window lays nothing out (CEF apps such as Steam/QQ/Chrome
  collapse to a bare shell) and is the one state that actually blocks
  operation;
- a window on **another virtual desktop** (not minimized) is usually fully
  usable.

So the provider automatically does two things before snapshotting or acting
(`workspace.py`):

1. a minimized window is shown with `SW_SHOWNOACTIVATE` - **never
   activating**, so whatever the user is typing into keeps its focus;
2. a window on another virtual desktop is moved to the current one through
   the official `IVirtualDesktopManager` (same process, no second login:
   QQ signs in once on the user's desktop).

The window's prior state is recorded, and provider `close()` **restores
it** (re-minimizes what was minimized) - agent work never leaves the user's
desktop rearranged.

Known limitation: a no-activate show cannot fully hide the window (its
taskbar icon appears; if it was topmost it is briefly visible on screen,
fully covered windows are unnoticeable). That is inherent to the
no-activation path and still less intrusive than any focus-based approach,
synthetic input included.

## The four tools

| Tool | Purpose | Returns |
| --- | --- | --- |
| `observe` | L0 overview of the target window | SUI-Text |
| `find` | `FIND [type] "query" [in ref]` | matching entity lines |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 detail |
| `call` | `CALL ref.cap(args)` | `OK`/`ERR` + delta |

## SUI-Text

The single LLM-facing format. Capabilities live on the node line; default
capabilities and default states are omitted (the system prompt carries the
table).

```
@rev 1
WINDOW w1 "Restaurant Demo" active
  SCREEN s1 "menu"
    LIST l1 "Products" count=3 entity=product +find
      PRODUCT p1 "Big Mac" price=5.99 +add +customize(size:small|medium|large)
    CART c1 "Cart" count=0 total=0 +open
    CHECKOUT b5 "Checkout" +submit!
```

Actions and results:

```
FIND product "Big Mac"   →  FOUND 1 / PRODUCT p1 "Big Mac" price=5.99 +add
CALL p1.add()            →  OK rev=2
                            ~ c1 count 0→1
                            ~ c1 total 0→5.99
CALL b5.submit()         →  ERR CONFIRMATION_REQUIRED b5 hint=ask-user
```

Full grammar: [`somnus/sui/grammar.md`](somnus/sui/grammar.md).

## Repository layout

```
somnus/
├── ir/            node / capability / action value objects (platform free)
├── providers/     base.py  mock.py  cdp.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py (built-in demo pack)
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
docs/
  packs.md              how to write an App Pack
  interactive-usage.md  REPL vs one-shot CLI vs script
  what-the-llm-sees.md  one window, four tools, with real output
examples/               agent loop, contract check, task evaluation (host examples)
bench/                  benchmark gates + LLM control demos
tests/{fixtures,golden,compiler,projection,runtime,providers,e2e}/
```

Dependency direction: `providers → compiler → projection → serialization →
protocol`; `runtime` depends on all of them; nothing imports an LLM.

## App Packs (optional)

A pack gives an application a domain vocabulary, turning the control tree into
entities with capabilities (`PRODUCT p1 +add`). It is an optimisation, not a
requirement: a window with no matching pack is compiled generically and stays
fully operable - see [Generic mode](#generic-mode).

```python
from somnus.packs.restaurant_demo import build_pack
from somnus.runtime.session import Session
from somnus.providers.mock import MockProvider

session = Session(provider, pack=build_pack())
```

Authoring guide: [`docs/packs.md`](docs/packs.md).

## Testing

```bash
pytest                      # compiler/projection/runtime tests run anywhere
pytest --update-golden      # rewrite golden SUI-Text fixtures
pytest -m windows           # UIA + WPF E2E (Windows runner only)
```

The `MockProvider` replays recorded `RawTree` fixtures and applies scripted
mutations, so the compiler, projection, resolver, delta and verification paths
are all exercised without a desktop.

## Metrics

| Metric | Target | Current (`--demo bench`) |
| --- | --- | --- |
| Zero Vision | 0 vision/coordinate calls | ✓ (structural) |
| Information isolation | 0 native ids in LLM context | ✓ (leak scan = 0) |
| Compression ratio | ≥ 10× | ≥ 73× |
| Action success | ≥ 99% | 100% |
| Task success | ≥ 90% | 100% |

Run with `somnus --demo bench --runs 100`.

## License

MIT.
