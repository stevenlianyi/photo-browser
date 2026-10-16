# ==========================================================================
#  photo-browser 一键打包（Windows）
#
#  一条命令产出：
#     dist\photo-browser\photo-browser.exe      （PyInstaller --onedir 的独立 exe）
#     dist\PhotoBrowser-Setup-<版本>.exe        （Inno Setup 6 的安装包）
#
#  三步：前端构建 -> PyInstaller -> ISCC
#
#  用法（在仓库根或任意目录都能跑）：
#     powershell -ExecutionPolicy Bypass -File build\package.ps1
#     powershell -ExecutionPolicy Bypass -File build\package.ps1 -Version 0.2.0
#     powershell -ExecutionPolicy Bypass -File build\package.ps1 -SkipFrontend
#
#  ⚠️ 为什么要有 `-ExecutionPolicy Bypass` 这一句
#     PowerShell 5.1 在策略为 Restricted/AllSigned 时**拒绝运行 .ps1**。
#     `start.cmd`（源码启动器）之所以是 .cmd 就是为了绕开它；
#     但打包脚本没法用 .cmd 写（要处理大量路径与错误码），
#     所以这里在**命令行上**显式绕过 —— 那是本次调用的一次性选择，
#     不是让用户去改机器的安全策略。
#
#  ⚠️ 为什么前端必须重新构建
#     `code/webserver/dist` 是**上一次 npm run build 的快照**。
#     不重建就会把一个"落后于源码"的前端打进安装包，
#     而症状是「新功能在开发环境有、装完没有」，且完全没有任何报错。
#
#  ⚠️⚠️ 本文件**必须**保存成「UTF-8 with BOM」
#     Windows PowerShell 5.1 对**没有 BOM** 的 .ps1 会按**系统 ANSI 代码页**
#     （本机 936）读 —— 文件里的中文会变成乱码，而乱码里出现的引号/大括号
#     会让解析器直接报 `Missing closing '}'`（本机实测：
#     一句 `Die "$what 失败（退出码 $rc）"` 就能让整个脚本解析失败）。
#     ⇒ 用编辑器保存时请选 "UTF-8 with BOM"，或：
#         $t = [IO.File]::ReadAllText($p,[Text.Encoding]::UTF8)
#         [IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
#     （`start.cmd` 走的是相反的路子：它**必须纯 ASCII**，因为 cmd.exe 没有 BOM 机制。
#       两者不能混：.cmd 纯 ASCII，.ps1 带 BOM。）
# ==========================================================================

[CmdletBinding()]
param(
    [string]$Version      = "0.2.0",
    [switch]$SkipFrontend,
    [switch]$SkipPyInstaller,
    [switch]$SkipInstaller,
    [switch]$KeepWork,          # 保留 build\work（排障用；默认删掉）
    [switch]$NoClean            # 不删 dist\（增量重打 PyInstaller 时用）
)

$ErrorActionPreference = "Stop"

$BuildDir = $PSScriptRoot
$Repo     = Split-Path -Parent $BuildDir
$Code     = Join-Path $Repo "code"
$Src      = Join-Path $Code "src"
$Py       = Join-Path $Code ".venv\Scripts\python.exe"
$WebDir   = Join-Path $Code "webserver"
$WebDist  = Join-Path $WebDir "dist"
$Dist     = Join-Path $Repo "dist"
$Work     = Join-Path $BuildDir "work"
$AppDist  = Join-Path $Dist "photo-browser"
$SpecFile = Join-Path $BuildDir "photo-browser.spec"
$IssFile  = Join-Path $BuildDir "installer.iss"

$script:sw = [System.Diagnostics.Stopwatch]::StartNew()

function Step([string]$text) {
    Write-Host ""
    Write-Host ("==== " + $text + " " + ("=" * [Math]::Max(0, 66 - $text.Length))) -ForegroundColor Cyan
}

function Ok([string]$text)   { Write-Host ("  [ok] " + $text) -ForegroundColor Green }
function Info([string]$text) { Write-Host ("  " + $text) }
function Warn([string]$text) { Write-Host ("  [warn] " + $text) -ForegroundColor Yellow }
function Die([string]$text) {
    Write-Host ""
    Write-Host ("  [FAIL] " + $text) -ForegroundColor Red
    exit 1
}

