# NanoClaw 분석 메모

| 항목 | 내용 |
|---|---|
| 저장소 | github.com/nanocoai/nanoclaw |
| 분석 커밋 | `6e5008f` (2026-09-16) |
| 언어 | TypeScript (호스트: Node + pnpm, 에이전트 컨테이너: Bun), SQLite |
| 라이선스 | MIT (Copyright 2026 Gavriel) |
| 적용 위치 | `app/scheduler`(예약 작업 저장·실행 제어), `app/tools`(예약 작업 도구) |
| 참고 범위 | `src/modules/scheduling/`, `src/mailbox/sqlite/tasks.ts`, `src/cli/resources/tasks.ts`, `docs/scheduled-tasks.md` |

> 컨테이너 격리, 에이전트 그룹·권한, 채널 설치 스킬, OneCLI 비밀값 게이트웨이, 승인 흐름은 보지 않았다.

## 풀고 있는 문제

에이전트가 대화 중에 "내일 6시에 알려 줘", "평일 9시마다 브리핑해 줘" 같은 작업을 스스로 등록·조회·일시정지할 수 있어야 한다.
동시에 두 가지 사고를 막아야 한다.

- 반복 작업이 너무 자주 돌아 토큰을 낭비하는 것
- 고장 난 작업이 끝없이 반복되는 것

## 핵심 아이디어

1. **작업 = 프롬프트 + 실행 시각 + (선택) cron.** 1회성은 `process_after`만, 반복은 `recurrence`(cron)를 함께 둔다. 반복 작업의 첫 실행 시각은 cron에서 계산한다.
2. **반복 작업은 "시리즈"다.**
   - 한 회차가 끝나면 다음 회차 행을 새로 만들어 `series_id`를 이어받게 한다. 원래 행의 `recurrence`는 비운다.
   - 이 두 쓰기를 한 번에 처리해서, 중간에 죽어도 중복 생성이나 누락이 생기지 않게 한다.
   - 실행 이력은 완료된 행들이고, 다음 실행은 살아 있는(`pending`/`paused`) 행이다.
3. **상태 5종과 명확한 조작.**

   | 조작 | 변화 |
   |---|---|
   | pause | `pending` → `paused` |
   | resume | `paused` → `pending` |
   | cancel | `pending`/`paused` → `cancelled`, `recurrence` 제거. 이력은 남김 |
   | delete | 시리즈 전체 행 삭제 |
   | 실행 결과 | `completed` / `failed` |

   pause·resume·cancel은 모두 `WHERE id = ? OR series_id = ?` 조건의 UPDATE 한 줄이다.
4. **cron은 사용자 시간대로 해석한다.** UTC로 해석하면 "9시"가 UTC 9시(한국 18시)가 되는 버그가 생긴다. 저장은 ISO UTC, 표시는 로컬 시간이다.
5. **빈도 제한.** 게이트 스크립트가 없는 반복 작업이 앞으로 24시간 안에 4회를 넘게 실행되면 등록을 거부한다. 거부 메시지로 모델에게 대안(게이트 사용)을 안내한다.
6. **스크립트 게이트.** 작업 전에 스크립트를 실행하고, 마지막 줄 JSON이 `{"wakeAgent": false}`이면 모델을 호출하지 않고 끝낸다. `true`이면 `data`를 프롬프트에 붙인다.
7. **실패 백오프와 자동 일시정지.**
   - 연속 실패 횟수는 따로 저장하지 않고 최근 이력에서 계산한다.
   - 다음 실행을 2, 4, 8, 16, 32, 60분 순으로 늦춘다 (이후 60분 유지).
   - 8회 연속 실패하면 시리즈를 `paused`로 바꾸고 실행 로그에 사유를 남긴다.
   - 게이트가 정상적으로 "실행 안 함"을 반환한 경우는 실패로 세지 않는다.
8. **run now.** 일정을 바꾸지 않고 1회만 추가 실행한다(테스트용). 일시정지 중에도 가능하며, 이때 추가되는 행에는 `recurrence`가 없다.
9. **읽기 쉬운 작업 ID.** 이름 슬러그 + 4자리 hex(예: `sales-briefing-a25c`), 이름이 없으면 `t-xxxxxx`. `[a-z0-9-]`만 허용해서 파일 이름으로 써도 경로 조작이 불가능하다.
10. **실행 로그.** 시리즈별 파일에 `시각 — 내용` 형식으로 한 줄씩 추가한다.
11. **목록 요약.** 시리즈, 일정(없으면 once), 실행 수, 실패 수, 마지막 실행, 다음 실행, 상태, 생성 후 경과, 프롬프트 앞부분을 보여 준다.
12. **"검증·계산"과 "저장"의 분리.** `prepareScheduledTask`는 cron 검증, 빈도 제한, 첫 실행 계산만 하고 아무것도 쓰지 않는다. 저장은 `createScheduledTask`가 맡는다.

