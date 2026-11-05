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
LLM planner (seven tools)
   ↓
Somnus runtime ─ observe / find / inspect / call / run / switch / configure
   ├─ Session registry   ref ↔ fingerprint ↔ native element
   ├─ Resolver           semantic capability → native primitives
   ├─ Verifier           settle + effects + delta
   └─ Policy             allow-list + input-channel gates
   ↓
Semantic graph
   ↑
Semantic compiler (deterministic: normalize → infer → match → dedupe → project → compress)
   ↑
AccessibilityProvider (Windows UI Automation; AX / AT-SPI planned)
   └─ peer provider: a session on the far side of the session boundary
```

## Principles

- **No vision.** The accessibility tree is the only perception channel. The
  tree is read from the **visible viewport**: off-screen rows are summarised as
  `rows= / visible=`, never silently dropped, and `IsOffscreen` is not trusted
  (it reports 0 hidden rows on a list where only 12 are on screen).
- **Every action takes the foreground.** A window is raised before it is read
  or written, and text goes in through a channel the application can verify:
  `ValuePattern` first, then the clipboard channel, then the keyboard channel.
  All of them need the target in front, and the response states which channel
  answered. The runtime does not attempt focus-free writes — a write that lands
  wherever the focus happens to be is neither predictable nor attributable. To
  work without disturbing the user, run the agent in a child session
  (see [docs/child-session.md](docs/child-session.md)). An element that
  publishes no usable interface is reported as such.
- **Capability-first, no confirmation gate.** There is no consent step, no
  risk scoring and no dangerous-action interception: a capability printed on a
  node runs when called. The `!` suffix (`+submit!`) is declared metadata about
  the capability, not a permission request. The host decides whether to ask a
  human; the runtime does not.
- **Low token cost.** Observation is projected and budgeted and read only from
  the visible viewport: homogeneous lists preview a few complete rows and fold
  the rest into `… +N more`, anonymous leaves fold into one summary line. An L0
  overview typically costs tens to a few hundred tokens.
- **Semantics first.** The model calls `CALL p1.add()`, never `click(312, 528)`.
- **Deterministic compiler.** No language model runs inside the runtime or the
  compiler.
- **Fail loud.** Anything the accessibility API cannot express is reported as
  `UNKNOWN`, never guessed. A UIA capability is still reported honestly when
  the control refuses it.

## Quick start

Run these **from the repository root** (the `bench/` and `tests/` packages are
imported by path, not installed).

```bash
pip install -e ".[dev]"

# Replay the built-in restaurant demo (no Windows required):
somnus --demo observe
somnus --demo call 'CALL p1.add()'
python -m somnus.cli bench --runs 100   # the console-script form cannot import bench/

# Against a real Windows application (requires the [windows] extra):
pip install -e ".[windows]"
somnus windows                     # list windows
somnus observe                     # what the target window offers
somnus observe w1                  # one window
somnus call 'CALL b1.activate()'
somnus --no-pack repl              # multi-step session with no App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP server (stdio), seven tools

# A second desktop the agent can work in without disturbing the user (Windows Pro+):
somnus child-session status        # is the feature available here?
somnus child-session setup         # once, elevated
somnus child-session start         # open it
somnus child-session close         # end it and clean up
```

> **Refs live only as long as one session.** `somnus observe` / `somnus call`
> are one-shot commands: the process exits after that single tool call, so every
> ref (`p1`, `w1`, `q1`, …) dies with it. For anything involving more than one
> call — which is most real tasks — use **`somnus repl`** (one live session,
> multi-line input), the **MCP server** (one long-lived session per client), or
> the **`run`** tool (a whole flow inside one call). The same is true of
> `somnus --demo` / `somnus --scenario`, which are otherwise convenient for
> exploring a single observation.
>
> Why `python -m somnus.cli bench` instead of `somnus ... bench`: the console
> script's `sys.path` starts in `.venv/Scripts`, so the repo-local `bench`
> package is not importable; `python -m` from the repo root puts the repository
> on `sys.path`. Every other command above works through the `somnus` entry
> point.

### As an MCP server

Any MCP client can connect — Claude Desktop, Cursor, pi and others:

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

The server exposes `observe` / `find` / `inspect` / `call` / `run` / `switch` /
`configure`. The provider is selected automatically: real windows through UI
Automation when the `[windows]` extra is installed, recorded fixtures otherwise.
On startup the server's `instructions` state the effective settle timings
(`settle_quiet_ms=40`, `settle_poll_ms=10`, `settle_timeout_s=3.0`) and the
`post_action_enum=on` default, so a client knows both the values and their
trade-offs before the first call; `configure` changes them at runtime.

## What the model sees

One real window, four tool calls. The only desktop context is the one-line
`WINDOWS` header (window ref, title, active flag) — no other window's contents
are included.

**① `observe`** — the desktop header, then the current screen as capabilities:

```
@rev 1
WINDOWS w1 "agent 属性" active
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

