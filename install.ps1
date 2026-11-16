# Hermes-crawler 一键安装（含从零装 MediaCrawler）
#
# 用法（三种都行）：
#   1) 双击同目录「一键安装.bat」
#   2) powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
#   3) powershell ... -File install.ps1 -Target D:\MediaCrawler -RepoUrl https://github.com/NanmiCoder/MediaCrawler.git
#
# 可选参数：
#   -Target    MediaCrawler 装在哪。不填默认 %USERPROFILE%\MediaCrawler。
#              若该目录已存在且是完整 MediaCrawler，就装补丁；不存在就自动 git clone。
#   -RepoUrl   从哪 clone。默认官方仓库，国内网络慢可用镜像。
#   -DryRun    只检查、不落盘。
#   -SkipDeps  跳过「建 venv / 装依赖」这步（依赖已经装好时用，能省几分钟）。
#
# 环境变量：MC_NO_PAUSE=1 跑完不暂停（给 AI/CI 用）；MC_TARGET 同 -Target。

param(
    [string]$Target = '',
    [string]$RepoUrl = 'https://github.com/NanmiCoder/MediaCrawler.git',
    [switch]$DryRun,
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Continue'   # 不用 Stop：git 的正常输出会走 stderr，Stop 会把脚本炸掉

$BASELINE = 'ded4273a64373004f855c38b34fa8e457e9197e4'

function Say($m, $c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Ok($m) { Say "  [OK] $m" 'Green' }
function Bad($m) { Say "  [不行] $m" 'Red' }
function Info($m) { Say "  $m" }
function Step($n, $m) { Write-Host ''; Say "$n  $m" 'Cyan' }

$verFile = Join-Path $PSScriptRoot 'VERSION'
$VER = if (Test-Path -LiteralPath $verFile) { (Get-Content -LiteralPath $verFile -Raw).Trim() } else { '未知' }

# 安装包自带的源码（已含全部改造）。有它就不用连 GitHub，也不需要 git。
$BundleZip = Join-Path $PSScriptRoot 'vendor\mediacrawler-ded4273.zip'
$HasBundle = Test-Path -LiteralPath $BundleZip

Write-Host ''
Say '==================================================' 'Cyan'
Say "  Hermes-crawler 爬虫插件 —— 一键安装   版本 $VER" 'Cyan'
Say '==================================================' 'Cyan'

# ---------- 0. 前置检查：git / python ----------
Step '0/9' '先看这台电脑有没有必需的工具'

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    if ($HasBundle) {
        Ok '没装 git —— 不影响，安装包自带源码，用不着它'
    } else {
        Bad '没装 git。'
        Say '  去 https://git-scm.com/download/win 下载安装（一路下一步），装完重开这个窗口。' 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
} else {
    Ok ('git：' + (& git --version))
}

# 找 Python 3.11+（MediaCrawler 要求 >=3.11）
# 注意：探测用的 Python 代码里不能出现双引号 —— PowerShell 5.1 往 native 程序传参时
#       会把这层双引号吞掉，python 收到的就变成语法错误，于是「明明装了 python 却说没有」。
$pyExe = $null
$pyVer = $null
$pyArgs = @()
foreach ($cand in @('python', 'py -3.12', 'py -3.11', 'py')) {
    $parts = $cand.Split(' ')
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    $probe = @('-c', 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')
    if ($parts.Count -gt 1) { $probe = @($parts[1]) + $probe }
    $out = @(& $exe.Source @probe 2>$null)
    if ($LASTEXITCODE -ne 0 -or $out.Count -eq 0) { continue }
    $num = 0
    if (-not [int]::TryParse("$($out[0])".Trim(), [ref]$num)) { continue }
    if ($num -ge 311) {
        $pyExe = $exe.Source
        $pyVer = "$([math]::Floor($num / 100)).$($num % 100)"
        $pyArgs = if ($parts.Count -gt 1) { @($parts[1]) } else { @() }
        break
    }
}
if (-not $pyExe) {
    Bad '没找到 Python 3.11 以上。'
    Say '  去 https://www.python.org/downloads/windows/ 下载 3.12，安装时勾上「Add python.exe to PATH」。' 'Yellow'
    Say '  装完重开这个窗口再双击一次。' 'Yellow'
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 1
}
Ok "Python $pyVer（$pyExe）"

# ---------- 1. 决定装到哪 ----------
Step '1/9' '决定 MediaCrawler 装在哪'

if ($Target) {
    Info "你指定的：$Target"
} elseif ($env:MC_TARGET) {
    $Target = $env:MC_TARGET
    Info "环境变量 MC_TARGET：$Target"
} else {
    $Target = Join-Path $env:USERPROFILE 'MediaCrawler'
    Info "默认位置：$Target"
    Say '       （想换地方：关掉窗口，把本文件用 -Target 参数跑，或设环境变量 MC_TARGET）'
}

$Target = $Target.Trim('"').Trim()
$isExisting = $false

if (Test-Path -LiteralPath $Target) {
    $looksLikeMC = (Test-Path -LiteralPath (Join-Path $Target 'main.py')) -and
                   (Test-Path -LiteralPath (Join-Path $Target 'media_platform'))
    if ($looksLikeMC) {
        $isExisting = $true
        $Target = (Resolve-Path -LiteralPath $Target).Path
        Ok "这里已经有 MediaCrawler：$Target"
    } elseif ((Get-ChildItem -LiteralPath $Target -Force -ErrorAction SilentlyContinue | Measure-Object).Count -gt 0) {
        Bad "这个目录存在、但不像 MediaCrawler（里面有别的东西），不能覆盖：$Target"
        Say '  换个位置：-Target D:\MediaCrawler，或者把那个目录清空。' 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    } else {
        Ok "目录是空的，可以往里装：$Target"
    }
} else {
    Ok "目录还不存在，稍后自动创建：$Target"
}

# ---------- 2. 取 MediaCrawler 源码（只在需要时） ----------
Step '2/9' '取 MediaCrawler 源码'

$fromBundle = $false

if ($isExisting) {
    Info '已经有源码，跳过下载。'
} elseif ($HasBundle) {
    if ($DryRun) {
        Info "[只检查] 会用自带源码包解压到：$Target"
        Say ''
        Say '[只检查] 到此为止 —— 本次没动任何文件。' 'Green'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 0
    }
    $bundleMb = [math]::Round((Get-Item -LiteralPath $BundleZip).Length / 1MB, 1)
    Info "用安装包自带的源码（$bundleMb MB）—— 不联网、不用 git"
    try {
        Expand-Archive -LiteralPath $BundleZip -DestinationPath $Target -Force -ErrorAction Stop
    } catch {
        Bad '自带源码包解压失败，安装包可能没下完整。'
        Info $_.Exception.Message
        Say '  重新下载一份完整的安装包再试。' 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Target 'main.py'))) {
        Bad '解压完了但找不到 main.py，安装包不对劲。'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
    $fromBundle = $true
    Ok '源码已就位（自带包，已含全部改造）'
} else {
    if ($DryRun) {
        Info "[只检查] 会执行：git clone $RepoUrl `"$Target`""
        Info '[只检查] 然后切到基线版本，再打补丁。'
        Say ''
        Say '[只检查] 到此为止 —— 后面的步骤要真克隆才能检查，本次没动任何文件。' 'Green'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 0
    }
    Info "从 $RepoUrl 下载到 $Target（约 20~60 秒，看网速）"
    $parent = Split-Path -Parent $Target
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    & git clone $RepoUrl $Target 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $Target 'main.py'))) {
        Bad '下载失败。'
        Say '  常见原因：网络连不上 GitHub。换成镜像再试一次（把下面这行整段复制到 PowerShell 跑）：' 'Yellow'
        Say "    powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -RepoUrl https://ghfast.top/https://github.com/NanmiCoder/MediaCrawler.git" 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
    Ok '下载完成'
}

# ---------- 3. 核对版本 / 切基线 ----------
Step '3/9' '核对源码版本（补丁只认特定版本）'

if ($fromBundle) {
    Ok '自带源码包：版本固定、改造已内置，这步跳过'
} else {

$head = (& git -C $Target rev-parse HEAD 2>$null)
Info "当前版本：$head"

if ($head -ne $BASELINE) {
    $hasBaseline = $false
    & git -C $Target cat-file -e "$BASELINE^{commit}" 2>$null
    if ($LASTEXITCODE -eq 0) { $hasBaseline = $true }

    if ($hasBaseline) {
        Info "补丁是按 $BASELINE 生成的，正在切过去"
        if ($DryRun) {
            Info '[只检查] 会 git checkout 到基线，不落盘。'
        } else {
            $dirty = @(& git -C $Target status --porcelain 2>$null)
            if ($dirty.Count -gt 0) {
                Bad "工作区有 $($dirty.Count) 个文件被改过，不敢直接切版本。"
                Say "  先处理这些改动（git -C `"$Target`" status 看清单），或者重新干净 clone 一份。" 'Yellow'
                if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
                exit 1
            }
            & git -C $Target checkout --quiet $BASELINE 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
            if ($LASTEXITCODE -ne 0) {
                Bad '切版本失败。'
                if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
                exit 1
            }
            Ok "已切到基线 $BASELINE"
        }
    } else {
        Bad '这份源码里找不到基线版本，可能是克隆不完整或仓库换了历史。'
        Say "  删掉 $Target 重新装一次；仍然不行就把这行发我：git -C `"$Target`" log --oneline -3" 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
} else {
    Ok '版本正好是基线'
}

}

# ---------- 4. 建 venv + 装依赖 ----------
Step '4/9' '准备 Python 环境（第一次装要几分钟）'

$venvPy = Join-Path $Target '.venv\Scripts\python.exe'
$needDeps = $false
if (-not (Test-Path -LiteralPath $venvPy)) {
    $needDeps = $true
    Info '还没有 .venv，需要新建'
} else {
    $miss = @()
    foreach ($m in @('httpx', 'playwright', 'openpyxl', 'typer', 'tenacity', 'pandas')) {
        & $venvPy -c "import $m" 2>$null
        if ($LASTEXITCODE -ne 0) { $miss += $m }
    }
    if ($miss.Count -gt 0) {
        $needDeps = $true
        Info ('venv 在，但缺依赖：' + ($miss -join '、'))
    } else {
        Ok 'venv 和依赖都齐了'
    }
}

if ($needDeps) {
    if ($SkipDeps) {
        Bad '缺依赖但你选了 -SkipDeps，后面很可能跑不起来。'
    } elseif ($DryRun) {
        Info '[只检查] 会新建 .venv 并安装 requirements.txt（约 3~8 分钟）。'
    } else {
        if (-not (Test-Path -LiteralPath $venvPy)) {
            Info '新建 .venv …'
            $mkArgs = @($pyArgs) + @('-m', 'venv', (Join-Path $Target '.venv'))
            & $pyExe @mkArgs 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
            if (-not (Test-Path -LiteralPath $venvPy)) {
                Bad '建 .venv 失败。'
                if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
                exit 1
            }
            Ok '.venv 建好了'
        }
        Info '装依赖（这几分钟会很安静，别以为卡死了）…'
        $req = Join-Path $Target 'requirements.txt'
        & $venvPy -m pip install --upgrade pip --quiet 2>&1 | Out-Null

        # 国内直连 pypi 经常拿不到某些包（实测 pillow 会报 from versions: none），
        # 而且卡住时能静默等十几分钟。所以默认先走国内镜像，官方放最后兜底。
        $indexes = @(
            @{ Name = '清华镜像'; Url = 'https://pypi.tuna.tsinghua.edu.cn/simple' },
            @{ Name = '阿里云镜像'; Url = 'https://mirrors.aliyun.com/pypi/simple' },
            @{ Name = 'pypi 官方'; Url = '' }
        )
        $depsOk = $false
        foreach ($idx in $indexes) {
            Info "用 $($idx.Name) 装…"
            $pipArgs = @('-m', 'pip', 'install', '-r', $req, '--quiet')
            if ($idx.Url) {
                $pipArgs += @('-i', $idx.Url, '--trusted-host', ([Uri]$idx.Url).Host)
            }
            & $venvPy @pipArgs 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
            if ($LASTEXITCODE -eq 0) { $depsOk = $true; Ok "依赖装完（$($idx.Name)）"; break }
            Say "      $($idx.Name) 没成功，换下一个源" 'Yellow'
        }
        if (-not $depsOk) {
            Bad '三个源都装不全，基本是网络被限。'
            Say "  手动补一次看具体报错：`n    `"$venvPy`" -m pip install -r `"$req`" -i https://pypi.tuna.tsinghua.edu.cn/simple" 'Yellow'
            Say '  还不行就换网络（手机热点通常能通），再重跑本文件。' 'Yellow'
            if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
            exit 1
        }
    }
}

# ---------- 5. 试打补丁（只检查） ----------
Step '5/9' '试打一遍补丁（只检查，一个字都不改）'

$alreadyInstalled = $false

if ($fromBundle) {
    Ok '自带源码包已含全部改造，不用打补丁（这步跳过）'
    Step '6/9' '真正打补丁'
    Ok '跳过（自带包已经改好了）'
} else {

$patchDir = Join-Path $PSScriptRoot 'patches'
if (-not (Test-Path -LiteralPath $patchDir)) { $patchDir = $PSScriptRoot }
$patches = @(Get-ChildItem -LiteralPath $patchDir -Filter '*.patch' -File | Sort-Object Name)
if ($patches.Count -eq 0) {
    Bad '本目录没有 .patch 文件，安装包不完整。'
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 1
}
Info "共 $($patches.Count) 条，逐条检查…"

$pass = New-Object System.Collections.ArrayList
$fail = New-Object System.Collections.ArrayList
foreach ($p in $patches) {
    $null = & git -C $Target apply --check --whitespace=nowarn $p.FullName 2>&1
    if ($LASTEXITCODE -eq 0) { [void]$pass.Add($p.Name) } else { [void]$fail.Add($p.Name) }
}
Info "能打上：$($pass.Count) 条    打不上：$($fail.Count) 条"

if ($fail.Count -eq 0) {
    Ok '全部能打上，是全新安装'
} elseif ($pass.Count -eq 0) {
    $agentPy = Join-Path $Target 'agent_crawler\agent_crawl.py'
    if (Test-Path -LiteralPath $agentPy) {
        $alreadyInstalled = $true
        Ok '补丁一条都打不上，但项目里已有 Hermes-crawler —— 判断为「以前装过」'
        Info '跳过打补丁，只把封装层覆盖成最新版。'
    } else {
        Bad '一条都打不上，项目里也没有 Hermes-crawler。多半是源码版本差太远。'
        Say "  删掉 $Target 重新装一次最省事。" 'Yellow'
        if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
        exit 1
    }
} else {
    Bad "一半能打、一半打不上（$($pass.Count) 能 / $($fail.Count) 不能），不能硬来。"
    Info '打不上的：'
    foreach ($f in $fail) { Say "      $f" 'DarkGray' }
    Say '  说明源码被改过或版本不对。现在一个字都没落盘，你的项目没被动过。' 'Green'
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 1
}

if ($DryRun) {
    Write-Host ''
    Say '==================================================' 'Cyan'
    Say '  [只检查] 全部通过，没改动任何文件。' 'Green'
    Say '==================================================' 'Cyan'
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 0
}

# ---------- 6. 真打补丁 ----------
Step '6/9' '真正打补丁'

if ($alreadyInstalled) {
    Info '跳过（以前装过）。'
} else {
    $n = 0
    foreach ($p in $patches) {
        $null = & git -C $Target apply --whitespace=nowarn $p.FullName 2>&1
        if ($LASTEXITCODE -ne 0) {
            Bad "第 $($p.Name) 条失败了。"
            Say "  撤销全部改动：git -C `"$Target`" checkout -- ." 'Yellow'
            if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
            exit 1
        }
        $n++
    }
    Ok "打完 $n 条"
}

}

