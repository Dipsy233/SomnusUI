# Child session: a second desktop the agent can work in

The problem this exists to solve, the mechanism that turned out to work, and the
two that did not.

## The problem

An application is normally driven through the interfaces it publishes
(`Invoke`, `Value`, `Toggle`, ...), and that needs no foreground: a visible but
covered window answers every one of them. Two channels are the exception -
`--allow-clipboard` and `--allow-keyboard` - and they exist because a control
that publishes no writable interface is otherwise unreachable. A self-drawn
editor, a terminal surface, an omnibox that only commits on Enter: for these,
`Ctrl+V` or `SendInput` is the only thing that lands, and both were measured as
the single largest class of unfinished tasks.

They also go to the **foreground window**. A desktop has exactly one. So when the
agent uses them, it is competing with the person at the keyboard for the one
thing there is one of - and whoever loses stops working. That is the disturbance.

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

## For the agent: one tool

The agent drives **one desktop at a time**. It is not given both, because every
ref (`w1`, `b3`, `q1`) is a bare name that only means something relative to a
desktop; two at once would make every call ambiguous, and asking the model to
disambiguate each one would cost more than the feature is worth.

```
SWITCH main          go to the user's desktop
SWITCH child         go to the separate session (must be open)
SESSION open         start the separate session and move there
SESSION close        end it, and return
SESSION detach       leave it running after this process exits
```

Switching **discards every ref**. After a `SWITCH` the window list is different,
so `w1` does not merely go stale - it names whatever `w1` is on the new desktop.
Re-`OBSERVE` before acting.

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

That is what makes it *one agent* rather than two, and it is why a switch is
cheap: `Session` is written against `AccessibilityProvider`, so the far session
is simply another provider (`peerprovider.py`). Switching swaps the provider and
resets the refs; the compiler and runtime never learn anything was remote.

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

An orphaned loopback is not merely untidy: it holds a session open, so a later
start attaches to it instead of making a new one, which reads as "the settings
did not take effect".

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
- but `ConnectionRequested` **never fires**;
- and `RemoteAutomationServer.ReportSession` answers **"class not registered"** -
  in the main session *and* inside the child session, with the connection up.

So it pairs with a private provider context that Microsoft's own remote-desktop
stack sets up and a third-party host cannot reproduce. Recorded rather than
argued: `somnus/session/bridge.py` carries the same finding at the point where it
would have been used.

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