The `WINDOWS` line lists up to six top-level windows and marks exactly one as
`active`; anything beyond six folds into `+N more`, and the active window is
always included (it replaces the sixth slot if it falls outside the first six).
It carries no `ops` count (counting operable elements needs one shallow read per
window, measured at ~111 ms for five windows — above the observation's ~80 ms
budget). `OBSERVE windows` is still the cheapest way to get the list alone.

A list larger than the viewport is read **only where it is visible**; off-screen
rows are summarised, never silently missing:

```
@rev 4
WINDOWS w1 "Store" active
WINDOW w1 "Store" active
  SCREEN s1 "catalog"
    LIST l1 "Products" rows=240 visible=12 scroll=5% +find
      ITEM li1 "Item 0" price=$1.00 +add          ← visible rows are complete
      ITEM li2 "Item 1" price=$1.50 +add
      ITEM li3 "Item 2" price=$2.00 +add
      +more items=228 use FIND "<name>" or OBSERVE w1 expand l1
```

`rows=` is the real total, `visible=` how many rows are on screen. Use
`FIND "<name>"` to reach an off-screen row, or `OBSERVE w1 expand l1` to ask the
collector to read a page for that container (the page size is not specified; it
is whatever the collector reads next for that ref). Scroll/expand invalidates
the viewport, so the next response says `~ viewport changed; OBSERVE again` when
the captured set no longer matches the screen.

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
→ OK rev=2
  ~ tx1 value →Hello SomnusUI                  changed
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   added
  - t2                                          removed
```

`OK_UNVERIFIED` is reserved for a settled call whose delta is empty (it ran, but
nothing visible changed); a call that produced a delta is `OK`.

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

**⑤ `read`** — the implicit content capability. `CALL q1.read()` returns the
text the control publishes as data, not a delta:

```
CALL q1.read()
→ OK rev=5
  ~ q1 value="共享" len=2
```

- The payload is `value="…" len=N`. `len=` is exact unless the provider's
  **4096-character read cap** was hit: then it is `len>=4097` and the line ends
  with `truncated=true; use INSPECT with range=0:2000 for more`.
- At most **200 characters** come back inline (199 + `…` when cut); longer text
  needs `INSPECT`, which takes a range.
- The UIA `Text` pattern is tried first, then the `Value` property; a pure
  label with neither is read from its accessible Name.
- `read` is implicit: L0 does not print `+read`, and `INSPECT` lists it only
  when the node really publishes a readable surface.

Every quoted string in SUI-Text — `value=` included — escapes `\\`, `\"`, `\r`,
`\n` and `\t`, so a node is always exactly one line. Previews are for reading,
not for re-parsing: the parser decodes only `\\` and `\"`. `observe`/`inspect`
also print a short `value=` for text-like nodes (max **80 characters**, `…` when
cut); values longer than 160 characters switch to the serializer's
`len= / preview=` form instead.

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
  | 2 | clipboard + `Ctrl+V` | `allow_clipboard` (on by default) | no / **yes** |
  | 3 | focus and type | `allow_keyboard` (on by default) | yes / no |

  Route 1 writes through an interface the application itself publishes, so the
  write happens on the application's own thread: atomic, no synthetic input, no
  clipboard borrowed. Route 2 is constant-cost (~37 ms from 10 to 3000
  characters) and reaches a control that publishes no writable interface, but it
  borrows the user's clipboard and restores it format by format. Route 3 is the
  last resort and can stall on the target's input queue. (A `WM_SETTEXT` /
  `EM_REPLACESEL` route existed in an earlier design and was removed: any
  control that maps an HWND and holds editable text also publishes a Value
  pattern, so it was unreachable.) Every route requires the target window in the
  foreground — the runtime raises it first rather than writing without focus.
  Route selection is decided at execution time, because static inspection
  cannot settle it: UIA reports interfaces an element then refuses. The route
  that answered is remembered for the rest of the session.

  Both synthetic channels are **on by default in this build**; turn one off for
  one run with `--no-allow-clipboard` / `--no-allow-keyboard`, or persistently
  with `somnus config --no-allow-clipboard` / `--no-allow-keyboard`. A call that
  cannot be served then is reported rather than silently redirected:

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