# ---------- 7. 复制封装层 ----------
Step '7/9' '复制 Hermes-crawler 封装层（命令行、MCP、说明文档、免扫码扩展）'

$nf = Join-Path $PSScriptRoot 'newfiles'
if (-not (Test-Path -LiteralPath $nf)) {
    Bad '找不到 newfiles 目录，安装包不完整。'
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 1
}
try {
    Copy-Item -Path (Join-Path $nf '*') -Destination $Target -Recurse -Force
} catch {
    Bad '复制失败，通常是文件被占用（关掉正在跑的爬虫再试）。'
    Info $_.Exception.Message
    if (-not $env:MC_NO_PAUSE) { Read-Host '按回车关闭' }
    exit 1
}
$nSrc = (Get-ChildItem -LiteralPath $nf -Recurse -File | Measure-Object).Count
$agentDst = Join-Path $Target 'agent_crawler'
$nExpect = (Get-ChildItem -LiteralPath (Join-Path $nf 'agent_crawler') -Recurse -File | Measure-Object).Count
$nGot = if (Test-Path -LiteralPath $agentDst) { (Get-ChildItem -LiteralPath $agentDst -Recurse -File | Measure-Object).Count } else { 0 }
if ($nGot -lt $nExpect) {
    Bad "封装层文件数不对：应该 $nExpect 个，实际只有 $nGot 个。可能没复制全，重试一次。"
} else {
    Ok "复制了 $nSrc 个文件（封装层 $nGot 个，已核对）"
}

