# SomnusUI

English | [中文](README.zh-CN.md)

SomnusUI compiles a native GUI into a text interface. The runtime reads the
Windows accessibility tree, projects it into SUI-Text (nodes, capabilities and
attributes), and executes capability calls through UI Automation, with optional
clipboard and keyboard channels for text input. The model receives SUI-Text; it
receives no screenshots, coordinates or native identifiers.

Input is one of nine tool calls: `observe`, `find`, `inspect`, `call`, `run`,
`help`, `switch`, `launch` or `configure`. Output is SUI-Text: an observation, a
match list, an `OK`/`ERR` line with a delta, a settings line, or a topic page.

## Overview

- The accessibility tree is the only perception channel. Tree reads are
  restricted to the visible viewport. Off-screen rows are summarised as
  `rows= / visible=` (max 6 windows on the desktop line). `IsOffscreen` is not
  used as a visibility signal: on a 240-row list where 12 rows were on screen,
  it reported 0 hidden rows.
- Every read or write path raises the target window first. A window that is
  already in front is not raised again. `OBSERVE windows` is the exception.
  Self-drawn surfaces (Chromium, Electron, Qt Quick) can publish an empty shell
  until they are visible.
- A capability printed on a node executes when called. There is no confirmation
  step, risk score or dangerous-action interception. The `!` suffix on
  `+submit!` is metadata; it is not a permission request.
- Text entry uses three routes in order: `ValuePattern.SetValue`, clipboard +
  `Ctrl+V`, then focus and type. The response states which route produced the
  write. `WM_SETTEXT` / `EM_REPLACESEL` was removed because every control that
  maps an HWND and holds editable text also publishes a Value pattern.
- The compiler is deterministic. No language model runs inside the runtime or
  the compiler.
- Unsupported or unreadable content is reported as such. The renderer emits
  `? ref role reason=visual` for content that cannot be read without vision.
  An image with no name, no children and no identity is treated as decoration
  and dropped before compilation; one that carries a name or an automation id
  keeps the line. A UIA interface that the control refuses at execution time
  produces `ACTION_NOT_SUPPORTED`, with a recovery hint.

## Getting started

Run the commands from the repository root. `bench/` and `tests/` are imported
by path and are not installed.

```bash
pip install -e ".[dev]"

# Replay the built-in restaurant demo (no Windows required):
somnus --demo observe
somnus --demo call 'CALL p1.add()'
python -m somnus.cli bench --runs 100

# Real Windows application (requires the [windows] extra):
pip install -e ".[windows]"
somnus windows                     # list windows
somnus observe                     # describe the target window
somnus observe w1                  # one window
somnus call 'CALL b1.activate()'
somnus observe w2 --out obs.txt    # large result to a file, stdout gets a receipt
somnus --no-pack repl              # multi-step session without an App Pack
somnus record w1 -o fixture.json
somnus mcp                         # MCP server over stdio
```

`python -m somnus.cli bench` is used because the console script starts with
`.venv/Scripts` on `sys.path`, where the repository-local `bench` package is not
importable. `python -m` from the repository root puts the repository on
`sys.path`. The other commands above work through the `somnus` entry point.

Refs (`p1`, `w1`, `q1`, …) live only for the lifetime of one session.
`somnus observe` and `somnus call` exit after one tool call, so every ref dies
with the process. Multi-step work uses `somnus repl` (one live session),
the MCP server (one long-lived session per client), or the `run` tool (a whole
flow inside one call). `somnus --demo` and `somnus --scenario` follow the same
one-shot rule.

### MCP server

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

The server exposes the tools listed above, filtered to the selected capability
tiers (all nine by default). The provider is selected automatically: UI
Automation when the `[windows]` extra is installed, recorded fixtures otherwise.
The `instructions` field is sent once at `initialize`; it carries the shared
rules plus a snapshot of the values in force at that time: `settle_quiet_ms=40`,
`settle_poll_ms=10`, `settle_timeout_s=3.0` and `post_action_enum=on`.
`configure` returns the live values.

By default the session lives as long as the client. `SOMNUS_IDLE_TIMEOUT_S`
(seconds, off by default) closes the provider after that long without a tool
call; the next call rebuilds it, so refs from before the gap answer
`STALE_TARGET` like any other fresh session.

## Tools and protocol

