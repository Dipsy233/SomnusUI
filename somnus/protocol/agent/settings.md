# Settings

`configure` tunes call timing for the current session.

| Key | Default | Range | Meaning |
| --- | --- | --- | --- |
| `settle_quiet_ms` | 40 | 10–1000 | how long the UI must stay unchanged before a call is done |
| `settle_poll_ms` | 10 | 1–100 | probe spacing |
| `settle_timeout_s` | 3.0 | 0.5–30 | cap for one settle; on timeout the reply says `settled=false` and still carries the delta |
| `post_action_enum` | on | bool | re-enumerate windows after an action so an opened window is reported |

```
CONFIGURE quiet_ms=80 poll_ms=20 post_action_enum=off
→ SETTINGS quiet_ms=80 poll_ms=20 timeout_s=3.0 post_action_enum=off scope=session
```

Smaller `quiet_ms` is faster; too small can read an animation mid-flight.
`persist=true` writes the config file; `reset=true` returns to the startup
values. One invalid value refuses the whole patch and nothing changes.