function Assert-ChildPath([string]$path, [string]$why) {
    if (-not (Test-Path -LiteralPath $path)) { Die "$why`n         缺失：$path" }
}

function Get-DirSizeMb([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if (-not $sum) { return 0 }
    return [Math]::Round($sum / 1MB, 1)
}

function Get-DirFileCount([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    return (Get-ChildItem -LiteralPath $path -Recurse -File -ErrorAction SilentlyContinue).Count
}

# 调用外部程序并**保留退出码**（PowerShell 不会自动把原生命令的非零退出码变成异常，
# 不显式检查的话，"PyInstaller 失败了但脚本继续跑到 ISCC" 会产出一个半个包）
function Invoke-Checked([string]$file, [string[]]$arguments, [string]$workDir, [string]$what) {
    if ($workDir) { Push-Location $workDir }
    try {
        & $file @arguments
        $rc = $LASTEXITCODE
    } finally {
        if ($workDir) { Pop-Location }
    }
    if ($rc -ne 0) { Die "$what 失败（退出码 $rc）" }
}

# 清掉「在打包产物目录里试跑过 exe」留下的运行残留。
#
# ⚠️ 这是**实测踩到的坑**，不是洁癖：
#    打包后直接跑过 `dist\photo-browser\photo-browser.exe` 做冒烟，exe 旁边就会多出
#    `app.ini` 与 `log\`。只要那份 payload 后来被拿去出安装包（比如单独跑一次 ISCC），
#    它们就会**一起进包**，落到 `{app}\app\app.ini`。后果不是"多几个文件"，而是
#    **配置被遮蔽**：`appIni` 的候选顺序里 `<exe目录>\app.ini` 优先于 `<exe目录>\..\app.ini`，
#    于是这份指向 dist 的残渣会**压过**安装器写的 `{app}\app.ini` ——
#    用户改了配置不生效，而且**不报错**（实测：装到 D:\apps\photobrowser 却读成 dist 路径）。
#
# ⇒ 所以这一步要挂在**两个**地方：PyInstaller 之后，以及 ISCC 之前。
#    只挂前者不够 —— `-SkipPyInstaller` 或手敲 ISCC 都会绕过它。
#    （另有一道文件级防线：installer.iss 的 [Files] 用了 Excludes，见那里的注释。）
function Remove-RuntimeLeftovers {
    $names = @('app.ini', 'log', 'db', 'thumb', 'photo', 'backup')
    foreach ($name in $names) {
        $stale = Join-Path $AppDist $name
        if (Test-Path -LiteralPath $stale) {
            Remove-Item -LiteralPath $stale -Recurse -Force
            Warn ("已清掉 dist 里的运行残留：" + $name + "（在 dist 里试跑过 exe 就会留下它）")
        }
    }
}

# ==========================================================================
Write-Host ""
Write-Host "photo-browser 打包" -ForegroundColor White
Write-Host ("  仓库    : " + $Repo)
Write-Host ("  版本    : " + $Version)
Write-Host ("  产物    : dist\photo-browser\  +  dist\PhotoBrowser-Setup-$Version.exe")

# --------------------------------------------------------------------------
Step "0/4 前置检查"

if (-not (Test-Path -LiteralPath $Py)) {
    Die "找不到 venv 解释器：$Py`n         先建一次：`n           py -3.13 -m venv code\.venv`n           code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple -r requirements.txt"
}
$pyVersion = (& $Py -c "import sys;print(sys.version.split()[0])").Trim()
Ok "venv Python $pyVersion"

# PyInstaller / Inno 都**不是运行时依赖**，不进 requirements.txt（见 R10 硬约束）。
# 所以这里必须显式检查它们是否装了，并给出可直接照抄的安装命令。
& $Py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Die "没装 PyInstaller（它只是**打包工具**，故意不进 requirements.txt）`n         装一次：`n           code\.venv\Scripts\python.exe -m pip install -i https://pypi.org/simple pyinstaller"
}
$piVersion = (& $Py -c "import PyInstaller;print(PyInstaller.__version__)").Trim()
Ok "PyInstaller $piVersion"

Assert-ChildPath $SpecFile "打包 spec 不见了"
Assert-ChildPath $IssFile  "Inno Setup 脚本不见了"

if (-not $SkipInstaller) {
    $iscc = $null
    foreach ($candidate in @(
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $iscc = $candidate; break }
    }
    if (-not $iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $iscc = $cmd.Source }
    }
    if (-not $iscc) {
        Die "没装 Inno Setup 6（只有它才能产出安装包）`n         装一次（任选其一）：`n           winget install --id JRSoftware.InnoSetup -e`n           或从 https://jrsoftware.org/isdl.php 下载 innosetup-6.x.x.exe 安装`n         只想先要 exe 目录：加 -SkipInstaller"
    }
    Ok "Inno Setup: $iscc"
    $script:Iscc = $iscc
}