| Tool | Purpose | Returns |
| --- | --- | --- |
| `observe` | L0 overview of one window, scoped to the viewport, plus a `WINDOWS` line | SUI-Text |
| `find` | `FIND [type] "query" [in ref]`; an optional `timeout` waits for a match | matching entity lines |
| `inspect` | `INSPECT ref [depth=N] [range=A:B]` | L1/L2 detail |
| `call` | `CALL ref.cap(args)`; several lines run as one batch | `OK`/`ERR` plus a delta |
| `run` | `CALL` / `AWAIT [TYPE] "name" [in ref] [timeout=s]` / `CALL $it.cap(args)` | `RUN OK`/`RUN PARTIAL`, per-step lines and a merged delta |
| `help` | `HELP [topic]`; no topic lists tools, tiers and topics | a topic page or the discovery summary |
| `switch` | `SWITCH main` / `SWITCH child` / `SESSION open|close|detach` | `OK` plus a window list |
| `launch` | start a program on the current or child desktop; `wait_s` defaults to 3 s | `OK pid=…` plus new window lines (max six, then `+ N more`) |
| `configure` | tune `settle_quiet_ms` / `settle_poll_ms` / `settle_timeout_s` / `post_action_enum` | `SETTINGS …` |

`configure` is a pacing control. It does not grant or deny capabilities.

### Progressive tool surface

Tools belong to tiers. A host lists only the tiers in force, so a constrained
client never pays for a tool it cannot need:

| Tier | Tools |
| --- | --- |
| `core` | `observe`, `find`, `inspect`, `call`, `run`, `help` |
| `desktop` | `switch`, `launch`, `configure` |

`help` stays in the smallest surface: a model that cannot list the tools it is
missing will guess instead of asking.

```bash
SOMNUS_CAPABILITIES=core somnus mcp        # six tools (the loop + help)
SOMNUS_CAPABILITIES=desktop somnus mcp     # every tool (nine)
SOMNUS_CAPABILITIES=core,desktop somnus mcp  # same, the default
```

`core` is always included; unset or malformed means every tier. The table is the
single source of truth for the MCP `tools/list`, the JSON-RPC transport and
`examples/agent.py`, and `tests/protocol/test_tool_policy.py` pins it.

The protocol is split the same way. Only a short core prompt
(`somnus/protocol/system_prompt.md`) is injected every turn; the full syntax
lives in `somnus/protocol/agent/*.md` and is read on demand. `HELP` without a
topic is the discovery reply - enabled tiers, their tools, tiers that can still
be enabled, and the topic list. `HELP <topic>` returns one page (`format`,
`gestures`, `batching`, `desktops`, `errors`, `settings`); `somnus help <topic>`
prints the same text. Measured: the core prompt is 699 tokens, a core-only host
pays 1295 (prompt + six schemas), and the complete nine-tool surface 1680 -
against 2669 before the split.

### Switching desktops

A switch that changes desktop returns `OK session=… windows=N`, followed by
`desktop changed; observe again`. It assigns no refs. Until a fresh `OBSERVE`,
`CALL` and `INSPECT` that name an old node or window ref return `STALE_TARGET`
with `hint=observe`. A switch that does not change desktop returns
`OK session=… (already here)` and leaves the current refs valid.

### Launching programs

`launch(command, args, cwd, window, wait_s)` starts a program and returns its
`pid` plus the windows that appeared within `wait_s` (default 3 s). Window
lines are capped at six, followed by `+ N more`; more than one new window adds
`~ N windows from one launch`. On the current desktop the reply assigns refs,
unless the session is awaiting a fresh `OBSERVE` after a switch; then it adds
`desktop changed; observe again`.
With `window="child"` from main the program starts through the peer and the
reply lists titles only, followed by `~ on child; switch child to address
them`. Launching on the peer also requires `child_allow_spawn`; both paths
apply `allow_processes`.

### SUI-Text

Capabilities are printed on the node line. Default capabilities and default
states are omitted; the system prompt carries the table.

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

```
FIND product "Big Mac"   →  FOUND 1 / PRODUCT p1 "Big Mac" price=5.99 +add
CALL p1.add()            →  OK rev=2
                            ~ c1 count 0→1
                            ~ c1 total 0→5.99
CALL b5.submit()         →  OK rev=3
```

Full grammar: [`somnus/sui/grammar.md`](somnus/sui/grammar.md).

### Observation output

