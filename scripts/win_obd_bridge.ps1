<#
.SYNOPSIS
  Zero-dependency PowerShell Windows Bluetooth COM -> TCP Bridge for Blue Lodge Shop Bench v2.6.4
  (Carly-safe persistent COM handle, protocol pre-lock, ATAT2 speedup & silent-stall watchdog)

.DESCRIPTION
  Pairs with a Classic Bluetooth SPP OBD-II adapter (such as Carly for Torque or ELM327)
  via its Windows Outgoing Bluetooth COM port (e.g. COM6) and bridges raw bytes
  bidirectionally over a local/LAN TCP socket to Blue Lodge Shop Bench.

  v2.6.4 Stability Architecture:
  - Leaves DTR/RTS at Windows defaults (False): forcing DtrEnable/RtsEnable=True deadlocks
    Windows Bluetooth SPP virtual COM ports (bthmodem.sys) on Carly adapters.
  - Opens the Bluetooth COM port ONCE and keeps the RFCOMM link warm and open across
    stateless Shop Bench HTTP/TCP requests (live gauge ticks, health scans, Test Link).
  - Performs a one-time ELM327 + CAN protocol warm-up (ATZ -> ATE0 -> ATL0 -> ATS1 -> ATSP0
    -> 0100 [6s protocol lock] -> ATAT2 -> ATRV) so live ticks & PID discovery never stall
    on SEARCHING...
  - Watchdog auto-recovers if Bluetooth silently stalls (>6s with 0 RX bytes after TX) and
    drops idle/zombie TCP clients (>15s idle) so no stuck request can starve the bridge.

.EXAMPLE
  # Start on COM6 -> TCP 35000 for Rancher / K8s on laptop Wi-Fi IP:
  .\win_obd_bridge.ps1 -ComPort COM6 -TcpPort 35000 -BindAddress 0.0.0.0

.EXAMPLE
  # Stop a running bridge cleanly:
  .\win_obd_bridge.ps1 -Stop -TcpPort 35000
