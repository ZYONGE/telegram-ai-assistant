# eClass 화면 구조 (실제 확인)

`scripts/eclass_explore.py`로 로그인해 훑은 결과다 (2026-09-20, 화면 102개 중 목록형 27개).
학교 주소와 표본은 `private/`에 있다. 여기에는 **경로 모양과 요청 방식만** 적는다 (CLAUDE.md 절대 규칙 12).

이 저장소가 쓰는 eClass는 `.acl` 확장자를 쓰는 이러닝 제품이다. 경로는 모두 `/ilos/` 아래에 있다.

## 1. 화면이 내용을 받아 오는 세 가지 방식

파서를 쓰기 전에 그 화면이 어느 쪽인지부터 가려야 한다. 주소를 열어 보고 표가 비어 있다고
"내용이 없다"고 판단하면 안 된다.

| 방식 | 생김새 | 예 |
|---|---|---|
| **껍데기 + 내용** | `..._list_form.acl`이 빈 틀만 주고, 화면 안에서 `..._list.acl`을 다시 불러 줄을 채운다 | 할 일, 전체 공지 |
| **한 번에** | 주소를 열면 내용까지 들어 있다 | 강의계획서, 시험, 쪽지함 |
| **자기 자신에게 다시** | 화면의 `<form action>`이 자기 경로다. 같은 주소로 한 번 더 요청해야 줄이 온다 | 보낸 쪽지 |

내용 주소(`..._list.acl`)는 그냥 부르면 "세션이 종료되었습니다"가 온다.
`X-Requested-With: XMLHttpRequest`와 직전 화면 `Referer`를 붙여 화면 안 요청처럼 보내야 한다
(`app/collectors/eclass/session.py`의 `post()`가 그렇게 한다).

**브라우저는 필요 없다.** 로그인도 내용도 순수 HTTP로 된다 (2026-09-21 실제 계정으로 확인, ADR 0008).
로그인은 `POST /ilos/lo/login.acl`이고, 화면의 폼에 적힌 숨은 칸(`returnURL`·`challenge`·`response`)을
그대로 돌려보내면 된다. `challenge`·`response`는 이름과 달리 빈 값으로도 통한다.

## 2. 로그인

| 무엇 | 경로 |
|---|---|
| 로그인 화면 | `/ilos/main/member/login_form.acl` |
| 메인 | `/ilos/main/main_form.acl` |

- 로그인 단추는 `<div onclick="loginForm();">`이라 Enter(폼 제출)로는 넘어가지 않는다. 눌러야 한다.
- **로그인에 실패해도 메인 주소로 보내 준다.** 주소로는 성패를 알 수 없고, 화면에 로그아웃 표시
  (`logout.acl` 또는 "로그아웃")가 있는지로 판단한다. `.header_logout` 같은 CSS 이름에 걸리지 않게 한다.

## 3. 과목방

과목방 화면(`/ilos/st/course/…`)은 **방에 들어간 뒤에야** 내용이 온다. 방 밖에서 열면 빈 껍데기다.

```
POST /ilos/st/course/eclass_room2.acl
     KJKEY=<과목 열쇠>&returnData=json&returnURI=/ilos/st/course/submain_form.acl&encoding=utf-8
GET  /ilos/st/course/submain_form.acl      ← 방 첫 화면
```

과목 열쇠는 할 일 화면(`/ilos/mp/todo_list_form.acl`)의 과목 선택 상자에서 읽는다
(`<option value="열쇠||L">`, `app/collectors/eclass/parse.py`의 `parse_course_select`).

한 방에 들어가면 그 과목이 "현재 방"이 되므로, 다음 과목을 보려면 다시 문을 연다.

### 과목방 메뉴

| 메뉴 | 경로 | 비고 |
|---|---|---|
| 공지사항 | `/ilos/st/course/notice_list.acl` | 날짜. 휴강·시험 변경이 여기로 온다 |
| 과제 | `/ilos/st/course/report_list.acl` | 마감·제출 여부·점수 |
| 시험 | `/ilos/st/course/test_list_form.acl` | 마감 |
| 온라인강의 | `/ilos/st/course/online_list_form.acl` → `online_list.acl` | 내용 요청에 `ud`(학번)·`ky`(과목 열쇠)·`WEEK_NO`(주차)를 보내야 한다. 빼면 빈 응답 (2026-09-22 확인). 주차는 껍데기의 `.wb[id^=week-]`, 지금 주는 `wb-choice`. 강의별 진도율·학습 시간이 나온다 |
| 실시간강의 | `/ilos/st/course/zoom_list.acl` | |
| 강의자료 | `/ilos/st/course/lecture_material_list.acl` | 날짜 |
| 열린게시판 | `/ilos/st/course/material_list_form.acl` | |
| 강의계획서 | `/ilos/st/course/plan_form.acl` | 게시판이 아니라 `table.bbsview` 두 개(과목 정보, 주차별)다. 한 줄에 이름·내용이 두 쌍씩 들어 있다. 학기에 한 번 바뀌므로 하루 1회 |
| 출석 | `/ilos/st/course/attendance_list_form.acl` → `attendance_list.acl` | `ud`·`ky`를 보내야 한다. 빼면 "접근 권한이 없습니다". 표가 아니라 글로 온다 (출석·지각·결석 수, 차시별 상태) |
| 성적 | `/ilos/st/course/eval3_result_view_form.acl` | |
| 팀프로젝트 | `/ilos/st/course/project_list.acl` | |
| 토론 | `/ilos/st/course/discuss_list.acl` | |
| 설문 | `/ilos/st/course/survey2_list.acl` | 마감 |

## 4. 공통 화면

