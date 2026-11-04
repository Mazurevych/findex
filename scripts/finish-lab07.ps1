# Лаба 7: кроки на власному комп'ютері. Два запуски:
#
#   1) ДО розгортання — оновлює uv.lock (FastAPI, uvicorn, jinja2, pydantic-settings) і пушить:
#        powershell -ExecutionPolicy Bypass -File scripts\finish-lab07.ps1
#
#   2) ПІСЛЯ розгортання на Render — навантажувальний тест oha, адреса в README, теги:
#        powershell -ExecutionPolicy Bypass -File scripts\finish-lab07.ps1 -Url https://findex-xxxx.onrender.com

param([string]$Url = "")

function Step($title, [scriptblock]$action) {
    Write-Host ""
    Write-Host "==> $title" -ForegroundColor Cyan
    & $action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Помилка на кроці: $title" -ForegroundColor Red
        exit 1
    }
}

Step "Ім'я автора для комітів (лише для цього репозиторію)" {
    if (-not (git config user.name)) { git config user.name "Yakiv Mazurevych" }
    if (-not (git config user.email)) { git config user.email "348002754+Mazurevych@users.noreply.github.com" }
}

Step "Оновлення коду з GitHub" { git pull --ff-only }

if ($Url -eq "") {
    Step "Залежності (оновлює uv.lock)" { uv sync }
    Step "Тести (без повільних)" { uv run pytest -q -m "not slow" }
    Step "Коміт uv.lock" {
        git add uv.lock
        git diff --cached --quiet
        if ($LASTEXITCODE -ne 0) { git commit -m "Update uv.lock for lab 7 (FastAPI)" } else { $global:LASTEXITCODE = 0 }
    }
    Step "Відправка на GitHub" { git push }
    Write-Host ""
    Write-Host "Готово (етап 1). Тепер розгорніть сайт на Render і запустіть скрипт ще раз з -Url." -ForegroundColor Green
    exit 0
}

Step "Програма oha для навантажувального тесту" {
    if (-not (Get-Command oha -ErrorAction SilentlyContinue)) {
        # готовий .exe з релізів github.com/hatoo/oha — у ту саму папку, куди uv ставить findex
        $bin = Join-Path $env:USERPROFILE ".local\bin"
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri "https://github.com/hatoo/oha/releases/latest/download/oha-windows-amd64.exe" -OutFile (Join-Path $bin "oha.exe")
        $env:Path = "$bin;$env:Path"
    }
    oha --version
}

Step "Навантажувальний тест задеплоєного сайту (30 с)" {
    uv run python scripts/loadtest.py remote $Url
}

Step "Коміт результатів і адреси сайту" {
    git add README.md docs/lab07/loadtest.json
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) { git commit -m "Lab 7: live URL and deployed load test" } else { $global:LASTEXITCODE = 0 }
}

Step "Відправка на GitHub" { git push }

Step "Теги" {
    git tag -f lab-07
    git tag -f v0.7.0
    git push -f origin lab-07 v0.7.0
}

Write-Host ""
Write-Host "Готово! Лаба 7 на GitHub з тегом lab-07, живий сайт: $Url" -ForegroundColor Green