One real window, four tool calls. The desktop context is the one-line `WINDOWS`
header (window ref, title, active flag). Other windows' contents are not
included.

**`observe`**: desktop line, then the current screen as capabilities:

```
@rev 1
WINDOWS w1 "agent 属性" active
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" +set_text(text)    ← a writable text box
  INPUT q2 "只读路径" readonly               ← an input without +set_text
  BUTTON b1 "共享(S)..."                      ← clickable (activate is implicit)
  BUTTON b2 "高级共享(D)..."
  BUTTON b5 "应用(A)" disabled                ← currently unavailable
  TAB t3 "共享"
```

The `WINDOWS` line lists up to six top-level windows and marks exactly one as
`active`. Additional windows fold into `+N more`; the active window is always
included and replaces the sixth slot when it falls outside the first six. The
line carries no `ops=` count. Counting operable elements requires one shallow
read per window, measured at 111 ms for five windows, against an observation
budget of about 80 ms. `OBSERVE windows` returns the list alone.

A list larger than the viewport is read only where it is visible. Off-screen
rows are summarised:

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

`rows=` is the real total; `visible=` is the number of rows on screen.
`FIND "<name>"` reaches an off-screen row. `OBSERVE w1 expand l1` asks the
collector to read a page for that container; the page size is not specified.
Scroll and expand invalidate the viewport. The next response contains
`~ viewport changed; OBSERVE again` when the captured set no longer matches
the screen.

**`find`**: locate by keyword. Each hit is a complete entity line:

```
FIND "共享"
→ FOUND 7
BUTTON b1 "共享(S)..."
BUTTON b2 "高级共享(D)..."
LINK lnk1 "网络和共享中心"
```

`FIND` deduplicates to one hit per element and prefers the parent row. A child
that exposes an action the parent lacks, such as `+set_text`, remains a
separate hit with its own line.

**`inspect`**: all information about one node, including implicit capabilities
and relations:

```
INSPECT mi2
→ MENUITEM mi2 "编辑" +activate +collapse +expand +focus +read selected_in=mn1
```

**`call`**: execute an action and return a delta:

```
CALL tx1.set_text("Hello SomnusUI")
→ OK rev=2
  ~ tx1 value →Hello SomnusUI                  changed
  + TAB t4 "Hello SomnusUI. 已修改。" +close!   added
  - t2                                          removed
```

`OK_UNVERIFIED` is used when a settled call produced an empty delta: the action
ran and no visible change was observed. A call that produced a delta reports
`OK`.

Failures are one line with a recovery hint:

```
ERR TARGET_DISABLED b5 hint=inspect
ERR NOT_ACTIONABLE b5 hint=wait msg="state=disabled waited=300ms"
ERR CAPABILITY_NOT_FOUND b2 hint=inspect msg="no submit"
ERR PARSE_ERROR hint=fix-syntax msg="primitive verb 'click' is not allowed"
ERR ACTION_NOT_SUPPORTED b1 hint=try select
```

`NOT_ACTIONABLE` is the refusal after the short wait for a blocked element
(a disabled control) ran out: the state is still in force, so retrying will not
help. A provider with no signature probe cannot observe a change and keeps the
immediate `TARGET_DISABLED`.

When `activate` ends in `ACTION_NOT_SUPPORTED` and the node still exposes
`select`, the same error line carries `hint=try select`. The runtime does not
switch to `select` automatically.

**`read`**: the implicit content capability.

```
CALL q1.read()
→ OK rev=5
  ~ q1 value="共享" len=2
```

The payload is `value="…" len=N`. `len=` is exact unless the provider's
4096-character read cap was hit; in that case it is `len>=4097` and the line
ends with `truncated=true; use INSPECT with range=0:2000 for more`. At most
200 characters are returned inline (199 plus `…` when cut). The UIA Text
pattern is tried first, then the Value property. A UIA Text control (control
type 50020) whose two surfaces answer empty is read from its accessible Name;
a calculator display returns `value="显示为 46" len=6`. An empty Edit (50004)
keeps `value=""`, and its Name is not used as content. An element with no Text
or Value surface also falls back to its accessible Name. `read` is implicit:
L0 does not print `+read`, and `INSPECT` lists it when the node publishes a
readable surface.

