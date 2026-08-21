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