# 把文档里的 %MC_ROOT% 换成真实安装路径，免得用户照着占位符抄命令
$docs = @('agent_crawler\SKILL.md', 'agent_crawler\使用说明.md', 'agent_crawler\给AI的调用说明.md', 'agent_crawler\mcp_server.py')
$patchedDocs = 0
foreach ($rel in $docs) {
    $p = Join-Path $Target $rel
    if (-not (Test-Path -LiteralPath $p)) { continue }
    # 注意：必须显式按 UTF-8 读。Windows PowerShell 5.1 的 Get-Content 默认按本地 ANSI 解码，
    #       读 UTF-8 文件会把中文注释变成乱码，写回去就成了坏文件（Python 直接语法错误）。
    $c = [System.IO.File]::ReadAllText($p, [System.Text.Encoding]::UTF8)
    if ($c -notmatch '%MC_ROOT%') { continue }
    $c = $c.Replace('%MC_ROOT%', $Target)
    [System.IO.File]::WriteAllText($p, $c, (New-Object System.Text.UTF8Encoding($false)))
    $patchedDocs++
}
Ok "文档里的路径已替换成本机位置（$patchedDocs 个文件）"

# ---------- 8. 浏览器（Playwright 内核 + 本机 Edge/Chrome） ----------
Step '8/9' '准备浏览器'