Every quoted SUI-Text string, including `value=`, escapes `\\`, `\"`, `\r`,
`\n` and `\t`, so a node occupies exactly one line. Previews are for reading;
the parser decodes only `\\` and `\"`. `observe` and `inspect` also print a
short `value=` preview for text-like nodes (max 80 characters, `…` when cut).
Values longer than 160 characters use the serializer's `len= / preview=`
form.

The model receives no handle, coordinate, `AutomationId`, process id, COM
pointer, raw `control_type` or unnamed `Pane` wrapper. A full walkthrough is in
[docs/what-the-llm-sees.md](docs/what-the-llm-sees.md).

### Batch calls

`call` accepts several `CALL` statements, one per line. They run in order in
one round trip. Every step is validated against the current compilation before
the first step executes. A step that cannot work refuses the whole batch before
any state changes. The steps then run without settling between them, and the
batch ends with one settle, one delta and one rev.

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
→ BATCH OK steps=3/3 rev=7
    1. CALL q1.set_text("张三") -> ok
    …
  ~ q1 text="张三"
```

A failing step stops the batch. The reply names the failing step, states which
steps ran, and carries the delta they produced. Every step must name an element
that exists when the batch starts. A flow that opens a menu and then acts on its
items requires separate calls because the items exist only after the settle
between them. Grammar and response shapes:
[`somnus/sui/grammar.md`](somnus/sui/grammar.md).

### Scripts (`run`)

`run` takes a script, one step per line. A script made only of `CALL`s keeps the
batch pre-check. When an `AWAIT` appears, execution becomes step by step:
execute, settle, re-read, then resolve the next step. A step can then name an
element that an earlier step created.

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
→ RUN OK steps=3/3 rev=9
```

- `AWAIT [TYPE] "name" [in ref] [timeout=s]` binds `$it` to the first match.
  The default is 2 s; `timeout=0` performs a single search. It re-reads the
  viewport until the match appears.
- A failing step stops the run: `RUN PARTIAL steps=2/3 failed-at=3`, followed
  by the step lines and the merged delta for the steps that ran.
- Limits: a cross-window flow must name the window explicitly because a switch
  reallocates refs. The delta is the net difference between the start and the
  end of the script. `$it` is the first hit, not a collection.
- Measured on a real menu flow: one 3-step run took 453 ms; two separate `call`s
  took 428 ms. The runtime cost is unchanged because every step settles. The
  saving is one model inference.

### Generic mode

A pack is optional. With `--no-pack`, or for a window without a matching pack,
the compiler derives capabilities from structure and native patterns.

- Controls are defined by the platform: an element that exposes an interaction
  pattern (`invoke`, `selectionitem`, `toggle`, `value`, …). Inert labels and
  layout containers receive no actions.
- Capabilities are derived from published interfaces. `activate` appears only
  when the platform offers a default action (an `Invoke` pattern or the
  `LegacyIAccessible` fallback). A button that exposes only `ExpandCollapse`
  shows `+expand +collapse`, without `activate`.
- `set_text` is derived for every text-entry surface. The route is chosen by
  the control's interface, not by its class name or role, and is decided at
  execution time because UIA reports interfaces that an element can then
  refuse. The route that answered is remembered for the session.

| # | Route | Requires | Synthetic input / clipboard |
| --- | --- | --- | --- |
| 1 | `ValuePattern.SetValue` | the control publishes a Value pattern | no / no |
| 2 | clipboard + `Ctrl+V` | `allow_clipboard` (on by default) | no / yes |
| 3 | focus and type | `allow_keyboard` (on by default) | yes / no |

Route 1 writes through the application's own interface on the application's
thread. Route 2 has a constant cost (about 37 ms from 10 to 3000 characters),
borrows the clipboard and restores it format by format. Route 3 is the last
route and can wait on the target's input queue. Every route requires the target
window in the foreground.

Both synthetic channels are on by default. Turn one off for one run with
`--no-allow-clipboard` / `--no-allow-keyboard`, or persistently with
`somnus config --no-allow-clipboard` / `--no-allow-keyboard`. A call that
cannot be served is reported:

```
CALL q1.set_text("x")
ERR ACTION_NOT_SUPPORTED q1 hint=report-user
    msg="the element offers no interface this runtime can use, and synthetic
         keyboard input is disabled; enable it for one run with
         --allow-keyboard, or persistently with `somnus config --allow-keyboard`"
```

