#!/usr/bin/env python3
"""Unit tests for artifact_store.py and build_fingerprint.py."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

import artifact_store as store_mod  # noqa: E402
import build_fingerprint as fp_mod  # noqa: E402


class ArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="artifact-store-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"

    def _make_tree(self, name: str, files: dict[str, bytes]) -> Path:
        root = self.tmp / name
        for rel, data in files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        return root

    def test_store_materialize_dedup_and_verify(self) -> None:
        shared_bytes = b"shared-library-binary-content"
        shared_hash = hashlib.sha256(shared_bytes).hexdigest()

        src1 = self._make_tree("src1", {"bin/app.txt": b"app-v1", "lib/shared.bin": shared_bytes})
        src2 = self._make_tree("src2", {"bin/app.txt": b"app-v2", "lib/shared.bin": shared_bytes})

        rel1 = store_mod.materialize_release(self.store, src1, refs=["current"], metadata={"version": "1.0"})
        rel2 = store_mod.materialize_release(self.store, src2, metadata={"version": "2.0"})

        blob = self.store / "blobs" / "sha256" / shared_hash[:2] / shared_hash
        self.assertTrue(blob.is_file())
        self.assertEqual(store_mod.digest_file(blob), shared_hash)

        file_r1 = Path(rel1["release_dir"]) / "lib" / "shared.bin"
        file_r2 = Path(rel2["release_dir"]) / "lib" / "shared.bin"
        self.assertEqual(blob.stat().st_ino, file_r1.stat().st_ino)
        self.assertEqual(blob.stat().st_ino, file_r2.stat().st_ino)
        self.assertGreaterEqual(blob.stat().st_nlink, 3)

        receipt = json.loads(Path(rel1["receipt"]).read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema"], "artifact-store.release/v1")
        self.assertEqual(receipt["id"], rel1["id"])
        self.assertEqual(receipt["metadata"], {"version": "1.0"})

        # Idempotent re-materialization with identical inputs reuses release.
        rel1_again = store_mod.materialize_release(self.store, src1, metadata={"version": "1.0"})
        self.assertTrue(rel1_again["reused"])
        self.assertEqual(rel1_again["id"], rel1["id"])

        # Explicit release ID conflict raises immutability error.
        with self.assertRaises(ValueError):
            store_mod.materialize_release(self.store, src2, release_id=rel1["id"])

        ver = store_mod.verify_store(self.store)
        self.assertTrue(ver["ok"], msg=ver["errors"])
        self.assertEqual(ver["verified_blobs"], 3)
        self.assertEqual(set(ver["verified_releases"]), {rel1["id"], rel2["id"]})

        # Corrupting a release file is caught by verify.
        (Path(rel2["release_dir"]) / "bin" / "app.txt").write_bytes(b"tampered")
        bad_ver = store_mod.verify_store(self.store, release_id=rel2["id"])
        self.assertFalse(bad_ver["ok"])
        self.assertTrue(bad_ver["errors"])

    def test_copy_fallback_when_hardlink_fails(self) -> None:
        src = self._make_tree("src-copy", {"data/item.txt": b"cross-device-payload"})
        with mock.patch("os.link", side_effect=OSError("EXDEV")):
            rel = store_mod.materialize_release(self.store, src, refs=["pinned"])
        self.assertEqual(rel["link_modes"]["copy"], 1)
        self.assertEqual(rel["link_modes"]["hardlink"], 0)
        self.assertTrue(store_mod.verify_store(self.store)["ok"])

        # Even when st_nlink == 1 due to copy fallback, GC keeps blobs referenced by retained releases.
        gc_res = store_mod.gc_store(self.store)
        self.assertEqual(gc_res["removed_releases"], [])
        self.assertEqual(gc_res["removed_blobs"], [])
        self.assertEqual(gc_res["kept_blobs"], 1)

    def test_symlink_and_junction_rejection(self) -> None:
        real_dir = self.tmp / "real-outside"
        real_dir.mkdir()
        (real_dir / "secret.txt").write_bytes(b"secret")

        src = self._make_tree("src-with-link", {"normal.txt": b"ok"})
        link_created = False
        if os.name == "nt":
            junction = src / "linked-dir"
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(real_dir)],
                capture_output=True,
                text=True,
            )
            link_created = proc.returncode == 0
        else:
            os.symlink(real_dir, src / "linked-dir")
            link_created = True

        self.assertTrue(link_created, "Expected junction or symlink creation to succeed in temp directory")
        with self.assertRaises(store_mod.SymlinkTraversalError):
            store_mod.materialize_release(self.store, src)

        # Store root traversing through a link/junction is also rejected.
        linked_store = self.tmp / "linked-store-parent"
        target_store = self.tmp / "real-store-parent"
        target_store.mkdir()
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(linked_store), str(target_store)],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(target_store, linked_store)
        with self.assertRaises(store_mod.SymlinkTraversalError):
            store_mod.materialize_release(linked_store / "store", self._make_tree("ok-src", {"a.txt": b"a"}))

    def test_store_lock_and_gc_retention(self) -> None:
        shared = b"common-payload"
        only_stale = b"stale-only-payload"
        only_kept = b"kept-only-payload"

        s_keep = self._make_tree("keep", {"shared.bin": shared, "kept.bin": only_kept})
        s_ref = self._make_tree("ref", {"shared.bin": shared})
        s_stale = self._make_tree("stale", {"shared.bin": shared, "stale.bin": only_stale})

        r_keep = store_mod.materialize_release(self.store, s_keep, release_id="rel-keep")
        r_ref = store_mod.materialize_release(self.store, s_ref, release_id="rel-ref", refs=["active"])
        r_stale = store_mod.materialize_release(self.store, s_stale, release_id="rel-stale")

        # Advisory lock excludes concurrent second lock attempt.
        with store_mod.store_lock(self.store):
            with self.assertRaises(store_mod.StoreLockError):
                with store_mod.store_lock(self.store):
                    pass

        dry_gc = store_mod.gc_store(self.store, keep=[r_keep["id"]], dry_run=True)
        self.assertEqual(dry_gc["removed_releases"], ["rel-stale"])
        stale_hash = hashlib.sha256(only_stale).hexdigest()
        self.assertEqual(dry_gc["removed_blobs"], [stale_hash])
        self.assertTrue((self.store / "releases" / "rel-stale").is_dir())

        real_gc = store_mod.gc_store(self.store, keep=[r_keep["id"]], dry_run=False)
        self.assertEqual(real_gc["removed_releases"], ["rel-stale"])
        self.assertEqual(real_gc["removed_blobs"], [stale_hash])
        self.assertFalse((self.store / "releases" / "rel-stale").exists())
        self.assertTrue((self.store / "releases" / "rel-keep").is_dir())
        self.assertTrue((self.store / "releases" / r_ref["id"]).is_dir())

        shared_hash = hashlib.sha256(shared).hexdigest()
        kept_hash = hashlib.sha256(only_kept).hexdigest()
        self.assertTrue(store_mod.blob_path(self.store, shared_hash).is_file())
        self.assertTrue(store_mod.blob_path(self.store, kept_hash).is_file())
        self.assertFalse(store_mod.blob_path(self.store, stale_hash).exists())
        self.assertTrue(store_mod.verify_store(self.store)["ok"])

    def test_cli_verify_and_gc(self) -> None:
        src = self._make_tree("cli-src", {"x.txt": b"xyz"})
        mat = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "artifact_store.py"),
                "materialize",
                "--store",
                str(self.store),
                "--source",
                str(src),
                "--id",
                "r1",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(mat.stdout)["id"], "r1")

        ver = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "artifact_store.py"),
                "verify",
                "--store",
                str(self.store),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ver.returncode, 0)
        self.assertTrue(json.loads(ver.stdout)["ok"])

        gc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "artifact_store.py"),
                "gc",
                "--store",
                str(self.store),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(gc.stdout)["removed_releases"], ["r1"])


class BuildFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="fingerprint-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_deterministic_hashing_and_sensitivity(self) -> None:
        src_a = self.tmp / "tree-a" / "proj"
        src_b = self.tmp / "tree-b" / "proj"
        for root in (src_a, src_b):
            (root / "src").mkdir(parents=True)
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (root / "src" / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
            (root / "README.txt").write_text("hello\n", encoding="utf-8")

        libm_archive = self.tmp / "libmath.a"
        libm_archive.write_bytes(b"static-math-archive-bytes")

        fp1 = fp_mod.compute_fingerprint(
            compiler="icx",
            compiler_version="Intel oneAPI 2026.1.1",
            flags=["-fp-model=precise -ffp-contract=off -fno-fast-math -fsigned-zeros -fno-builtin-cos -march=x86-64-v2"],
            inputs=[str(src_a)],
            libm=[str(libm_archive)],
            include_host_cpuid=False,
        )
        # Dot-file change in .git does not disturb default hidden-filtered fingerprint, and identical copy matches.
        (src_b / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
        fp2 = fp_mod.compute_fingerprint(
            compiler="icx",
            compiler_version="Intel oneAPI 2026.1.1",
            flags=["-fp-model=precise -ffp-contract=off -fno-fast-math -fsigned-zeros -fno-builtin-cos -march=x86-64-v2"],
            inputs=[str(src_b)],
            libm=[str(libm_archive)],
            include_host_cpuid=False,
        )
        self.assertEqual(fp1["fingerprint_sha256"], fp2["fingerprint_sha256"])
        self.assertEqual(fp1["subject"]["fp_contract"]["fp_model"], "precise")
        self.assertEqual(fp1["subject"]["fp_contract"]["fp_contract"], "off")
        self.assertEqual(fp1["subject"]["fp_contract"]["nobuiltins"], ["cos"])
        self.assertEqual(fp1["subject"]["libm"]["libraries"][0]["kind"], "archive")

        # Changing flags, compiler, libm, or source bytes changes the digest.
        fp_fast = fp_mod.compute_fingerprint(
            compiler="icx",
            compiler_version="Intel oneAPI 2026.1.1",
            flags=["-ffast-math"],
            inputs=[str(src_a)],
            libm=[str(libm_archive)],
            include_host_cpuid=False,
        )
        self.assertNotEqual(fp1["fingerprint_sha256"], fp_fast["fingerprint_sha256"])

        (src_a / "src" / "main.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
        fp_edited = fp_mod.compute_fingerprint(
            compiler="icx",
            compiler_version="Intel oneAPI 2026.1.1",
            flags=["-fp-model=precise -ffp-contract=off -fno-fast-math -fsigned-zeros -fno-builtin-cos -march=x86-64-v2"],
            inputs=[str(src_a)],
            libm=[str(libm_archive)],
            include_host_cpuid=False,
        )
        self.assertNotEqual(fp1["fingerprint_sha256"], fp_edited["fingerprint_sha256"])

    def test_cpuid_and_cli_formats_and_verify(self) -> None:
        cpuid = fp_mod.probe_cpuid()
        self.assertIn("arch", cpuid)
        self.assertIsInstance(cpuid["features"], list)

        input_file = self.tmp / "module.c"
        input_file.write_text("double f(double x) { return x; }\n", encoding="utf-8")
        receipt_path = self.tmp / "receipt.json"

        json_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "build_fingerprint.py"),
                "--compiler",
                "gcc",
                "--compiler-version",
                "gcc 14.2.0",
                "--flags",
                "-O2 -ffp-contract=off",
                "--input",
                str(input_file),
                "--format",
                "json",
                "--output",
                str(receipt_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        doc = json.loads(json_proc.stdout)
        self.assertEqual(doc["schema"], "build-fingerprint/v1")
        self.assertEqual(len(doc["fingerprint_sha256"]), 64)

        sha_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "build_fingerprint.py"),
                "--compiler",
                "gcc",
                "--compiler-version",
                "gcc 14.2.0",
                "--flags",
                "-O2 -ffp-contract=off",
                "--input",
                str(input_file),
                "--format",
                "sha256",
                "--verify",
                str(receipt_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(sha_proc.returncode, 0)
        self.assertEqual(sha_proc.stdout.strip(), doc["fingerprint_sha256"])

        # Mutate input and verify fails.
        input_file.write_text("double f(double x) { return x + 1.0; }\n", encoding="utf-8")
        fail_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "build_fingerprint.py"),
                "--compiler",
                "gcc",
                "--compiler-version",
                "gcc 14.2.0",
                "--flags",
                "-O2 -ffp-contract=off",
                "--input",
                str(input_file),
                "--verify",
                str(receipt_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(fail_proc.returncode, 1)

        if os.name == "nt":
            for launcher in ("artifact-store.cmd", "build-fingerprint.cmd"):
                proc = subprocess.run(
                    ["cmd", "/c", str(PACKAGE_DIR / launcher), "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("0.1.0", proc.stdout)


if __name__ == "__main__":
    unittest.main()
