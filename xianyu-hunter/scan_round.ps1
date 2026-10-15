param(
  [string]$Device = "AADEGL4701000138",
  [string]$WiFiDevice = "192.168.31.95:5555",
  [int]$Round = 2,
  [string]$ActiveStart = "09:00",
  [string]$ActiveEnd = "23:30",
  [int]$SleepMinutes = 0,
  [string]$SessionId = "xianyu-hunter",
  [string]$Keywords = "",                # 逗号分隔；每关键词单独起一次搜索 job
  [string]$BaseDir = "E:\code\AutoTaskByMuseSpark1.3\xianyu-hunter",
  [string]$ParseScript = "C:\Users\22651\.config\opencode\skills\android-phone\scripts\parse_ui.py"
)
$ErrorActionPreference = "Continue"
$logFile = "$BaseDir\scan-log.md"
$OutDir = "$BaseDir\scans"
$LeaseTool = "$BaseDir\device-lease\devlease.py"

function Log($msg) {
  $line = "- $(Get-Date -Format 'MM-dd HH:mm:ss') R${Round}: $msg"
  Add-Content -Path $logFile -Value $line -Encoding UTF8
  Write-Output $msg
}

# 0) wait for scheduled slot
if ($SleepMinutes -gt 0) { Start-Sleep -Seconds ($SleepMinutes * 60) }

# 1) active window
$now = Get-Date
$t = $now.ToString("HH:mm")
if (($t -lt $ActiveStart) -or ($t -gt $ActiveEnd)) {
  Log "OUT_OF_WINDOW ($t), skip"
  exit 0
}

# 2) acquire device lease (multi-session coordination)
$leaseOut = python $LeaseTool acquire --device $Device --session $SessionId --ttl 1200 --task "xianyu-scan-r$Round" 2>$null
if ($LASTEXITCODE -eq 2) {
  Log "LEASE_BUSY: $leaseOut (skip this round, no fighting)"
  exit 0
}
if ($LASTEXITCODE -ne 0) {
  Log "LEASE_ERROR: $leaseOut"
  exit 1
}
Log "LEASE_OK"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$tag = "round$Round" + "_" + (Get-Date).ToString("MMdd_HHmm")

function Ensure-Device {
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { return $true }
  Log "DEVICE_STALE, recovering..."
  adb reconnect offline 2>$null | Out-Null
  adb connect $WiFiDevice 2>$null | Out-Null
  Start-Sleep 5
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { Log "RECOVERED (reconnect)"; return $true }
  $list = adb devices 2>$null | Out-String
  if ($list -match [regex]::Escape($WiFiDevice)) {
    $Device = $WiFiDevice
    Log "SWITCHED to wifi device"
    return $true
  }
  Start-Sleep 6
  $ok = adb -s $Device shell echo ok 2>$null
  if ("$ok".Trim() -eq "ok") { Log "RECOVERED (retry)"; return $true }
  Log "DEVICE_LOST"
  return $false
}

function Dump-Uixml([string]$name) {
  if (-not (Ensure-Device)) { return }
  adb -s $Device shell uiautomator dump /sdcard/ui.xml 2>$null | Out-Null
  adb -s $Device pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  $f = Get-Item "$OutDir\$name.xml" -ErrorAction SilentlyContinue
  if (-not $f -or $f.Length -lt 5000) {
    Start-Sleep 3
    if (-not (Ensure-Device)) { return }
    adb -s $Device shell uiautomator dump /sdcard/ui.xml 2>$null | Out-Null
    adb -s $Device pull /sdcard/ui.xml "$OutDir\$name.xml" 2>$null | Out-Null
  }
  python $ParseScript "$OutDir\$name.xml" > "$OutDir\$name.txt" 2>$null
}

# Tap a text node by its visible text; returns $true on hit.
function Tap-Text([string]$xmlfile, [string]$needle) {
  if (-not (Test-Path $xmlfile)) { return $false }
  $x = [IO.File]::ReadAllText($xmlfile, [Text.Encoding]::UTF8)
  $m = [regex]::Match($x, ('text="' + [regex]::Escape($needle) + '"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'))
  if ($m.Success) {
    $cx = [int](([int]$m.Groups[1].Value + [int]$m.Groups[3].Value) / 2)
    $cy = [int](([int]$m.Groups[2].Value + [int]$m.Groups[4].Value) / 2)
    adb -s $Device shell input tap $cx $cy | Out-Null
    return $true
  }
  return $false
}

