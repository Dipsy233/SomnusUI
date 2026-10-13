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
├── providers/     base.py  mock.py  windows/{uia,cache,executor,events}.py
├── compiler/      normalize → caps → relations → simplify → match → compile
│                  yaml_pack.py (v0.2 YAML pack loader)
├── projection/    rules.py budget.py engine.py
├── sui/           serialize.py parse.py grammar.md
├── runtime/       registry resolver settle verifier delta policy session
├── packs/         generic.py restaurant_demo.py (v0.1)
│                  notepad.pack.yaml explorer.pack.yaml (v0.2)
├── protocol/      tools.py mcp_server.py jsonrpc.py
└── cli.py
apps/restaurant-demo/   WPF (.NET 8) acceptance app
docs/packs.md           how to write an App Pack
bench/                  metrics harness + LLM control demos
tests/{fixtures,golden,compiler,projection,runtime,e2e}/
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

## License

MIT. Design borrows from `desktop-cli` (GPL-3.0 – **design only, no code**),
`Touchpoint` (MIT) and `agent-desktop`; see the development document section 20.