Persistent settings live in a small hand-editable TOML file. **Both synthetic
channels are on by default in this build** (the 50-task benchmark showed that
refusing them leaves whole classes of tasks unfinished); the file is how you
turn them *off* and keep them off:

```bash
somnus config --path              # where the file is
somnus config                     # show the effective state (writes defaults on first run)
somnus config --no-allow-clipboard   # turn the clipboard channel off, persistently
somnus config --no-allow-keyboard    # turn the keyboard channel off
somnus config --allow-clipboard      # turn it back on
```

Precedence for the two channels: **one-run flag (`--allow-clipboard` /
`--no-allow-clipboard`) > environment variable (`SOMNUS_ALLOW_CLIPBOARD` /
`SOMNUS_ALLOW_KEYBOARD`) > file > built-in (on)**. A malformed file degrades to
the defaults and reports the problem in `somnus config`; it never becomes
"allow everything".

Timing knobs can be set in the same file **and changed at runtime** from the
model side with the `configure` tool:

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `settle_quiet_ms` | `40` | 10–1000 | how long the UI must stay unchanged before a call is reported done. Smaller = faster; too small can read an animation mid-flight. |
| `settle_poll_ms` | `10` | 1–100 | probe spacing. |
| `settle_timeout_s` | `3.0` | 0.5–30 | hard cap for one settle; on timeout the reply is `settled=false` but still carries the delta. |
| `post_action_enum` | `true` | bool | re-enumerate windows after an action, so a window the action opened is reported. Off saves ~100 ms per call and can hide that new window. |

Precedence for these: **built-in < file < environment
(`SOMNUS_SETTLE_QUIET_MS`, `SOMNUS_SETTLE_POLL_MS`, `SOMNUS_SETTLE_TIMEOUT_S`,
`SOMNUS_POST_ACTION_ENUM`) < `configure`**. `configure` is session-scoped and
applies to the next call; `persist=true` writes the file, `reset=true` restores
the startup values. One bad value refuses the whole patch.

```json
{"settle_quiet_ms": 80, "settle_poll_ms": 20, "post_action_enum": false}
```

```text
SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

The MCP server repeats the effective values in its `instructions` at startup,
so the model knows the defaults and what changing them costs before it acts.

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

When the raise is refused, or the snapshot comes back with a titled window and
**no children at all**, the observation says so instead of presenting an empty
window as fact:

```
~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows
~ tree looks empty; window may be covered or app not rendering
```

### When that is not enough: a child session

Raising a window takes the user's focus, and the clipboard and keyboard channels
additionally send synthetic input to whatever is in the foreground. A **child
session** — a second interactive logon for the same account, with its own input
desktop, foreground window and input queue — removes the interference entirely:
work there cannot disturb the console session.

**Preconditions and costs**

- Windows **Pro / Enterprise / Education**; Home cannot create child sessions.
- One elevated `somnus child-session setup` (enables the feature, the RDP
  listener, credential delegation, and sets `TermService` to automatic).
- The session is reached through an **RDP loopback**, so it exists only while
  that connection does. A Windows Hello **PIN cannot be used** for the loopback;
  if delegation is refused, store the account password once with
  `somnus child-session credential --save`.
- **One child session at a time**, system-wide.
- No security isolation: both sessions are the same account.

**CLI (setup and diagnostics)**

| Command | Effect |
| --- | --- |
| `somnus child-session status` | read-only: is the feature available, is a session live |
| `somnus child-session setup` | the one elevated step; `--dry-run` / `--disable` |
| `somnus child-session start` | open the loopback (`--headless`, `--auto`) |
| `somnus child-session peer-setup` | start the far-side peer with the child logon (per-user Run key) |
| `somnus child-session probe` | ask the peer on the pipe what it can see |
| `somnus child-session close` | end it and clear leftovers |
| `somnus child-session run -- <cmd>` | run one program on the child desktop, print exit code and stdout/stderr |

`somnus child-session run` sends `process.start` to the peer, polls
`process.status` until the command exits (or `--timeout`, default 600 s, kills
it), then prints the exit code and the full output. Output lives under
`%TEMP%\somnusui-child-run\<timestamp>\`; the last run is kept. It is gated by
`child_allow_spawn` (default `true`, the capability itself) **and**
`allow_processes` (empty = any; the same list that decides which processes may
be operated — a program that cannot be operated cannot be spawned either). It is
**CLI-only**: there is no `SESSION run`, because the model-facing surface does
not expose an arbitrary process launcher.

**For the agent: one desktop at a time**

| Command | Effect |
| --- | --- |
| `SWITCH main` | go to the user's desktop |
| `SWITCH child` | go to the separate session (must be open) |
| `SESSION open` | start it and move there |
| `SESSION close` | end it, and return |
| `SESSION detach` | leave it running after this process exits |

**Refs live only on the current desktop.** A switch swaps the provider and
resets the registry and window-ref maps: after `SWITCH`, `w1` no longer means
the window it meant before. Re-`OBSERVE` before acting.

The compiler, projection, registry, resolver, policy and ref namespace all stay
on the main side; the far session runs a thin peer that answers tool calls over
a named pipe. Design notes, measurements, and the approaches that were tried and
rejected are in [docs/child-session.md](docs/child-session.md).

## The seven tools

| Tool | Purpose | Returns |
| --- | --- | --- |
| `observe` | L0 overview of the target window (viewport-scoped) + a `WINDOWS` header | SUI-Text |
| `find` | `FIND [type] "query" [in ref]`; `timeout` waits for a match | matching entity lines |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 detail |
| `call` | `CALL ref.cap(args)` — several lines run as a batch | `OK`/`ERR` + delta |
| `run` | `CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap(args)` | `RUN OK`/`RUN PARTIAL` + per-step lines + merged delta |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` + window list |
| `configure` | tune `settle_quiet_ms` / `settle_poll_ms` / `settle_timeout_s` / `post_action_enum` | `SETTINGS …` |