# Run a single keyword search: tap home search bar, type $kw, submit, apply personal-idle filter, collect 3 screens
function Run-KeywordJob([string]$kw, [int]$idx) {
  $sub = $tag + "_k$idx"
  Log "KEYWORD $idx/$($Keywords.Count): $kw"

  # 回到首页
  adb -s $Device shell input tap 85 1521; Start-Sleep 2

  # 点首页搜索框
  Dump-Uixml "${sub}_searchpage"
  $f0 = "$OutDir\${sub}_searchpage.xml"
  $hX = $null; $hY = $null
  if (Test-Path $f0) {
    $xml0 = [IO.File]::ReadAllText($f0, [Text.Encoding]::UTF8)
    foreach ($rid in @('id/default_search','id/fy26_home_search_bar','id/search_bar_layout')) {
      $m = [regex]::Match($xml0, ('resource-id="com\.taobao\.idlefish:' + $rid + '"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'))
      if ($m.Success) {
        $hX = [int](([int]$m.Groups[1].Value + [int]$m.Groups[3].Value) / 2)
        $hY = [int](([int]$m.Groups[2].Value + [int]$m.Groups[4].Value) / 2)
        break
      }
    }
  }
  if ($hX) {
    adb -s $Device shell input tap $hX $hY; Start-Sleep 2
    # 新版闲鱼首次 tap 进的是搜索建议页（无 EditText），需要再 tap 一次中间区域激活输入态
    adb -s $Device shell input tap 320 165; Start-Sleep 3
  } else {
    Log "WARN: search bar not found"
    return
  }

  # 找 EditText (sbox 前等 1.5s 让键盘完全弹出)
  Start-Sleep 2
  Dump-Uixml "${sub}_sbox"
  $f = "$OutDir\${sub}_sbox.xml"
  $edX = $null; $edY = $null
  if (Test-Path $f) {
    $xml = [IO.File]::ReadAllText($f, [Text.Encoding]::UTF8)
    $cands = [regex]::Matches($xml, 'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    foreach ($c in $cands) {
      $x1=[int]$c.Groups[1].Value; $y1=[int]$c.Groups[2].Value
      $x2=[int]$c.Groups[3].Value; $y2=[int]$c.Groups[4].Value
      $w = $x2 - $x1
      if ($y1 -lt 260 -and $w -gt 200) {
        $edX = [int](($x1+$x2)/2); $edY = [int](($y1+$y2)/2)
        break
      }
    }
  }
  if (-not $edX) { Log "WARN: input field not found (skip kw=$kw)"; return }
  adb -s $Device shell input tap $edX $edY; Start-Sleep 2
  # 清空
  adb -s $Device shell input keyevent KEYCODE_MOVE_END; Start-Sleep 0.3
  for ($d=0;$d -lt 8;$d++) { adb -s $Device shell input keyevent KEYCODE_DEL; Start-Sleep 0.2 }

  # 真正的关键词（用 adb input text，空格用 %s，% 在 shell 会被 format 转义需 %%）
  $kwEscaped = $kw -replace ' ', '%s'
  # 关键修复：adb shell 内部 %s 会被 printf 解释成 format，需转义
  $kwForShell = $kwEscaped -replace '%', '%%'
  adb -s $Device shell input text $kwForShell; Start-Sleep 1
  adb -s $Device shell input keyevent 66; Start-Sleep 4

  # personal-idle 筛选
  Dump-Uixml "${sub}_res0"
  $rf = "$OutDir\${sub}_res0.xml"
  $persId = $null
  if (Test-Path $rf) {
    $xmlr = [IO.File]::ReadAllText($rf, [Text.Encoding]::UTF8)
    foreach ($rid in @('id/personal_idle','id/filter_personal_idle','id/personalIdle')) {
      $m = [regex]::Match($xmlr, ('resource-id="com\.taobao\.idlefish:' + $rid + '"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'))
      if ($m.Success) {
        $pX = [int](([int]$m.Groups[1].Value + [int]$m.Groups[3].Value) / 2)
        $pY = [int](([int]$m.Groups[2].Value + [int]$m.Groups[4].Value) / 2)
        adb -s $Device shell input tap $pX $pY; Start-Sleep 3
        $persId = $rid
        Log "filter personal-idle tapped ($pX,$pY, rid=$rid)"
        break
      }
    }
  }
  if (-not $persId) {
    Log "WARN: personal-idle filter not found by id; trying tap-text fallback"
    if (Tap-Text $rf "个人闲置") { Log "filter personal-idle tapped (fallback)"; Start-Sleep 3 }
    else { if (Tap-Text $rf "个人") { Log "filter 个人 tapped (fallback)"; Start-Sleep 3 } }
  }

  # 3 屏采集
  for ($i = 1; $i -le 3; $i++) {
    Dump-Uixml "${sub}_s$i"
    if ($i -lt 3) {
      if (-not (Ensure-Device)) { return }
      adb -s $Device shell input swipe 360 1300 360 500 500; Start-Sleep 3
    }
  }
}

