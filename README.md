# Blender MCP — Performance Tuning

Bottleneck analysis and drop-in replacement files for the [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp) addon. The stock addon has four critical performance bugs that silently corrupt large payloads, thrash the timer queue, and force N round-trips for N operations. This repo documents those bugs and provides fixed implementations.

---

## The Problems (stock addon)

### 1. `recv(8192)` truncation — silent data corruption *(CRITICAL)*
Any payload larger than 8 KB (STL import paths, scene info with many objects, screenshots) is silently truncated. The JSON parser throws and the connection resets. Every large command fails without a clear error.

### 2. Per-request timer registration — throughput killer *(HIGH)*
`bpy.app.timers.register()` is called for every single command. Under load this builds up a backlog of timer objects, increases GC pressure, and causes unpredictable execution ordering.

### 3. No command batching — N round-trips for N ops *(HIGH)*
A workflow that imports 10 STL files pays ~8 ms of timer-wait latency per file — ~80 ms wasted just scheduling. There's no way to send commands in bulk.

### 4. Blocking `bpy.ops` in the socket thread *(MEDIUM)*
Some operations (`bpy.ops.wm.stl_import`, etc.) block the main thread. When called from the socket thread they can deadlock Blender or cause context errors.

Full analysis with measured latency numbers: **[BOTTLENECK_REPORT.md](BOTTLENECK_REPORT.md)**

---

## The Fixes

### `addon_turbo.py` — drop-in replacement for `addon.py`
- **Length-prefix framing** replaces raw `recv(8192)` — handles arbitrarily large payloads correctly
- **One persistent queue-drain timer** at 5 ms tick replaces per-request timer registration
- **Batch command support** — send `[cmd1, cmd2, cmd3]` as a JSON array, get `[result1, result2, result3]` back in one round-trip
- **Context-safe execution** — all `bpy.ops` calls go through a thread-safe queue processed on the main thread

### `server_turbo.py` — drop-in replacement for `server.py` (MCP side)
- Matches the new framing protocol in `addon_turbo.py`
- Exposes a `batch_execute` MCP tool for sending multiple commands at once
- Keeps full backwards compatibility with all existing single-command calls

---

## Repository Structure

```
├── addon_turbo.py          # Drop-in Blender addon (replaces addon.py)
├── server_turbo.py         # Drop-in MCP server (replaces server.py)
├── __init__.py             # Package init
├── addon/
│   └── performance.py      # Performance measurement helpers (bpy side)
├── analysis/
│   ├── __init__.py
│   └── airplane.py         # RC plane–specific analysis utilities
├── benchmarks/
│   └── benchmark.py        # Benchmark suite — measures round-trip latency
└── BOTTLENECK_REPORT.md    # Full analysis with code diffs and latency measurements
```

---

## Installation

### Swap the addon
1. In Blender: Edit → Preferences → Add-ons → find **Blender MCP** → disable it.
2. Install `addon_turbo.py` as a new addon (same panel, "Install from File").
3. Enable it. The sidebar panel and port 9876 server start as before.

### Swap the MCP server
Replace `server.py` in your `blender-mcp` clone with `server_turbo.py`:
```bash
cp server_turbo.py /path/to/blender-mcp/server.py
```
Restart the MCP server process. No other config changes needed.

---

## Benchmark Results

Measured on localhost (Windows 11, Blender 4.x, RTX 3080):

| Operation | Stock addon | Turbo addon | Improvement |
|-----------|------------|-------------|-------------|
| Single execute_code | ~12ms | ~6ms | 2× |
| 10× STL import (sequential) | ~850ms | ~320ms | 2.7× |
| 10× STL import (batched) | N/A | ~180ms | — |
| Large payload (>8KB) | **FAILS** | works | ∞ |

Full methodology and raw numbers in [BOTTLENECK_REPORT.md](BOTTLENECK_REPORT.md).

---

## Compatibility

Tested against Blender 3.6 LTS, 4.0, 4.1, 4.2 LTS. The turbo addon is a strict superset of the original — all existing MCP tools continue to work without modification.