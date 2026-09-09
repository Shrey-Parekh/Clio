"""What the machine is doing now — CPU, memory, disk, GPU, and what's eating them.

Read-only and local, so FREE. Two deliberate absences: CPU temperature (Windows
doesn't expose it without a driver, so "temperature" means the GPU), and any
caching (a stale reading spoken confidently is the same failure as a stale rate).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time

import psutil

# psutil needs two samples for CPU load (the first reads zero). One shared
# window per snapshot, so a question costs 0.3s once, not once per number.
_SAMPLE_S = 0.3
_TOP_N = 3
_NVIDIA_TIMEOUT_S = 4.0

# Ordered: the specific readings win over the general one, so "how's the disk"
# is not answered with everything.
_TOPICS: list[tuple[str, str]] = [
    ("hogs", r"what'?s? (?:eating|hogging|using all|chewing|slowing)|"
             r"top processes|biggest processes|which process|what'?s? running hot"),
    ("gpu", r"\bgpu\b|graphics card|\bvram\b|video card"),
    ("temperature", r"\btemperatures?\b|\btemps?\b|how hot is (?:the|my|it running)|"
                    r"thermal|overheating"),
    ("memory", r"\bram\b|memory (?:usage|used|free|pressure)|how much memory"),
    ("disk", r"disk (?:space|usage)|free space|how much space|storage left|"
             r"\bssd\b|drive space"),
    ("cpu", r"\bcpu\b|processor (?:usage|load)"),
    ("overview", r"how'?s (?:the|my) (?:machine|pc|computer|system)|"
                 r"system resources|resource usage|how is the computer|"
                 r"is (?:the|my) (?:machine|pc|computer) (?:ok|okay|struggling)"),
]

_COMPILED = [(name, re.compile(pattern)) for name, pattern in _TOPICS]

# Reading a process name aloud is only useful if it is the thing he recognises.
_FRIENDLY = {
    "msedge.exe": "Edge", "chrome.exe": "Chrome", "firefox.exe": "Firefox",
    "Code.exe": "VS Code", "python.exe": "Python", "pythonw.exe": "Python",
    "explorer.exe": "Explorer", "Discord.exe": "Discord", "Spotify.exe": "Spotify",
    "steam.exe": "Steam", "MemCompression": "Windows memory compression",
}

# The idle process is the machine doing nothing — reported as top consumer it
# would invert the answer.
_NOT_A_HOG = {"System Idle Process", "Idle"}


def parse_system_query(text: str) -> str | None:
    """The topic asked about, or None to fall through to conversation."""
    lowered = " ".join(text.lower().split())
    for name, pattern in _COMPILED:
        if pattern.search(lowered):
            return name
    return None


def _gb(num_bytes: float) -> str:
    return f"{num_bytes / 1_073_741_824:.0f}"


def _friendly(name: str) -> str:
    return _FRIENDLY.get(name, name.removesuffix(".exe"))


def read_gpu() -> dict | None:
    """None means no NVIDIA GPU is visible, which is said plainly rather than
    reported as zero load."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,"
             "memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_NVIDIA_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines:
        return None
    fields = [f.strip() for f in lines[0].split(",")]
    if len(fields) != 5:
        return None
    try:
        return {
            "name": fields[0], "load": float(fields[1]),
            "used_mb": float(fields[2]), "total_mb": float(fields[3]),
            "temperature": float(fields[4]),
        }
    except ValueError:
        return None


def _top_processes(sample_s: float) -> list[tuple[str, float, float]]:
    """First cpu_percent() per process reads 0.0, so prime all, then read after
    one shared window."""
    procs = []
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] in _NOT_A_HOG or proc.pid == 0:
            continue
        try:
            proc.cpu_percent(None)
            procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    time.sleep(sample_s)
    cores = psutil.cpu_count() or 1

    readings = []
    for proc in procs:
        try:
            # cpu_percent is per-core, so it exceeds 100 on a threaded process.
            # Divided down, it matches the machine-wide number he just heard.
            load = proc.cpu_percent(None) / cores
            readings.append((proc.info["name"] or "something", load, proc.memory_info().rss))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    readings.sort(key=lambda r: r[1], reverse=True)
    return readings[:_TOP_N]


def snapshot(sample_s: float = _SAMPLE_S) -> dict:
    """One blocking read of everything — the caller runs it on a thread."""
    top = _top_processes(sample_s)
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage("/")
    return {
        "cpu": psutil.cpu_percent(None),
        "cores": psutil.cpu_count(logical=False) or psutil.cpu_count(),
        "memory_percent": memory.percent,
        "memory_used": memory.total - memory.available,
        "memory_total": memory.total,
        "disk_free": disk.free,
        "disk_total": disk.total,
        "gpu": read_gpu(),
        "top": top,
        "uptime_s": time.time() - psutil.boot_time(),
    }


def _describe_uptime(seconds: float) -> str:
    hours = int(seconds // 3600)
    if hours < 1:
        return f"Up {int(seconds // 60)} minutes."
    if hours < 48:
        return f"Up {hours} hours."
    return f"Up {hours // 24} days."


def describe(topic: str, reading: dict) -> str:
    """Spoken, so one or two facts per topic — a full readout is unlistenable."""
    gpu = reading["gpu"]

    if topic == "cpu":
        return (f"CPU is at {reading['cpu']:.0f} percent across "
                f"{reading['cores']} cores. {_describe_uptime(reading['uptime_s'])}")

    if topic == "memory":
        return (f"Memory is at {reading['memory_percent']:.0f} percent, "
                f"{_gb(reading['memory_used'])} of {_gb(reading['memory_total'])} gigs used.")

    if topic == "disk":
        return (f"{_gb(reading['disk_free'])} gigs free of "
                f"{_gb(reading['disk_total'])} on the system drive.")

    if topic == "gpu":
        if gpu is None:
            return "I can't see an NVIDIA GPU on this machine."
        return (f"The {gpu['name']} is at {gpu['load']:.0f} percent, "
                f"{gpu['used_mb'] / 1024:.1f} of {gpu['total_mb'] / 1024:.0f} gigs of memory, "
                f"and {gpu['temperature']:.0f} degrees.")

    if topic == "temperature":
        if gpu is None:
            return ("I can't read temperatures on this machine. Windows doesn't expose "
                    "the CPU sensor without a driver, and there's no NVIDIA GPU to ask.")
        return (f"The GPU is at {gpu['temperature']:.0f} degrees. I can't read the CPU "
                f"temperature on Windows, so I'd rather not guess at it.")

    if topic == "hogs":
        busy = [r for r in reading["top"] if r[1] >= 1.0]
        if not busy:
            return "Nothing much is running. The machine is basically idle."
        # Memory only when it is worth hearing - "and 0.0 gigs" is noise.
        parts = [
            f"{_friendly(name)} at {load:.0f} percent"
            + (f" and {rss / 1_073_741_824:.1f} gigs" if rss >= 1_073_741_824 else "")
            for name, load, rss in busy
        ]
        return "Biggest right now: " + ", ".join(parts) + "."

    line = (f"CPU {reading['cpu']:.0f} percent, memory {reading['memory_percent']:.0f} percent, "
            f"{_gb(reading['disk_free'])} gigs of disk free")
    if gpu is not None:
        line += f", GPU {gpu['load']:.0f} percent at {gpu['temperature']:.0f} degrees"
    top = reading["top"]
    if top and top[0][1] >= 10.0:
        line += f". {_friendly(top[0][0])} is the busiest thing on it"
    return line + "."


async def describe_system(topic: str) -> str:
    return describe(topic, await asyncio.to_thread(snapshot))
