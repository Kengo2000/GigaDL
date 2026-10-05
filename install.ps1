#  GigaDL インストーラ（Windows）
#  install.bat をダブルクリックすると実行されます。
#  PCの状態を調べて、Pythonが無ければ自動で入れます。何度実行しても上書き更新されるだけです。

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Src     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Base    = Join-Path $env:LOCALAPPDATA 'GigaDL'
$AppCode = Join-Path $Base 'app'
$Venv    = Join-Path $Base 'venv'
$PyVer   = '3.12.10'

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Fail($msg) {
    Say ""
    Say "インストールできませんでした: $msg" Red
    exit 1
}

Say "== GigaDL をインストールします ==" Cyan

# ------------------------------------------------------------ PCの状態を確認
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
$winName = if ($build -ge 22000) { 'Windows 11' } else { 'Windows 10' }
try {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
} catch {
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
}
$arch = switch -Regex ($arch) { 'arm' { 'ARM64' } default { 'x64' } }
Say "PC: $winName (ビルド $build) / $arch"

if ($build -lt 17763) {
    Fail "Windows 10 バージョン1809 以降が必要です。Windows Update を行ってから再実行してください。"
}

# ------------------------------------------------------------ Python を探す
function Test-Python($exe, [string[]]$pre = @()) {
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable) if sys.version_info >= (3,9) else sys.exit(3)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path $out.Trim())) {
            # Microsoft Store の「ダミーのpython」は除外
            if ($out -notmatch 'WindowsApps') { return $out.Trim() }
        }
    } catch { }
    return $null
}

function Find-Python {
    $found = $null
    if (Get-Command py -ErrorAction SilentlyContinue) { $found = Test-Python 'py' @('-3') }
    if (-not $found -and (Get-Command python -ErrorAction SilentlyContinue)) { $found = Test-Python 'python' }
    if (-not $found) {
        $roots = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles\Python3*", "$env:ProgramFiles")
        $cands = Get-ChildItem -Path $roots -Filter python.exe -Recurse -Depth 2 -ErrorAction SilentlyContinue |
                 Where-Object { $_.Directory.Name -match '^Python3\d+' } |
                 Sort-Object { [int]($_.Directory.Name -replace '\D', '') } -Descending
        foreach ($c in $cands) {
            $found = Test-Python $c.FullName
            if ($found) { break }
        }
    }
    return $found
}

$py = Find-Python
if ($py) {
    Say "Python: 見つかりました ($py)"
} else {
    Say "Python が入っていないので、自動でインストールします…" Yellow
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Say "  winget で Python 3.12 を入れています（1〜3分）"
        & winget install -e --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Host
        $installed = ($LASTEXITCODE -eq 0)
    }
    $py = Find-Python
    if (-not $py) {
        $suffix = if ($arch -eq 'ARM64') { 'arm64' } else { 'amd64' }
        $url = "https://www.python.org/ftp/python/$PyVer/python-$PyVer-$suffix.exe"
        $exe = Join-Path $env:TEMP "python-$PyVer-$suffix.exe"
        Say "  python.org から入手しています: $url"
        try { Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing }
        catch { Fail "Python のダウンロードに失敗しました。ネット接続を確認してください。" }
        Say "  インストール中…（1〜2分）"
        $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList `
            '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0', 'Shortcuts=0'
        Remove-Item $exe -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0) { Fail "Python のインストーラがエラーで終了しました (コード $($p.ExitCode))" }
        $py = Find-Python
    }
    if (-not $py) { Fail "Python を入れましたが見つかりません。PCを再起動してから、もう一度 install.bat を実行してください。" }
    Say "Python: インストールしました ($py)" Green
}

# ------------------------------------------------------------ 配置と仮想環境
New-Item -ItemType Directory -Force -Path $AppCode | Out-Null
foreach ($f in 'engine.py', 'gigadl.py', 'platform_util.py', 'make_icon.py') {
    Copy-Item -Force (Join-Path $Src $f) $AppCode
}

$vpy  = Join-Path $Venv 'Scripts\python.exe'
$vpyw = Join-Path $Venv 'Scripts\pythonw.exe'
if (-not (Test-Path $vpy)) {
    Say "専用の実行環境を作成中…"
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "実行環境の作成に失敗しました" }
}

Say "ライブラリをインストール中…（初回は数分かかります）"
& $vpy -m pip install --upgrade pip -q --disable-pip-version-check
& $vpy -m pip install -q --disable-pip-version-check -r (Join-Path $Src 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    if ($arch -eq 'ARM64') {
        Fail "ARM版Windows向けの画面部品(PySide6)が入りませんでした。表示されたエラーを教えてください。"
    }
    Fail "ライブラリのインストールに失敗しました。表示されたエラーを教えてください。"
}

# アイコン（失敗しても動作には影響なし）
try { & $vpy (Join-Path $AppCode 'make_icon.py') $AppCode 2>$null } catch { }
$ico = Join-Path $AppCode 'GigaDL.ico'

# ------------------------------------------------------------ ショートカット
$ws = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'GigaDL.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Desktop'))  'GigaDL.lnk')
)
foreach ($lnk in $targets) {
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $vpyw
    $s.Arguments = '"' + (Join-Path $AppCode 'gigadl.py') + '"'
    $s.WorkingDirectory = $AppCode
    $s.Description = 'ギガファイル便ダウンローダー'
    if (Test-Path $ico) { $s.IconLocation = $ico }
    $s.Save()
}

Say ""
Say "インストール完了！" Green
Say "  スタートメニューとデスクトップの「GigaDL」から起動できます。"
Say "  このフォルダ（$($Src)）は削除してかまいません。"
Start-Process -FilePath $vpyw -ArgumentList ('"' + (Join-Path $AppCode 'gigadl.py') + '"') -WorkingDirectory $AppCode
