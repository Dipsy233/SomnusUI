# SUI-Text grammar (v0.2)

The single LLM-facing format. Internal formats are JSON; SUI-Text is the wire
format. All rules below are enforced by `somnus/sui/serialize.py` and
`somnus/sui/parse.py`.

## Observation

```
doc     := header line*
header  := "@rev" INT
line    := INDENT* node
node    := TYPE REF [STRING] (attr | flag)* cap* [collapse]
TYPE    := entity type (uppercase) | role (uppercase) | layer (uppercase)
REF     := [a-z]{1,3}[0-9]+
attr    := KEY "=" (NUMBER | STRING | BARE | "?")
flag    := disabled | readonly | required | invalid | selected | checked
         | unchecked | mixed | expanded | collapsed | focused | loading | active
cap     := "+" NAME ["(" PARAMS ")"] ["!"]
collapse:= "+" INT "more"
unknown := "?" REF ROLE "reason=" REASON
more    := "…" "+" INT "more"
INDENT  := 2 spaces per level
```

### Rules

1. **Capabilities live on the node line.**
2. **Implicit capabilities are omitted** - `focus`/`read` everywhere;
   `context_menu` on any control; `activate` on `BUTTON`, `set_text` on `INPUT`,
   `toggle` on `CHECKBOX`, `select` on `LISTITEM`, `scroll` on containers. The
   system prompt carries this table. They are always available to the resolver
   regardless of printing.

### Gesture semantics (`select` vs `activate` vs `context_menu`)

These three are distinct, and conflating them is a real bug class:

| Capability | Native pattern | Meaning |
| --- | --- | --- |
| `select` | SelectionItem | highlight/select - what a **single** click does |
| `activate` | Invoke | the element's **default action** - what a **double** click does |
| `context_menu` | `IUIAutomationElement3::ShowContextMenu` | open the element's context menu - what a **right** click does |

`activate` is **not** a click. Invoke means "perform the single unambiguous
action this control represents": press for a button, but **open** for a file or
list row. Measured in File Explorer, on the same file:

```
CALL li11.select()    → highlights the file (selection only)
CALL li11.activate()  → opens the file in its associated application
```

A control whose Invoke is a decoration rather than an action (a chat list row
that answers Invoke by highlighting and nothing else) is reached through
`context_menu`: the menu it exposes carries the real command, and the menu items
are ordinary UIA items whose `activate` works. `context_menu` is a semantic UIA
call, not synthetic input - it moves no pointer and needs no foreground window.

So a row that should only be *picked* must be driven with `select`; `activate`
opens it. Applications differ - some answer Invoke by only highlighting - which
is why the delta, not the status word, is the source of truth.
3. **`enabled` is the default state** and never printed; only deviations appear.
4. **Strings are double-quoted**, escape `\"` and `\n`, clip at 80 chars with `…`.
5. **Numeric values drop the currency symbol** into an attribute (`price=5.99`,
   optional `cur=USD`).
6. **Refs are ASCII**; names keep their original language.
7. **Bare attrs** are unquoted enum-ish identifiers: `entity=product`,
   `cur=USD`, `type=primary`.
8. Anything unreadable without vision is rendered as
   `? r9 canvas reason=visual`, never guessed.

### Examples

```
@rev 7
SCREEN s1 "menu"
  PRODUCT p1 "Big Mac" price=5.99 +add +customize(size)
  PRODUCT p2 "Fries" price=2.49 +add(size)
  … +125 more find
  CART c1 count=0 total=0 +open
  BUTTON b9 "Checkout" +submit!
  ? r3 canvas reason=visual

TEXT t1 len=5230 preview="…"
LIST l1 "Products" count=128 entity=product +find
```

## Actions

The only action syntax is `CALL`. Primitive verbs (`click`, `uia_invoke`) and
native identifiers (`AutomationId`, RuntimeId, coordinates) are rejected at
parse time with `PARSE_ERROR`.

```
action  := "CALL" REF "." CAP "(" [args] ")"
args    := VALUE | KEY "=" VALUE ("," KEY "=" VALUE)*
query   := "FIND" [REF] [TYPE] STRING ["in" REF]
inspect := "INSPECT" REF ["depth=" INT] ["range=" INT ":" INT]
abort   := "UNKNOWN" STRING
observe := "OBSERVE" [REF] ["windows" | "desktop" | "full"] ["expand" REF]
```

```
FIND product "Big Mac"
FIND w5 "库"              # scope: search inside window w5 (same as ... in w5)
CALL p1.add()
CALL p1.customize(size="large")
CALL q1.set_text("Big Mac")
INSPECT l1 depth=1
INSPECT t1 range=0:2000
OBSERVE w5 full           # the whole window tree, no folding, no budget
OBSERVE w5 expand g3      # unfold one collapsed node, keep the rest folded
OBSERVE windows           # just the window list (how you switch windows)
OBSERVE w5 desktop        # w5 plus the list of the other windows
```

Large homogeneous collections (a game list, a message list) render the first
three children as a preview and fold the rest into `\u2026 +N more`, so the agent
can tell what the collection holds without paying one line per row. Use
`OBSERVE w5 expand REF` to unfold a specific node, or `FIND` to search inside
it.

FIND is a full-text search over every *named* node in the window (all roles),
deduplicated to one hit per element with the parent row preferred. Results are
rendered with their full capability line, so the actionable gesture is always
visible.

The resolver maps `p1.add` to the entity's `product.add` rule using the ref's
entity type; the LLM never writes `product.add(target=p1)`.

## Results and deltas

```
OK rev=8
~ c1 count 0→1 total 0→5.99
+ CARTITEM i1 "Big Mac" qty=1 in=c1
- d2
```

```
OK_UNVERIFIED rev=8          # executed, no declared effects to verify
ERR STALE_TARGET p1 hint=find
ERR CONFIRMATION_REQUIRED b9.submit hint=ask-user
```

Delta line kinds:

| Kind | Meaning |
| --- | --- |
| `~ REF FIELD OLD→NEW` | value changed |
| `+ NODE ...` | node added (full node line) |
| `- REF` | node removed |

## Error codes

| Code | Meaning | Hint |
| --- | --- | --- |
| `PARSE_ERROR` | bad syntax | `fix-syntax` |
| `TARGET_NOT_FOUND` | ref unknown | `observe` |
| `TARGET_AMBIGUOUS` | multiple matches | `refine-query` |
| `STALE_TARGET` | ref invalidated | `find` |
| `CAPABILITY_NOT_FOUND` | target lacks capability | `inspect` |
| `INVALID_ARGUMENT` | bad/missing argument | `inspect` |
| `ACTION_NOT_SUPPORTED` | no native pattern | `report-user` |
| `TARGET_DISABLED` | target disabled | `inspect` |
| `CONFIRMATION_REQUIRED` | critical action needs consent | `ask-user` |
| `PERMISSION_DENIED` | allow-list / UIPI | `report-user` |
| `NATIVE_EXECUTION_FAILED` | pattern raised | `retry-once` |
| `TIMEOUT` | exec/settle timed out | `observe` |
| `EFFECT_NOT_CONFIRMED` | declared effect absent | `observe` |
| `UNKNOWN_CONTENT` | needs vision | `report-unknown` |

Errors are one line: `ERR CODE ref hint=...`; stack traces go to the log only.
