# host-lock: one machine's shared resource, held shared or exclusive, with writer preference. POSIX sh (dash, bash).
#
#   . host-lock.sh
#   host_lock MODE FD PATH [WAIT]
#       MODE   shared | exclusive
#       FD     the descriptor that holds the lock when this returns 0 (opened on PATH, append, group-writable, unless it
#              is open on PATH already: a nested command of the holder inherited it and takes it again at once, never
#              waiting behind a writer that waits for its own parent). Not 7, which the turnstile uses.
#       PATH   the lock file; never unlinked
#       WAIT   seconds to wait; 0 refuses at once; "wait" or empty waits as long as it takes
#       0 held; 3 not acquired by the deadline, nothing held; 2 misused; 1 the lock file cannot be opened
#   host_lock_holders PATH
#       every process that has PATH open, by pid, start time and command line, on standard error
#
# Writer preference: flock has none, so a new shared holder is granted past a waiting exclusive one, and a stream of
# overlapping readers holds a writer off until its deadline. Every taker that may wait first passes a turnstile,
# PATH.turnstile on descriptor 7, held exclusively only while it waits for the lock itself. A waiting writer holds the
# turnstile, so a later reader queues behind it while the readers already in drain. Descriptor 7 is closed before
# host_lock returns. Every taker of the lock, in any language, must pass the turnstile the same way.

host_lock_holders() {
  [ -d /proc/self/fd ] || return 0
  _hl_seen=$(find /proc/[0-9]*/fd -maxdepth 1 -lname "$1" 2>/dev/null | cut -d/ -f3 | sort -un | grep -vx "$$")
  if [ -z "$_hl_seen" ]; then
    echo "  holder not visible to $(id -un) (another account's process): ls -l /proc/*/fd as root" >&2
    return 0
  fi
  for _hl_pid in $_hl_seen; do
    # this waiter's own children inherit the descriptor; they are not holders
    [ "$(ps -o ppid= -p "$_hl_pid" 2>/dev/null | tr -d ' ')" = "$$" ] && continue
    _hl_who=$(ps -o lstart= -o args= -p "$_hl_pid" 2>/dev/null | cut -c1-200)
    [ -n "$_hl_who" ] && echo "  open in pid $_hl_pid: $_hl_who" >&2
  done
  return 0
}

_hl_notice() {
  if [ -z "$_hl_wait" ]; then echo "Waiting for $_hl_path ($_hl_mode)" >&2
  else echo "Waiting up to ${_hl_wait}s for $_hl_path ($_hl_mode)" >&2; fi
  host_lock_holders "$_hl_path"
}
# flock on a descriptor until the deadline that began at _hl_start (none: as long as it takes)
_hl_await() {
  if [ -z "$_hl_wait" ]; then flock "$1" "$2"; return; fi
  _hl_left=$((_hl_wait - ($(date +%s) - _hl_start)))
  [ "$_hl_left" -ge 0 ] || _hl_left=0
  flock "$1" -w "$_hl_left" "$2"
}
_hl_open() {   # descriptor, path: append, group-writable
  _hl_mask=$(umask); umask 002
  if ! eval "exec $1>>\"\$2\"" 2>/dev/null; then umask "$_hl_mask"; return 1; fi
  umask "$_hl_mask"
}

host_lock() {
  _hl_mode=$1 _hl_fd=$2 _hl_path=$3 _hl_wait=${4:-}
  case $_hl_mode in shared) _hl_flag=-s ;; exclusive) _hl_flag=-x ;;
    *) echo "host_lock: MODE must be shared or exclusive, not '$_hl_mode'" >&2; return 2 ;; esac
  case $_hl_fd in ''|*[!0-9]*|7) echo "host_lock: FD must be a descriptor number other than 7" >&2; return 2 ;; esac
  [ -n "$_hl_path" ] || { echo "host_lock: PATH required" >&2; return 2; }
  [ "$_hl_wait" != wait ] || _hl_wait=
  case $_hl_wait in *[!0-9]*) echo "host_lock: WAIT must be a nonnegative integer or wait" >&2; return 2 ;; esac
  [ -d "$(dirname "$_hl_path")" ] || { echo "host_lock: no directory for $_hl_path" >&2; return 1; }
  if [ -e "/proc/self/fd/$_hl_fd" ] && [ "$(stat -L -c %d:%i "/proc/self/fd/$_hl_fd" 2>/dev/null)" = "$(stat -L -c %d:%i "$_hl_path" 2>/dev/null)" ]; then
    # a nested command of the holder: the descriptor it inherited holds the lock already
    flock "$_hl_flag" -n "$_hl_fd" && return 0
  elif ! _hl_open "$_hl_fd" "$_hl_path"; then
    echo "host_lock: cannot open $_hl_path (check ownership and group write permission)" >&2; return 1
  fi
  _hl_open 7 "$_hl_path.turnstile" || { echo "host_lock: cannot open $_hl_path.turnstile" >&2; return 1; }
  _hl_start=$(date +%s) _hl_said=
  if ! flock -x -n 7; then
    _hl_notice; _hl_said=1
    _hl_await -x 7 || { exec 7>&-; echo "host lock not acquired: $_hl_path" >&2; host_lock_holders "$_hl_path"; return 3; }
  fi
  if ! flock "$_hl_flag" -n "$_hl_fd"; then
    [ -n "$_hl_said" ] || _hl_notice
    _hl_await "$_hl_flag" "$_hl_fd" || { exec 7>&-; echo "host lock not acquired: $_hl_path" >&2; host_lock_holders "$_hl_path"; return 3; }
  fi
  exec 7>&-
  return 0
}
