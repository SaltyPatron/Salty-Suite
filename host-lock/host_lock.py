#!/usr/bin/env python3
"""Cross-platform advisory file lock with writer preference, stale-PID recovery, and JSON receipts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

if os.name == 'nt':
    import ctypes
    from ctypes import wintypes
    import msvcrt
else:
    import fcntl

EXIT_OK = 0
EXIT_IO = 1
EXIT_MISUSE = 2
EXIT_TIMEOUT = 3

WRITER_FLAG_OFFSET = 0
SLOT_BASE = 4096
SLOT_COUNT = 65536
POLL_INTERVAL = 0.015
META_LOCK_TIMEOUT = 10.0
META_POLL_INTERVAL = 0.002

if os.name == 'nt':
    _KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _SYNCHRONIZE = 0x00100000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _WAIT_TIMEOUT = 0x00000102
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _KERNEL32.WaitForSingleObject.restype = wintypes.DWORD
    _KERNEL32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL


class LockIOError(OSError):
    """Raised when lock files, sidecars, or receipts cannot be accessed."""


class _UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        print(f'{self.prog}: error: {message}', file=sys.stderr)
        raise SystemExit(EXIT_MISUSE)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID currently exists and has not exited."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == 'nt':
        handle = _KERNEL32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            err = ctypes.get_last_error()
            if err != _ERROR_ACCESS_DENIED:
                return False
            handle = _KERNEL32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
            try:
                code = wintypes.DWORD()
                if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == _STILL_ACTIVE
            finally:
                _KERNEL32.CloseHandle(handle)
        try:
            return _KERNEL32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        finally:
            _KERNEL32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    stat_path = Path(f'/proc/{pid}/stat')
    try:
        stat_text = stat_path.read_text(encoding='utf-8', errors='replace')
        rparen = stat_text.rfind(')')
        if rparen != -1:
            fields = stat_text[rparen + 2:].split()
            if fields and fields[0] == 'Z':
                return False
    except OSError:
        pass
    return True


def _open_rw(path: Path) -> int:
    try:
        return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
    except OSError as exc:
        raise LockIOError(f'cannot open {path}: {exc}') from exc


def _close_quiet(fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _is_contended_error(exc: OSError) -> bool:
    if isinstance(exc, BlockingIOError):
        return True
    err = getattr(exc, 'errno', None)
    return err in {
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, 'EWOULDBLOCK', errno.EAGAIN),
        getattr(errno, 'EDEADLK', -1),
    }


def _msvcrt_try_lock(fd: int, offset: int, length: int) -> bool:
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, length)
        return True
    except OSError as exc:
        if _is_contended_error(exc):
            return False
        raise LockIOError(f'locking error: {exc}') from exc


def _msvcrt_unlock(fd: int, offset: int, length: int) -> None:
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, length)
    except OSError:
        pass


def _flock_try(fd: int, shared: bool) -> bool:
    flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    try:
        fcntl.flock(fd, flag | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if _is_contended_error(exc):
            return False
        raise LockIOError(f'flock error: {exc}') from exc


def _flock_unlock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class _MetadataStore:
    """Guard and update holder sidecar metadata without unlinking files."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self.meta_path = Path(f'{lock_path}.holders.json')
        self.meta_lock_path = Path(f'{lock_path}.holders.lock')

    def _acquire_meta_lock(self) -> int:
        fd = _open_rw(self.meta_lock_path)
        deadline = time.monotonic() + META_LOCK_TIMEOUT
        try:
            while True:
                if os.name == 'nt':
                    if _msvcrt_try_lock(fd, 0, 1):
                        return fd
                else:
                    if _flock_try(fd, shared=False):
                        return fd
                if time.monotonic() >= deadline:
                    raise LockIOError(f'timed out waiting for metadata lock {self.meta_lock_path}')
                time.sleep(META_POLL_INTERVAL)
        except Exception:
            _close_quiet(fd)
            raise

    @staticmethod
    def _release_meta_lock(fd: int) -> None:
        if os.name == 'nt':
            _msvcrt_unlock(fd, 0, 1)
        else:
            _flock_unlock(fd)
        _close_quiet(fd)

    def _read_raw(self) -> tuple[list[dict], bool]:
        fd = _open_rw(self.meta_path)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b''.join(chunks).decode('utf-8', errors='replace').strip()
        finally:
            _close_quiet(fd)

        if not raw:
            return [], False
        try:
            parsed = json.loads(raw)
        except ValueError:
            return [], True

        if isinstance(parsed, dict):
            if 'holders' in parsed and isinstance(parsed['holders'], list):
                entries = parsed['holders']
            elif 'pid' in parsed:
                entries = [parsed]
            else:
                return [], True
        elif isinstance(parsed, list):
            entries = parsed
        else:
            return [], True

        normalized = [item for item in entries if isinstance(item, dict)]
        corrupt = len(normalized) != len(entries)
        return normalized, corrupt

    def _write_raw(self, holders: list[dict]) -> None:
        payload = (json.dumps({'holders': holders}, indent=2) + '\n').encode('utf-8')
        fd = _open_rw(self.meta_path)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            view = memoryview(payload)
            written = 0
            while written < len(payload):
                written += os.write(fd, view[written:])
        except OSError as exc:
            raise LockIOError(f'cannot write metadata sidecar {self.meta_path}: {exc}') from exc
        finally:
            _close_quiet(fd)

    def inspect_holders(self) -> tuple[list[dict], list[dict]]:
        """Return (live_holders, stale_holders) without altering live entries."""
        if not self.meta_path.exists():
            return [], []
        guard = self._acquire_meta_lock()
        try:
            entries, _ = self._read_raw()
            live: list[dict] = []
            stale: list[dict] = []
            for entry in entries:
                pid = entry.get('pid')
                if isinstance(pid, int) and is_pid_alive(pid):
                    live.append(entry)
                else:
                    stale.append(entry)
            return live, stale
        finally:
            self._release_meta_lock(guard)

    def register(self, mode: str, started_utc: str, token: str) -> bool:
        """Register this holder after lock acquisition; return True if stale entries were recovered."""
        guard = self._acquire_meta_lock()
        try:
            entries, stale_recovered = self._read_raw()
            kept: list[dict] = []
            for entry in entries:
                pid = entry.get('pid')
                entry_mode = entry.get('mode')
                if not isinstance(pid, int) or not is_pid_alive(pid):
                    stale_recovered = True
                    continue
                if mode == 'exclusive':
                    # An exclusive kernel lock proves no other live process holds this lock.
                    stale_recovered = True
                    continue
                if entry_mode == 'exclusive':
                    # A shared kernel lock proves no live exclusive holder remains.
                    stale_recovered = True
                    continue
                kept.append(entry)

            kept.append({
                'pid': os.getpid(),
                'mode': mode,
                'started_utc': started_utc,
                'token': token,
            })
            self._write_raw(kept)
            return stale_recovered
        finally:
            self._release_meta_lock(guard)

    def unregister(self, token: str) -> None:
        """Remove only this acquisition's entry, preserving other live shared holders."""
        guard = self._acquire_meta_lock()
        try:
            entries, _ = self._read_raw()
            kept = [entry for entry in entries if entry.get('token') != token]
            self._write_raw(kept)
        except OSError:
            pass
        finally:
            self._release_meta_lock(guard)