The model sees only `set_text`; the runtime selects the channel. Measurements,
clipboard-restore guarantees and rejected routes are in
[docs/text-input-channels.md](docs/text-input-channels.md).

### Providers

| Provider | Source | Status |
| --- | --- | --- |
| `uia` | Windows UI Automation (Win32 / WPF / UWP) | available |
| `mock` | recorded `RawTree` fixtures | available |
| `peer` | a `Session` running in the child session | available |

```bash
somnus --provider uia windows
```

The compiler, projection and runtime are identical for all providers. Window
enumeration, the accessibility tree and synthetic input for the `peer` provider
are confined to the caller's Windows session.

### Window focus and capture warnings

`OBSERVE` and `CALL` raise the target window before reading or acting. A window
that is already in front is not raised again. `OBSERVE windows` does not take
the foreground. The prior state of every touched window is recorded and
restored when the provider closes.

When the raise is refused, or the snapshot has a titled window with no children,
the observation reports it:

```
~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows
~ tree looks empty; window may be covered or app not rendering
```

## Settings

Persistent settings live in a TOML file. The two synthetic channels are on by
default.

```bash
somnus config --path              # file location
somnus config                     # effective state; writes defaults on first run
somnus config --no-allow-clipboard
somnus config --no-allow-keyboard
somnus config --allow-clipboard
```

Channel precedence: one-run flag (`--allow-clipboard` / `--no-allow-clipboard`)
> environment variable (`SOMNUS_ALLOW_CLIPBOARD` / `SOMNUS_ALLOW_KEYBOARD`) >
file > built-in (on). A malformed file falls back to the defaults and reports
the problem in `somnus config`.

Timing values can be set in the same file and changed at runtime with
`configure`:

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `settle_quiet_ms` | `40` | 10–1000 | how long the UI must stay unchanged before a call is reported done. Smaller values are faster; small values can read an animation mid-flight. |
| `settle_poll_ms` | `10` | 1–100 | probe spacing. |
| `settle_timeout_s` | `3.0` | 0.5–30 | hard cap for one settle; on timeout the reply reports `settled=false` and still carries the delta. |
| `post_action_enum` | `true` | bool | re-enumerate windows after an action so a window opened by the action is reported. Off saves about 100 ms per call and can hide that window. |
| `actionable_timeout_s` | `0.3` | 0–2 | how long a blocked action (a disabled control) waits for the element to become actionable. Only paid on the blocked path; `0` disables the wait. File/environment only (`SOMNUS_ACTIONABLE_TIMEOUT_S`), not settable through `configure`. |

Timing precedence: built-in < file < environment
(`SOMNUS_SETTLE_QUIET_MS`, `SOMNUS_SETTLE_POLL_MS`, `SOMNUS_SETTLE_TIMEOUT_S`,
`SOMNUS_POST_ACTION_ENUM`) < `configure`. `configure` applies to the next call
in the current session. `persist=true` writes the file. `reset=true` restores
the startup values. One invalid value refuses the whole patch.

```json
{"settle_quiet_ms": 80, "settle_poll_ms": 20, "post_action_enum": false}
```

```text
SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

Two additional generic behaviours:

- `context_menu` is offered where a menu is plausible: the element exposes
  something to operate. The menu opens through UIA (`ShowContextMenu`), so the
  pointer does not move and no keystroke is sent. UIA cannot report whether an
  element has a menu, so the result is reported after the call: a menu that
  appeared produces `OK`; one that did not produces `OK_UNVERIFIED` and names
  the gesture that works.
- Entities are detected structurally: a titled row with a verb button, or three
  or more titled rows under one container (a list, toolbar or navigation pane).
- A control the platform reports without a name gets one from the label it
  visibly has: the text sibling in front of a field, or the only leaf text it
  contains. The name is presentation evidence only - `FIND` and entity
  matching use it, the fingerprint does not, so a caption that changes (a
  counter, a price) never re-mints a ref. Without a label the control stays
  unnamed.

## Decision traces

`somnus trace to-script DIR` distils a recorded flow into a `run`-script
draft: every successful `CALL` becomes `AWAIT "name"` + `CALL $it.cap(args)`,
so the second time the task costs one `run` call instead of one round
trip per step; steps the trace cannot name become `# TODO` comments, and
`heal` repairs the draft when the UI drifts.

