param(
    [Parameter(Mandatory = $true)]
    [string]$BundleDir,

    [ValidateRange(10, 180)]
    [int]$LaunchTimeoutSeconds = 75
)

$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class PackageNativeLibrary
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadLibraryEx(
        string lpFileName,
        IntPtr hFile,
        uint dwFlags
    );

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool FreeLibrary(IntPtr hModule);
}
'@

function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required package file is missing: $Path"
    }

    $item = Get-Item -LiteralPath $Path
    if ($item.Length -le 0) {
        throw "Required package file is empty: $Path"
    }

    return $item.FullName
}

function Test-NativeLibraryLoad {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Paths
    )

    $handles = @()
    try {
        foreach ($path in $Paths) {
            # LOAD_WITH_ALTERED_SEARCH_PATH makes dependent DLL lookup start in
            # the packaged _internal directory instead of the caller's PATH.
            $handle = [PackageNativeLibrary]::LoadLibraryEx(
                $path,
                [IntPtr]::Zero,
                0x00000008
            )
            if ($handle -eq [IntPtr]::Zero) {
                $code = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
                $message = (New-Object System.ComponentModel.Win32Exception -ArgumentList $code).Message
                throw "Unable to load $path (Win32 ${code}: $message)"
            }
            $handles += $handle
            Write-Host "  Loaded: $([IO.Path]::GetFileName($path))"
        }
    }
    finally {
        for ($index = $handles.Count - 1; $index -ge 0; $index--) {
            [void][PackageNativeLibrary]::FreeLibrary($handles[$index])
        }
    }
}

$bundle = (Resolve-Path -LiteralPath $BundleDir).Path
$internal = Join-Path $bundle '_internal'
$executable = Resolve-RequiredFile (Join-Path $bundle 'NekroAgent.exe')
$nativeFiles = @(
    Resolve-RequiredFile (Join-Path $internal 'python311.dll')
    Resolve-RequiredFile (Join-Path $internal 'libcrypto-3-x64.dll')
    Resolve-RequiredFile (Join-Path $internal 'libssl-3-x64.dll')
    Resolve-RequiredFile (Join-Path $internal '_ssl.pyd')
)

Write-Host 'Checking packaged Python and SSL native libraries...'
Test-NativeLibraryLoad -Paths $nativeFiles

Write-Host 'Starting packaged executable in backend-check mode...'
$temporaryLocalAppData = Join-Path (
    [IO.Path]::GetTempPath()
) ("NekroAgentPackageSmoke-{0}" -f [Guid]::NewGuid().ToString('N'))
$bundleDataDir = Join-Path $bundle 'data'
$bundleDataAlreadyExisted = Test-Path -LiteralPath $bundleDataDir
$oldLocalAppData = $env:LOCALAPPDATA
$process = $null

function Get-BackendCheckReport {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        # windowed 构建的 stdout 为 None，拿不到报告本身就是一个信号
        return '<no backend-check report was produced>'
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).TrimEnd()
}

try {
    New-Item -ItemType Directory -Path $temporaryLocalAppData -Force | Out-Null
    $env:LOCALAPPDATA = $temporaryLocalAppData
    # 打包后的 EXE 没有控制台，stdout 无处可去；让它把检查结果写成文件再读回来。
    $reportPath = Join-Path $temporaryLocalAppData 'backend-check.txt'
    $process = Start-Process -FilePath $executable `
        -ArgumentList '--backend-check', "--backend-check-output=$reportPath" `
        -WorkingDirectory $bundle `
        -PassThru

    if (-not $process.WaitForExit($LaunchTimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Packaged executable did not exit within $LaunchTimeoutSeconds seconds"
    }

    $report = Get-BackendCheckReport -Path $reportPath
    Write-Host '  --- backend check report ---'
    foreach ($line in ($report -split "`r?`n")) {
        Write-Host "    $line"
    }

    # Exit 1 means the executable loaded correctly but one of the host's WSL or
    # Docker checks failed. Other codes indicate a launcher/package startup fault.
    if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 1) {
        throw ("Packaged executable exited unexpectedly with code " +
            "$($process.ExitCode). Backend check report:`n$report")
    }
    if ($process.ExitCode -eq 1) {
        Write-Host '  Executable started successfully; host backend checks reported unavailable components.'
    }
    else {
        Write-Host '  Executable and host backend checks completed successfully.'
    }
}
finally {
    $env:LOCALAPPDATA = $oldLocalAppData
    if (Test-Path -LiteralPath $temporaryLocalAppData) {
        Remove-Item -LiteralPath $temporaryLocalAppData -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $bundleDataAlreadyExisted -and (Test-Path -LiteralPath $bundleDataDir)) {
        Remove-Item -LiteralPath $bundleDataDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host 'Package smoke test passed.'