def _proc_holders(lock_path: Path) -> list[int]:
    """Best-effort Linux /proc fd holder lookup for interoperability with shell flock users."""
    proc_root = Path('/proc')
    if os.name == 'nt' or not (proc_root / 'self/fd').is_dir():
        return []
    try:
        target_stat = lock_path.stat()
        target_id = (target_stat.st_dev, target_stat.st_ino)
    except OSError:
        return []

    my_pid = os.getpid()
    pids: list[int] = []
    try:
        for proc_dir in proc_root.iterdir():
            if not proc_dir.name.isdigit():
                continue
            pid = int(proc_dir.name)
            if pid == my_pid:
                continue
            fd_dir = proc_dir / 'fd'
            try:
                for fd_entry in fd_dir.iterdir():
                    try:
                        st = fd_entry.stat()
                    except OSError:
                        continue
                    if (st.st_dev, st.st_ino) == target_id:
                        pids.append(pid)
                        break
            except OSError:
                continue
    except OSError:
        pass
    return sorted(set(pids))


def report_holders(lock_path: Path, store: _MetadataStore) -> None:
    """Emit holder diagnostics to standard error."""
    try:
        live, stale = store.inspect_holders()
    except LockIOError:
        live, stale = [], []

    seen_pids: set[int] = set()
    for entry in live:
        pid = entry.get('pid')
        if isinstance(pid, int):
            seen_pids.add(pid)
        mode = entry.get('mode', 'unknown')
        started = entry.get('started_utc', 'unknown')
        print(f'  held by pid {pid} ({mode}, started {started})', file=sys.stderr)

    for pid in _proc_holders(lock_path):
        if pid not in seen_pids:
            print(f'  open in pid {pid}', file=sys.stderr)

    if stale:
        stale_pids = ', '.join(str(item.get('pid', '?')) for item in stale)
        print(f'  stale holder metadata detected for dead pid(s): {stale_pids}', file=sys.stderr)