`--trace DIR` (or `SOMNUS_TRACE=DIR`) records what the runtime decided: the
route a capability took, the settle frames, the delta, the verifier status and
the error code, plus the SUI-Text the model saw before and after the call.
Without `DIR`, a fresh run directory is created under the platform data
directory (`%LOCALAPPDATA%\somnusui\traces` on Windows), outside the repository.

```bash
somnus --demo --trace /tmp/somnus-demo-trace call 'CALL p1.add()'
somnus trace show /tmp/somnus-demo-trace                     # timeline
somnus trace show /tmp/somnus-demo-trace --snapshots         # + before/after SUI-Text
```

Tracing is off by default and costs nothing on the hot path when off. Trace
files are development evidence: they stay outside the repository and are **not
redacted in this stage** (snapshots can contain window titles, form values and
chat text). Redaction is a close-out step before any trace is added to the
repository as reference evidence; the entry point and its required decisions
are marked `TODO(stage-final)` in `somnus/observability/jsonl.py`.

## Child session

A child session is a second interactive logon for the same account. It has its
own input desktop, foreground window and input queue, so synthetic input and
window raises do not reach the console session.

Preconditions and costs:

- Windows Pro / Enterprise / Education. Home cannot create child sessions.
- One elevated `somnus child-session setup`. It enables the feature, the RDP
  listener and credential delegation, and sets `TermService` to automatic.
- The session is reached through an RDP loopback, so it exists only while that
  connection does. A Windows Hello PIN cannot be used. If delegation is refused,
  store the account password once with `somnus child-session credential --save`.
- One child session at a time, system-wide.
- No security isolation: both sessions are the same account.

CLI:

| Command | Effect |
| --- | --- |
| `somnus child-session status` | read-only: feature availability and live session state |
| `somnus child-session setup` | the one elevated step; `--dry-run` / `--disable` |
| `somnus child-session start` | open the loopback (`--headless`, `--auto`) |
| `somnus child-session peer-setup` | register the peer to start with the child logon (per-user Run key) |
| `somnus child-session probe` | ask the peer on the pipe what it can see |
| `somnus child-session close` | end the session and clear leftovers |
| `somnus child-session run -- <cmd>` | run one program on the child desktop and print its exit code and output |

