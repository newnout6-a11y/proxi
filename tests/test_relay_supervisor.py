"""scripts/run_relay.ps1 restarted a relay that could not start every 2-3 s, forever and hidden.

A second supervisor (the README's start line run again) or any program holding the port made each start fail on
WinError 10048, after the relay had already written its start into the shared journal and scanned the stored
conversations; each attempt also added a traceback to relay.log. The supervisor now refuses to start a relay while
the port is listening, waits twice as long after each exit within 30 s of the start, and stops after a number of
them in a row.

The supervisor's functions run here alone: they are loaded from the script's syntax tree, so no line of the script
itself runs, and a .cmd stub stands in for python. No relay is ever started.
"""
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_relay.ps1"

pytestmark = pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell.exe"),
                                reason="needs Windows PowerShell")

# Defines the functions of run_relay.ps1 without running the script, and records the waits instead of sleeping.
# Output is UTF-8, as the script itself sets it before it starts the relay.
LOADER = r"""
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tree = [System.Management.Automation.Language.Parser]::ParseFile($env:RUN_RELAY_SCRIPT, [ref]$null, [ref]$null)
$isFunction = { $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] }
foreach ($function in $tree.FindAll($isFunction, $false)) {
    . ([ScriptBlock]::Create($function.Extent.Text))
}
$script:delays = @()
function Start-Sleep([int]$Seconds) {
    $script:delays += $Seconds
    if ($script:delays.Count -ge 20) { throw "still restarting after 20 waits" }
}
"""

# Stands in for python: counts its starts, prints like a relay that cannot bind and exits with 1.
STUB = ('@echo off\r\necho started>>"%~dp0starts.txt"\r\necho relay could not bind\r\n'
        '>&2 echo OSError: [WinError 10048]\r\nexit /b 1\r\n')


def powershell(tmp_path, scenario, **env):
    harness = tmp_path / "harness.ps1"
    harness.write_text(LOADER + scenario, encoding="utf-8-sig")
    environ = {key: value for key, value in os.environ.items() if key != "RELAY_PORT"}
    environ.update(RUN_RELAY_SCRIPT=str(SCRIPT), **env)
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(harness)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=environ, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def supervise(tmp_path, *, port=None, config=None, python=None, fast_exit_seconds=30, max_fast_exits=4, **env):
    """Run Invoke-Supervisor with the stub; returns its result, the waits, the stub's starts and the log."""
    stub = tmp_path / "python.cmd"
    stub.write_text(STUB, encoding="ascii")
    config_path = tmp_path / "config.json"
    if config is not None:
        config_path.write_text(config, encoding="utf-8")
    elif port is not None:
        config_path.write_text(json.dumps({"port": port}), encoding="utf-8")
    log = tmp_path / "relay.log"
    result = powershell(tmp_path, """
try {
    $code = Invoke-Supervisor -Python $env:STUB -RelayArgs @('-u', 'proxy.py') -ConfigPath $env:CONFIG `
        -LogPath $env:LOG -FastExitSeconds %d -MaxFastExits %d 3>$null
} catch {
    $code = "thrown: $_"
}
@{ code = $code; delays = @($script:delays) } | ConvertTo-Json -Compress
""" % (fast_exit_seconds, max_fast_exits), STUB=python or str(stub), CONFIG=str(config_path), LOG=str(log), **env)
    starts = (tmp_path / "starts.txt").read_text().split() if (tmp_path / "starts.txt").exists() else []
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result["code"], result["delays"], len(starts), lines


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def listening():
    """A port this test listens on itself: never the relay's."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    yield server.getsockname()[1]
    server.close()


def test_a_relay_that_keeps_failing_to_start_is_restarted_with_growing_waits_and_then_given_up(tmp_path):
    code, delays, starts, lines = supervise(tmp_path, port=free_port(), max_fast_exits=4)
    assert code == 1 and starts == 4 and delays == [2, 4, 8]
    assert lines.count("relay could not bind") == 4 and lines.count("OSError: [WinError 10048]") == 4
    assert [line.split(" (")[0] for line in lines if line.startswith("restart")] == [
        "restart in 2 s", "restart in 4 s", "restart in 8 s"]
    assert all("(exit code 1)" in line for line in lines if line.startswith("restart"))
    assert lines[-1].startswith("supervisor: the relay exited within 30 s of its start 4 times in a row "
                                "(exit code 1); the supervisor stops")


def test_the_wait_doubles_from_two_seconds_up_to_five_minutes(tmp_path):
    result = powershell(tmp_path, "@{ delays = @(0..11 | ForEach-Object { Get-RestartDelay $_ }) } | "
                                  "ConvertTo-Json -Compress")
    assert result["delays"] == [2, 2, 4, 8, 16, 32, 64, 128, 256, 300, 300, 300]


def test_a_relay_that_ran_a_while_is_restarted_after_two_seconds_as_before(tmp_path):
    # With no exit counted as fast, the supervisor never gives up; the loader's Start-Sleep ends the loop.
    code, delays, starts, _ = supervise(tmp_path, port=free_port(), fast_exit_seconds=0, max_fast_exits=2)
    assert code == "thrown: still restarting after 20 waits" and delays == [2] * 20 and starts == 20


@pytest.mark.parametrize("where", ["config", "RELAY_PORT"])
def test_no_relay_is_started_while_its_port_is_listening(tmp_path, listening, where):
    if where == "config":
        code, delays, starts, lines = supervise(tmp_path, port=listening)
    else:
        code, delays, starts, lines = supervise(tmp_path, port=free_port(), RELAY_PORT=str(listening))
    assert code == 1 and starts == 0 and delays == []
    assert len(lines) == 1 and lines[0].startswith(
        "supervisor: port %d is already in use (another relay, a second supervisor or another program listens on "
        "it); the relay was not started and the supervisor stops" % listening)


def test_the_port_check_reads_the_listener_table_and_the_relay_port(tmp_path, listening):
    (tmp_path / "set.json").write_text('{"port": 28787, "host": "127.0.0.1"}', encoding="utf-8")
    (tmp_path / "unset.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    result = powershell(tmp_path, """
