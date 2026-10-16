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
AccessibilityProvider (Windows UIA; AX / AT-SPI later)
```

The LLM never sees pixels, coordinates, screenshots, handles, `AutomationId`s or
COM pointers. It sees entities, states, relations, capabilities and values.

## Principles

- **No Vision** – the accessibility tree is the only perception channel.
- **No Synthetic Input** – an application is driven through the interfaces it
  publishes, never by pretending to be a person at the keyboard. This is not a
  preference but the reason the project exists: a keystroke or a click needs
  the window focused, races the target's message pump, and cannot be verified.
  An element that publishes no usable interface is **reported as such**, not
  typed into; `--allow-clipboard` and `--allow-keyboard` opt in when nothing
  else can work (both seize the screen's foreground), and the answer says so
  either way.
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
  chosen by the control's own interface, never by its class name or its role.
  Routes are ordered by how much they cost the user and the first that actually
  works wins:

  | # | Route | Requires | Steals focus |
  | --- | --- | --- | --- |
  | 1 | `ValuePattern.SetValue` | the control publishes a Value pattern | no |
  | 2 | `WM_SETTEXT` / `EM_REPLACESEL` | **the element maps its own HWND** | no |
  | 3 | clipboard + `Ctrl+V` | `--allow-clipboard` | **yes** |
  | 4 | focus and type | `--allow-keyboard` | **yes** |

  **The rule is: edit through UIA first.** Routes 1 and 2 are interfaces the
  application itself publishes, so the write happens on the application's own
  thread: atomic, no focus taken, no synthetic input, invisible to the user.
  Only when neither applies - typically a self-drawn editor (QQ's or Doubao's
  rich-text box) - does the chain fall back to the last two, and **both of those
  seize the screen's foreground**, because being foreground is their
  precondition rather than a side effect.

  Route 2 is a focus-free write: measured against Notepad with its window in the
  **background**, 3/3 exact writes including CJK and a 50-character string, with
  the foreground window never changing. Synthetic input cannot do this under any
  settings (same conditions: `SendInput` landed 0/20 complete, always as a
  truncated prefix). Its criterion is the **element's own HWND** - not "does the
  process own a native `Edit` child window", which wrongly rules out controls in
  the same process that can in fact be written.

  Route 3 (clipboard) needs the foreground too, but on a self-drawn control it is
  **far better than typing**: the text travels in the clipboard instead of the
  input queue, so delivery costs the same regardless of length (measured:
  constant ~37 ms from 10 to 3000 characters), while per-character `SendInput`
  parks 2N queue entries that the target stalls on.

  It borrows the user's clipboard but **hands every format back losslessly**: the
  snapshot stores each format's raw bytes (not just the text) and the restore
  rebuilds them one by one - text, RTF, HTML, file lists, and **images** (a
  bitmap is serialised through `GetDIBits` and rebuilt with `CreateDIBitmap`).
  Measured: all 8 formats on a live clipboard, bitmap included, come back
  byte-identical; after a real paste into QQ the user's image was still there.
  The payload also carries four opt-out formats so it **never enters clipboard
  history or cloud sync**. Only a few formats cannot be rebuilt (`CF_PALETTE`,
  the OLE carrier formats), and those are **named in the result** rather than
  dropped silently. See
  [docs/interface-capability-report.md](docs/interface-capability-report.md) 3.3.

  Which route works is decided **at execution time**, because no static
  inspection can settle it - UIA reports interfaces an element then refuses
  (measured: an editable advertises `value` and its setter is a no-op shell).

  ```
  CALL q1.set_text("x")
  ERR ACTION_NOT_SUPPORTED q1 hint=report-user
      msg="the element offers no interface this runtime can use, and synthetic
           keyboard input is disabled; enable it for one run with
           --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
  ```

  Both synthetic routes are separately authorised and off by default. So Notepad
  is written natively, while a field that publishes no interface (QQ's
  self-drawn box, Steam, VS Code) is **reported rather than silently written**
  unless the host explicitly enables the clipboard or keyboard route. Which route
  answered is remembered for the rest of the session, so a repeated call on the
  same element does not re-probe it. The model still sees only the one name
  `set_text`; which channel runs underneath is the runtime's decision.

  Each channel's principle, measurements and the routes already proven dead are
  in [docs/interface-capability-report.md](docs/interface-capability-report.md)
  (including why a self-drawn Chromium editor cannot be written by any UIA,
  MSAA or `WM_SETTEXT` route).

#### The settings file: an opt-in you can write down

A switch that only exists as a command-line flag is not really an opt-in - **an
MCP client launches this process itself, so there is nowhere for the user to pass
one**. Both switches can therefore be written to a small config file:

```bash
somnus config --path              # where the file is
somnus config                     # show state (writes defaults on first run)
somnus config --allow-clipboard   # turn on the clipboard channel, persistently
somnus config --allow-keyboard    # turn on the keyboard channel
```

The file is plain TOML: **commented, hand-editable, and small enough for an LLM
to edit**:

```toml
# Both synthetic-input channels are OFF unless set to true below.
# They seize the screen's foreground, ...
allow_clipboard = false
allow_keyboard  = false
allow_processes = []
```

Precedence: **flag > environment (`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`) > file > built-in default (both off)**. A one-off
override therefore never has to edit state, and the file's decision applies to
MCP, JSON-RPC and the CLI alike.

A malformed file neither stops the runtime nor silently becomes "allow
everything": it degrades to the defaults and reports the problem in
`somnus config`.
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
| `mock` | recorded `RawTree` fixtures (any platform) | available |

```bash
somnus --provider uia windows          # native windows
```

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
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py (built-in demo pack)
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      persistent settings (the opt-in switches live here)
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
apps/scenario-suite/    WPF (.NET 8) acceptance app: five screens covering the
                        shapes a real GUI throws at an agent (see below)
docs/
  packs.md              how to write an App Pack
  interactive-usage.md  REPL vs one-shot CLI vs script
  what-the-llm-sees.md  one window, four tools, with real output
  scenario-test-plan.md the scenario matrix and its pass criteria
examples/               agent loop, contract check, task evaluation (host examples)
bench/                  benchmark gates + LLM control demos
tests/{
  fixtures/             raw-tree builders, incl. `scenarios.py` (the suite)
  scenarios/            S1: scenario suite on fixtures — runs anywhere
  golden,compiler,projection,runtime,providers/
  e2e/                  S2: real windows (`-m windows`)
}/
```

