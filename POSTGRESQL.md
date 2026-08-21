# PostgreSQL 운영 메모

이 프로젝트의 운영 데이터베이스는 로컬 PostgreSQL 17의 `kis_trading` 데이터베이스다.
연결 정보는 Git에서 제외된 `.env`의 `DATABASE_URL`에만 저장한다.

## 연결 확인

```powershell
python -B -c "from trading_lab import store; print(store.repository.count_stocks())"
```

## 애플리케이션 실행

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 백업

PostgreSQL 설치 디렉터리의 `pg_dump.exe`를 사용한다. 비밀번호를 명령행에 직접 넣지 말고
`.env`의 접속 정보를 안전하게 읽어 환경변수 또는 비밀번호 파일로 전달한다.

```powershell
& 'C:\Program Files\PostgreSQL\17\bin\pg_dump.exe' `
  --host 127.0.0.1 --port 5432 --username kis_trading `
  --format custom --file kis_trading.backup kis_trading
```

## SQLite 원본

`data/trading_lab.db`는 전환 시점의 복구 원본으로 보존한다. 애플리케이션은
`DATABASE_URL`이 설정되어 있으면 PostgreSQL을 사용하고, 테스트에서 명시적으로 임시 DB
경로만 넘긴 경우에는 SQLite를 사용한다.

## 마이그레이션 검증

`migrate_sqlite_to_postgres.py`는 대상 PostgreSQL DB가 비어 있을 때만 실행된다. 대상에
데이터가 있으면 중단하므로 운영 데이터를 덮어쓰지 않는다. 마이그레이션 후 모든 테이블의
SQLite/PostgreSQL 행 수가 일치해야 성공으로 처리된다.
