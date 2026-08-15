param(
    [string]$Repo = "carpetcrawler78/jobsearch-ntfy",
    [Parameter(Mandatory = $true)]
    [string]$ServiceAccountJsonPath
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI (gh) is not installed or not in PATH."
}

gh auth status

$jsonPath = (Resolve-Path $ServiceAccountJsonPath).Path
$json = Get-Content -Raw -Encoding UTF8 $jsonPath
$serviceAccount = $json | ConvertFrom-Json

if (-not $serviceAccount.client_email) {
    throw "The JSON file does not contain client_email."
}

$secureTopic = Read-Host "Enter the new long random ntfy topic" -AsSecureString
$topicPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureTopic)
try {
    $topic = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($topicPointer)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($topicPointer)
}

if ([string]::IsNullOrWhiteSpace($topic)) {
    throw "NTFY_TOPIC cannot be empty."
}

$json | gh secret set GOOGLE_SERVICE_ACCOUNT_JSON --repo $Repo
$topic | gh secret set NTFY_TOPIC --repo $Repo

Write-Host "Secrets configured."
Write-Host "Share Jobs_Masterliste as Editor with this service-account address:"
Write-Host $serviceAccount.client_email
Write-Host "After sharing the sheet, start the safe smoke test with:"
Write-Host "gh workflow run daily-ntfy.yml --repo $Repo -f mode=smoke"
Write-Host "Then test the empty/current outbox with:"
Write-Host "gh workflow run daily-ntfy.yml --repo $Repo -f mode=outbox"
