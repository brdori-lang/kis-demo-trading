# 프로젝트 실행 환경

이 프로젝트는 저장소 안의 `myapi` Python 가상환경을 사용한다. 전역 Python으로 서버나
테스트를 실행하지 않는다.

## 최초 구성

```powershell
python -m venv myapi
& .\myapi\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
```

PowerShell 실행 정책 때문에 `Activate.ps1`이 차단되더라도 가상환경 Python을 직접 호출하면
동일하게 격리된 환경으로 실행된다.

## 서버 실행

```powershell
.\run_server.ps1
```

또는 다음 명령을 직접 사용할 수 있다.

```powershell
& .\myapi\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 테스트

```powershell
.\run_tests.ps1
```

## 확인

```powershell
& .\myapi\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.prefix != sys.base_prefix)"
```

두 번째 출력이 `True`이면 가상환경 Python이다.
# 보안 기본값 (2026-08-27)

- API 입력은 정의되지 않은 필드, 잘못된 종목코드, 비정상 수치와 허용 범위를 벗어난 값을 거부합니다.
- 토큰 캐시는 프로젝트 `data` 디렉터리의 고정 경로에만 저장합니다.
- 외부 통신은 한국투자증권 HTTPS 호스트 allowlist와 공인 IP 검증을 통과해야 합니다.
- 백테스터는 내장된 `LAB Strategy v1`만 직접 호출하며 사용자 제공 코드를 생성하거나 실행하지 않습니다.
