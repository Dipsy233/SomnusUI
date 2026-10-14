# SomnusUI

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
AccessibilityProvider (Windows UIA; AX / AT-SPI / CDP later)
```

The LLM never sees pixels, coordinates, screenshots, handles, `AutomationId`s or
COM pointers. It sees entities, states, relations, capabilities and values.

## Principles

- **No Vision** – the accessibility tree is the only perception channel.
- **Low Token** – observation is projected and budgeted; L0 fits in ~200 tokens.
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

## What the LLM sees

One real window, four tool calls. Nothing else is ever shown to the model.

**① `observe`** - the current screen as capabilities:

```
@rev 1
WINDOW w1 "agent 属性" +scroll_into_view
  SCREEN s1 "网络文件和文件夹共享"
  TEXT tx3 "网络路径(N):"
  INPUT q1 "网络路径(N):" readonly          ← a field, and it is read-only
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
- **`set_text`** is derived for every text-entry surface, routed at execution
  time: a plain field writes through its Value pattern, a rich-text or
  document editor is typed into. A self-drawn editor (Chromium/Electron) is
  writable without any pack.
- **`context_menu`** is offered on every control, alongside `activate`. It is
  the escape hatch for an application that ignores the native patterns: the
  menu is opened through UIA itself (`ShowContextMenu`), so the pointer never
  moves and no keystroke is sent, and the menu's own items are ordinary menu
  items whose `activate` works. An application whose default action only
  highlights a row (a chat list) is driven this way, without synthetic input.
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

The runtime talks to one `AccessibilityProvider`. Three exist, and the compiler,
projection and runtime are identical for all of them.

| Provider | Source | Status |
| --- | --- | --- |
| `uia` | Windows UI Automation (Win32 / WPF / UWP) | v0.1 |
| `cdp` | Chrome DevTools Protocol accessibility tree (Edge / Chrome) | v0.3 |
| `mock` | recorded `RawTree` fixtures (any platform) | v0.1 |

```bash
somnus --provider uia windows          # native windows
somnus --provider cdp windows          # browser tabs
somnus --provider cdp observe          # a page, as SUI-Text
```

### Browsers (CDP)

The browser provider reads the page's own accessibility tree
(`Accessibility.getFullAXTree`) - never a screenshot, never DOM scraping - and
drives it through the DOM node the tree names (`Runtime.callFunctionOn`), so the
page's real event handlers run instead of synthetic input at coordinates. It
attaches to any page reachable on the loopback debug endpoint, including
browsers launched with `--remote-debugging-port=0` (found via their
`DevToolsActivePort` file). Requires the `[cdp]` extra.

A web form, read with no pack at all:

```
WINDOW w1 "SomnusUI CDP 测试页" active
  GROUP g1 +scroll(...)
    TEXT tx2 "用户名"
    INPUT q1 "用户名"
    TEXT tx4 "密码"
    INPUT q2 "密码"
    CHECKBOX cb1 " 记住我" unchecked
    BUTTON b1 "登录"
```

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
│                  windows/{uia,cache,executor,events,keyboard}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
│                  yaml_pack.py (v0.2 YAML pack loader)
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py (v0.1)
│                  notepad / explorer / qq / windows_settings .pack.yaml (v0.2)
├── protocol/      tools.py mcp_server.py jsonrpc.py system_prompt.md
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
docs/
  packs.md              how to write an App Pack
  interactive-usage.md  REPL vs one-shot CLI vs script
  what-the-llm-sees.md  one window, four tools, with real output
examples/               agent loop, contract check, task evaluation
bench/                  metrics harness + LLM control demos
  run.py                the benchmark gates
  demo_llm.py           end-to-end demo on the mock backend (any platform)
  demo_notepad.py       the same tools against a live Notepad
tests/{fixtures,golden,compiler,projection,runtime,providers,e2e}/
```

Dependency direction (enforced in CI): `providers → compiler → projection →
serialization → protocol`; `runtime` depends on all of them; nothing imports an
LLM.

## App Packs

A pack gives an application a domain vocabulary, turning the control tree into
entities with capabilities. v0.2 uses a single YAML file per app, loaded from
`somnus/packs/*.pack.yaml`:

```yaml
app:
  id: notepad
  match: { process: "notepad.exe" }     # by process, never by title
entities:
  document:
    match: { role: text, patterns: [value, text] }
    key: "$self.automation_id"
    caps:
      set_text:
        via: ["$self.set_text"]
        params: { text: { type: string, required: true } }
```

Shipped packs: `notepad`, `explorer`, `windows-settings` and `qq`, plus the
v0.1 `restaurant-demo` Python pack. Full authoring guide:
[`docs/packs.md`](docs/packs.md).

Packs are optional. A window with no matching pack is compiled generically -
see [Generic mode](#generic-mode).

## Testing

```bash
pytest                      # compiler/projection/runtime tests run anywhere
pytest --update-golden      # rewrite golden SUI-Text fixtures
pytest -m windows           # UIA + WPF E2E (Windows runner only)
```

The `MockProvider` replays recorded `RawTree` fixtures and applies scripted
mutations, so the compiler, projection, resolver, delta and verification paths
are all exercised without a desktop.

## Metrics (v0.1)

| Metric | Target |
| --- | --- |
| Zero Vision | 0 vision/coordinate calls |
| Information isolation | 0 native ids in LLM context |
| Overview tokens (L0) | ≤ 200 |
| Compression ratio | ≥ 10× |
| Action success | ≥ 99% |
| Task success | ≥ 90% |
| Unconfirmed critical actions | 0 |

Run with `somnus --demo bench --runs 100`.

## Driving the runtime with a real model

The four tools are exercised by a local OpenAI-compatible model. Three example
hosts live under `examples/` (the agent loop is host code, not core - dev doc
section 17):

| Script | What it measures |
| --- | --- |
| `examples/agent.py` | native function-calling loop over the four tools |
| `examples/check_contract.py` | tool-contract correctness: does the model emit valid SUI-Text for one aimed call |
| `examples/eval_agent.py` | task success, judged from runtime state (not the model's claim) |

```bash
export SOMNUS_LLM_API_KEY=...            # if the endpoint needs one
python examples/agent.py --provider uia --allow restaurantdemo.exe \
    --pack restaurant-demo --task "帮我点一个巨无霸和一份薯条，然后打开购物车"
python examples/check_contract.py --runs 5
python examples/eval_agent.py --runs 3
```

## License

MIT. Design borrows from `desktop-cli` (GPL-3.0 – **design only, no code**),
`Touchpoint` (MIT) and `agent-desktop`; see the development document section 20.
