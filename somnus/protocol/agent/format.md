# SUI-Text

One line per node. Default capabilities and default states are omitted, so a
line shows only what is worth acting on.

```
@rev 7
WINDOWS w1 "Demo" | w2 "Docs" active
WINDOW w1 "Demo" active
  SCREEN s1 "menu"
    LIST l1 "Products" count=3 entity=product +find
      PRODUCT p1 "Big Mac" price=5.99 +add +customize(size:small|medium|large)
    CART c1 "Cart" count=0 total=0 +open
    CHECKOUT b5 "Checkout" +submit!
  ? r3 canvas reason=visual
```

`TYPE REF "Name" attr=value flags +caps`

- `+cap` is callable on the node that prints it: `CALL p1.add()`.
- `+submit!` is critical: it runs directly; there is no confirmation gate.
- `count=N` is the real total (`?` = unknown); `visible=N` is on screen;
  `scroll=5%` is the scroll position.
- `… +N more` rows are folded. `find` reaches them; `OBSERVE w1 expand l1`
  asks the collector to read one page of a container.
- `+find` on a container means more rows exist.
- `? ref role reason=visual` cannot be read without vision: report it, do not
  guess.
- Attribute values are quoted: `value="…" len=N`. `observe` and `inspect`
  print an 80-character preview; `read` returns 200 characters from at most
  4096 (`truncated=true`). Longer values use the `len= / preview=` form.
- Quoted strings escape `\\`, `\"`, `\r`, `\n`, `\t`; the parser decodes only
  `\\` and `\"`. A node is always exactly one line.

## WINDOWS line

`WINDOWS` lists up to six top-level windows, exactly one marked `active`
(= focused); extras fold as `+N more`. `OBSERVE windows` returns the list
alone, as `WINDOWS count=N` followed by `WINDOW wN "title"` lines.

## Refs

A ref (`w1`, `p1`) lives for one session and survives repaints. A stale ref
gives `STALE_TARGET`; `find` again. A desktop switch reallocates every ref.
