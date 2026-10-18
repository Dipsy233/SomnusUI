# SomnusUIm (SomnusDroid)

**Do language models dream of pixels?**

English | [中文](README.zh-CN.md)

SomnusUIm turns an Android phone's accessibility tree into a plain-text
semantic API so that a language model can operate the device by **capability
calls** instead of pixel taps. The model receives a compact description of what
each control *is* and what can be done with it; it never receives screenshots,
coordinates, or native identifiers.

This repository is the Android build. It grew out of
[SomnusUI](../SomnusUI), the Windows UI-Automation edition; the shared
compiler / runtime / SUI-Text protocol are the same design.

```
User intent
   ↓
LLM planner (five tools)
   ↓
Somnus runtime ─ observe / find / inspect / call / switch
   ├─ Session registry   ref ↔ fingerprint ↔ native node
   ├─ Resolver           semantic capability → native primitives
   ├─ Settler + Verifier quiet window + effects + delta
   └─ Policy             risk, confirmation
   ↓
Semantic graph
   ↑
Semantic compiler (deterministic: normalize → infer → match → project → compress)
   ↑
AndroidProvider ── Portal app (accessibility service, JSON-RPC over WebSocket)
                    reached through `adb forward` (the trust boundary)
```

## Principles

- **No vision.** The accessibility tree is the only perception channel.
- **Capabilities are evidence.** A capability is advertised only when the
  platform offers the interface behind it: `scroll` from the node's own
  SCROLL actions, `select` never on inert labels, `context_menu` on Android
  only where a LONG_CLICK exists. A control with nothing usable is reported
  as such.
- **Reads follow the screen.** A phone shows one screen; observe / find /
  inspect / call always describe the window that is active *right now*, and
  re-snapshot before serving.
- **Low token cost.** Observation is projected and budgeted: homogeneous lists
  preview a few items, anonymous leaves fold into one summary line. An L0
  overview typically costs tens to a few hundred tokens.
- **Semantics first.** The model calls `CALL b9.activate()`, never
  `tap(312, 528)`. The few gestures the tree cannot express (launcher page
  turns) are synthesized inside the Portal, where the screen size is known.
- **Deterministic compiler.** No language model runs inside the runtime or the
  compiler.
- **Fail loud.** Anything the accessibility API cannot express is reported as
  `UNKNOWN`, never guessed.

## Quick start

```bash
pip install -e ".[dev,mcp]"

# On the phone: install the Portal APK (portal/) and enable its accessibility
# service. Then forward the RPC port:
adb forward tcp:18677 tcp:8677

# Observe the current screen:
somnus --provider android observe

# Run one action:
somnus --provider android call 'CALL b1.activate()'

# Interactive loop:
somnus --provider android repl
```

### As an MCP server

```bash
somnus-mcp android
```

Exposes five tools to any MCP host: `observe`, `find`, `inspect`, `call`,
`switch`. See `.mcp.json` for a ready-made client configuration.

## What the model sees

Every observation is SUI-Text: one line per control, its role, its ref, and the
capabilities it honours.

```
@rev 12
WINDOW w3 "com.tencent.mobileqq" active
  GROUP g1 +scroll_into_view +select +tap
    LIST l5 +context_menu +scroll_into_view +select
    TEXT tx147 "我的电脑" +activate +focus +read +scroll_into_view
    INPUT q1 +activate +context_menu +set_text(text)
    BUTTON b4 "发送" disabled +focus +read +scroll_into_view +select +tap
```

Actions are plain sentences over refs:

```
CALL q1.set_text(text:"你好")
CALL b4.activate()
SWITCH home          # back to the launcher (platform global action)
SWITCH next          # turn the launcher one page (synthesized swipe)
```

The reply to a call is a **delta** - what changed, not a re-render:

```
OK rev=11
~ q1 name →""            # the input emptied
~ b4 disabled false→true # the send button relocked
+ TEXT tx167 "你好…"      # the bubble that appeared
```

## Screen navigation

What a user moves between on a phone are *screens*, and some of them exist in
no tree at all: the home screen behind an app, the previous app, a launcher
page that is not instantiated yet. `switch` reaches them through the
platform's own APIs - `performGlobalAction` for home/back, a swipe gesture
synthesized inside the Portal for pager pages - never through coordinates the
model provides.

## The five tools

| tool | reads/writes | syntax |
| --- | --- | --- |
| `observe` | current screen, L0 | `OBSERVE [window] [full]` |
| `find` | search, fresh snapshot | `FIND "text" [in ref]` |
| `inspect` | one node in full | `INSPECT ref [depth=N]` |
| `call` | act | `CALL ref.cap(args)` |
| `switch` | screen navigation | `SWITCH home\|back\|next\|prev` |

## Portal app

`portal/` is the on-device half: an accessibility service that snapshots the
whole screen in one traversal (keys are index paths; password text never
leaves the phone), performs actions by key, and owns the gesture channel. The
RPC server binds loopback only - the adb forward *is* the trust boundary.

Build: `gradle assembleDebug` (see `portal/app/build/outputs/apk/debug/`).

## Repository layout

```
somnus/
  compiler/        raw tree -> semantic graph (normalize, caps, match, packs)
  projection/      budget + folding rules for L0/L1/L2
  runtime/         session, registry (stable refs), resolver, settler, delta
  providers/
    android/       Portal transport, raw-tree conversion, Android mock
    mock.py        platform-neutral replay provider (tests, offline runs)
  protocol/        MCP server, JSON-RPC server, SUI-Text parser, prompts
  sui/             SUI-Text grammar (parse + render)
portal/            the Android app (Kotlin): a11y service + RPC server
tests/             pytest suite (offline: fixtures replay the phone path)
docs/              design + progress notes
```

## Testing

```bash
python -m pytest tests -q
```

Everything runs offline: recorded Portal snapshots are replayed through the
real conversion, compiler and runtime, so what passes in CI is evidence about
the phone path. `tests/android/smoke_real_device.py` exercises a live device.

## License

MIT
