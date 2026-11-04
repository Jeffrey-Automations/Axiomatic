"""只起自己的行程，驗證啟動閘門、失去父行程的子孫與巢狀工作物件。"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

import _process_control as control
import _process_job as jobs


@pytest.fixture
def real_jobs(monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows job objects")
    monkeypatch.setattr(jobs, "_new_job", jobs.ProcessJob)


async def _wait_file(path):
    for _ in range(1000):
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        await asyncio.sleep(0.01)
    raise AssertionError("child never became ready")


@pytest.mark.parametrize("nested", [False, True])
def test_job_stops_orphans_without_a_snapshot_and_keeps_neighbours(real_jobs, tmp_path, monkeypatch, nested):
    async def scenario():
        marker = tmp_path / "child.pid"
        script = tmp_path / "backend.py"
        package = str(Path(jobs.__file__).parent)
        orphan = ("import os,time; from pathlib import Path; "
                  f"Path({str(marker)!r}).write_text(str(os.getpid()),encoding='utf-8'); time.sleep(90)")
        middle = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{orphan!r}])"
        script.write_text(
            "import subprocess,sys,time\n"
            + (f"sys.path.insert(0,{package!r})\nfrom _process_job import ProcessJob\n"
               "inner = ProcessJob()\ninner.assign(__import__('os').getpid())\n" if nested else "")
            + f"middle=subprocess.Popen([sys.executable,'-c',{middle!r}])\n"
            "middle.wait()\ntime.sleep(90)\n", encoding="utf-8")
        proc = await jobs.create_subprocess_exec(
            sys.executable, str(script), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        neighbour = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(90)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        try:
            orphan_pid = int(await _wait_file(marker))
            orphan_proc = psutil.Process(orphan_pid)
            monkeypatch.setattr(control, "child_processes_started_by",
                                lambda proc: pytest.fail("a job must not need a snapshot"))
            control.kill_child_process_tree(proc)
            await asyncio.wait_for(proc.wait(), 5)
            for _ in range(500):
                if not orphan_proc.is_running():
                    break
                await asyncio.sleep(0.01)
            assert not orphan_proc.is_running()
            assert neighbour.poll() is None
        finally:
            jobs.close_process_job(proc)
            neighbour.kill()
            neighbour.wait(timeout=5)
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_a_backend_cannot_run_before_the_job_is_assigned(real_jobs, monkeypatch, tmp_path):
    marker = tmp_path / "started"
    class CheckedJob(jobs.ProcessJob):
        def assign(self, pid):
            time.sleep(0.2)
            assert not marker.exists()
            super().assign(pid)
    monkeypatch.setattr(jobs, "_new_job", CheckedJob)
    async def scenario():
        proc = await jobs.create_subprocess_exec(
            sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).write_text('ready',encoding='utf-8')",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert await asyncio.wait_for(proc.wait(), 10) == 0
        assert marker.read_text(encoding="utf-8") == "ready"
    asyncio.run(scenario())


def test_job_relay_preserves_stdin_stdout_and_exit_code(real_jobs):
    async def scenario():
        proc = await jobs.create_subprocess_exec(
            sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(7)",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        stdout, stderr = await asyncio.wait_for(proc.communicate("完整訊息".encode("utf-8")), 10)
        assert stdout.decode("utf-8").strip() == "完整訊息"
        assert stderr == b"" and proc.returncode == 7
    asyncio.run(scenario())


def test_synchronous_job_lifetime(real_jobs):
    proc = jobs.popen([sys.executable, "-c", "import time;time.sleep(90)"],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert jobs.close_process_job(proc)
        proc.wait(timeout=5)
        assert not psutil.pid_exists(proc.pid)
        assert not jobs.close_process_job(proc)
    finally:
        jobs.close_process_job(proc)


def test_assignment_failure_never_releases_the_backend(real_jobs, monkeypatch, tmp_path):
    marker = tmp_path / "must_not_start"
    class RefusedJob(jobs.ProcessJob):
        def assign(self, pid):
            raise PermissionError("assignment refused")
    monkeypatch.setattr(jobs, "_new_job", RefusedJob)
    async def scenario():
        with pytest.raises(PermissionError):
            await jobs.create_subprocess_exec(
                sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()",
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert not marker.exists()
    asyncio.run(scenario())


def test_batch_stop_does_not_scan_unrelated_browsers(monkeypatch):
    monkeypatch.setattr(control, "_find_all_webrunner_pids", lambda: ([], True))
    def unrelated():
        raise AssertionError("must not enumerate unrelated browsers")
    monkeypatch.setattr(control, "_find_all_chrome_processes", unrelated)
    assert asyncio.run(control._terminate_all_webrunner_instances(None, None)) == []


def test_cooperating_gate_keeps_script_identity(real_jobs, tmp_path):
    script = tmp_path / "cooperating.py"
    script.write_text(
        f"import sys;sys.path.insert(0,{str(Path(jobs.__file__).parent)!r})\n"
        "from _process_job import wait_start_gate\n"
        "assert wait_start_gate()\n"
        "import os\nprint(os.getpid(),flush=True)\n", encoding="utf-8")
    proc = jobs.popen([sys.executable, str(script)], guarded=True,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate(timeout=10)
    assert proc.returncode == 0 and not stderr
    # 虛擬環境的轉接殼與實際直譯器會有不同 PID，但 argv 不得改成轉接腳本。
    assert int(stdout.strip()) > 0
    assert proc.args == [sys.executable, str(script)]


def test_backend_can_start_when_its_parent_is_already_in_a_job(real_jobs, tmp_path):
    script = tmp_path / "nested_parent.py"
    script.write_text(
        f"import sys;sys.path.insert(0,{str(Path(jobs.__file__).parent)!r})\n"
        "import asyncio\nfrom _process_job import create_subprocess_exec\n"
        "async def main():\n"
        " p=await create_subprocess_exec(sys.executable,'-c','print(42)')\n"
        " assert await p.wait()==0\n"
        "asyncio.run(main())\n", encoding="utf-8")
    proc = jobs.popen([sys.executable, str(script)],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate(timeout=15)
    assert proc.returncode == 0 and not stderr
    assert stdout.strip() == b"42"


@pytest.mark.parametrize("keep_alive", [False, True])
def test_parent_exit_cleans_backends_but_preserves_an_independent_batch(real_jobs, tmp_path, keep_alive):
    marker = tmp_path / "ready.json"
    package = str(Path(jobs.__file__).parent)
    interpreter = sys._base_executable
    child = tmp_path / "child.py"
    child.write_text(
        f"import sys;sys.path.insert(0,{package!r})\n"
        "from _process_job import wait_start_gate\nassert wait_start_gate()\n"
        "import subprocess,time,os,json\nfrom pathlib import Path\n"
        f"p=subprocess.Popen([{interpreter!r},'-c','import time;time.sleep(90)'])\n"
        f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),p.pid]),encoding='utf-8')\n"
        "time.sleep(90)\n", encoding="utf-8")
    holder = tmp_path / "holder.py"
    holder.write_text(
        f"import sys;sys.path.insert(0,{package!r})\n"
        "from _process_job import popen\nimport time\n"
        f"p=popen([{interpreter!r},{str(child)!r}],guarded=True,keep_alive={keep_alive!r})\n"
        "time.sleep(90)\n", encoding="utf-8")
    proc = subprocess.Popen([interpreter, str(holder)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pids = []
    try:
        import json
        for _ in range(1000):
            if marker.exists():
                try:
                    pids = json.loads(marker.read_text(encoding="utf-8"))
                    break
                except ValueError:
                    pass
            time.sleep(.01)
        assert len(pids) == 2
        proc.kill()
        proc.wait(timeout=5)
        if keep_alive:
            assert all(psutil.pid_exists(pid) for pid in pids)
            psutil.Process(pids[0]).kill()
        for _ in range(500):
            if not any(psutil.pid_exists(pid) for pid in pids):
                break
            time.sleep(.01)
        assert not any(psutil.pid_exists(pid) for pid in pids)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for pid in pids:
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
