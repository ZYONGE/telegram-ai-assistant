# 맥북에어 서버 설정

비서를 맥북에어 M1에서 24시간 돌린다 (ADR 0008).

> 이 문서에는 계정·주소·키를 적지 않는다. 개인 파일은 `private/` 한 곳에만 둔다 (CLAUDE.md 절대 규칙 12).
> 명령은 위에서부터 차례로 한다. **`사용자이름`은 맥북의 실제 사용자 이름으로 바꾼다** (`whoami`로 확인).

---

## 0. 먼저 정할 것 — FileVault

맥의 디스크 암호화다. **봇을 24시간 돌리는 데 직접 영향이 있다.**

| | 켜 두면 | 끄면 |
|---|---|---|
| 정전·강제 재시작 뒤 | **사람이 가서 암호를 넣어야 봇이 뜬다** | 저절로 다시 뜬다 |
| 맥북을 잃어버리면 | `private/`(학교 계정·토큰)를 열 수 없다 | 디스크를 뽑으면 읽힌다 |

계획된 재시작은 `sudo fdesetup authrestart`로 암호 없이 넘길 수 있다. 문제는 예상 못 한 재시작이다.

**집에 두고 쓰는 기기이고 무인 운영이 목적이면 끄는 쪽이 맞다.** 밖으로 들고 나갈 일이 생기면 그때 켠다.

```bash
fdesetup status        # 지금 상태 보기
sudo fdesetup disable  # 끄기 (암호를 묻는다)
```

---

## 1. 기본 준비

### 1-1. 시간대·잠자기

```bash
sudo systemsetup -settimezone Asia/Seoul
sudo pmset -a disablesleep 1
```

`pmset -g` 로 `SleepDisabled 1` 인지 확인한다. **뚜껑을 닫아도, 배터리로만 돌려도 잠들지 않는다** (`-a`는 전원·배터리 모두).

전원은 꽂아 두지 않아도 된다. 배터리가 떨어지기 전에 사용자가 충전한다 (2026-09-21 사용자 결정).
배터리가 다 닳으면 맥북이 꺼지고 봇도 멈춘다. 충전 뒤 켜면 launchd가 봇을 다시 띄운다 (FileVault를 꺼 두었으므로 로그인 없이).

### 1-2. 자동 로그인은 켜지 않는다

봇은 로그인 없이 뜬다(LaunchDaemon). 자동 로그인을 켤 까닭이 없고, 켜면 기기를 주웠을 때 바로 열린다.

### 1-3. 개발 도구

```bash
xcode-select --install          # 이미 있으면 그냥 넘어간다
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL -l                  # PATH 다시 읽기
uv --version
```

---

## 2. 저장소와 개인 파일

### 2-1. 저장소

```bash
cd ~
git clone https://github.com/ZYONGE/telegram-ai-assistant.git
cd telegram-ai-assistant
git config core.hooksPath .githooks
uv sync --no-dev
```

`--no-dev`는 테스트 도구를 빼고 받는다. 서버에서 테스트를 돌리려면 `uv sync` 로 한 번 더 한다.

### 2-2. `private/` 옮기기 — **git으로 옮기지 않는다**

개인 파일은 저장소에 없다. 윈도우 PC에서 직접 옮긴다.

**먼저 윈도우 쪽 봇을 멈춘다.** DB를 쓰는 중에 옮기면 깨진다. 그리고 **두 기기에서 봇을 동시에 돌리면 안 된다** — 같은 토큰으로 텔레그램을 둘이 물면 서로 메시지를 빼앗는다.

옮길 것은 `private/` 폴더 통째로다.

```
.env                     토큰·키·학교 계정
profile.md               신상·학사 정보
instructions.md          판단 기준
local.toml               학교 주소 등 개인 설정
assistant.db             할 일·기억·수집 기록
memory.md                장기 기억
google_client.json       OAuth 클라이언트
google_token_1~3.json    계정 3개 토큰
browser/                 eClass 세션 쿠키
eclass_catalog.json      화면 목록
eclass_scope.json        수집 범위 결정
backups/                 지난 백업 (안 옮겨도 된다)
logs/                    지난 로그 (안 옮겨도 된다)
```

AirDrop이나 USB로 옮긴다. 옮긴 뒤 권한을 좁힌다.

```bash
chmod 700 private
chmod 600 private/.env private/google_token_*.json private/google_client.json
```

### 2-3. 도는지 먼저 확인

launchd에 걸기 전에 손으로 한 번 띄워 본다.

```bash
uv run python -m app.main
```

이렇게 나오면 된 것이다.