There is no confirmation gate on any of them: a printed capability runs when
called. `configure` is a pacing knob, not a permission switch.

### Batch calls

`call` accepts several `CALL` statements, one per line. They run **in order in
one round trip**: every step is validated against the current compilation
before the first one executes (so a step that could never work refuses the
whole batch while nothing has moved), the steps then run without settling
between them, and the batch ends with one settle, one delta and one rev.

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
→ BATCH OK steps=3/3 rev=7
    1. CALL q1.set_text("张三") -> ok
    …
  ~ q1 text="张三"
```

A step that fails stops the batch there; the reply names the failing step,
says which steps really ran, and carries the delta they produced. Every step
must name an element that exists when the batch starts — a flow that opens a
menu and then acts on its items is separate calls, because the items only come
into being at the settle between them. Grammar and response shapes:
[`somnus/sui/grammar.md`](somnus/sui/grammar.md).

### Scripts (`run`)

`run` takes a script, one step per line. A script made only of `CALL`s keeps the
batch's all-or-nothing pre-check; as soon as an `AWAIT` appears, execution
becomes **step by step** (execute → settle → re-read → resolve the next step),
so a step may name an element an earlier step created:

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
→ RUN OK steps=3/3 rev=9
```

- `AWAIT [TYPE] "name" [in ref] [timeout=s]` binds `$it` to the first match
  (default 2 s; `timeout=0` is a single search). It re-reads the viewport until
  the match appears instead of spending a model round-trip on `observe`.
- A failing step stops the run: `RUN PARTIAL steps=2/3 failed-at=3`, followed by
  the step lines and the merged delta for what really ran.
- Limits: a cross-window flow must name the window explicitly (a switch
  reallocates refs); the delta is the net difference between the start and the
  end of the script; `$it` is the first hit, not a collection.
- Measured on a real menu flow: one 3-step run took 453 ms against 428 ms for
  two separate `call`s. The runtime cost is unchanged (every step still
  settles); the saving is one model inference, which is the point.

## SUI-Text

The single model-facing format. Capabilities live on the node line; default
capabilities and default states are omitted (the system prompt carries the
table).

```
@rev 1
WINDOWS w1 "Restaurant Demo" active
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
CALL b5.submit()         →  OK rev=3   (critical caps run directly; no gate)
```

Full grammar: [`somnus/sui/grammar.md`](somnus/sui/grammar.md).

## Repository layout

