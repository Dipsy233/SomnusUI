# SomnusUI

English | [中文](README.zh-CN.md)

## What it does

SomnusUI compiles a native GUI into a text interface. The runtime reads the Windows accessibility tree, projects it into SUI-Text (nodes, capabilities and attributes), and executes capability calls through UI Automation, with optional clipboard and keyboard channels for text input. Observations and replies contain no screenshots, coordinates or native tree identifiers; a `launch` reply reports the started process's `pid`.

The tool vocabulary is nine calls: `observe`, `find`, `inspect`, `call`, `run`, `help`, `switch`, `launch` and `configure`. A default MCP host exposes the six `core` tools; `switch`, `launch` and `configure` are the opt-in `desktop` tier. Replies are SUI-Text: an observation, a match list, an `OK`/`ERR` line with a delta, a settings line, or a topic page.

The compiler is deterministic and no language model runs inside the runtime. Content that cannot be read without vision is reported as unreadable.

## Usage

Python 3.11 or later is required. Run the commands from the repository root.

```bash
pip install -e ".[dev]"

# Replay the built-in restaurant demo (no Windows required):
somnus --demo observe
somnus --demo call 'CALL p1.add()'
python -m somnus.cli bench --runs 100

# Drive a real Windows application (requires the [windows] extra):
pip install -e ".[windows]"
somnus windows                     # list windows
somnus observe                     # describe the target window
somnus observe w1                  # one window
somnus call 'CALL b1.activate()'
somnus repl                        # one live session for multi-step work
somnus mcp                         # MCP server over stdio
```

Refs (`w1`, `p1`, `q1`, …) exist only for the lifetime of one session. One-shot commands such as `somnus observe` and `somnus call` exit after one tool call, so every ref dies with the process. Multi-step work uses `somnus repl`, the MCP server, or the `run` tool.

MCP configuration:

```json
{
  "mcpServers": {
    "somnus": { "command": "somnus-mcp" }
  }
}
```

Full command reference: [docs/cli.md](docs/cli.md). Settings and environment variables: [docs/configuration.md](docs/configuration.md).

## Documentation

| Document | Content |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | repository layout, compile/project/runtime pipeline, module responsibilities, dependency direction |
| [docs/technology.md](docs/technology.md) | technology choices and their current implementation form |
| [docs/protocol.md](docs/protocol.md) | nine tools, SUI-Text, observation, find/inspect/call, batches, scripts, error codes, tool tiers |
| [docs/cli.md](docs/cli.md) | installation, CLI commands, REPL, MCP, traces, self-check and replay |
| [docs/configuration.md](docs/configuration.md) | settings file, environment variables, precedence and the `configure` tool |
| [docs/development.md](docs/development.md) | build, test, fixtures, benchmarks and the acceptance applications |
| [docs/guides/observation-walkthrough.md](docs/guides/observation-walkthrough.md) | one window and its tool calls, with real output |
| [docs/reference/packs.md](docs/reference/packs.md) | App Pack authoring |
| [docs/reference/child-session.md](docs/reference/child-session.md) | child-session design, costs and rejected approaches |
| [docs/reference/text-input-channels.md](docs/reference/text-input-channels.md) | text input routes, costs and verification |
| [docs/benchmarks/tool-surface.md](docs/benchmarks/tool-surface.md) | tool tiers and the tool-exposure benchmark |
| [docs/benchmarks/app-tasks.md](docs/benchmarks/app-tasks.md) | 50 user tasks and measured coverage |
| [docs/benchmarks/scenario-suite.md](docs/benchmarks/scenario-suite.md) | acceptance screens and pass criteria |
| [docs/archive/performance-2026-11.md](docs/archive/performance-2026-11.md) | performance and viewport capture: measurements, changes and remaining work |
| [docs/archive/plan-v3.zh-CN.md](docs/archive/plan-v3.zh-CN.md) | black-box test plan v3 (historical proposal) |