## 데이터 구조

작업은 세션의 `messages_in` 테이블에 `kind='task'` 행으로 저장된다.

| 컬럼 | 의미 |
|---|---|
| `id` | 이 회차의 ID |
| `series_id` | 시리즈 ID (첫 회차의 id). 인덱스 있음 |
| `status` | pending / paused / completed / failed / cancelled |
| `process_after` | 실행 가능 시각 (ISO UTC) |
| `recurrence` | cron 식. 살아 있는 회차에만 있음 |
| `tries` | 재시도 횟수 |
| `content` | JSON `{ prompt, script, originSessionId }` |
| `timestamp`, `seq` | 생성 시각, 정렬 순서 |

별도로 호스트가 60초마다 스윕을 돌며, 실행 시각에 도달한 행을 깨우고 완료된 반복 행의 다음 회차를 만든다.

## 인터페이스

- `prepareScheduledTask({name, prompt, recurrence, processAfter, script, timezone})` → 검증된 작업 (쓰기 없음)
- `createScheduledTask(group, prepared, {status})` → 저장된 행
- `handleRecurrence(db, session)`: 스윕마다 다음 회차 준비, 백오프, 자동 일시정지
- `pauseTask(id)`, `resumeTask(id)`, `cancelTask(id)`, `deleteTask(id)`, `updateTask(id, {prompt, recurrence, processAfter})`
- `tasks run <id>`, `tasks list`(이력 요약 포함), `tasks get <id>`(최근 로그 10줄 포함)
- `appendRunLog(group, series, msg)`

## 가져올 것

- **작업 모델.** 1회성·반복을 같은 형식으로 표현하고, 상태 5종과 pause/resume/cancel/delete를 구분한다.
- **검증과 저장 분리.** 모델이 준 입력(cron, 시각)을 먼저 검증하고, 틀리면 도구 결과로 오류를 돌려줘서 모델이 고치게 한다.
- **cron을 `Asia/Seoul`로 해석하고 UTC로 저장.**
- **빈도 제한.** 모델이 실수로 1분 간격 작업을 등록하는 사고를 막는다.
- **연속 실패 백오프 + 자동 일시정지 + 사유 기록.** 실패는 "수집 실패 알림" 경로로 알린다.
- **run now** (테스트·"지금 한 번 해 줘").
- **읽기 쉬운 작업 ID.**
- **목록 요약 필드.** 텔레그램 출력에서는 표 대신 "·" 목록 문장으로 바꾼다.

## 버릴 것 / 다르게 할 것

| NanoClaw | 우리 프로젝트 | 이유 |
|---|---|---|
| 자체 60초 스윕 + 회차마다 행 복제 | 실행 시각 계산과 발화는 **APScheduler(SQLite 작업 저장)** 가 맡음. 우리 SQLite `scheduled_tasks` 테이블은 의미(프롬프트, 상태, 실행 이력)를 맡음. APScheduler job id = 작업 id로 연결하고, pause/resume은 두 곳을 함께 바꿈 | 3절 확정 스택. 재시작 후 두 저장소의 정합성 점검 방법은 3단계 설계 과제 |
| Bash 스크립트 게이트 (모델이 작성) | 셸 실행 없음. 코드에 미리 정의된 점검 함수(수집기)만 게이트로 쓰고, 모델은 게이트를 새로 만들 수 없음 | 절대 규칙 5 |
| 작업마다 에이전트 세션·컨테이너 실행 | 리마인더는 등록 때 저장한 문장을 알림 게이트로 보냄. 모델 호출은 브리핑처럼 내용 생성이 필요한 작업에만 | "알릴 내용이 있을 때만 모델 호출" 원칙, 비용 |
| 결과 전달처를 작업 프롬프트에 지정 | 사용자 1명·채널 1개로 고정. 모든 선제 발송은 알림 게이트 경유 | 7절 |
| 조용한 시간 개념 없음 | 사용자가 직접 그 시간으로 요청한 리마인더는 `Event.meta`에 표시해서 게이트가 조용한 시간 예외로 처리 | 7절 조용한 시간 예외 |
| 등록 전 사용자 확인 여부를 모델이 판단 | 리마인더·예약 작업은 즉시 등록하고 결과를 알림 (규칙으로 고정) | 7절 확인 단계 |
| 모델이 delete(이력까지 영구 삭제) 가능 | 모델 도구는 cancel까지만. 완전 삭제는 필요해지면 재검토 | 되돌릴 수 없는 조작 최소화 |
| 에이전트 그룹, owner/admin 권한, 승인 흐름, 템플릿 작업, 시리즈별 시간대 재정의 | 제외 | 사용자 1명 |
| 실행 로그를 마크다운 파일에 추가 | SQLite 실행 이력 테이블 | 구조화 데이터는 SQLite (3절) |
