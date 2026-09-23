# Runs a throwaway LiveKit server on this machine, for testing calls locally.
#
#     powershell -ExecutionPolicy Bypass -File scripts\livekit-local.ps1
#
# Needs Docker Desktop running. It reads LIVEKIT_API_KEY and LIVEKIT_API_SECRET
# from .env, so the server accepts exactly the tokens Django signs and no
# secret is written here. Then set, in .env:
#
#     LIVEKIT_URL=ws://localhost:7880
#     LIVEKIT_API_URL=
#
# and restart runserver. Ctrl+C stops it; --rm removes the container.
#
# Same image version as the staging service (aws-server), so behaviour matches.
# Signalling and TCP/UDP media only: no webhook (a closed tab is cleaned up by
# the sweep, not at once) and no egress, so recording does not work here.
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$envFile = Join-Path $PSScriptRoot '..\.env'

function Get-EnvValue($name) {
    $line = Select-String -Path $envFile -Pattern "^$name=(.*)$" | Select-Object -First 1
    if (-not $line) { throw "$name is not set in .env" }
    $value = $line.Matches[0].Groups[1].Value.Trim()
    if (-not $value) { throw "$name is empty in .env" }
    return $value
}

$key = Get-EnvValue 'LIVEKIT_API_KEY'
$secret = Get-EnvValue 'LIVEKIT_API_SECRET'

# One mux port for UDP so a single -p covers it (the default is a 10,000-port
# range), and the browser is told to use this machine's own address.
$config = "port: 7880`nrtc:`n  tcp_port: 7881`n  udp_port: 7882`n  use_external_ip: false`n"

$dockerArgs = @(
    'run', '--rm', '--name', 'livekit',
    '-p', '7880:7880', '-p', '7881:7881', '-p', '7882:7882/udp',
    '-e', "LIVEKIT_KEYS=${key}: ${secret}",
    '-e', "LIVEKIT_CONFIG=$config",
    'livekit/livekit-server:v1.13.7',
    '--node-ip', '127.0.0.1'
)

if ($DryRun) {
    # The secret is never printed.
    Write-Host "docker $(($dockerArgs -join ' ').Replace($secret, '<secret>'))"
    return
}

docker @dockerArgs