Dependency direction: `providers → compiler → projection → serialization →
protocol`; `runtime` depends on all of them; nothing imports an LLM.

### The acceptance app: one app, five real-world shapes

A GUI agent is only as tested as the screen it was tested on. A three-row demo
proves nothing about folding, routing or verification, so the acceptance app is
a *suite* of screens, each aimed at one class of difficulty:

| Screen | What it is | What it forces the runtime to get right |
| --- | --- | --- |
| `store` | search box + a **240-row** scrollable catalog + cart | large data: folding, `FIND` into a folded list, a search field written natively |
| `form` | labelled fields (one writable, one read-only), checkbox, combo, disabled button, gated submit | a form's shape: writable vs read-only is visible on the line, validation is a real outcome, the submit **record** is the verification surface |
| `chat` | a **self-drawn** rich-text composer, message history, send button, visible send log | the `set_text` fallback: a control that publishes nothing writable at all (no ValuePattern, no element HWND, reported as a `Group` with the toolkit's class name) - so the runtime must refuse honestly or use an opt-in channel, then record the send |
| `console` | a **plain** text box composer and a contact list | the routing contrast: a control with its own write interface must never fall back to the synthetic channels |
| `tree` | a nested, expandable tree (thousands of nodes) | compression by expansion level: a bounded overview, and a detailed view that costs what was actually unfolded |

Each screen exists twice: as a real WPF window (`apps/scenario-suite`, run with
`ScenarioSuite.exe <screen>`) and as a fixture (`tests/fixtures/scenarios.py`).
The fixtures mirror the live accessibility shape - including the parts that are
easy to get wrong, such as the chat composer, which is a hand-drawn control that
UIA reports as a `Group` called `ProseMirror ExEditor` (no value pattern, no
HWND) exactly as QQ's and 豆包's are - so `tests/scenarios/` runs anywhere while
`tests/e2e/test_windows_scenarios.py` proves the fixtures are faithful.

Replay any screen without Windows:

```bash
somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
```

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
pytest                      # everything portable, plus the scenario suite (S1)
pytest tests/scenarios      # just the scenario suite on fixtures
pytest --update-golden      # rewrite golden SUI-Text fixtures

# S2: the real windows (Windows runner only)
dotnet build apps/scenario-suite
pytest -m windows
```

Two layers, on purpose. **S1** (`tests/scenarios/`) drives the five acceptance
screens replayed from fixtures, so it runs on any platform and every commit.
**S2** (`tests/e2e/test_windows_scenarios.py`) drives the same screens as real
WPF windows through the real UIA provider, and is what makes S1 trustworthy: a
fixture that drifted from the live control would pass S1 and fail S2.

The `MockProvider` replays recorded `RawTree` fixtures and applies scripted
mutations - including real per-row behaviour through a named callback - so the
compiler, projection, resolver, delta, routing-chain and verification paths are
all exercised without a desktop.

## Metrics

| Metric | Target | Current (`somnus --demo bench --runs 100`) |
| --- | --- | --- |
| Zero Vision | 0 vision/coordinate calls | ✓ (structural) |
| Information isolation | 0 native ids in LLM context | ✓ (leak scan = 0) |
| Compression ratio | ≥ 10× | ≥ 1500× (fully expanded tree) |
| Action success | ≥ 99% | 100% |
| Task success | ≥ 90% | 100% |
| L0 observation | ≤ 300 tokens | 80 tokens (240-row catalog, expanded tree) |

Run with `somnus --demo bench --runs 100`. The isolation check is evidence-based
rather than a regex guess: it enumerates the actual automation ids, class names
and runtime ids of the tree under test and asserts none appears in the text the
model would receive, so application data that merely *looks* like an id (a form's
`REQ-0001`) is not mistaken for a leak.

## License

MIT.
