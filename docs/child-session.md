# Child session: a second desktop the agent can work in

The problem this exists to solve, the mechanism that turned out to work, and the
two that did not.

## The problem

An application is driven through the interfaces it publishes (`Invoke`,
`Value`, `Toggle`, ...). For a control that publishes no writable interface -
a self-drawn editor, a terminal surface, an omnibox that only commits on Enter -
the only thing that lands is synthetic input: the clipboard channel
(`allow_clipboard`) or the keyboard channel (`allow_keyboard`).

Every write first raises the target window (see
[text-input-channels.md](text-input-channels.md)); the runtime does not write
without focus, because a write that lands wherever the focus happens to be is
neither predictable nor attributable. That makes the foreground the resource the
agent needs, and a desktop has exactly one. So while the agent works, it is
competing with the person at the keyboard for it - and whoever loses stops
working. That is the disturbance a child session removes.

## What works: a child session

A **child session** is a second interactive logon for the same account. Same
files, same registry, same programs. But its own **input desktop**, its own
foreground window and its own input queue.

An agent parked there can raise windows, type and paste as much as it likes. The
user, in the console session, is unaffected. Not because the agent is polite -
because the two sessions do not share the resource they used to fight over.

Verified live, in both directions: a process in session 1 enumerates sessions 0
and 1 and nothing else; a process in session 2 enumerates sessions 0 and 2. The
accessibility tree behaves the same way. It is a kernel boundary.

## For the agent: desktop switching

The agent drives **one desktop at a time**. Desktop switching is one tool. The
agent is not given both desktops, because every ref (`w1`, `b3`, `q1`) is a
bare name that only means something relative to a desktop; two at once would
make every call ambiguous, and asking the model to disambiguate each one would
cost more than the feature is worth.

```
SWITCH main          go to the user's desktop
SWITCH child         go to the separate session (must be open)
SESSION open         start the separate session and move there
SESSION close        end it, and return
SESSION detach       leave it running after this process exits
```

A switch that changes desktop **assigns no refs**. The reply is
`OK session=… windows=N` followed by `desktop changed; observe again`. Until a
fresh `OBSERVE`, `CALL` and `INSPECT` that name an old node or window ref return
`STALE_TARGET` with `hint=observe`. A switch that does not change desktop
returns `OK session=… (already here)` and leaves the current refs valid.

## Starting programs on the child desktop

A program has to be started *inside* the child session to appear there. The
peer already runs there, so it starts the program with its own token - no
`CreateProcessAsUser`, no elevation, no token borrowing:

```bash
somnus child-session run --cwd E:/0down/SomnusUI -- python.exe -m pytest -m windows
somnus child-session run -- cmd /c start notepad.exe      # launcher exits, Notepad stays
```

`run` sends `process.start` to the peer, polls `process.status` until the child
command exits (or `--timeout`, default 600 s, kills it), then prints the exit
code and the full stdout/stderr. Output files live in
`%TEMP%\somnusui-child-run\<timestamp>\`; the last run is kept and its paths are
printed, older runs are removed.

Wire methods (not agent-facing tools):

| method | params | result |
| --- | --- | --- |
| `process.start` | `command`, `args[]`, `cwd`, `stdout_path`, `stderr_path` | `{pid}` |
| `process.status` | `pid` | `{pid, alive, exit_code}` (`exit_code` null while alive) |
| `process.kill` | `pid` | `{pid, killed}` |

Errors are `PERMISSION_DENIED` / `INVALID_ARGUMENT` / `NATIVE_EXECUTION_FAILED`
with a message. `process.kill` exists so `run --timeout` can end the command; a
program started through a launcher (`cmd /c start ...`) is left running for the
caller to observe.

Two switches decide who may start what:

- `child_allow_spawn` (default `true`) - the capability itself;
- `allow_processes` (empty = any) - the executable's basename must be in it,
  which is the same list that decides which processes may be *operated*. A
  program that could not be operated is not allowed to be spawned either.

`run` is CLI-only. The model-facing surface has eight tools. `launch` starts a
program on the current desktop (local `CreateProcess` on main, the peer's
`process.start` on child). Both paths apply `allow_processes`; the peer path
also requires `child_allow_spawn`. `wait_s` defaults to 3 s. The reply carries
`pid` and at most six `+ WINDOW` lines, then `+ N more`; more than one new
window adds `~ N windows from one launch`. On the current desktop it assigns
refs unless the session still awaits a fresh `OBSERVE` after a switch; then it
adds `desktop changed; observe again`. From main, `window="child"` starts
the program through the peer and reports titles only, followed by
`~ on child; switch child to address them`. There is no `SESSION run` command
in SUI-Text.


## Architecture

```
main session                              child session
────────────                              ─────────────
LLM
 │ tool calls
Somnus Runtime
 ├─ compiler / projection / registry / resolver / policy
 ├─ ref namespace
 └─ PeerProvider ──┐
                   │  named pipe  \\.\pipe\somnus-peer-<id>
                   └──────────────►  peer: Session + UiaProvider
                                        │
                                        └─► the windows the agent drives
