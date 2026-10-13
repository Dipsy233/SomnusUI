# App Packs (v0.2)

A **pack** teaches SomnusUI a domain vocabulary for one application: it turns
the generic control tree (`group` / `text` / `button`) into named entities with
capabilities (`PRODUCT p1 "Big Mac" +add`).

v0.1 packs were Python modules. **v0.2 uses a single YAML file** per app,
discovered automatically from `somnus/packs/*.pack.yaml`. See
`somnus/packs/notepad.pack.yaml` and `somnus/packs/explorer.pack.yaml` for
complete, real-world examples.

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
with the open document, which would invalidate every ref (dev doc section 8).

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
first matching descendant (dev doc section 15).

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
  increment, decrement, scroll, open, close`). `type_text` exists only as a
  conditional fallback step - see below.
* **`effects`** is how the verifier knows an action worked. Supported forms:
  `ref.field += N`, `-=`, `=`, `!=`, `contains`, `startswith`. Effects on
  fields the runtime cannot observe are treated as *unverifiable*, and the
  action returns `OK_UNVERIFIED` rather than failing.
* **`risk: critical`** (or a critical capability name) forces
  `CONFIRMATION_REQUIRED` unless the host pre-confirms.

The `key` field is the fingerprint primary key: prefer `$self.automation_id`
or a pack member's automation id. When `key` resolves, volatile names are
excluded from the fingerprint, so a changing label never invalidates a ref.

### Conditional steps and the keyboard fallback

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
      # ... otherwise focus the control and type (declarative keyboard fallback,
      # dev doc section 12.2). `type_text` is not a capability the LLM can call.
      - { target: "$self", primitive: type_text, value: "$text", when_missing: value }
```

`when_has` / `when_missing` name a native pattern (`value`, `invoke`,
`text`, ...). Only one of the two steps survives compilation for any given
target, which is how the same pack works on both a native edit control and a
custom-drawn one that only accepts keystrokes.

## Writing a pack for a new app

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

## Verification status

| Pack | Backend | Verified |
| --- | --- | --- |
| `restaurant-demo` | Python module (v0.1) | fixture + live WPF app |
| `notepad` | YAML | live Windows 11 Notepad |
| `explorer` | YAML | live Windows 11 File Explorer |
| `generic` | YAML-less defaults | projection rules for every app |

### Windows Settings (UWP)

Settings is a UWP app: `SystemSettings.exe` is a small launcher and the real UI
lives in a window hosted by `ApplicationFrameHost.exe`. The provider handles
this - see "UWP windows" below - so a Settings pack can match on
`systemsettings.exe` exactly like any other app.

The pack itself is **not written yet**. The UIA tree could not be captured in
the development environment because UWP activation is unavailable there
(`ms-settings:` and Calculator both fail to launch), and writing a pack from
memory would violate the "never guess" rule. The steps above apply unchanged
once a real tree is captured with `somnus --provider uia record wN -o settings.json`.

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