$folder = Split-Path -Parent $env:RUN_RELAY_HARNESS
$ports = @('set.json', 'unset.json', 'broken.json', 'missing.json') | ForEach-Object { Get-RelayPort (Join-Path $folder $_) }
$env:RELAY_PORT = '1234'
$override = Get-RelayPort (Join-Path $folder 'set.json')
@{ ports = @($ports); override = $override; listening = (Test-PortListening %d); free = (Test-PortListening %d) } |
    ConvertTo-Json -Compress
""" % (listening, free_port()), RUN_RELAY_HARNESS=str(tmp_path / "harness.ps1"))
    # A config that cannot be read gives no port: the relay itself then reports what is wrong with it.
    assert result == {"ports": [28787, 8787, None, None], "override": 1234, "listening": True, "free": False}


def test_a_relay_whose_config_cannot_be_read_is_still_started_to_report_it(tmp_path):
    code, _, starts, _ = supervise(tmp_path, config="{", max_fast_exits=1)
    assert code == 1 and starts == 1


def test_a_python_that_cannot_be_started_counts_as_a_failed_start(tmp_path):
    code, delays, starts, lines = supervise(tmp_path, port=free_port(), python=str(tmp_path / "missing-python.exe"),
                                            max_fast_exits=2)
    assert code == 1 and delays == [2] and starts == 0
    assert sum(line.startswith("supervisor: cannot start the relay with ") for line in lines) == 2
    assert lines[-1].startswith("supervisor: the relay exited within 30 s of its start 2 times in a row")


# relay.log took every console line of the relay, forever and across restarts, while the journal of the same events
# is capped: tens of MB a day under %LOCALAPPDATA%\Proxi\state, never removed.
MIB = 1024 * 1024


def test_relay_log_starts_again_past_twenty_mib_while_a_viewer_holds_it_open(tmp_path):
    log = tmp_path / "relay.log"
    log.write_bytes(b"old line\r\n" * (2 * MIB))
    # watch_relay.py keeps the log open: on Windows that forbids renaming it, so it is copied and emptied in place
    # (the viewer reads a file that shrank from its start again).
    with open(log, encoding="utf-8") as viewer:
        assert viewer.readline() == "old line\n"
        powershell(tmp_path, "Write-RelayLog $env:LOG 'new line'; '{}'", LOG=str(log))
    assert log.read_bytes() == b"new line\r\n"
    assert (tmp_path / "relay.log.1").stat().st_size == 20 * MIB
    assert sorted(path.name for path in tmp_path.glob("relay.log*")) == ["relay.log", "relay.log.1"]


def test_only_the_newest_old_relay_logs_are_kept(tmp_path):
    log = tmp_path / "relay.log"
    powershell(tmp_path, "1..12 | ForEach-Object { Write-RelayLog $env:LOG (('line {0:d2} ' -f $_) + ('x' * 20)) "
                         "-MaxBytes 64 -Keep 2 }; '{}'", LOG=str(log))
    files = {path.name: path.read_text(encoding="utf-8").split() for path in tmp_path.glob("relay.log*")}
    numbers = {name: [int(word) for word in words if word.isdigit()] for name, words in files.items()}
    assert numbers == {"relay.log.2": [4, 5, 6], "relay.log.1": [7, 8, 9], "relay.log": [10, 11, 12]}
