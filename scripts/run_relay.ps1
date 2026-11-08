# Keeps the relay alive independent of the job runner.
param(
    [string]$Config,
    [string]$StateDir,
    [string]$Python = "python",
    # A relay that exits sooner than this after its start did not get going (its port is taken, its config is
    # broken): each such exit in a row doubles the wait before the next start, and $MaxFastExits of them stop
    # the supervisor instead of restarting a relay that cannot start.
    [int]$FastExitSeconds = 30,
    [int]$MaxFastExits = 8
)

function Format-RelayLogLine([string]$Line) {
    # Python supplies its event time; supervisor messages (or startup errors before Python logging) need one too.
    if ($Line -match '^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2}\] ') { return $Line }
    return ('[{0}] {1}' -f [DateTimeOffset]::Now.ToString('yyyy-MM-dd HH:mm:ss.fff zzz'), $Line)
}

function Write-RelayLog([string]$Path, [string]$Line, [long]$MaxBytes = 20MB, [int]$Keep = 3) {
    # Bounded like the journal: past $MaxBytes the log becomes relay.log.1 (relay.log.2 .. $Keep are older ones) and
    # starts again. It is copied and emptied in place rather than renamed: watch_relay.py holds the file open, which
    # on Windows forbids a rename, and it reads a file that shrank from its start again.
    try {
        $file = [IO.FileInfo]::new($Path)
        if ($file.Exists -and $file.Length -ge $MaxBytes) {
            if ($Keep -gt 0) {
                if ([IO.File]::Exists("$Path.$Keep")) { [IO.File]::Delete("$Path.$Keep") }
                for ($n = $Keep - 1; $n -ge 1; $n--) {
                    if ([IO.File]::Exists("$Path.$n")) { [IO.File]::Move("$Path.$n", "$Path.$($n + 1)") }
                }
                [IO.File]::Copy($Path, "$Path.1")
            }
            [IO.FileStream]::new($Path, [IO.FileMode]::Truncate, [IO.FileAccess]::Write,
                                 [IO.FileShare]::ReadWrite).Dispose()
        }
    } catch {
        # A rotation that fails (an old file held open, a full disk) leaves the log growing; no line is lost.
    }
    foreach ($part in ($Line -split "\r?\n")) {
        [IO.File]::AppendAllText($Path, "$(Format-RelayLogLine $part)`r`n", [Text.UTF8Encoding]::new($false))
    }
}

function Get-RestartDelay([int]$FastExits) {
    # 2 s after a relay that ran, as before; after failed starts in a row 2, 4, 8 ... s, at most 5 minutes.
    [int][Math]::Min(300, [Math]::Pow(2, [Math]::Max(1, $FastExits)))
}

function Get-RelayPort([string]$ConfigPath) {
    # The port proxy.py binds: RELAY_PORT, else the config's port, else 8787. $null when the config cannot be
    # read: the relay then says what is wrong with it.
    try {
        if ($env:RELAY_PORT) { return [int]$env:RELAY_PORT }
        $saved = Get-Content -Raw -LiteralPath $ConfigPath -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json
        if ($null -eq $saved.port) { return 8787 }
        return [int]$saved.port
    } catch {
        return $null
    }
}

function Test-PortListening([int]$Port) {
    # Reads the system's table of listeners: nothing connects to the port, so a relay serving on it sees nothing.
    try {
        $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    } catch {
        return $false
    }
    return [bool]($listeners | Where-Object { $_.Port -eq $Port })
}

function Invoke-Supervisor {
    param([string]$Python, [string[]]$RelayArgs, [string]$ConfigPath, [string]$LogPath,
          [int]$FastExitSeconds, [int]$MaxFastExits)
    $fastExits = 0
    while ($true) {
        # A relay that cannot bind still writes its start into the shared journal and scans the stored
        # conversations before it fails, so none is started while something listens on the port: a second
        # supervisor (the start line run again), a relay in a terminal or another program.
        $port = Get-RelayPort $ConfigPath
        if ($null -ne $port -and (Test-PortListening $port)) {
            $text = "supervisor: port $port is already in use (another relay, a second supervisor or another " +
                    "program listens on it); the relay was not started and the supervisor stops"
            Write-RelayLog $LogPath "$text $(Get-Date -Format o)"
            Write-Warning (Format-RelayLogLine $text)
            return 1
        }
        $clock = [Diagnostics.Stopwatch]::StartNew()
        $code = "none"
        try {
            & $Python @RelayArgs 2>&1 | ForEach-Object { Write-RelayLog $LogPath "$_" }
            $code = $LASTEXITCODE
        } catch {
            Write-RelayLog $LogPath "supervisor: cannot start the relay with ${Python}: $_"
        }
        $fastExits = if ($clock.Elapsed.TotalSeconds -lt $FastExitSeconds) { $fastExits + 1 } else { 0 }
        if ($fastExits -ge $MaxFastExits) {
            $text = "supervisor: the relay exited within $FastExitSeconds s of its start $fastExits times in a row " +
                    "(exit code $code); the supervisor stops, the lines above say why"
            Write-RelayLog $LogPath "$text $(Get-Date -Format o)"
            Write-Warning (Format-RelayLogLine $text)
            return 1
        }
        $delay = Get-RestartDelay $fastExits
        Write-RelayLog $LogPath "restart in $delay s (exit code $code) $(Get-Date -Format o)"
        Start-Sleep -Seconds $delay
    }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Split-Path -Parent $here)
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:NO_COLOR = "1" # This supervisor redirects to a plain file; colorize in watch_relay.py.
# Windows PowerShell 5.1 decodes native output with this encoding and `*>>` would
# re-encode it as UTF-16LE with a BOM, so relay.log is written explicitly as UTF-8.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$relayArgs = @("-u", "proxy.py")
if ($Config) { $relayArgs += @("--config", $Config) }
if ($StateDir) {
    $null = New-Item -ItemType Directory -Path $StateDir -Force
    $relayArgs += @("--state-dir", $StateDir)
}
$logPath = if ($StateDir) { Join-Path $StateDir "relay.log" } else { "relay.log" }
# .NET resolves relative paths against the process directory, not Set-Location.
$logPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($logPath)
$configPath = if ($Config) { $Config } else { "config.json" }
$code = Invoke-Supervisor -Python $Python -RelayArgs $relayArgs -ConfigPath $configPath -LogPath $logPath `
    -FastExitSeconds $FastExitSeconds -MaxFastExits $MaxFastExits
exit $code
