"""Windows 子行程工作物件：啟動閘門先入組，真正的指令才開始。

關閉最後一個 handle 會帶走整組（含已失去中間父行程的子孫）。不開放 breakaway，
不枚舉全機行程。Windows 8 起支援巢狀工作物件：外層監督者與後端自己的組可並存。
https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs

閘門是只等待具名事件的輕量子行程；父行程完成指派之後才設事件。父行程若在指派
之前離開，閘門最多等待 15 秒後自行結束，絕不起真正的指令。不借用 stdin 做握手。
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
from ctypes import wintypes
import functools
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
import weakref


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                ("max_working", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IOCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


@functools.lru_cache(maxsize=1)
def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenEventW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


class ProcessJob:
    def __init__(self):
        self.handle = None
        self.event = None
        kernel = _kernel()
        self.name = "Local\\process-group-" + uuid.uuid4().hex
        self.handle = kernel.CreateJobObjectW(None, self.name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.event_name = "Local\\process-start-" + uuid.uuid4().hex
        self.event = kernel.CreateEventW(None, True, False, self.event_name)
        if not self.event:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, pid: int):
        kernel = _kernel()
        process = kernel.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(process)

    def start(self):
        if not _kernel().SetEvent(self.event):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        kernel = _kernel()
        for attribute in ("handle", "event"):
            handle = getattr(self, attribute, None)
            if handle:
                setattr(self, attribute, None)
                kernel.CloseHandle(handle)

    def __del__(self):
        with contextlib.suppress(OSError, AttributeError):
            self.close()


def _new_job():
    return ProcessJob() if os.name == "nt" else None


_jobs = weakref.WeakKeyDictionary()
_monitors = set()


def close_process_job(proc) -> bool:
    """只關掉這個行程自己擁有的組；回 False 表示它不是由本模組啟動的。"""
    if proc is None:
        return False
    try:
        job = _jobs.pop(proc, None)
    except TypeError:  # 不接受弱參照的測試替身不會有本模組建立的組
        return False
    if job is None:
        return False
    try:
        # 批次自己也持有 handle，得明確終止；單純失去 bot 的 handle 則保持批次存活。
        if job.handle and not _kernel().TerminateJobObject(job.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        job.close()
    return True


def _relay_argv(argv, job):
    return [sys.executable, "-u", str(Path(__file__).resolve()), "--relay", job.event_name, *argv]


async def create_subprocess_exec(*argv, **kwargs):
    job = _new_job()
    if job is None:
        return await asyncio.create_subprocess_exec(*argv, **kwargs)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*_relay_argv(argv, job), **kwargs)
        job.assign(proc.pid)
        _jobs[proc] = job
        job.start()

        async def monitor():
            try:
                # asyncio.wait 等管線 EOF；根已結束、子孫還握著管線時不能等它。
                while proc.returncode is None:
                    await asyncio.sleep(0.05)
            finally:
                close_process_job(proc)

        watcher = asyncio.create_task(monitor())
        _monitors.add(watcher)
        watcher.add_done_callback(_monitors.discard)
        return proc
    except BaseException:
        job.close()
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise


def popen(argv, *, guarded: bool = False, keep_alive: bool = False, **kwargs):
    job = _new_job()
    if job is None:
        return subprocess.Popen(argv, **kwargs)
    proc = None
    try:
        if guarded:
            kwargs["env"] = {**(kwargs.get("env") or os.environ), "_PROCESS_START_GATE": job.event_name}
            if keep_alive:
                kwargs["env"]["_PROCESS_KEEP_JOB"] = job.name
            command = argv
        else:
            command = _relay_argv(argv, job)
        proc = subprocess.Popen(command, **kwargs)
        job.assign(proc.pid)
        _jobs[proc] = job
        job.start()

        def monitor():
            try:
                proc.wait()
            finally:
                close_process_job(proc)

        threading.Thread(target=monitor, name="process-job-reaper", daemon=True).start()
        return proc
    except BaseException:
        job.close()
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise


def _wait_for_gate(event_name):
    kernel = _kernel()
    event = kernel.OpenEventW(0x100000, False, event_name)
    if not event:
        return False
    try:
        return kernel.WaitForSingleObject(event, 15000) == 0
    finally:
        kernel.CloseHandle(event)


_owned_jobs = []  # 僅批次持有到行程結束；不繼承給瀏覽器子孫。


def wait_start_gate() -> bool:
    """合作式腳本的第一步；完成指派前不得載入會啟動子行程的程式碼。"""
    name = os.environ.pop("_PROCESS_START_GATE", None)
    if name is None:
        return False
    keep = os.environ.pop("_PROCESS_KEEP_JOB", None)
    if keep:
        handle = _kernel().OpenJobObjectW(0x0004, False, keep)  # JOB_OBJECT_QUERY
        if not handle:
            raise SystemExit(125)
        _owned_jobs.append(handle)
    if not _wait_for_gate(name):
        raise SystemExit(125)
    return True


def _relay_main(event_name, argv):
    if not _wait_for_gate(event_name):
        return 125
    child = subprocess.Popen(argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    return child.wait()


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--relay":
        raise SystemExit(2)
    raise SystemExit(_relay_main(sys.argv[2], sys.argv[3:]))
