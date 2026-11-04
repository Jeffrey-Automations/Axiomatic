"""Helpers shared by process supervisor entry points.

`restart_backoff` and `child_exit_is_fatal` are pure functions.
`acquire_single_instance_lock` touches the filesystem (it has to — an OS-held
lock is the only staleness-free way to answer "is another instance already
running?").

The landing of child-process output (`stream_child` / `trim_log` / `say` as a
set) lives here too: both launchers must tee the child's stdout+stderr to the
console and the log file at once, and there should be only one implementation.
"""
from __future__ import annotations

import errno
if __package__:
    from ._process_job import popen as _job_popen
else:
    from _process_job import popen as _job_popen
import os
import subprocess  # nosec B404 — the supervisor's whole job is to spawn children
import sys
import threading
import time


# The child uses this rc to say "I didn't break — **another instance is already
# running**".
#
# Why a dedicated rc is needed: the supervisor's restart loop originally had only
# two give-up conditions, and neither catches this class of failure. Backoff only
# slows retries down, and rapid-fail giveup looks at "how long it ran" — but a
# child blocked by the lock exits **cleanly** within a second every time, so
# retrying a hundred times will never succeed, and every round still prints a
# "restarting in Ns" line. So we need an rc that lets the supervisor tell
# outright that "retrying is pointless".
#
# Why 3: 0 is a normal exit, 1 is an uncaught exception and general failure, 2 is
# CPython's own command-line error (`python nonexistent_file.py`). 3 is the first
# value not already taken. **Do not change it to 1**: that would blur it with a
# real crash and the supervisor could no longer tell them apart.
RC_ALREADY_RUNNING = 3

# The child uses this rc to say "**configuration is not filled in yet**" — the
# credentials file or `bot_config.json` is missing / still the template as
# shipped. Same class as `RC_ALREADY_RUNNING`: retrying a hundred times will
# never succeed, and every round reprints the same complaint. A brand-new clone
# always takes this path on its first launch, so it must be a legible sentence
# plus a clean exit, not a traceback plus endless respawning.
#
# **This one deliberately spans two rc contracts** (the bot's and the
# webrunner's), so it takes 5, which neither side has used yet: the bot uses
# 0–3, the webrunner uses 0–4. Both sides ask the same question ("this machine
# has no usable configuration yet") and have the same answer (stop and wait for a
# human), so there is no reason to give it two different numbers.
RC_SETUP_INCOMPLETE = 5

# ---- webrunner-specific rc contract (unrelated to the bot's rc above) --------
# Both supervisors (`start_webrunner.py` and the bot's `_watch_for_fallback`) use
# the values here to decide "should we respawn", kept in one place to stop the
# two from drifting.
#
# 3 = **zero output**: this round attempted generation but saved not a single
#     image (both the full-round zero-save backstop and the mid-round give-up
#     from "consecutive failures reaching the abort threshold" return this).
#     Respawning **once** is right — what broke is this session. But several
#     rounds in a row with zero output means respawning cannot fix it; stop.
#     Note that this value being 3 like `RC_ALREADY_RUNNING` is a coincidence and
#     they do not affect each other: that one is what the bot body says to the
#     bot launcher, this one is what the webrunner says to the webrunner
#     supervisor, and the two lines never cross. **Do not** use
#     `child_exit_is_fatal` to judge the webrunner's rc.
# 4 = **blocked**: the site pops up purchase / plan information, and retrying
#     will get nowhere. Respawning even once is too much.
RC_ZERO_PROGRESS = 3
RC_GENERATION_BLOCKED = 4
RC_SERVED_ONESHOT = 6


def webrunner_exit_needs_human(rc: int) -> bool:
    """After the webrunner exits: True = do not respawn, just stop and wait for
    a human.

    The criterion is the same as `child_exit_is_fatal` — "does retrying have any
    chance of succeeding" — but the subject differs (webrunner child vs bot
    child), so these are deliberately two functions.
    """
    return rc in (RC_GENERATION_BLOCKED, RC_SETUP_INCOMPLETE)


def child_exit_is_fatal(rc: int) -> bool:
    """After the child exits: True = the supervisor should not retry, just wrap
    up.

    Two cases: "another instance is already running" and "configuration is not
    filled in yet". The criterion is **whether retrying has any chance of
    succeeding**, not "how severe the error is": expired credentials or a wrong
    config value also will not succeed on retry, but their rc is indistinguishable
    from a real crash and can only be caught by rapid-fail giveup. The
    missing-file case is distinguishable, so it has its own rc.
    """
    return rc in (RC_ALREADY_RUNNING, RC_SETUP_INCOMPLETE)


