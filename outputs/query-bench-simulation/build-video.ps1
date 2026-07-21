param([string]$Voice = 'Microsoft Zira Desktop')
$ErrorActionPreference = 'Stop'
$out = Split-Path -Parent $MyInvocation.MyCommand.Path
$html = Join-Path $out 'simulation.html'
$frames = Join-Path $out 'frames'
$audio = Join-Path $out 'audio'
$segments = Join-Path $out 'segments'
New-Item -ItemType Directory -Force -Path $frames,$audio,$segments | Out-Null

$chromeCandidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw 'Google Chrome was not found.' }

$dep = Join-Path $env:TEMP 'query-bench-video-deps'
$ffmpeg = python -c "import sys; sys.path.insert(0, r'$dep'); import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
if (-not (Test-Path $ffmpeg)) { throw 'FFmpeg runtime was not found.' }

$lines = Get-Content -Raw (Join-Path $out 'narration.json') | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
for ($i=1; $i -le $lines.Count; $i++) {
  $png = Join-Path $frames ("scene-{0:00}.png" -f $i)
  $wav = Join-Path $audio ("scene-{0:00}.wav" -f $i)
  $seg = Join-Path $segments ("scene-{0:00}.mp4" -f $i)
  $url = ([uri]$html).AbsoluteUri + "?scene=$i"
  $profile = Join-Path $env:TEMP ("query-bench-video-chrome-{0}-{1}" -f $PID,$i)
  New-Item -ItemType Directory -Force -Path $profile | Out-Null
  & $chrome --headless=new --incognito --no-first-run --disable-gpu --hide-scrollbars --user-data-dir=$profile --window-size=1920,1080 --force-device-scale-factor=1 --virtual-time-budget=500 --screenshot=$png $url | Out-Null
  if (-not (Test-Path $png)) { throw "Failed to render scene $i" }
  $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $synth.SelectVoice($Voice)
  $synth.Rate = 0
  $synth.Volume = 100
  $synth.SetOutputToWaveFile($wav)
  $synth.Speak([string]$lines[$i-1])
  $synth.Dispose()
  & $ffmpeg -y -loop 1 -i $png -i $wav -af "apad=pad_dur=0.65" -vf "fade=t=in:st=0:d=0.25,fade=t=out:st=20:d=0.25,format=yuv420p" -c:v libx264 -preset medium -tune stillimage -r 30 -c:a aac -b:a 192k -shortest $seg | Out-Null
}
$list = Join-Path $segments 'concat.txt'
$segmentLines = 1..$lines.Count | ForEach-Object { "file 'scene-{0:00}.mp4'" -f $_ }
Set-Content -Path $list -Value $segmentLines -Encoding ascii
$final = Join-Path $out 'query-bench-simulation.mp4'
& $ffmpeg -y -f concat -safe 0 -i $list -c copy -movflags +faststart $final | Out-Null
if (-not (Test-Path $final)) { throw 'Final video was not created.' }
Get-Item $final | Select-Object FullName,Length,LastWriteTime