```
somnus/
├── ir/            node / capability / action value objects (platform free)
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → caps → relations → simplify → twins → match → compile
├── projection/    rules.py budget.py engine.py (viewport rows/visible/+more)
├── sui/           serialize.py parse.py grammar.md (incl. RUN / AWAIT)
├── runtime/       registry resolver settle verifier delta policy session
├── session/       child session: childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── packs/         generic.py restaurant_demo.py (built-in demo pack)
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
├── config.py      persistent settings (channels + settle timings)
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
apps/scenario-suite/    WPF (.NET 8) acceptance app: five screens covering the
                        shapes a real GUI throws at an agent (see below)
tools/rdp-host/         .NET (WinForms) OLE container for the RDP control, the
                        one piece Python cannot host. Built with `dotnet build`.
docs/
  what-the-llm-sees.md      one window, the tools, with real output
  interactive-usage.md      REPL vs one-shot CLI vs script
  packs.md                  how to write an App Pack
  child-session.md          the second desktop: design, costs, rejected options
  text-input-channels.md    how text reaches a control, and at what cost
  optimization-plan.md      what was measured, what changed, what is left
  viewport-capture-proposal.md  the viewport collector: measurements and design
  scenario-suite.md         the acceptance screens and their pass criteria
  benchmark-50-tasks.md     50 real user tasks: what this project can and cannot do
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
| `store` | search box + a 240-row scrollable catalog + cart | large data: viewport folding (`rows=` / `visible=`), `FIND` into rows that are off screen, a search field written natively |
| `form` | labelled fields (one writable, one read-only), checkbox, combo, disabled button, gated submit | writable vs read-only on the line, validation as a real outcome, the submit record as the verification surface |
| `chat` | a self-drawn rich-text composer, message history, send button, visible send log | `set_text` with a control that publishes nothing writable: refuse honestly, or use the clipboard/keyboard channel (both on by default, disable with `--no-allow-*`), then record the send |
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

**Acceptance gates** (`python -m somnus.cli bench --runs 100`, mock/fixtures):

| Metric | Target | Measured (100 runs) |
| --- | --- | --- |
| Zero vision | 0 vision/coordinate calls | ✓ (structurally 0) |
| Information isolation | 0 native ids in model context | ✓ (leak scan = 0) |
| Compression ratio | ≥ 10× | **1373.6×** (min, fully expanded tree) |
| Action success | ≥ 99% | **100%** |
| Task success | ≥ 90% | **100%** (median 3 steps) |
| L0 observation | ≤ 300 tokens | **91 tokens** (median and max) |

`python -m somnus.cli bench --runs 100` (see the Quick start note: the
`somnus ... bench` console-script form cannot import `bench/`). The isolation
check is evidence-based rather than a regex guess: it enumerates the actual
automation ids, class names and runtime ids of the tree under test and asserts
that none appears in the text the model would receive, so application data that
merely looks like an id (a form's `REQ-0001`) is not mistaken for a leak.

### Performance on real windows (measured)

Same machine, Windows 11, `apps/scenario-suite` **store** screen (240-row
non-virtualised `ItemsControl`, 1467-node raw tree), real UIA provider (no
mock), i5-13500HX / 2560×1440@96DPI. The `CALL` range is across repeats; the
`observe` figure is a single run.

| Scenario | Before | After | What changed |
| --- | --- | --- | --- |
| `observe` the store | 3385 ms | **594 ms** (−82.5%) | viewport-scoped capture: skeleton + row peek + only visible rows read in full |
| `CALL` on the store, unchanged tree | ~6 s | **686–706 ms** | signature probe: **0 full snapshots** when nothing changed |
| Signature probe itself | 1622–1938 ms | **384–404 ms** | probe reads only the signature fields on the visible set |
| `CALL` on a small window | 423 ms | **215–231 ms** | fewer settle frames (`quiet=40 ms`, `poll=10 ms`) |
| Token cost of the store `observe` | 292 | **292** | unchanged — the projection still folds to the same lines |
| Desktop change check (`top_level_signature`) | — | **p50 0.31–0.52 ms** (max ≤2.15 ms) | a Win32 `(hwnd, title)` walk; `list_windows` on the same machine is p50 **70–78 ms** |
| `CALL` that writes, title unchanged | — | **81–104 ms** total / **0.6–0.9 ms** enum | the desktop signature matched, so enumeration was skipped |
| `CALL` that writes, title changed | — | **78–104 ms** enum / **154–238 ms** total | a title change changes the window list, so the full enumeration is paid; same order as before — the fix is correctness |
| `observe()` window enumeration | — | **78–102 ms** | observations still enumerate fully; no signature shortcut |

These are the only performance numbers measured end-to-end. A *changing* call
on the store still needs one full snapshot and has **not** been re-measured
after this change; no number is claimed for it. `SOMNUS_PROFILE=1` prints the
per-stage split (enum / snapshot / compile / settle) if you want to reproduce
the measurement on your own machine.

Test counts on this machine: **733 passed** in the portable suite (which
excludes `-m windows` by default) and **38 passed** for the real-window suite
(`pytest -m windows`) run inside the child session, about **62–66 s**, with no
`window ... did not appear` timeout. A UWP app (Calculator) started and read
inside that child desktop; `OBSERVE full` reported 34 buttons.

## License

MIT.