def restart_backoff(
    current: float,
    *,
    minimum: float,
    maximum: float,
    healthy: bool,
) -> tuple[float, float]:
    """Return ``(wait_now, next_backoff)`` for a failed child process.

    A healthy run resets immediately. A short-lived failure waits for the
    current delay and only then increases the delay for the following retry.
    """
    if minimum <= 0 or maximum < minimum:
        raise ValueError("backoff requires 0 < minimum <= maximum")
    wait_now = minimum if healthy else min(max(current, minimum), maximum)
    next_backoff = minimum if healthy else min(wait_now * 2, maximum)
    return wait_now, next_backoff


class InstanceLock:
    """A held single-instance lock. The caller **should** keep it alive until
    the process ends.

    **This class deliberately has no `__del__`, and must not have one.** Measured
    (Windows 11 / CPython): `hasattr(InstanceLock, "__del__")` is False, and after
    dropping the last reference and calling `gc.collect()`, a second
    `acquire_single_instance_lock` still returns `None` — **the lock is still
    held**. Because the lock hangs off the open file description, nobody closes
    the fd so nobody releases the lock: when the object is collected the fd leaks,
    the lock stays held until the process ends, and mutual exclusion still holds.

    This is a deliberately chosen safe direction; **do not "finish off" the
    `__del__`**: leaking one extra fd hurts nobody (one per process, all reclaimed
    by the OS when the process ends), whereas silently releasing the lock lets a
    second instance start — two batch supervisors fighting over the same
    `.chrome_profile/`, each nuclear-sweeping the other's Chrome, while both logs
    look normal. `test_supervisor` guards this with two tests
    (`test_instance_lock_must_not_grow_a_del_method` pins the attribute,
    `test_dropping_the_reference_does_not_release_the_lock` pins the behaviour).

    This is **a different thing** from `acquire_single_instance_lock`'s "when it
    cannot decide, fall toward starting anyway"; do not conflate them: that one is
    about which way to fall **when the lock cannot be obtained** (toward allowing),
    this one is about what to do **after the lock has been obtained** when the
    reference disappears (keep holding it).

    `degraded=True` means "the locking mechanism itself is unavailable" (see the
    notes on `acquire_single_instance_lock`); in that case it is just an empty
    shell and guarantees no mutual exclusion.
    """

    __slots__ = ("_fd", "path", "degraded")

    def __init__(self, fd: int | None, path: str, degraded: bool = False):
        self._fd = fd
        self.path = path
        self.degraded = degraded

    def release(self) -> None:
        """Release the lock — **just a courtesy wrap-up**, not part of
        correctness.

        Whether the process exits normally, on an uncaught exception, or is cut
        down by `taskkill /F`, the OS closes the fd and releases the lock along
        with it, so failing to call this leaves no unbreakable stale lock behind.
        Deliberately made safe to call several times: the one in `finally` may
        have released already, and a `degraded` empty shell (`fd is None`) has no
        fd to close at all.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            os.close(fd)
        except OSError:
            pass


# The errno set for "this file is already locked by someone else". **Only these**
# mean "another instance is running"; any other errno means "the locking
# mechanism on this machine has a problem", which is an entirely different thing —
# blurring the two into one answer makes a concurrency-unrelated problem look
# exactly like "an instance is already running", and then the launcher refuses to
# start forever (see the notes on `acquire_single_instance_lock`).
#
# The two platforms give different answers, so **catch both** (measured locally,
# 2026-09-08):
#   * Windows `msvcrt.locking(fd, LK_NBLCK, 1)` gives **EACCES(13)** for an
#     already-locked region. The blocking `LK_LOCK` gives **EDEADLOCK(36)** when
#     its retries fail — we use the non-blocking version, so catching it is purely
#     insurance (if someone switches to the blocking version one day, at least it
#     will not be misread as "the lock broke").
#   * POSIX `flock(fd, LOCK_EX | LOCK_NB)` gives **EWOULDBLOCK/EAGAIN** for an
#     already-held file.
#
# `EWOULDBLOCK` on Linux is just `EAGAIN` (both 11), **but on Windows CPython it
# is not**: measured `errno.EAGAIN == 11` while `errno.EWOULDBLOCK == 10035`
# (Winsock's WSAEWOULDBLOCK). So you cannot write only one of them, nor assume
# the two are equal. `getattr` is used because these names are not guaranteed to
# exist on every platform; if one is missing, treat it as absent.
_LOCK_HELD_ERRNOS = frozenset(
    value for value in (
        getattr(errno, _name, None)
        for _name in ("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK", "EDEADLOCK")
    ) if value is not None
)


def acquire_single_instance_lock(path) -> InstanceLock | None:
    """Acquire an exclusive, process-lifetime lock on `path`, used to block "the
    same program being started twice". Acquired → return `InstanceLock`;
    **already held by another instance → return None**.

    **One lock file per program**: the launcher locks
    `.discord_bot_supervisor.lock`, the bot body locks `.discord_bot.lock`,
    deliberately kept separate. If they shared one file, the launcher would block
    the very bot it spawned — the one child the launcher must let through.

    An OS-level file lock is used rather than a pid file because a pid file has
    two incurable flaws: when the process is hard-killed the file lingers (every
    subsequent start refused forever), and the PID gets reclaimed and reassigned
    by the system (a lingering pid happens to match an unrelated process, again
    refusing every start forever). An OS lock releases automatically the instant
    the process disappears, even under `taskkill /F`, so there is no such thing as
    a leftover.

    **Which way to fall when it "cannot decide"**: fall toward "start anyway"
    (return a `degraded` shell), not toward "refuse to start". This is the
    opposite of CLAUDE.md's conservative direction for PID liveness probing, and
    it is deliberate — that rule guards "do not open a second Chrome stack", where
    the costs are symmetric; here the two mistakes have asymmetric costs: getting
    it wrong as "refuse" makes the bot **fail to start at all** over an unrelated
    filesystem problem with nobody noticing, while getting it wrong as "allow" at
    worst reverts to the state before this lock existed (duplicate instances).

    **Three outcomes, told apart by errno** (fixed 2026-09-08; before that there
    were only two, and any `OSError` counted as "already an instance", i.e. the
    policy above written backwards):

    | Outcome | Meaning | Return |
    |---|---|---|
    | Got the lock | Only I am running | `InstanceLock` (`degraded=False`) |
    | `_LOCK_HELD_ERRNOS` | Another instance is running | `None` |
    | Other errno / no `msvcrt`&`fcntl` / cannot open the file | **Cannot decide** | `InstanceLock(degraded=True)` |

    `degraded=True` is an honest report of "I provided no mutual-exclusion
    protection", and **the caller is responsible for saying so**: both launchers
    `say()` a line. Otherwise the allow is silent, and a silent degradation is the
    same thing as not having this lock at all.

    The child does not inherit this lock: since Python 3.4+ fds are
    non-inheritable by default (PEP 446), so the bot spawned under the launcher
    does not carry the lock away with it.
    """
    path = str(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as error:
        print(f"single-instance lock unavailable ({path}): {error!r}",
              file=sys.stderr)
        return InstanceLock(None, path, degraded=True)

    try:
        if os.name == "nt":
            import msvcrt
            # LK_NBLCK: non-blocking, locks 1 byte from the current position. If
            # already locked by someone else it raises OSError and does not hang.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _LOCK_HELD_ERRNOS:
            # Locked by another instance — this is the only path that returns None.
            try:
                os.close(fd)
            except OSError:
                pass
            return None
        # "cannot lock" is not the same as "someone holds it". Reaching here means
        # the lock call itself broke (EBADF, EINVAL, the ENOLCK a network drive
        # that does not support file locks gives, …), i.e. **cannot decide**, so
        # fall toward "start anyway" per the policy spelled out above.
        #
        # **This line deliberately changed a safety mechanism's failure
        # direction** (2026-09-08): before the change any `OSError` returned
        # `None`, so a filesystem problem with nothing to do with concurrency got
        # reported as "another instance is already running", the launcher then
        # **refused to start forever**, and the message even pointed at an instance
        # that did not exist — nobody could trace it. After the change an unknown
        # errno lets it through, at the cost of possible duplicate instances. The
        # latter is chosen because this function's policy already says to fall
        # toward "start anyway"; this just makes the code match it. And the allow
        # is **no longer silent**: `degraded` is printed by both launchers, and the
        # errno is left on stderr too.
        print(f"single-instance lock check failed (errno={error.errno}): "
              f"{error!r}; starting anyway, but without mutual exclusion this time",
              file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)
    except ImportError as error:
        # The platform has no msvcrt/fcntl (extremely rare). Fall toward "start
        # anyway" as well.
        print(f"single-instance lock unsupported: {error!r}", file=sys.stderr)
        return InstanceLock(fd, path, degraded=True)

    return InstanceLock(fd, path)


def other_launcher_pids(script_name: str, *, self_pid: int | None = None,
                        procs=None, also_contains: str | None = None
                        ) -> list[int]:
    """The pids of **other** live processes running the `script_name` launcher.

    Used only as diagnostics for the "another instance is already running"
    message — **never part of a decision** (deciding is the single-instance lock's
    job), so when psutil is absent or unhappy, returning an empty list is fine and
    must not block startup.

    Lifted up from `start_discord_bot.py` on 2026-09-03 to be shared by both
    launchers. When lifted, the hardcoded `Path(__file__).name` became a
    parameter — placed here, `__file__` would resolve to `_supervisor.py` and
    never find any launcher.

    `procs` is injectable (an iterable of `(pid, name, cmdline, ppid)`), so this
    logic is testable without actually opening two launchers.

    `also_contains` additionally requires this argument to be on the command line.
    The bot's launcher is now **one process per platform** and shares one script
    filename, so matching on filename alone would let the discord one answer "yes"
    to "is the telegram one running". Passing `also_contains="telegram"` counts
    only that platform. **This is still only diagnostics**: a default platform
    started manually with `py -3 start_discord_bot.py` carries no `--platform`, so
    it is invisible here — real mutual exclusion is always the single-instance
    lock's job.
    """
    from axiomatic._process_control import (  # deferred import: avoids a startup dependency cycle
        cmdline_runs_script,
        collapse_interpreter_stub_pairs,
        looks_like_python_process,
    )

    me = os.getpid() if self_pid is None else self_pid
    raw: list[tuple[int, int, str]] = []
    if procs is None:
        try:
            import psutil  # type: ignore
        except ImportError:
            return []

        def _iter():
            # Filter first with the cheap `name`, then read `cmdline()` / `ppid()`
            # only for Python processes. On Windows psutil rebuilds the whole
            # machine's mapping table every time you fetch ppid, so writing
            # `attrs=[..., "ppid"]` is O(N²) (measured 3.7 seconds, and this is a
            # path that runs on every startup).
            for proc in psutil.process_iter(attrs=["pid", "name"]):
                try:
                    yield (proc.info.get("pid"), proc.info.get("name"),
                           proc.cmdline(), proc.ppid() or 0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        procs = _iter()

    try:
        for pid, name, cmdline, ppid in procs:
            if not looks_like_python_process(name):
                continue
            # The condition is tightened to "the argument is this script's path":
            # substring matching would count any command line that **mentions**
            # the filename (a shell, `python -c`) as "another instance".
            try:
                hit = cmdline_runs_script(cmdline, (script_name,))
            except Exception:  # pylint: disable=broad-except
                continue
            if hit and also_contains is not None:
                # Match a whole argument verbatim, not a substring: `--platform
                # telegram` is split into two argv elements, whereas a substring
                # match would let a name like `telegram2` match by mistake.
                hit = any(str(arg) == also_contains for arg in (cmdline or ()))
            if hit:
                # **Keep** our own entry and let collapse exclude it — it needs to
                # see our own ppid to drop the "our own interpreter stub" half.
                raw.append((pid, ppid, "launcher"))
    except Exception:  # pylint: disable=broad-except
        pass
    # When the launcher runs via `.venv\Scripts\python.exe`, the scan hits two
    # entries, "stub + real" (identical cmdline, parent/child relationship). If
    # not collapsed, one existing instance gets reported as two pids and the
    # reader thinks they really opened two copies.
    return [pid for pid, _script in
            collapse_interpreter_stub_pairs(raw, exclude_pid=me)]


# ---- Landing child-process output (shared by both launchers) ----------------
#
# On 2026-08-23 this was already hit and fixed on the webrunner side: the
# launcher was `subprocess.run(cmd)`, the child inherited the console directly,
# and **once the window was closed that line was gone forever**. That fix was
# applied only to `start_webrunner.py`; `start_discord_bot.py` kept the same
# pattern untouched until 2026-08-30.
#
# The bot side is actually worse, because the whole design of Secrecy Layer 1
# rests on "send a generic message to Discord, write full detail to the log" —
# the bot even replies "please check the log". With no landed file, that sentence
# points at something that does not exist, and every `print(..., file=sys.stderr)`
# diagnostic (including background-task crashes, raw exception text, and the
# supervisor's give-up reason) lives only in some console nobody is watching.
#
# So the implementation lives here in one copy, used by both launchers. **Do
# not** write it again separately in each launcher.

_LOG_PUMP_JOIN_SEC = 5.0


def trim_log(path, *, max_bytes: int, keep_bytes: int) -> None:
    """When `path` exceeds `max_bytes`, keep only the last `keep_bytes` (cut on a
    whole-line boundary).

    Best-effort: any I/O failure just means no trim; never let the supervisor die
    over a log file. The tail is kept rather than the whole thing cleared — the
    seam that blows up is exactly "crash → respawn", and clearing it throws away
    the very thing you need to investigate.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    try:
        with path.open("rb") as handle:
            handle.seek(size - keep_bytes)
            handle.readline()          # drop the half-line where the seek landed
            tail = handle.read()
        path.write_bytes(tail)
    except OSError as error:
        print(f"trim_log({path.name}) failed: {error!r}", file=sys.stderr)


