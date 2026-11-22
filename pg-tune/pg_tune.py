#!/usr/bin/env python3
"""pg-tune: hardware- and workload-driven PostgreSQL configuration planner."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import glob
import json
import os
from pathlib import Path
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, Sequence


DEFAULT_VERSION = "0.1.0"
DEFAULT_PRELOAD = "pg_stat_statements,auto_explain"
RESERVED_SUPERUSER_CONNS = 3
MAX_INT_BOUND = 999999

_SIZE_KB_RE = re.compile(r"^\s*(\d+)\s*(tb|t|gb|g|mb|m|kb|k|b)?\s*$", re.IGNORECASE)
_STRICT_UINT_RE = re.compile(r"^(0|[1-9][0-9]{0,5})$")
_STRICT_PIN_INT_RE = re.compile(r"^[1-9][0-9]{0,5}$")
_MOUNT_OCTAL_RE = re.compile(r"\\([0-7]{3})")
_UNQUOTED_CONF_RE = re.compile(
    r"^(?:-?\d+(?:\.\d+)?(?:[A-Za-z]+)?|on|off|true|false|try|minimal|replica|"
    r"worker|sync|zstd|lz4|all|top|none|warning)$"
)

LIST_PARAMETERS = frozenset(
    {
        "shared_preload_libraries",
        "session_preload_libraries",
        "local_preload_libraries",
        "search_path",
        "temp_tablespaces",
        "log_destination",
        "synchronous_standby_names",
        "unix_socket_directories",
        "listen_addresses",
        "wal_consistency_checking",
    }
)

_ENV_KEY_MAP = {
    "ram_kb": "ram_kb",
    "memory_kb": "ram_kb",
    "cores": "cores",
    "threads": "threads",
    "wal_bytes": "wal_bytes",
    "wal_shared": "wal_shared",
    "rotational": "rotational",
    "reserved_kb": "reserved_kb",
    "reserved": "reserved_kb",
    "buffers_max_kb": "buffers_max_kb",
    "buffers_max": "buffers_max_kb",
    "os": "os",
    "wal_level": "wal_level",
    "max_wal_senders": "max_wal_senders",
    "huge_pages": "huge_pages",
    "io_method": "io_method",
    "ingest_conns": "ingest_conns",
    "serving_conns": "serving_conns",
    "serving": "serving_conns",
    "active_queries": "active_queries",
    "work_nodes": "work_nodes",
    "locks_per_tx": "locks_per_tx",
    "port": "port",
    "temp_tablespaces": "temp_tablespaces",
    "trace": "trace",
    "preload": "preload",
    "data_path": "data_path",
    "wal_path": "wal_path",
    "listen_addresses": "listen_addresses",
    "ssl": "ssl",
    "psql": "psql",
    "conninfo": "conninfo",
    "format": "format",
}


class TuneError(Exception):
    """Raised when configuration inputs, hardware facts, or validation fail."""

    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class HardwareFacts:
    os_name: str
    ram_kb: int
    reserved_kb: int
    buffers_max_kb: int
    threads: int
    cores: int
    wal_bytes: int
    wal_shared: int
    rotational: int


@dataclass(frozen=True)
class WorkloadProfile:
    wal_level: str
    max_wal_senders: int
    huge_pages: str
    io_method: str
    ingest_conns: int
    serving_conns: int
    active_queries: int
    work_nodes: int
    locks_per_tx: int
    port: int
    temp_tablespaces: str | None
    trace: str
    preload: list[str]
    listen_addresses: str
    ssl: str | None


@dataclass(frozen=True)
class BudgetPlan:
    usable_kb: int
    shared_kb: int
    ecs_kb: int
    private_kb: int
    wal_mb: int
    min_wal_mb: int
    wal_buf_kb: int
    par: int
    gather: int
    avw: int
    iow: int
    mwp: int
    reserved_conns: int
    conns: int
    active_queries: int
    backends: int
    work_nodes: int
    temp_kb: int
    temp_total_kb: int
    exec_kb: int
    mwm_kb: int
    avwm_kb: int
    work_kb: int
    eic: int
    rpc: str
    locks: int
    over_kb: int


@dataclass(frozen=True)
class TuneResult:
    version: str
    facts: HardwareFacts
    profile: WorkloadProfile
    budget: BudgetPlan
    settings: dict[str, str]


def read_version() -> str:
    version_file = Path(__file__).resolve().with_name("VERSION")
    try:
        text = version_file.read_text(encoding="utf-8").strip()
        return text or DEFAULT_VERSION
    except OSError:
        return DEFAULT_VERSION


def clamp(value: int, lo: int, hi: int) -> int:
    return min(max(value, lo), hi)


def parse_size_kb(value: Any, name: str = "size") -> int:
    """Parse a size expression (kB by default, or TB/GB/MB/KB/B) into integer kB."""
    if isinstance(value, bool):
        raise TuneError(f"pg-tune: {name} must be a size, not boolean")
    if isinstance(value, int):
        if value < 0:
            raise TuneError(f"pg-tune: {name} must be non-negative (got {value})")
        return value
    if not isinstance(value, str):
        raise TuneError(f"pg-tune: not a size for {name}: {value!r}")
    match = _SIZE_KB_RE.match(value)
    if not match:
        raise TuneError(f"pg-tune: not a size for {name}: '{value}'")
    num = int(match.group(1))
    unit = (match.group(2) or "kb").lower()
    if unit in ("tb", "t"):
        return num * 1073741824
    if unit in ("gb", "g"):
        return num * 1048576
    if unit in ("mb", "m"):
        return num * 1024
    if unit in ("kb", "k"):
        return num
    if unit == "b":
        return num // 1024
    raise TuneError(f"pg-tune: unsupported unit in {name}: '{value}'")


def parse_size_bytes(value: Any, name: str = "wal-bytes") -> int:
    """Parse a size expression (bytes by default, or TB/GB/MB/KB/B) into integer bytes."""
    if isinstance(value, bool):
        raise TuneError(f"pg-tune: {name} must be a byte count, not boolean")
    if isinstance(value, int):
        if value < 0:
            raise TuneError(f"pg-tune: {name} must be non-negative (got {value})")
        return value
    if not isinstance(value, str):
        raise TuneError(f"pg-tune: not a byte size for {name}: {value!r}")
    match = _SIZE_KB_RE.match(value)
    if not match:
        raise TuneError(f"pg-tune: not a byte size for {name}: '{value}'")
    num = int(match.group(1))
    unit = (match.group(2) or "b").lower()
    if unit in ("tb", "t"):
        return num * 1024**4
    if unit in ("gb", "g"):
        return num * 1024**3
    if unit in ("mb", "m"):
        return num * 1024**2
    if unit in ("kb", "k"):
        return num * 1024
    if unit == "b":
        return num
    raise TuneError(f"pg-tune: unsupported unit in {name}: '{value}'")


def parse_bool_int(value: Any, name: str) -> int:
    """Parse 0/1/true/false/yes/no/on/off into 0 or 1."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return 1
        if low in ("0", "false", "no", "off"):
            return 0
    raise TuneError(f"pg-tune: {name} must be 0/1 or true/false (got {value!r})")


