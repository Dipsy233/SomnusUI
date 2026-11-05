# SUI-Text grammar

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
2. **Implicit capabilities are omitted** - any node gets `focus`; readable
   nodes (`Value`/`Text` pattern, or a named `TEXT`/`STATUSBAR`/`HEADING`/
   `PARAGRAPH`) get `read`; controls exposing one of `Invoke`/`LegacyIAccessible`/
   `Toggle`/`SelectionItem`/`GridItem`/`Value`/`RangeValue`/`ExpandCollapse`,
   except `MENU`/`MENUITEM`, get `context_menu`; `LINK`/`MENUITEM` and any node
   with `Invoke`/`LegacyIAccessible` get `activate`; `CHECKBOX`/`SWITCH` or a
   `Toggle` pattern gets `toggle`; `SELECT`/`LISTITEM`/`TAB`/`RADIO`/`ROW`/
   `TREEITEM` or `SelectionItem`/`GridItem` gets `select`; `LIST`/`TREE`/
   `TABLE`/`PANE` or `Scroll`/`ScrollItem` gets `scroll(to=up|down|top|bottom)`.
   The system prompt carries this table. They are always available to the
   resolver regardless of printing.

   An implicit capability is one the node's role or patterns guarantee, so the
   omitted set and the always-present set are the same thing. `set_text` is
   therefore **never** implicit: a field is an `INPUT` whether or not it accepts
   text, so hiding the capability would render a writable field and a read-only
   one identically. It is printed on every input that has it, and its absence on
   an `INPUT` means exactly what it says - the field cannot be written (it is
   `readonly`/`disabled`). The same rule applies to any capability whose absence
   is itself a signal (see `NEVER_IMPLICIT_CAPS` in `serialize.py`).

### Gesture semantics (`select` vs `activate` vs `context_menu`)

These three are distinct, and conflating them is a real bug class:

| Capability | Native pattern | Meaning |
| --- | --- | --- |
| `select` | SelectionItem | highlight/select - what a **single** click does |
| `activate` | Invoke | the element's **default action** - what a **double** click does |
| `context_menu` | `IUIAutomationElement3::ShowContextMenu` | open the element's context menu - what a **right** click does |

A capability is printed only when the platform offers it. `activate` is derived
from a real `Invoke` pattern (or the `LegacyIAccessible` fallback the executor
uses), **never** from the role: a `role=button` element that exposes no default
action would otherwise be handed a capability whose only possible outcome is
`ACTION_NOT_SUPPORTED`, while the gesture that does work (`expand`/`collapse`)
goes unmentioned. `link` and `menuitem` are the exceptions, because the
accessibility layer itself activates them.

`context_menu` is the one capability that cannot be proven up front: UIA exposes
no "has a context menu" query, and `ShowContextMenu` is callable on any element
while silently doing nothing when the application registered no menu. It is
therefore an **offer**, and the runtime reports the outcome honestly instead:
a call that produced no menu is `OK_UNVERIFIED`, naming a direct capability
when one exists. A menu counts as produced only when it was
not in the tree before the call, so a window with a permanent `MENU` node (File
Explorer's system menu, Notepad's `MENU "File"`) does not make every
`context_menu` look successful.

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
4. **Strings are double-quoted**, escape `\\`, `\"`, `\r`, `\n` and `\t`,
   clip at 80 chars with `…`. A node is always exactly one line: Windows text
   surfaces report a line break as a bare CR, so both `\r` and `\t` must appear
   escaped. Previews (and `preview=` in particular) are for reading; the parser
   decodes only `\\` and `\"`, so escaped whitespace is display syntax, not an
   argument that round-trips.
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
await   := "AWAIT" [TYPE] STRING ["in" REF] ["timeout=" NUMBER]
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

## Batch calls

The `call` tool accepts several `CALL` statements, one per line (blank lines
and whole-line `#` comments ignored, at most 20). They run **in order in one
round trip**: every step is resolved and authorised against the compilation as
it stands before the first one executes (a step that could never work refuses
the whole batch while refusing it is free), the steps then run without
settling between them, and the batch ends with one settle, one delta, one rev.

```
batch    := statement (NEWLINE statement)*      # max 20, CALL statements only
statement:= action | "#" ... | ""
```

The splitting is quote- and paren-aware: a statement may contain a real line
feed inside a quoted argument (JSON delivers one unescaped) and is not cut
there.