def echo_line(line: str) -> None:
    """Write one line from the child back to the launcher's own console. **Under
    no circumstances may it throw an exception outward.**

    The child's output is forced to UTF-8 (see `stream_child`), but the
    **launcher**'s stdout is not necessarily a console — when redirected to a file
    it uses the system locale encoding, and hits `UnicodeEncodeError` on a
    character it cannot encode. The supervisor must not die over one log line, so
    it falls back to a replace-on-error write.

    The two `try` blocks here are **separate**, not two `except` clauses of one
    `try` (fixed 2026-09-07). Both things went wrong on their own:

    1. An exception thrown from inside an `except` block is **not** caught by
       another `except` of the same `try`. The fallback used to be a direct
       stdout write inside `except UnicodeEncodeError:` with an
       `except OSError: pass` hung below it — the latter covers the former not at
       all. Measured: the first write threw `UnicodeEncodeError`, the fallback
       threw `OSError(28)`, and that `OSError` shot straight out of this function.
    2. **Writing to a closed stream throws `ValueError`, not `OSError`.** This
       function runs on the pump thread, and `pump_stream`'s
       `except (OSError, ValueError)` wraps the whole loop — so one broken console
       equals **the entire pump stopping**, and the child then fills the pipe and
       hangs forever. Measured: 3 lines, only 1 pumped. That is worse than having
       no log at all: the supervisor is still alive, the batch is stuck, and it
       says nothing.
    """
    try:
        sys.stdout.write(line)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass                    # fall through to the replace-on-error write
    except (OSError, ValueError):
        return
    # The fallback path wraps itself again. `getattr` for `encoding`: this object
    # is not one we control (it could be any redirection wrapper), and a missing
    # attribute must not let the supervisor die over one log line either.
    # `LookupError` / `UnicodeError` cover "the stream lies about its encoding".
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(line.encode(enc, "replace").decode(enc, "replace"))
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeError, LookupError):
        pass


