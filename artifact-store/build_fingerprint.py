#!/usr/bin/env python3
"""Deterministic compiler, ISA, floating-point contract, libm, and source-tree fingerprinter."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

FINGERPRINT_SCHEMA = "build-fingerprint/v1"


class FingerprintError(ValueError):
    """Raised when build fingerprinting fails validation or verification."""


def read_version() -> str:
    version_file = Path(__file__).resolve().with_name("VERSION")
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.1.0"


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
            raise FingerprintError(f"Input path may not traverse a symlink or junction: {candidate}")


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def split_flags(flag_strings: list[str]) -> list[str]:
    tokens: list[str] = []
    for raw in flag_strings:
        if not raw.strip():
            continue
        if os.name == "nt":
            for part in shlex.split(raw, posix=False):
                if len(part) >= 2 and part[0] == part[-1] and part[0] in ('"', "'"):
                    part = part[1:-1]
                tokens.append(part)
        else:
            tokens.extend(shlex.split(raw, posix=True))
    return tokens


def probe_cpuid() -> dict:
    arch = (platform.machine() or "unknown").lower()
    if arch in ("amd64", "x86_64"):
        arch = "x86_64"
    elif arch in ("arm64", "aarch64"):
        arch = "aarch64"

    model = platform.processor() or "unknown"
    features: set[str] = set()

    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if isinstance(val, str) and val.strip():
                    model = val.strip()
        except OSError:
            pass

        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            win_flags = (
                ("sse2", 10),
                ("sse3", 13),
                ("ssse3", 36),
                ("sse4_1", 37),
                ("sse4_2", 38),
                ("avx", 39),
                ("avx2", 40),
                ("avx512f", 41),
                ("arm_v8", 29),
            )
            for name, idx in win_flags:
                if kernel32.IsProcessorFeaturePresent(idx):
                    features.add(name)
        except (AttributeError, OSError):
            pass

    elif sys.platform.startswith("linux"):
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            text = cpuinfo.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.lower().startswith("model name") and ":" in line:
                    model = line.split(":", 1)[1].strip()
                    break
            raw_flags: set[str] = set()
            for line in text.splitlines():
                lower = line.lower()
                if (lower.startswith("flags") or lower.startswith("features")) and ":" in line:
                    raw_flags.update(line.split(":", 1)[1].split())
                    break
            known = {
                "sse2",
                "sse3",
                "ssse3",
                "sse4_1",
                "sse4_2",
                "avx",
                "avx2",
                "fma",
                "bmi1",
                "bmi2",
                "avx_vnni",
                "avx512f",
                "avx512dq",
                "avx512bw",
                "avx512vl",
                "avx512_vnni",
                "neon",
                "asimd",
                "sve",
            }
            features.update(raw_flags & known)

    elif sys.platform == "darwin":
        for sysctl_key in ("machdep.cpu.brand_string",):
            proc = subprocess.run(["sysctl", "-n", sysctl_key], capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                model = proc.stdout.strip()
        for sysctl_key in ("machdep.cpu.features", "machdep.cpu.leaf7_features"):
            proc = subprocess.run(["sysctl", "-n", sysctl_key], capture_output=True, text=True)
            if proc.returncode == 0:
                raw_flags = {tok.lower() for tok in proc.stdout.split()}
                features.update(
                    raw_flags
                    & {
                        "sse2",
                        "sse3",
                        "ssse3",
                        "sse4.1",
                        "sse4.2",
                        "avx1.0",
                        "avx2",
                        "fma",
                        "bmi1",
                        "bmi2",
                        "avx512f",
                    }
                )

    return {
        "arch": arch,
        "model": model,
        "features": sorted(features),
    }


def _compiler_family(name: str, version_line: str) -> str:
    joined = f"{name} {version_line}".lower()
    if "icx" in joined or "icpx" in joined or "intel" in joined:
        return "icx"
    if "clang" in joined:
        return "clang"
    if "gcc" in joined or "g++" in joined:
        return "gcc"
    if "microsoft" in joined or Path(name).stem.lower() == "cl":
        return "msvc"
    return "unknown"


def probe_compiler(
    compiler: str | None,
    *,
    override_version: str | None = None,
    require_compiler: bool = False,
) -> tuple[dict, str | None]:
    requested = compiler.strip() if compiler else ""
    if not requested:
        return (
            {
                "name": "unspecified",
                "family": "unknown",
                "version": override_version or "unspecified",
                "available": False,
            },
            None,
        )

    resolved = shutil.which(requested)
    if resolved is None and Path(requested).is_file():
        resolved = str(Path(requested).resolve())

    if override_version is not None:
        return (
            {
                "name": Path(requested).name,
                "family": _compiler_family(requested, override_version),
                "version": override_version.strip(),
                "available": resolved is not None,
            },
            resolved,
        )

    if resolved is None:
        if require_compiler:
            raise FingerprintError(f"Required compiler not found: {requested}")
        return (
            {
                "name": Path(requested).name,
                "family": _compiler_family(requested, ""),
                "version": "unavailable",
                "available": False,
            },
            None,
        )

    version_line = "unknown"
    try:
        proc = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=15)
        output = (proc.stdout or proc.stderr or "").strip()
        if not output:
            proc = subprocess.run([resolved], capture_output=True, text=True, timeout=15)
            output = (proc.stderr or proc.stdout or "").strip()
        if output:
            version_line = output.splitlines()[0].strip()
    except OSError as exc:
        if require_compiler:
            raise FingerprintError(f"Failed to execute compiler {resolved}: {exc}") from exc
        version_line = "unavailable"

    return (
        {
            "name": Path(requested).name,
            "family": _compiler_family(requested, version_line),
            "version": version_line,
            "available": True,
        },
        resolved,
    )


def analyze_fp_flags(flags: list[str]) -> dict:
    fp_model = "default"
    fp_contract = "default"
    fast_math: bool | None = None
    signed_zeros: bool | None = None
    nobuiltins: set[str] = set()
    arch_flags: list[str] = []

    for flag in flags:
        normalized = flag[len("/clang:") :] if flag.startswith("/clang:") else flag
        if normalized in ("-fp-model=precise", "/fp:precise"):
            fp_model = "precise"
        elif normalized in ("-fp-model=strict", "/fp:strict"):
            fp_model = "strict"
        elif normalized in ("-fp-model=fast", "/fp:fast"):
            fp_model = "fast"
        elif normalized == "-ffast-math":
            fp_model = "fast"
            fast_math = True
        elif normalized == "-fno-fast-math":
            fast_math = False
        elif normalized.startswith("-ffp-contract="):
            fp_contract = normalized.split("=", 1)[1]
        elif normalized == "-fsigned-zeros":
            signed_zeros = True
        elif normalized == "-fno-signed-zeros":
            signed_zeros = False
        elif normalized.startswith("-fno-builtin-"):
            nobuiltins.add(normalized[len("-fno-builtin-") :])
        elif normalized.startswith(("-march=", "-mtune=", "/arch:", "-mavx", "-msse")):
            arch_flags.append(flag)

    return {
        "fp_model": fp_model,
        "fp_contract": fp_contract,
        "fast_math": fast_math,
        "signed_zeros": signed_zeros,
        "nobuiltins": sorted(nobuiltins),
        "arch_flags": arch_flags,
    }


def analyze_libm(flags: list[str], libm_specs: list[str]) -> dict:
    suppressed: list[str] = []
    libraries: list[dict] = []

    for flag in flags:
        if flag.startswith("-no-intel-lib="):
            suppressed.extend(x.strip() for x in flag.split("=", 1)[1].split(",") if x.strip())
        elif flag.upper().startswith("/NODEFAULTLIB:"):
            suppressed.append(flag.split(":", 1)[1].strip())
        elif flag == "-lm":
            libraries.append({"spec": "-lm", "kind": "system_flag"})

    for spec in libm_specs:
        candidate = Path(spec)
        if candidate.is_file():
            _assert_no_links(candidate)
            sha, size = _sha256_file(candidate)
            libraries.append(
                {
                    "spec": candidate.name,
                    "kind": "archive",
                    "bytes": size,
                    "sha256": sha,
                }
            )
        else:
            libraries.append({"spec": spec, "kind": "declared"})

    return {
        "suppressed_vendor_libs": sorted(set(suppressed)),
        "libraries": sorted(libraries, key=lambda item: (item["kind"], item["spec"])),
    }


def fingerprint_input(spec: str, *, include_hidden: bool = False) -> dict:
    if "=" in spec and not Path(spec).exists():
        label, raw_path = spec.split("=", 1)
        label = label.strip()
        target = Path(raw_path.strip())
    else:
        target = Path(spec)
        label = target.name or str(target)

    _assert_no_links(target)
    if not target.exists():
        raise FileNotFoundError(f"Fingerprint input does not exist: {target}")

    if target.is_file():
        sha, size = _sha256_file(target)
        return {
            "name": label,
            "kind": "file",
            "file_count": 1,
            "total_bytes": size,
            "sha256": sha,
        }

    if not target.is_dir():
        raise FingerprintError(f"Unsupported input type: {target}")

    records: list[tuple[str, int, str]] = []
    for current_dir, dirnames, filenames in os.walk(target, followlinks=False):
        current_path = Path(current_dir)
        if _is_link_or_junction(current_path):
            raise FingerprintError(f"Input directory may not contain links: {current_path}")
        if not include_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for dname in dirnames:
            dpath = current_path / dname
            if _is_link_or_junction(dpath):
                raise FingerprintError(f"Input directory may not contain links: {dpath}")
        for fname in filenames:
            if not include_hidden and fname.startswith("."):
                continue
            fpath = current_path / fname
            if _is_link_or_junction(fpath):
                raise FingerprintError(f"Input directory may not contain links: {fpath}")
            sha, size = _sha256_file(fpath)
            rel = fpath.relative_to(target).as_posix()
            records.append((rel, size, sha))

    records.sort(key=lambda item: item[0].encode("utf-8"))
    tree_hasher = hashlib.sha256()
    total_bytes = 0
    for rel, size, sha in records:
        total_bytes += size
        tree_hasher.update(f"{rel}\t{size}\t{sha}\n".encode("utf-8"))

    return {
        "name": label,
        "kind": "directory",
        "file_count": len(records),
        "total_bytes": total_bytes,
        "sha256": tree_hasher.hexdigest(),
    }


def compute_fingerprint(
    *,
    compiler: str | None = None,
    compiler_version: str | None = None,
    flags: list[str] | None = None,
    inputs: list[str] | None = None,
    libm: list[str] | None = None,
    isa: str | None = None,
    include_host_cpuid: bool = True,
    include_hidden: bool = False,
    require_compiler: bool = False,
) -> dict:
    parsed_flags = split_flags(flags or [])
    compiler_info, compiler_path = probe_compiler(
        compiler,
        override_version=compiler_version,
        require_compiler=require_compiler,
    )
    host_cpuid = probe_cpuid()
    fp_contract = analyze_fp_flags(parsed_flags)
    libm_info = analyze_libm(parsed_flags, libm or [])

    input_entries = [
        fingerprint_input(spec, include_hidden=include_hidden) for spec in (inputs or [])
    ]
    input_entries.sort(key=lambda item: (item["name"], item["kind"], item["sha256"]))

    isa_section: dict = {
        "arch": host_cpuid["arch"],
        "target_isa": isa or (fp_contract["arch_flags"][0] if fp_contract["arch_flags"] else "default"),
        "requested_arch_flags": fp_contract["arch_flags"],
    }
    if include_host_cpuid:
        isa_section["cpuid_features"] = host_cpuid["features"]

    subject = {
        "compiler": compiler_info,
        "flags": parsed_flags,
        "fp_contract": {
            "fp_model": fp_contract["fp_model"],
            "fp_contract": fp_contract["fp_contract"],
            "fast_math": fp_contract["fast_math"],
            "signed_zeros": fp_contract["signed_zeros"],
            "nobuiltins": fp_contract["nobuiltins"],
        },
        "isa": isa_section,
        "libm": libm_info,
        "inputs": input_entries,
    }

    canonical_payload = json.dumps(subject, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fingerprint_sha256 = hashlib.sha256(canonical_payload).hexdigest()

    return {
        "schema": FINGERPRINT_SCHEMA,
        "fingerprint_sha256": fingerprint_sha256,
        "subject": subject,
        "environment": {
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu_model": host_cpuid["model"],
            "cpuid_features": host_cpuid["features"],
            "compiler_path": compiler_path,
            "observed_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def _atomic_write_text(path: Path, text: str) -> None:
    _assert_no_links(path.parent)
    if _is_link_or_junction(path):
        raise FingerprintError(f"Output path may not be a symlink or junction: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".fingerprint-")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument("--compiler", help="Compiler command or path to inspect")
    parser.add_argument("--compiler-version", help="Explicit compiler version string override")
    parser.add_argument("--flags", action="append", default=[], help="Compiler/linker flags string (repeatable)")
    parser.add_argument("--input", action="append", default=[], help="Input file or directory (optionally LABEL=PATH)")
    parser.add_argument("--libm", action="append", default=[], help="Math library name or archive path (repeatable)")
    parser.add_argument("--isa", help="Declared target ISA identifier (e.g. x86-64-v2)")
    parser.add_argument("--no-host-cpuid", action="store_true", help="Exclude host CPUID features from canonical digest")
    parser.add_argument("--include-hidden", action="store_true", help="Include dot-files and dot-directories in inputs")
    parser.add_argument("--require-compiler", action="store_true", help="Fail if --compiler cannot be resolved/executed")
    parser.add_argument("--format", choices=("json", "sha256"), default="json", help="Output format")
    parser.add_argument("--output", type=Path, help="Write formatted receipt atomically to PATH")
    parser.add_argument("--verify", type=Path, help="Compare computed fingerprint against existing JSON receipt")
    args = parser.parse_args(argv)

    if args.version:
        print(f"build-fingerprint {read_version()}")
        return 0

    receipt = compute_fingerprint(
        compiler=args.compiler,
        compiler_version=args.compiler_version,
        flags=args.flags,
        inputs=args.input,
        libm=args.libm,
        isa=args.isa,
        include_host_cpuid=not args.no_host_cpuid,
        include_hidden=args.include_hidden,
        require_compiler=args.require_compiler,
    )

    if args.verify is not None:
        expected_doc = json.loads(args.verify.read_text(encoding="utf-8"))
        expected_sha = expected_doc.get("fingerprint_sha256", "")
        actual_sha = receipt["fingerprint_sha256"]
        if expected_sha != actual_sha:
            print(
                f"Fingerprint mismatch: expected {expected_sha}, got {actual_sha}",
                file=sys.stderr,
            )
            return 1

    rendered = (
        receipt["fingerprint_sha256"] + "\n"
        if args.format == "sha256"
        else json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    )
    if args.output is not None:
        _atomic_write_text(args.output, rendered)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FingerprintError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"build-fingerprint error: {exc}", file=sys.stderr)
        sys.exit(1)