# --------------------------------------------------------------------------
if (-not $SkipFrontend) {
    Step "1/4 前端构建（code\webserver）"
    if (-not (Test-Path -LiteralPath (Join-Path $WebDir "node_modules"))) {
        Warn "没装前端依赖，先跑 npm install（可能要几分钟）"
        Invoke-Checked "npm.cmd" @("install") $WebDir "npm install"
    }
    Invoke-Checked "npm.cmd" @("run", "build") $WebDir "npm run build"
    Assert-ChildPath (Join-Path $WebDist "index.html") "前端构建产物没有 index.html"
    Ok ("前端 dist：" + (Get-DirFileCount $WebDist) + " 个文件 / " + (Get-DirSizeMb $WebDist) + " MB")
} else {
    Step "1/4 前端构建（已跳过 -SkipFrontend）"
    Assert-ChildPath (Join-Path $WebDist "index.html") "跳过前端构建，但已有 dist 里没有 index.html"
    Warn "用的是**上一次** npm run build 的快照，可能落后于源码"
}

# --------------------------------------------------------------------------
if (-not $SkipPyInstaller) {
    Step "2/4 PyInstaller（--onedir）"

    if (-not $NoClean) {
        # ⚠️ 只删我们自己产出的两个目录，**绝不动**其它任何东西
        foreach ($target in @($AppDist, $Work)) {
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Recurse -Force
                Info ("已清理 " + $target)
            }
        }
    }
    New-Item -ItemType Directory -Force -Path $Dist | Out-Null

    Invoke-Checked $Py @(
        "-m", "PyInstaller", $SpecFile,
        "--distpath", $Dist,
        "--workpath", $Work,
        "--noconfirm"
    ) $Repo "PyInstaller"

    Assert-ChildPath (Join-Path $AppDist "photo-browser.exe") "PyInstaller 没产出 exe"
    Ok ("onedir 目录：" + (Get-DirFileCount $AppDist) + " 个文件 / " + (Get-DirSizeMb $AppDist) + " MB")

    # ---- 把 web\ 与 models\ 从 _internal 搬到 exe 同级 ----
    #
    # 为什么搬：安装说明里的目录布局是
    #     {app}\app\photo-browser.exe
    #     {app}\app\_internal\...
    #     {app}\app\web\
    #     {app}\app\models\buffalo_l\
    # 而 PyInstaller 6 默认把 datas 全塞进 _internal。
    # 搬到同级有两个实际好处：① 目录长得和文档一致；② 用户想单独替换前端
    # （或手工补模型）时不必钻进 _internal（那目录看起来就是"别动"）。
    # ⚠️ 两种布局 resourcePath 都能找到（exeDir 优先、_MEIPASS 兜底），
    #    所以这一段即使失败也只是布局难看，不会让包跑不起来 —— 但仍然报错，
    #    因为"数据不全"和"位置不对"是两件事，后者不该静默。
    $internal = Join-Path $AppDist "_internal"
    foreach ($name in @("web", "models")) {
        $inner = Join-Path $internal $name
        $outer = Join-Path $AppDist $name
        if (Test-Path -LiteralPath $inner) {
            if (Test-Path -LiteralPath $outer) { Remove-Item -LiteralPath $outer -Recurse -Force }
            Move-Item -LiteralPath $inner -Destination $outer
            Info ("搬出 _internal：" + $name + " -> " + $outer)
        }
    }
    Assert-ChildPath (Join-Path $AppDist "web\index.html") "前端没有落在 exe 同级或 _internal 里"
    Ok "目录布局就绪（web / models 已与 _internal 同级）"

    # ---- 清掉"在 dist 里试跑过 exe"留下的运行残留 ----
    # （理由与第二道防线见 Remove-RuntimeLeftovers 的注释）
    Remove-RuntimeLeftovers

    if (-not $KeepWork) {
        if (Test-Path -LiteralPath $Work) { Remove-Item -LiteralPath $Work -Recurse -Force }
    }
} else {
    Step "2/4 PyInstaller（已跳过 -SkipPyInstaller）"
    Assert-ChildPath (Join-Path $AppDist "photo-browser.exe") "跳过打包，但 dist\photo-browser 里没有 exe"
}