#>
param(
    [string]$ComPort = "COM5",
    [int]$BaudRate = 38400,
    [string]$BindAddress = "127.0.0.1",
    [int]$TcpPort = 35000,
    [string]$PidFile = "",
    [string]$StopFlagFile = "",
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

if ($TcpPort -lt 1024 -or $TcpPort -gt 65535) {
    throw "TcpPort must be between 1024 and 65535 (got $TcpPort)."
}

# Handle graceful -Stop mode (for Hiram / manual CLI usage)
if ($Stop) {
    if ($StopFlagFile -and (Test-Path -LiteralPath (Split-Path -Parent $StopFlagFile))) {
        Set-Content -LiteralPath $StopFlagFile -Value "stop" -Encoding ASCII
        Start-Sleep -Milliseconds 400
    }
    if ($PidFile -and (Test-Path -LiteralPath $PidFile)) {
        $oldPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($oldPid -match '^\d+$') {
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
            Write-Host "[OK] Stopped Shop Bench BT-OBD Bridge PID $oldPid." -ForegroundColor Yellow
        }
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        exit 0
    }
    $conns = Get-NetTCPConnection -LocalPort $TcpPort -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        foreach ($c in $conns) {
            if ($c.OwningProcess -gt 0) {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
                Write-Host "[OK] Stopped listener PID $($c.OwningProcess) on TCP port $TcpPort." -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "[i] No active bridge listener found on TCP port $TcpPort." -ForegroundColor Gray
    }
    exit 0
}

$ComPort = $ComPort.Trim().ToUpperInvariant()
if ($ComPort -notmatch '^COM[1-9][0-9]{0,2}$') {
    throw "Invalid ComPort '$ComPort'. Expected COM1..COM999 (e.g. COM5)."
}
$BindAddress = $BindAddress.Trim()
$parsedIp = $null
if (-not [System.Net.IPAddress]::TryParse($BindAddress, [ref]$parsedIp) -or $parsedIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
    throw "BindAddress must be a valid IPv4 address (got '$BindAddress')."
}
$oct = $parsedIp.GetAddressBytes()
$isAllowedBind = (
    ($BindAddress -eq "127.0.0.1") -or
    ($BindAddress -eq "0.0.0.0") -or
    ($oct[0] -eq 10) -or
    ($oct[0] -eq 172 -and $oct[1] -ge 16 -and $oct[1] -le 31) -or
    ($oct[0] -eq 192 -and $oct[1] -eq 168) -or
    ($oct[0] -eq 100 -and $oct[1] -ge 64 -and $oct[1] -le 127)
)
if (-not $isAllowedBind) {
    throw "BindAddress must be 127.0.0.1, 0.0.0.0, or a private LAN/Tailscale IPv4 address (got '$BindAddress')."
}
if ($BaudRate -notin @(9600, 19200, 38400, 57600, 115200)) {
    throw "Unsupported BaudRate $BaudRate. Allowed: 9600, 19200, 38400, 57600, 115200."
}

if ($StopFlagFile -and (Test-Path -LiteralPath $StopFlagFile)) {
    Remove-Item -LiteralPath $StopFlagFile -Force -ErrorAction SilentlyContinue
}
if ($PidFile) {
    $pidParent = Split-Path -Parent $PidFile
    if ($pidParent -and -not (Test-Path -LiteralPath $pidParent)) {
        New-Item -ItemType Directory -Path $pidParent -Force | Out-Null
    }
    Set-Content -LiteralPath $PidFile -Value $PID -Encoding ASCII
}

$script:sp = $null

function Test-StopRequested {
    if ($StopFlagFile -and (Test-Path -LiteralPath $StopFlagFile)) { return $true }
    return $false
}

function Close-PersistentSerial {
    if ($null -ne $script:sp) {
        try {
            if ($script:sp.IsOpen) { $script:sp.Close() }
        } catch {}
        try { $script:sp.Dispose() } catch {}
        $script:sp = $null
    }
}

function Invoke-ElmWarmCmd([System.IO.Ports.SerialPort]$port, [string]$cmd, [int]$waitMs = 1200) {
    try {
        if ($port.BytesToRead -gt 0) { $port.DiscardInBuffer() }
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($cmd + "`r")
        $port.Write($bytes, 0, $bytes.Length)
        $deadline = [DateTime]::UtcNow.AddMilliseconds($waitMs)
        $sb = New-Object System.Text.StringBuilder
        $tmp = New-Object byte[] 256
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($port.BytesToRead -gt 0) {
                $n = $port.Read($tmp, 0, [Math]::Min($tmp.Length, $port.BytesToRead))
                if ($n -gt 0) {
                    [void]$sb.Append([System.Text.Encoding]::ASCII.GetString($tmp, 0, $n))
                    if ($sb.ToString().Contains(">")) { break }
                }
            } else {
                Start-Sleep -Milliseconds 10
            }
        }
        return ($sb.ToString().Replace(">", "").Trim())
    } catch {
        return ""
    }
}

function Ensure-PersistentSerial {
    if ($null -ne $script:sp -and $script:sp.IsOpen) {
        try {
            if ($script:sp.BytesToRead -gt 0) { $script:sp.DiscardInBuffer() }
            return $script:sp
        } catch {
            Close-PersistentSerial
        }
    }
    Write-Host "[i] Opening persistent Bluetooth link on $ComPort @ $BaudRate baud..." -ForegroundColor Cyan
    # IMPORTANT: Keep DtrEnable/RtsEnable at default ($false)! Setting them $true deadlocks Windows Bluetooth SPP (bthmodem.sys) on Carly.
    $port = [System.IO.Ports.SerialPort]::new($ComPort, $BaudRate, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
    $port.ReadTimeout = 150
    $port.WriteTimeout = 1500
    $port.Open()
    $script:sp = $port

    # One-time ELM327 + CAN protocol warm-up (pre-locks 0100 SEARCHING... & enables ATAT2 adaptive timing)
    $ident = Invoke-ElmWarmCmd $port "ATZ" 2200
    if (-not $ident) {
        $ident = Invoke-ElmWarmCmd $port "ATI" 1200
    }
    [void](Invoke-ElmWarmCmd $port "ATE0" 800)
    [void](Invoke-ElmWarmCmd $port "ATL0" 800)
    [void](Invoke-ElmWarmCmd $port "ATS1" 800)
    [void](Invoke-ElmWarmCmd $port "ATSP0" 1000)
    [void](Invoke-ElmWarmCmd $port "ATAT2" 800)
    [void](Invoke-ElmWarmCmd $port "ATST14" 800)
    $p0 = Invoke-ElmWarmCmd $port "0100" 6000
    $proto = Invoke-ElmWarmCmd $port "ATDP" 800
    $volt = Invoke-ElmWarmCmd $port "ATRV" 800

    $cleanId = (($ident -split "[\r\n]+") | Where-Object { $_.Trim() -and $_.Trim() -notin @("ATZ","ATI") } | Select-Object -Last 1)
    if (-not $cleanId) { $cleanId = "ELM327 Ready" }
    $cleanV = (($volt -split "[\r\n]+") | Where-Object { $_.Trim() -and $_.Trim() -ne "ATRV" } | Select-Object -Last 1)
    $cleanP = (($proto -split "[\r\n]+") | Where-Object { $_.Trim() -and $_.Trim() -ne "ATDP" } | Select-Object -Last 1)
    $ecuOk = ($p0 -match "41\s*00")
    Write-Host "[OK] Persistent $ComPort link ACTIVE ($cleanId | Battery: $(if ($cleanV) { $cleanV } else { 'n/a' }) | Proto: $(if ($cleanP) { $cleanP } else { 'AUTO' }) | ECU: $(if ($ecuOk) { 'LOCKED' } else { 'STANDBY' }))." -ForegroundColor Green
    return $script:sp
}

$listener = [System.Net.Sockets.TcpListener]::new($parsedIp, $TcpPort)
$listener.Start()
Write-Host "[OK] Blue Lodge Shop Bench BT-OBD Bridge v2.6.4 (PID $PID) listening on tcp://${BindAddress}:${TcpPort} -> $ComPort ($BaudRate baud)" -ForegroundColor Cyan
if ($BindAddress -eq "0.0.0.0") {
    $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } |
        Select-Object -ExpandProperty IPAddress -Unique
    if ($lanIps) {
        $targets = ($lanIps | ForEach-Object { "tcp://${_}:${TcpPort}" }) -join "  or  "
        Write-Host "     Connect from K8s / Rancher via: $targets" -ForegroundColor Green
    }
} elseif ($BindAddress -ne "127.0.0.1") {
    Write-Host "     Connect from K8s / Rancher via: tcp://${BindAddress}:${TcpPort}" -ForegroundColor Green
} else {
    Write-Host "     Connect from WSL2 via: tcp://127.0.0.1:${TcpPort}" -ForegroundColor Green
}
Write-Host "     Press Ctrl+C or click 'Stop Bridge' in Shop Bench to close cleanly and release $ComPort." -ForegroundColor Gray

try {
    [void](Ensure-PersistentSerial)
} catch {
    Write-Host "[!] $ComPort not ready yet ($($_.Exception.Message)). Will auto-connect on first Shop Bench request." -ForegroundColor Yellow
}

$lastLogTime = [DateTime]::MinValue
$pollCount = 0

try {
    while (-not (Test-StopRequested)) {
        if (-not $listener.Pending()) {
            Start-Sleep -Milliseconds 25
            continue
        }
        $client = $listener.AcceptTcpClient()
        $client.NoDelay = $true
        $pollCount++
        $nowUtc = [DateTime]::UtcNow
        if (($nowUtc - $lastLogTime).TotalSeconds -ge 15) {
            Write-Host "[+] Shop Bench active from $($client.Client.RemoteEndPoint) (poll #$pollCount; $ComPort held persistently open)" -ForegroundColor Green
            $lastLogTime = $nowUtc
        }

        try {
            $sp = Ensure-PersistentSerial
            $stream = $client.GetStream()
            $buf = New-Object byte[] 1024
            $lastActivityUtc = [DateTime]::UtcNow
            $pendingReplySinceUtc = $null

            while ($client.Connected -and -not (Test-StopRequested)) {
                $didWork = $false
                $curUtc = [DateTime]::UtcNow

                if ($stream.DataAvailable) {
                    $n = $stream.Read($buf, 0, $buf.Length)
                    if ($n -le 0) { break }
                    try {
                        $sp.Write($buf, 0, $n)
                    } catch {
                        Write-Host "[!] Serial write error on ${ComPort}: $($_.Exception.Message) — resetting COM link." -ForegroundColor Yellow
                        Close-PersistentSerial
                        break
                    }
                    $lastActivityUtc = $curUtc
                    if ($null -eq $pendingReplySinceUtc) { $pendingReplySinceUtc = $curUtc }
                    $didWork = $true
                }

                if ($null -ne $script:sp -and $script:sp.IsOpen -and $sp.BytesToRead -gt 0) {
                    try {
                        $m = $sp.Read($buf, 0, [Math]::Min($buf.Length, $sp.BytesToRead))
                    } catch {
                        Write-Host "[!] Serial read error on ${ComPort}: $($_.Exception.Message) — resetting COM link." -ForegroundColor Yellow
                        Close-PersistentSerial
                        break
                    }
                    if ($m -gt 0) {
                        $stream.Write($buf, 0, $m)
                        $lastActivityUtc = $curUtc
                        $pendingReplySinceUtc = $null
                        $didWork = $true
                    }
                }

                # Watchdog 1: Detect silent Bluetooth SPP stall (TX sent, 0 bytes back for >7.5s)
                if ($null -ne $pendingReplySinceUtc -and ($curUtc - $pendingReplySinceUtc).TotalSeconds -gt 7.5) {
                    Write-Host "[!] Watchdog: $ComPort silent for >7.5s after TX — resetting Bluetooth COM link for auto-recovery." -ForegroundColor Yellow
                    Close-PersistentSerial
                    break
                }

                # Watchdog 2: Drop zombie/idle TCP clients after 15s inactivity so no stuck client starves Shop Bench
                if (($curUtc - $lastActivityUtc).TotalSeconds -gt 15.0) {
                    Write-Host "[!] Closing idle TCP client (>15s inactive)." -ForegroundColor Yellow
                    break
                }

                if ($client.Client.Poll(0, [System.Net.Sockets.SelectMode]::SelectRead) -and ($client.Client.Available -eq 0)) {
                    break
                }
                if (-not $didWork) {
                    Start-Sleep -Milliseconds 5
                }
            }
        } catch {
            Write-Host "[!] Client session error: $($_.Exception.Message)" -ForegroundColor Yellow
            if ($null -eq $script:sp -or -not $script:sp.IsOpen) {
                Close-PersistentSerial
            }
        } finally {
            try { $client.Close() } catch {}
            if ($null -ne $script:sp -and $script:sp.IsOpen) {
                try {
                    if ($script:sp.BytesToRead -gt 0) { $script:sp.DiscardInBuffer() }
                } catch {
                    Close-PersistentSerial
                }
            }
        }
    }
} finally {
    Close-PersistentSerial
    $listener.Stop()
    if ($PidFile -and (Test-Path -LiteralPath $PidFile)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    }
    if ($StopFlagFile -and (Test-Path -LiteralPath $StopFlagFile)) {
        Remove-Item -LiteralPath $StopFlagFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "[i] Bridge stopped cleanly; TCP port $TcpPort and $ComPort released." -ForegroundColor Gray
}