# 3) wake + cold-restart xianyu
if (-not (Ensure-Device)) { python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null; exit 1 }
adb -s $Device shell input keyevent 224; Start-Sleep 1
adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 2
adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
Start-Sleep 12
Dump-Uixml "${tag}_health"
$h = Get-Item "$OutDir\${tag}_health.xml" -ErrorAction SilentlyContinue
if (-not $h -or $h.Length -lt 10000) {
  adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 3
  adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
  Start-Sleep 15
}
# navigate back to home feed safely (BACK never sends messages)
$atHome = $false
for ($j = 1; $j -le 5; $j++) {
  Dump-Uixml "${tag}_nav$j"
  $navContent = [IO.File]::ReadAllText("$OutDir\${tag}_nav$j.xml")
  if ($navContent -and ($navContent -match '卖闲置' -or $navContent -match '猜你喜欢')) { $atHome = $true; break }
  adb -s $Device shell input keyevent 4; Start-Sleep 2
}
if (-not $atHome) {
  adb -s $Device shell am force-stop com.taobao.idlefish; Start-Sleep 2
  adb -s $Device shell monkey -p com.taobao.idlefish -c android.intent.category.LAUNCHER 1 2>$null | Out-Null
  Start-Sleep 12
}
# dismiss app-upgrade popup if present (fresh dump)
Dump-Uixml "${tag}_popupchk"
$popContent = [IO.File]::ReadAllText("$OutDir\${tag}_popupchk.xml")
if ($popContent -match '暂不升级') {
  adb -s $Device shell input tap 211 924; Start-Sleep 2
  Log "UPGRADE_POPUP dismissed"
}

# 4) per-keyword loop
$kwList = @()
if ($Keywords) { $kwList = $Keywords -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } }
if ($kwList.Count -eq 0) {
  Log "WARN: no -Keywords provided; nothing to do"
  python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null
  exit 0
}
Log "keyword count = $($kwList.Count)"
for ($i = 0; $i -lt $kwList.Count; $i++) {
  Run-KeywordJob $kwList[$i] ($i + 1)
  if ($i -lt $kwList.Count - 1) {
    # keyword 之间：返回首页
    for ($b = 0; $b -lt 3; $b++) { adb -s $Device shell input keyevent 4; Start-Sleep 1 }
  }
}

if (-not (Ensure-Device)) { exit 1 }
adb -s $Device shell screencap -p /sdcard/s.png
adb -s $Device pull /sdcard/s.png "$OutDir\${tag}_final.png" 2>$null | Out-Null
adb -s $Device shell rm /sdcard/ui.xml /sdcard/s.png 2>$null | Out-Null
python $LeaseTool release --device $Device --session $SessionId 2>$null | Out-Null
Log "DONE $tag"

# === 三件套溯源（10-15 用户硬规则）===
$leadsMd = "$OutDir\${tag}_leads.md"
if (Test-Path $leadsMd) {
  $env:PYTHONIOENCODING = "utf-8"
  python "$BaseDir\trace_leads.py" $Round $leadsMd $Device 2>&1 | Out-String | ForEach-Object { Log "TRACE: $_" }
} else {
  Log "TRACE: no leads.md, skip"
}