```
비서를 시작합니다. 종료하려면 Ctrl+C를 누르세요.
eClass 소스 22개 (할 일 포함)
메일 수집: 10분마다 (06시~23시)
eClass 수집: 90분마다 (06시~23시)
백업: 매일 03:30 (14일 보관)
```

텔레그램으로 말을 걸어 답이 오는지 본다. 확인했으면 `Ctrl+C`.

---

## 3. 상시 실행 (launchd)

### 3-1. plist 고치기

`deploy/macos/com.assistant.bot.plist`에서 **`사용자이름`을 모두 바꾼다** (네 군데).

```bash
whoami                                              # 실제 이름 확인
sed -i '' "s/사용자이름/$(whoami)/g" deploy/macos/com.assistant.bot.plist
grep -n "$(whoami)" deploy/macos/com.assistant.bot.plist   # 바뀌었는지 확인
```

> 이 파일은 저장소에 있다. 사용자 이름이 개인정보이므로 **바꾼 파일을 커밋하지 않는다.**
> 설치한 뒤 `git checkout deploy/macos/com.assistant.bot.plist` 로 되돌려 둔다.

### 3-2. 설치

```bash
mkdir -p private/logs
sudo cp deploy/macos/com.assistant.bot.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.assistant.bot.plist
sudo chmod 644 /Library/LaunchDaemons/com.assistant.bot.plist
sudo launchctl load -w /Library/LaunchDaemons/com.assistant.bot.plist
```

### 3-3. 확인

```bash
sudo launchctl list | grep com.assistant.bot    # 왼쪽이 PID, 가운데가 0이면 정상
tail -f private/logs/assistant.log
```

가운데 숫자가 0이 아니면 죽은 것이다. `private/logs/launchd.err.log` 를 본다.

### 3-4. 평소 쓰는 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 | `sudo launchctl list \| grep com.assistant.bot` |
| 로그 | `tail -f ~/telegram-ai-assistant/private/logs/assistant.log` |
| 재시작 | `sudo launchctl kickstart -k system/com.assistant.bot` |
| 멈추기 | `sudo launchctl unload /Library/LaunchDaemons/com.assistant.bot.plist` |
| 새 코드 반영 | `git pull && uv sync --no-dev && sudo launchctl kickstart -k system/com.assistant.bot` |
| 새 코드 반영 (개발 PC에서 원격으로) | 아래 3-5 |
| 재시작 (sudo 없이, 원격) | `kill -9 $(pgrep -f "python -m app.main")` — 비정상 종료로 보고 launchd가 몇 초 안에 다시 띄운다 (2026-09-22 확인). `kill`(SIGTERM)은 정상 종료라 다시 뜨지 않는다. 조용한 시간처럼 한가할 때 한다 |

### 3-5. 개발 PC에서 원격으로 반영하기

SSH로 명령만 보내면 로그인 셸 설정을 읽지 않아 `uv`가 PATH에 없다. **전체 경로 `~/.local/bin/uv`를 쓴다.**
DB 구조가 바뀌는 코드(마이그레이션)를 받을 때는 먼저 DB를 떠 둔다. `sqlite3`의 `.backup`은 봇이 켜져 있어도 온전한 사본을 만든다.

```bash
ssh 사용자이름@<Tailscale 주소> 'cd ~/telegram-ai-assistant \
  && sqlite3 private/assistant.db ".backup private/backups/pre-deploy-$(date +%Y%m%d)/assistant.db" \
  && git pull && ~/.local/bin/uv sync --no-dev \
  && kill -9 $(pgrep -f "python -m app.main")'
```

`.backup` 앞에 폴더가 있어야 한다 (`mkdir -p private/backups/pre-deploy-날짜`). 재시작은 조용한 시간처럼 한가할 때 한다.
확인: `tail -20 private/logs/assistant.log` 에 "비서를 시작합니다"와 "스케줄러 시작"이 찍히면 된다.

### 3-6. 개인 파일 고치기

**서버의 `private/`가 원본이다** (2026-09-22부터). 개발 PC의 사본은 오래됐다. 개인 파일은 서버에서 고친다.

| 방법 | 명령 |
|---|---|
| 맥북에서 직접 | `open -e ~/telegram-ai-assistant/private/instructions.md` |
| 개발 PC에서 SSH로 들어가서 | `ssh 사용자이름@<Tailscale 주소>` → `nano ~/telegram-ai-assistant/private/instructions.md` (`Ctrl+O` 저장, `Ctrl+X` 나가기) |
| 개발 PC에서 고쳐 보내기 | `scp private/instructions.md 사용자이름@<Tailscale 주소>:telegram-ai-assistant/private/instructions.md` — **서버 파일을 통째로 덮어쓴다.** 서버에서 고친 내용이 있으면 사라지므로 한쪽에서만 고친다 |

