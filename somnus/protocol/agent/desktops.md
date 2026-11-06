# Two desktops

The user's desktop is **main**; **child** is a separate Windows session where
your input cannot disturb them.

| Command | Effect |
| --- | --- |
| `SWITCH main` / `SWITCH child` | move (child must be open) |
| `SESSION open` | start the child session and move in |
| `SESSION close` | end it, back to main |
| `SESSION detach` | leave it running after this process exits |

**A switch discards every ref and assigns no new ones.** Old refs give
`STALE_TARGET` until `observe`; the reply says `desktop changed; observe again`.
A switch that does not change desktop answers `OK session=… (already here)` and
leaves the current refs valid.

`launch(command, args, cwd, window, wait_s)` starts a program and returns its
`pid` plus the windows that appeared within `wait_s` (default 3 s). Window
lines are capped at six, followed by `+ N more`; more than one new window adds
`~ N windows from one launch`. `window="child"` starts it on the peer desktop
(requires `child_allow_spawn`); the reply lists titles only and ends with
`on child; switch child to address them`.

Switch when work must not disturb the user (`SESSION open` does both), or the
window is on the other side. Say which desktop you are on.
