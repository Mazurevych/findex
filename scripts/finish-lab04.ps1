# Лаби 2-4: кроки, які можна зробити лише на власному комп'ютері.
#
#   cd $HOME\Documents\findex
#   git pull
#   powershell -ExecutionPolicy Bypass -File scripts\finish-lab04.ps1
#
# Що робить: створює uv.lock і комітить його, проганяє тести, будує індекс для демо,
# збирає wheel, ставить теги lab-02 / lab-03 / lab-04 / v0.4.0 і встановлює команду findex.

function Step($title, [scriptblock]$action) {
    Write-Host ""
    Write-Host "==> $title" -ForegroundColor Cyan
    & $action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Помилка на кроці: $title" -ForegroundColor Red
        exit 1
    }
}

$LAB2_COMMIT = "4493983"   # коміт лаби 2 — на нього ставимо тег lab-02

Step "Ім'я автора для комітів (лише для цього репозиторію)" {
    if (-not (git config user.name)) { git config user.name "Yakiv Mazurevych" }
    if (-not (git config user.email)) { git config user.email "348002754+Mazurevych@users.noreply.github.com" }
}

Step "Оновлення коду з GitHub" { git pull --ff-only }

Step "Встановлення залежностей (створює uv.lock)" { uv sync }

Step "Тести" { uv run pytest -q -m "not slow" }

Step "Перевірка типів і стилю" {
    uv run pyright
    if ($LASTEXITCODE -eq 0) { uv run ruff check . }
}

Step "Коміт uv.lock" {
    git add uv.lock
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -m "Add uv.lock (exact dependency versions for CI)"
    } else {
        $global:LASTEXITCODE = 0   # uv.lock уже закомічено
    }
}

Step "Відправка на GitHub" { git push }

Step "Індекс для демо (з позиціями, ~1 хв)" {
    uv run findex index data/ --out index.bin --positions
}

Step "Збирання wheel" { uv build }

Step "Теги" {
    git tag -f lab-02 $LAB2_COMMIT
    git tag -f lab-03
    git tag -f lab-04
    git tag -f v0.4.0
    git push -f origin lab-02 lab-03 lab-04 v0.4.0
}

Step "Встановлення команди findex з wheel" {
    uv tool install --force (Get-ChildItem dist\findex-0.4.0-py3-none-any.whl).FullName
}

Write-Host ""
Write-Host "Готово! Wheel для релізу: $((Get-ChildItem dist\*.whl).FullName)" -ForegroundColor Green
Write-Host "Відкрийте нове вікно PowerShell, і команда findex буде доступна з будь-якої папки."
