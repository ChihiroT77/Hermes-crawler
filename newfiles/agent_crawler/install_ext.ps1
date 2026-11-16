# 爬虫登录票导出插件 —— 安装脚本（双击 安装免扫码插件.bat 会调到这里）
# 做的事：把插件复制到一个固定位置 → 打开 Edge 扩展页 → 打开插件文件夹
# 为什么复制：直接指向项目目录的话，你以后挪动项目文件夹，插件就会失效报错。

$ErrorActionPreference = 'Stop'

$src = Join-Path $PSScriptRoot 'edge_cookie_ext'
$dst = Join-Path $env:LOCALAPPDATA 'MediaCrawler免扫码插件\edge_cookie_ext'

Write-Host ''
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host '  爬虫登录票导出插件 —— 安装' -ForegroundColor Cyan
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host ''

if (-not (Test-Path (Join-Path $src 'manifest.json'))) {
    Write-Host '[出错] 没找到插件本体。' -ForegroundColor Red
    Write-Host "       这个脚本必须和 edge_cookie_ext 文件夹放在一起。"
    Write-Host "       当前位置：$PSScriptRoot"
    Write-Host ''
    Read-Host '按回车关闭'
    exit 1
}

Write-Host '正在把插件复制到固定位置（以后你挪动项目文件夹也不会失效）...'
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
New-Item -ItemType Directory -Path $dst -Force | Out-Null

try {
    Copy-Item (Join-Path $src '*') -Destination $dst -Recurse -Force
} catch {
    Write-Host ''
    Write-Host '[出错] 复制失败，通常是插件文件被占用。请先关掉 Edge 再双击一次。' -ForegroundColor Red
    Write-Host "       详情：$($_.Exception.Message)"
    Write-Host ''
    Read-Host '按回车关闭'
    exit 1
}

# 复制完核对一下：装过去的文件数要和源一致，别装了个残缺的
$nSrc = (Get-ChildItem $src -Recurse -File).Count
$nDst = (Get-ChildItem $dst -Recurse -File).Count
if ($nSrc -ne $nDst) {
    Write-Host "[出错] 复制不完整：源 $nSrc 个文件，目标 $nDst 个。" -ForegroundColor Red
    Read-Host '按回车关闭'
    exit 1
}
Write-Host "  已复制 $nDst 个文件到：" -ForegroundColor Green
Write-Host "  $dst"
Write-Host ''

# 自检用：设了 MC_NO_OPEN=1 就只复制，不弹浏览器
if ($env:MC_NO_OPEN -eq '1') {
    Write-Host '[自检模式] 已跳过打开浏览器。'
    exit 0
}

Write-Host '接下来会打开两个窗口：Edge 的扩展管理页、插件的文件夹。'
Write-Host '如果 Edge 没有自动打开，就自己打开 Edge，在地址栏输入：edge://extensions'
Write-Host ''
Write-Host '在扩展管理页里做三步：'
Write-Host '  第 1 步  打开「开发人员模式」开关（页面右上角或左下角）'
Write-Host '  第 2 步  点「加载解压缩的扩展」'
Write-Host '  第 3 步  在弹出的窗口里选中刚打开的那个文件夹，点「选择文件夹」'
Write-Host ''
Write-Host '装好后工具栏会出现一个蓝色小人图标，就成功了。' -ForegroundColor Green
Write-Host '这一步只需要做一次。以后插件更新了，重新双击本文件，再在扩展页点一下「重新加载」。'
Write-Host ''

Start-Process 'microsoft-edge:edge://extensions' -ErrorAction SilentlyContinue
Start-Process 'explorer.exe' -ArgumentList "`"$dst`"" -ErrorAction SilentlyContinue

Write-Host '装完按回车关闭这个窗口。'
Read-Host | Out-Null
