# eclass-cli 분석 메모

| 항목 | 내용 |
|---|---|
| 저장소 | github.com/pinion05/eclass-cli |
| 분석 커밋 | `e966b46` (2026-04-07) |
| 언어 | TypeScript, Playwright, cheerio, zod |
| 라이선스 | MIT (2026-09-17 사용자 확인). `package.json`과 README에 MIT 표기, LICENSE 파일은 없음 |
| 적용 위치 | `app/collectors/eclass` |
| 참고 범위 | SSO 로그인 흐름, 수강과목·할 일(과제) 목록 파싱, 계층 구조 |

> 코드를 옮기거나 Python으로 번역해 가져오면 `THIRD_PARTY_NOTICES.md`에 출처와 MIT 전문을 기록한다.

## 풀고 있는 문제

ilos(지누스) 기반 e-Class에는 공개 API가 없다. 로그인도 학교 포털 SSO를 거쳐야 해서 단순 HTTP 요청만으로는 세션을 얻기 어렵다.
eclass-cli는 headless 브라우저로 로그인한 뒤, 같은 브라우저 세션 안에서 페이지를 요청하고 HTML을 파싱해 과목·과제·강의자료를 터미널에서 다룬다.

## 핵심 아이디어

1. **브라우저는 로그인과 세션 유지만 맡는다.** 데이터는 목록 페이지 HTML을 받아 파서로 뽑는다. 구조는 전송 계층(BrowserClient)과 도메인 서비스(Course/Assignment/Material)로 나뉜다.
2. **SSO는 URL 도달 여부로 단계마다 성공을 판정한다.** 포털 로그인 → 포털 학생 메인 URL 대기 → e-Class SSO 진입 → e-Class 메인 URL 대기. 단계마다 타임아웃은 15초다.
3. **할 일 목록 한 번으로 전 과목 마감을 수집한다.** "todo 목록" 엔드포인트에 카테고리 ALL로 POST하면 과제·시험·온라인 강의·프로젝트가 한 페이지에 모인다. 과목별로 돌 필요가 없다.
4. **과목 강의실 진입은 서버 쪽 상태다.** 페이지의 JS 함수를 호출하면 서버 세션에 "현재 과목"이 설정된다. 따라서 과목별 페이지는 한 세션 안에서 **순차로** 처리해야 한다.
5. **식별자는 onclick 문자열에서 정규식으로 뽑는다.** 과목 키(KJKEY), 과제 번호(RT_SEQ), 게시글 번호(ARTL_NUM)가 여기에 해당한다.
6. **파싱 결과를 스키마(zod)로 검증한다.** 사이트 구조가 바뀌면 조용히 틀린 값이 나오는 대신 검증에서 실패한다.
7. POST 요청은 페이지 안에서 `fetch`를 실행해 쿠키를 자동으로 싣는다.

## 관찰된 엔드포인트·선택자

2026-04 커밋 기준이다. 6단계에서 실제 사이트로 다시 확인해야 한다.

| 용도 | 경로 | 파싱 포인트 |
|---|---|---|
| 포털 SSO 로그인 | `https://<학교 포털 SSO 호스트>/sso/login_stand.jsp` | 입력 `#internalId`, `#internalPw`, 버튼 `#internalLogin`. 성공 판정 URL `**/portal/default/stu**` |
| e-Class SSO 진입 | `/ilos/sso/index.jsp` | 성공 판정 URL `**/ilos/main/main_form.acl` |
| 수강과목 목록 | `/ilos/mp/course_register_list_form.acl` | `.content-container` 단위, `.content-title`(과목명), `.content-author li` 1번째(교수)·2번째(시간), onclick `eclassRoom('KJKEY')` |
| 할 일 목록 | `POST /ilos/mp/todo_list.acl` (`todoKjList=''`, `chk_cate=ALL`, `encoding=utf-8`) | `.todo_wrap` 단위, `.todo_subjt`(과목), `.todo_title`, `.todo_d_day`, `.todo_date`(마지막 요소가 마감), `input[id^=gubun_]`(유형), `input[id^=kj_]`(과목 키), onclick `goLecture(KJKEY, SEQ, CATEGORY)` |
| 강의실 진입 | 수강과목 페이지에서 `eclassRoom(kjkey)` 실행 | 도달 URL `**/ilos/st/course/submain_form.acl` |
| 강의계획서 | `/ilos/st/course/plan_form.acl?lecture_id=KJKEY` | 첫 표: 교수·이메일·강의시간·학점·개요·평가. 둘째 표: 주차별 계획 |
| 강의자료 목록 | `/ilos/st/course/lecture_material_list_form.acl` (강의실 진입 후) | `.subjt_top`(제목), `.subjt_bottom`(작성자·조회수), `.unread_article`(안 읽음), onclick `pageMove(...ARTL_NUM=...)` |