def log_write(log, line: str) -> None:
    """Write one line to the log file, prefixed with a timestamp (the console
    copy stays as-is, no prefix).

    The timestamp is for reconciliation after the fact — `events.ndjson`'s `ts`
    must line up with the lines here.
    """
    if log is None:
        return
    try:
        log.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}")
    except (OSError, ValueError):
        pass


def say(log, message: str, *, err: bool = False) -> None:
    """The supervisor's own messages: one copy to the console, one to the log
    file.

    Give-up reasons, rcs, and backoff seconds are exactly what after-the-fact
    diagnosis needs to see, and printing only to the console keeps nothing.
    """
    print(message, file=sys.stderr if err else sys.stdout)
    log_write(log, message.strip() + "\n")


def pump_stream(stream, log) -> None:
    """Send the child's output line by line to the console + log file, until EOF.

    **This pump loop runs on its own thread and must never stop**: as soon as
    `stdout=PIPE` is given but nobody reads, once the OS pipe buffer (about 64 KB
    on Windows) fills, the child's next print hangs forever — worse than having no
    log at all. The point of a separate thread is to keep pumping even the wrap-up
    output after Ctrl+C, so that waiting for the child to finish does not instead
    wedge it. Never raises.
    """
    if stream is None:
        return
    try:
        for line in stream:
            echo_line(line)
            log_write(log, line)
    except (OSError, ValueError):
        pass