$browserFound = $null
foreach ($b in @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
)) {
    if (Test-Path -LiteralPath $b) { $browserFound = $b; break }
}
if ($browserFound) {
    Ok "找到本机浏览器：$browserFound"
    Info '爬虫默认用这台浏览器开无痕窗口，不用另下内核。'
} else {
    Bad '没找到 Edge 或 Chrome。'
    Say '  装一个 Edge（Windows 自带）或 Chrome，再重跑本文件。' 'Yellow'
}

if (-not $DryRun) {
    # Playwright 内核只在非 CDP 模式下才用得上；没装就顺手装一下，装失败不算致命。
    $pwOk = $false
    & $venvPy -c "from playwright.sync_api import sync_playwright as p; c=p().start().chromium; c.launch(); c.close()" 2>$null
    if ($LASTEXITCODE -eq 0) { $pwOk = $true }
    if ($pwOk) {
        Ok 'Playwright 浏览器内核可用'
    } else {
        Info 'Playwright 内核还没下（可选，只在特殊模式下才用），正在补…'
        & $venvPy -m playwright install chromium 2>&1 | ForEach-Object { Say "      $_" 'DarkGray' }
        if ($LASTEXITCODE -eq 0) { Ok '内核补好了' } else { Info '内核没补上，不影响默认用法（走本机浏览器）。' }
    }
}