def parse_bounded_int(value: Any, name: str, *, positive: bool) -> int:
    """Validate an integer matching ^[1-9][0-9]{0,5}$ or ^(0|[1-9][0-9]{0,5})$."""
    if isinstance(value, bool):
        raise TuneError(f"pg-tune: {name} must be an integer, not boolean")
    if isinstance(value, int):
        lo = 1 if positive else 0
        if value < lo or value > MAX_INT_BOUND:
            kind = "positive" if positive else "nonnegative"
            raise TuneError(
                f"pg-tune: {name} must be a {kind} integer (at most six digits)"
            )
        return value
    if isinstance(value, str):
        s = value.strip()
        pattern = _STRICT_PIN_INT_RE if positive else _STRICT_UINT_RE
        if not pattern.match(s):
            kind = "positive" if positive else "nonnegative"
            raise TuneError(
                f"pg-tune: {name} must be a {kind} integer (at most six digits)"
            )
        return int(s)
    raise TuneError(f"pg-tune: {name} must be an integer (got {value!r})")


def parse_preload(value: Any) -> list[str]:
    if value is None:
        raw_items = DEFAULT_PRELOAD.split(",")
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    elif isinstance(value, str):
        raw_items = value.split(",")
    else:
        raise TuneError(f"pg-tune: preload must be a list or comma-separated string")
    return [item.strip() for item in raw_items if item.strip()]


# ---------------------------------------------------------------------------
# Linux topology & storage auto-detection
# ---------------------------------------------------------------------------


def unescape_mountinfo(path_str: str) -> str:
    return _MOUNT_OCTAL_RE.sub(lambda m: chr(int(m.group(1), 8)), path_str)


def parse_mountinfo(target_path: str, mountinfo_text: str) -> tuple[str, str] | None:
    """Resolve the deepest mountpoint and major:minor device covering target_path."""
    if not target_path.startswith("/"):
        raise TuneError("pg-tune: mount path must be absolute on Linux")
    best_dev: str | None = None
    best_mp: str | None = None
    best_len = 0
    for line in mountinfo_text.splitlines():
        if not line.strip():
            continue
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        dev = fields[2]
        mp = unescape_mountinfo(fields[4])
        if mp == "/":
            matched = True
        elif target_path == mp or target_path.startswith(mp + "/"):
            matched = True
        else:
            matched = False
        if matched and len(mp) >= best_len:
            best_len = len(mp)
            best_dev = dev
            best_mp = mp
    if best_dev is None or best_mp is None:
        return None
    return best_dev, best_mp


