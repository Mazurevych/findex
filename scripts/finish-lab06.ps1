# Лаби 5-6: кроки, які можна зробити лише на власному комп'ютері.
#
#   cd $HOME\Documents\findex
#   git pull
#   powershell -ExecutionPolicy Bypass -File scripts\finish-lab06.ps1
#
# Що робить: оновлює uv.lock (нові залежності httpx і matplotlib) і комітить його,
# проганяє тести і перевірку типів, ставить теги lab-05 / lab-06, оновлює команду findex.

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

Step "Залежності (оновлює uv.lock: додано httpx і matplotlib)" { uv sync }

Step "Тести (без повільних)" { uv run pytest -q -m "not slow" }

Step "Перевірка типів і стилю" {
    uv run pyright
    if ($LASTEXITCODE -eq 0) { uv run ruff check . }
}

Step "Коміт uv.lock" {
    git add uv.lock
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -m "Update uv.lock for labs 5-6 (httpx, matplotlib)"
    } else {
        $global:LASTEXITCODE = 0
    }
}

Step "Відправка на GitHub" { git push }

Step "Теги" {
    git tag -f lab-05
    git tag -f lab-06
    git push -f origin lab-05 lab-06
}

Step "Оновлення команди findex (тепер з crawl)" { uv tool install --force --reinstall . }

Write-Host ""
Write-Host "Готово! Лаби 5 і 6 на GitHub з тегами lab-05 і lab-06." -ForegroundColor Green
Write-Host "Відкрийте нове вікно PowerShell, щоб команда findex crawl була доступна."
