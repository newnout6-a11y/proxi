"""A stopped relay does not wait for a web search or a balance check that is still under way.

The default executor (asyncio.to_thread, run_in_executor(None)) and a ThreadPoolExecutor are joined at interpreter exit: a
search or a balance call under way kept the process alive for its 10-20 s of network timeouts after Ctrl+C."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import io_workers

ROOT = Path(__file__).resolve().parents[1]


def test_a_blocking_call_off_the_loop_does_not_hold_the_exit_of_the_process(tmp_path):
    script = tmp_path / "stopping_relay.py"
    script.write_text(
        "import asyncio, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import io_workers, orchestrator\n"
        "loop = asyncio.new_event_loop()\n"
        "loop._relay_io_executor = io_workers.DaemonIOExecutor(2)\n"
        "async def main():\n"
        "    loop.create_task(orchestrator._off_loop(time.sleep, 60))  # a search that takes its time\n"
        "    await asyncio.sleep(0.2)\n"
        "loop.run_until_complete(main())\n"
        "print('stopped')\n" % str(ROOT), encoding="utf-8")
    started = time.monotonic()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and "stopped" in done.stdout, done.stderr
    assert time.monotonic() - started < 15


def test_the_account_checks_run_on_daemon_workers():
    """The dispatcher's renewal and balance jobs ran on a ThreadPoolExecutor, which the interpreter joins at exit."""
    from test_account_dispatcher import Account, dispatcher
    value, _clock = dispatcher([Account("a")])
    value.start()
    try:
        seen, daemon = threading.Event(), []
        value._executor.submit(lambda: (daemon.append(threading.current_thread().daemon), seen.set()))
        assert seen.wait(5) and daemon == [True]
        assert isinstance(value._executor, io_workers.DaemonIOExecutor)
    finally:
        value.stop()
