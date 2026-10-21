$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$src = 'C:\Users\Administrator\Downloads\Shadows of Prestige Academy'
$prompt = 'C:\Users\Administrator\AppData\Local\Temp\E16E21_inspect_20261022001022\E16-E21\E16-E21_storyboard_integrated.md'
$stage = Join-Path $repo 'private\production_packages\A4_E16-E21_HMS'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item (Join-Path $stage 'prompt') -ItemType Directory -Force | Out-Null
New-Item (Join-Path $stage 'assets') -ItemType Directory -Force | Out-Null
Copy-Item $prompt (Join-Path $stage 'prompt\E16-E21_storyboard_integrated.md')
$text = Get-Content $prompt -Raw
$ids = [regex]::Matches($text, '\b(?:R\d{2}(?:-V\d{2})?|P\d{2}|S\d{2})\b') | ForEach-Object Value | Sort-Object -Unique
$inputDir = 'D:\cm\ComfyUI_windows_portable\ComfyUI\input'
$manifest = [ordered]@{}
foreach ($id in $ids) {
  if ($id -eq 'S35') { $candidates = @(Get-ChildItem $inputDir -File | Where-Object Name -like 'S35*' | Select-Object -First 1) }
  elseif ($id -eq 'S36') { $candidates = @(Get-ChildItem $inputDir -File | Where-Object { $_.Name -like 'S36_MORGANA_OUTER_WALL*' } | Select-Object -First 1) }
  else {
    $candidates = @(Get-ChildItem $src -Recurse -File | Where-Object { $_.BaseName -match ('^' + [regex]::Escape($id) + '(?:_|$)') })
    if ($candidates.Count -gt 1) {
      $preferred = @($candidates | Where-Object BaseName -match 'v02')
      if ($preferred.Count -eq 0) { $preferred = @($candidates | Where-Object BaseName -match '_v01') }
      if ($preferred.Count -gt 0) { $candidates = @($preferred[0]) }
    }
  }
  if ($candidates.Count -ne 1) { throw "$id candidates: $($candidates.FullName -join ', ')" }
  $dest = $id + '__' + $candidates[0].Name
  Copy-Item -LiteralPath $candidates[0].FullName -Destination (Join-Path $stage "assets\$dest")
  $manifest[$id] = "assets/$dest"
}
$manifest | ConvertTo-Json | Set-Content (Join-Path $stage 'asset_manifest.json') -Encoding UTF8
$zip = Join-Path $repo 'private\production_packages\A4_E16-E21_HMS_assets.zip'
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -CompressionLevel Optimal
Write-Output "stage=$stage"
Write-Output "zip=$zip"
Write-Output ($ids -join ', ')