class AdvisoryLock:
    """Cross-platform shared/exclusive advisory lock with writer-preference turnstile."""

    def __init__(self, path: Path, mode: str, wait_seconds: float | None) -> None:
        self.path = path
        self.turnstile_path = Path(f'{path}.turnstile')
        self.mode = mode
        self.wait_seconds = wait_seconds
        self.lock_fd: int | None = None
        self.turn_fd: int | None = None
        self.held_slot_offset: int | None = None
        self.held_exclusive = False
        self.store = _MetadataStore(path)
        self.token = uuid.uuid4().hex
        self.started_utc = _utc_now_iso()
        self.stale_recovered = False
        self.acquired = False

    def _try_turnstile(self) -> bool:
        assert self.turn_fd is not None
        if os.name == 'nt':
            return _msvcrt_try_lock(self.turn_fd, 0, 1)
        return _flock_try(self.turn_fd, shared=False)

    def _release_turnstile(self) -> None:
        if self.turn_fd is None:
            return
        if os.name == 'nt':
            _msvcrt_unlock(self.turn_fd, 0, 1)
        else:
            _flock_unlock(self.turn_fd)
        _close_quiet(self.turn_fd)
        self.turn_fd = None

    def _try_lock(self) -> bool:
        assert self.lock_fd is not None
        if os.name != 'nt':
            return _flock_try(self.lock_fd, shared=(self.mode == 'shared'))

        if self.mode == 'exclusive':
            if not _msvcrt_try_lock(self.lock_fd, WRITER_FLAG_OFFSET, 1):
                return False
            if not _msvcrt_try_lock(self.lock_fd, SLOT_BASE, SLOT_COUNT):
                _msvcrt_unlock(self.lock_fd, WRITER_FLAG_OFFSET, 1)
                return False
            self.held_exclusive = True
            return True

        # Shared mode (while holding turnstile): fast-check writer flag, then claim one reader slot.
        if not _msvcrt_try_lock(self.lock_fd, WRITER_FLAG_OFFSET, 1):
            return False
        _msvcrt_unlock(self.lock_fd, WRITER_FLAG_OFFSET, 1)

        start = os.getpid() % SLOT_COUNT
        for idx in range(SLOT_COUNT):
            offset = SLOT_BASE + ((start + idx) % SLOT_COUNT)
            if _msvcrt_try_lock(self.lock_fd, offset, 1):
                self.held_slot_offset = offset
                return True
        return False

    def _await(self, try_fn, deadline: float | None, on_busy) -> bool:
        if try_fn():
            return True
        on_busy()
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(POLL_INTERVAL, remaining))
            else:
                time.sleep(POLL_INTERVAL)
            if try_fn():
                return True

    def acquire(self) -> bool:
        if not self.path.parent.is_dir():
            raise LockIOError(f'no directory for {self.path}')
        if self.path.is_dir():
            raise LockIOError(f'lock path is a directory: {self.path}')

        self.lock_fd = _open_rw(self.path)
        try:
            self.turn_fd = _open_rw(self.turnstile_path)
        except Exception:
            _close_quiet(self.lock_fd)
            self.lock_fd = None
            raise

        deadline = None if self.wait_seconds is None else time.monotonic() + self.wait_seconds
        noticed = False

        def notify() -> None:
            nonlocal noticed
            if noticed:
                return
            noticed = True
            if self.wait_seconds is None:
                print(f'Waiting for {self.path} ({self.mode})', file=sys.stderr)
            else:
                print(f'Waiting up to {self.wait_seconds:g}s for {self.path} ({self.mode})', file=sys.stderr)
            report_holders(self.path, self.store)

        if not self._await(self._try_turnstile, deadline, notify):
            _close_quiet(self.turn_fd)
            self.turn_fd = None
            _close_quiet(self.lock_fd)
            self.lock_fd = None
            return False

        if not self._await(self._try_lock, deadline, notify):
            self._release_turnstile()
            _close_quiet(self.lock_fd)
            self.lock_fd = None
            return False

        self._release_turnstile()
        self.acquired = True
        try:
            self.stale_recovered = self.store.register(self.mode, self.started_utc, self.token)
        except Exception:
            self.release()
            raise
        return True

    def release(self) -> None:
        if self.acquired:
            self.store.unregister(self.token)
            self.acquired = False
        if self.lock_fd is not None:
            if os.name == 'nt':
                if self.held_exclusive:
                    _msvcrt_unlock(self.lock_fd, SLOT_BASE, SLOT_COUNT)
                    _msvcrt_unlock(self.lock_fd, WRITER_FLAG_OFFSET, 1)
                    self.held_exclusive = False
                elif self.held_slot_offset is not None:
                    _msvcrt_unlock(self.lock_fd, self.held_slot_offset, 1)
                    self.held_slot_offset = None
            else:
                _flock_unlock(self.lock_fd)
            _close_quiet(self.lock_fd)
            self.lock_fd = None
        self._release_turnstile()


