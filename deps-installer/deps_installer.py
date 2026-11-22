#!/usr/bin/env python3
"""Generic pinned dependency manifest installer, planner, and verifier."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

MANIFEST_COLUMNS = (
    "name",
    "version",
    "kind",
    "origin",
    "ref",
    "pin",
    "build",
    "profile",
    "platforms",
    "depends",
    "use",
    "proof",
)

VALID_KINDS = frozenset({"source", "release", "tool", "data"})
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")

BUILTIN_PROFILES: dict[str, dict] = {
    "icx-deterministic": {
        "name": "icx-deterministic",
        "description": "Deterministic Intel LLVM compiler profile with strict IEEE-754 floating-point semantics",
        "c_compiler": "icx",
        "cxx_compiler": {"windows": "icx", "default": "icpx"},
        "fp_flags": {
            "gnu": [
                "-fp-model=precise",
                "-ffp-contract=off",
                "-fno-fast-math",
                "-fsigned-zeros",
                "-march=x86-64-v2",
                "-Wno-overriding-option",
            ],
            "msvc": [
                "/fp:precise",
                "/clang:-ffp-contract=off",
                "/clang:-fno-fast-math",
                "/clang:-fsigned-zeros",
                "/clang:-march=x86-64-v2",
                "-Wno-overriding-option",
            ],
        },
        "link_flags": {
            "gnu": ["-no-intel-lib=libimf,libsvml,libirng"],
            "msvc": ["/NODEFAULTLIB:libmmd.lib"],
        },
        "deterministic_fp": True,
    },
    "icx": {
        "name": "icx",
        "description": "Intel LLVM compiler profile with precise floating-point model",
        "c_compiler": "icx",
        "cxx_compiler": {"windows": "icx", "default": "icpx"},
        "fp_flags": {
            "gnu": [
                "-fp-model=precise",
                "-ffp-contract=off",
                "-fno-fast-math",
                "-fsigned-zeros",
            ],
            "msvc": [
                "/fp:precise",
                "/clang:-ffp-contract=off",
                "/clang:-fno-fast-math",
                "/clang:-fsigned-zeros",
            ],
        },
        "link_flags": {"gnu": [], "msvc": []},
        "deterministic_fp": True,
    },
    "upstream-default": {
        "name": "upstream-default",
        "description": "Upstream default compiler flags without deterministic floating-point overrides",
        "c_compiler": None,
        "cxx_compiler": None,
        "fp_flags": {"gnu": [], "msvc": []},
        "link_flags": {"gnu": [], "msvc": []},
        "deterministic_fp": False,
    },
    "none": {
        "name": "none",
        "description": "No compilation toolchain profile",
        "c_compiler": None,
        "cxx_compiler": None,
        "fp_flags": {"gnu": [], "msvc": []},
        "link_flags": {"gnu": [], "msvc": []},
        "deterministic_fp": False,
    },
}


class ManifestError(ValueError):
    """Raised when a dependency manifest is malformed or invalid."""


class DependencyCycleError(ManifestError):
    """Raised when a circular dependency is detected."""


class PinVerificationError(ValueError):
    """Raised when a checksum, commit pin, or receipt check fails."""


class UnsafePathError(ValueError):
    """Raised when a path escapes its root or traverses a symlink/junction."""


@dataclass(frozen=True)
class DependencyEntry:
    name: str
    version: str
    kind: str
    origin: str
    ref: str
    pin: str
    build: str
    profile: str
    platforms: tuple[str, ...]
    depends: tuple[str, ...]
    use: str
    proof: str
    line_number: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "origin": self.origin,
            "ref": self.ref,
            "pin": self.pin,
            "build": self.build,
            "profile": self.profile,
            "platforms": list(self.platforms),
            "depends": list(self.depends),
            "use": self.use,
            "proof": self.proof,
            "line_number": self.line_number,
        }


@dataclass(frozen=True)
class InstallLayout:
    prefix_root: Path
    src_root: Path
    build_root: Path
    data_root: Path | None = None
    archive_cache: Path | None = None
    prefix_overrides: dict[str, Path] | None = None

    def resolved_data_root(self) -> Path:
        return self.data_root if self.data_root is not None else self.prefix_root / "data"

    def entry_prefix(self, entry: DependencyEntry) -> Path:
        if self.prefix_overrides and entry.name in self.prefix_overrides:
            return self.prefix_overrides[entry.name]
        if entry.kind == "source" and entry.build == "none":
            return self.src_root / entry.name
        if entry.kind == "data":
            return self.resolved_data_root()
        return self.prefix_root / entry.name / entry.version

    def entry_checkout_dir(self, entry: DependencyEntry) -> Path:
        if entry.kind == "source":
            return self.src_root / entry.name
        return self.src_root / entry.name / entry.version

    def entry_build_dir(self, entry: DependencyEntry) -> Path:
        return self.build_root / entry.name

    def entry_proof_path(self, entry: DependencyEntry) -> Path | None:
        if not entry.proof:
            return None
        rel = _validate_relative_path(entry.proof, f"proof for {entry.name}")
        return self.entry_prefix(entry) / rel

    def entry_receipt_path(self, entry: DependencyEntry) -> Path:
        if entry.kind == "data":
            proof_path = self.entry_proof_path(entry)
            if proof_path is not None:
                return Path(str(proof_path) + ".pin")
            return self.resolved_data_root() / f".{entry.name}-{entry.version}.pin"
        if entry.kind == "source" and entry.build == "none":
            return self.prefix_root / entry.name / entry.version / ".pin"
        return self.entry_prefix(entry) / ".pin"


def read_version() -> str:
    version_file = Path(__file__).resolve().with_name("VERSION")
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.1.0"


def detect_platform() -> str:
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "darwin"
    return sys.platform


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    if os.name == "nt" and hasattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT"):
        try:
            attrs = os.lstat(path).st_file_attributes
            if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        except OSError:
            pass
    return False


def _assert_no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if _is_link_or_junction(candidate):
            raise UnsafePathError(f"Refusing to traverse symlink or junction: {candidate}")


def _validate_safe_segment(value: str, label: str) -> str:
    if not value or not _SAFE_SEGMENT_RE.fullmatch(value) or value in (".", ".."):
        raise ManifestError(f"Invalid {label}: {value!r}")
    return value


def _validate_relative_path(raw: str, label: str) -> Path:
    normalized = raw.replace("\\", "/")
    p = Path(normalized)
    if p.is_absolute() or p.drive or ".." in p.parts or "\x00" in raw:
        raise UnsafePathError(f"Unsafe relative path in {label}: {raw!r}")
    return p


def _split_csv(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    items = [item.strip() for item in value.split(",") if item.strip()]
    return tuple(items)


def _is_git_pinned(kind: str, origin: str, build: str) -> bool:
    return kind == "source" or origin.endswith(".git") or build in ("cargo", "stockfish", "lc0")


def _validate_pin_format(kind: str, origin: str, build: str, pin: str, line_number: int, name: str) -> str:
    if not pin:
        return ""
    if _is_git_pinned(kind, origin, build):
        if not (_SHA1_RE.fullmatch(pin) or _SHA256_RE.fullmatch(pin)):
            raise ManifestError(
                f"Line {line_number} ({name}): git pin must be a 40- or 64-character hex commit SHA, got {pin!r}"
            )
    else:
        if not _SHA256_RE.fullmatch(pin):
            raise ManifestError(
                f"Line {line_number} ({name}): {kind} pin must be a 64-character SHA-256 hex digest, got {pin!r}"
            )
    return pin.lower()


def parse_manifest(path: Path | str) -> list[DependencyEntry]:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise ManifestError(f"Manifest file does not exist: {manifest_path}")
    text = manifest_path.read_text(encoding="utf-8")
    columns: list[str] | None = None
    col_index: dict[str, int] = {}
    entries: list[DependencyEntry] = []
    seen_name_platform: set[tuple[str, str]] = set()

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = raw_line.split("\t")
        if columns is None:
            columns = [f.strip() for f in fields]
            if len(set(columns)) != len(columns):
                raise ManifestError(f"Line {line_no}: duplicate column names in header")
            missing = [col for col in MANIFEST_COLUMNS if col not in columns]
            if missing:
                raise ManifestError(f"Line {line_no}: manifest header missing required columns: {', '.join(missing)}")
            col_index = {col: idx for idx, col in enumerate(columns)}
            continue
        if len(fields) != len(columns):
            raise ManifestError(
                f"Line {line_no}: expected {len(columns)} tab-separated fields, got {len(fields)}"
            )

        def field(col: str) -> str:
            val = fields[col_index[col]].strip()
            return "" if val == "-" else val

        name = _validate_safe_segment(field("name"), f"dependency name at line {line_no}")
        version = _validate_safe_segment(field("version"), f"version for {name} at line {line_no}")
        kind = field("kind")
        if kind not in VALID_KINDS:
            raise ManifestError(
                f"Line {line_no} ({name}): invalid kind {kind!r}; expected one of {', '.join(sorted(VALID_KINDS))}"
            )
        origin = field("origin")
        ref = field("ref")
        build = field("build") or "none"
        pin = _validate_pin_format(kind, origin, build, field("pin"), line_no, name)
        profile = field("profile") or "none"
        platforms = _split_csv(field("platforms"))
        if not platforms:
            raise ManifestError(f"Line {line_no} ({name}): platforms column cannot be empty")
        depends = _split_csv(field("depends"))
        if name in depends:
            raise DependencyCycleError(f"Line {line_no} ({name}): dependency cannot depend on itself: {name} -> {name}")
        use = field("use")
        proof = field("proof")
        if proof:
            _validate_relative_path(proof, f"line {line_no} ({name}) proof")

        for plat in platforms:
            key = (name, plat)
            if key in seen_name_platform:
                raise ManifestError(
                    f"Line {line_no} ({name}): duplicate dependency declaration for platform {plat!r}"
                )
            seen_name_platform.add(key)

        entries.append(
            DependencyEntry(
                name=name,
                version=version,
                kind=kind,
                origin=origin,
                ref=ref,
                pin=pin,
                build=build,
                profile=profile,
                platforms=platforms,
                depends=depends,
                use=use,
                proof=proof,
                line_number=line_no,
            )
        )

    if columns is None:
        raise ManifestError(f"Manifest has no header row: {manifest_path}")

    declared_names = {entry.name for entry in entries}
    for entry in entries:
        unknown = [dep for dep in entry.depends if dep not in declared_names]
        if unknown:
            raise ManifestError(
                f"Line {entry.line_number} ({entry.name}): unknown dependencies: {', '.join(unknown)}"
            )

    return entries


def filter_manifest(entries: list[DependencyEntry], platform: str | None = None) -> list[DependencyEntry]:
    target = detect_platform() if platform is None else platform
    if target == "all":
        return list(entries)
    filtered = [
        entry
        for entry in entries
        if target in entry.platforms or "all" in entry.platforms
    ]
    seen: set[str] = set()
    for entry in filtered:
        if entry.name in seen:
            raise ManifestError(f"Duplicate dependency {entry.name!r} for platform {target!r}")
        seen.add(entry.name)
    return filtered


def _find_cycle(graph: dict[str, list[str]]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, ()):
            nxt_state = state.get(nxt, 0)
            if nxt_state == 1:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if nxt_state == 0:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in graph:
        if state.get(node, 0) == 0:
            found = dfs(node)
            if found:
                return found
    return []


def resolve_order(
    entries: list[DependencyEntry],
    names: list[str] | None = None,
    *,
    include_depends: bool = True,
) -> list[DependencyEntry]:
    by_name: dict[str, DependencyEntry] = {}
    index_by_name: dict[str, int] = {}
    for idx, entry in enumerate(entries):
        if entry.name in by_name:
            raise ManifestError(
                f"Topological resolution requires unique entry names (filter by --platform first): {entry.name!r}"
            )
        by_name[entry.name] = entry
        index_by_name[entry.name] = idx

    if names:
        missing = [name for name in names if name not in by_name]
        if missing:
            raise ManifestError(f"Requested dependencies not found for target platform: {', '.join(missing)}")
        selected: set[str] = set()
        queue = list(names)
        while queue:
            cur = queue.pop(0)
            if cur in selected:
                continue
            selected.add(cur)
            if include_depends:
                for dep in by_name[cur].depends:
                    if dep in by_name and dep not in selected:
                        queue.append(dep)
        active = [entry for entry in entries if entry.name in selected]
    else:
        active = list(entries)

    active_names = {entry.name for entry in active}
    indegree: dict[str, int] = {entry.name: 0 for entry in active}
    dependents: dict[str, list[str]] = {entry.name: [] for entry in active}
    dep_edges: dict[str, list[str]] = {entry.name: [] for entry in active}

    for entry in active:
        seen_deps: set[str] = set()
        for dep in entry.depends:
            # Dependencies declared only on other platforms are absent from the filtered active set.
            if dep in active_names and dep not in seen_deps:
                seen_deps.add(dep)
                dependents[dep].append(entry.name)
                dep_edges[entry.name].append(dep)
                indegree[entry.name] += 1

    heap: list[tuple[int, str]] = [
        (index_by_name[name], name) for name, deg in indegree.items() if deg == 0
    ]
    heapq.heapify(heap)
    ordered: list[DependencyEntry] = []

    while heap:
        _, current = heapq.heappop(heap)
        ordered.append(by_name[current])
        for nxt in dependents[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, (index_by_name[nxt], nxt))

    if len(ordered) != len(active):
        cycle = _find_cycle(dep_edges)
        cycle_desc = " -> ".join(cycle) if cycle else ", ".join(sorted(n for n, d in indegree.items() if d > 0))
        raise DependencyCycleError(f"Circular dependency detected: {cycle_desc}")

    return ordered


def load_profiles(profile_config: Path | str | None = None) -> dict[str, dict]:
    profiles = {k: json.loads(json.dumps(v)) for k, v in BUILTIN_PROFILES.items()}
    if profile_config is None:
        return profiles
    config_path = Path(profile_config)
    if not config_path.is_file():
        raise ManifestError(f"Profile configuration file does not exist: {config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError(f"Profile configuration must be a JSON object: {config_path}")
    custom = raw.get("profiles") if isinstance(raw.get("profiles"), dict) and set(raw.keys()) <= {"profiles", "schema", "description"} else raw
    for name, body in custom.items():
        if not isinstance(name, str) or not isinstance(body, dict):
            raise ManifestError(f"Invalid profile entry {name!r} in {config_path}")
        merged = dict(profiles.get(name, {}))
        merged.update(body)
        merged["name"] = name
        profiles[name] = merged
    return profiles


def resolve_profile(profile_name: str, profiles: dict[str, dict] | None = None, *, platform: str | None = None) -> dict:
    table = profiles if profiles is not None else BUILTIN_PROFILES
    key = profile_name.strip() if profile_name else "none"
    if key == "-":
        key = "none"
    if key not in table:
        raise ManifestError(f"Unknown toolchain profile {key!r}; available: {', '.join(sorted(table))}")
    spec = dict(table[key])
    target = detect_platform() if platform is None else platform
    driver = "msvc" if target == "windows" else "gnu"
    cxx_raw = spec.get("cxx_compiler")
    if isinstance(cxx_raw, dict):
        cxx = cxx_raw.get(target, cxx_raw.get("default"))
    else:
        cxx = cxx_raw
    fp_raw = spec.get("fp_flags", {})
    fp_flags = fp_raw.get(driver, fp_raw.get("default", [])) if isinstance(fp_raw, dict) else list(fp_raw or [])
    link_raw = spec.get("link_flags", {})
    link_flags = link_raw.get(driver, link_raw.get("default", [])) if isinstance(link_raw, dict) else list(link_raw or [])
    return {
        "name": key,
        "description": spec.get("description", ""),
        "driver": driver,
        "c_compiler": spec.get("c_compiler"),
        "cxx_compiler": cxx,
        "fp_flags": list(fp_flags),
        "link_flags": list(link_flags),
        "deterministic_fp": bool(spec.get("deterministic_fp", False)),
    }


def compute_sha256(path: Path | str) -> str:
    target = Path(path)
    _assert_no_links(target)
    if not target.is_file():
        raise FileNotFoundError(f"File does not exist for SHA-256 verification: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path | str, expected_pin: str) -> str:
    expected = expected_pin.strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise PinVerificationError(f"Expected pin is not a valid SHA-256 hex digest: {expected_pin!r}")
    actual = compute_sha256(path)
    if actual != expected:
        raise PinVerificationError(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")
    return actual


def write_pin_receipt(receipt_path: Path | str, payload: dict) -> Path:
    path = Path(receipt_path)
    _assert_no_links(path.parent)
    if _is_link_or_junction(path):
        raise UnsafePathError(f"Refusing to overwrite symlink or junction receipt: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pin-", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def read_pin_receipt(receipt_path: Path | str) -> dict | None:
    path = Path(receipt_path)
    if _is_link_or_junction(path):
        raise UnsafePathError(f"Refusing to read symlink or junction receipt: {path}")
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise PinVerificationError(f"Malformed JSON receipt at {path}")
        return data
    return {"pin": raw.lower(), "legacy_text": True}


def _make_pin_payload(
    entry: DependencyEntry,
    *,
    pin_algorithm: str,
    prefix: Path,
    archive_sha256: str | None = None,
    commit: str | None = None,
    source_path: str | None = None,
    files_extracted: int | None = None,
) -> dict:
    payload: dict = {
        "schema": "deps-installer.pin/v1",
        "name": entry.name,
        "version": entry.version,
        "kind": entry.kind,
        "origin": entry.origin,
        "ref": entry.ref,
        "pin": entry.pin,
        "pin_algorithm": pin_algorithm,
        "build": entry.build,
        "profile": entry.profile,
        "prefix": str(prefix),
        "proof": entry.proof,
        "recorded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if archive_sha256 is not None:
        payload["archive_sha256"] = archive_sha256
    if commit is not None:
        payload["commit"] = commit
    if source_path is not None:
        payload["source_path"] = source_path
    if files_extracted is not None:
        payload["files_extracted"] = files_extracted
    return payload


def _normalize_git_url(url: str) -> str:
    stripped = url.strip().rstrip("/")
    if stripped.endswith(".git"):
        stripped = stripped[:-4]
    return stripped


def _run_git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git"]
    if cwd is not None:
        cmd.extend(["-C", str(cwd)])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def checkout_commit(
    entry: DependencyEntry,
    checkout_dir: Path | str,
    *,
    apply: bool = False,
    offline: bool = False,
    receipt_path: Path | str | None = None,
) -> dict:
    if not entry.pin:
        raise PinVerificationError(f"Source dependency {entry.name!r} has no commit pin")
    if not (_SHA1_RE.fullmatch(entry.pin) or _SHA256_RE.fullmatch(entry.pin)):
        raise PinVerificationError(f"Invalid commit pin for {entry.name!r}: {entry.pin!r}")

    dest = Path(checkout_dir)
    _assert_no_links(dest)
    result = {
        "action": "checkout_commit",
        "name": entry.name,
        "version": entry.version,
        "origin": entry.origin,
        "dest": str(dest),
        "pin": entry.pin,
        "apply": apply,
    }
    if not apply:
        result["status"] = "planned"
        return result

    git_dir = dest / ".git"
    if git_dir.exists():
        origin_proc = _run_git(["remote", "get-url", "origin"], cwd=dest)
        if origin_proc.returncode == 0 and entry.origin:
            actual_origin = _normalize_git_url(origin_proc.stdout)
            expected_origin = _normalize_git_url(entry.origin)
            if actual_origin != expected_origin:
                raise RuntimeError(
                    f"Checkout {dest} origin {actual_origin!r} does not match manifest origin {expected_origin!r}"
                )
        head_proc = _run_git(["rev-parse", "HEAD"], cwd=dest)
        head = head_proc.stdout.strip().lower() if head_proc.returncode == 0 else ""
        has_index = (git_dir / "index").exists()
        if head == entry.pin and has_index:
            status_kind = "already_checked_out"
        else:
            if has_index:
                dirty = _run_git(["status", "--porcelain", "--untracked-files=no"], cwd=dest)
                if dirty.returncode != 0:
                    raise RuntimeError(f"git status failed in {dest}: {dirty.stderr.strip()}")
                if dirty.stdout.strip():
                    raise RuntimeError(f"Checkout {dest} has local changes at {head}; refusing to move to {entry.pin}")
            if not offline:
                fetch = _run_git(["fetch", "-q", "origin", entry.pin], cwd=dest)
                if fetch.returncode != 0 and entry.ref:
                    _run_git(["fetch", "-q", "origin", entry.ref], cwd=dest)
            co = _run_git(["-c", "advice.detachedHead=false", "checkout", "-q", entry.pin], cwd=dest)
            if co.returncode != 0:
                raise RuntimeError(f"Failed to check out {entry.pin} in {dest}: {co.stderr.strip()}")
            verify_proc = _run_git(["rev-parse", "HEAD"], cwd=dest)
            verified_head = verify_proc.stdout.strip().lower()
            if verified_head != entry.pin:
                raise PinVerificationError(
                    f"Checked out HEAD {verified_head!r} does not match expected pin {entry.pin!r}"
                )
            status_kind = "checked_out"
    else:
        if not entry.origin:
            raise ManifestError(f"Source dependency {entry.name!r} has no origin")
        origin_path = Path(entry.origin)
        is_local = entry.origin.startswith("file://") or origin_path.exists()
        if offline and not is_local:
            raise RuntimeError(f"Offline mode prevents cloning remote repository {entry.origin}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone = _run_git(["clone", "-q", "--filter=blob:none", "--no-checkout", entry.origin, str(dest)])
        if clone.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            clone = _run_git(["clone", "-q", "--no-checkout", entry.origin, str(dest)])
            if clone.returncode != 0:
                raise RuntimeError(f"git clone failed for {entry.origin}: {clone.stderr.strip()}")
        if not offline or is_local:
            _run_git(["fetch", "-q", "origin", entry.pin], cwd=dest)
        co = _run_git(["-c", "advice.detachedHead=false", "checkout", "-q", entry.pin], cwd=dest)
        if co.returncode != 0:
            raise RuntimeError(f"git checkout {entry.pin} failed in {dest}: {co.stderr.strip()}")
        verify_proc = _run_git(["rev-parse", "HEAD"], cwd=dest)
        verified_head = verify_proc.stdout.strip().lower()
        if verified_head != entry.pin:
            raise PinVerificationError(
                f"Cloned HEAD {verified_head!r} does not match expected pin {entry.pin!r}"
            )
        status_kind = "cloned_and_checked_out"

    if receipt_path is not None:
        receipt = _make_pin_payload(
            entry,
            pin_algorithm="git-commit",
            prefix=dest,
            commit=entry.pin,
        )
        write_pin_receipt(receipt_path, receipt)
        result["receipt_path"] = str(receipt_path)

    result["status"] = status_kind
    return result


def _validate_archive_member(member_name: str) -> Path:
    cleaned = member_name.replace("\\", "/")
    rel = Path(cleaned)
    if rel.is_absolute() or rel.drive or ".." in rel.parts or "\x00" in member_name:
        raise UnsafePathError(f"Archive contains unsafe member path: {member_name!r}")
    return rel


def _safe_extract_zip(archive: Path, stage_dir: Path) -> int:
    count = 0
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            rel = _validate_archive_member(info.filename)
            if not rel.parts:
                continue
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise UnsafePathError(f"Archive contains disallowed symlink entry: {info.filename!r}")
            dest = stage_dir / rel
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            count += 1
    return count


def _safe_extract_tar(archive: Path, stage_dir: Path) -> int:
    count = 0
    with tarfile.open(archive, "r:*") as tf:
        members = tf.getmembers()
        for member in members:
            rel = _validate_archive_member(member.name)
            if not rel.parts:
                continue
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise UnsafePathError(f"Archive contains disallowed special entry: {member.name!r}")
            if not (member.isdir() or member.isfile()):
                raise UnsafePathError(f"Archive contains unsupported entry type: {member.name!r}")
        for member in members:
            rel = _validate_archive_member(member.name)
            if not rel.parts:
                continue
            dest = stage_dir / rel
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                raise UnsafePathError(f"Could not read tar member: {member.name!r}")
            with extracted, dest.open("wb") as dst:
                shutil.copyfileobj(extracted, dst)
            count += 1
    return count


def _extract_archive(archive: Path, stage_dir: Path) -> int:
    lower = archive.name.lower()
    if lower.endswith(".zip"):
        return _safe_extract_zip(archive, stage_dir)
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")):
        return _safe_extract_tar(archive, stage_dir)
    raise ManifestError(f"Unsupported archive format for {archive.name!r}")


def build_archive(
    entry: DependencyEntry,
    archive_path: Path | str,
    prefix_dir: Path | str,
    *,
    apply: bool = False,
    receipt_path: Path | str | None = None,
    unwrap_single_dir: bool = True,
) -> dict:
    if not entry.pin:
        raise PinVerificationError(f"Archive dependency {entry.name!r} has no SHA-256 pin")

    archive = Path(archive_path)
    prefix = Path(prefix_dir)
    _assert_no_links(prefix)
    receipt_target = Path(receipt_path) if receipt_path is not None else prefix / ".pin"

    plan = {
        "action": "build_archive",
        "name": entry.name,
        "version": entry.version,
        "build": entry.build,
        "archive": str(archive),
        "prefix": str(prefix),
        "receipt_path": str(receipt_target),
        "pin": entry.pin,
        "apply": apply,
    }
    if not apply:
        plan["status"] = "planned"
        return plan

    actual_sha = verify_sha256(archive, entry.pin)

    if entry.build == "file":
        if not entry.proof:
            raise ManifestError(f"Data file dependency {entry.name!r} requires a proof path")
        target_file = prefix / _validate_relative_path(entry.proof, f"proof for {entry.name}")
        _assert_no_links(target_file)
        existing_receipt = read_pin_receipt(receipt_target)
        if existing_receipt is not None and target_file.is_file():
            if existing_receipt.get("pin") == entry.pin and compute_sha256(target_file) == entry.pin:
                plan["status"] = "already_installed"
                plan["archive_sha256"] = actual_sha
                return plan
            if existing_receipt.get("pin") != entry.pin:
                raise PinVerificationError(
                    f"Existing receipt at {receipt_target} holds pin {existing_receipt.get('pin')!r}, expected {entry.pin!r}"
                )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, target_file)
        payload = _make_pin_payload(
            entry,
            pin_algorithm="sha256",
            prefix=prefix,
            archive_sha256=actual_sha,
            source_path=str(archive),
            files_extracted=1,
        )
        write_pin_receipt(receipt_target, payload)
        plan["status"] = "installed"
        plan["archive_sha256"] = actual_sha
        plan["files_extracted"] = 1
        return plan

    existing_receipt = read_pin_receipt(receipt_target)
    if existing_receipt is not None:
        if existing_receipt.get("pin") == entry.pin:
            plan["status"] = "already_installed"
            plan["archive_sha256"] = actual_sha
            return plan
        raise PinVerificationError(
            f"{prefix} already holds pin {existing_receipt.get('pin')!r}, which differs from {entry.pin!r}"
        )

    prefix.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f".{entry.name}-stage-", dir=prefix.parent))
    try:
        file_count = _extract_archive(archive, stage_root)
        payload_root = stage_root
        if unwrap_single_dir and entry.build in ("unpack", "amalgamation", "zip"):
            children = list(stage_root.iterdir())
            if len(children) == 1 and children[0].is_dir():
                payload_root = children[0]

        if entry.build == "zip":
            if not entry.proof:
                raise ManifestError(f"Zip data dependency {entry.name!r} requires a proof path")
            files_in_zip = [p for p in payload_root.rglob("*") if p.is_file()]
            if len(files_in_zip) != 1:
                raise ManifestError(f"Zip data archive {archive} contains {len(files_in_zip)} files, expected 1")
            target_file = prefix / _validate_relative_path(entry.proof, f"proof for {entry.name}")
            _assert_no_links(target_file)
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files_in_zip[0], target_file)
            file_count = 1
        else:
            if prefix.exists():
                for item in payload_root.iterdir():
                    dest_item = prefix / item.name
                    if dest_item.exists():
                        if dest_item.is_dir():
                            shutil.rmtree(dest_item)
                        else:
                            dest_item.unlink()
                    shutil.move(str(item), str(dest_item))
            else:
                shutil.move(str(payload_root), str(prefix))
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)

    payload = _make_pin_payload(
        entry,
        pin_algorithm="sha256",
        prefix=prefix,
        archive_sha256=actual_sha,
        source_path=str(archive),
        files_extracted=file_count,
    )
    write_pin_receipt(receipt_target, payload)
    plan["status"] = "installed"
    plan["archive_sha256"] = actual_sha
    plan["files_extracted"] = file_count
    return plan


def _origin_filename(origin: str) -> str:
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme in ("http", "https", "file"):
        name = Path(urllib.parse.unquote(parsed.path)).name
        return name or "archive"
    return Path(origin).name or "archive"


def _resolve_archive_path(
    entry: DependencyEntry,
    layout: InstallLayout,
    *,
    apply: bool,
    offline: bool,
) -> Path:
    filename = _origin_filename(entry.origin) if entry.origin else f"{entry.name}-{entry.version}"
    if layout.archive_cache is not None:
        cached = layout.archive_cache / filename
        if cached.is_file():
            return cached
        nested = layout.archive_cache / entry.name / entry.version / filename
        if nested.is_file():
            return nested

    store_dir = layout.src_root / entry.name / entry.version
    stored = store_dir / filename
    if stored.is_file():
        return stored

    parsed = urllib.parse.urlparse(entry.origin)
    if parsed.scheme == "file":
        local = Path(urllib.request.url2pathname(parsed.path))
        if not local.is_file() and apply:
            raise FileNotFoundError(f"Local archive URI does not exist: {local}")
        return local
    if not parsed.scheme or len(parsed.scheme) == 1:
        local = Path(entry.origin)
        if local.is_file() or not apply:
            return local

    if not apply:
        return stored

    if offline:
        raise RuntimeError(f"Offline mode enabled and archive is not cached locally: {stored}")
    if not entry.origin:
        raise ManifestError(f"Dependency {entry.name!r} has no origin URL or local archive path")

    store_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".download-", dir=store_dir)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with urllib.request.urlopen(entry.origin, timeout=60) as response, tmp_path.open("wb") as out:
            shutil.copyfileobj(response, out)
        os.replace(tmp_path, stored)
    finally:
        tmp_path.unlink(missing_ok=True)
    return stored


def check_proof(entry: DependencyEntry, layout: InstallLayout) -> dict:
    prefix = layout.entry_prefix(entry)
    proof_path = layout.entry_proof_path(entry)
    receipt_path = layout.entry_receipt_path(entry)
    checkout_dir = layout.entry_checkout_dir(entry)

    receipt = read_pin_receipt(receipt_path) if receipt_path.exists() else None
    pin_verified = False
    status = "installed"
    details: list[str] = []

    if not entry.pin:
        details.append("unpinned in manifest")
    if receipt is not None:
        if entry.pin and receipt.get("pin") != entry.pin:
            status = "pin_mismatch"
            details.append(f"receipt pin {receipt.get('pin')!r} != {entry.pin!r}")
        elif entry.pin:
            pin_verified = True

    if _is_git_pinned(entry.kind, entry.origin, entry.build) and (checkout_dir / ".git").exists() and entry.pin:
        head_proc = _run_git(["rev-parse", "HEAD"], cwd=checkout_dir)
        if head_proc.returncode == 0:
            head = head_proc.stdout.strip().lower()
            if head != entry.pin:
                status = "pin_mismatch"
                details.append(f"checkout HEAD {head!r} != {entry.pin!r}")
            else:
                pin_verified = True

    if proof_path is None:
        if status == "installed":
            status = "no_proof_declared"
            details.append("manifest omits proof path")
    elif not proof_path.exists():
        status = "missing_proof"
        details.append(f"missing proof: {proof_path}")
    else:
        _assert_no_links(proof_path)
        if entry.kind == "data" and entry.build == "file" and entry.pin:
            actual = compute_sha256(proof_path)
            if actual != entry.pin:
                status = "pin_mismatch"
                details.append(f"data file sha256 {actual} != {entry.pin}")
            else:
                pin_verified = True

    if not entry.pin and status == "installed":
        status = "unpinned"

    return {
        "name": entry.name,
        "version": entry.version,
        "kind": entry.kind,
        "build": entry.build,
        "profile": entry.profile,
        "prefix": str(prefix),
        "proof_path": str(proof_path) if proof_path is not None else None,
        "receipt_path": str(receipt_path),
        "status": status,
        "proof_present": proof_path.exists() if proof_path is not None else None,
        "receipt_present": receipt is not None,
        "pin_verified": pin_verified,
        "details": "; ".join(details),
    }


def verify_proofs(entries: list[DependencyEntry], layout: InstallLayout) -> dict:
    results = [check_proof(entry, layout) for entry in entries]
    failures = [
        item for item in results if item["status"] in ("missing_proof", "pin_mismatch")
    ]
    return {
        "ok": len(failures) == 0,
        "total": len(results),
        "failures": len(failures),
        "results": results,
    }


def _describe_build_command(
    entry: DependencyEntry,
    layout: InstallLayout,
    profile_info: dict,
    dep_prefixes: list[Path],
) -> list[str] | None:
    src = layout.entry_checkout_dir(entry)
    bld = layout.entry_build_dir(entry)
    pfx = layout.entry_prefix(entry)
    if entry.build == "cmake":
        cmd = [
            "cmake",
            "-S",
            str(src),
            "-B",
            str(bld),
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={pfx}",
            "-DBUILD_TESTING=OFF",
        ]
        if profile_info["c_compiler"]:
            cmd.append(f"-DCMAKE_C_COMPILER={profile_info['c_compiler']}")
        if profile_info["cxx_compiler"]:
            cmd.append(f"-DCMAKE_CXX_COMPILER={profile_info['cxx_compiler']}")
        if dep_prefixes:
            joined = ";".join(p.as_posix() for p in dep_prefixes)
            cmd.append(f"-DCMAKE_PREFIX_PATH={joined}")
        return cmd
    if entry.build == "meson":
        return [
            "meson",
            "setup",
            str(bld),
            str(src),
            f"--prefix={pfx}",
            "--buildtype=release",
        ]
    if entry.build == "configure":
        return [str(src / "configure"), f"--prefix={pfx}"]
    if entry.build == "make":
        return ["make", "-C", str(src), f"PREFIX={pfx}"]
    return None


def plan_install(
    entries: list[DependencyEntry],
    layout: InstallLayout,
    *,
    platform: str | None = None,
    names: list[str] | None = None,
    profile_config: Path | str | None = None,
    offline: bool = False,
) -> dict:
    filtered = filter_manifest(entries, platform=platform)
    ordered = resolve_order(filtered, names=names)
    profiles = load_profiles(profile_config)
    entry_map = {e.name: e for e in filtered}

    steps: list[dict] = []
    for entry in ordered:
        profile_info = resolve_profile(entry.profile, profiles, platform=platform)
        proof_status = check_proof(entry, layout)
        dep_prefixes = [
            layout.entry_prefix(entry_map[d])
            for d in entry.depends
            if d in entry_map and entry_map[d].kind != "tool"
        ]
        actions: list[dict] = []

        if proof_status["status"] == "installed":
            actions.append({"action": "skip_installed", "reason": "proof and pin satisfied"})
        else:
            if _is_git_pinned(entry.kind, entry.origin, entry.build) and entry.pin:
                actions.append(
                    checkout_commit(
                        entry,
                        layout.entry_checkout_dir(entry),
                        apply=False,
                        offline=offline,
                        receipt_path=layout.entry_receipt_path(entry),
                    )
                )
            elif entry.kind in ("release", "tool", "data") and entry.build in (
                "unpack",
                "amalgamation",
                "file",
                "zip",
            ):
                archive_path = _resolve_archive_path(entry, layout, apply=False, offline=offline)
                target_prefix = (
                    layout.entry_checkout_dir(entry) / "source"
                    if entry.build == "amalgamation"
                    else layout.entry_prefix(entry)
                )
                actions.append({"action": "verify_sha256", "path": str(archive_path), "pin": entry.pin})
                actions.append(
                    build_archive(
                        entry,
                        archive_path,
                        target_prefix,
                        apply=False,
                        receipt_path=layout.entry_receipt_path(entry),
                    )
                )

            build_cmd = _describe_build_command(entry, layout, profile_info, dep_prefixes)
            if build_cmd is not None:
                actions.append(
                    {
                        "action": "build_step",
                        "driver": entry.build,
                        "profile": profile_info["name"],
                        "command": build_cmd,
                    }
                )

        steps.append(
            {
                "name": entry.name,
                "version": entry.version,
                "kind": entry.kind,
                "build": entry.build,
                "depends": list(entry.depends),
                "prefix": str(layout.entry_prefix(entry)),
                "checkout_dir": str(layout.entry_checkout_dir(entry)),
                "build_dir": str(layout.entry_build_dir(entry)),
                "proof_path": proof_status["proof_path"],
                "receipt_path": proof_status["receipt_path"],
                "current_status": proof_status["status"],
                "profile": profile_info,
                "actions": actions,
            }
        )

    return {
        "schema": "deps-installer.plan/v1",
        "platform": detect_platform() if platform is None else platform,
        "prefix": str(layout.prefix_root),
        "src_dir": str(layout.src_root),
        "build_dir": str(layout.build_root),
        "order": [step["name"] for step in steps],
        "steps": steps,
    }


def apply_install(
    entries: list[DependencyEntry],
    layout: InstallLayout,
    *,
    platform: str | None = None,
    names: list[str] | None = None,
    profile_config: Path | str | None = None,
    offline: bool = False,
    execute_builds: bool = False,
) -> dict:
    filtered = filter_manifest(entries, platform=platform)
    ordered = resolve_order(filtered, names=names)
    profiles = load_profiles(profile_config)
    entry_map = {e.name: e for e in filtered}

    applied: list[dict] = []
    for entry in ordered:
        profile_info = resolve_profile(entry.profile, profiles, platform=platform)
        before = check_proof(entry, layout)
        step_actions: list[dict] = []

        if before["status"] == "installed":
            step_actions.append({"action": "skip_installed", "status": "installed"})
        else:
            if _is_git_pinned(entry.kind, entry.origin, entry.build) and entry.pin:
                co_res = checkout_commit(
                    entry,
                    layout.entry_checkout_dir(entry),
                    apply=True,
                    offline=offline,
                    receipt_path=layout.entry_receipt_path(entry),
                )
                step_actions.append(co_res)
            elif entry.kind in ("release", "tool", "data") and entry.build in (
                "unpack",
                "amalgamation",
                "file",
                "zip",
            ):
                archive_path = _resolve_archive_path(entry, layout, apply=True, offline=offline)
                target_prefix = (
                    layout.entry_checkout_dir(entry) / "source"
                    if entry.build == "amalgamation"
                    else layout.entry_prefix(entry)
                )
                arch_res = build_archive(
                    entry,
                    archive_path,
                    target_prefix,
                    apply=True,
                    receipt_path=layout.entry_receipt_path(entry),
                )
                step_actions.append(arch_res)

            if execute_builds:
                dep_prefixes = [
                    layout.entry_prefix(entry_map[d])
                    for d in entry.depends
                    if d in entry_map and entry_map[d].kind != "tool"
                ]
                build_cmd = _describe_build_command(entry, layout, profile_info, dep_prefixes)
                if build_cmd is not None:
                    layout.entry_build_dir(entry).mkdir(parents=True, exist_ok=True)
                    proc = subprocess.run(build_cmd, capture_output=True, text=True)
                    if proc.returncode != 0:
                        raise RuntimeError(
                            f"Build step failed for {entry.name}: {(proc.stderr or proc.stdout).strip()}"
                        )
                    step_actions.append({"action": "build_step", "status": "completed", "command": build_cmd})

        after = check_proof(entry, layout)
        applied.append(
            {
                "name": entry.name,
                "version": entry.version,
                "profile": profile_info["name"],
                "actions": step_actions,
                "status": after["status"],
                "proof_path": after["proof_path"],
                "receipt_path": after["receipt_path"],
            }
        )

    return {
        "schema": "deps-installer.apply/v1",
        "platform": detect_platform() if platform is None else platform,
        "order": [item["name"] for item in applied],
        "steps": applied,
    }


def _parse_overrides(values: list[str]) -> dict[str, Path]:
    overrides: dict[str, Path] = {}
    for raw in values:
        if "=" not in raw:
            raise ManifestError(f"Prefix override must be NAME=PATH, got {raw!r}")
        name, path = raw.split("=", 1)
        overrides[_validate_safe_segment(name.strip(), "prefix override name")] = Path(path.strip())
    return overrides


def _print_text_status(report: dict) -> None:
    for item in report["results"]:
        suffix = f" ({item['details']})" if item["details"] else ""
        print(f"{item['name']:24s} {item['version']:14s} {item['status']:18s} {item['prefix']}{suffix}")


def _print_text_plan(plan: dict) -> None:
    print(f"Platform: {plan['platform']}  Prefix: {plan['prefix']}")
    print(f"Order ({len(plan['order'])}): {', '.join(plan['order'])}")
    for step in plan["steps"]:
        action_names = ", ".join(a["action"] for a in step["actions"])
        print(
            f"  {step['name']:22s} {step['version']:12s} [{step['profile']['name']}] "
            f"{step['current_status']} -> {action_names}"
        )


def _print_text_apply(report: dict) -> None:
    print(f"Applied ({len(report['steps'])}): {', '.join(report['order'])}")
    for step in report["steps"]:
        action_names = ", ".join(f"{a['action']}:{a.get('status', '')}" for a in step["actions"])
        print(f"  {step['name']:22s} {step['version']:12s} {step['status']} ({action_names})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--manifest", type=Path, help="Path to tab-separated dependency manifest")
    parser.add_argument("--platform", help="Target platform filter (default: host OS; 'all' for unfiltered)")
    parser.add_argument("--prefix", type=Path, default=Path("deps"), help="Isolated install root (<prefix>/<name>/<version>)")
    parser.add_argument("--src-dir", type=Path, default=Path("deps-src"), help="Source checkout and archive root")
    parser.add_argument("--build-dir", type=Path, default=Path("deps-build"), help="Out-of-tree build root")
    parser.add_argument("--data-dir", type=Path, help="Data artifact root (default: <prefix>/data)")
    parser.add_argument("--archive-cache", type=Path, help="Optional pre-populated archive directory")
    parser.add_argument("--profile-config", type=Path, help="Optional JSON file defining/extending toolchain profiles")
    parser.add_argument("--prefix-override", action="append", default=[], help="Override prefix for a package as NAME=PATH")
    parser.add_argument("--name", action="append", dest="names", help="Select specific dependency name(s) and their dependencies")
    parser.add_argument("--offline", action="store_true", help="Disallow network downloads/fetches")
    parser.add_argument("--execute-builds", action="store_true", help="Execute cmake/meson/configure build steps under --apply")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Report proof and pin verification status")
    mode.add_argument("--verify-proofs", action="store_true", help="Verify proofs and exit non-zero if any fail")
    mode.add_argument("--plan", action="store_true", help="Dry-run topological install plan")
    mode.add_argument("--apply", action="store_true", help="Apply checkouts, SHA-256 verification, archives, and receipts")
    mode.add_argument("--list-profiles", action="store_true", help="Print resolved toolchain profile definitions")
    mode.add_argument("--verify-sha256", type=Path, help="Verify a standalone file against --pin")
    parser.add_argument("--pin", help="Expected SHA-256 hex digest when using --verify-sha256")

    args = parser.parse_args(argv)

    if args.version:
        print(f"deps-installer {read_version()}")
        return 0

    if args.verify_sha256 is not None:
        if not args.pin:
            parser.error("--verify-sha256 requires --pin")
        digest = verify_sha256(args.verify_sha256, args.pin)
        if args.format == "json":
            print(json.dumps({"ok": True, "path": str(args.verify_sha256), "sha256": digest}, indent=2))
        else:
            print(f"{digest}  {args.verify_sha256}")
        return 0

    if args.list_profiles:
        profiles = load_profiles(args.profile_config)
        resolved = {name: resolve_profile(name, profiles, platform=args.platform) for name in sorted(profiles)}
        if args.format == "json":
            print(json.dumps(resolved, indent=2, sort_keys=True))
        else:
            for name, info in resolved.items():
                print(f"{name:20s} compiler={info['c_compiler'] or '-'} deterministic_fp={info['deterministic_fp']}")
        return 0

    if args.manifest is None:
        parser.error("--manifest is required")

    entries = parse_manifest(args.manifest)
    layout = InstallLayout(
        prefix_root=args.prefix,
        src_root=args.src_dir,
        build_root=args.build_dir,
        data_root=args.data_dir,
        archive_cache=args.archive_cache,
        prefix_overrides=_parse_overrides(args.prefix_override),
    )

    if args.plan:
        plan = plan_install(
            entries,
            layout,
            platform=args.platform,
            names=args.names,
            profile_config=args.profile_config,
            offline=args.offline,
        )
        if args.format == "json":
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            _print_text_plan(plan)
        return 0

    if args.apply:
        report = apply_install(
            entries,
            layout,
            platform=args.platform,
            names=args.names,
            profile_config=args.profile_config,
            offline=args.offline,
            execute_builds=args.execute_builds,
        )
        if args.format == "json":
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            _print_text_apply(report)
        return 0

    filtered = filter_manifest(entries, platform=args.platform)
    if args.names:
        if args.platform == "all":
            selected_set = set(args.names)
            filtered = [e for e in filtered if e.name in selected_set]
        else:
            filtered = resolve_order(filtered, names=args.names)
    elif args.platform != "all":
        filtered = resolve_order(filtered)

    report = verify_proofs(filtered, layout)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text_status(report)

    if args.verify_proofs and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ManifestError, PinVerificationError, UnsafePathError, FileNotFoundError, RuntimeError, OSError) as exc:
        print(f"deps-installer error: {exc}", file=sys.stderr)
        sys.exit(1)
