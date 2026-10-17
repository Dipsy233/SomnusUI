# SomnusUI

**Do language models dream of pixels?**

English | [中文](README.zh-CN.md)

SomnusUI compiles a native GUI into a plain-text semantic API so that a language
model can operate desktop software by **capability calls** instead of pixel
clicks. The model receives a compact description of what each control *is* and
what can be done with it; it never receives screenshots, coordinates, or native
identifiers.

```
User intent
   ↓
LLM planner (five tools)
   ↓
Somnus runtime ─ observe / find / inspect / call / switch
   ├─ Session registry   ref ↔ fingerprint ↔ native element
   ├─ Resolver           semantic capability → native primitives
   ├─ Verifier           settle + effects + delta
   └─ Policy             risk, confirmation, allow-list
   ↓
Semantic graph
   ↑
Semantic compiler (deterministic: normalize → infer → match → project → compress)
   ↑
AccessibilityProvider (Windows UI Automation; AX / AT-SPI planned)
   └─ peer provider: a session on the far side of the session boundary
```

## Principles

- **No vision.** The accessibility tree is the only perception channel.
- **Every action takes the foreground.** A window is raised before it is read
  or written, and text goes in through a channel the application can verify:
  `ValuePattern` first, then the clipboard channel, then the keyboard channel.
  All of them need the target in front, and the response states which channel
  answered. The runtime does not attempt focus-free writes — a write that lands
  wherever the focus happens to be is neither predictable nor attributable. To
  work without disturbing the user, run the agent in a child session
  (see [docs/child-session.md](docs/child-session.md)). An element that
  publishes no usable interface is reported as such.
- **Low token cost.** Observation is projected and budgeted: homogeneous lists
  preview a few items, anonymous leaves fold into one summary line. An L0
  overview typically costs tens to a few hundred tokens.
- **Semantics first.** The model calls `CALL p1.add()`, never `click(312, 528)`.
- **Deterministic compiler.** No language model runs inside the runtime or the
  compiler.
- **Fail loud.** Anything the accessibility API cannot express is reported as
  `UNKNOWN`, never guessed.

## Quick start

```bash
pip install -e ".[dev]"

# Replay the built-in restaurant demo (no Windows required):
somnus --demo observe
somnus --demo call 'CALL p1.add()'
somnus --demo bench --runs 100

# Against a real Windows application (requires the [windows] extra):
pip install -e ".[windows]"
somnus windows                     # list windows
somnus observe                     # what the target window offers
somnus observe w1                  # one window
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # multi-step session with no App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP server (stdio), five tools

# A second desktop the agent can work in without disturbing the user (Windows Pro+):
somnus child-session status        # is the feature available here?
somnus child-session setup         # once, elevated
somnus child-session start         # open it
somnus child-session close         # end it and clean up
```

### As an MCP server

Any MCP client can connect — Claude Desktop, Cursor, pi and others:

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

The server exposes `observe` / `find` / `inspect` / `call` / `switch`. The
provider is selected automatically: real windows through UI Automation when
the `[windows]` extra is installed, recorded fixtures otherwise.

## What the model sees

One real window, four tool calls. Nothing else is shown to the model.

**① `observe`** — the current screen as capabilities:

```
@rev 1
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← a writable text box
  INPUT q2 "只读路径" readonly               ← an input with no +set_text: read-only
  BUTTON b1 "共享(S)..."                      ← clickable (activate is implicit)
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← currently unavailable
  TAB t3 "共享"
```

**② `find`** — locate by keyword; each hit is a complete entity line:

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

**③ `inspect`** — everything about one node, including implicit capabilities
and relations:

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**④ `call`** — act, and receive a delta instead of a fresh observation:

```
CALL tx1.set_text("Hello SomnusUI")
→ OK_UNVERIFIED rev=2
  ~ tx1 value →Hello SomnusUI                  changed
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   added
  - t2                                          removed
```

Failures are a single line with a recovery hint, never a stack trace:

```
ERR TARGET_DISABLED b5 hint=inspect
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
```

The model never sees a handle, a coordinate, an `AutomationId`, a process id, a
COM pointer, a raw `control_type`, or the stacks of unnamed `Pane` wrappers that
a UIA dump contains. A full walkthrough is in
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md).

## Generic mode

A pack is an optimisation, not a requirement. With `--no-pack` — or for a window
that has no pack — the compiler derives everything from structure and native
patterns, so an application it has never seen is still usable:

- **Controls** are defined by the platform: an element that exposes an
  interaction pattern (`invoke`, `selectionitem`, `toggle`, `value`, …). Inert
  labels and layout containers are offered no actions.
- **Capabilities are proven, never inferred from the role.** `activate` appears
  only when the platform offers a default action (an `Invoke` pattern, or the
  `LegacyIAccessible` fallback). A button that exposes only `ExpandCollapse`
  shows `+expand +collapse` and no `activate`; a control that exposes nothing
  gets no action at all rather than one that can only fail.