VS Code의 Remote - SSH 확장으로 서버 파일을 개발 PC 화면에서 바로 열어도 된다.

고친 뒤 재시작이 필요한지:

| 파일 | 적용 |
|---|---|
| `instructions.md` | 저장하면 바로 (대화·알림·브리핑이 부를 때마다 다시 읽는다) |
| `profile.md` | 대화에는 바로. 호칭을 바꿨으면 재시작 (브리핑 인사말은 켤 때 읽는다) |
| `.env`, `local.toml` | 재시작해야 적용 |
| `memory.md` | 봇이 쓰는 파일이라 손대지 않는다. 기억 삭제는 대화로 요청한다 |

### 3-7. 되돌리기

```bash
sudo launchctl unload /Library/LaunchDaemons/com.assistant.bot.plist
sudo rm /Library/LaunchDaemons/com.assistant.bot.plist
```

---

## 4. 원격 관리 (SSH + Tailscale)

집 밖에서도 맥북에 들어가 로그를 보고 새 코드를 받기 위한 것이다.
**공유기에 포트를 열지 않는다.** Tailscale이 두 기기를 직접 이어 준다.

### 4-1. 원격 로그인 켜기

**시스템 설정 → 일반 → 공유 → 원격 로그인** 을 켠다. 접근 권한은 "다음 사용자만"으로 본인만 둔다.

### 4-2. 키만 허용하고 비밀번호 로그인은 끈다

윈도우 PC의 공개키를 맥북에 넣는다 (윈도우에서 실행).

```bash
cat ~/.ssh/id_ed25519.pub
```

맥북에서 그 한 줄을 붙여 넣는다.

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
nano ~/.ssh/authorized_keys      # 붙여 넣고 저장
chmod 600 ~/.ssh/authorized_keys
```

키로 접속되는 것을 **확인한 뒤에** 비밀번호 로그인을 끈다.

```bash
sudo nano /etc/ssh/sshd_config
```

```
PasswordAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin no
```

```bash
sudo launchctl kickstart -k system/com.openssh.sshd
```

### 4-3. Tailscale

```bash
brew install --cask tailscale     # 또는 앱스토어에서 설치
```

앱을 열고 기존 계정으로 로그인한다. 윈도우 PC에도 같은 계정으로 설치한다.

```bash
tailscale ip -4                   # 맥북의 주소 (100.x.x.x)
```

윈도우에서 접속해 본다.

```bash
ssh 사용자이름@100.x.x.x
```

> Tailscale 주소는 개인 정보다. 저장소나 문서에 적지 않는다.

---

## 5. 처음 하루 동안 볼 것

### 5-1. 메모리 (T-27)

8GB 기기라 스왑이 생기면 SSD 수명을 쓴다. 하루 돌린 뒤 잰다.

```bash
ps -o rss=,command= -p $(pgrep -f "app.main") | awk '{printf "%.0fMB  %s\n", $1/1024, $2}'
sysctl vm.swapusage
```

- 상주 메모리(RSS)가 **200MB 안팎**이면 정상이다
- `swapusage`의 `used`가 0에 가까우면 좋다. 계속 늘면 원인을 찾는다

eClass 수집은 이제 브라우저를 쓰지 않아 수 MB만 더 쓴다 (T-23).

### 5-2. 제때 도는지

- 아침 07:00 브리핑이 오는가
- 90분마다 `eClass 확인:` 줄이 로그에 찍히는가
- 새벽 03:30에 `private/backups/오늘날짜/` 가 생기는가

### 5-3. Wi-Fi가 끊겼다 돌아올 때 (T-28)

기기를 옮기거나 공유기를 껐다 켠 뒤, 저절로 다시 붙는지 본다. 안 붙으면 알려 주시면 프로세스를 끝내 launchd가 다시 띄우도록 고친다.

---

## 6. 주의

- **봇을 두 곳에서 동시에 돌리지 않는다.** 맥북에 올린 뒤에는 윈도우 PC의 봇을 끈다.
- `private/`를 저장소에 넣지 않는다. 커밋 검사 훅이 막지만 `git status`도 한 번 본다.
- plist의 사용자 이름을 커밋하지 않는다.
- 맥북을 옮기면 Wi-Fi가 끊겨 그동안 봇이 멈춘다. 감수하기로 한 것이다 (ADR 0008).
