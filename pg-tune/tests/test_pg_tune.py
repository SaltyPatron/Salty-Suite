"""Comprehensive unit tests for pg-tune."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SUITE_ROOT = Path(__file__).resolve().parent.parent
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

import pg_tune  # noqa: E402


GIB = 1024 * 1024 * 1024


class SizeAndInputParsingTests(unittest.TestCase):
    def test_parse_size_kb_units(self) -> None:
        self.assertEqual(pg_tune.parse_size_kb("4096"), 4096)
        self.assertEqual(pg_tune.parse_size_kb(4096), 4096)
        self.assertEqual(pg_tune.parse_size_kb(" 1024 kB "), 1024)
        self.assertEqual(pg_tune.parse_size_kb("512MB"), 512 * 1024)
        self.assertEqual(pg_tune.parse_size_kb("34GB"), 34 * 1048576)
        self.assertEqual(pg_tune.parse_size_kb("1g"), 1048576)
        self.assertEqual(pg_tune.parse_size_kb("2TB"), 2 * 1073741824)
        self.assertEqual(pg_tune.parse_size_kb("4096B"), 4)
        self.assertEqual(pg_tune.parse_size_kb("0"), 0)

    def test_parse_size_kb_rejects_invalid(self) -> None:
        for bad in ("", "-1", "1.5GB", "10XB", "abc", True, -5):
            with self.assertRaises(pg_tune.TuneError):
                pg_tune.parse_size_kb(bad)

    def test_parse_size_bytes_units(self) -> None:
        self.assertEqual(pg_tune.parse_size_bytes("68719476736"), 64 * GIB)
        self.assertEqual(pg_tune.parse_size_bytes(64 * GIB), 64 * GIB)
        self.assertEqual(pg_tune.parse_size_bytes("64GB"), 64 * GIB)
        self.assertEqual(pg_tune.parse_size_bytes("256MB"), 256 * 1024 * 1024)
        self.assertEqual(pg_tune.parse_size_bytes("16kB"), 16 * 1024)
        self.assertEqual(pg_tune.parse_size_bytes("512B"), 512)
        for bad in ("", "-10", "2.5GB", "bad", False):
            with self.assertRaises(pg_tune.TuneError):
                pg_tune.parse_size_bytes(bad)

    def test_parse_bool_and_bounded_int(self) -> None:
        self.assertEqual(pg_tune.parse_bool_int("1", "x"), 1)
        self.assertEqual(pg_tune.parse_bool_int("true", "x"), 1)
        self.assertEqual(pg_tune.parse_bool_int("0", "x"), 0)
        self.assertEqual(pg_tune.parse_bool_int("false", "x"), 0)
        with self.assertRaises(pg_tune.TuneError):
            pg_tune.parse_bool_int("maybe", "x")

        self.assertEqual(pg_tune.parse_bounded_int("0", "s", positive=False), 0)
        self.assertEqual(pg_tune.parse_bounded_int("999999", "s", positive=True), 999999)
        for bad in ("0", "-1", "01", "1.5", "abc", "1000000"):
            with self.assertRaises(pg_tune.TuneError):
                pg_tune.parse_bounded_int(bad, "a", positive=True)


class TopologyAndMemoryMathTests(unittest.TestCase):
    FIXTURES = [
        # name, wal_level, ram_kb, threads, cores, wal_bytes, wal_shared, rot, reserved, cap, ingest, serving, locks
        ("node-a", "minimal", 131799160, 12, 6, 64 * GIB, 0, 0, "0", "0", 16, 48, 1024),
        ("node-b", "minimal", 32862256, 8, 4, 16 * GIB, 0, 0, "0", "0", 12, 32, 1024),
        ("node-c", "replica", 4007004, 2, 1, 50 * GIB, 1, 0, "0", "0", 0, 16, 64),
        ("node-d", "minimal", 50037756, 32, 24, 2000 * GIB, 1, 0, "34GB", "1GB", 52, 128, 1024),
        ("one-core-hdd", "replica", 2048000, 1, 1, 20 * GIB, 1, 1, "0", "0", 0, 16, 64),
        ("node-d-uncapped-res", "minimal", 50037756, 32, 24, 2000381014016, 1, 0, "0", "1GB", 52, 128, 1024),
        ("odd-fractions", "minimal", 17000003, 7, 3, 21474836481, 1, 0, "0", "0", 10, 28, 1024),
    ]

    def _assert_invariants(self, res: pg_tune.TuneResult, expected_cap_kb: int) -> None:
        b = res.budget
        f = res.facts
        s = res.settings

        self.assertEqual(s["max_prepared_transactions"], "0")
        self.assertEqual(s["auto_explain.log_min_duration"], "-1")
        self.assertEqual(s["max_locks_per_transaction"], str(b.locks))
        self.assertLessEqual(b.shared_kb, (f.ram_kb * 2) // 5)
        self.assertLessEqual(b.shared_kb, b.usable_kb // 4)
        self.assertLessEqual(b.ecs_kb, b.usable_kb)
        if expected_cap_kb > 0:
            self.assertLessEqual(b.shared_kb, expected_cap_kb)

        self.assertEqual(b.backends, b.active_queries + b.par)
        self.assertEqual(b.temp_total_kb, b.conns * b.temp_kb)
        self.assertEqual(b.temp_kb, 8192)
        self.assertGreaterEqual(b.work_kb, 4096)
        self.assertGreaterEqual(b.mwm_kb, 65536)
        self.assertGreaterEqual(b.avwm_kb, 1024)
        self.assertGreaterEqual(b.conns, 16)

        used = b.backends * b.work_nodes * b.work_kb + b.temp_total_kb + b.mwm_kb + b.avw * b.avwm_kb
        self.assertEqual(b.over_kb, max(used - b.private_kb, 0))
        self.assertLessEqual(used, b.private_kb + b.over_kb)

        self.assertLessEqual(b.wal_buf_kb, (b.wal_mb * 1024) // 16)
        self.assertGreaterEqual(b.wal_buf_kb, 16384)
        self.assertLess(b.min_wal_mb, b.wal_mb)
        self.assertEqual(b.par, f.cores)
        self.assertLessEqual(b.gather, f.cores // 2)
        self.assertLessEqual(b.iow, 32)
        self.assertLessEqual(b.par, b.mwp)

        if f.rotational == 1:
            self.assertEqual(b.eic, 2)
            self.assertEqual(b.rpc, "4")
        else:
            self.assertEqual(b.eic, b.iow)
            self.assertEqual(b.rpc, "1.1")

        if res.profile.wal_level == "minimal":
            self.assertEqual(s["max_wal_senders"], "0")
        else:
            self.assertEqual(s["max_wal_senders"], "3")

    def test_linux_and_windows_math_parity(self) -> None:
        for (
            _name,
            wal_level,
            ram_kb,
            threads,
            cores,
            wal_bytes,
            wal_shared,
            rot,
            reserved,
            cap,
            ingest,
            serving,
            locks,
        ) in self.FIXTURES:
            base = {
                "ram_kb": ram_kb,
                "threads": threads,
                "cores": cores,
                "wal_bytes": wal_bytes,
                "wal_shared": wal_shared,
                "rotational": rot,
                "reserved_kb": reserved,
                "buffers_max_kb": cap,
                "wal_level": wal_level,
                "ingest_conns": ingest,
                "serving_conns": serving,
                "locks_per_tx": locks,
            }
            lin = pg_tune.generate_plan({**base, "os": "linux"})
            win = pg_tune.generate_plan({**base, "os": "windows"})

            cap_kb = pg_tune.parse_size_kb(cap)
            self._assert_invariants(lin, cap_kb)
            self._assert_invariants(win, cap_kb)
            self.assertEqual(lin.budget, win.budget)
            self.assertEqual(lin.settings["huge_pages"], "try")
            self.assertEqual(win.settings["huge_pages"], "off")

            lin_wo_hp = {k: v for k, v in lin.settings.items() if k != "huge_pages"}
            win_wo_hp = {k: v for k, v in win.settings.items() if k != "huge_pages"}
            self.assertEqual(lin_wo_hp, win_wo_hp)

    def test_workload_population_knobs(self) -> None:
        base = {
            "os": "linux",
            "ram_kb": 131799160,
            "threads": 12,
            "cores": 6,
            "wal_bytes": 64 * GIB,
            "wal_shared": 0,
            "rotational": 0,
            "ingest_conns": 16,
            "serving_conns": 48,
        }
        r_base = pg_tune.generate_plan(base)
        r_single = pg_tune.generate_plan({**base, "active_queries": 1})
        self.assertEqual(r_single.budget.conns, r_base.budget.conns)
        self.assertGreater(r_single.budget.work_kb, r_base.budget.work_kb)

        r_idle = pg_tune.generate_plan({**base, "active_queries": 1, "serving_conns": 100})
        self.assertEqual(r_idle.budget.active_queries, 1)
        self.assertEqual(r_idle.budget.backends, r_single.budget.backends)
        self.assertGreater(r_idle.budget.temp_total_kb, r_single.budget.temp_total_kb)

        r_four = pg_tune.generate_plan({**base, "active_queries": 1, "serving_conns": 100, "work_nodes": 4})
        self.assertLessEqual(r_four.budget.work_kb, r_idle.budget.work_kb // 2)

        for bad_active in ("0", "-1", "01", "1.5", "abc", "1000000", str(r_idle.budget.conns - 2)):
            with self.assertRaises(pg_tune.TuneError):
                pg_tune.generate_plan({**base, "serving_conns": 100, "active_queries": bad_active})

    def test_invalid_hardware_rejected(self) -> None:
        with self.assertRaises(pg_tune.TuneError):
            pg_tune.generate_plan(
                {
                    "os": "linux",
                    "ram_kb": 0,
                    "cores": 4,
                    "threads": 8,
                    "wal_bytes": 64 * GIB,
                    "wal_shared": 0,
                    "rotational": 0,
                }
            )
        with self.assertRaises(pg_tune.TuneError):
            pg_tune.generate_plan(
                {
                    "os": "linux",
                    "ram_kb": 16 * 1048576,
                    "cores": 8,
                    "threads": 4,
                    "wal_bytes": 64 * GIB,
                    "wal_shared": 0,
                    "rotational": 0,
                }
            )


class WalAndIoSettingsTests(unittest.TestCase):
    def test_shared_vs_dedicated_wal_sizing(self) -> None:
        common = {
            "os": "linux",
            "ram_kb": 64 * 1048576,
            "cores": 8,
            "threads": 16,
            "wal_bytes": 256 * GIB,
            "rotational": 0,
        }
        shared = pg_tune.generate_plan({**common, "wal_shared": 1})
        dedicated = pg_tune.generate_plan({**common, "wal_shared": 0})
        self.assertEqual(shared.budget.wal_mb, 32768)
        self.assertEqual(dedicated.budget.wal_mb, 131072)
        self.assertEqual(shared.settings["max_wal_size"], "32768MB")
        self.assertEqual(dedicated.settings["max_wal_size"], "131072MB")
        self.assertEqual(shared.settings["wal_compression"], "zstd")

        tiny = pg_tune.generate_plan({**common, "wal_bytes": 1 * GIB, "wal_shared": 1})
        self.assertEqual(tiny.budget.wal_mb, 1024)
        self.assertEqual(tiny.budget.min_wal_mb, 128)

    def test_rotational_vs_nvme_io_settings(self) -> None:
        common = {
            "os": "linux",
            "ram_kb": 32 * 1048576,
            "cores": 6,
            "threads": 12,
            "wal_bytes": 64 * GIB,
            "wal_shared": 1,
        }
        nvme = pg_tune.generate_plan({**common, "rotational": 0})
        hdd = pg_tune.generate_plan({**common, "rotational": 1})
        self.assertEqual(nvme.settings["io_method"], "worker")
        self.assertEqual(nvme.settings["io_workers"], "8")
        self.assertEqual(nvme.settings["effective_io_concurrency"], "8")
        self.assertEqual(nvme.settings["maintenance_io_concurrency"], "8")
        self.assertEqual(nvme.settings["random_page_cost"], "1.1")

        self.assertEqual(hdd.settings["io_workers"], "8")
        self.assertEqual(hdd.settings["effective_io_concurrency"], "2")
        self.assertEqual(hdd.settings["maintenance_io_concurrency"], "2")
        self.assertEqual(hdd.settings["random_page_cost"], "4")

        sync = pg_tune.generate_plan({**common, "rotational": 0, "io_method": "sync", "huge_pages": "on"})
        self.assertEqual(sync.settings["io_method"], "sync")
        self.assertEqual(sync.settings["huge_pages"], "on")

    def test_trace_preload_and_optional_knobs(self) -> None:
        base = {
            "os": "linux",
            "ram_kb": 16 * 1048576,
            "cores": 4,
            "threads": 8,
            "wal_bytes": 64 * GIB,
            "wal_shared": 1,
            "rotational": 0,
        }
        reduced = pg_tune.generate_plan(
            {
                **base,
                "trace": "reduced",
                "port": 5433,
                "temp_tablespaces": "fast_tmp",
                "ssl": "on",
            }
        )
        self.assertEqual(reduced.settings["pg_stat_statements.track"], "top")
        self.assertEqual(reduced.settings["track_io_timing"], "on")
        self.assertEqual(reduced.settings["port"], "5433")
        self.assertEqual(reduced.settings["temp_tablespaces"], "fast_tmp")
        self.assertEqual(reduced.settings["ssl"], "on")

        off = pg_tune.generate_plan({**base, "trace": "off", "preload": ""})
        self.assertEqual(off.settings["shared_preload_libraries"], "")
        self.assertNotIn("pg_stat_statements.track", off.settings)
        self.assertNotIn("auto_explain.log_min_duration", off.settings)
        self.assertEqual(off.settings["track_io_timing"], "off")
        self.assertEqual(off.settings["track_wal_io_timing"], "off")
        self.assertNotIn("temp_tablespaces", off.settings)


class OvercommitAndDatabaseGuardTests(unittest.TestCase):
    OVERCOMMIT_ARGS = [
        "--os", "linux",
        "--ram-kb", "50037756",
        "--cores", "24",
        "--threads", "32",
        "--wal-bytes", "2000381014016",
        "--wal-shared", "1",
        "--rotational", "0",
        "--reserved-kb", "46GB",
        "--buffers-max-kb", "1GB",
        "--ingest-conns", "52",
        "--serving-conns", "128",
    ]

    def test_overcommit_reported_in_json_and_refused_by_validate(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pg_tune.main([*self.OVERCOMMIT_ARGS, "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["over_kb"], 817892)
        self.assertEqual(payload["budget"]["over_kb"], 817892)

        err2 = io.StringIO()
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            rc2 = pg_tune.main([*self.OVERCOMMIT_ARGS, "--format", "plan", "--validate"])
        self.assertNotEqual(rc2, 0)
        self.assertIn("OVER-COMMITTED", err2.getvalue())

    def test_check_and_apply_refuse_overcommit_before_running_psql(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "db-touched"
            fake_psql = Path(tmp) / "fake-psql.py"
            fake_psql.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

            with mock.patch.object(
                pg_tune,
                "run_psql",
                side_effect=AssertionError("run_psql must not be called when overcommitted"),
            ):
                for flag in ("--check", "--apply"):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        rc = pg_tune.main(
                            [
                                *self.OVERCOMMIT_ARGS,
                                flag,
                                "--psql",
                                str(fake_psql),
                                "--conninfo",
                                "host=localhost dbname=postgres",
                            ]
                        )
                    self.assertNotEqual(rc, 0)
                    self.assertIn("OVER-COMMITTED", err.getvalue())
                    self.assertFalse(marker.exists())

    def test_check_and_apply_require_psql_and_conninfo(self) -> None:
        ok_args = [
            "--os", "linux",
            "--ram-kb", "32GB",
            "--cores", "4",
            "--threads", "8",
            "--wal-bytes", "64GB",
            "--wal-shared", "0",
            "--rotational", "0",
        ]
        for missing in (
            [*ok_args, "--check"],
            [*ok_args, "--apply", "--psql", "psql"],
            [*ok_args, "--check", "--conninfo", "host=localhost dbname=postgres"],
        ):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = pg_tune.main(missing)
            self.assertNotEqual(rc, 0)

    def test_check_and_apply_with_mock_psql(self) -> None:
        args = [
            "--os", "linux",
            "--ram-kb", "32GB",
            "--cores", "4",
            "--threads", "8",
            "--wal-bytes", "64GB",
            "--wal-shared", "0",
            "--rotational", "0",
            "--temp-tablespaces", "tmp_a, tmp_b",
        ]
        plan = pg_tune.generate_plan(
            {
                "os": "linux",
                "ram_kb": "32GB",
                "cores": 4,
                "threads": 8,
                "wal_bytes": "64GB",
                "wal_shared": 0,
                "rotational": 0,
                "temp_tablespaces": "tmp_a, tmp_b",
            }
        )
        matching_rows = [f"{name}|t|sighup|configuration file|f|{val}" for name, val in sorted(plan.settings.items())]

        with tempfile.TemporaryDirectory() as tmp:
            fake_psql = Path(tmp) / "psql_stub.exe"
            fake_psql.write_text("", encoding="utf-8")

            with mock.patch.object(pg_tune, "run_psql", return_value=matching_rows):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = pg_tune.main([*args, "--check", "--psql", str(fake_psql), "--conninfo", "dbname=postgres"])
                self.assertEqual(rc, 0)
                self.assertIn("settings match", out.getvalue())

            drifted_rows = [
                (
                    f"{name}|f|postmaster|default|f|wrong"
                    if name == "shared_preload_libraries"
                    else f"{name}|t|postmaster|configuration file|f|{val}"
                )
                for name, val in sorted(plan.settings.items())
            ]
            with mock.patch.object(pg_tune, "run_psql", return_value=drifted_rows):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = pg_tune.main([*args, "--check", "--psql", str(fake_psql), "--conninfo", "dbname=postgres"])
                self.assertEqual(rc, 1)
                self.assertIn("shared_preload_libraries", err.getvalue())

            recorded_sql: list[str] = []

            def fake_run_psql(_exe: str, _conn: str, sql: str) -> list[str]:
                recorded_sql.append(sql)
                if sql.startswith("WITH want"):
                    return [
                        (
                            f"{name}|f|sighup|default|f|old"
                            if name in ("shared_preload_libraries", "work_mem", "temp_tablespaces")
                            else f"{name}|t|postmaster|configuration file|f|{val}"
                        )
                        for name, val in sorted(plan.settings.items())
                    ]
                return []

            with mock.patch.object(pg_tune, "run_psql", side_effect=fake_run_psql):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = pg_tune.main([*args, "--apply", "--psql", str(fake_psql), "--conninfo", "dbname=postgres"])
                self.assertEqual(rc, 0)
                self.assertIn("3 set", out.getvalue())

            joined = "\n".join(recorded_sql)
            self.assertIn("ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements', 'auto_explain';", joined)
            self.assertIn("ALTER SYSTEM SET temp_tablespaces = 'tmp_a', 'tmp_b';", joined)
            self.assertIn("ALTER SYSTEM SET work_mem =", joined)
            self.assertIn("SELECT pg_reload_conf();", joined)


class LinuxDetectorFixtureTests(unittest.TestCase):
    def test_parse_mountinfo(self) -> None:
        fixture = "\n".join(
            [
                "20 1 253:8 / / rw - ext4 /dev/root rw",
                "21 20 253:2 / /data/pgdata rw - ext4 /dev/data rw",
                "22 21 253:3 / /data/pgdata/pg_wal rw - ext4 /dev/wal rw",
                r"23 20 8:1 / /data/with\040space rw - ext4 /dev/space rw",
                "24 20 0:20 / /run/netns/example rw - nsfs nsfs rw",
            ]
        )
        self.assertEqual(pg_tune.parse_mountinfo("/data/pgdata", fixture), ("253:2", "/data/pgdata"))
        self.assertEqual(pg_tune.parse_mountinfo("/data/pgdata/base/1", fixture), ("253:2", "/data/pgdata"))
        self.assertEqual(pg_tune.parse_mountinfo("/data/pgdata/pg_wal", fixture), ("253:3", "/data/pgdata/pg_wal"))
        self.assertEqual(pg_tune.parse_mountinfo("/data/pgdata/pg_wal/seg", fixture), ("253:3", "/data/pgdata/pg_wal"))
        self.assertEqual(pg_tune.parse_mountinfo("/data/pgdata-other", fixture), ("253:8", "/"))
        self.assertEqual(pg_tune.parse_mountinfo("/data/with space/file", fixture), ("8:1", "/data/with space"))
        self.assertEqual(pg_tune.parse_mountinfo("/unrelated", fixture), ("253:8", "/"))
        with self.assertRaises(pg_tune.TuneError):
            pg_tune.parse_mountinfo("relative", fixture)

    def test_linux_ram_cpu_rotational_wal_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meminfo = root / "meminfo"
            meminfo.write_text("MemTotal:       65899580 kB\nMemFree:        1000 kB\n", encoding="utf-8")
            cg_v2 = root / "memory.max"
            cg_v2.write_text("max\n", encoding="utf-8")
            cg_v1 = root / "memory.limit_in_bytes"
            cg_v1.write_text(str(16 * GIB), encoding="utf-8")
            self.assertEqual(pg_tune.detect_linux_ram_kb(str(meminfo), [str(cg_v2), str(cg_v1)]), 16 * 1048576)

            cg_v2.write_text(str(8 * GIB), encoding="utf-8")
            self.assertEqual(pg_tune.detect_linux_ram_kb(str(meminfo), [str(cg_v2)]), 8 * 1048576)

            cpu_root = root / "cpu"
            for idx, sib in ((0, "0,2"), (1, "1,3"), (2, "0,2"), (3, "1,3")):
                topo = cpu_root / f"cpu{idx}" / "topology"
                topo.mkdir(parents=True)
                (topo / "core_cpus_list").write_text(sib + "\n", encoding="utf-8")
            threads, cores = pg_tune.detect_linux_cpu(str(cpu_root), threads_override=4)
            self.assertEqual((threads, cores), (4, 2))

            mountinfo = root / "mountinfo"
            mountinfo.write_text(
                "20 1 253-2 / /data/pgdata rw - ext4 /dev/dm-0 rw\n"
                "21 20 253-3 / /data/pgdata/pg_wal rw - ext4 /dev/nvme1n1p1 rw\n"
                "22 20 253-4 / /data/ssd rw - ext4 /dev/nvme0n1p1 rw\n",
                encoding="utf-8",
            )
            sys_dev = root / "sys_dev_block"
            sys_cls = root / "sys_class_block"
            (sys_dev / "253-2" / "slaves" / "sda1").mkdir(parents=True)
            (sys_cls / "sda1" / "queue").mkdir(parents=True)
            (sys_cls / "sda1" / "queue" / "rotational").write_text("1\n", encoding="utf-8")
            (sys_dev / "253-3").mkdir(parents=True)
            (sys_dev / "253-3" / "size").write_text(str((64 * GIB) // 512) + "\n", encoding="utf-8")
            (sys_dev / "253-4" / "queue").mkdir(parents=True)
            (sys_dev / "253-4" / "queue" / "rotational").write_text("0\n", encoding="utf-8")
            (sys_dev / "253-4" / "size").write_text(str((100 * GIB) // 512) + "\n", encoding="utf-8")

            self.assertEqual(
                pg_tune.detect_linux_rotational(
                    "/data/pgdata",
                    mountinfo_path=str(mountinfo),
                    sys_dev_block=str(sys_dev),
                    sys_class_block=str(sys_cls),
                ),
                1,
            )
            self.assertEqual(
                pg_tune.detect_linux_rotational(
                    "/data/ssd",
                    mountinfo_path=str(mountinfo),
                    sys_dev_block=str(sys_dev),
                    sys_class_block=str(sys_cls),
                ),
                0,
            )

            self.assertEqual(
                pg_tune.detect_linux_wal(
                    "/data/pgdata/pg_wal",
                    "/data/pgdata",
                    mountinfo_path=str(mountinfo),
                    sys_dev_block=str(sys_dev),
                ),
                (64 * GIB, 0),
            )
            self.assertEqual(
                pg_tune.detect_linux_wal(
                    "/data/ssd/pg_wal",
                    "/data/ssd",
                    mountinfo_path=str(mountinfo),
                    sys_dev_block=str(sys_dev),
                ),
                (100 * GIB, 1),
            )


@unittest.skipUnless(sys.platform.startswith("win"), "Windows kernel32 ctypes test")
class WindowsAutoDetectTests(unittest.TestCase):
    def test_windows_kernel32_detection_and_cli_fallback(self) -> None:
        ram_kb, threads, cores = pg_tune.detect_windows_topology()
        self.assertGreater(ram_kb, 0)
        self.assertGreaterEqual(cores, 1)
        self.assertGreaterEqual(threads, cores)

        disk_bytes = pg_tune.detect_windows_disk_bytes(str(SUITE_ROOT))
        self.assertGreater(disk_bytes, 0)
        self.assertIn(pg_tune.detect_windows_rotational(str(SUITE_ROOT)), (0, 1))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = pg_tune.main(["--format", "json"])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["facts"]["os"], "windows")
        self.assertEqual(data["facts"]["memory_kb"], ram_kb)
        self.assertEqual(data["facts"]["cores"], cores)
        self.assertEqual(data["facts"]["threads"], threads)
        self.assertEqual(data["settings"]["huge_pages"], "off")


class OutputFormatsAndConfigTests(unittest.TestCase):
    BASE_ARGS = [
        "--os", "linux",
        "--ram-kb", "64GB",
        "--cores", "8",
        "--threads", "16",
        "--wal-bytes", "128GB",
        "--wal-shared", "0",
        "--rotational", "0",
        "--ingest-conns", "10",
        "--serving-conns", "32",
        "--locks-per-tx", "256",
        "--wal-level", "minimal",
        "--temp-tablespaces", "fast_tmp",
    ]

    def test_all_output_formats(self) -> None:
        # json
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pg_tune.main([*self.BASE_ARGS, "--format", "json"]), 0)
        j = json.loads(out.getvalue())
        self.assertEqual(j["version"], "0.1.0")
        self.assertIn("facts", j)
        self.assertIn("budget", j)
        self.assertIn("over_kb", j)
        self.assertIn("settings", j)

        # conf
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pg_tune.main([*self.BASE_ARGS, "--format", "conf"]), 0)
        conf_lines = out.getvalue().splitlines()
        self.assertIn("wal_level = minimal", conf_lines)
        self.assertIn("shared_preload_libraries = 'pg_stat_statements,auto_explain'", conf_lines)
        self.assertIn("listen_addresses = '*'", conf_lines)
        self.assertIn("temp_tablespaces = 'fast_tmp'", conf_lines)
        self.assertIn("max_locks_per_transaction = 256", conf_lines)

        # plan
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pg_tune.main([*self.BASE_ARGS, "--format", "plan"]), 0)
        plan_text = out.getvalue()
        self.assertIn("facts of", plan_text)
        self.assertIn("workload budget", plan_text)
        self.assertIn("derived settings", plan_text)

        # declared
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pg_tune.main([*self.BASE_ARGS, "--format", "declared"]), 0)
        decl_lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(decl_lines), len(j["settings"]))
        parsed_keys = [ln.split()[0] for ln in decl_lines]
        self.assertEqual(parsed_keys, list(j["settings"].keys()))

        # env (and round-trip into --env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pg_tune.main([*self.BASE_ARGS, "--format", "env"]), 0)
        env_text = out.getvalue()
        self.assertIn("PG_TUNE_RAM_KB=", env_text)
        self.assertIn("PG_TUNE_OVER_KB=0", env_text)

        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "roundtrip.env"
            env_file.write_text(env_text, encoding="utf-8")
            out_rt = io.StringIO()
            with contextlib.redirect_stdout(out_rt):
                self.assertEqual(pg_tune.main(["--env", str(env_file), "--format", "json"]), 0)
            self.assertEqual(json.loads(out_rt.getvalue()), j)

    def test_json_config_and_explicit_env_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cluster.json"
            cfg.write_text(
                json.dumps(
                    {
                        "os": "linux",
                        "ram_kb": "32GB",
                        "cores": 4,
                        "threads": 8,
                        "wal_bytes": "64GB",
                        "wal_shared": 1,
                        "rotational": 0,
                        "serving_conns": 20,
                    }
                ),
                encoding="utf-8",
            )
            # Without --env, environment variables are ignored.
            with mock.patch.dict(os.environ, {"PG_TUNE_SERVING_CONNS": "99"}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(pg_tune.main(["--config", str(cfg), "--format", "json"]), 0)
                self.assertEqual(json.loads(out.getvalue())["profile"]["serving_conns"], 20)

            # With --env, explicit prefix overrides JSON, and CLI overrides both.
            with mock.patch.dict(os.environ, {"CUSTOM_PG_SERVING_CONNS": "40"}):
                out1 = io.StringIO()
                with contextlib.redirect_stdout(out1):
                    self.assertEqual(
                        pg_tune.main(["--config", str(cfg), "--env", "CUSTOM_PG_", "--format", "json"]),
                        0,
                    )
                self.assertEqual(json.loads(out1.getvalue())["profile"]["serving_conns"], 40)

                out2 = io.StringIO()
                with contextlib.redirect_stdout(out2):
                    self.assertEqual(
                        pg_tune.main(
                            [
                                "--config", str(cfg),
                                "--env", "CUSTOM_PG_",
                                "--serving-conns", "55",
                                "--format", "json",
                            ]
                        ),
                        0,
                    )
                self.assertEqual(json.loads(out2.getvalue())["profile"]["serving_conns"], 55)

            # Default --env (PG_TUNE_) and --env KEY=VALUE also work cleanly.
            with mock.patch.dict(os.environ, {"PG_TUNE_SERVING_CONNS": "64"}):
                out3 = io.StringIO()
                with contextlib.redirect_stdout(out3):
                    self.assertEqual(
                        pg_tune.main(
                            [
                                "--config", str(cfg),
                                "--env",
                                "--env", "LOCKS_PER_TX=512",
                                "--format", "json",
                            ]
                        ),
                        0,
                    )
                j3 = json.loads(out3.getvalue())
                self.assertEqual(j3["profile"]["serving_conns"], 64)
                self.assertEqual(j3["profile"]["locks_per_tx"], 512)


class LauncherTests(unittest.TestCase):
    def test_alter_system_sql_quoting(self) -> None:
        self.assertEqual(
            pg_tune.build_alter_system_sql("shared_preload_libraries", ""),
            "ALTER SYSTEM SET shared_preload_libraries = '';",
        )
        self.assertEqual(
            pg_tune.build_alter_system_sql("shared_preload_libraries", "a, b"),
            "ALTER SYSTEM SET shared_preload_libraries = 'a', 'b';",
        )
        self.assertEqual(
            pg_tune.build_alter_system_sql("work_mem", "64MB"),
            "ALTER SYSTEM SET work_mem = '64MB';",
        )

    def test_launchers(self) -> None:
        if sys.platform.startswith("win"):
            proc = subprocess.run(
                ["cmd.exe", "/c", str(SUITE_ROOT / "pg-tune.cmd"), "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.1.0")

            pwsh = shutil.which("pwsh") or shutil.which("powershell")
            if pwsh:
                ps = subprocess.run(
                    [pwsh, "-NoProfile", "-File", str(SUITE_ROOT / "pg-tune.ps1"), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(ps.returncode, 0, ps.stderr)
                self.assertEqual(ps.stdout.strip(), "0.1.0")
        else:
            sh = shutil.which("sh")
            if sh:
                proc = subprocess.run(
                    [sh, str(SUITE_ROOT / "pg-tune"), "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "0.1.0")


if __name__ == "__main__":
    unittest.main()