- **`set_text`** is derived for every text-entry surface, and its route is
  chosen by the control's own interface — never by its class name or role.
  Routes are ordered by how much they cost the user; the first that works wins:

  | # | Route | Requires | Synthetic input / clipboard |
  | --- | --- | --- | --- |
  | 1 | `ValuePattern.SetValue` | the control publishes a Value pattern | no / no |
  | 2 | `WM_SETTEXT` / `EM_REPLACESEL` (not wired) | the element maps its own HWND | no / no |
  | 3 | clipboard + `Ctrl+V` | `--allow-clipboard` | yes / **yes** |
  | 4 | focus and type | `--allow-keyboard` | yes / no |

  Route 1 writes through an interface the application itself publishes, so the
  write happens on the application's own thread: atomic, no synthetic input, no
  clipboard borrowed. Route 2 (`WM_SETTEXT` / `EM_REPLACESEL`) is described for
  completeness and is not wired: a control that maps an HWND and holds editable
  text also publishes a Value pattern, so it would never be reached. Every route
  requires the target window in the foreground — the runtime raises it first
  rather than writing without focus. Route selection is decided at execution
  time, because static inspection cannot settle it: UIA reports interfaces an
  element then refuses. The route that answered is remembered for the rest of
  the session.

  A call that cannot be served by an enabled route is reported rather than
  silently redirected:

  ```
  CALL q1.set_text("x")
  ERR ACTION_NOT_SUPPORTED q1 hint=report-user
      msg="the element offers no interface this runtime can use, and synthetic
           keyboard input is disabled; enable it for one run with
           --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
  ```

  The model still sees only the name `set_text`; which channel runs underneath
  is the runtime's decision. Measurements, clipboard-restore guarantees and the
  routes already proven unusable are documented in
  [docs/text-input-channels.md](docs/text-input-channels.md).

### Settings

Because an MCP client launches the process itself, a command-line flag is not
a practical opt-in. Both channels can be configured in a small TOML file:

```bash
somnus config --path              # where the file is
somnus config                     # show state (writes defaults on first run)
somnus config --allow-clipboard   # turn the clipboard channel on, persistently
somnus config --allow-keyboard    # turn the keyboard channel on
```

Precedence: **flag > environment variable (`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`) > file > built-in default**. A malformed file degrades
to the defaults and reports the problem in `somnus config`; it never becomes
"allow everything".

- **`context_menu`** is offered where a menu is plausible (the element exposes
  something to operate on). It is the escape hatch for an application that
  ignores the native patterns: the menu is opened through UIA itself
  (`ShowContextMenu`), so the pointer never moves and no keystroke is sent, and
  the menu's own items are ordinary menu items whose `activate` works. UIA
  cannot be asked whether an element *has* a menu, so the call reports the truth
  afterwards: a menu that appeared is `OK`, one that did not is `OK_UNVERIFIED`
  and the response names the gesture that does work.
- **Entities** are detected structurally: a titled row with a verb button, or
  three or more titled rows under one container (a list, a toolbar, a
  navigation pane).

```bash
somnus --no-pack repl          # drive anything
somnus --no-pack observe w4
```

Packs remain useful for encoding a domain vocabulary the generic path cannot
infer (`PRODUCT p1 +add`, `+submit!`), but they are never needed merely to reach
and operate a control.

## Providers

The runtime talks to a single `AccessibilityProvider`. The compiler, projection
and runtime are identical for all of them.

| Provider | Source | Status |
| --- | --- | --- |
| `uia` | Windows UI Automation (Win32 / WPF / UWP) | available |
| `mock` | recorded `RawTree` fixtures (any platform) | available |
| `peer` | a `Session` running in the child session | available |

```bash
somnus --provider uia windows          # native windows
```

The `peer` provider backs the child session described below: window enumeration,
the accessibility tree and synthetic input are all confined to the caller's
Windows session, so a second desktop is reached by running a provider where
those windows actually are.

## Windows are raised before use

`OBSERVE` and `CALL` put the target window in the foreground. Covered,
minimized or off-desktop windows are not reliably rendered — self-drawn surfaces
(Chromium, Electron, Qt Quick) publish an empty shell until they are actually
visible. Every path that reads or acts on a window therefore raises it first,
skipping the raise when the window is already in front. `OBSERVE windows` (the
window list) is the exception and never seizes the foreground. The prior state
of every touched window is recorded and restored when the provider closes.

### When that is not enough: a child session

Raising a window takes the user's focus, and the clipboard and keyboard channels
additionally send synthetic input to whatever is in the foreground. A **child
session** — a second interactive logon for the same account, with its own input
desktop, foreground window and input queue — removes the interference entirely:
work there cannot disturb the console session.

```bash
somnus child-session setup     # once, elevated
somnus child-session start     # open the loopback
somnus child-session close     # end it and clean up
```

For the agent this is one tool; it stays on one desktop at a time and `SWITCH`
moves it:

| Command | Effect |
| --- | --- |
| `SWITCH main` | go to the user's desktop |
| `SWITCH child` | go to the separate session (must be open) |
| `SESSION open` | start it and move there |
| `SESSION close` | end it, and return |
| `SESSION detach` | leave it running after this process exits |

