"""
Benchmark: legacy vs turbo Blender MCP server

Connects directly to the Blender socket server (bypassing MCP layer)
and measures wall-clock time for the key workflow operations.

Run from Windows with Blender open and the addon running:
  python benchmarks/benchmark.py

Adjust STL_DIR to point at a folder with test .stl files.
"""

import socket
import json
import time
import struct
import zlib
import os
import glob
import statistics
import argparse

HOST = "localhost"
PORT = 9876

# ---------------------------------------------------------------------------
# Protocol helpers (mirrors addon_turbo.py framing)
# ---------------------------------------------------------------------------

FLAG_COMPRESSED = 0x01


def _encode(data: dict, compress: bool = True) -> bytes:
    payload = json.dumps(data, default=str).encode()
    flags = 0
    if compress and len(payload) >= 1024:
        c = zlib.compress(payload, level=1)
        if len(c) < len(payload):
            payload = c
            flags |= FLAG_COMPRESSED
    return struct.pack(">I", len(payload)) + bytes([flags]) + payload


def _decode(sock: socket.socket) -> dict:
    header = _recv_exact(sock, 5)
    length = struct.unpack(">I", header[:4])[0]
    flags  = header[4]
    payload = _recv_exact(sock, length)
    if flags & FLAG_COMPRESSED:
        payload = zlib.decompress(payload)
    return json.loads(payload.decode())


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Legacy protocol helper (plain JSON, no framing)
# ---------------------------------------------------------------------------

def _send_legacy(sock: socket.socket, data: dict) -> dict:
    """Simulate the old server.py / addon.py plain-JSON protocol."""
    sock.sendall(json.dumps(data).encode())
    buf = b""
    sock.settimeout(60.0)
    while True:
        try:
            return json.loads(buf.decode())
        except json.JSONDecodeError:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

class Benchmark:
    def __init__(self, host: str, port: int, use_legacy: bool = False):
        self.host = host
        self.port = port
        self.legacy = use_legacy
        self.sock: socket.socket | None = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def send(self, data: dict) -> dict:
        if self.legacy:
            return _send_legacy(self.sock, data)
        self.sock.sendall(_encode(data))
        return _decode(self.sock)

    def measure(self, label: str, cmd: dict, runs: int = 3) -> dict:
        times = []
        for _ in range(runs):
            t0 = time.perf_counter()
            resp = self.send(cmd)
            elapsed = (time.perf_counter() - t0) * 1000
            times.append(elapsed)
            if resp.get("status") == "error":
                print(f"  ERROR in {label}: {resp.get('error')}")
        med = statistics.median(times)
        print(f"  {label:45s}  median {med:7.1f} ms  (runs={runs})")
        return {"label": label, "median_ms": med, "times_ms": times}


# ---------------------------------------------------------------------------
# Test suites
# ---------------------------------------------------------------------------

def bench_single_commands(bm: Benchmark) -> list:
    results = []

    results.append(bm.measure(
        "get_scene_info",
        {"type": "get_scene_info", "params": {}},
        runs=5,
    ))

    # execute_code round-trip with trivial code
    results.append(bm.measure(
        "execute_code (trivial)",
        {"type": "execute_code", "params": {"code": "pass"}},
        runs=5,
    ))

    return results


def bench_stl_import_sequential(bm: Benchmark, stl_files: list) -> list:
    """Import N STL files one at a time (legacy behavior)."""
    results = []
    times = []
    for fp in stl_files:
        t0 = time.perf_counter()
        bm.send({"type": "import_stl", "params": {"filepath": fp}})
        times.append((time.perf_counter() - t0) * 1000)

    total = sum(times)
    print(f"  {'import_stl × ' + str(len(stl_files)) + ' sequential':45s}  "
          f"total {total:7.1f} ms  avg {total/len(times):.1f} ms/file")
    results.append({
        "label": f"import_stl_sequential_x{len(stl_files)}",
        "total_ms": total,
        "avg_ms": total / len(times),
    })
    return results


