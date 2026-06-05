"""
Blender MCP Turbo — server side (Claude ↔ Blender bridge)

Drop-in replacement for server.py from ahujasid/blender-mcp.

Changes vs legacy server.py:
  - Framed protocol (length-prefix) with optional zlib compression
  - Batch tool: send N commands, pay socket overhead once
  - turbo_pipeline tool for the full STL→Export workflow
  - Persistent connection with auto-reconnect
  - async throughout (no blocking .recv on the MCP event loop)

Usage (Claude Desktop config):
  {
    "mcpServers": {
      "blender-turbo": {
        "command": "python",
        "args": ["path/to/server_turbo.py"],
        "env": {}
      }
    }
  }

Requires: pip install mcp
"""

import asyncio
import json
import zlib
import struct
import logging
import sys
import os
from typing import Any

log = logging.getLogger("blender_mcp_turbo.server")

# Allow running server_turbo.py directly from its directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Blender connection config
# ---------------------------------------------------------------------------

BLENDER_HOST = "localhost"
BLENDER_PORT = 9876
CONNECT_TIMEOUT = 5.0
RESPONSE_TIMEOUT = 120.0
COMPRESS_THRESHOLD = 1024

_FLAG_COMPRESSED = 0x01

# ---------------------------------------------------------------------------
# Async Blender client
# ---------------------------------------------------------------------------

