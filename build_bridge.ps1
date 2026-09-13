# Собирает smbridge.exe как самостоятельный 32-битный (win-x86) исполняемый
# файл. Нужен .NET SDK (проверялось на 8.0+; у вас уже стоит 10.0 — тоже подойдёт,
# он умеет собирать проекты под net8.0).
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File build_bridge.ps1
#
# Результат: bridge\publish\smbridge.exe

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$proj = Join-Path $root "bridge\Bridge.csproj"
$out  = Join-Path $root "bridge\publish"

Write-Host "Сборка моста (win-x86, self-contained)..." -ForegroundColor Cyan

dotnet publish $proj `
    -c Release `
    -r win-x86 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -o $out

if ($LASTEXITCODE -ne 0) {
    Write-Host "Сборка не удалась." -ForegroundColor Red
    exit 1
}

$exe = Join-Path $out "smbridge.exe"
if (Test-Path $exe) {
    Write-Host "Готово: $exe" -ForegroundColor Green
} else {
    Write-Host "smbridge.exe не найден после публикации — проверьте вывод выше." -ForegroundColor Red
    exit 1
}
