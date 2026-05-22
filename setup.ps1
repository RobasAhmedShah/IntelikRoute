# IntelikRoute Automated Windows Setup
Write-Host "=== IntelikRoute Automated Windows Setup ===" -ForegroundColor Cyan

# 1. Ensure .intelikroute directory exists
$HomeDir = [System.Environment]::GetFolderPath("UserProfile")
$IntelikDir = Join-Path $HomeDir ".intelikroute"
$BinDir = Join-Path $IntelikDir "bin"

if (!(Test-Path $BinDir)) {
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
}

# 2. Download and install upnpc.exe if not already in PATH
$UpnpcPath = Get-Command upnpc.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue
if ($UpnpcPath) {
    Write-Host "Found existing upnpc.exe at: $UpnpcPath" -ForegroundColor Green
} else {
    Write-Host "upnpc.exe not found. Downloading MiniUPnP client..." -ForegroundColor Yellow
    $ZipUrl = "https://miniupnp.tuxfamily.org/files/upnpc-exe-win32-20150918.zip"
    $TempZip = Join-Path $env:TEMP "upnpc.zip"
    $ExtractDir = Join-Path $env:TEMP "upnpc_extracted"

    try {
        # Download zip
        Invoke-WebRequest -Uri $ZipUrl -OutFile $TempZip -UseBasicParsing
        Write-Host "Download complete. Extracting..." -ForegroundColor Yellow

        # Extract zip
        if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
        Expand-Archive -Path $TempZip -DestinationPath $ExtractDir -Force

        # Find upnpc.exe in extracted contents
        $ExtractedExe = Get-ChildItem -Path $ExtractDir -Filter "upnpc.exe" -Recurse | Select-Object -First 1
        if ($ExtractedExe) {
            Move-Item -Path $ExtractedExe.FullName -Destination (Join-Path $BinDir "upnpc.exe") -Force
            Write-Host "Installed upnpc.exe to: $BinDir" -ForegroundColor Green
        } else {
            throw "Could not find upnpc.exe inside the downloaded zip."
        }
    } catch {
        Write-Host "Error during upnpc installation: $_" -ForegroundColor Red
        Write-Host "Please install upnpc.exe manually and add it to your PATH." -ForegroundColor Red
    } finally {
        if (Test-Path $TempZip) { Remove-Item $TempZip -Force }
        if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
    }
}

# 3. Install Python package via pip
Write-Host ""
Write-Host "Installing IntelikRoute Python package..." -ForegroundColor Yellow
try {
    pip install -e .
    Write-Host "Python package installed successfully!" -ForegroundColor Green
} catch {
    Write-Host "Failed to install Python package. Make sure Python and pip are installed and on your PATH." -ForegroundColor Red
    Exit 1
}

# 4. Configure PATH variables
Write-Host ""
Write-Host "Configuring PATH..." -ForegroundColor Yellow

# Get Python scripts user path
$PythonUserBase = python -m site --user-base 2>$null
if ($PythonUserBase) {
    # On Windows, python user binaries are stored in "Scripts" under user-base
    $PythonBin = Join-Path $PythonUserBase "Scripts"
} else {
    $PythonBin = Join-Path $env:APPDATA "Python\Scripts"
}

# Update User PATH environment variable
$UserPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
$PathsToAdd = @($BinDir, $PythonBin)
$PathUpdated = $false

foreach ($Path in $PathsToAdd) {
    if ($UserPath -notlike "*$Path*") {
        Write-Host "Adding $Path to User PATH..." -ForegroundColor Yellow
        $UserPath = "$Path;$UserPath"
        $PathUpdated = $true
    }
}

if ($PathUpdated) {
    [System.Environment]::SetEnvironmentVariable("PATH", $UserPath, "User")
    Write-Host "PATH successfully updated! Please restart your terminal/PowerShell window to apply changes." -ForegroundColor Green
} else {
    Write-Host "PATH is already configured correctly." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== IntelikRoute Windows Setup Completed ===" -ForegroundColor Green
Write-Host "Please close this PowerShell window and open a new one." -ForegroundColor Green
Write-Host "You can then run: intelikroute --help" -ForegroundColor Green