`somnus child-session run` sends `process.start` to the peer, polls
`process.status` until the command exits (or `--timeout`, default 600 s, kills
it), then prints the exit code and the full stdout/stderr. Output files are
written under `%TEMP%\somnusui-child-run\<timestamp>\`; the last run is kept.
The command is gated by `child_allow_spawn` (default `true`) and
`allow_processes` (empty = any). `allow_processes` is the same list that
decides which processes may be operated; a program that cannot be operated
cannot be spawned. `run` is CLI-only. SUI-Text has no `SESSION run`.

For the agent, one desktop is active at a time:

| Command | Effect |
| --- | --- |
| `SWITCH main` | go to the user's desktop |
| `SWITCH child` | go to the child session (must be open) |
| `SESSION open` | start the child session and move there |
| `SESSION close` | end it and return |
| `SESSION detach` | leave it running after this process exits |

A switch replaces the provider and resets the registry and window-ref maps, and
assigns no refs. The reply is `OK session=… windows=N` plus
`desktop changed; observe again`. Until a fresh `OBSERVE`, `CALL` and `INSPECT`
that name an old node or window ref return `STALE_TARGET`. The compiler,
projection, registry, resolver, policy and ref namespace remain on the main
side; the child session runs a thin peer over a named pipe.

`launch(..., window="child")` starts a program on the child desktop from main.
The reply lists titles only, at most six `+ WINDOW` lines followed by
`+ N more`, and ends with `~ on child; switch child to address them`. This path
requires `child_allow_spawn` and applies `allow_processes`.

Design notes, measurements and rejected approaches are in
[docs/child-session.md](docs/child-session.md).

## Self-check and replay

Four CLI-only commands let an agent (or a person) drive the sandbox the way a
model would, without a language model in the loop:

```bash
somnus doctor                 # environment report + observe/find/call smoke
somnus --demo doctor          # the same, offline
somnus eval                   # JSON verdict, judged from runtime state
somnus --scenario store eval  # offline
somnus replay <trace>         # replay recorded calls, name the first divergence
somnus --demo replay <trace>  # offline
somnus heal script.sui        # replay a run script, patch one failed step
somnus --demo heal script.sui # offline
```

The sandbox rule is fixed: the **child session** is the default, mock/scenario
fixtures are the offline path, and the user's main desktop requires an explicit
`--real`. A missing child session is reported with the command that starts one,
never silently replaced by the main desktop. `doctor` is read-only with respect
to the session: it reports whether one is live and what to run if it is not.

`eval` runs three generic tasks - `observe`, `find`, `call` - against whatever
the current desktop offers. Success comes from the runtime, not from a task
script: a compiled result for `observe`, a hit whose ref resolves inside the
session's own result for `find`, an `OK` outcome for `call`. The tasks use only
`focus`, so they are a probe, not a mutation.

`replay` reads the `call.start` records of a trace. Refs are session-scoped, so
it resolves each one from the before-snapshot instead: type plus name (document
order for same-named rows), then `FIND` on the live desktop; window refs resolve
by title. It compares status, error code and delta counts, and stops at the first
divergence because later steps would then run against a different state. Both
`eval` and `replay` write a trace of their own run unless `--no-trace` is given.

`heal` runs a `run` script, observes the target window first (so the session
holds the recipe for every ref), and when a step fails it rebuilds the intended
element from that recipe - role, name, entity and the capability the step wants -
through the runtime's locator. Exactly one match produces a patch and one re-run;
a tie is reported with its candidates and nothing is acted on. The runtime uses
the same locator internally as the second stale-ref strategy, but it stays
internal: no `locate` tool is exposed on the model surface.

## Documentation

| Document | Content |
| --- | --- |
| [docs/what-the-llm-sees.md](docs/what-the-llm-sees.md) | one window and its tool calls, with real output |
| [docs/interactive-usage.md](docs/interactive-usage.md) | REPL, one-shot CLI and script usage |
| [docs/packs.md](docs/packs.md) | App Pack authoring |
| [docs/child-session.md](docs/child-session.md) | child session design, costs and rejected approaches |
| [docs/text-input-channels.md](docs/text-input-channels.md) | text input routes and their costs |
| [docs/optimization-plan.md](docs/optimization-plan.md) | measurements, changes and remaining work |
| [docs/viewport-capture-proposal.md](docs/viewport-capture-proposal.md) | viewport collection measurements and design |
| [docs/scenario-suite.md](docs/scenario-suite.md) | acceptance screens and pass criteria |
| [docs/benchmark-50-tasks.md](docs/benchmark-50-tasks.md) | 50 user tasks and measured coverage |

## Repository layout

```
somnus/
├── ir/            node / capability / action value objects
├── providers/     base.py  mock.py
│                  windows/{uia,cache,executor,events,keyboard,workspace}.py
├── compiler/      normalize → simplify/distill → names → twins → match → compile
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry locator resolver settle verifier delta policy session
├── session/       child session: childsession setup credentials rdp launch
│                  bridge wire peerprovider peerstart lifecycle
├── selfcheck/     doctor, eval, replay and heal (host-side, no model)
├── packs/         generic.py restaurant_demo.py
├── protocol/      tools.py policy.py help.py mcp_server.py jsonrpc.py
│                  system_prompt.md agent/*.md
├── config.py      persistent settings
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
apps/scenario-suite/    WPF (.NET 8) acceptance app
tools/rdp-host/         .NET (WinForms) OLE container for the RDP control
docs/                   documents listed above
examples/               agent loop, contract check, task evaluation
bench/                  benchmark gates and LLM control demos
tests/{fixtures,scenarios,golden,compiler,projection,runtime,providers,e2e}
```

Dependency direction: `providers → compiler → projection → serialization →
protocol`; `runtime` depends on all of them. No module imports an LLM.

### Acceptance app

`apps/scenario-suite` contains five screens, each aimed at one class of
difficulty:

| Screen | Contents | What it exercises |
| --- | --- | --- |
| `store` | search box, 240-row scrollable catalog, cart | viewport folding (`rows=` / `visible=`), `FIND` for off-screen rows, native search write |
| `form` | labelled fields, checkbox, combo, disabled button, validated submit | writable vs read-only fields, validation, submit record |
| `chat` | self-drawn composer, history, send button, send log | `set_text` on a control with no writable interface |
| `console` | text box composer and contact list | route selection: a control with its own write interface does not use synthetic channels |
| `tree` | nested expandable tree | compression by expansion level |

Each screen exists twice: as a real WPF window (`ScenarioSuite.exe <screen>`)
and as a fixture (`tests/fixtures/scenarios.py`). `tests/scenarios/` therefore
runs anywhere. `tests/e2e/test_windows_scenarios.py` verifies that the fixtures
match the live controls.

```bash
somnus --scenario chat observe
somnus --scenario store repl
somnus --scenario tree --allow-clipboard repl
```

### App Packs

A pack adds a domain vocabulary to an application (`PRODUCT p1 +add`). A window
without a matching pack is compiled generically and remains operable; see
Generic mode.

```python
from somnus.packs.restaurant_demo import build_pack
from somnus.runtime.session import Session
from somnus.providers.mock import MockProvider

session = Session(provider, pack=build_pack())
```

Authoring guide: [docs/packs.md](docs/packs.md).

## Testing

```bash
pytest                      # portable suite, including the scenario suite
pytest tests/scenarios      # scenario suite on fixtures
pytest --update-golden      # rewrite golden SUI-Text fixtures
dotnet build apps/scenario-suite
pytest -m windows           # real windows, Windows runner only
```

The portable suite (`tests/scenarios/`) drives the five acceptance screens
from fixtures on any platform. The real-window suite
(`tests/e2e/test_windows_scenarios.py`) drives the same screens through the
real UIA provider. `MockProvider` replays recorded `RawTree` fixtures and
applies scripted mutations, including per-row behaviour through a named
callback.

Measured on this machine: 788 passed in the portable suite. The real-window
suite (`pytest -m windows`) also passes; the last recorded run took about
62–66 s inside the child session, with no `window ... did not appear` timeout. A UWP app (Calculator) started and was
read in that child desktop; `OBSERVE full` reported 34 buttons.

## Metrics

Acceptance gates (`python -m somnus.cli bench --runs 100`, mock/fixtures):

| Metric | Target | Measured (100 runs) |
| --- | --- | --- |
| Zero vision | 0 vision/coordinate calls | 0 (structural) |
| Information isolation | 0 native ids in model context | 0 (leak scan) |
| Compression ratio | ≥ 10× | 1373.6× (minimum, fully expanded tree) |
| Action success | ≥ 99% | 100% |
| Task success | ≥ 90% | 100% (median 3 steps) |
| L0 observation | ≤ 300 tokens | 91 tokens (median and maximum) |

Small, untruncated viewport windows may raise the L0 budget to 80 nodes / depth
10 / 700 tokens so their controls stay visible.

The isolation check enumerates the actual automation ids, class names and
runtime ids of the tree under test and asserts that none appears in the text
the model receives. Application data that resembles an id, such as a form's
`REQ-0001`, is not counted as a leak.

### Measured performance on real windows

Same machine, Windows 11, `apps/scenario-suite` store screen (240-row
non-virtualised `ItemsControl`, 1467-node raw tree), real UIA provider,
i5-13500HX / 2560×1440@96DPI. `CALL` ranges come from repeated runs; the
`observe` figure is a single run.

| Scenario | Before | After | Change |
| --- | --- | --- | --- |
| `observe` the store | 3385 ms | 594 ms (−82.5%) | viewport capture: skeleton, row peek, visible rows read in full |
| `CALL` on the store, unchanged tree | ~6 s | 686–706 ms | signature probe; 0 full snapshots when nothing changed |
| Signature probe itself | 1622–1938 ms | 384–404 ms | probe reads signature fields on the visible set |
| `CALL` on a small window | 423 ms | 215–231 ms | fewer settle frames (`quiet=40 ms`, `poll=10 ms`) |
| Token cost of the store `observe` | 292 | 292 | unchanged |
| Desktop change check (`top_level_signature`) | — | p50 0.31–0.52 ms (max ≤2.15 ms) | Win32 `(hwnd, title)` walk; `list_windows` p50 70–78 ms |
| `CALL` that writes, title unchanged | — | 81–104 ms total / 0.6–0.9 ms enum | desktop signature matched; enumeration skipped |
| `CALL` that writes, title changed | — | 78–104 ms enum / 154–238 ms total | title change changes the window list; full enumeration |
| `observe()` window enumeration | — | 78–102 ms | observations enumerate fully |

A call that changes the store still needs one full snapshot and has not been
re-measured after this change. `SOMNUS_PROFILE=1` prints the per-stage split
(enum / snapshot / compile / settle).

## License

MIT.
