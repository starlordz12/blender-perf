"""
blender_mcp.analysis
====================
Analysis tools that connect live to a running Blender addon and produce
structured reports. Each module is both importable (for use in server_turbo.py
MCP tools) and runnable as a standalone CLI script.

Available modules:
  airplane  — create airplane parts, measure timings, dump analysis report
"""

from .airplane import AirplaneAnalysis, BlenderConnection, ReportWriter, PARTS

__all__ = ["AirplaneAnalysis", "BlenderConnection", "ReportWriter", "PARTS"]