def detect_linux_ram_kb(
    meminfo_path: str = "/proc/meminfo",
    cgroup_paths: Sequence[str] = (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",
    ),
) -> int:
    try:
        meminfo = Path(meminfo_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TuneError(f"pg-tune: cannot read {meminfo_path}: {exc}") from exc
    m = re.search(r"^MemTotal:\s*(\d+)\s*kB", meminfo, re.MULTILINE)
    if not m:
        raise TuneError(f"pg-tune: MemTotal not found in {meminfo_path}")
    kb = int(m.group(1))
    for cg_path in cgroup_paths:
        try:
            raw = Path(cg_path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not raw or raw == "max":
            continue
        if raw.isdigit():
            lim_kb = int(raw) // 1024
            if 0 < lim_kb < kb:
                kb = lim_kb
        break
    return kb


def detect_linux_cpu(
    sys_cpu_root: str = "/sys/devices/system/cpu",
    threads_override: int | None = None,
) -> tuple[int, int]:
    """Return (threads, physical_cores) on Linux."""
    if threads_override is not None:
        threads = threads_override
    else:
        if hasattr(os, "sched_getaffinity"):
            try:
                threads = len(os.sched_getaffinity(0))
            except OSError:
                threads = os.cpu_count() or 1
        else:
            threads = os.cpu_count() or 1

    cpu_root = Path(sys_cpu_root)
    distinct_cores: set[str] = set()
    for pattern in ("cpu[0-9]*/topology/core_cpus_list", "cpu[0-9]*/topology/thread_siblings_list"):
        matches = sorted(glob.glob(str(cpu_root / pattern)))
        for match in matches:
            cpu_dir = Path(match).parent.parent.name
            if not re.fullmatch(r"cpu\d+", cpu_dir):
                continue
            try:
                content = Path(match).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                distinct_cores.add(content)
        if distinct_cores:
            break

    cores = len(distinct_cores) if distinct_cores else threads
    return threads, cores


def _norm_posix(path: str) -> str:
    if not path.startswith("/"):
        raise TuneError("pg-tune: mount path must be absolute on Linux")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def detect_linux_rotational(
    data_path: str = "/",
    mountinfo_path: str = "/proc/self/mountinfo",
    sys_dev_block: str = "/sys/dev/block",
    sys_class_block: str = "/sys/class/block",
) -> int:
    try:
        mountinfo_text = Path(mountinfo_path).read_text(encoding="utf-8")
    except OSError:
        return 0
    resolved = parse_mountinfo(_norm_posix(data_path), mountinfo_text)
    if not resolved:
        return 0
    dev, _ = resolved
    start = Path(sys_dev_block) / dev
    if not start.is_dir():
        return 0

    queue: list[Path] = [start]
    seen: set[Path] = set()
    rot = 0
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        slaves_dir = cur / "slaves"
        if slaves_dir.is_dir():
            try:
                slaves = sorted(slaves_dir.iterdir())
            except OSError:
                slaves = []
            if slaves:
                for s in slaves:
                    queue.append(Path(sys_class_block) / s.name)
                continue
        rot_file = cur.parent / "queue" / "rotational"
        if not rot_file.is_file():
            rot_file = cur / "queue" / "rotational"
        try:
            if rot_file.read_text(encoding="utf-8").strip() == "1":
                rot = 1
        except OSError:
            pass
    return rot


def detect_linux_wal(
    wal_path: str | None,
    data_path: str | None = None,
    mountinfo_path: str = "/proc/self/mountinfo",
    sys_dev_block: str = "/sys/dev/block",
) -> tuple[int, int]:
    """Return (wal_volume_bytes, wal_shared) on Linux."""
    raw_wal = wal_path or (posixpath.join(data_path, "pg_wal") if data_path else "/")
    probe_wal = _norm_posix(raw_wal)
    try:
        mountinfo_text = Path(mountinfo_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TuneError(f"pg-tune: cannot read {mountinfo_path}: {exc}") from exc

    wal_mount = parse_mountinfo(probe_wal, mountinfo_text)
    if not wal_mount:
        raise TuneError(f"pg-tune: no mount holds {probe_wal}")
    dev, mp = wal_mount

    size_path = Path(sys_dev_block) / dev / "size"
    wal_bytes: int | None = None
    try:
        wal_bytes = int(size_path.read_text(encoding="utf-8").strip()) * 512
    except (OSError, ValueError):
        if hasattr(os, "statvfs"):
            try:
                st = os.statvfs(probe_wal if os.path.exists(probe_wal) else mp)
                wal_bytes = st.f_frsize * st.f_blocks
            except OSError:
                wal_bytes = None
    if wal_bytes is None:
        raise TuneError(f"pg-tune: device {dev} of {probe_wal} has no size in sysfs")

    shared = 1
    if wal_path is not None and mp == probe_wal:
        if data_path is not None:
            data_mount = parse_mountinfo(_norm_posix(data_path), mountinfo_text)
            if not data_mount or data_mount[1] != mp:
                shared = 0
        else:
            shared = 0
    return wal_bytes, shared


# ---------------------------------------------------------------------------
# Windows topology & storage auto-detection (ctypes stdlib)
# ---------------------------------------------------------------------------


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
    _fields_ = [
        ("PropertyId", ctypes.c_uint32),
        ("QueryType", ctypes.c_uint32),
        ("AdditionalParameters", ctypes.c_ubyte * 1),
    ]


class _DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_uint32),
        ("Size", ctypes.c_uint32),
        ("IncursSeekPenalty", ctypes.c_ubyte),
    ]


def _win32_kernel32() -> Any:
    return ctypes.WinDLL("kernel32", use_last_error=True)


def detect_windows_topology() -> tuple[int, int, int]:
    """Return (ram_kb, threads, cores) via Windows kernel32 APIs."""
    kernel32 = _win32_kernel32()

    mem = _MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise ctypes.WinError(ctypes.get_last_error())
    ram_kb = int(mem.ullTotalPhys // 1024)

    kernel32.GetActiveProcessorCount.argtypes = [ctypes.c_uint16]
    kernel32.GetActiveProcessorCount.restype = ctypes.c_uint32
    threads = int(kernel32.GetActiveProcessorCount(0xFFFF))
    if threads == 0:
        raise ctypes.WinError(ctypes.get_last_error())

    kernel32.GetLogicalProcessorInformationEx.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    kernel32.GetLogicalProcessorInformationEx.restype = ctypes.c_int

    length = ctypes.c_uint32(0)
    ok = kernel32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(length))
    err = ctypes.get_last_error()
    if (not ok and err != 122) or length.value < 8:
        raise ctypes.WinError(err)

    buf = (ctypes.c_ubyte * length.value)()
    if not kernel32.GetLogicalProcessorInformationEx(0, ctypes.byref(buf), ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())

    cores = 0
    offset = 0
    total_len = int(length.value)
    while offset < total_len:
        if total_len - offset < 8:
            raise TuneError("pg-tune: truncated Windows processor topology buffer")
        relationship = ctypes.c_uint32.from_buffer(buf, offset).value
        entry_size = ctypes.c_uint32.from_buffer(buf, offset + 4).value
        if entry_size < 8 or entry_size > total_len - offset:
            raise TuneError("pg-tune: invalid Windows processor topology entry")
        if relationship == 0:  # RelationProcessorCore
            cores += 1
        offset += entry_size

    if ram_kb <= 0 or cores < 1 or threads < cores:
        raise TuneError("pg-tune: invalid Windows memory/processor facts")
    return ram_kb, threads, cores


def _existing_windows_probe_path(path: str | None) -> str:
    candidate = os.path.abspath(path or os.getcwd())
    while candidate and not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        candidate = parent
    if not candidate or not os.path.exists(candidate):
        drive, _ = os.path.splitdrive(os.path.abspath(path or os.getcwd()))
        if drive:
            return drive + "\\"
    return candidate


def detect_windows_disk_bytes(path: str | None) -> int:
    kernel32 = _win32_kernel32()
    kernel32.GetDiskFreeSpaceExW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    kernel32.GetDiskFreeSpaceExW.restype = ctypes.c_int

    target = _existing_windows_probe_path(path)
    avail = ctypes.c_uint64(0)
    total = ctypes.c_uint64(0)
    free = ctypes.c_uint64(0)
    if not kernel32.GetDiskFreeSpaceExW(
        target, ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(total.value)


def detect_windows_rotational(data_path: str | None) -> int:
    """Best-effort seek penalty query via CreateFileW/DeviceIoControl; defaults to 0."""
    drive, _ = os.path.splitdrive(os.path.abspath(data_path or os.getcwd()))
    if not drive:
        return 0
    volume_path = f"\\\\.\\{drive}"
    kernel32 = _win32_kernel32()

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    FILE_SHARE_READ_WRITE = 0x00000001 | 0x00000002
    OPEN_EXISTING = 3
    IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
    StorageDeviceSeekPenaltyProperty = 7
    PropertyStandardQuery = 0

    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.DeviceIoControl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.CreateFileW(
        volume_path, 0, FILE_SHARE_READ_WRITE, None, OPEN_EXISTING, 0, None
    )
    if handle is None or handle == INVALID_HANDLE_VALUE:
        return 0
    try:
        query = _STORAGE_PROPERTY_QUERY()
        query.PropertyId = StorageDeviceSeekPenaltyProperty
        query.QueryType = PropertyStandardQuery
        desc = _DEVICE_SEEK_PENALTY_DESCRIPTOR()
        returned = ctypes.c_uint32(0)
        ok = kernel32.DeviceIoControl(
            handle,
            IOCTL_STORAGE_QUERY_PROPERTY,
            ctypes.byref(query),
            ctypes.sizeof(query),
            ctypes.byref(desc),
            ctypes.sizeof(desc),
            ctypes.byref(returned),
            None,
        )
        if not ok or returned.value < ctypes.sizeof(desc):
            return 0
        return 1 if desc.IncursSeekPenalty else 0
    finally:
        kernel32.CloseHandle(handle)


def detect_windows_wal(wal_path: str | None, data_path: str | None) -> tuple[int, int]:
    probe = wal_path or data_path or os.getcwd()
    wal_bytes = detect_windows_disk_bytes(probe)
    # Separate Windows drives still commonly hold other workloads; default to shared unless overridden.
    return wal_bytes, 1


# ---------------------------------------------------------------------------
# Planning math & settings generation
# ---------------------------------------------------------------------------


def resolve_os_name(os_choice: str) -> str:
    choice = os_choice.strip().lower()
    if choice == "auto":
        return "windows" if sys.platform.startswith("win") else "linux"
    if choice in ("linux", "windows"):
        return choice
    raise TuneError(f"pg-tune: --os must be linux, windows, or auto (got '{os_choice}')")


def resolve_facts(raw: dict[str, Any]) -> HardwareFacts:
    os_name = resolve_os_name(str(raw.get("os") or "auto"))

    ram_kb = parse_size_kb(raw["ram_kb"], "ram-kb") if raw.get("ram_kb") is not None else None
    threads = (
        parse_bounded_int(raw["threads"], "threads", positive=True)
        if raw.get("threads") is not None
        else None
    )
    cores = (
        parse_bounded_int(raw["cores"], "cores", positive=True)
        if raw.get("cores") is not None
        else None
    )
    wal_bytes = (
        parse_size_bytes(raw["wal_bytes"], "wal-bytes")
        if raw.get("wal_bytes") is not None
        else None
    )
    wal_shared = (
        parse_bool_int(raw["wal_shared"], "wal-shared")
        if raw.get("wal_shared") is not None
        else None
    )
    rotational = (
        parse_bool_int(raw["rotational"], "rotational")
        if raw.get("rotational") is not None
        else None
    )
    reserved_kb = (
        parse_size_kb(raw["reserved_kb"], "reserved-kb")
        if raw.get("reserved_kb") is not None
        else 0
    )
    buffers_max_kb = (
        parse_size_kb(raw["buffers_max_kb"], "buffers-max-kb")
        if raw.get("buffers_max_kb") is not None
        else 0
    )

    data_path = raw.get("data_path")
    wal_path = raw.get("wal_path")

    if os_name == "windows":
        if ram_kb is None or threads is None or cores is None:
            if not sys.platform.startswith("win"):
                raise TuneError(
                    "pg-tune: cannot auto-detect Windows hardware on non-Windows host; "
                    "pass --ram-kb, --cores, and --threads"
                )
            d_ram, d_threads, d_cores = detect_windows_topology()
            if ram_kb is None:
                ram_kb = d_ram
            if threads is None:
                threads = d_threads
            if cores is None:
                cores = d_cores
        if wal_bytes is None or wal_shared is None:
            if not sys.platform.startswith("win"):
                raise TuneError(
                    "pg-tune: cannot auto-detect Windows disk on non-Windows host; "
                    "pass --wal-bytes and --wal-shared"
                )
            d_wal_bytes, d_wal_shared = detect_windows_wal(wal_path, data_path)
            if wal_bytes is None:
                wal_bytes = d_wal_bytes
            if wal_shared is None:
                wal_shared = d_wal_shared
        if rotational is None:
            rotational = (
                detect_windows_rotational(data_path)
                if sys.platform.startswith("win")
                else 0
            )
    else:
        if ram_kb is None:
            ram_kb = detect_linux_ram_kb()
        if threads is None or cores is None:
            d_threads, d_cores = detect_linux_cpu()
            if threads is None:
                threads = d_threads
            if cores is None:
                cores = d_cores
        if wal_bytes is None or wal_shared is None:
            d_wal_bytes, d_wal_shared = detect_linux_wal(wal_path, data_path)
            if wal_bytes is None:
                wal_bytes = d_wal_bytes
            if wal_shared is None:
                wal_shared = d_wal_shared
        if rotational is None:
            rotational = detect_linux_rotational(data_path or "/")

    if ram_kb <= 0 or cores < 1 or threads < cores:
        raise TuneError(
            "pg-tune: invalid hardware facts (positive RAM and threads >= cores >= 1 required)"
        )
    if wal_bytes < 0:
        raise TuneError("pg-tune: wal-bytes must be non-negative")

    return HardwareFacts(
        os_name=os_name,
        ram_kb=ram_kb,
        reserved_kb=reserved_kb,
        buffers_max_kb=buffers_max_kb,
        threads=threads,
        cores=cores,
        wal_bytes=wal_bytes,
        wal_shared=wal_shared,
        rotational=rotational,
    )


def compute_budget_and_profile(
    facts: HardwareFacts, raw: dict[str, Any]
) -> tuple[WorkloadProfile, BudgetPlan]:
    usable_kb = max(facts.ram_kb - facts.reserved_kb, facts.ram_kb // 8)
    shared_kb = usable_kb // 4
    if facts.buffers_max_kb > 0:
        shared_kb = min(shared_kb, facts.buffers_max_kb)
    shared_kb = max(shared_kb, 131072)
    ecs_kb = (usable_kb * 7) // 10
    private_kb = usable_kb // 4

    if facts.wal_shared == 1:
        wal_mb = min(facts.wal_bytes // (1048576 * 4), 32768)
    else:
        wal_mb = facts.wal_bytes // (1048576 * 2)
    wal_mb = max(wal_mb, 1024)
    min_wal_mb = clamp(wal_mb // 8, 80, 4096)
    wal_buf_kb = clamp(shared_kb // 32, 16384, (wal_mb * 1024) // 16)

    par = facts.cores
    gather = facts.cores // 2
    avw = max(3, facts.cores // 2)
    iow = clamp((facts.threads * 2) // 3, 1, 32)
    mwp = par + 4

    ingest_conns = (
        parse_bounded_int(raw["ingest_conns"], "ingest-conns", positive=False)
        if raw.get("ingest_conns") is not None
        else 0
    )
    serving_conns = (
        parse_bounded_int(raw["serving_conns"], "serving-conns", positive=False)
        if raw.get("serving_conns") is not None
        else max(16, 4 * facts.threads)
    )
    reserved_conns = RESERVED_SUPERUSER_CONNS
    conns = ingest_conns + serving_conns + avw + reserved_conns

    if raw.get("active_queries") is not None:
        active_queries = parse_bounded_int(
            raw["active_queries"], "active-queries", positive=True
        )
        if active_queries > conns - reserved_conns:
            raise TuneError(
                "pg-tune: active-queries must be positive and fit non-reserved connection capacity"
            )
    else:
        active_queries = min(facts.cores, conns - reserved_conns)

    work_nodes = (
        parse_bounded_int(raw["work_nodes"], "work-nodes", positive=True)
        if raw.get("work_nodes") is not None
        else 2
    )
    locks_per_tx = (
        parse_bounded_int(raw["locks_per_tx"], "locks-per-tx", positive=True)
        if raw.get("locks_per_tx") is not None
        else 64
    )

    maint_dom_kb = private_kb // 4
    mwm_kb = max(maint_dom_kb // 2, 65536)
    avwm_kb = max(maint_dom_kb // (2 * avw), 1024)
    temp_kb = 8192
    temp_total_kb = conns * temp_kb
    exec_kb = private_kb - mwm_kb - avw * avwm_kb - temp_total_kb
    backends = active_queries + par
    work_kb = max(exec_kb // (backends * work_nodes), 4096)
    over_kb = max(
        backends * work_nodes * work_kb + temp_total_kb + mwm_kb + avw * avwm_kb - private_kb,
        0,
    )

    if facts.rotational == 1:
        eic = 2
        rpc = "4"
    else:
        eic = iow
        rpc = "1.1"

    wal_level = str(raw.get("wal_level") or "replica").strip().lower()
    if wal_level not in ("minimal", "replica"):
        raise TuneError(f"pg-tune: --wal-level must be minimal or replica (got '{wal_level}')")

    if raw.get("max_wal_senders") is not None:
        max_wal_senders = parse_bounded_int(
            raw["max_wal_senders"], "max-wal-senders", positive=False
        )
        if wal_level == "minimal" and max_wal_senders != 0:
            raise TuneError("pg-tune: wal_level=minimal requires max_wal_senders=0")
    else:
        max_wal_senders = 0 if wal_level == "minimal" else 3

    default_huge_pages = "off" if facts.os_name == "windows" else "try"
    huge_pages = str(raw.get("huge_pages") or default_huge_pages).strip().lower()
    if huge_pages not in ("try", "off", "on"):
        raise TuneError(f"pg-tune: --huge-pages must be try, off, or on (got '{huge_pages}')")

    io_method = str(raw.get("io_method") or "worker").strip().lower()
    if io_method not in ("worker", "sync"):
        raise TuneError(f"pg-tune: --io-method must be worker or sync (got '{io_method}')")

    port = 5432 if raw.get("port") is None else parse_bounded_int(raw["port"], "port", positive=True)
    if port < 1 or port > 65535:
        raise TuneError(f"pg-tune: --port must be in 1..65535 (got {port})")

    temp_ts_raw = raw.get("temp_tablespaces")
    temp_tablespaces = (
        str(temp_ts_raw).strip() if temp_ts_raw is not None and str(temp_ts_raw).strip() else None
    )

    trace = str(raw.get("trace") or "full").strip().lower()
    if trace not in ("full", "reduced", "off"):
        raise TuneError(f"pg-tune: --trace must be full, reduced, or off (got '{trace}')")

    preload = parse_preload(raw.get("preload"))
    listen_addresses = str(raw.get("listen_addresses") or "*").strip() or "*"

    ssl_raw = raw.get("ssl")
    ssl: str | None = None
    if ssl_raw is not None:
        ssl_str = str(ssl_raw).strip().lower()
        if ssl_str not in ("on", "off"):
            raise TuneError(f"pg-tune: --ssl must be on or off (got '{ssl_raw}')")
        ssl = ssl_str

    profile = WorkloadProfile(
        wal_level=wal_level,
        max_wal_senders=max_wal_senders,
        huge_pages=huge_pages,
        io_method=io_method,
        ingest_conns=ingest_conns,
        serving_conns=serving_conns,
        active_queries=active_queries,
        work_nodes=work_nodes,
        locks_per_tx=locks_per_tx,
        port=port,
        temp_tablespaces=temp_tablespaces,
        trace=trace,
        preload=preload,
        listen_addresses=listen_addresses,
        ssl=ssl,
    )
    budget = BudgetPlan(
        usable_kb=usable_kb,
        shared_kb=shared_kb,
        ecs_kb=ecs_kb,
        private_kb=private_kb,
        wal_mb=wal_mb,
        min_wal_mb=min_wal_mb,
        wal_buf_kb=wal_buf_kb,
        par=par,
        gather=gather,
        avw=avw,
        iow=iow,
        mwp=mwp,
        reserved_conns=reserved_conns,
        conns=conns,
        active_queries=active_queries,
        backends=backends,
        work_nodes=work_nodes,
        temp_kb=temp_kb,
        temp_total_kb=temp_total_kb,
        exec_kb=exec_kb,
        mwm_kb=mwm_kb,
        avwm_kb=avwm_kb,
        work_kb=work_kb,
        eic=eic,
        rpc=rpc,
        locks=locks_per_tx,
        over_kb=over_kb,
    )
    return profile, budget


def _mb_str(kb: int) -> str:
    return f"{kb // 1024}MB"


def build_settings(profile: WorkloadProfile, budget: BudgetPlan) -> dict[str, str]:
    pgss_track = {"full": "all", "reduced": "top", "off": "none"}[profile.trace]
    io_timing = "off" if profile.trace == "off" else "on"

    settings: dict[str, str] = {
        "wal_level": profile.wal_level,
        "max_wal_senders": str(profile.max_wal_senders),
        "shared_buffers": _mb_str(budget.shared_kb),
        "effective_cache_size": _mb_str(budget.ecs_kb),
        "work_mem": _mb_str(budget.work_kb),
        "maintenance_work_mem": _mb_str(budget.mwm_kb),
        "autovacuum_work_mem": _mb_str(budget.avwm_kb),
        "temp_buffers": _mb_str(budget.temp_kb),
        "hash_mem_multiplier": "1.0",
        "huge_pages": profile.huge_pages,
        "io_method": profile.io_method,
        "io_workers": str(budget.iow),
        "effective_io_concurrency": str(budget.eic),
        "maintenance_io_concurrency": str(budget.eic),
        "random_page_cost": budget.rpc,
        "default_toast_compression": "lz4",
        "max_wal_size": f"{budget.wal_mb}MB",
        "max_slot_wal_keep_size": f"{budget.wal_mb}MB",
        "min_wal_size": f"{budget.min_wal_mb}MB",
        "wal_buffers": _mb_str(budget.wal_buf_kb),
        "wal_compression": "zstd",
        "checkpoint_timeout": "60min",
        "checkpoint_completion_target": "0.9",
        "bgwriter_lru_maxpages": "1000",
        "max_worker_processes": str(budget.mwp),
        "max_parallel_workers": str(budget.par),
        "max_parallel_workers_per_gather": str(budget.gather),
        "max_parallel_maintenance_workers": str(budget.gather),
        "jit": "off",
        "enable_parallel_append": "on",
        "gin_pending_list_limit": "32MB",
        "autovacuum_max_workers": str(budget.avw),
        "autovacuum_naptime": "10s",
        "autovacuum_vacuum_cost_delay": "0",
        "autovacuum_vacuum_cost_limit": "2000",
        "autovacuum_vacuum_scale_factor": "0.05",
        "autovacuum_analyze_scale_factor": "0.02",
        "autovacuum_vacuum_insert_scale_factor": "0.01",
        "max_locks_per_transaction": str(budget.locks),
        "shared_preload_libraries": ",".join(profile.preload),
    }

    if "pg_stat_statements" in profile.preload:
        settings["pg_stat_statements.track"] = pgss_track
    if "auto_explain" in profile.preload:
        settings["auto_explain.log_min_duration"] = "-1"
        settings["auto_explain.log_parameter_max_length"] = "0"

    settings["track_io_timing"] = io_timing
    settings["track_wal_io_timing"] = io_timing
    settings["listen_addresses"] = profile.listen_addresses
    if profile.ssl is not None:
        settings["ssl"] = profile.ssl
    settings["port"] = str(profile.port)
    settings["max_connections"] = str(budget.conns)
    settings["superuser_reserved_connections"] = str(budget.reserved_conns)
    settings["max_prepared_transactions"] = "0"

    if profile.temp_tablespaces is not None:
        settings["temp_tablespaces"] = profile.temp_tablespaces

    return settings


def generate_plan(raw: dict[str, Any]) -> TuneResult:
    facts = resolve_facts(raw)
    profile, budget = compute_budget_and_profile(facts, raw)
    settings = build_settings(profile, budget)
    return TuneResult(
        version=read_version(),
        facts=facts,
        profile=profile,
        budget=budget,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------


def result_to_dict(result: TuneResult) -> dict[str, Any]:
    f = result.facts
    p = result.profile
    b = result.budget
    return {
        "version": result.version,
        "facts": {
            "os": f.os_name,
            "memory_kb": f.ram_kb,
            "reserved_kb": f.reserved_kb,
            "buffers_max_kb": f.buffers_max_kb,
            "threads": f.threads,
            "cores": f.cores,
            "wal_volume_bytes": f.wal_bytes,
            "wal_volume_shared": f.wal_shared,
            "data_rotational": f.rotational,
        },
        "profile": {
            "wal_level": p.wal_level,
            "max_wal_senders": p.max_wal_senders,
            "huge_pages": p.huge_pages,
            "io_method": p.io_method,
            "ingest_conns": p.ingest_conns,
            "serving_conns": p.serving_conns,
            "active_queries": p.active_queries,
            "work_nodes": p.work_nodes,
            "locks_per_tx": p.locks_per_tx,
            "port": p.port,
            "temp_tablespaces": p.temp_tablespaces,
            "trace": p.trace,
            "preload": list(p.preload),
        },
        "budget": {
            "usable_kb": b.usable_kb,
            "shared_kb": b.shared_kb,
            "ecs_kb": b.ecs_kb,
            "private_kb": b.private_kb,
            "wal_mb": b.wal_mb,
            "min_wal_mb": b.min_wal_mb,
            "wal_buf_kb": b.wal_buf_kb,
            "par": b.par,
            "gather": b.gather,
            "avw": b.avw,
            "iow": b.iow,
            "mwp": b.mwp,
            "reserved_conns": b.reserved_conns,
            "conns": b.conns,
            "active_queries": b.active_queries,
            "backends": b.backends,
            "work_nodes": b.work_nodes,
            "temp_kb": b.temp_kb,
            "temp_total_kb": b.temp_total_kb,
            "exec_kb": b.exec_kb,
            "mwm_kb": b.mwm_kb,
            "avwm_kb": b.avwm_kb,
            "work_kb": b.work_kb,
            "eic": b.eic,
            "rpc": b.rpc,
            "locks": b.locks,
            "over_kb": b.over_kb,
        },
        "over_kb": b.over_kb,
        "settings": dict(result.settings),
    }


def format_json(result: TuneResult) -> str:
    return json.dumps(result_to_dict(result), indent=2)


def _format_conf_value(value: str) -> str:
    if value == "":
        return "''"
    if _UNQUOTED_CONF_RE.match(value):
        return value
    return "'" + value.replace("'", "''") + "'"


def format_conf(result: TuneResult) -> str:
    lines = [f"{name} = {_format_conf_value(val)}" for name, val in result.settings.items()]
    return "\n".join(lines)


def format_declared(result: TuneResult) -> str:
    lines = [
        (f"{name:<40} {val}" if val != "" else f"{name:<40} ''")
        for name, val in result.settings.items()
    ]
    return "\n".join(lines)


def _sh_env_val(value: Any) -> str:
    s = str(value)
    if re.fullmatch(r"[A-Za-z0-9_./:,-]+", s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def format_env(result: TuneResult) -> str:
    f = result.facts
    b = result.budget
    p = result.profile
    pairs: list[tuple[str, Any]] = [
        ("PG_TUNE_OS", f.os_name),
        ("PG_TUNE_RAM_KB", f.ram_kb),
        ("PG_TUNE_RESERVED_KB", f.reserved_kb),
        ("PG_TUNE_BUFFERS_MAX_KB", f.buffers_max_kb),
        ("PG_TUNE_THREADS", f.threads),
        ("PG_TUNE_CORES", f.cores),
        ("PG_TUNE_WAL_BYTES", f.wal_bytes),
        ("PG_TUNE_WAL_SHARED", f.wal_shared),
        ("PG_TUNE_ROTATIONAL", f.rotational),
        ("PG_TUNE_WAL_LEVEL", p.wal_level),
        ("PG_TUNE_HUGE_PAGES", p.huge_pages),
        ("PG_TUNE_IO_METHOD", p.io_method),
        ("PG_TUNE_INGEST_CONNS", p.ingest_conns),
        ("PG_TUNE_SERVING_CONNS", p.serving_conns),
        ("PG_TUNE_ACTIVE_QUERIES", p.active_queries),
        ("PG_TUNE_WORK_NODES", p.work_nodes),
        ("PG_TUNE_LOCKS_PER_TX", p.locks_per_tx),
        ("PG_TUNE_PORT", p.port),
        ("PG_TUNE_TRACE", p.trace),
        ("PG_TUNE_PRELOAD", ",".join(p.preload)),
        ("PG_TUNE_USABLE_KB", b.usable_kb),
        ("PG_TUNE_SHARED_KB", b.shared_kb),
        ("PG_TUNE_ECS_KB", b.ecs_kb),
        ("PG_TUNE_PRIVATE_KB", b.private_kb),
        ("PG_TUNE_WAL_MB", b.wal_mb),
        ("PG_TUNE_MIN_WAL_MB", b.min_wal_mb),
        ("PG_TUNE_WAL_BUF_KB", b.wal_buf_kb),
        ("PG_TUNE_PAR", b.par),
        ("PG_TUNE_GATHER", b.gather),
        ("PG_TUNE_AVW", b.avw),
        ("PG_TUNE_IOW", b.iow),
        ("PG_TUNE_MWP", b.mwp),
        ("PG_TUNE_CONNS", b.conns),
        ("PG_TUNE_BACKENDS", b.backends),
        ("PG_TUNE_TEMP_TOTAL_KB", b.temp_total_kb),
        ("PG_TUNE_EXEC_KB", b.exec_kb),
        ("PG_TUNE_MWM_KB", b.mwm_kb),
        ("PG_TUNE_AVWM_KB", b.avwm_kb),
        ("PG_TUNE_WORK_KB", b.work_kb),
        ("PG_TUNE_TEMP_KB", b.temp_kb),
        ("PG_TUNE_EIC", b.eic),
        ("PG_TUNE_RPC", b.rpc),
        ("PG_TUNE_OVER_KB", b.over_kb),
    ]
    if p.temp_tablespaces is not None:
        pairs.append(("PG_TUNE_TEMP_TABLESPACES", p.temp_tablespaces))
    for k, v in result.settings.items():
        env_key = "PG_TUNE_SETTING_" + re.sub(r"[^A-Za-z0-9]+", "_", k).upper()
        pairs.append((env_key, v))
    return "\n".join(f"{k}={_sh_env_val(v)}" for k, v in pairs)


def format_plan(result: TuneResult, hostname: str | None = None) -> str:
    host = hostname or socket.gethostname() or "localhost"
    f = result.facts
    b = result.budget
    lines = [
        f"pg-tune: facts of {host} ({f.os_name})",
        f"  {'memory_kb':<24} {f.ram_kb}",
        f"  {'reserved_kb':<24} {f.reserved_kb}",
        f"  {'buffers_max_kb':<24} {f.buffers_max_kb}",
        f"  {'threads':<24} {f.threads}",
        f"  {'cores':<24} {f.cores}",
        f"  {'wal_volume_bytes':<24} {f.wal_bytes}",
        f"  {'wal_volume_shared':<24} {f.wal_shared}",
        f"  {'data_rotational':<24} {f.rotational}",
        "pg-tune: workload budget (estimated concurrency, not query admission control)",
        f"  {'usable_kb':<24} {b.usable_kb}",
        f"  {'shared_kb':<24} {b.shared_kb}",
        f"  {'ecs_kb':<24} {b.ecs_kb}",
        f"  {'private_kb':<24} {b.private_kb}",
        f"  {'conns':<24} {b.conns}",
        f"  {'active_queries':<24} {b.active_queries}",
        f"  {'par':<24} {b.par}",
        f"  {'backends':<24} {b.backends}",
        f"  {'work_nodes':<24} {b.work_nodes}",
        f"  {'temp_total_kb':<24} {b.temp_total_kb}",
        f"  {'exec_kb':<24} {b.exec_kb}",
        f"  {'over_kb':<24} {b.over_kb}",
        "pg-tune: derived settings",
    ]
    for name, val in result.settings.items():
        display = val if val != "" else "''"
        lines.append(f"  {name:<40} {display}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PostgreSQL live check / apply via explicit --psql and --conninfo
# ---------------------------------------------------------------------------


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_report_sql(settings: dict[str, str]) -> str:
    vals = ",\n".join(f"  ({_sql_quote(k)}, {_sql_quote(v)})" for k, v in settings.items())
    return f"""WITH want(name, value) AS (VALUES
{vals}
),
p AS (
  SELECT w.name, w.value,
         substring(w.value from '^(-?[0-9.]+)')::numeric AS n,
         nullif(substring(lower(w.value) from '[a-z]+$'), '') AS u
  FROM want w
)
SELECT w.name,
       CASE
         WHEN s.name IS NULL THEN 'f'
         WHEN s.vartype = 'bool' THEN
           CASE WHEN current_setting(w.name, true) =
             CASE lower(w.value) WHEN 'on' THEN 'on' WHEN 'true' THEN 'on' WHEN '1' THEN 'on' WHEN 'yes' THEN 'on'
                                 WHEN 'off' THEN 'off' WHEN 'false' THEN 'off' WHEN '0' THEN 'off' WHEN 'no' THEN 'off'
                                 ELSE w.value END
           THEN 't' ELSE 'f' END
         WHEN s.vartype IN ('integer', 'real') AND coalesce(s.unit, '') = '' THEN
           CASE WHEN abs(s.setting::numeric - p.n) < 0.0000001 THEN 't' ELSE 'f' END
         WHEN s.vartype IN ('integer', 'real') AND s.unit ~ 'B$' THEN
           CASE WHEN s.setting::numeric = round(pg_size_bytes(w.value)::numeric
              / pg_size_bytes(CASE WHEN s.unit ~ '^[0-9]' THEN s.unit ELSE '1 ' || s.unit END)) THEN 't' ELSE 'f' END
         WHEN s.vartype IN ('integer', 'real') AND s.unit IN ('us','ms','s','min','h','d') THEN
           CASE WHEN s.setting::numeric * CASE s.unit WHEN 'us' THEN 0.001 WHEN 'ms' THEN 1 WHEN 's' THEN 1000 WHEN 'min' THEN 60000 WHEN 'h' THEN 3600000 WHEN 'd' THEN 86400000 END
                     = p.n * CASE coalesce(p.u, s.unit) WHEN 'us' THEN 0.001 WHEN 'ms' THEN 1 WHEN 's' THEN 1000 WHEN 'min' THEN 60000 WHEN 'h' THEN 3600000 WHEN 'd' THEN 86400000 END
           THEN 't' ELSE 'f' END
         ELSE
           CASE WHEN replace(current_setting(w.name, true), ' ', '') = replace(w.value, ' ', '') THEN 't' ELSE 'f' END
       END,
       coalesce(s.context, ''),
       coalesce(s.source, ''),
       CASE WHEN s.pending_restart THEN 't' ELSE 'f' END,
       coalesce(current_setting(w.name, true), '')
FROM want w
LEFT JOIN pg_settings s ON s.name = w.name
LEFT JOIN p ON p.name = w.name
ORDER BY 1;"""


def build_alter_system_sql(name: str, value: str) -> str:
    if name in LIST_PARAMETERS:
        items = [part.strip() for part in value.split(",") if part.strip()]
        literal = ", ".join(_sql_quote(item) for item in items) if items else "''"
    else:
        literal = _sql_quote(value)
    return f"ALTER SYSTEM SET {name} = {literal};"


def _resolve_psql_executable(psql_bin: str | None, conninfo: str | None) -> tuple[str, str]:
    if not psql_bin or not str(psql_bin).strip():
        raise TuneError("pg-tune: --psql is required for --check / --apply")
    if not conninfo or not str(conninfo).strip():
        raise TuneError("pg-tune: --conninfo is required for --check / --apply")
    psql_str = str(psql_bin).strip()
    if os.path.sep in psql_str or (os.path.altsep and os.path.altsep in psql_str):
        if not os.path.isfile(psql_str):
            raise TuneError(f"pg-tune: psql executable not found: {psql_str}")
        resolved_psql = psql_str
    else:
        found = shutil.which(psql_str)
        if not found:
            raise TuneError(f"pg-tune: psql executable not found in PATH: {psql_str}")
        resolved_psql = found
    return resolved_psql, str(conninfo).strip()


def run_psql(psql_exe: str, conninfo: str, sql: str) -> list[str]:
    fd, tmp_path = tempfile.mkstemp(prefix="pg_tune_", suffix=".sql")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(sql)
            if not sql.endswith("\n"):
                fh.write("\n")
        cmd = [psql_exe, "-X", conninfo, "-v", "ON_ERROR_STOP=1", "-At", "-F", "|", "-f", tmp_path]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise TuneError(f"pg-tune: psql failed: {detail}", exit_code=1)
        return [line for line in proc.stdout.splitlines() if line.strip()]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _validate_not_overcommitted(result: TuneResult) -> None:
    over_kb = result.budget.over_kb
    if over_kb > 0:
        raise TuneError(
            f"pg-tune: OVER-COMMITTED by {over_kb} kB; refusing database changes. "
            "Reconcile reserved memory, serving connections, or active queries before applying.",
            exit_code=1,
        )


def _fetch_report_rows(
    result: TuneResult, psql_exe: str, conninfo: str
) -> list[tuple[str, str, str, str, str, str]]:
    lines = run_psql(psql_exe, conninfo, build_report_sql(result.settings))
    rows: list[tuple[str, str, str, str, str, str]] = []
    for line in lines:
        parts = line.split("|", 5)
        if len(parts) != 6:
            raise TuneError(f"pg-tune: unexpected report row from psql: {line!r}", exit_code=1)
        rows.append((parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]))
    return rows


def check_postgres(result: TuneResult, psql_bin: str | None, conninfo: str | None) -> int:
    _validate_not_overcommitted(result)
    psql_exe, conn = _resolve_psql_executable(psql_bin, conninfo)
    rows = _fetch_report_rows(result, psql_exe, conn)
    bad = 0
    for name, match, _context, source, pending, live in rows:
        if match != "t" or pending == "t":
            want = result.settings.get(name, "")
            print(
                f"pg-tune: {name} = '{live}' declared '{want}' source={source} pending={pending}",
                file=sys.stderr,
            )
            bad += 1
    if len(rows) != len(result.settings):
        print(
            f"pg-tune: {len(rows)} of {len(result.settings)} settings read",
            file=sys.stderr,
        )
        return 1
    if bad:
        return 1
    print(f"pg-tune: {len(rows)} settings match")
    return 0


def apply_postgres(result: TuneResult, psql_bin: str | None, conninfo: str | None) -> int:
    _validate_not_overcommitted(result)
    psql_exe, conn = _resolve_psql_executable(psql_bin, conninfo)
    rows = _fetch_report_rows(result, psql_exe, conn)
    applied = 0
    pending_restart: list[str] = []
    for name, match, context, source, pending, _live in rows:
        if pending == "t":
            pending_restart.append(name)
        if match == "t" and (source == "configuration file" or context == "postmaster"):
            continue
        value = result.settings[name]
        run_psql(psql_exe, conn, build_alter_system_sql(name, value))
        print(f"pg-tune: set {name} = {value}")
        applied += 1
        if context == "postmaster":
            pending_restart.append(name)

    if applied > 0:
        run_psql(psql_exe, conn, "SELECT pg_reload_conf();")

    print(f"pg-tune: {len(result.settings)} declared, {applied} set")
    if pending_restart:
        unique_pending = sorted(set(pending_restart))
        print(
            f"pg-tune: restart required for: {', '.join(unique_pending)}",
            file=sys.stderr,
        )
    return 0


# ---------------------------------------------------------------------------
# Config & explicit --env loading
# ---------------------------------------------------------------------------


def _normalize_config_key(key: str) -> str | None:
    norm = key.strip().lower().replace("-", "_")
    if norm.startswith("pg_tune_"):
        norm = norm[len("pg_tune_") :]
    return _ENV_KEY_MAP.get(norm)


def load_json_config(config_path: str) -> dict[str, Any]:
    if config_path == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise TuneError(f"pg-tune: cannot read config '{config_path}': {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TuneError(f"pg-tune: invalid JSON in '{config_path}': {exc}") from exc
    if not isinstance(data, dict):
        raise TuneError("pg-tune: JSON config root must be an object")

    out: dict[str, Any] = {}
    for k, v in data.items():
        mapped = _normalize_config_key(str(k))
        if mapped is not None and v is not None:
            out[mapped] = v
    return out


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None
    k, v = stripped.split("=", 1)
    k = k.strip()
    v = v.strip()
    if len(v) >= 2 and ((v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')):
        v = v[1:-1]
    return k, v


def load_env_spec(spec: str) -> dict[str, Any]:
    """Load config from KEY=VALUE, a .env file path, or os.environ with an explicit prefix."""
    out: dict[str, Any] = {}
    if "=" in spec and not os.path.isfile(spec):
        k, v = spec.split("=", 1)
        mapped = _normalize_config_key(k)
        if mapped is None:
            raise TuneError(f"pg-tune: unknown env key '{k}'")
        out[mapped] = v
        return out

    path = Path(spec)
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise TuneError(f"pg-tune: cannot read env file '{spec}': {exc}") from exc
        for line in lines:
            parsed = _parse_dotenv_line(line)
            if not parsed:
                continue
            mapped = _normalize_config_key(parsed[0])
            if mapped is not None and parsed[1] != "":
                out[mapped] = parsed[1]
        return out

    if os.path.sep in spec or (os.path.altsep and os.path.altsep in spec) or spec.endswith(".env"):
        raise TuneError(f"pg-tune: env file not found: '{spec}'")

    prefix = spec if spec.endswith("_") else f"{spec}_"
    for env_k, env_v in os.environ.items():
        if not env_k.startswith(prefix) or env_v == "":
            continue
        suffix = env_k[len(prefix) :]
        mapped = _normalize_config_key(suffix)
        if mapped is not None:
            out[mapped] = env_v
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-tune",
        description="Compute and optionally apply hardware- and workload-tuned PostgreSQL 18 settings.",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--config", metavar="JSON", help="Load inputs from JSON file (or '-' for stdin)")
    parser.add_argument(
        "--env",
        action="append",
        nargs="?",
        const="PG_TUNE_",
        metavar="PREFIX|FILE|K=V",
        help="Read inputs from os.environ prefix (default PG_TUNE_), .env file, or KEY=VALUE",
    )

    topo = parser.add_argument_group("hardware and storage topology (auto-detected when omitted)")
    topo.add_argument("--ram-kb", metavar="SIZE", help="Total physical memory (kB or size like 64GB)")
    topo.add_argument("--cores", metavar="N", help="Physical CPU core count")
    topo.add_argument("--threads", metavar="N", help="Logical CPU thread count")
    topo.add_argument("--wal-bytes", metavar="SIZE", help="WAL volume size (bytes or size like 64GB)")
    topo.add_argument(
        "--wal-shared",
        nargs="?",
        const="1",
        metavar="0|1",
        help="1 if WAL shares its volume with data/other files, 0 if dedicated",
    )
    topo.add_argument(
        "--rotational",
        nargs="?",
        const="1",
        metavar="0|1",
        help="1 if data disk rotates (HDD), 0 for SSD/NVMe",
    )
    topo.add_argument("--reserved-kb", metavar="SIZE", help="Memory reserved for co-located workloads")
    topo.add_argument("--buffers-max-kb", metavar="SIZE", help="Upper cap on shared_buffers (0 = uncapped)")
    topo.add_argument("--data-path", metavar="PATH", help="Data directory path for local auto-detection")
    topo.add_argument("--wal-path", metavar="PATH", help="WAL directory path for local auto-detection")
    topo.add_argument("--os", choices=("linux", "windows", "auto"), help="Target OS (linux, windows, auto)")

    workload = parser.add_argument_group("workload profile and PostgreSQL knobs")
    workload.add_argument("--wal-level", choices=("minimal", "replica"), help="WAL level")
    workload.add_argument("--max-wal-senders", metavar="N", help="Override max_wal_senders")
    workload.add_argument("--huge-pages", choices=("try", "off", "on"), help="huge_pages setting")
    workload.add_argument("--io-method", choices=("worker", "sync"), help="Asynchronous I/O method")
    workload.add_argument("--ingest-conns", metavar="N", help="Connection slots budgeted for ingest")
    workload.add_argument("--serving-conns", metavar="N", help="Connection slots budgeted for serving pools")
    workload.add_argument("--active-queries", metavar="N", help="Estimated concurrent active query leaders")
    workload.add_argument("--work-nodes", metavar="N", help="Simultaneous sort/hash nodes per active executor")
    workload.add_argument("--locks-per-tx", metavar="N", help="max_locks_per_transaction")
    workload.add_argument("--port", metavar="PORT", help="PostgreSQL listen port (default 5432)")
    workload.add_argument("--temp-tablespaces", metavar="LIST", help="Comma-separated temp_tablespaces")
    workload.add_argument("--trace", choices=("full", "reduced", "off"), help="Instrumentation trace level")
    workload.add_argument("--preload", metavar="LIST", help="Comma-separated shared_preload_libraries")
    workload.add_argument("--listen-addresses", metavar="LIST", help="listen_addresses (default '*')")
    workload.add_argument("--ssl", choices=("on", "off"), help="Explicitly emit ssl = on|off")

    output = parser.add_argument_group("output and live PostgreSQL actions")
    output.add_argument(
        "--format",
        choices=("json", "plan", "conf", "env", "declared"),
        help="Output format (default: plan)",
    )
    output.add_argument("--plan", action="store_true", help="Alias for --format plan")
    output.add_argument("--validate", action="store_true", help="Exit non-zero if over_kb > 0")
    action_group = output.add_mutually_exclusive_group()
    action_group.add_argument(
        "--check", action="store_true", help="Check live server settings via --psql and --conninfo"
    )
    action_group.add_argument(
        "--apply", action="store_true", help="Apply ALTER SYSTEM SET and reload via --psql and --conninfo"
    )
    output.add_argument("--psql", metavar="PATH", help="Path or binary name for psql")
    output.add_argument("--conninfo", metavar="STR", help="libpq connection string for --check / --apply")
    return parser


def _merge_inputs(args: argparse.Namespace) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if args.config is not None:
        merged.update(load_json_config(args.config))
    if args.env:
        for spec in args.env:
            merged.update(load_env_spec(spec))

    cli_keys = (
        "ram_kb",
        "cores",
        "threads",
        "wal_bytes",
        "wal_shared",
        "rotational",
        "reserved_kb",
        "buffers_max_kb",
        "data_path",
        "wal_path",
        "os",
        "wal_level",
        "max_wal_senders",
        "huge_pages",
        "io_method",
        "ingest_conns",
        "serving_conns",
        "active_queries",
        "work_nodes",
        "locks_per_tx",
        "port",
        "temp_tablespaces",
        "trace",
        "preload",
        "listen_addresses",
        "ssl",
        "psql",
        "conninfo",
        "format",
    )
    for key in cli_keys:
        val = getattr(args, key, None)
        if val is not None:
            merged[key] = val
    if args.plan and args.format is None:
        merged["format"] = "plan"
    return merged


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(read_version())
        return 0

    try:
        raw = _merge_inputs(args)
        result = generate_plan(raw)

        if args.check:
            return check_postgres(result, raw.get("psql"), raw.get("conninfo"))
        if args.apply:
            return apply_postgres(result, raw.get("psql"), raw.get("conninfo"))

        fmt = str(raw.get("format") or "plan").strip().lower()
        if fmt == "json":
            print(format_json(result))
        elif fmt == "plan":
            print(format_plan(result))
        elif fmt == "conf":
            print(format_conf(result))
        elif fmt == "env":
            print(format_env(result))
        elif fmt == "declared":
            print(format_declared(result))
        else:
            raise TuneError(f"pg-tune: unsupported format '{fmt}'")

        if result.budget.over_kb > 0 and (fmt == "plan" or args.validate):
            print(
                f"pg-tune: OVER-COMMITTED by {result.budget.over_kb // 1024} MB "
                f"({result.budget.over_kb} kB): declared connections at PostgreSQL floors "
                "exceed the private memory quarter",
                file=sys.stderr,
            )
        if args.validate and result.budget.over_kb > 0:
            return 1
        return 0
    except TuneError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
