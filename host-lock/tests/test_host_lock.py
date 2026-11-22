#!/usr/bin/env python3
"""Unit tests for host_lock.py and its Windows/POSIX launchers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
PKG_DIR = HERE.parent
HOST_LOCK_PY = PKG_DIR / 'host_lock.py'
HOST_LOCK_CMD = PKG_DIR / 'host-lock.cmd'
HOST_LOCK_PS1 = PKG_DIR / 'host-lock.ps1'


def run_host_lock(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOST_LOCK_PY), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def popen_host_lock(args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(HOST_LOCK_PY), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_popen(proc: subprocess.Popen[str] | None, *, kill: bool = False) -> tuple[str, str]:
    if proc is None:
        return '', ''
    if kill and proc.poll() is None:
        proc.kill()
    out, err = proc.communicate(timeout=10)
    return out, err


def wait_for_file(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.02)
    raise AssertionError(f'Timed out waiting for file: {path}')


def make_hold_script(ready: Path, release: Path) -> str:
    return '\n'.join([
        'import pathlib, time',
        f'pathlib.Path({str(ready)!r}).write_text("1", encoding="utf-8")',
        'deadline = time.monotonic() + 10',
        f'while not pathlib.Path({str(release)!r}).exists() and time.monotonic() < deadline:',
        '    time.sleep(0.02)',
    ])


class HostLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name)
        self.lock = self.work / 'resource.lock'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shared_locks_admit_concurrent_readers_and_refuse_writer(self) -> None:
        r1_ready = self.work / 'r1.ready'
        r1_release = self.work / 'r1.release'
        r1_receipt = self.work / 'r1.receipt.json'
        r2_receipt = self.work / 'r2.receipt.json'
        w_receipt = self.work / 'w.receipt.json'

        p1 = popen_host_lock([
            '--mode', 'shared',
            '--path', str(self.lock),
            '--wait', '5',
            '--receipt', str(r1_receipt),
            '--', sys.executable, '-c', make_hold_script(r1_ready, r1_release),
        ])
        try:
            wait_for_file(r1_ready)

            # Second shared reader succeeds concurrently without failing or evicting holder 1.
            r2 = run_host_lock([
                '--mode', 'shared',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(r2_receipt),
                '--', sys.executable, '-c', 'print("ok")',
            ])
            self.assertEqual(r2.returncode, 0, msg=r2.stderr)
            self.assertEqual(r2.stdout.strip(), 'ok')

            rec2 = json.loads(r2_receipt.read_text(encoding='utf-8'))
            self.assertTrue(rec2['acquired'])
            self.assertEqual(rec2['mode'], 'shared')
            self.assertFalse(rec2['stale_recovered'])

            # Exclusive writer times out with exit code 3 while reader 1 holds lock.
            w = run_host_lock([
                '--mode', 'exclusive',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(w_receipt),
                '--', sys.executable, '-c', 'print("should_not_run")',
            ])
            self.assertEqual(w.returncode, 3)
            self.assertNotIn('should_not_run', w.stdout)
            self.assertIn('host lock not acquired', w.stderr)
            self.assertIn(f'held by pid {p1.pid}', w.stderr)

            w_rec = json.loads(w_receipt.read_text(encoding='utf-8'))
            self.assertFalse(w_rec['acquired'])
            self.assertEqual(w_rec['mode'], 'exclusive')
        finally:
            r1_release.write_text('go', encoding='utf-8')
            finish_popen(p1)
        self.assertEqual(p1.returncode, 0)

    def test_exclusive_blocks_shared_and_exclusive_then_releases(self) -> None:
        ready = self.work / 'ex.ready'
        release = self.work / 'ex.release'
        holder = popen_host_lock([
            '--mode', 'exclusive',
            '--path', str(self.lock),
            '--wait', '5',
            '--', sys.executable, '-c', make_hold_script(ready, release),
        ])
        try:
            wait_for_file(ready)
            for mode in ('shared', 'exclusive'):
                blocked = run_host_lock([
                    '--mode', mode,
                    '--path', str(self.lock),
                    '--wait', '0.15',
                    '--', sys.executable, '-c', 'print("nope")',
                ])
                self.assertEqual(blocked.returncode, 3)
                self.assertEqual(blocked.stdout, '')
                self.assertIn('Waiting up to', blocked.stderr)
        finally:
            release.write_text('done', encoding='utf-8')
            finish_popen(holder)
        self.assertEqual(holder.returncode, 0)

        # Lock file stays present and remains openable for subsequent exclusive acquisition.
        self.assertTrue(self.lock.exists())
        after = run_host_lock([
            '--mode', 'exclusive',
            '--path', str(self.lock),
            '--wait', '0',
            '--', sys.executable, '-c', 'print("acquired_after")',
        ])
        self.assertEqual(after.returncode, 0)
        self.assertEqual(after.stdout.strip(), 'acquired_after')

    def test_writer_preference_blocks_late_reader_behind_waiting_writer(self) -> None:
        r1_ready = self.work / 'r1.ready'
        r1_release = self.work / 'r1.release'
        w_written = self.work / 'writer.done'
        r2_after = self.work / 'reader2.after'

        r1 = popen_host_lock([
            '--mode', 'shared', '--path', str(self.lock), '--wait', '5',
            '--', sys.executable, '-c', make_hold_script(r1_ready, r1_release),
        ])
        writer = None
        r2 = None
        try:
            wait_for_file(r1_ready)

            writer_script = '\n'.join([
                'import pathlib, time',
                f'pathlib.Path({str(w_written)!r}).write_text("written", encoding="utf-8")',
                'time.sleep(0.25)',
            ])
            writer = popen_host_lock([
                '--mode', 'exclusive', '--path', str(self.lock), '--wait', '10',
                '--', sys.executable, '-c', writer_script,
            ])

            # Give writer time to claim the turnstile behind active reader 1.
            time.sleep(0.2)
            self.assertFalse(w_written.exists())

            r2_script = '\n'.join([
                'import pathlib',
                f'assert pathlib.Path({str(w_written)!r}).exists()',
                f'pathlib.Path({str(r2_after)!r}).write_text("after", encoding="utf-8")',
            ])
            r2 = popen_host_lock([
                '--mode', 'shared', '--path', str(self.lock), '--wait', '10',
                '--', sys.executable, '-c', r2_script,
            ])
            time.sleep(0.2)
            self.assertFalse(w_written.exists())
            self.assertFalse(r2_after.exists())

            r1_release.write_text('release', encoding='utf-8')
            finish_popen(r1)
            _, w_err = finish_popen(writer)
            _, r2_err = finish_popen(r2)
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(writer.returncode, 0, msg=w_err)
            self.assertEqual(r2.returncode, 0, msg=r2_err)
            self.assertTrue(r2_after.exists())
        finally:
            if not r1_release.exists():
                r1_release.write_text('release', encoding='utf-8')
            for proc in (r1, writer, r2):
                finish_popen(proc, kill=(proc is not None and proc.poll() is None))

    def test_misuse_returns_exit_code_2(self) -> None:
        bad_invocations = [
            [],
            ['--mode', 'both', '--path', str(self.lock)],
            ['--mode', 'shared', '--path', ''],
            ['--mode', 'exclusive', '--path', str(self.lock), '--wait', '-1'],
            ['--mode', 'exclusive', '--path', str(self.lock), '--wait', 'abc'],
            ['--mode', 'exclusive', '--path', str(self.lock), '--wait', 'nan'],
            ['--mode', 'exclusive', '--path', str(self.lock), '--'],
            ['--mode', 'exclusive', '--path', str(self.lock), '--unknown-flag'],
        ]
        for argv in bad_invocations:
            with self.subTest(argv=argv):
                res = run_host_lock(argv)
                self.assertEqual(res.returncode, 2, msg=res.stderr)

    def test_missing_parent_directory_returns_exit_code_1(self) -> None:
        bad_lock = self.work / 'missing_dir' / 'resource.lock'
        res = run_host_lock(['--mode', 'exclusive', '--path', str(bad_lock), '--wait', '0'])
        self.assertEqual(res.returncode, 1)

    def test_subcommand_exit_code_forwarded_and_receipt_written(self) -> None:
        receipt = self.work / 'receipt.json'
        probe_script = '\n'.join([
            'import json, pathlib, sys',
            f'data = json.loads(pathlib.Path({str(receipt)!r}).read_text(encoding="utf-8"))',
            'assert data["acquired"] is True',
            'sys.exit(7)',
        ])
        res = run_host_lock([
            '--mode', 'exclusive',
            '--path', str(self.lock),
            '--wait', '0',
            '--receipt', str(receipt),
            '--', sys.executable, '-c', probe_script,
        ])
        self.assertEqual(res.returncode, 7, msg=res.stderr)

        payload = json.loads(receipt.read_text(encoding='utf-8'))
        self.assertEqual(set(payload.keys()), {
            'pid', 'started_utc', 'mode', 'path', 'acquired', 'stale_recovered',
        })
        self.assertIsInstance(payload['pid'], int)
        self.assertTrue(payload['started_utc'].endswith('Z'))
        self.assertEqual(payload['mode'], 'exclusive')
        self.assertEqual(payload['path'], str(self.lock.resolve()))
        self.assertTrue(payload['acquired'])
        self.assertFalse(payload['stale_recovered'])

    def test_stale_pid_recovery_after_killed_holder(self) -> None:
        ready = self.work / 'victim.ready'
        cleanup = self.work / 'victim.cleanup'
        victim_receipt = self.work / 'victim.receipt.json'
        victim = subprocess.Popen(
            [
                sys.executable, str(HOST_LOCK_PY),
                '--mode', 'exclusive',
                '--path', str(self.lock),
                '--wait', '5',
                '--receipt', str(victim_receipt),
                '--', sys.executable, '-c', make_hold_script(ready, cleanup),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_file(ready)
            wait_for_file(victim_receipt)
            victim_pid = victim.pid
            victim.kill()
            victim.wait(timeout=5)

            meta_path = Path(f'{self.lock}.holders.json')
            self.assertTrue(meta_path.exists())
            stale_before = json.loads(meta_path.read_text(encoding='utf-8'))
            self.assertTrue(any(item['pid'] == victim_pid for item in stale_before['holders']))

            recovery_receipt = self.work / 'recovery.receipt.json'
            res = run_host_lock([
                '--mode', 'exclusive',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(recovery_receipt),
                '--', sys.executable, '-c', 'print("recovered")',
            ])
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stdout.strip(), 'recovered')

            rec = json.loads(recovery_receipt.read_text(encoding='utf-8'))
            self.assertTrue(rec['acquired'])
            self.assertTrue(rec['stale_recovered'])

            after_meta = json.loads(meta_path.read_text(encoding='utf-8'))
            self.assertEqual(after_meta['holders'], [])
        finally:
            cleanup.write_text('stop', encoding='utf-8')
            if victim.poll() is None:
                victim.kill()
                victim.wait(timeout=5)

    def test_stale_sidecar_cleanup_preserves_live_shared_holder(self) -> None:
        live_ready = self.work / 'live.ready'
        live_release = self.work / 'live.release'
        live = popen_host_lock([
            '--mode', 'shared', '--path', str(self.lock), '--wait', '5',
            '--', sys.executable, '-c', make_hold_script(live_ready, live_release),
        ])
        try:
            wait_for_file(live_ready)
            meta_path = Path(f'{self.lock}.holders.json')
            meta = json.loads(meta_path.read_text(encoding='utf-8'))
            meta['holders'].append({
                'pid': 99999999,
                'mode': 'shared',
                'started_utc': '2020-01-01T00:00:00Z',
                'token': 'deadbeef',
            })
            meta_path.write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')

            rec_path = self.work / 'shared.receipt.json'
            check_script = '\n'.join([
                'import json, pathlib',
                f'm = json.loads(pathlib.Path({str(meta_path)!r}).read_text(encoding="utf-8"))',
                'pids = {h["pid"] for h in m["holders"]}',
                f'assert {live.pid} in pids',
                'assert 99999999 not in pids',
            ])
            res = run_host_lock([
                '--mode', 'shared',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(rec_path),
                '--', sys.executable, '-c', check_script,
            ])
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            rec = json.loads(rec_path.read_text(encoding='utf-8'))
            self.assertTrue(rec['acquired'])
            self.assertTrue(rec['stale_recovered'])
        finally:
            live_release.write_text('go', encoding='utf-8')
            finish_popen(live)
        self.assertEqual(live.returncode, 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher test')
    def test_windows_cmd_and_ps1_launchers(self) -> None:
        receipt_cmd = self.work / 'cmd.receipt.json'
        res_cmd = subprocess.run(
            [
                'cmd.exe', '/c', str(HOST_LOCK_CMD),
                '--mode', 'exclusive',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(receipt_cmd),
                '--', sys.executable, '-c', 'import sys; sys.exit(9)',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res_cmd.returncode, 9, msg=res_cmd.stderr)
        self.assertTrue(json.loads(receipt_cmd.read_text(encoding='utf-8'))['acquired'])

        receipt_ps1 = self.work / 'ps1.receipt.json'
        res_ps1 = subprocess.run(
            [
                'pwsh', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(HOST_LOCK_PS1),
                '--mode', 'exclusive',
                '--path', str(self.lock),
                '--wait', '0',
                '--receipt', str(receipt_ps1),
                '--', sys.executable, '-c', 'import sys; sys.exit(11)',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res_ps1.returncode, 11, msg=res_ps1.stderr)
        self.assertTrue(json.loads(receipt_ps1.read_text(encoding='utf-8'))['acquired'])


if __name__ == '__main__':
    unittest.main()
