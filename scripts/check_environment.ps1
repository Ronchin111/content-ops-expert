#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('douyin', 'wechat', 'xhs')][string]$Platform,
    [Parameter(Mandatory = $true)][string]$ToolRoot,
    [string]$PythonPath,
    [string]$ChromePath,
    [ValidateRange(1, 30)][int]$ProbeSeconds = 10
)

# Native, read-only prerequisite check. No installer, panel or account is started.
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function New-EnvironmentCheck($Name, $State, $Detail, $NextAction) {
    [pscustomobject][ordered]@{ name = $Name; state = $State; detail = $Detail; next_action = $NextAction }
}

function Get-EnvironmentLocalPath([string]$Value) {
    $full = [IO.Path]::GetFullPath($Value)
    if ($full -notmatch '^[A-Za-z]:\\') { throw 'only_local_drive_paths_supported' }
    $driveRoot = $full.Substring(0, 3)
    $drive = New-Object IO.DriveInfo($driveRoot)
    if ($drive.DriveType -notin @([IO.DriveType]::Fixed, [IO.DriveType]::Removable)) {
        throw 'network_or_unverified_drive_not_supported'
    }
    # Inspect each ancestor before touching its children; never traverse a junction.
    $partPath = $driveRoot
    $parts = @('') + @($full.Substring(3).Split([char[]]'\', [StringSplitOptions]::RemoveEmptyEntries))
    foreach ($part in $parts) {
        if ($part) { $partPath = Join-Path $partPath $part }
        try { $entry = Get-Item -LiteralPath $partPath -Force }
        catch [System.Management.Automation.ItemNotFoundException] { break }
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse_paths_not_supported' }
    }
    return $full
}

function Find-EnvironmentPython([string]$ExplicitPath, [string]$Root) {
    if ($ExplicitPath) { return Get-EnvironmentLocalPath $ExplicitPath }
    $candidatePaths = @(Join-Path $Root '.venv\Scripts\python.exe')
    foreach ($folder in ($env:PATH -split ';')) {
        $folder = $folder.Trim('"')
        if ($folder -match '^[A-Za-z]:[\\/]' -and $folder -notmatch '\\Microsoft\\WindowsApps(?:\\|$)') {
            $candidatePaths += Join-Path $folder 'python.exe'
        }
    }
    foreach ($candidate in $candidatePaths) {
        try { $localPython = Get-EnvironmentLocalPath $candidate }
        catch { continue }
        if (Test-Path -LiteralPath $localPython -PathType Leaf) { return $localPython }
    }
    return $null
}

function Invoke-EnvironmentPython([string]$Executable, [string]$SelectedPlatform, [int]$Seconds) {
    $probeCode = @'
import contextlib, importlib, io, json, re, sys
result = {"version": list(sys.version_info[:3]), "packages": {}}
if sys.version_info >= (3, 10):
    import importlib.metadata
    required = {"playwright": ("playwright.async_api", (1, 40, 0)), "openpyxl": ("openpyxl", (3, 1, 0))}
    if sys.argv[1] == "wechat":
        required["httpx"] = ("httpx", None)
    for name, (module, minimum) in required.items():
        row = {"state": "ready", "version": None, "error_type": None}
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                importlib.import_module(module)
                try:
                    row["version"] = importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:
                    pass
            if minimum:
                parts = re.match(r"^(\d+)\.(\d+)\.(\d+)$", row["version"] or "")
                if not parts:
                    row["state"] = "version_unverified"
                elif tuple(map(int, parts.groups())) < minimum:
                    row["state"] = "version_too_old"
        except ModuleNotFoundError as exc:
            row["state"] = "missing" if exc.name == name else "broken"
            row["error_type"] = "ModuleNotFoundError"
        except Exception as exc:
            row["state"] = "broken"
            row["error_type"] = type(exc).__name__
        result["packages"][name] = row
sys.stdout.write(json.dumps(result, ensure_ascii=True))
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probeCode))
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $Executable
    # Only constant/base64 code and a ValidateSet platform enter the argument string.
    $start.Arguments = '-I -B -c "import base64;exec(base64.b64decode(''' + $encoded + '''))" ' + $SelectedPlatform
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'probe_start_failed' }
        $outTask = $process.StandardOutput.ReadToEndAsync()
        $errTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Seconds * 1000)) {
            $process.Kill()
            $null = $process.WaitForExit(1000)
            return [pscustomobject]@{ state = 'timeout'; data = $null }
        }
        # Never echo a Python environment's stderr or an unrecognized stdout.
        if (-not $outTask.Wait(1000) -or -not $errTask.Wait(1000) -or $process.ExitCode -ne 0) {
            return [pscustomobject]@{ state = 'probe_failed'; data = $null }
        }
        $raw = $outTask.Result
        if ($raw.Length -gt 32768) { throw 'probe_response_limit' }
        $data = $raw | ConvertFrom-Json
        if (@($data.version).Count -ne 3 -or -not $data.packages) { throw 'probe_response_invalid' }
        foreach ($number in $data.version) {
            if ($number -isnot [int] -and $number -isnot [long]) { throw 'probe_version_invalid' }
        }
        return [pscustomobject]@{ state = 'ready'; data = $data }
    }
    catch { return [pscustomobject]@{ state = 'probe_failed'; data = $null } }
    finally { $process.Dispose() }
}

function Invoke-EnvironmentCheck([string]$SelectedPlatform, [string]$Root, [string]$Interpreter, [string]$Browser, [int]$Seconds) {
    $SelectedPlatform = $SelectedPlatform.ToLowerInvariant()
    $checks = New-Object 'Collections.Generic.List[object]'
    $rootPath = Get-EnvironmentLocalPath $Root
    $requiredFiles = @('launcher.py', 'panel.html', 'scripts\mediaexport_maintenance.py', 'scripts\run_paths.py', 'scripts\export_results.py')
    switch ($SelectedPlatform) {
        'douyin' { $requiredFiles += 'scripts\douyin_export_all.py' }
        'xhs' { $requiredFiles += 'scripts\xhs_export_all.py' }
        'wechat' { $requiredFiles += 'scripts\wechat_export_all.py' }
    }
    $missingFiles = @($requiredFiles | Where-Object { -not (Test-Path -LiteralPath (Get-EnvironmentLocalPath (Join-Path $rootPath $_)) -PathType Leaf) })
    if ($missingFiles.Count) {
        $checks.Add((New-EnvironmentCheck 'tool_files' 'missing' ('缺少工具程序：' + ($missingFiles -join '、')) '选择已有完整工具根目录；从已批准的交付包恢复程序后复检，不读取历史报表。'))
    } else {
        $checks.Add((New-EnvironmentCheck 'tool_files' 'ready' '本平台入口及关键程序存在；尚未校验整包版本和许可。' $null))
    }
    try {
        $null = Get-Item -LiteralPath (Get-EnvironmentLocalPath (Join-Path $rootPath '.mediaexport-maintenance')) -Force
        $checks.Add((New-EnvironmentCheck 'maintenance' 'blocked' '工具处于维护状态。' '由维护负责人完成原有解除流程后复检。'))
    } catch [System.Management.Automation.ItemNotFoundException] {
        $checks.Add((New-EnvironmentCheck 'maintenance' 'ready' '未发现维护标记。' $null))
    }

    $selectedPython = Find-EnvironmentPython $Interpreter $rootPath
    $packages = @('playwright', 'openpyxl')
    if ($SelectedPlatform -eq 'wechat') { $packages += 'httpx' }
    $probe = $null
    if (-not $selectedPython -or -not (Test-Path -LiteralPath $selectedPython -PathType Leaf) -or [IO.Path]::GetExtension($selectedPython) -ine '.exe' -or $selectedPython -match '\\Microsoft\\WindowsApps\\') {
        $checks.Add((New-EnvironmentCheck 'python' 'missing' '未找到可用的明确Python解释器；指定路径无效时不会另换一个。' '优先提供已有可信Python的完整exe路径；确实没有时查看environment.md，先列安装内容与独立用户目录。'))
    } elseif (-not $Interpreter) {
        $checks.Add((New-EnvironmentCheck 'python' 'needs_confirmation' '已发现本地Python候选，尚未启动或核实来源。' '由Agent核对候选来源可信后，以完整PythonPath明确指定，再运行导入检查；不执行来源不明程序。'))
    } else {
        $probe = Invoke-EnvironmentPython $selectedPython $SelectedPlatform $Seconds
        if ($probe.state -ne 'ready') {
            $checks.Add((New-EnvironmentCheck 'python' 'blocked' ('Python检测未完成：' + $probe.state) '检查所选解释器能否运行；保留本次结果，处理权限或超时后手动复检，不自动重试。'))
        } elseif ($probe.data.version[0] -lt 3 -or ($probe.data.version[0] -eq 3 -and $probe.data.version[1] -lt 10)) {
            $checks.Add((New-EnvironmentCheck 'python' 'blocked' ('Python版本过低：' + ($probe.data.version -join '.') + '；原导出器需要3.10及以上。') '选用满足版本的已有解释器，或先列独立安装方案。'))
            $probe = $null
        } else {
            $checks.Add((New-EnvironmentCheck 'python' 'ready' ('Python ' + ($probe.data.version -join '.')) $null))
        }
    }
    foreach ($name in $packages) {
        if (-not $probe -or $probe.state -ne 'ready') {
            $checks.Add((New-EnvironmentCheck $name 'not_checked' '解释器未就绪，未推断依赖存在或缺失。' '先处理Python，再复检本平台依赖。'))
            continue
        }
        $row = $probe.data.packages.PSObject.Properties[$name]
        if (-not $row) { throw 'probe_package_missing' }
        $packageState = [string]$row.Value.state
        if ($packageState -notin @('ready', 'missing', 'broken', 'version_unverified', 'version_too_old')) { throw 'probe_package_invalid' }
        if ($packageState -eq 'ready') {
            $checks.Add((New-EnvironmentCheck $name 'ready' '本次解释器导入检查通过；不代替真实导出验证。' $null))
        } else {
            $checks.Add((New-EnvironmentCheck $name $packageState ('组件未就绪：' + $packageState) '在同一独立环境补齐或修复此组件后复检；不要向其他Python盲装。'))
        }
    }

    $browserCandidates = @()
    if ($Browser) { $browserCandidates = @(Get-EnvironmentLocalPath $Browser) }
    else {
        foreach ($folder in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            if ($folder) { $browserCandidates += Join-Path $folder 'Google\Chrome\Application\chrome.exe' }
        }
    }
    $selectedChrome = @($browserCandidates | Where-Object {
        try { $localBrowser = Get-EnvironmentLocalPath $_ }
        catch { return $false }
        (Test-Path -LiteralPath $localBrowser -PathType Leaf) -and [IO.Path]::GetFileName($localBrowser) -ieq 'chrome.exe'
    } | Select-Object -First 1)
    if ($selectedChrome.Count) {
        $checks.Add((New-EnvironmentCheck 'chrome' 'ready' 'Chrome程序文件存在，未启动；版本及登录尚未验证。' $null))
    } else {
        $checks.Add((New-EnvironmentCheck 'chrome' 'missing' '未发现Chrome程序；仅Edge暂不能通过现有面板Chrome检查。' '提供已有Chrome的完整exe路径；自定义路径还需面板原预检支持，或先列浏览器安装方案。'))
    }
    $unready = @($checks | Where-Object { $_.state -ne 'ready' })
    [pscustomobject][ordered]@{
        schema_version = 1; checker_version = 'PAY-S3-03-candidate2'; platform = $SelectedPlatform
        state = $(if ($unready.Count) { 'needs_environment' } else { 'prerequisites_ready' })
        tool_root = $rootPath; python_path = $selectedPython
        checks = @($checks.ToArray()); installation_performed = $false; export_started = $false
        next_step = $(if ($unready.Count) { '按缺项处理后复检。环境缺项不计为本轮导出失败；本次尚未导出。' } else { '仅此解释器的环境前置条件就绪；继续run_export.py默认面板预检，确认实际面板环境与账号后按原授权取数。' })
        limitations = @('仅支持本地固定/可移动磁盘，拒绝UNC、网络盘和重解析路径；检查依赖可信本地环境，不能保证被替换程序的行为。', '未查询或启动面板；面板可能使用其他解释器。', '未读登录资料、报表或配置；未验证账号、业务数据、浏览器运行或保存目录写入能力。')
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $result = Invoke-EnvironmentCheck $Platform $ToolRoot $PythonPath $ChromePath $ProbeSeconds
        $result | ConvertTo-Json -Depth 8
        if ($result.state -eq 'prerequisites_ready') { exit 0 }
        exit 1
    } catch {
        $errorCode = 'check_failed'
        $nextStep = '检测未完成；核对路径与读取权限后复检。未安装、未导出，未回显原始异常内容。'
        if ($_.Exception.Message -in @('only_local_drive_paths_supported', 'network_or_unverified_drive_not_supported', 'reparse_paths_not_supported')) {
            $errorCode = 'unsupported_path'
            $nextStep = '路径不支持：仅允许本地固定或可移动磁盘，拒绝UNC、网络盘及重解析路径。改用本地可信副本后复检；不是缺Python或导出失败。'
        }
        [pscustomobject]@{ schema_version = 1; state = 'check_incomplete'; error_code = $errorCode; error_type = $_.Exception.GetType().Name; next_step = $nextStep; installation_performed = $false; export_started = $false } | ConvertTo-Json
        exit 2
    }
}