Two statements are refused at the dry run, with the reason:

- `CALL wN.focus()` — switching windows re-allocates refs;
- `CALL ref.context_menu()` — the menu only exists after a settle, so no later
  step could name it.

Consequently every step must name an element that exists **when the batch
starts**: a flow that opens a menu or a dialog and then acts on what appeared
is separate calls.

Responses:

```
BATCH OK steps=2/2 rev=7
  1. CALL q1.set_text("张三") -> ok
  2. CALL b5.activate() -> ok
~ q1 text="张三"
~ c1 count 0→2
```

```
BATCH PARTIAL steps=1/2 failed-at=2 rev=7
  1. CALL p1.add() -> ok
  2. CALL p2.add() -> ERR TARGET_DISABLED
ERR TARGET_DISABLED p2 hint=inspect msg="..."
~ steps 1-1 really ran; fix the failing step and continue with fresh CALL statements
~ c1 count 0→1
```

`steps=` counts what was performed. Step states: `ok`, `dispatched` (sent, the
outcome is being settled - a modal may be opening, which stops the batch at
`stopped-at=N`), `ERR <code>`, `not-run`. The delta covers the steps that
actually ran; steps that never ran have their effects excluded from
verification. Cumulative effects are verified as a sum per field: two steps
each declaring `count += 1` expect the final value to be the pre-batch
baseline + 2.

## Scripts

`run` takes a script of the same `CALL` statements plus `AWAIT`, one per line,
at most 40. `AWAIT` waits for an element an earlier step created and binds it to
`$it`; the next step names it as `CALL $it.cap(args)`. `$it` is invalid before
the `AWAIT` that binds it (`INVALID_ARGUMENT`), and there is no other binding.

```
script  := step (NEWLINE step)*              # max 40
step    := action | await | "$it" "." CAP "(" [args] ")"
```

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
```

`AWAIT` is a `FIND` plus waiting: same TYPE/name/scope matching rules, but the
name **must** be quoted. `timeout=` defaults to 2s; `timeout=0` is one search
with no wait. The first hit is the binding - name the TYPE, or scope with
`in REF`, when several elements share the name.

Execution has two modes, and the script's own text selects it:

- **No `AWAIT`** - nothing depends on an earlier step's effect, so the batch
  path applies unchanged: every step is resolved and authorised before the
  first runs, then one settle, one delta, one rev. A step that could never
  work refuses the whole script while refusing is still free.
- **With `AWAIT`** - each step is resolved when it runs, because the element a
  later step names does not exist until an earlier one settled. Every step
  settles before the next, so the foreground and the settle happen inside this
  one call. The first failure stops the script: the reply says how many steps
  ran, reports their net delta, and prints the failing step's error and hint.

`window=REF` focuses that window before the first step. `CALL wN.focus()` is
refused as a step (it re-allocates refs and discards `$it`).

```
RUN OK steps=3/3 rev=9
  1. CALL mb1.expand() -> ok
  2. AWAIT MENUITEM "新建" timeout=2 -> ok ($it=mi5)
  3. CALL $it.activate() -> ok
~ t1 len 0→4
```

```
RUN PARTIAL steps=1/3 failed-at=2 rev=8
  1. CALL mb1.expand() -> ok
  2. AWAIT MENUITEM "保存" timeout=2 -> ERR TARGET_NOT_FOUND
  3. CALL $it.activate() -> not-run
ERR TARGET_NOT_FOUND after=2.0s hint=observe msg="AWAIT MENUITEM \"保存\" found nothing; the element did not appear"
~ steps 1-1 really ran; fix the failing step and continue with a fresh RUN
```

`context_menu` is allowed in a script with an `AWAIT` (that is exactly the
flow it exists for): the menu is a real step that a later `AWAIT` can find.

A critical (`!`) capability executes **directly** - in `CALL`, in a batch and
in a script alike. This build has no confirmation gate, so the `!` marker is a
description of the capability, never a permission request.

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
| `PERMISSION_DENIED` | allow-list / UIPI | `report-user` |
| `NATIVE_EXECUTION_FAILED` | pattern raised | `retry-once` |
| `TIMEOUT` | exec/settle timed out | `observe` |
| `EFFECT_NOT_CONFIRMED` | declared effect absent | `observe` |
| `UNKNOWN_CONTENT` | needs vision | `report-unknown` |

Errors are one line: `ERR CODE ref hint=...`; stack traces go to the log only.
