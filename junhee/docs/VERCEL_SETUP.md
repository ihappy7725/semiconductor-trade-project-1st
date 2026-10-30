# Vercel 배포

Vercel에서는 로그인 세션·메일 재요청 간격·업로드 원본·분석 상태/결과·통계 캐시·문의 내용을
Supabase PostgreSQL의 **비공개 `axport_server` 스키마**에 저장한다.
브라우저 쿠키에는 세션 ID만 담고, Supabase access/refresh 토큰은 기존처럼 암호화해서 보관한다.
로컬/Render는 `AXPORT_DATABASE_URL`을 비워 두면 기존 SQLite 저장 방식을 사용한다.

## 1. Supabase 준비

기존 [회원가입 설정](ACCOUNTS_SETUP.md)의 Email 인증, 메일 템플릿, SMTP 설정과
`junhee/supabase/migrations/`의 기존 SQL 두 개를 먼저 적용한다.

Supabase 대시보드 **Connect → Transaction pooler → URI**를 복사한다.
예시(실제 호스트/사용자는 대시보드에 표시된 값을 사용):

```text
postgresql://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@POOLER_HOST:6543/postgres
```

- `[YOUR-PASSWORD]`는 **데이터베이스 비밀번호**로 교체한다. API 키나 Supabase 로그인 비밀번호가 아니다.
- 비밀번호에 `@`, `#`, `:`, `/`, `%` 등이 있으면 URI 인코딩한다.
- 이 URI는 서버 전용 비밀값이다. GitHub나 프런트엔드 코드에 넣지 않는다.
- 서버가 최초 연결 시 `axport_server` 스키마와 테이블을 자동 생성한다. 스키마 생성 권한이 있는 DB 사용자가 필요하다.
- `axport_server`를 Supabase Data API의 Exposed schemas에 추가하거나 anon/authenticated 역할에 접근 권한을 부여하지 않는다.
- 풀러 호환성을 위해 prepared statements를 끄고, DB 통신은 TLS를 사용한다.

공식 문서: [Supabase DB 연결](https://supabase.com/docs/guides/database/connecting-to-postgres)

## 2. Vercel 프로젝트 가져오기

로컬 변경을 GitHub의 배포 브랜치에 커밋하고 push한 뒤 Import한다.

| 화면 항목 | 값 |
|---|---|
| Root Directory | `./` |
| Application Preset | `Flask` |
| Build Command | 기본값 `None` |
| Output Directory | 기본값 `N/A` |
| Install Command | `pip install -r requirements.txt` |

루트 `app.py`를 자동 인식한다. `vercel.json`은 함수 실행 한도를 300초로 설정하고,
`.python-version`은 Python 3.12를 선택한다. 프로젝트의 Fluid compute를 활성화한다.
`gunicorn` 시작 명령이나 `python app.py`를 Build Command에 넣지 않는다.

## 3. Environment Variables

아래 값은 Production에 등록한다. Preview에서도 로그인/분석을 쓰려면 해당 환경에도 등록한다.

```dotenv
AXPORT_DATABASE_URL=<1번에서 복사한 Transaction pooler URI>
SUPABASE_URL=https://<PROJECT_REF>.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
FLASK_SECRET_KEY=<32자 이상의 무작위 문자열>
SESSION_COOKIE_SECURE=true
AXPORT_PUBLIC_URL=https://<실제 운영 도메인>
AXPORT_DATA_MODE=demo
AXPORT_CHAT_MODE=demo
AXPORT_WIDGET_REFRESH=0
```

`FLASK_SECRET_KEY`는 아래 명령으로 한 번 생성한 뒤 고정해서 사용한다.
키를 변경하면 기존 로그인 세션의 토큰을 복호화할 수 없어 다시 로그인해야 한다.

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

선택 기능:

- 실제 AI 챗봇: `AXPORT_CHAT_MODE=live`, `OPENAI_API_KEY`, `OPENAI_MODEL` 추가.
- 분석 API: `UN_COMTRADE_API_KEY`, `KCS_TRADE_API_KEY`, `ECOS_API_KEY`, `LAW_API_KEY`.
- 실시간 위젯 제공자: `AXPORT_DATA_MODE=live`와 `.env.example`의 해당 제공자 키.
  `static/data/widgets/widgets.json`을 갱신하는 기존 상주 프로세스는 Vercel에서 실행하지 않는다.
  정적 위젯 스냅샷은 저장소에 포함된 자료를 사용한다.

## 4. 배포 주소 연결

배포 후 실제 주소를 확인해서 `AXPORT_PUBLIC_URL`에 반영하고 **Redeploy**한다.
Supabase **Authentication → URL Configuration**에도 다음을 등록한다.

- Site URL: `https://<실제 운영 도메인>`
- Redirect URLs: `https://<실제 운영 도메인>/auth/confirm`

Preview 도메인에서 인증을 테스트하려면 해당 도메인의 redirect URL도 등록하고,
Preview 환경의 `AXPORT_PUBLIC_URL`을 그 주소에 맞춘다.

## 5. 동작 확인과 제한

홈 → 회원가입 → 인증 메일 → 로그인 → 샘플 분석 → 파일 업로드/다운로드 → 보고서 → 로그아웃 순으로 확인한다.
다시 배포한 뒤 재로그인해서 업로드 원본과 분석 결과를 열어 지속 저장 여부도 확인한다.

- Vercel 요청/응답 본문 제한(4.5MB)에 여유를 두어 **파일 한도는 4MB**로 표시하고 검사한다.
  로컬/Render는 기존 20MB다. 4MB보다 큰 파일은 별도 직접 업로드 방식이 필요하다.
- 분석은 요청 안에서 실행한다. 요청이 끝난 뒤 백그라운드 스레드에 남겨 두지 않는다.
  Hobby 함수 한도는 300초이며, 외부 통계 API 응답에 따라 초과할 수 있다.
  이 경우 재시도하거나 긴 분석을 처리할 별도 작업 서버/큐가 필요하다.
- 프로세스 ID로 다른 인스턴스의 분석을 중단 처리하지 않는다.
  강제 종료된 분석은 기존 15분 만료 규칙으로 실패 처리된다.
- 기존 로컬 SQLite/Render 디스크의 데이터는 자동 이전하지 않는다.
  Supabase에 이미 저장된 바탕화면·완료 보고서는 기존 저장소에서도 조회한다.
- 실제 DB URI·키가 없으면 Vercel 정상 구동을 검증할 수 없다. 배포 실패 시 Vercel Logs에서 확인한다.

공식 문서: [Flask 배포](https://vercel.com/docs/frameworks/backend/flask),
[함수 실행·본문 크기 제한](https://vercel.com/docs/functions/limitations)

## 개발 검증

```powershell
python -m unittest discover -s junhee/tests -t . -v
```

별도 임시 PostgreSQL이 있으면 `AXPORT_TEST_DATABASE_URL`에 **폐기 가능한 로컬 DB URI**를 넣고
`python -m unittest junhee.tests.test_vercel -v`로 인스턴스 간 세션/파일/결과/삭제 저널을 검증할 수 있다.
이 테스트는 로컬 DB의 TLS를 끄므로 운영 Supabase에 실행하지 않는다.
