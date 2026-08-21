$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot 'myapi\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '가상환경이 없습니다. 먼저 python -m venv myapi 를 실행하세요.'
}
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 @args
