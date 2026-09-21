# 서버 배포

> **보류 (2026-09-21).** Oracle Cloud A1 인스턴스가 용량 부족으로 생성되지 않아, 지금은 맥북에어 M1을 임시 서버로 쓴다
> (`docs/adr/0008-macbook-temporary-server.md`, `docs/tasks.md` T-24~T-28). 이 문서는 A1을 확보해 옮길 때 쓴다.

Oracle Cloud A1(arm64, Ubuntu 24.04)에서 비서를 24시간 돌린다.

> 개인 파일(`private/`)은 이미지에 들어가지 않는다. 볼륨으로만 붙는다 (CLAUDE.md 절대 규칙 12).
> 이 문서에는 서버 주소·계정·키를 적지 않는다.

## 1. 서버 준비

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"   # 다시 로그인해야 적용된다
```

시간대를 맞춰 둔다. 컨테이너 안은 이미지가 `Asia/Seoul`로 잡지만, 서버 로그도 같은 시각으로 보는 편이 낫다.

```bash
sudo timedatectl set-timezone Asia/Seoul
```

## 2. 코드와 개인 파일

```bash
git clone https://github.com/ZYONGE/telegram-ai-assistant.git
cd telegram-ai-assistant
```

`private/` 폴더는 git에 없다. 개발 PC에서 통째로 옮긴다.

```bash
# 개발 PC에서 (서버 주소는 각자)
scp -r private ubuntu@<서버>:~/telegram-ai-assistant/
```

들어 있어야 하는 것: `.env`(토큰·키·학교 계정), `profile.md`, `instructions.md`, `local.toml`,
`assistant.db`, `memory.md`, `google_client.json`, `google_token_*.json`,
`browser/`(eClass 세션), `eclass_catalog.json`, `eclass_scope.json`.

옮긴 뒤 권한을 좁힌다.

```bash
chmod 700 private && chmod 600 private/.env
```

## 3. 컨테이너 안에서 브라우저가 뜨게

크로미움은 자기 자신을 격리해서 띄우는데, 컨테이너 안에서는 그 격리가 막혀 브라우저가 열리지 않을 수
있다. 그때는 서버의 `private/local.toml`에 다음을 더한다 (기기마다 다른 설정이라 여기에 둔다).

```toml
[eclass]
browser_args = ["--no-sandbox"]
```

컨테이너 자체가 울타리 역할을 하고, 여는 곳은 학교 사이트 하나뿐이라 이 정도는 받아들일 만하다.
**먼저 이 줄 없이 해 보고, 브라우저가 안 뜰 때만 넣는다.**

## 4. 띄우기

```bash
docker compose -f deploy/compose.yaml up -d --build
```

첫 빌드는 10~20분쯤 걸린다 (파이썬 3.14와 크로미움을 받아 온다).

```bash
docker compose -f deploy/compose.yaml logs -f
```

이렇게 나오면 정상이다.

```
비서를 시작합니다. 종료하려면 Ctrl+C를 누르세요.
eClass 소스 22개 (할 일 포함)
메일 수집: 10분마다 (06시~23시)
eClass 수집: 90분마다 (06시~23시)
```

## 5. 확인할 것 두 가지

**브라우저가 arm64에서 도는가**

```bash
docker compose -f deploy/compose.yaml exec assistant \
  uv run --no-dev python -c "
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        page = await b.new_page()
        await page.goto('https://example.com')
        print('열림:', await page.title())
        await b.close()
asyncio.run(main())
"
```

**학교 eClass가 일본 IP를 받아 주는가** — 다음 수집(최대 90분) 뒤 로그를 본다.
`eClass 확인: 소스 N개, 항목 M건` 이 나오면 된 것이다. 추가 인증을 요구하면 수집기가 멈추고 알린다.

## 6. 평소 쓰는 명령

| 하고 싶은 것 | 명령 |
|---|---|
| 상태 보기 | `docker compose -f deploy/compose.yaml ps` |
| 로그 보기 | `docker compose -f deploy/compose.yaml logs -f --tail 100` |
| 새 코드 반영 | `git pull && docker compose -f deploy/compose.yaml up -d --build` |
| 재시작 | `docker compose -f deploy/compose.yaml restart` |
| 멈추기 | `docker compose -f deploy/compose.yaml down` |

서버가 다시 켜지거나 봇이 죽으면 `restart: unless-stopped`가 알아서 다시 올린다.

## 7. 주의

- **포트를 열지 않는다.** 밖에서 들어오는 요청이 없다 (텔레그램에 봇이 접속하는 폴링 방식).
  오라클 쪽 보안 목록도 22번(SSH)만 열어 둔다.
- **봇을 두 곳에서 동시에 돌리지 않는다.** 텔레그램 폴링이 충돌하고 알림이 두 번 간다.
  서버에 올린 뒤에는 개발 PC의 봇을 끈다.
- `private/`를 옮길 때 실수로 저장소에 넣지 않는다. `.gitignore`와 커밋 검사 훅이 막지만, 서버에서도
  `git status`가 깨끗한지 한 번 본다.