```

Everything intelligent stays on the main side: the compiler, projection,
registry, resolver, policy and the ref namespace. The far side runs a **thin
peer** that owns a `Session` and answers tool calls - perception and actuation,
no model, no policy of its own.

That keeps the design at one agent. A switch has low cost: `Session` is written
against `AccessibilityProvider`, so the far session is another provider
(`peerprovider.py`). Switching swaps the provider, resets the ref namespace and
assigns no refs; the compiler and runtime do not detect that the provider is
remote.

The transport is a named pipe, chosen after measuring what crosses the boundary
(see below). It carries the provider interface directly - `list_windows`,
`snapshot`, `execute` - serialized field by field in `wire.py`.

## Signing in

The loopback is still a logon, and Windows will not create the session without a
credential. Two routes, tried in this order:

1. **Credential delegation** (the setup's job). Windows reuses the credentials
   the user already signed in with. No password is typed and none is stored.
   This is the normal path.
2. **A stored password** (`somnus child-session credential --save`), for when
   policy refuses delegation. DPAPI-encrypted, CurrentUser scope, with extra
   entropy - decryptable only on this machine, by this account. Never in
   `config.toml`: that file gets pasted into bug reports.

Delegation was measured to be refused on a local account marked *"password not
required"* (`UF_PASSWD_NOTREQD`), even with the allow-list correct. Clearing the
flag was not enough on its own; the stored password is what made it work there.
The setup does not guess which route a machine needs - it configures delegation
and reports what it did, and the stored password is the explicit fallback.

A Windows Hello PIN cannot be used for this logon. It is not a password and the
loopback logon cannot take it.

## Requirements and costs

- Windows **Pro / Enterprise / Education**. Home has no child sessions and cannot
  be made to have them.
- One elevated `child-session setup`, which enables the feature, enables the RDP
  listener, configures delegation and sets `TermService` to automatic.
- **One child session at a time**, system-wide. That is Windows' limit, not this
  program's.
- **No security isolation.** The two sessions are the same account, so the agent
  can read and write every file the user can. This separates *input*, not
  *privilege*.
- A session exists **only as long as its RDP connection does**. Closing the
  loopback ends the session and the programs in it.

## Cleanup

An orphaned loopback holds a session open, so a later start attaches to it and
does not create a new one. This appears as "the settings did not take effect".

- A session this process opened is cleaned up **when the process exits**
  (`atexit`), unless `SESSION detach` said otherwise.
- `SESSION close` cleans up immediately: the loopback, the peer, and the
  session.
- A session left by a crash is detected and cleared by the next `close` or
  `status`.

Detaching a session *disowns* it, so the exit hook leaves it alone - the whole
point of detaching is that the desktop keeps running for a later run.

## What was tried first, and why it is not here

### Giving the agent both desktops at once

Not done, by design: the agent drives **one desktop at a time**. Every ref
(`w1`, `b3`, `q1`) is a bare name that only means something relative to a
desktop, so exposing two at once would make every call ambiguous, and asking
the model to disambiguate each one would cost more than the feature is worth.
`SWITCH` swaps the provider; it does not merge the two trees.

### `Windows.UI.UIAutomation.Core` - the documented way

This is the supported API for bridging UIA across a session boundary, and the
documentation names the intended user: *"a host application (such as the remote
desktop app running on the local machine)"* - which is what this program is,
since it holds the RDP connection.

It was implemented against a live loopback. The result:

- `RemoteAutomationClientSession` **constructs and `Start()`s** from the RDP
  host, with `SessionId` reading back;
- `ConnectionRequested` **does not fire**;
- and `RemoteAutomationServer.ReportSession` answers **"class not registered"** -
  in the main session *and* inside the child session, with the connection up.

It pairs with a private provider context that Microsoft's own remote-desktop
stack sets up and a third-party host cannot reproduce. This finding is recorded
in `somnus/session/bridge.py` at the point where it would have been used.

### A virtual desktop

A virtual desktop shares the session's input desktop, foreground window and
input queue with every other virtual desktop. It therefore cannot separate the
agent's input from the user's: a synthetic keystroke either goes to the wrong
virtual desktop, or - by moving the target to the current one - brings the
agent's window onto the user's, which is exactly the disturbance it was supposed
to avoid.

It is a good fit for "do not put it on screen"; it is not a fit for "do not
touch the user's input".

### Just reporting the element as unsupported

Rejected, but worth stating: it is what the runtime does by default (the
"No Synthetic Input" principle), and it is the right answer when the alternative
is unverifiable input into a stranger's window. A child session is the opposite
trade - it makes the synthetic channels safe by giving them somewhere to happen
that the user is not.

## Diagnostics

```bash
somnus child-session status      # feature available? is a session live?
somnus child-session probe       # ask the peer what it can see
somnus child-session setup       # (re)apply the system configuration
somnus child-session close       # end it and clear leftovers
```

The self-check commands use the session as their default sandbox:

```bash
somnus doctor                    # environment + observe/find/call smoke
somnus eval                      # generic runtime-state verdict (JSON)
somnus replay <trace>            # replay recorded calls, first divergence
```

They are read-only with respect to the session's lifecycle: `doctor` reports
whether one is live but does not open it, and `eval` / `replay` refuse with the
start command when none is live instead of falling back to the main desktop.
`--real` is the explicit opt-in to the user's desktop; `--demo` / `--scenario`
run offline on fixtures.

`status` distinguishes the **raw** child-session id from a **live** one. They
differ exactly when a session is half-dead - an attempt whose logon was refused,
or one that has been reset. `WTSGetChildSessionId` keeps reporting an id in those
cases, for a session that is not in the session list; acting on it produced "the
session exists but has no peer" when there was no session at all.

## If it does not work

Check `somnus child-session status` first.

- **"not available"** - the feature is off: run the elevated `setup`.
- **The window shows a sign-in screen** and stays there: delegation was refused.
  Store the password (`child-session credential --save`) and use `--auto`.
- **"the hint names a session that is gone"** - a leftover from a failed attempt;
  `child-session close` clears it.
- **A display name of "other user" at the sign-in screen** means no credential
  was handed over at all - the same delegation problem.