def _parse_wait(raw: str) -> float | None:
    if raw == 'wait':
        return None
    if not raw or raw.strip() != raw:
        raise ValueError('WAIT must be a nonnegative number or "wait"')
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError('WAIT must be a nonnegative number or "wait"') from exc
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise ValueError('WAIT must be a nonnegative number or "wait"')
    return value


def _write_receipt(receipt_path: Path, lock: AdvisoryLock) -> None:
    payload = {
        'pid': os.getpid(),
        'started_utc': lock.started_utc,
        'mode': lock.mode,
        'path': str(lock.path.resolve()),
        'acquired': lock.acquired,
        'stale_recovered': lock.stale_recovered,
    }
    try:
        receipt_path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    except OSError as exc:
        raise LockIOError(f'cannot write receipt {receipt_path}: {exc}') from exc


def parse_cli(argv: list[str]) -> tuple[argparse.Namespace, float | None, list[str] | None]:
    if '--' in argv:
        split_at = argv.index('--')
        flag_argv = argv[:split_at]
        cmd: list[str] | None = argv[split_at + 1:]
        if not cmd:
            print('host-lock: command required after --', file=sys.stderr)
            raise SystemExit(EXIT_MISUSE)
    else:
        flag_argv = argv
        cmd = None

    parser = _UsageParser(prog='host-lock', description='Cross-platform advisory file lock')
    parser.add_argument('--mode', required=True, choices=('shared', 'exclusive'))
    parser.add_argument('--path', required=True)
    parser.add_argument('--wait', default='wait')
    parser.add_argument('--receipt', default=None)
    args = parser.parse_args(flag_argv)

    if not args.path or not args.path.strip() or '\0' in args.path:
        print('host-lock: --path must be a non-empty path', file=sys.stderr)
        raise SystemExit(EXIT_MISUSE)
    if args.receipt is not None and (not args.receipt.strip() or '\0' in args.receipt):
        print('host-lock: --receipt must be a non-empty path', file=sys.stderr)
        raise SystemExit(EXIT_MISUSE)

    try:
        wait_seconds = _parse_wait(args.wait)
    except ValueError as exc:
        print(f'host-lock: {exc}', file=sys.stderr)
        raise SystemExit(EXIT_MISUSE) from exc

    return args, wait_seconds, cmd


def main(argv: list[str] | None = None) -> int:
    args, wait_seconds, cmd = parse_cli(sys.argv[1:] if argv is None else argv)
    lock_path = Path(args.path)
    receipt_path = Path(args.receipt) if args.receipt is not None else None
    lock = AdvisoryLock(lock_path, args.mode, wait_seconds)

    try:
        acquired = lock.acquire()
    except LockIOError as exc:
        print(f'host-lock: {exc}', file=sys.stderr)
        return EXIT_IO

    try:
        if not acquired:
            if receipt_path is not None:
                try:
                    _write_receipt(receipt_path, lock)
                except LockIOError as exc:
                    print(f'host-lock: {exc}', file=sys.stderr)
                    return EXIT_IO
            print(f'host lock not acquired: {lock_path}', file=sys.stderr)
            report_holders(lock_path, lock.store)
            return EXIT_TIMEOUT

        if receipt_path is not None:
            try:
                _write_receipt(receipt_path, lock)
            except LockIOError as exc:
                print(f'host-lock: {exc}', file=sys.stderr)
                return EXIT_IO

        if cmd is None:
            return EXIT_OK

        try:
            proc = subprocess.run(cmd)
        except FileNotFoundError:
            print(f'host-lock: command not found: {cmd[0]}', file=sys.stderr)
            return 127
        except PermissionError as exc:
            print(f'host-lock: cannot execute {cmd[0]}: {exc}', file=sys.stderr)
            return 126
        except OSError as exc:
            print(f'host-lock: failed to run {cmd[0]}: {exc}', file=sys.stderr)
            return EXIT_IO
        return proc.returncode
    finally:
        lock.release()


if __name__ == '__main__':
    raise SystemExit(main())
