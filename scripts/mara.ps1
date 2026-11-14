# Start MARA in the browser without installing the desktop app (Windows).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/moritzlauper/mara/main/scripts/mara.ps1 | iex"
#
# Installs uv if needed, fetches the latest MARA from GitHub, and opens
# http://127.0.0.1:8765. Run the same line again to update and start.
# Everything runs inside a script block: with `irm | iex`, `exit` would close the user's window.
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Windows PowerShell 5.1 downloads very slowly with a progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $repo = if ($env:MARA_REPO) { $env:MARA_REPO } else { 'moritzlauper/mara' }
    $ref = if ($env:MARA_REF) { $env:MARA_REF } else { 'main' }
    $root = if ($env:MARA_HOME) { $env:MARA_HOME } else { Join-Path $HOME '.mara' }
    $workspace = if ($env:MARA_WORKSPACE) { $env:MARA_WORKSPACE } else { Join-Path $env:LOCALAPPDATA 'MARA' }
    $app = Join-Path $root 'app'

    function Say($text) { Write-Host 'MARA ' -NoNewline -ForegroundColor Cyan; Write-Host $text }

    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        $env:Path = (Join-Path $HOME '.local\bin') + [IO.Path]::PathSeparator + $env:Path
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say 'Installing uv (Python package manager) ...'
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
        $env:Path = (Join-Path $HOME '.local\bin') + [IO.Path]::PathSeparator + $env:Path
    }

    New-Item -ItemType Directory -Force -Path $root, $workspace | Out-Null
    Say 'Fetching the latest version ...'
    $tmp = Join-Path $root ('download-' + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $tmp | Out-Null
        $zip = Join-Path $tmp 'mara.zip'
        Invoke-WebRequest -UseBasicParsing "https://github.com/$repo/archive/refs/heads/$ref.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $tmp
        $source = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
        if (Test-Path $app) { Remove-Item -Recurse -Force $app }
        Move-Item -Path $source.FullName -Destination $app
    } catch {
        if (-not (Test-Path (Join-Path $app 'pyproject.toml'))) {
            Say "Download failed. Check your internet connection and try again. ($($_.Exception.Message))"
            return
        }
        Say 'Download failed; starting the version already installed.'
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }

    # The environment lives outside app\, so updates only reinstall what changed.
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $root 'venv'
    Say "Starting (data: $workspace). Your browser opens MARA; stop it with Ctrl+C."
    Push-Location $workspace
    try { uv run --quiet --python 3.12 --project $app mixmatch ui @args }
    finally { Pop-Location }
} @args
