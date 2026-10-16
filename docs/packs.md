# App Packs

A **pack** teaches SomnusUI a domain vocabulary for one application: it turns
the generic control tree (`group` / `text` / `button`) into named entities with
capabilities (`PRODUCT p1 "Big Mac" +add`).

A pack is **optional**. Without one, the runtime still compiles every control
generically and can reach and operate it; a pack adds a vocabulary the generic
path cannot infer (which button means "add to cart", what a row's price is).
Use `--no-pack` to drive an application with no pack at all.

A pack can be written as a **single YAML file** (loaded with
`somnus.compiler.yaml_pack.load_pack_file`) or as a Python module (the built-in
`restaurant-demo` pack is one). The YAML loader is a soft dependency: it needs
PyYAML.

## File shape

```yaml
app:
  id: restaurant-demo            # required, unique
  version: "0.2.0"
  match:
    process: "RestaurantDemo.exe" # substring match, case-insensitive
    # window_class: "Notepad"      # optional second filter
    # "*" matches any process (used by the generic pack)

projection:                       # ordered; later rules win
  - { select: "role=toolbar", rule: "collapse" }
  - { select: "name=导航", rule: "prune" }

named_refs:                       # optional: named anchors
  editor: { role: text, patterns: [value, text] }

entities:
  <entity-name>:
    match: <MatchRule>
    key: "$self.automation_id"    # fingerprint primary key
    attrs: { ... }
    caps: { ... }
```

## Matching

Match by **process**, never by window title: titles are localised and change
with the open document, which would invalidate every ref.

`MatchRule` fields:

| Field | Meaning |
| --- | --- |
| `role` | portable role, or a list (`[group, listitem]`) |
| `name` / `value` | regex, or `/regex/flags` (`i`, `s`, `m`) |
| `has_name` | `true` / `false` |
| `min_children` | minimum child count |
| `patterns` | native patterns that must all be present (`[value, text]`) |
| `has` | required descendants; each may carry `as: <member>` |

Roles and patterns are the *normalised* values from
`compiler/normalize.py` / `providers/windows/uia.py` — e.g. UIA's `dataitem`
becomes `listitem`, `document` becomes `text`.

### `has` and members

```yaml
match:
  role: [group, listitem]
  has:
    - { role: text, as: title }
    - { role: text, name: "/\\d+[.,]\\d{2}/", as: price_text }
    - { role: button, name: "/^(add|加入)$/i", as: add_btn }
```

Each `as` binds the first matching descendant to a member name usable in
`key`, `attrs` and `caps`.

## Attribute expressions

`attrs` values are either literals, a `$member.attr` expression, or an object
with `from` / `regex` / `cast` / `default`:

| Attribute | Meaning |
| --- | --- |
| `automation_id` / `id` | native automation id |
| `name` | the node's name |
| `value` | the node's value |
| `class_name` | native class |
| `role` | portable role |

`from` also accepts `descendant(role=text, name=/re/)` to pull a value from the
first matching descendant.

```yaml
attrs:
  name: "$title.name"
  price:
    from: "$price_text.name"
    regex: "(\\d+[.,]\\d{2})"
    cast: number          # number | int
    default: 0
```

## Capabilities

```yaml
caps:
  add:
    via: ["$add_btn.activate"]     # an ordered list of primitive steps
    effects: ["cart.count += 1"]   # verifiable post-conditions
    risk: normal                   # safe | normal | critical
    params:
      size: { type: enum, enum: [small, medium, large] }
```

* **`via`** names a member and a primitive: `$self.activate`,
  `$add_btn.activate`, `$self.set_text`. The primitive vocabulary is fixed
  (`activate, focus, read, set_text, toggle, select, expand, collapse,
  increment, decrement, scroll, open, close`). `type_text`, `set_text_hwnd`
  and `paste_text` exist only as routing steps inside a capability, never as
  capabilities the LLM can call - see below.
* **`effects`** is how the verifier knows an action worked. Supported forms:
  `ref.field += N`, `-=`, `=`, `!=`, `contains`, `startswith`. Effects on
  fields the runtime cannot observe are treated as *unverifiable*, and the
  action returns `OK_UNVERIFIED` rather than failing.
* **`risk: critical`** (or a critical capability name) forces
  `CONFIRMATION_REQUIRED` unless the host pre-confirms.

The `key` field is the fingerprint primary key: prefer `$self.automation_id`
or a pack member's automation id. When `key` resolves, volatile names are
excluded from the fingerprint, so a changing label never invalidates a ref.

### Conditional steps and the synthetic-input fallbacks

A `via` entry may be a mapping instead of a string, gating the step on the
native patterns the target exposes. The condition is evaluated at **compile
time**, so the resolver only ever receives steps the target can actually run,
and the LLM never sees the fallback primitive.

```yaml
caps:
  set_text:
    params: { text: { type: string, required: true } }
    via:
      # Prefer the native Value pattern ...
      - { target: "$self", primitive: set_text, value: "$text", when_has: value }
      # ... then the element's own HWND (no focus, no clipboard) ...
      - { target: "$self", primitive: set_text_hwnd, value: "$text", when_missing: value }
      # ... then the clipboard channel (--allow-clipboard) ...
      - { target: "$self", primitive: paste_text, value: "$text", when_missing: value }
      # ... otherwise focus the control and type (declarative keyboard fallback).
      # `type_text` and `paste_text` are not capabilities the LLM can call.
      - { target: "$self", primitive: type_text, value: "$text", when_missing: value }
```

`when_has` / `when_missing` name a native pattern (`value`, `invoke`,
`text`, ...). Only one of the two steps survives compilation for any given
target, which is how the same pack works on both a native edit control and a
custom-drawn one that only accepts keystrokes.

The built-in `set_text` chain is exactly this shape, in the order the runtime
tries them: `set_text` (Value pattern) → `set_text_hwnd` (element-level HWND) →
`paste_text` (clipboard) → `type_text` (keyboard). The last two need a host flag
(`--allow-clipboard` / `--allow-keyboard`) because they seize the screen's
foreground; see
[interface-capability-report.md](interface-capability-report.md) for why that
ordering is the one that costs the user least.

## Writing a pack for a new app

**A pack is optional.** Without one, the compiler runs in *Generic Mode*: it
still normalises roles, derives capabilities from the native patterns, builds
relations and projects the tree. Any app whose controls expose standard UIA
patterns is usable the moment it opens.

What a pack adds is *certainty*, not reachability:

| | Generic Mode | App Mode |
| --- | --- | --- |
| Bluetooth switch | `BUTTON b10 "蓝牙" +toggle` | `TOGGLE_SETTING b10 "蓝牙" +toggle` |
| What the agent must do | read the name and infer | the type states it |
| Match basis | name + native pattern | `class_name` / `automation_id` (language-independent) |
| Domain actions | the primitive vocabulary | semantic caps (`add`, `submit`, ...) |

Generic capabilities are derived only from patterns, so they never guess
(generic capabilities never guess); a control that exposes no pattern gets no capability
rather than a wrong one.

### Write a pack when

- the app has domain objects worth naming (`product`, `invoice`, `file`),
- the same control type must be told apart by class (a WinUI `ToggleSwitch`
  next to a plain `Button`),
- a capability needs several steps or an effect to verify,
- an action is dangerous and must be gated (`risk: critical`).

### Steps

1. Record the app's tree:

   ```bash
   somnus windows
   somnus --provider uia record w1 -o app.json
   ```

2. Inspect it to find stable, localised-independent anchors — prefer
   `patterns`, `role` and `automation_id` over names:

   ```python
   from somnus.providers.mock import load_tree
   tree = load_tree("app.json")
   for n in tree.walk():
       print(n.role, n.native.automation_id, n.name, sorted(n.patterns))
   ```

3. Write the pack, then check it without a desktop:

   ```bash
   somnus --provider mock --fixture app.json --pack my-app observe
   ```

4. Keep a trimmed fixture under `tests/` and assert the entities in a test, so
   the pack is verified in CI (see `tests/compiler/test_yaml_pack.py`).

## UWP windows (ApplicationFrameHost)

A UWP app appears in the tree as an `ApplicationFrameWindow` whose process is
`ApplicationFrameHost.exe`, not the app. Two things make it usable:

1. **Discovery.** The desktop root is walked with `ControlViewWalker`, which
   surfaces UWP frames; a ControlType filter on the root's children does not.
   Win32 `EnumWindows` fills in anything the walker stops short of.
2. **Identity.** When a window's process is `ApplicationFrameHost.exe`, the
   provider walks the frame's immediate children for one with a different pid -
   that child belongs to the real app. The window's `process` is then reported
   as e.g. `SystemSettings.exe`, so `match: { process: systemsettings.exe }`
   works.

Both behaviours are covered by `tests/e2e/test_windows_windows.py`.