The compiler, projection, registry, resolver, policy and ref namespace all stay
on the main side; the far session runs a thin peer that answers tool calls over
a named pipe. Requirements: Windows Pro / Enterprise / Education, one elevated
setup, one child session at a time. There is no security isolation — the two
sessions are the same account. Design notes, measurements and the approaches
that were tried and rejected are in [docs/child-session.md](docs/child-session.md).

## The five tools

| Tool | Purpose | Returns |
| --- | --- | --- |
| `observe` | L0 overview of the target window | SUI-Text |
| `find` | `FIND [type] "query" [in ref]` | matching entity lines |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 detail |
| `call` | `CALL ref.cap(args)` | `OK`/`ERR` + delta |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` + window list |

## SUI-Text

The single model-facing format. Capabilities live on the node line; default
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
├── session/       child session: childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── packs/         generic.py restaurant_demo.py (built-in demo pack)
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      persistent settings (the opt-in switches live here)
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
apps/scenario-suite/    WPF (.NET 8) acceptance app: five screens covering the
                        shapes a real GUI throws at an agent (see below)
tools/rdp-host/         .NET (WinForms) OLE container for the RDP control, the
                        one piece Python cannot host. Built with `dotnet build`.
docs/
  what-the-llm-sees.md      one window, five tools, with real output
  interactive-usage.md      REPL vs one-shot CLI vs script
  packs.md                  how to write an App Pack
  child-session.md          the second desktop: design, costs, rejected options
  text-input-channels.md    how text reaches a control, and at what cost
  scenario-suite.md         the acceptance screens and their pass criteria
  benchmark-50-tasks.md     the proposed semantic-vs-vision benchmark
examples/               agent loop, contract check, task evaluation (host code)
bench/                  benchmark gates and LLM control demos
tests/{
  fixtures/             raw-tree builders, incl. `scenarios.py` (the suite)
  scenarios/            S1: scenario suite on fixtures — runs anywhere
  golden,compiler,projection,runtime,providers/
  e2e/                  S2: real windows (`-m windows`)
}/
```

Dependency direction: `providers → compiler → projection → serialization →
protocol`; `runtime` depends on all of them; nothing imports an LLM.

### The acceptance app: one app, five screens

A GUI agent is only as tested as the screen it was tested on, so the acceptance
app is a suite of screens, each aimed at one class of difficulty:

| Screen | What it is | What it exercises |
| --- | --- | --- |
| `store` | search box + a 240-row scrollable catalog + cart | large data: folding, `FIND` into a folded list, a search field written natively |
| `form` | labelled fields (one writable, one read-only), checkbox, combo, disabled button, gated submit | writable vs read-only on the line, validation as a real outcome, the submit record as the verification surface |
| `chat` | a self-drawn rich-text composer, message history, send button, visible send log | `set_text` with a control that publishes nothing writable: refuse honestly, or use an opt-in channel, then record the send |
| `console` | a plain text box composer and a contact list | routing contrast: a control with its own write interface must never fall back to the synthetic channels |
| `tree` | a nested, expandable tree (thousands of nodes) | compression by expansion level: a bounded overview, detail paid for only where it was unfolded |

Each screen exists twice: as a real WPF window (`apps/scenario-suite`, run with
`ScenarioSuite.exe <screen>`) and as a fixture (`tests/fixtures/scenarios.py`)
that mirrors the live accessibility shape. `tests/scenarios/` therefore runs
anywhere, while `tests/e2e/test_windows_scenarios.py` proves the fixtures are
faithful.

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
fully operable — see [Generic mode](#generic-mode).

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
screens replayed from fixtures, so it runs on any platform and on every commit.
**S2** (`tests/e2e/test_windows_scenarios.py`) drives the same screens as real
WPF windows through the real UIA provider, and is what keeps S1 trustworthy: a
fixture that drifted from the live control would pass S1 and fail S2.

`MockProvider` replays recorded `RawTree` fixtures and applies scripted
mutations — including per-row behaviour through a named callback — so the
compiler, projection, resolver, delta, routing-chain and verification paths are
all exercised without a desktop.

## Metrics

| Metric | Target | Current (`somnus --demo bench --runs 100`) |
| --- | --- | --- |
| Zero vision | 0 vision/coordinate calls | ✓ (structural) |
| Information isolation | 0 native ids in model context | ✓ (leak scan = 0) |
| Compression ratio | ≥ 10× | ≥ 1500× (fully expanded tree) |
| Action success | ≥ 99% | 100% |
| Task success | ≥ 90% | 100% |
| L0 observation | ≤ 300 tokens | 80 tokens (240-row catalog, expanded tree) |

Run them with `somnus --demo bench --runs 100`. The isolation check is
evidence-based rather than a regex guess: it enumerates the actual automation
ids, class names and runtime ids of the tree under test and asserts that none
appears in the text the model would receive, so application data that merely
looks like an id (a form's `REQ-0001`) is not mistaken for a leak.

## License

MIT.