- 마감 날짜 형식: `2026.04.08 오후 11:59` (오전/오후 표기)
- 할 일 유형(gubun) 매핑: report → 과제, test/quiz → 시험, project → 프로젝트, 그 외 → 온라인 강의(lecture_weeks)

## 데이터 구조

- `Course { name, professor, time, kjkey }`
- `Assignment { title, course, category(report|test|lecture_weeks|project), dDay, deadline(문자열), status(진행중|종료), kjkey, seq }`
- `CourseDetail { name, professor, email, time, credits, overview, grading, weeklyPlan[{week, content}], kjkey }`
- `Material { title, author, views, publishDate, hasAttachment, isRead, artlNum }`
- 설정: `{ id, pw, university }`. 환경변수가 우선이고, 없으면 `~/.eclass-cli/config.json`을 읽는다.

## 인터페이스

- `BrowserClient`: `launch()`, `login(config)`, `getHtml(url)`, `postHtml(url, data)`, `enterCourseRoom(kjkey)`, `downloadFile()`, `uploadFiles()`, `close()`
- `CourseService.listCourses()`, `getCourseDetail(name)`
- `AssignmentService.listAssignments(courseFilter?)`, `submit(seq, files)`
- `MaterialService.listMaterials(course)`, `download(artlNum)`
- `createAppContext()`: 설정 읽기 → 브라우저 실행 → 로그인(실패하면 브라우저 닫기) → 서비스 조립

## 가져올 것 (우리 구조에 맞게 Python으로 새로 작성)

- **브라우저 계층과 파서 분리.** 파서는 HTML 문자열을 받는 순수 함수로 만든다. 그러면 저장해 둔 HTML 샘플로 로그인 없이 테스트할 수 있다.
- **SSO 단계별 URL 대기.** 어느 단계에서 멈췄는지를 실패 유형 분류에 쓴다.
- **할 일 목록 한 번 호출로 과제·시험·온라인 강의 기한 수집.**
  - 결과는 `Event(source="eclass", kind=...)` 목록으로 반환한다.
  - 중복 방지 ID는 `ref_id = "eclass:{category}:{kjkey}:{seq}"` 형태로 만든다.
- **과목별 페이지는 순차 처리.** 서버 세션 상태 때문이다.
- **파싱 결과 검증.** 필수 필드가 비었거나 결과가 비정상적으로 0건이면 "사이트 구조 변경 의심"으로 보고한다.

## 버릴 것 / 다르게 할 것

| eclass-cli | 우리 프로젝트 | 이유 |
|---|---|---|
| 과제 제출, 파일 업로드, 에디터 이미지 삽입 | 만들지 않음 | 절대 규칙 5, 조회 기능만 만든다 |
| 강의자료 다운로드 | 만들지 않음 | 역할 범위 밖 |
| CLI(commander), 표 출력 | 없음. 수집기는 `list[Event]`만 반환 | 수집기는 모델·출력과 분리한다 |
| 홈 디렉토리 설정 파일에 평문 비밀번호 저장 | `private/.env`에만 저장. 수집기 안에서만 읽고 로그·예외 메시지·Event에 넣지 않음 | 절대 규칙 6·7 |
| 실행할 때마다 새로 로그인 | Playwright 세션 상태를 `private/`에 저장해 재사용하고, 만료됐을 때만 재로그인 | 로그인 횟수를 줄여 계정 잠금·추가 인증 위험을 낮춘다 |
| 실행 머신의 지역 시간으로 날짜 해석 | `Asia/Seoul`로 명시 해석 후 UTC로 저장 | 서버(OCI)가 UTC면 9시간 틀어진다 |
| 수집 시점에 "진행중/종료" 상태를 계산해 저장 | 마감 시각 원자료만 저장하고, 상태는 판단 시점에 계산 | 저장된 상태가 금방 낡는다 |
| 실패하면 예외를 그대로 던짐 | `Event(kind="collector_failed")`로 보고. 실패를 로그인 실패 / 추가 인증·CAPTCHA / 구조 변경 / 네트워크로 구분 | 3절 공통 규칙, 6절 eClass 규칙 |
| 여러 대학 분기, 서비스마다 기본 URL 하드코딩 | 사용하는 학교 하나만 지원하고, 학교 주소는 공개 저장소 밖의 로컬 설정에 둠 | 범위 축소, 중복 제거 |

## eclass-cli에 없어서 6단계에서 직접 조사할 것

- 과목별 공지, 알림함, 전체 공지, 학사일정 페이지의 경로와 구조
- 온라인 강의 수강 기한: 할 일 목록의 `lecture_weeks` 항목만으로 충분한지
- 마감 변경 감지: 이전 마감 값을 저장해 두고 비교한 뒤, 바뀌면 1회만 알림
- 로그인 실패 신호: 오류 문구, 머무르는 URL
- 추가 인증·CAPTCHA가 나타날 때의 신호(선택자·URL). 감지하면 자동화를 즉시 중단하고 알린다.
- 로그인 연속 2회 실패 시 재시도를 중단하는 카운터를 어디에 저장할지
