# SomnusUI

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
somnus windows
somnus observe w1
somnus call 'CALL b1.activate()'
somnus record w1 -o fixture.json
somnus mcp           # MCP server (stdio), 4 tools
```

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
│                  notepad / explorer / windows_settings .pack.yaml (v0.2)
├── protocol/      tools.py mcp_server.py jsonrpc.py
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
docs/packs.md           how to write an App Pack
examples/               agent loop, contract check, task evaluation
bench/                  metrics harness + LLM control demos
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

Shipped packs: `notepad` and `explorer` (both verified against the live Windows
11 apps), plus the v0.1 `restaurant-demo` Python pack. Full authoring guide:
[`docs/packs.md`](docs/packs.md).

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

| Metric | Target | Demo result |
| --- | --- | --- |
| Zero Vision | 0 vision/coordinate calls | 0 |
| Information isolation | 0 native ids in LLM context | 0 |
| Overview tokens (L0) | ≤ 200 | 176 |
| Compression ratio | ≥ 10× | 41× |
| Action success | ≥ 99% | 100% |
| Task success | ≥ 90% | 100% |
| Unconfirmed critical actions | 0 | 0 |

Reproduce with `somnus --demo bench --runs 100`.

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

Measured against `hy3` (reasoning model, native tool calls):

| Check | Result |
| --- | --- |
| Tool contract (`add_product`, `open_cart`) | 10/10 |
| Task success (`one_big_mac`, `big_mac_and_fries`, `three_colas`) | 9/9 |
| Live WPF app, full acceptance transcript | completed in 3 tool calls |

On the live WPF app the model ran `observe` → `p1.add()` → `p2.add()` →
`c1.open()`, each producing the expected delta (`~ c1 count 0→1 total 0→5.99`,
then `1→2`, then a real `+ WINDOW "Cart"`), and reported completion on its own.

## License

MIT. Design borrows from `desktop-cli` (GPL-3.0 – **design only, no code**),
`Touchpoint` (MIT) and `agent-desktop`; see the development document section 20.