def reap_child(proc, log, *, grace_sec: float, kill_sec: float) -> int:
    """Reap the child cleanly after Ctrl+C: first wait for it to finish on its
    own, terminate on timeout, then kill on a further timeout.

    Why you cannot just re-raise KeyboardInterrupt and walk away: that would leave
    an orphan child (and, on the webrunner side, a whole Chrome tree).
    `subprocess.run` on KeyboardInterrupt does `process.kill()`, which on Windows
    is TerminateProcess, so the child's `finally` does not run at all. Here it is
    courtesy first, force after.
    """
    try:
        return proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        say(log, "supervisor: child did not exit in time; terminating", err=True)
        proc.terminate()
    try:
        return proc.wait(timeout=kill_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def stream_child(cmd: list[str], log, *, cwd: str, on_spawn=None,
                 pump_name: str = "log-pump",
                 grace_sec: float = 30.0, kill_sec: float = 10.0) -> int:
    """跑一次子行程，把它的 stdout＋stderr 同時寫到主控台與記錄檔，回 rc。

    子行程強制 `PYTHONIOENCODING=utf-8`：管線不是主控台，CPython 會退回系統地區
    編碼（本機是 cp950），中文輸出就可能讓**子行程**自己炸掉。解碼端一併用
    `errors="replace"`，任何怪位元組都不會中斷監督。呼叫端給的 `-u` 也還是要留，
    否則子行程的輸出會在它自己的緩衝區裡積著、記錄檔變成一陣一陣的。

    **「強制」＝覆寫，不是 `setdefault`**（2026-09-12 修）。這一行原本是
    `env.setdefault(...)`，而上面這段說明從第一天就寫著「強制」——兩者差在呼叫端
    環境**已經帶著一個值**的時候誰贏。選覆寫的理由是下面 `Popen` 的解碼端是
    **寫死的** `encoding="utf-8"`：兩端只要不一致就是安靜的資料損壞，
    `errors="replace"` 保證不會有例外，於是 rc 正常、沒有紅字，只有記錄檔裡的繁中
    進度行、resume 不符的原因、放棄理由整段變成 U+FFFD。而啟動器最常見的起法
    （桌面捷徑、開機自動啟動、排程工作、別人的殼）正是「環境裡有一個我們沒設過的
    值」。本專案其餘 16 個交代子行程編碼的地方全部是覆寫
    （`{**os.environ, "PYTHONIOENCODING": "utf-8"}`），這裡曾是唯一的例外，方向還
    剛好是安靜壞掉的那一邊。

    也刻意**不**走「只有不是 UTF-8 變體才覆寫」的中間路線（放行
    `utf-8:surrogateescape` 之類）：那要多一段 codec 正規化（而 `codecs.lookup`
    自己會丟 `LookupError`），換來的只是保留呼叫端的錯誤處理器——那個處理器對我們
    這一端毫無作用，因為我們本來就 `errors="replace"`。監督者的記錄檔長什麼樣，
    不該取決於誰、從哪個殼把它點起來。

    行為測試兩半都有（`test_supervisor.py`）：環境裡沒有那個變數
    （`test_the_child_gets_utf8_io_encoding`），以及環境裡帶著一個錯的值
    （`test_a_wrong_pythonioencoding_in_the_parent_is_overridden`）。**只有後者
    分得出覆寫與 `setdefault`。** 這一站 `test_text_encoding` 的靜態掃描結構上
    看不到——`cmd` 是呼叫端給的變數，掃描器認不出這是 Python 子行程——所以這兩支
    具名測試就是它的全部覆蓋。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    spawn = _job_popen if any(str(part).endswith("webrunner_je_only.py") for part in cmd) else subprocess.Popen
    job_options = {"guarded": True} if spawn is _job_popen else {}
    proc = spawn(  # nosec B603 — cmd 由呼叫端組出來，不經 shell
        cmd, **job_options,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        if on_spawn is not None:
            on_spawn(proc)
        pump = threading.Thread(target=pump_stream, args=(proc.stdout, log),
                                name=pump_name, daemon=True)
        pump.start()
    except Exception:  # pylint: disable=broad-except
        # 子行程**已經起來了**，所以這條路不能只是往上拋（2026-09-07 修）。
        # `on_spawn` 會做真的 I/O——`start_webrunner._on_spawn` 寫 `webrunner.pid`
        # ——磁碟滿了或權限不對就丟 OSError。拋掉的話會留下一個 stdout 是 PIPE、
        # 沒有人抽、也沒有人 wait 的孤兒：它下一次 print 就卡死在滿掉的管線裡，
        # 而它手上握著整棵 Chrome。監督者自己死掉、批次還在那裡卡著不動，正是
        # 最難查的那種收場。先收屍再把例外往上送。
        #
        # `grace_sec=0`：這裡的子行程**沒有**收到 Ctrl+C，沒有理由自己收工，等
        # 寬限期只是白等——直接 terminate，剩下的 Chrome 由 webrunner 下次啟動
        # 的 `_kill_orphan_chrome()` 掃掉。
        #
        # **不要**把這兩個 handler 改成 `except BaseException`：專案規則禁止
        # （`test_exception_handlers.test_nothing_swallows_cancellation`，理由是
        # 那會連 `CancelledError`／`KeyboardInterrupt` 一起吞掉），而這裡也不需要
        # ——真正會留下孤兒的是 `on_spawn` 丟 `OSError`（pid 檔寫不進去），那是
        # `Exception`。Ctrl+C 落在這個微小視窗裡的情況本來就由**主控台群組**兜著：
        # 子行程跟啟動器在同一個群組，同一個 Ctrl+C 它自己也收到了。
        try:
            say(log, "supervisor: spawn hook failed; terminating the child "
                     "that was already started", err=True)
            reap_child(proc, log, grace_sec=0, kill_sec=kill_sec)
        except Exception:  # pylint: disable=broad-except  # nosec B110
            pass               # 收屍失敗也不能蓋掉原本那個例外
        raise
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # 同一個主控台群組，子行程也收到了 Ctrl+C。抽水執行緒還活著，所以它在
        # 收尾時大量輸出也不會把自己卡在滿掉的管線裡。
        reap_child(proc, log, grace_sec=grace_sec, kill_sec=kill_sec)
        raise
    finally:
        pump.join(timeout=_LOG_PUMP_JOIN_SEC)
        # 抽水還卡在 read 就**不要**關（2026-09-07 加的條件）。實測：
        # `BufferedReader.close()` 不會丟例外，也不會把串流從抽水手上抽走——它去
        # 搶同一把鎖，於是**一路擋到那次 read 回來為止**（量到 19.05 秒，正好是
        # 子行程還活著的時間）。
        #
        # 正常路徑走不到：`proc.wait()` 回來＝子行程已死＝管線 EOF＝抽水立刻結束。
        # 走得到的是「收屍失敗、子行程還活著」那條（`reap_child` 裡的
        # `terminate()`／`kill()` 自己丟例外）——那時候這一行會把監督者**永久**卡在
        # 收工的最後一步，而它手上還握著單一實例鎖，於是誰也重啟不了，畫面上什麼
        # 都沒有。抽水是 daemon 執行緒，行程結束時 OS 會把 fd 收掉。
        if proc.stdout is not None and not pump.is_alive():
            try:
                proc.stdout.close()
            except OSError:
                pass


def start_log_pump(stream, path):
    """把子行程管線轉存到獨立附加 handle，避免覆寫同檔的函式庫紀錄。"""
    if stream is None:
        return None

    def pump():
        try:
            with open(path, "a", encoding="utf-8", buffering=1) as log:
                for line in stream:
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    log.write(line)
        except (OSError, ValueError) as error:
            print(f"log pump failed: {type(error).__name__}", file=sys.stderr)
        finally:
            stream.close()

    thread = threading.Thread(target=pump, name="batch-log-pump", daemon=True)
    thread.start()
    return thread


def webrunner_exit_made_no_progress(rc: int) -> bool:
    """webrunner 結束後：True = 這一輪對批次佇列零推進，計入零產出放棄閘。

    兩種：`RC_ZERO_PROGRESS`（嘗試過生成、一張都沒存）與 `RC_SERVED_ONESHOT`
    （跑成了單圖伺服器，批次佇列一筆都沒碰）。兩種都**活很久**——前者要跑完
    `consecutive_fail_abort`，後者光閒置逾時就 120 秒——所以只看「活了多久」的
    rapid-fail 閘永遠不會響，要走這道慢速閘才數得到。兩支監督者都呼叫這一支，
    規則只有一份。
    """
    return rc in (RC_ZERO_PROGRESS, RC_SERVED_ONESHOT)
