#!/usr/bin/env python3
"""Content-addressed blob store and immutable release materializer."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

RECEIPT_FILENAME = ".artifact-receipt.json"
RELEASE_SCHEMA = "artifact-store.release/v1"
REF_SCHEMA = "artifact-store.ref/v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PREFIX_RE = re.compile(r"^[0-9a-f]{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


class SymlinkTraversalError(ValueError):
    """Raised when a symlink or NTFS junction is encountered along a protected path."""


class StoreLockError(OSError):
    """Raised when the advisory store lock is held by another process."""


class VerificationError(ValueError):
    """Raised when artifact store verification detects missing or corrupted state."""


def read_version() -> str:
    version_file = Path(__file__).resolve().with_name("VERSION")
    if version_file.is_file():
        return version_file.read_text(encoding="utf-8").strip()
    return "0.1.0"


def is_link_or_junction(path: Path) -> bool:
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


def assert_no_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if is_link_or_junction(candidate):
            raise SymlinkTraversalError(f"Path may not redirect through a symlink or junction: {candidate}")


def _validate_id(value: str, label: str = "release id") -> str:
    if not value or value in (".", "..") or not _ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def digest_file(path: Path | str) -> str:
    target = Path(path)
    if is_link_or_junction(target):
        raise SymlinkTraversalError(f"Refusing to digest symlink or junction: {target}")
    if not target.is_file():
        raise FileNotFoundError(f"Missing file for digest: {target}")
    value = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def files_inventory(root: Path | str) -> tuple[dict[str, str], int]:
    base = Path(root)
    assert_no_links(base)
    if not base.is_dir():
        raise ValueError(f"Artifact source must be a directory: {base}")

    entries: list[tuple[str, str, int]] = []
    for current_dir, dirnames, filenames in os.walk(base, followlinks=False):
        current_path = Path(current_dir)
        if is_link_or_junction(current_path):
            raise SymlinkTraversalError(f"Artifact source may not traverse a link: {current_path}")
        for dname in dirnames:
            dpath = current_path / dname
            if is_link_or_junction(dpath):
                raise SymlinkTraversalError(f"Artifact source may not redirect through a link: {dpath}")
        for fname in filenames:
            fpath = current_path / fname
            if is_link_or_junction(fpath):
                raise SymlinkTraversalError(f"Artifact source may not contain a link: {fpath}")
            if fname == RECEIPT_FILENAME and current_path == base:
                raise ValueError(f"Artifact source may not contain reserved receipt file: {fpath}")
            rel = fpath.relative_to(base).as_posix()
            entries.append((rel, digest_file(fpath), fpath.stat().st_size))

    entries.sort(key=lambda item: item[0].encode("utf-8"))
    inventory = {rel: sha for rel, sha, _ in entries}
    total_bytes = sum(size for _, _, size in entries)
    return inventory, total_bytes


def inherit_access(path: Path | str) -> None:
    """Reset explicit ACLs on Windows so staged objects inherit parent permissions."""
    if os.name != "nt":
        return
    target = Path(path)
    if is_link_or_junction(target):
        raise SymlinkTraversalError(f"Staging access must not follow a link: {target}")
    result = subprocess.run(["icacls.exe", str(target), "/reset", "/Q"], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PermissionError(f"Cannot inherit artifact-store access for {target}: {detail}")


def staging_directory(parent: Path | str, prefix: str = ".stage-") -> Path:
    parent_path = Path(parent)
    assert_no_links(parent_path)
    parent_path.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent_path))
    try:
        inherit_access(directory)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory


def atomic_write_json(path: Path | str, value: dict) -> Path:
    target = Path(path)
    assert_no_links(target.parent)
    if is_link_or_junction(target):
        raise SymlinkTraversalError(f"Refusing to write receipt over link: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".receipt-")
    tmp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        inherit_access(tmp_path)
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
    return target


@contextmanager
def store_lock(store: Path | str):
    store_path = Path(store)
    assert_no_links(store_path)
    store_path.mkdir(parents=True, exist_ok=True)
    lock_file = store_path / ".store.lock"
    if is_link_or_junction(lock_file):
        raise SymlinkTraversalError(f"Store lock must not be a link: {lock_file}")
    with lock_file.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise StoreLockError(f"Artifact store is locked by another process: {store_path}") from error
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise StoreLockError(f"Artifact store is locked by another process: {store_path}") from error
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def blob_path(store: Path | str, sha256_hex: str) -> Path:
    digest = sha256_hex.strip().lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"Invalid SHA-256 digest: {sha256_hex!r}")
    return Path(store) / "blobs" / "sha256" / digest[:2] / digest


def link_file(source: Path | str, target: Path | str) -> str:
    src = Path(source)
    dst = Path(target)
    assert_no_links(src)
    assert_no_links(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or is_link_or_junction(dst):
        if is_link_or_junction(dst):
            raise SymlinkTraversalError(f"Target path may not be a symlink or junction: {dst}")
        src_stat = os.stat(src)
        dst_stat = os.stat(dst)
        if src_stat.st_ino and src_stat.st_dev == dst_stat.st_dev and src_stat.st_ino == dst_stat.st_ino:
            return "hardlink"
        dst.unlink()
    try:
        os.link(src, dst)
        return "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        inherit_access(dst)
        return "copy"


def store_blob(source: Path | str, store: Path | str, expected: str | None = None) -> tuple[Path, str]:
    src = Path(source)
    store_path = Path(store)
    assert_no_links(src)
    assert_no_links(store_path)
    expected_digest = (expected or digest_file(src)).strip().lower()
    blob = blob_path(store_path, expected_digest)
    blob.parent.mkdir(parents=True, exist_ok=True)
    if is_link_or_junction(blob):
        raise SymlinkTraversalError(f"Blob path may not be a link: {blob}")
    if not blob.is_file() or digest_file(blob) != expected_digest:
        descriptor, temporary = tempfile.mkstemp(dir=blob.parent, prefix=".blob-")
        os.close(descriptor)
        tmp_path = Path(temporary)
        try:
            shutil.copy2(src, tmp_path)
            if digest_file(tmp_path) != expected_digest:
                raise ValueError(f"Source file changed while storing blob: {src}")
            inherit_access(tmp_path)
            os.replace(tmp_path, blob)
        finally:
            tmp_path.unlink(missing_ok=True)
    return blob, expected_digest


def materialize_file(source: Path | str, target: Path | str, store: Path | str, expected: str | None = None) -> tuple[str, str]:
    blob, digest = store_blob(source, store, expected)
    mode = link_file(blob, target)
    return digest, mode


def compute_release_id(inventory: dict[str, str], metadata: dict | None = None) -> str:
    payload = {
        "schema": RELEASE_SCHEMA,
        "files": inventory,
        "metadata": metadata or {},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_release_tree(release_dir: Path, store: Path | None = None, *, check_blobs: bool = True) -> dict:
    assert_no_links(release_dir)
    if not release_dir.is_dir():
        raise VerificationError(f"Release directory is absent: {release_dir}")
    receipt_path = release_dir / RECEIPT_FILENAME
    if is_link_or_junction(receipt_path) or not receipt_path.is_file():
        raise VerificationError(f"Release lacks atomic receipt {RECEIPT_FILENAME}: {release_dir}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or receipt.get("schema") != RELEASE_SCHEMA:
        raise VerificationError(f"Invalid release receipt schema at {receipt_path}")
    if receipt.get("id") != release_dir.name:
        raise VerificationError(f"Receipt id {receipt.get('id')!r} does not match directory {release_dir.name!r}")
    files = receipt.get("files")
    if not isinstance(files, dict) or not files:
        raise VerificationError(f"Receipt contains empty or invalid file inventory: {receipt_path}")

    actual_files: set[str] = set()
    for current_dir, dirnames, filenames in os.walk(release_dir, followlinks=False):
        current_path = Path(current_dir)
        if is_link_or_junction(current_path):
            raise SymlinkTraversalError(f"Release contains symlink/junction directory: {current_path}")
        for dname in dirnames:
            dpath = current_path / dname
            if is_link_or_junction(dpath):
                raise SymlinkTraversalError(f"Release contains symlink/junction directory: {dpath}")
        for fname in filenames:
            fpath = current_path / fname
            if is_link_or_junction(fpath):
                raise SymlinkTraversalError(f"Release contains symlink/junction file: {fpath}")
            if current_path == release_dir and fname == RECEIPT_FILENAME:
                continue
            actual_files.add(fpath.relative_to(release_dir).as_posix())

    expected_files = set(files.keys())
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        raise VerificationError(
            f"Release {release_dir.name} file mismatch (missing={missing}, extra={extra})"
        )

    for rel, expected_sha in files.items():
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts or not _SHA256_RE.fullmatch(str(expected_sha)):
            raise VerificationError(f"Invalid receipt file entry {rel!r}: {expected_sha!r}")
        file_path = release_dir / rel_path
        actual_sha = digest_file(file_path)
        if actual_sha != expected_sha:
            raise VerificationError(
                f"Release {release_dir.name} file {rel} corrupted: expected {expected_sha}, got {actual_sha}"
            )
        if check_blobs and store is not None:
            blob = blob_path(store, expected_sha)
            if not blob.is_file() or digest_file(blob) != expected_sha:
                raise VerificationError(f"Blob missing or corrupted for {release_dir.name}/{rel}: {blob}")

    return receipt


def set_ref(store: Path | str, ref_name: str, release_id: str) -> Path:
    store_path = Path(store)
    name = _validate_id(ref_name, "ref name")
    rid = _validate_id(release_id, "release id")
    release_dir = store_path / "releases" / rid
    _validate_release_tree(release_dir, store_path, check_blobs=False)
    ref_path = store_path / "refs" / f"{name}.json"
    payload = {
        "schema": REF_SCHEMA,
        "name": name,
        "release": rid,
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return atomic_write_json(ref_path, payload)


def materialize_release(
    store: Path | str,
    source: Path | str,
    *,
    release_id: str | None = None,
    metadata: dict | None = None,
    refs: list[str] | None = None,
    target: Path | str | None = None,
) -> dict:
    store_path = Path(store)
    source_path = Path(source)

    with store_lock(store_path):
        inventory, total_bytes = files_inventory(source_path)
        if not inventory:
            raise ValueError(f"Cannot materialize empty source directory: {source_path}")

        rid = _validate_id(release_id, "release id") if release_id is not None else compute_release_id(inventory, metadata)
        releases_dir = store_path / "releases"
        assert_no_links(releases_dir)
        releases_dir.mkdir(parents=True, exist_ok=True)
        release_dir = releases_dir / rid

        reused = False
        link_modes = {"hardlink": 0, "copy": 0}
        if release_dir.exists() or is_link_or_junction(release_dir):
            if is_link_or_junction(release_dir):
                raise SymlinkTraversalError(f"Release path may not be a symlink or junction: {release_dir}")
            existing = _validate_release_tree(release_dir, store_path, check_blobs=True)
            if existing.get("files") != inventory:
                raise ValueError(
                    f"Immutable release {rid!r} already exists with different file contents"
                )
            reused = True
        else:
            stage = staging_directory(releases_dir, prefix=f".stage-{rid[:12]}-")
            try:
                for rel, sha in inventory.items():
                    _, mode = materialize_file(source_path / rel, stage / rel, store_path, sha)
                    link_modes[mode] += 1
                receipt = {
                    "schema": RELEASE_SCHEMA,
                    "id": rid,
                    "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "files": inventory,
                    "file_count": len(inventory),
                    "total_bytes": total_bytes,
                    "link_modes": link_modes,
                    "metadata": metadata or {},
                }
                atomic_write_json(stage / RECEIPT_FILENAME, receipt)
                inherit_access(stage)
                os.replace(stage, release_dir)
            finally:
                if stage.exists():
                    shutil.rmtree(stage, ignore_errors=True)

        updated_refs: list[str] = []
        for ref_name in refs or ():
            set_ref(store_path, ref_name, rid)
            updated_refs.append(ref_name)

        target_dir_str = None
        if target is not None:
            target_path = Path(target)
            assert_no_links(target_path)
            target_path.mkdir(parents=True, exist_ok=True)
            for rel, sha in inventory.items():
                materialize_file(release_dir / rel, target_path / rel, store_path, sha)
            atomic_write_json(
                target_path / RECEIPT_FILENAME,
                json.loads((release_dir / RECEIPT_FILENAME).read_text(encoding="utf-8")),
            )
            target_dir_str = str(target_path)

        return {
            "id": rid,
            "store": str(store_path),
            "release_dir": str(release_dir),
            "receipt": str(release_dir / RECEIPT_FILENAME),
            "file_count": len(inventory),
            "total_bytes": total_bytes,
            "reused": reused,
            "link_modes": link_modes,
            "refs": updated_refs,
            "target": target_dir_str,
        }


def store_directory(store: Path | str, source: Path | str) -> dict:
    store_path = Path(store)
    source_path = Path(source)
    with store_lock(store_path):
        assert_no_links(source_path)
        if source_path.is_file():
            blob, digest = store_blob(source_path, store_path)
            return {
                "store": str(store_path),
                "files": {source_path.name: digest},
                "blobs": {digest: str(blob)},
            }
        inventory, total_bytes = files_inventory(source_path)
        if not inventory:
            raise ValueError(f"Cannot store empty directory: {source_path}")
        blobs: dict[str, str] = {}
        for rel, expected in inventory.items():
            blob, _ = store_blob(source_path / rel, store_path, expected)
            blobs[expected] = str(blob)
        return {
            "store": str(store_path),
            "file_count": len(inventory),
            "total_bytes": total_bytes,
            "files": inventory,
            "blobs": blobs,
        }


def verify_store(
    store: Path | str,
    *,
    release_id: str | None = None,
    target: Path | str | None = None,
    check_blobs: bool = True,
) -> dict:
    store_path = Path(store)
    errors: list[str] = []
    verified_releases: list[str] = []
    verified_blobs = 0

    try:
        assert_no_links(store_path)
    except SymlinkTraversalError as exc:
        return {"ok": False, "verified_releases": [], "verified_blobs": 0, "errors": [str(exc)]}

    if target is not None:
        try:
            _validate_release_tree(Path(target), store_path if check_blobs else None, check_blobs=check_blobs)
            verified_releases.append(Path(target).name)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
        return {
            "ok": len(errors) == 0,
            "verified_releases": verified_releases,
            "verified_blobs": verified_blobs,
            "errors": errors,
        }

    releases_dir = store_path / "releases"
    if release_id is not None:
        rid = _validate_id(release_id, "release id")
        try:
            _validate_release_tree(releases_dir / rid, store_path, check_blobs=check_blobs)
            verified_releases.append(rid)
        except (ValueError, OSError) as exc:
            errors.append(str(exc))
        return {
            "ok": len(errors) == 0,
            "verified_releases": verified_releases,
            "verified_blobs": verified_blobs,
            "errors": errors,
        }

    if releases_dir.exists():
        if is_link_or_junction(releases_dir):
            errors.append(f"Releases root may not be a symlink or junction: {releases_dir}")
        else:
            for item in sorted(releases_dir.iterdir()):
                if item.name.startswith(".stage-"):
                    continue
                if is_link_or_junction(item):
                    errors.append(f"Release entry may not be a symlink or junction: {item}")
                    continue
                if not item.is_dir():
                    errors.append(f"Unexpected non-directory entry in releases: {item}")
                    continue
                try:
                    _validate_release_tree(item, store_path, check_blobs=check_blobs)
                    verified_releases.append(item.name)
                except (ValueError, OSError) as exc:
                    errors.append(str(exc))

    sha_root = store_path / "blobs" / "sha256"
    if sha_root.exists():
        if is_link_or_junction(sha_root) or is_link_or_junction(sha_root.parent):
            errors.append(f"Blobs directory may not be a symlink or junction: {sha_root}")
        else:
            for bucket in sorted(sha_root.iterdir()):
                if is_link_or_junction(bucket):
                    errors.append(f"Blob bucket may not be a link: {bucket}")
                    continue
                if not bucket.is_dir() or not _PREFIX_RE.fullmatch(bucket.name):
                    errors.append(f"Unexpected entry in blobs/sha256: {bucket}")
                    continue
                for blob in sorted(bucket.iterdir()):
                    if is_link_or_junction(blob):
                        errors.append(f"Blob entry may not be a link: {blob}")
                        continue
                    if blob.name.startswith(".blob-"):
                        continue
                    if not blob.is_file() or not _SHA256_RE.fullmatch(blob.name) or not blob.name.startswith(bucket.name):
                        errors.append(f"Malformed blob filename or type: {blob}")
                        continue
                    actual = digest_file(blob)
                    if actual != blob.name:
                        errors.append(f"Blob hash mismatch for {blob}: actual {actual}")
                    else:
                        verified_blobs += 1

    refs_dir = store_path / "refs"
    if refs_dir.exists():
        if is_link_or_junction(refs_dir):
            errors.append(f"Refs directory may not be a link: {refs_dir}")
        else:
            for ref_file in sorted(refs_dir.iterdir()):
                if is_link_or_junction(ref_file):
                    errors.append(f"Ref file may not be a link: {ref_file}")
                    continue
                if ref_file.suffix != ".json":
                    continue
                try:
                    data = json.loads(ref_file.read_text(encoding="utf-8"))
                    target_id = data.get("release", "")
                    if not (releases_dir / target_id).is_dir():
                        errors.append(f"Ref {ref_file.name} points to missing release {target_id!r}")
                except (ValueError, OSError) as exc:
                    errors.append(f"Malformed ref {ref_file}: {exc}")

    return {
        "ok": len(errors) == 0,
        "verified_releases": verified_releases,
        "verified_blobs": verified_blobs,
        "errors": errors,
    }


def collect_referenced_releases(
    store: Path | str,
    *,
    keep: list[str] | None = None,
    keep_latest: int = 0,
) -> dict[str, list[str]]:
    store_path = Path(store)
    releases_dir = store_path / "releases"
    reasons: dict[str, list[str]] = {}

    def mark(rid: str, reason: str) -> None:
        if rid:
            reasons.setdefault(rid, [])
            if reason not in reasons[rid]:
                reasons[rid].append(reason)

    for rid in keep or ():
        mark(_validate_id(rid, "keep id"), "explicit_keep")

    current = store_path / "current"
    if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
        resolved = current.resolve(strict=False)
        if resolved.parent.resolve() == releases_dir.resolve():
            mark(resolved.name, "current_link")
    elif current.is_file():
        rid = current.read_text(encoding="utf-8").strip()
        if rid:
            mark(rid, "current_file")

    refs_dir = store_path / "refs"
    if refs_dir.is_dir() and not is_link_or_junction(refs_dir):
        for entry in sorted(refs_dir.iterdir()):
            if is_link_or_junction(entry) or not entry.is_file():
                continue
            raw = entry.read_text(encoding="utf-8").strip()
            if entry.suffix == ".json" and raw.startswith("{"):
                data = json.loads(raw)
                rid = str(data.get("release", "")).strip()
                if rid:
                    mark(rid, f"ref:{entry.stem}")
            elif raw:
                mark(raw, f"ref:{entry.name}")

    if keep_latest > 0 and releases_dir.is_dir():
        candidates = [
            p
            for p in releases_dir.iterdir()
            if p.is_dir() and not is_link_or_junction(p) and not p.name.startswith(".stage-")
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in candidates[:keep_latest]:
            mark(p.name, "keep_latest")

    return reasons


def _validate_tree_for_delete(target: Path, expected_parent: Path) -> None:
    if target.parent.resolve() != expected_parent.resolve():
        raise SymlinkTraversalError(f"Retirement escaped parent directory: {target}")
    assert_no_links(target)
    for current_dir, dirnames, filenames in os.walk(target, followlinks=False):
        current_path = Path(current_dir)
        for name in (*dirnames, *filenames):
            item = current_path / name
            if is_link_or_junction(item):
                raise SymlinkTraversalError(f"Refusing to delete tree containing link: {item}")


def gc_store(
    store: Path | str,
    *,
    keep: list[str] | None = None,
    keep_latest: int = 0,
    dry_run: bool = False,
) -> dict:
    store_path = Path(store)
    with store_lock(store_path):
        assert_no_links(store_path)
        releases_dir = store_path / "releases"
        sha_root = store_path / "blobs" / "sha256"

        protected = collect_referenced_releases(store_path, keep=keep, keep_latest=keep_latest)
        kept_releases: list[dict] = []
        removed_releases: list[str] = []
        removed_stages: list[str] = []
        retained_hashes: set[str] = set()
        retired_hash_links: dict[str, int] = {}
        freed_bytes = 0

        if releases_dir.exists():
            if is_link_or_junction(releases_dir):
                raise SymlinkTraversalError(f"Releases root may not be a symlink or junction: {releases_dir}")
            for entry in sorted(releases_dir.iterdir()):
                if is_link_or_junction(entry):
                    raise SymlinkTraversalError(f"Release entry may not be a symlink or junction: {entry}")
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".stage-"):
                    _validate_tree_for_delete(entry, releases_dir)
                    if not dry_run:
                        shutil.rmtree(entry)
                    removed_stages.append(entry.name)
                    continue

                receipt_path = entry / RECEIPT_FILENAME
                receipt_files: dict[str, str] = {}
                if receipt_path.is_file() and not is_link_or_junction(receipt_path):
                    try:
                        data = json.loads(receipt_path.read_text(encoding="utf-8"))
                        if isinstance(data.get("files"), dict):
                            receipt_files = data["files"]
                    except (ValueError, OSError):
                        receipt_files = {}

                if entry.name in protected:
                    kept_releases.append({"id": entry.name, "reasons": protected[entry.name]})
                    retained_hashes.update(receipt_files.values())
                else:
                    _validate_tree_for_delete(entry, releases_dir)
                    for sha in receipt_files.values():
                        retired_hash_links[sha] = retired_hash_links.get(sha, 0) + 1
                    if not dry_run:
                        shutil.rmtree(entry)
                    removed_releases.append(entry.name)

        removed_blobs: list[str] = []
        kept_blobs = 0
        if sha_root.exists():
            if is_link_or_junction(sha_root) or is_link_or_junction(sha_root.parent):
                raise SymlinkTraversalError(f"Blobs root may not be a symlink or junction: {sha_root}")
            for bucket in sorted(sha_root.iterdir()):
                if is_link_or_junction(bucket):
                    raise SymlinkTraversalError(f"Blob bucket may not be a symlink or junction: {bucket}")
                if not bucket.is_dir() or not _PREFIX_RE.fullmatch(bucket.name):
                    continue
                for blob in sorted(bucket.iterdir()):
                    if is_link_or_junction(blob):
                        raise SymlinkTraversalError(f"Blob may not be a symlink or junction: {blob}")
                    if not blob.is_file() or not _SHA256_RE.fullmatch(blob.name) or not blob.name.startswith(bucket.name):
                        continue
                    digest = blob.name
                    st = blob.stat()
                    projected_nlink = st.st_nlink - (retired_hash_links.get(digest, 0) if dry_run else 0)
                    if digest not in retained_hashes and projected_nlink <= 1:
                        freed_bytes += st.st_size
                        if not dry_run:
                            blob.unlink()
                        removed_blobs.append(digest)
                    else:
                        kept_blobs += 1
                if not dry_run and not any(bucket.iterdir()):
                    bucket.rmdir()

        return {
            "schema": "artifact-store.gc/v1",
            "store": str(store_path),
            "dry_run": dry_run,
            "kept_releases": kept_releases,
            "removed_releases": removed_releases,
            "removed_stages": removed_stages,
            "removed_blobs": removed_blobs,
            "kept_blobs": kept_blobs,
            "freed_bytes": freed_bytes,
        }


def list_store(store: Path | str) -> dict:
    store_path = Path(store)
    assert_no_links(store_path)
    releases_dir = store_path / "releases"
    refs_dir = store_path / "refs"
    sha_root = store_path / "blobs" / "sha256"

    releases: list[dict] = []
    if releases_dir.is_dir():
        for entry in sorted(releases_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".stage-") or is_link_or_junction(entry):
                continue
            receipt_path = entry / RECEIPT_FILENAME
            info = {"id": entry.name, "receipt": receipt_path.is_file()}
            if receipt_path.is_file():
                try:
                    data = json.loads(receipt_path.read_text(encoding="utf-8"))
                    info["file_count"] = data.get("file_count")
                    info["total_bytes"] = data.get("total_bytes")
                    info["created_utc"] = data.get("created_utc")
                except (ValueError, OSError):
                    pass
            releases.append(info)

    refs: dict[str, str] = {}
    if refs_dir.is_dir():
        for entry in sorted(refs_dir.iterdir()):
            if entry.is_file() and entry.suffix == ".json" and not is_link_or_junction(entry):
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    refs[entry.stem] = str(data.get("release", ""))
                except (ValueError, OSError):
                    pass

    blob_count = 0
    blob_bytes = 0
    if sha_root.is_dir():
        for bucket in sha_root.iterdir():
            if bucket.is_dir() and _PREFIX_RE.fullmatch(bucket.name):
                for blob in bucket.iterdir():
                    if blob.is_file() and _SHA256_RE.fullmatch(blob.name):
                        blob_count += 1
                        blob_bytes += blob.stat().st_size

    return {
        "store": str(store_path),
        "releases": releases,
        "refs": refs,
        "blob_count": blob_count,
        "blob_bytes": blob_bytes,
    }


def _parse_metadata(items: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Metadata entry must be KEY=VALUE, got {item!r}")
        key, val = item.split("=", 1)
        metadata[key.strip()] = val
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    sub = parser.add_subparsers(dest="command")

    p_store = sub.add_parser("store", help="Store file or directory contents into blobs/sha256")
    p_store.add_argument("--store", type=Path, required=True)
    p_store.add_argument("--source", type=Path, required=True)

    p_mat = sub.add_parser("materialize", help="Materialize an immutable release into releases/<id>")
    p_mat.add_argument("--store", type=Path, required=True)
    p_mat.add_argument("--source", type=Path, required=True)
    p_mat.add_argument("--id", dest="release_id", help="Explicit release identifier (default: content-addressed SHA-256)")
    p_mat.add_argument("--ref", action="append", default=[], help="Update named reference pointer in refs/<name>.json")
    p_mat.add_argument("--target", type=Path, help="Optional additional target directory to materialize into")
    p_mat.add_argument("--meta", action="append", default=[], help="Release metadata KEY=VALUE")

    p_ver = sub.add_parser("verify", help="Verify store blobs, release receipts, and payload integrity")
    p_ver.add_argument("--store", type=Path, required=True)
    p_ver.add_argument("--release", help="Specific release ID to verify")
    p_ver.add_argument("--target", type=Path, help="Materialized target directory to verify")
    p_ver.add_argument("--no-blobs", action="store_true", help="Skip blob store checks when verifying a release/target")

    p_gc = sub.add_parser("gc", help="Prune unreferenced releases and unreferenced blobs")
    p_gc.add_argument("--store", type=Path, required=True)
    p_gc.add_argument("--keep", action="append", default=[], help="Release ID to retain (repeatable)")
    p_gc.add_argument("--keep-latest", type=int, default=0, help="Keep N most recently modified releases")
    p_gc.add_argument("--dry-run", action="store_true", help="Report releases and blobs that would be pruned")

    p_ref = sub.add_parser("ref", help="Manage named release references")
    p_ref.add_argument("--store", type=Path, required=True)
    p_ref.add_argument("action", choices=("set", "remove", "list"))
    p_ref.add_argument("name", nargs="?")
    p_ref.add_argument("release_id", nargs="?")

    p_list = sub.add_parser("list", help="List releases, refs, and blob statistics")
    p_list.add_argument("--store", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.version:
        print(f"artifact-store {read_version()}")
        return 0
    if not args.command:
        parser.print_help()
        return 2

    if args.command == "store":
        result = store_directory(args.store, args.source)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "materialize":
        result = materialize_release(
            args.store,
            args.source,
            release_id=args.release_id,
            metadata=_parse_metadata(args.meta),
            refs=args.ref,
            target=args.target,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "verify":
        result = verify_store(
            args.store,
            release_id=args.release,
            target=args.target,
            check_blobs=not args.no_blobs,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    if args.command == "gc":
        result = gc_store(
            args.store,
            keep=args.keep,
            keep_latest=args.keep_latest,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "ref":
        if args.action == "list":
            print(json.dumps(list_store(args.store)["refs"], indent=2, sort_keys=True))
            return 0
        if not args.name:
            parser.error("ref name is required")
        if args.action == "set":
            if not args.release_id:
                parser.error("release_id is required for ref set")
            with store_lock(args.store):
                ref_path = set_ref(args.store, args.name, args.release_id)
            print(json.dumps({"name": args.name, "release": args.release_id, "ref_path": str(ref_path)}, indent=2))
            return 0
        if args.action == "remove":
            with store_lock(args.store):
                ref_path = Path(args.store) / "refs" / f"{_validate_id(args.name, 'ref name')}.json"
                removed = ref_path.exists()
                ref_path.unlink(missing_ok=True)
            print(json.dumps({"name": args.name, "removed": removed}, indent=2))
            return 0

    if args.command == "list":
        print(json.dumps(list_store(args.store), indent=2, sort_keys=True))
        return 0

    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"artifact-store error: {exc}", file=sys.stderr)
        sys.exit(1)
