# Batches and scripts

## `call` with several statements

Several `CALL` lines in one `call` run **in order in one round trip**: one
settle, one delta. Every step is validated before the first runs; steps must
name elements that exist **when the batch starts**.

```
CALL q1.set_text("张三")
CALL q2.set_text("a@b.c")
CALL b5.activate()
→ BATCH OK steps=3/3 rev=7
    1. CALL q1.set_text("张三") -> ok
  ~ q1 text="张三"
```

A step that cannot work refuses the whole batch before any state changes. A
failing step stops it: `BATCH PARTIAL steps=1/3 failed-at=2` names the failing
step and carries the delta for what ran (`not-run` = never sent). `wN.focus()`
and `context_menu` are refused inside a batch.

Open a menu and then act on its items requires two calls (the items exist only
after the settle), or one `run`.

## `run`: a script in one call

`run` takes one step per line: `CALL`, `AWAIT [TYPE] "name" [in ref]
[timeout=s]`, then `CALL $it.cap(args)`.

```
CALL mb1.expand()
AWAIT MENUITEM "新建" timeout=2
CALL $it.activate()
→ RUN OK steps=3/3 rev=9
```

- `AWAIT` binds `$it` to the first match; default timeout 2 s, `timeout=0`
  searches once. It re-reads the viewport until the match appears.
- A failing step stops the run: `RUN PARTIAL steps=2/3 failed-at=3`, then the
  step lines and the merged delta for the steps that ran.
- A cross-window flow must name the window explicitly: a switch reallocates
  refs. The delta is the net difference between the start and the end.
- Limits: 40 steps. `$it` is the first hit, not a collection.

## Control flow inside `run`

Three more line forms organise steps. None of them is a new capability: every
executed step goes through the same resolution and policy as a plain CALL.

```
EACH BUTTON "Add" in l1 max=10
CALL $it.activate()
END

TRY AWAIT DIALOG "提示" timeout=0
IF ok CALL $it.close()
```

- `EACH` re-evaluates its query at every iteration and binds `$it` to the next
  current match, in document order. `max` defaults to 20. Zero matches: the
  body is skipped (`ok (0 matches)`). EACH cannot nest.
- `TRY` before CALL or AWAIT records a failure (`ERR <code>`) and runs on.
- `IF ok` / `IF failed` runs its step only when the previously *executed* step
  ended in that state; a `skipped` step does not become that reference. IF
  cannot open a script or a loop body.
- Executed steps share one budget of 40; reaching it stops the run with
  `RUN PARTIAL ... reason=budget`. Step lines inside a loop carry the
  iteration number (`4.2` = textual step 4, iteration 2).
