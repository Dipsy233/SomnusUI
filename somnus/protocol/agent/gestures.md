# Capabilities and gestures

## Implicit capabilities

Omitted because the node's role or patterns guarantee them:

- any node: `focus`
- readable nodes (`Value`/`Text` pattern, or a named `TEXT`/`STATUSBAR`/
  `HEADING`/`PARAGRAPH`): `read`
- controls exposing one of `Invoke`/`LegacyIAccessible`/`Toggle`/
  `SelectionItem`/`GridItem`/`Value`/`RangeValue`/`ExpandCollapse`, except
  `MENU`/`MENUITEM`: `context_menu`
- `LINK`/`MENUITEM`, or any node with `Invoke`/`LegacyIAccessible`:
  `activate`; a **menu bar entry** printing `+expand` opens with `expand`
  first, its items do not exist yet
- `CHECKBOX`/`SWITCH`, or a `Toggle` pattern: `toggle`
- `SELECT` (combo)/`LISTITEM`/`TAB`/`RADIO`/`ROW`/`TREEITEM`, or
  `SelectionItem`/`GridItem`: `select`
- `LIST`/`TREE`/`TABLE`/`PANE`, or `Scroll`/`ScrollItem`:
  `scroll(to=up|down|top|bottom)`

`set_text` is **never** implicit; it answers "is this writable":

```
INPUT q1 "Search" +set_text(text)   ← writable: CALL q1.set_text("…")
INPUT q2 "Path" readonly            ← no set_text: read-only, do not write
```

Entity caps (`+add`, `+open`, `+submit`) are always printed.

## Three gestures; the runtime never picks one for you

| Cap | Really does | Human |
| --- | --- | --- |
| `select` | highlight (SelectionItem) | single click |
| `activate` | default action (Invoke) | **double** click |
| `context_menu` | open its menu (UIA `ShowContextMenu`) | right click |

**`activate` is not a click.** For a button it presses; for a file or list row
it **opens**. To pick without launching, use `select`.

`activate` is printed only when a default action exists; some controls only
offer `+expand`/`+collapse`, and it can still be refused. Trust the reply:

```
CALL mb1.expand()       ← menu bar entry: expand, not activate
+ MENUITEM mi5 "新建"    ← items arrive in this reply
CALL mi5.activate()
```

If `activate` only highlights a row, its command is under `context_menu`.
`context_menu` cannot be checked up front: `OK` means a menu appeared,
`OK_UNVERIFIED` means it produced none (the reply names a direct capability
when one exists). It moves no pointer, needs no keystroke or foreground, and
closes by itself.

## Arguments

The names after a cap are its parameters; pass `name=value`, or one value
positionally. Quote values with spaces, commas or parentheses. **`=` is the
only separator**; a write echoes what landed: `~ q1 text="Hello"`.