class BlenderClient:
    """
    Async TCP client that speaks the turbo framed protocol with the
    Blender addon_turbo.py socket server.
    """

    def __init__(self, host: str = BLENDER_HOST, port: int = BLENDER_PORT):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def ensure_connected(self) -> None:
        if self._writer and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=CONNECT_TIMEOUT,
        )
        self._writer.transport.set_write_buffer_limits(high=256 * 1024)
        log.info("Connected to Blender on %s:%d", self.host, self.port)

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    # ------------------------------------------------------------------
    # Wire protocol helpers
    # ------------------------------------------------------------------

    def _encode(self, data: dict) -> bytes:
        payload = json.dumps(data, default=str).encode("utf-8")
        flags = 0
        if len(payload) >= COMPRESS_THRESHOLD:
            c = zlib.compress(payload, level=1)
            if len(c) < len(payload):
                payload = c
                flags |= _FLAG_COMPRESSED
        return struct.pack(">I", len(payload)) + bytes([flags]) + payload

    async def _decode(self) -> dict:
        header = await self._reader.readexactly(5)
        length = struct.unpack(">I", header[:4])[0]
        flags  = header[4]
        payload = await self._reader.readexactly(length)
        if flags & _FLAG_COMPRESSED:
            payload = zlib.decompress(payload)
        return json.loads(payload.decode("utf-8"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_command(self, cmd_type: str, params: dict | None = None) -> dict:
        """Send a single command, return the response dict."""
        return await self._send({"type": cmd_type, "params": params or {}})

    async def send_batch(self, commands: list[dict]) -> dict:
        """
        Send multiple {type, params} commands as one batch_execute call.
        Single round-trip regardless of how many commands.
        """
        return await self._send({
            "type":   "batch_execute",
            "params": {"commands": commands},
        })

    async def send_turbo_pipeline(self, config: dict) -> dict:
        """Run the full Import→Align→Boolean→Mold→Export pipeline."""
        return await self._send({
            "type":   "turbo_pipeline",
            "params": {"config": config},
        })

    async def _send(self, message: dict) -> dict:
        async with self._lock:
            for attempt in range(3):
                try:
                    await self.ensure_connected()
                    self._writer.write(self._encode(message))
                    await self._writer.drain()
                    response = await asyncio.wait_for(
                        self._decode(), timeout=RESPONSE_TIMEOUT
                    )
                    return response
                except (ConnectionError, asyncio.IncompleteReadError, OSError) as e:
                    log.warning("Connection error (attempt %d): %s", attempt + 1, e)
                    await self.close()
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (2 ** attempt))
            raise RuntimeError("Failed to send command after 3 attempts")


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

_client = BlenderClient()


async def _run_command(cmd_type: str, params: dict) -> str:
    """Execute a command and return formatted result string."""
    try:
        resp = await _client.send_command(cmd_type, params)
        return json.dumps(resp, indent=2, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def _build_mcp_server():
    """
    Build and return the MCP server.
    Uses the 'mcp' package (pip install mcp).
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log.error("mcp package not found. Run: pip install mcp")
        sys.exit(1)

    mcp = FastMCP("blender-turbo")

    # ------------------------------------------------------------------
    # Core tools (backward compatible with legacy server.py)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_scene_info() -> str:
        """Get complete information about the current Blender scene and all objects."""
        return await _run_command("get_scene_info", {})

    @mcp.tool()
    async def get_object_info(name: str) -> str:
        """Get detailed information about a specific Blender object by name."""
        return await _run_command("get_object_info", {"name": name})

    @mcp.tool()
    async def execute_blender_code(code: str) -> str:
        """
        Execute arbitrary Python code inside Blender.
        Use for one-off operations not covered by other tools.
        For repeated or multi-step work, prefer batch_execute or turbo_pipeline.
        """
        return await _run_command("execute_code", {"code": code})

    # ------------------------------------------------------------------
    # Performance tools (new)
    # ------------------------------------------------------------------

    @mcp.tool()
    async def import_stl(filepath: str, name: str = "") -> str:
        """
        Import a single STL file into Blender.
        Uses numpy fast path (~4x faster than bpy.ops) when available.

        filepath: absolute path to the .stl file (Windows paths OK)
        name: optional object name (defaults to filename stem)
        """
        params: dict = {"filepath": filepath}
        if name:
            params["name"] = name
        return await _run_command("import_stl", params)

    @mcp.tool()
    async def import_stl_batch(files: list) -> str:
        """
        Import multiple STL files in ONE round-trip with a single depsgraph refresh.
        Dramatically faster than calling import_stl N times.

        files: list of objects, each with:
          - filepath (required): absolute path to .stl file
          - name (optional): object name override

        Example:
          [
            {"filepath": "C:/parts/wing_left.stl"},
            {"filepath": "C:/parts/wing_right.stl", "name": "Wing_R"},
            {"filepath": "C:/parts/fuselage.stl"}
          ]
        """
        return await _run_command("import_stl_batch", {"files": files})

    @mcp.tool()
    async def batch_execute(commands: list) -> str:
        """
        Execute multiple Blender commands in a single round-trip.
        All commands run with viewport updates suppressed until the end.

        commands: list of {type, params} objects using any tool name as type.

        Example:
          [
            {"type": "align_objects", "params": {"objects": ["A", "B"], "method": "origin"}},
            {"type": "cleanup_geometry", "params": {"name": "A"}},
            {"type": "cleanup_geometry", "params": {"name": "B"}}
          ]
        """
        return await _client.send_batch(commands).__await__()  # type: ignore[return-value]

    @mcp.tool()
    async def align_objects(
        objects: list,
        method: str = "origin",
        target: str = "",
        location: list = None,
        axis: str = "XYZ",
    ) -> str:
        """
        Align multiple objects without bpy.ops overhead.

        method:
          'origin'  — move to world origin or given location
          'target'  — align to named target object's location
          'bounds'  — align by bounding box centre
          'stack'   — stack objects along Z axis end-to-end

        objects: list of object names
        axis:    which axes to align, e.g. 'XY' to align only X and Y
        """
        params = {"objects": objects, "method": method, "axis": axis}
        if target:
            params["target"] = target
        if location:
            params["location"] = location
        return await _run_command("align_objects", params)

    @mcp.tool()
    async def boolean_operation(
        target: str,
        tool: str,
        operation: str = "DIFFERENCE",
        solver: str = "FAST",
        delete_tool: bool = True,
    ) -> str:
        """
        Apply a boolean modifier between two mesh objects.

        target:     name of the object to be modified
        tool:       name of the cutter/tool object
        operation:  DIFFERENCE | UNION | INTERSECT
        solver:     FAST (default) | EXACT (slower, more accurate for complex geometry)
        delete_tool: remove the tool object after applying (default: true)
        """
        return await _run_command("boolean_operation", {
            "target":      target,
            "tool":        tool,
            "operation":   operation,
            "solver":      solver,
            "delete_tool": delete_tool,
        })

    @mcp.tool()
    async def cleanup_geometry(
        name: str,
        merge_distance: float = 0.0001,
        remove_doubles: bool = True,
        recalc_normals: bool = True,
        dissolve_degenerate: bool = True,
    ) -> str:
        """
        Clean up mesh geometry for export quality using bmesh (data API, no edit-mode).

        name:                object name to clean
        merge_distance:      threshold for merging nearby vertices (default 0.1 mm)
        remove_doubles:      merge vertices within merge_distance
        recalc_normals:      fix flipped/inconsistent face normals
        dissolve_degenerate: remove zero-area faces and zero-length edges
        """
        return await _run_command("cleanup_geometry", {
            "name":                name,
            "merge_distance":      merge_distance,
            "remove_doubles":      remove_doubles,
            "recalc_normals":      recalc_normals,
            "dissolve_degenerate": dissolve_degenerate,
        })

    @mcp.tool()
    async def create_mold(
        part_name: str,
        mold_name: str = "",
        shell_thickness: float = 3.0,
        clearance: float = 0.2,
    ) -> str:
        """
        Create a two-part split mold around a mesh object.

        part_name:       object to create the mold from
        mold_name:       base name for the mold halves (default: {part_name}_mold)
        shell_thickness: wall thickness of the mold in scene units (default 3mm)
        clearance:       uniform gap between part and mold cavity (default 0.2mm)

        Returns names of mold_top and mold_bottom objects.
        """
        params: dict = {
            "part_name":       part_name,
            "shell_thickness": shell_thickness,
            "clearance":       clearance,
        }
        if mold_name:
            params["mold_name"] = mold_name
        return await _run_command("create_mold", params)

    @mcp.tool()
    async def export_stl(
        filepath: str,
        objects: list = None,
        apply_modifiers: bool = True,
        ascii_mode: bool = False,
    ) -> str:
        """
        Export objects to STL.

        filepath:         output file path
        objects:          list of object names to export (omit for all visible)
        apply_modifiers:  apply modifiers before export (default: true)
        ascii_mode:       write ASCII STL instead of binary (larger, slower)
        """
        params: dict = {
            "filepath":        filepath,
            "apply_modifiers": apply_modifiers,
            "ascii_mode":      ascii_mode,
        }
        if objects:
            params["objects"] = objects
        return await _run_command("export_stl", params)

    @mcp.tool()
    async def turbo_pipeline(config: dict) -> str:
        """
        Run the complete Import→Align→Boolean→Mold→Export pipeline in ONE command.
        All steps execute with viewport suppressed; single depsgraph refresh at end.
        Expected 60–80% faster than running each step individually.

        config example:
        {
          "import": [
            {"filepath": "C:/parts/wing_L.stl"},
            {"filepath": "C:/parts/wing_R.stl"},
            {"filepath": "C:/parts/fuselage.stl"}
          ],
          "align": {
            "method": "stack",
            "axis": "Z"
          },
          "boolean": [
            {"target": "fuselage", "tool": "wing_L", "operation": "UNION"},
            {"target": "fuselage", "tool": "wing_R", "operation": "UNION"}
          ],
          "cleanup": {
            "merge_distance": 0.0001
          },
          "mold": {
            "part": "fuselage",
            "shell_thickness": 4.0,
            "clearance": 0.3
          },
          "export": {
            "filepath": "C:/output/airplane_mold.stl"
          }
        }

        Any step can be omitted. Returns per-step results and timing breakdown.
        """
        resp = await _client.send_turbo_pipeline(config)
        return json.dumps(resp, indent=2, default=str)

    @mcp.tool()
    async def get_performance_stats() -> str:
        """Get server-side performance statistics (queue depth, cache size, etc.)."""
        return await _run_command("get_perf_stats", {})

    # ------------------------------------------------------------------
    # Analysis tools
    # ------------------------------------------------------------------

    @mcp.tool()
    async def analyze_airplane_parts(
        report_path: str = "AIRPLANE_ANALYSIS.md",
        stl_export_path: str = "airplane_assembly.stl",
        legacy_protocol: bool = False,
    ) -> str:
        """
        Connect live to Blender, create a full set of airplane parts, measure
        performance across all phases, and produce a complete analysis report.

        Runs 6 phases automatically:
          Phase 0 — protocol detection & round-trip latency
          Phase 1 — sequential part creation (fuselage, wings, nose, tail fins,
                     engines, cockpit) — one execute_code per part, timed individually
          Phase 2 — same parts in a single batched execute_code call
          Phase 3 — batch_execute API (turbo addon only)
          Phase 4 — scene interrogation: vertex/face counts per object
          Phase 5 — geometry cleanup via bmesh (no edit-mode overhead)
          Phase 6 — STL export timing

        Returns the full Markdown report as a string and writes it to report_path.

        report_path:      where to save the .md file (on this machine)
        stl_export_path:  where Blender should write the STL (on the Blender machine)
        legacy_protocol:  set true if using the original addon.py (plain-JSON protocol)
        """
        loop = asyncio.get_event_loop()
        try:
            from analysis.airplane import run_analysis
        except ImportError:
            return json.dumps({
                "error": "analysis package not found — ensure blender_mcp/analysis/ is on the Python path"
            })

        result = await loop.run_in_executor(
            None,
            lambda: run_analysis(
                host=BLENDER_HOST,
                port=BLENDER_PORT,
                legacy=legacy_protocol,
                report_path=report_path,
                stl_export_path=stl_export_path,
            ),
        )

        if result.get("error"):
            return json.dumps({"error": result["error"]}, indent=2)

        # Return a concise summary + first 4000 chars of the report
        summary = {
            "report_path": result["report_path"],
            "scene_stats": result["scene_stats"],
            "phase_timings": result["phases"],
            "report_preview": result["report_md"][:4000] + (
                "\n\n...(truncated, see report_path for full report)"
                if len(result["report_md"]) > 4000 else ""
            ),
        }
        return json.dumps(summary, indent=2, default=str)

    return mcp


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    mcp = _build_mcp_server()

    async def _shutdown():
        await _client.close()

    try:
        mcp.run()
    finally:
        asyncio.run(_shutdown())
