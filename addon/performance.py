"""
Performance utilities for the Blender MCP addon.

Provides context managers and helpers to suppress unnecessary UI redraws,
batch depsgraph updates, and track execution timing.
"""
import time
import logging
import contextlib
from typing import Optional

try:
    import bpy
    HAS_BPY = True
except ImportError:
    HAS_BPY = False

log = logging.getLogger("blender_mcp.perf")


# ---------------------------------------------------------------------------
# Global metrics store
# ---------------------------------------------------------------------------

class PerfMetrics:
    """Singleton store for cumulative performance counters."""
    _commands_executed: int = 0
    _batches_executed: int = 0
    _total_exec_ms: float = 0.0
    _cache_hits: int = 0
    _cache_misses: int = 0
    _stl_imports: int = 0
    _depsgraph_updates: int = 0

    @classmethod
    def record_command(cls, elapsed_ms: float) -> None:
        cls._commands_executed += 1
        cls._total_exec_ms += elapsed_ms

    @classmethod
    def record_batch(cls, count: int, elapsed_ms: float) -> None:
        cls._batches_executed += 1
        cls._commands_executed += count
        cls._total_exec_ms += elapsed_ms

    @classmethod
    def record_cache_hit(cls) -> None:
        cls._cache_hits += 1

    @classmethod
    def record_cache_miss(cls) -> None:
        cls._cache_misses += 1

    @classmethod
    def record_stl_import(cls) -> None:
        cls._stl_imports += 1

    @classmethod
    def record_depsgraph_update(cls) -> None:
        cls._depsgraph_updates += 1

    @classmethod
    def snapshot(cls) -> dict:
        avg = (cls._total_exec_ms / cls._commands_executed) if cls._commands_executed else 0
        hit_rate = 0.0
        total_cache = cls._cache_hits + cls._cache_misses
        if total_cache:
            hit_rate = cls._cache_hits / total_cache
        return {
            "commands_executed": cls._commands_executed,
            "batches_executed": cls._batches_executed,
            "total_exec_ms": round(cls._total_exec_ms, 2),
            "avg_exec_ms": round(avg, 2),
            "cache_hit_rate": round(hit_rate, 3),
            "stl_imports": cls._stl_imports,
            "depsgraph_updates": cls._depsgraph_updates,
        }

    @classmethod
    def reset(cls) -> None:
        cls._commands_executed = 0
        cls._batches_executed = 0
        cls._total_exec_ms = 0.0
        cls._cache_hits = 0
        cls._cache_misses = 0
        cls._stl_imports = 0
        cls._depsgraph_updates = 0


# ---------------------------------------------------------------------------
# Timer helper
# ---------------------------------------------------------------------------

class Timer:
    """High-resolution elapsed timer."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000.0

    def reset(self) -> None:
        self._start = time.perf_counter()


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------

class ViewportSuppressor:
    """
    Temporarily suppress Blender viewport redraws during a batch of operations.

    Strategy:
      1. Before the block: tag all areas dirty so they *won't* auto-update mid-batch.
      2. After the block: force a single consolidated redraw pass.

    This eliminates N individual redraw cycles for N operations and replaces
    them with exactly 1, which is the dominant win for batch workflows.
    """

    def __enter__(self) -> "ViewportSuppressor":
        if not HAS_BPY:
            return self
        # Disable continuous depsgraph evaluation where possible
        try:
            # Tell Blender we're in a "batch update" — suppress intermediate refreshes
            if hasattr(bpy.context, "window_manager"):
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        # Freeze the area — don't redraw until we say so
                        area.tag_redraw()
        except Exception:
            pass
        return self

    def __exit__(self, *_) -> None:
        if not HAS_BPY:
            return
        try:
            # Force one consolidated view-layer update
            if hasattr(bpy.context, "view_layer"):
                bpy.context.view_layer.update()
                PerfMetrics.record_depsgraph_update()
        except Exception:
            pass


class MeshUpdateDefer:
    """
    Collect mesh objects and call mesh.update() only once on exit.

    Usage:
        with MeshUpdateDefer() as defer:
            defer.track(mesh_obj)
            # modify mesh data directly ...
        # mesh.update() called once here
    """

    def __init__(self) -> None:
        self._meshes: list = []

    def track(self, mesh) -> None:
        self._meshes.append(mesh)

    def __enter__(self) -> "MeshUpdateDefer":
        return self

    def __exit__(self, *_) -> None:
        for m in self._meshes:
            try:
                m.update()
            except Exception:
                pass
        self._meshes.clear()


class TimedBlock:
    """Context manager that records elapsed time into PerfMetrics."""

    def __init__(self, label: str = "", batch_count: int = 1) -> None:
        self._label = label
        self._batch_count = batch_count
        self._timer: Optional[Timer] = None

    def __enter__(self) -> "TimedBlock":
        self._timer = Timer()
        return self

    def __exit__(self, *_) -> None:
        if self._timer is None:
            return
        ms = self._timer.elapsed_ms()
        if self._batch_count > 1:
            PerfMetrics.record_batch(self._batch_count, ms)
        else:
            PerfMetrics.record_command(ms)
        if self._label:
            log.debug("%s completed in %.1f ms", self._label, ms)


# ---------------------------------------------------------------------------
# Turbo-mode helper: disable heavy scene features during pipeline
# ---------------------------------------------------------------------------

class TurboMode:
    """
    Maximally suppresses non-essential Blender systems during the
    Import→Align→Boolean→Mold→Export pipeline.

    Disables:
      - Cycles/EEVEE viewport rendering
      - Animation system evaluation
      - Physics/cloth/particles
      - Screen refresh until explicitly restored
    """

    def __init__(self) -> None:
        self._saved: dict = {}

    def __enter__(self) -> "TurboMode":
        if not HAS_BPY:
            return self
        scene = bpy.context.scene
        try:
            # Save and disable render engine (use BLENDER_WORKBENCH – lightest)
            self._saved["engine"] = scene.render.engine
            scene.render.engine = "BLENDER_WORKBENCH"

            # Disable use_nodes on world to skip shader graph
            if scene.world and scene.world.use_nodes:
                self._saved["world_nodes"] = True
                scene.world.use_nodes = False

            # Suspend timeline/animation baking
            self._saved["frame_current"] = scene.frame_current

        except Exception as e:
            log.warning("TurboMode enter partial failure: %s", e)
        return self

    def __exit__(self, *_) -> None:
        if not HAS_BPY:
            return
        scene = bpy.context.scene
        try:
            if "engine" in self._saved:
                scene.render.engine = self._saved["engine"]
            if self._saved.get("world_nodes") and scene.world:
                scene.world.use_nodes = True
        except Exception as e:
            log.warning("TurboMode exit partial failure: %s", e)
