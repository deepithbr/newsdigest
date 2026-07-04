param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath ".git")) {
    throw "Run this from the news-brief-cloud repository folder."
}

$existingOrigin = git remote get-url origin 2>$null
if ($LASTEXITCODE -eq 0 -and $existingOrigin) {
    git remote set-url origin $RepoUrl
}
else {
    git remote add origin $RepoUrl
}

git push -u origin main

Write-Host ""
Write-Host "Pushed to GitHub."
Write-Host "Next: open the repository on GitHub, go to Settings > Pages, and set Source to GitHub Actions."
Write-Host "Then run Actions > Daily News Brief > Run workflow once."
