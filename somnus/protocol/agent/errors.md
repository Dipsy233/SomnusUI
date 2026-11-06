# Replies and errors

```
OK rev=8
~ c1 count 0→1 total 0→5.99
+ CARTITEM i1 "Big Mac" qty=1 in=c1
- d2
```

`~` changed, `+` added, `-` removed. Trust the delta over the status word.

| Status | Meaning |
| --- | --- |
| `OK` | ran and produced a verifiable change |
| `OK_UNVERIFIED` | ran, no verifiable effect seen |
| `BATCH OK` / `BATCH PARTIAL` | one line per statement; PARTIAL names the failing step |
| `RUN OK` / `RUN PARTIAL` | a script ran; PARTIAL adds `failed-at=K` |

`ERR CODE ref hint=…`

| Code | Do |
| --- | --- |
| `PARSE_ERROR` | fix the syntax |
| `TARGET_NOT_FOUND` / `STALE_TARGET` | `find` or `observe` |
| `TARGET_AMBIGUOUS` | refine the query |
| `CAPABILITY_NOT_FOUND` / `INVALID_ARGUMENT` | `inspect` |
| `ACTION_NOT_SUPPORTED` | no usable interface; report, do not retry |
| `PERMISSION_DENIED` | report, do not retry |
| `TARGET_DISABLED` | satisfy the precondition |
| `NOT_ACTIONABLE` | `state=…` was still blocking after the short wait; do not retry |
| `EFFECT_NOT_CONFIRMED` / `TIMEOUT` | `observe` and decide |

`ACTION_NOT_SUPPORTED` on `activate` with `hint=try select` means the node
offers another verb: the runtime does not switch verbs by itself.

`find` prints at most 50 hits; extras fold as `… +N more`. A timeout hit
produced by one escalated full read is marked `(full read)`.

## Limits

`call` 20 statements, `run` 40 steps, strings 80 characters. `read` returns a
200-character preview from at most 4096 characters (`truncated=true`); use
`INSPECT start/end` to read more.