def bench_stl_import_batch(bm: Benchmark, stl_files: list) -> list:
    """Import N STL files in one batch_execute call."""
    files = [{"filepath": fp} for fp in stl_files]
    t0 = time.perf_counter()
    bm.send({"type": "import_stl_batch", "params": {"files": files}})
    total = (time.perf_counter() - t0) * 1000
    print(f"  {'import_stl_batch × ' + str(len(stl_files)):45s}  "
          f"total {total:7.1f} ms")
    return [{"label": f"import_stl_batch_x{len(stl_files)}", "total_ms": total}]


def bench_batch_commands(bm: Benchmark) -> list:
    """Compare 5 sequential get_scene_info calls vs one batch."""
    N = 5
    commands = [{"type": "get_scene_info", "params": {}} for _ in range(N)]

    # sequential
    times_seq = []
    for cmd in commands:
        t0 = time.perf_counter()
        bm.send(cmd)
        times_seq.append((time.perf_counter() - t0) * 1000)
    total_seq = sum(times_seq)

    # batch
    t0 = time.perf_counter()
    bm.send({"type": "batch_execute", "params": {"commands": commands}})
    total_batch = (time.perf_counter() - t0) * 1000

    speedup = total_seq / total_batch if total_batch else 0
    print(f"  {'batch_execute × ' + str(N) + ' (sequential)':45s}  {total_seq:7.1f} ms")
    print(f"  {'batch_execute × ' + str(N) + ' (batched)':45s}  {total_batch:7.1f} ms  "
          f"→ {speedup:.1f}× speedup")
    return [
        {"label": f"sequential_x{N}", "total_ms": total_seq},
        {"label": f"batch_x{N}", "total_ms": total_batch, "speedup": speedup},
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Blender MCP Turbo benchmark")
    parser.add_argument("--host",   default=HOST)
    parser.add_argument("--port",   type=int, default=PORT)
    parser.add_argument("--legacy", action="store_true",
                        help="Use plain-JSON legacy protocol (test against old addon.py)")
    parser.add_argument("--stl-dir", default="",
                        help="Directory containing .stl test files")
    parser.add_argument("--runs",   type=int, default=3,
                        help="Number of runs per benchmark")
    args = parser.parse_args()

    stl_files = []
    if args.stl_dir:
        stl_files = glob.glob(os.path.join(args.stl_dir, "*.stl"))[:10]

    protocol = "LEGACY plain-JSON" if args.legacy else "TURBO framed+compressed"
    print(f"\n{'='*60}")
    print(f"  Blender MCP Benchmark — {protocol}")
    print(f"  Blender: {args.host}:{args.port}")
    print(f"  STL files found: {len(stl_files)}")
    print(f"{'='*60}\n")

    bm = Benchmark(args.host, args.port, use_legacy=args.legacy)
    try:
        bm.connect()
    except ConnectionRefusedError:
        print(f"ERROR: Could not connect to Blender on {args.host}:{args.port}")
        print("Make sure Blender is open and the addon server is running.")
        return

    all_results = {}

    print("[1] Single-command baseline")
    all_results["single"] = bench_single_commands(bm)

    print("\n[2] Batch vs sequential")
    all_results["batch"] = bench_batch_commands(bm)

    if stl_files:
        print(f"\n[3] STL import — {len(stl_files)} files")
        all_results["stl_seq"]   = bench_stl_import_sequential(bm, stl_files)
        all_results["stl_batch"] = bench_stl_import_batch(bm, stl_files)

        seq_total   = all_results["stl_seq"][0]["total_ms"]
        batch_total = all_results["stl_batch"][0]["total_ms"]
        if batch_total:
            speedup = seq_total / batch_total
            reduction = (1 - batch_total / seq_total) * 100
            print(f"\n  STL import speedup: {speedup:.1f}×  ({reduction:.0f}% time reduction)")
    else:
        print("\n[3] STL import — skipped (no --stl-dir given)")
        print("    Pass --stl-dir C:/your/stl/folder to benchmark STL import speed")

    bm.disconnect()
    print(f"\n{'='*60}")
    print("  Benchmark complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