# --------------------------------------------------------------------------
if (-not $SkipInstaller) {
    Step "3/4 Inno Setup 6（安装包）"
    # ⚠️ ISCC **之前**再清一次：这条路径能走到"上一次打包留下的脏 payload"上
    #    （`-SkipPyInstaller` 复用旧 dist、或手工敲 ISCC），
    #    而脏 payload 会让 `{app}\app\app.ini` 遮蔽安装器写的配置（见函数注释）。
    Remove-RuntimeLeftovers
    Invoke-Checked $script:Iscc @("/DPbVer=$Version", $IssFile) $Repo "ISCC"
} else {
    Step "3/4 Inno Setup 6（已跳过 -SkipInstaller）"
}

# --------------------------------------------------------------------------
Step "4/4 产物"

$appMb = Get-DirSizeMb $AppDist
$modelsMb = Get-DirSizeMb (Join-Path $AppDist "models")
$webMb = Get-DirSizeMb (Join-Path $AppDist "web")
$setup = Get-ChildItem -LiteralPath $Dist -Filter "PhotoBrowser-Setup-*.exe" -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1

Write-Host ""
Write-Host "  dist 内容：" -ForegroundColor White
Get-ChildItem -LiteralPath $Dist | ForEach-Object {
    if ($_.PSIsContainer) {
        Write-Host ("    {0,-20} {1,8} MB" -f ($_.Name + "\"), (Get-DirSizeMb $_.FullName))
    } else {
        Write-Host ("    {0,-20} {1,8} MB" -f $_.Name, [Math]::Round($_.Length / 1MB, 1))
    }
}
Write-Host ""
Write-Host ("  {0,-22} {1,8} MB" -f "photo-browser\ (onedir)", $appMb)
Write-Host ("  {0,-22} {1,8} MB" -f "  其中 models\", $modelsMb)
Write-Host ("  {0,-22} {1,8} MB" -f "  其中 web\", $webMb)
if ($setup) {
    Write-Host ("  {0,-22} {1,8} MB" -f $setup.Name, [Math]::Round($setup.Length / 1MB, 1))
    Write-Host ""
    Write-Host ("  安装包：" + $setup.FullName) -ForegroundColor Green
} else {
    Warn "没找到 PhotoBrowser-Setup-*.exe"
}
Write-Host ""
Write-Host ("  用时 " + [Math]::Round($script:sw.Elapsed.TotalSeconds, 1) + " 秒")
Write-Host ""
Write-Host "  下一步（排障与验收都在 build\README.md）：" -ForegroundColor White
Write-Host "    · 裸测 onedir：把 dist\photo-browser\ 拷到一个不含空格与中文的目录，双击 exe"
Write-Host "    · 安装包测试：先造个干净目录，安装时把目标位置改到别处，看数据是否跟着走"
Write-Host "    · 未签名 exe 会触发 SmartScreen（更多信息 -> 仍要运行），这是预期行为"
