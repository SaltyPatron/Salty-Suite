#!/usr/bin/env python3
"""Unit tests for deps_installer.py."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

PACKAGE_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))

import deps_installer as di  # noqa: E402


MANIFEST_HEADER = "name\tversion\tkind\torigin\tref\tpin\tbuild\tprofile\tplatforms\tdepends\tuse\tproof\n"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ManifestAndTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="deps-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write_manifest(self, body: str) -> Path:
        path = self.tmp / "dependencies.tsv"
        path.write_text(MANIFEST_HEADER + body, encoding="utf-8")
        return path

    def test_parse_filter_and_topological_order(self) -> None:
        pin_a = "a" * 40
        pin_b = "b" * 64
        pin_c = "c" * 40
        manifest = self._write_manifest(
            "# comment line\n"
            "\n"
            f"app\t1.2.0\tsource\thttps://example.invalid/app.git\tv1.2.0\t{pin_c}\tcmake\ticx-deterministic\tlinux,windows\tlibb,winonly\tmain app\tbin/app\n"
            f"libb\t2.0.0\trelease\thttps://example.invalid/libb.zip\tv2.0.0\t{pin_b}\tunpack\tnone\tlinux,windows\tliba\tshared lib\t.pin\n"
            f"liba\t1.0.0\tsource\thttps://example.invalid/liba.git\t-\t{pin_a}\tnone\ticx\tlinux,windows\t-\theaders\tinclude/a.h\n"
            f"winonly\t0.1.0\ttool\thttps://example.invalid/win.zip\t-\t{pin_b}\tunpack\tupstream-default\twindows\t-\twin helper\t.pin\n"
            f"cargotool\t0.2.0\ttool\thttps://example.invalid/liba.git\t-\t{pin_a}\tcargo\tupstream-default\twindows\tliba\tgenerator\tbin/tool.exe\n"
            f"runner\t1.0\trelease\thttps://example.invalid/runner-win.zip\t-\t{pin_b}\tunpack\tnone\twindows\t-\trunner\trunner.exe\n"
            f"runner\t1.0\trelease\thttps://example.invalid/runner-lin.tar.gz\t-\t{pin_b}\tunpack\tnone\tlinux\t-\trunner\trunner\n"
        )
        entries = di.parse_manifest(manifest)
        self.assertEqual(len(entries), 7)

        linux_entries = di.filter_manifest(entries, platform="linux")
        self.assertEqual([e.name for e in linux_entries], ["app", "libb", "liba", "runner"])
        ordered = di.resolve_order(linux_entries)
        self.assertEqual([e.name for e in ordered], ["liba", "libb", "app", "runner"])

        subset = di.resolve_order(linux_entries, names=["libb"])
        self.assertEqual([e.name for e in subset], ["liba", "libb"])

        win_entries = di.filter_manifest(entries, platform="windows")
        win_ordered = di.resolve_order(win_entries)
        self.assertEqual([e.name for e in win_ordered], ["liba", "libb", "winonly", "app", "cargotool", "runner"])

    def test_cycle_detection(self) -> None:
        pin = "1" * 40
        manifest = self._write_manifest(
            f"alpha\t1.0\tsource\thttps://example.invalid/a.git\t-\t{pin}\tcmake\ticx\tlinux\tgamma\t-\tlib/a.a\n"
            f"beta\t1.0\tsource\thttps://example.invalid/b.git\t-\t{pin}\tcmake\ticx\tlinux\talpha\t-\tlib/b.a\n"
            f"gamma\t1.0\tsource\thttps://example.invalid/c.git\t-\t{pin}\tcmake\ticx\tlinux\tbeta\t-\tlib/c.a\n"
        )
        entries = di.filter_manifest(di.parse_manifest(manifest), platform="linux")
        with self.assertRaises(di.DependencyCycleError) as ctx:
            di.resolve_order(entries)
        self.assertIn("->", str(ctx.exception))

    def test_self_dependency_and_bad_rows_rejected(self) -> None:
        pin = "2" * 40
        bad_self = self._write_manifest(
            f"selfie\t1.0\tsource\thttps://example.invalid/s.git\t-\t{pin}\tnone\tnone\tlinux\tselfie\t-\ta.h\n"
        )
        with self.assertRaises(di.DependencyCycleError):
            di.parse_manifest(bad_self)

        bad_unknown = self._write_manifest(
            f"orphan\t1.0\tsource\thttps://example.invalid/o.git\t-\t{pin}\tnone\tnone\tlinux\tghost\t-\ta.h\n"
        )
        with self.assertRaises(di.ManifestError):
            di.parse_manifest(bad_unknown)

        bad_pin = self._write_manifest(
            "rel\t1.0\trelease\thttps://example.invalid/r.zip\t-\tnot-a-sha256\tunpack\tnone\tlinux\t-\t-\t.pin\n"
        )
        with self.assertRaises(di.ManifestError):
            di.parse_manifest(bad_pin)

        bad_dup = self._write_manifest(
            f"dup\t1.0\tsource\thttps://example.invalid/d.git\t-\t{pin}\tnone\tnone\tlinux,windows\t-\t-\ta.h\n"
            f"dup\t1.1\tsource\thttps://example.invalid/d.git\t-\t{pin}\tnone\tnone\twindows\t-\t-\ta.h\n"
        )
        with self.assertRaises(di.ManifestError):
            di.parse_manifest(bad_dup)


class VerificationArchiveAndGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="deps-exec-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = self.tmp / "prefix"
        self.src_dir = self.tmp / "src"
        self.build_dir = self.tmp / "build"
        self.data_dir = self.tmp / "data"
        self.layout = di.InstallLayout(
            prefix_root=self.prefix,
            src_root=self.src_dir,
            build_root=self.build_dir,
            data_root=self.data_dir,
        )

    def _make_zip(self, path: Path, files: dict[str, bytes]) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return di.compute_sha256(path)

    def _make_tar_gz(self, path: Path, files: dict[str, bytes]) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(path, "w:gz") as tf:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        return di.compute_sha256(path)

    def test_verify_sha256_pass_and_fail(self) -> None:
        sample = self.tmp / "payload.bin"
        raw = b"deterministic-payload-bytes"
        sample.write_bytes(raw)
        expected = _sha256_bytes(raw)
        self.assertEqual(di.verify_sha256(sample, expected.upper()), expected)
        with self.assertRaises(di.PinVerificationError):
            di.verify_sha256(sample, "0" * 64)

    def test_build_archive_plan_apply_and_receipt(self) -> None:
        archive = self.tmp / "archives" / "toolkit-1.4.0.zip"
        pin = self._make_zip(
            archive,
            {
                "toolkit-1.4.0/bin/tool.txt": b"hello-tool\n",
                "toolkit-1.4.0/include/tool.h": b"#define TOOL_VER 140\n",
            },
        )
        entry = di.DependencyEntry(
            name="toolkit",
            version="1.4.0",
            kind="release",
            origin=str(archive),
            ref="v1.4.0",
            pin=pin,
            build="unpack",
            profile="none",
            platforms=("linux", "windows"),
            depends=(),
            use="tests",
            proof="include/tool.h",
        )
        target_prefix = self.layout.entry_prefix(entry)
        dry = di.build_archive(entry, archive, target_prefix, apply=False)
        self.assertEqual(dry["status"], "planned")
        self.assertFalse(target_prefix.exists())

        applied = di.build_archive(entry, archive, target_prefix, apply=True)
        self.assertEqual(applied["status"], "installed")
        self.assertTrue((target_prefix / "include/tool.h").is_file())
        self.assertEqual(target_prefix, self.prefix / "toolkit" / "1.4.0")

        receipt = di.read_pin_receipt(target_prefix / ".pin")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["schema"], "deps-installer.pin/v1")
        self.assertEqual(receipt["pin"], pin)
        self.assertEqual(receipt["archive_sha256"], pin)

        reapplied = di.build_archive(entry, archive, target_prefix, apply=True)
        self.assertEqual(reapplied["status"], "already_installed")

        bad_entry = di.DependencyEntry(**{**entry.__dict__, "pin": "f" * 64})
        with self.assertRaises(di.PinVerificationError):
            di.build_archive(bad_entry, archive, target_prefix, apply=True)

    def test_archive_traversal_rejected(self) -> None:
        bad_zip = self.tmp / "evil.zip"
        pin = self._make_zip(bad_zip, {"../outside.txt": b"boom"})
        entry = di.DependencyEntry(
            name="evil",
            version="1.0",
            kind="release",
            origin=str(bad_zip),
            ref="-",
            pin=pin,
            build="unpack",
            profile="none",
            platforms=("linux", "windows"),
            depends=(),
            use="test",
            proof=".pin",
        )
        with self.assertRaises(di.UnsafePathError):
            di.build_archive(entry, bad_zip, self.layout.entry_prefix(entry), apply=True)

        bad_tar = self.tmp / "evil-link.tar.gz"
        with tarfile.open(bad_tar, "w:gz") as tf:
            sym = tarfile.TarInfo(name="pkg/link")
            sym.type = tarfile.SYMTYPE
            sym.linkname = "/etc/passwd"
            tf.addfile(sym)
        tar_pin = di.compute_sha256(bad_tar)
        tar_entry = di.DependencyEntry(**{**entry.__dict__, "origin": str(bad_tar), "pin": tar_pin})
        with self.assertRaises(di.UnsafePathError):
            di.build_archive(tar_entry, bad_tar, self.layout.entry_prefix(tar_entry), apply=True)

    def test_checkout_commit_and_proof_verification(self) -> None:
        upstream = self.tmp / "upstream-repo"
        upstream.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=upstream, check=True)
        subprocess.run(["git", "config", "user.email", "ci@example.invalid"], cwd=upstream, check=True)
        subprocess.run(["git", "config", "user.name", "CI"], cwd=upstream, check=True)
        (upstream / "include").mkdir()
        (upstream / "include" / "core.h").write_text("#define CORE_V 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=upstream, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=upstream, check=True)
        commit_v1 = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()

        (upstream / "include" / "core.h").write_text("#define CORE_V 2\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-q", "-am", "v2"], cwd=upstream, check=True)

        tar_archive = self.tmp / "tarpkg.tar.gz"
        tar_pin = self._make_tar_gz(tar_archive, {"tarpkg-1.0/lib/hello.txt": b"tar-content\n"})

        manifest = self.tmp / "dependencies.tsv"
        manifest.write_text(
            MANIFEST_HEADER
            + f"corelib\t1.0.0\tsource\t{upstream}\t-\t{commit_v1}\tnone\ticx-deterministic\tlinux,windows\t-\tcore headers\tinclude/core.h\n"
            + f"tarpkg\t1.0.0\trelease\t{tar_archive}\t-\t{tar_pin}\tunpack\tupstream-default\tlinux,windows\tcorelib\tarchive dep\tlib/hello.txt\n",
            encoding="utf-8",
        )
        entries = di.filter_manifest(di.parse_manifest(manifest))

        before_report = di.verify_proofs(entries, self.layout)
        self.assertFalse(before_report["ok"])
        self.assertEqual(before_report["failures"], 2)
        self.assertEqual([r["status"] for r in before_report["results"]], ["missing_proof", "missing_proof"])

        plan = di.plan_install(entries, self.layout, offline=True)
        self.assertEqual(plan["order"], ["corelib", "tarpkg"])
        self.assertFalse((self.src_dir / "corelib").exists())
        self.assertFalse((self.prefix / "tarpkg" / "1.0.0").exists())

        applied = di.apply_install(entries, self.layout, offline=True)
        self.assertEqual(applied["order"], ["corelib", "tarpkg"])
        self.assertEqual([s["status"] for s in applied["steps"]], ["installed", "installed"])
        self.assertIn("#define CORE_V 1", (self.src_dir / "corelib" / "include" / "core.h").read_text(encoding="utf-8"))

        after_report = di.verify_proofs(entries, self.layout)
        self.assertTrue(after_report["ok"])
        self.assertEqual(after_report["failures"], 0)
        self.assertTrue(all(r["pin_verified"] for r in after_report["results"]))

    def test_toolchain_profiles_builtin_and_custom(self) -> None:
        profiles = di.load_profiles()
        det = di.resolve_profile("icx-deterministic", profiles, platform="linux")
        self.assertTrue(det["deterministic_fp"])
        self.assertIn("-fp-model=precise", det["fp_flags"])
        self.assertIn("-ffp-contract=off", det["fp_flags"])
        self.assertEqual(det["cxx_compiler"], "icpx")

        det_win = di.resolve_profile("icx-deterministic", profiles, platform="windows")
        self.assertIn("/fp:precise", det_win["fp_flags"])
        self.assertEqual(det_win["cxx_compiler"], "icx")

        up = di.resolve_profile("upstream-default", profiles, platform="linux")
        self.assertFalse(up["deterministic_fp"])
        self.assertEqual(up["fp_flags"], [])

        custom_json = self.tmp / "profiles.json"
        custom_json.write_text(
            json.dumps(
                {
                    "profiles": {
                        "clang-strict": {
                            "description": "Strict Clang FP profile",
                            "c_compiler": "clang",
                            "cxx_compiler": "clang++",
                            "fp_flags": {"gnu": ["-ffp-contract=off", "-fno-fast-math"], "msvc": []},
                            "link_flags": {"gnu": ["-lm"], "msvc": []},
                            "deterministic_fp": True,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        merged = di.load_profiles(custom_json)
        clang_prof = di.resolve_profile("clang-strict", merged, platform="linux")
        self.assertEqual(clang_prof["c_compiler"], "clang")
        self.assertTrue(clang_prof["deterministic_fp"])

        with self.assertRaises(di.ManifestError):
            di.resolve_profile("nonexistent-profile", merged)

    def test_cli_verify_proofs_and_launchers(self) -> None:
        manifest = self.tmp / "deps.tsv"
        archive = self.tmp / "pkg.zip"
        pin = self._make_zip(archive, {"pkg/ok.txt": b"ok"})
        manifest.write_text(
            MANIFEST_HEADER
            + f"pkg\t1.0\trelease\t{archive}\t-\t{pin}\tunpack\ticx\tlinux,windows\t-\tverify\tok.txt\n",
            encoding="utf-8",
        )
        fail_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "deps_installer.py"),
                "--manifest",
                str(manifest),
                "--prefix",
                str(self.prefix),
                "--src-dir",
                str(self.src_dir),
                "--build-dir",
                str(self.build_dir),
                "--verify-proofs",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(fail_proc.returncode, 1)
        self.assertFalse(json.loads(fail_proc.stdout)["ok"])

        apply_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "deps_installer.py"),
                "--manifest",
                str(manifest),
                "--prefix",
                str(self.prefix),
                "--src-dir",
                str(self.src_dir),
                "--build-dir",
                str(self.build_dir),
                "--apply",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(json.loads(apply_proc.stdout)["steps"][0]["status"], "installed")

        pass_proc = subprocess.run(
            [
                sys.executable,
                str(PACKAGE_DIR / "deps_installer.py"),
                "--manifest",
                str(manifest),
                "--prefix",
                str(self.prefix),
                "--src-dir",
                str(self.src_dir),
                "--build-dir",
                str(self.build_dir),
                "--verify-proofs",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(pass_proc.returncode, 0)
        self.assertTrue(json.loads(pass_proc.stdout)["ok"])

        if os.name == "nt":
            cmd_proc = subprocess.run(
                ["cmd", "/c", str(PACKAGE_DIR / "deps-installer.cmd"), "--version"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("deps-installer 0.1.0", cmd_proc.stdout)


if __name__ == "__main__":
    unittest.main()