# ---------- 9. 自检 ----------
Step '9/9' '自检：能不能真跑起来'

$agentPy = Join-Path $Target 'agent_crawler\agent_crawl.py'
if (-not (Test-Path -LiteralPath $venvPy)) {
    Bad '没有 .venv，跑不了。'
} elseif (-not (Test-Path -LiteralPath $agentPy)) {
    Bad '封装层没到位：找不到 agent_crawler\agent_crawl.py'
} else {
    Info '插件自报版本：'
    & $venvPy $agentPy version 2>&1 | ForEach-Object { Say "      $_" 'Gray' }
    Say ''
    Info '环境自检（不联网、不开浏览器）：'
    & $venvPy $agentPy status 2>&1 | ForEach-Object { Say "      $_" 'Gray' }
}

# ---------- 收尾 ----------
Write-Host ''
Say '==================================================' 'Cyan'
Say "  装完了 —— Hermes-crawler $VER" 'Green'
Say "  位置：$Target" 'Green'
Say '==================================================' 'Cyan'
Say ''
Say '接下来：' 'Cyan'
Say ''
Say '  1) 扫码登录（每个平台一次）'
Say "     `"$venvPy`" `"$agentPy`" login -p bili"
Say '     （会把浏览器窗口弹出来，扫码后自动关）'
Say ''
Say '  2) 试跑一次'
Say "     `"$venvPy`" `"$agentPy`" run -p bili -k 学习 -n 5"
Say ''
Say '  3) 接 AI 客户端（MCP）'
Say '     让 AI 读 install_for_ai.md，它会自己把配置写好。'
Say ''
Say '可选：不想每次都扫码？装个浏览器小插件（不装也能用）' 'Cyan'
Say "     双击这个文件：$Target\agent_crawler\安装免扫码插件.bat"
Say '     装一次，以后各平台都免扫码。不装的话，每个平台第一次要手机扫一次码。'
Say ''
Say "完整手册：$Target\agent_crawler\使用说明.md"
Say ''

if (-not $env:MC_NO_PAUSE) { Read-Host '装完了，按回车关闭' }
exit 0
