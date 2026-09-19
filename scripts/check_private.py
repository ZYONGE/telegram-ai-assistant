"""커밋 전 개인정보·비밀값 검사 (CLAUDE.md 절대 규칙 12).

private/ 폴더에 들어 있는 실제 값이 저장소로 들어가려 하면 커밋을 막는다.
검사 결과에는 **값을 출력하지 않고** 어떤 항목인지와 파일 이름만 적는다.

설치: git config core.hooksPath .githooks   (한 번만)
검사: py -3.14 scripts/check_private.py      (직접 실행하면 현재 스테이징된 내용을 본다)
건너뛰기: git commit --no-verify             (권장하지 않는다)
"""

import re
import subprocess
import sys
from pathlib import Path

PRIVATE_DIR = "private/"
# 짧은 낱말은 우연히 겹치기 쉬워 길이 기준을 둔다. 이름·호칭만 예외로 짧게 본다.
MIN_LENGTH = 4
SHORT_FIELDS = ("이름", "호칭")
# 애초에 추적하면 안 되는 경로
BLOCKED_NAME = re.compile(r"(^|/)\.env(\.|$)")

# 검사기 자신의 테스트처럼 일부러 가짜 키를 넣는 파일은 이 표시를 넣어 형태 검사만 면제한다.
# 실제 private/ 값 대조는 표시와 무관하게 언제나 한다.
FAKE_MARKER = "check-private: 예시 값"

KEY_PATTERNS = {
    "Google API 키": re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    "텔레그램 봇 토큰": re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}"),
    "OAuth 액세스 토큰": re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),
    "OAuth 클라이언트 비밀": re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),
    "OAuth refresh 토큰": re.compile(r"\b1//[A-Za-z0-9_\-]{30,}"),
    "개인 키 파일": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def secret_values(root: Path = Path(".")) -> dict[str, str]:
    """private/ 안의 실제 값을 '항목 이름 → 값'으로 모은다."""
    values: dict[str, str] = {}
    env_file = root / PRIVATE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                name, raw = line.split("=", 1)
                value = raw.strip().strip('"').strip("'")
                if len(value) >= MIN_LENGTH:
                    values[f".env의 {name.strip()}"] = value

    profile = root / PRIVATE_DIR / "profile.md"
    if profile.exists():
        for line in profile.read_text(encoding="utf-8").splitlines():
            if not line.startswith("- ") or ":" not in line:
                continue
            label, raw = line[2:].split(":", 1)
            label, value = label.strip(), raw.strip()
            limit = 2 if label in SHORT_FIELDS else MIN_LENGTH
            if len(value) >= limit:
                values[f"프로필의 {label}"] = value

    local = root / PRIVATE_DIR / "local.toml"
    if local.exists():
        for line in local.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                name, raw = line.split("=", 1)
                value = raw.strip().strip('"').strip("'")
                if len(value) >= MIN_LENGTH:
                    values[f"local.toml의 {name.strip()}"] = value
    return values


def staged_files() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def staged_text(path: str) -> str:
    result = subprocess.run(["git", "show", f":{path}"], capture_output=True, check=False)
    return result.stdout.decode("utf-8", errors="ignore")


def blocked_path(path: str) -> str | None:
    """추적 자체를 막는 경로면 이유를 돌려준다."""
    if path.startswith(PRIVATE_DIR):
        return "private/ 폴더의 파일"
    if BLOCKED_NAME.search(path):
        return "비밀값 파일(.env)"
    return None


def find_leaks(text: str, values: dict[str, str]) -> list[str]:
    """파일 내용에서 발견된 항목 이름 목록 (값은 담지 않는다)."""
    found = [label for label, value in values.items() if value and value in text]
    if FAKE_MARKER not in text:
        found += [f"{label} 형태의 문자열" for label, pattern in KEY_PATTERNS.items() if pattern.search(text)]
    return found


def check(paths: list[str], values: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for path in paths:
        reason = blocked_path(path)
        if reason:
            problems.append(f"{path}: {reason}은 저장소에 올릴 수 없습니다")
            continue
        for label in find_leaks(staged_text(path), values):
            problems.append(f"{path}: {label}이(가) 들어 있습니다")
    return problems


def main() -> int:
    values = secret_values()
    problems = check(staged_files(), values)
    if not problems:
        return 0
    print("커밋을 멈췄습니다. 개인정보나 비밀값이 섞여 있습니다 (CLAUDE.md 절대 규칙 12).", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("", file=sys.stderr)
    print("값 자체는 출력하지 않았습니다. 해당 부분을 자리표시나 예시 값으로 바꾼 뒤 다시 커밋하세요.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