| 메뉴 | 경로 | 방식 |
|---|---|---|
| 할 일 | `/ilos/mp/todo_list_form.acl` → `/ilos/mp/todo_list.acl` | 껍데기 + 내용 |
| 전체 공지 | `/ilos/community/notice_list_form.acl` → `/ilos/community/notice_list.acl` | 껍데기 + 내용 |
| 받은 쪽지 | `/ilos/message/received_list_pop_form.acl` | 한 번에 |
| 보낸 쪽지 | `/ilos/message/sent_list_pop_form.acl` | 자기 자신에게 다시 |
| 수강 과목 | `/ilos/st/main/course_ing_list_form.acl` → `/ilos/st/main/course_ing_list.acl` | 껍데기 + 내용 |
| 시간표 | `/ilos/st/main/pop_academic_timetable_form.acl` | 한 번에 |
| 학사일정 | `/ilos/st/schedule/academic_calendar_list_form.acl` | **내용 없음.** 화면의 `#data_list`를 자바스크립트가 채우는데, 화면이 잦아들 때까지 기다려도·질의를 붙여도·POST로 해도 비어 있다 (2026-09-20 세 방식 확인). 학교가 여기에 학사일정을 넣지 않는 것으로 본다 |
| 올린 파일함 | `/ilos/mp/file_list_form.acl` → `/ilos/mp/file_list.acl` | 껍데기 + 내용 |
| 질의응답 | `/ilos/community/qna_list_form.acl` → `/ilos/community/qna_list.acl` | 껍데기 + 내용 |

### 내용 요청에 보낼 값 읽기

껍데기 화면의 스크립트에 `$.ajax({ url: "…_list.acl", data: { … } })`로 적혀 있다.
과목 공지·과제·강의자료·설문도 `ud`·`ky`를 함께 보낸다 (없어도 되는 화면이 있지만 보내는 편이 안전하다).
**값을 코드에 적지 않고 화면에서 읽는다** (`app/collectors/eclass/page.py`의 `list_request`). 학번이 들어 있으므로 요청에만 쓰고 로그·결과에 옮기지 않는다.
따옴표로 적힌 값은 그대로, 스크립트가 실행 때 채우는 값은 `start`=쪽 번호, `WEEK_NO`=주차, 나머지는 빈 값으로 보낸다.

### 그 밖에 확인한 것 (2026-09-22)

| 무엇 | 경로·방식 |
|---|---|
| 과제 제출 여부 | 과제 목록 '제출' 칸. 글자가 아니라 **그림의 alt**("제출"/"미제출")에 있다. "미제출"에도 "제출"이 들어 있으니 정확히 일치로 본다 |
| 과제 상세 | `report_view_form.acl?RT_SEQ=…`. `table.bbsview`(제출방식·마감일·지각제출·점수공개) + `.textviewer` 본문. 본문 끝에 "첨부파일(N개) - 이름"이 붙는다 |
| 과제 번호 | 과제 게시판 글번호(`RT_SEQ`)와 할 일 목록의 번호가 같다. 이것으로 할 일과 이어진다 |
| 시험 상세 | `test_view_form.acl?exam_setup_seq=…`. 시험 정보 `bbsview` + 응시 기록 표(시작·종료·IP) + 점수. 여는 것만으로는 응시가 시작되지 않는다 |
| 받은 쪽지 열기 | 목록의 `viewPage('SEQ','SEND_ID')` → `/ilos/message/received_view_pop_form.acl?SEQ=…&SEND_ID=…` (GET) |
| 알림함 | `POST /ilos/mp/notification_list.acl` (`start`, `display`, `OPEN_DTM`=빈 값). 표가 아니라 글로 온다 |
| 성적 | `eval3_result_view_form.acl`. 공개 전에는 "최종성적이 공개되지 않았습니다" |
| 과목 선택 목록 | 할 일 화면의 과목 선택 상자가 법정의무교육 같은 비교과까지 15과목을 준다. `course_ing_list`는 다른 학생의 청강 과목까지 섞여 있어 쓰지 않는다 |

## 5. 열지 않는 주소

탐색기가 막는다 (`UNSAFE_WORDS`). 조회만 하기로 한 약속이다 (CLAUDE.md 절대 규칙 5).

- 바꾸는 것: `submit` `insert` `update` `delete` `remove` `save` `modify` `write` `regist` `apply` `cancel` `upload` `proc` `exec`
- 내려받기: `down`이 들어간 모든 주소, `attach`
- 세션을 끊는 것: `logout`
- 비서의 즉석 조회는 여기에 더해 `start`·`take`·`answer`·`exam`·`vote`·`reply`·`comment`·`button`·`check`와
  강의 재생(`online_view`·`play`·`viewer`·`movie`·`vod`)을 막고, 조회 화면 이름(`_list`·`_view`·`plan_form` 등)만 연다.
  강의 재생 화면은 여는 것만으로 수강 기록이 남는다.

한 가지 더: 머리글 스크립트가 **모든 화면에 똑같이** 들어 있어, 화면마다 스크립트를 훑으면
같은 메뉴를 끝없이 다시 찾는다. 스크립트 훑기는 메뉴가 있는 첫 화면에서만 한다.

## 6. 줄 세는 법

표 한 줄인지 가릴 때 글번호(`ARTL_NUM`)에 기대면 안 된다. 쪽지함·과제 목록처럼 글번호 없이
`onclick`으로 넘어가는 화면이 많다. **칸(`td`)이 둘 이상인 줄**을 세면 대체로 맞고,
"조회할 자료가 없습니다"는 칸 하나를 늘려 쓰므로 저절로 빠진다.
