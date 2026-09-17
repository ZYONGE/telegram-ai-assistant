# 0004. 모델은 Gemini, 호출은 결제 연결 프로젝트로, 제공사는 설정으로 교체

- 상태: 채택 (2026-09-18, 사용자님 결정)
- 코드: `app/llm/` (`base.py`, `__init__.py`, `gemini.py`), `config.toml`의 `[llm]`
- 이전 결정: Anthropic SDK(대화 Sonnet급, 문장 다듬기 Haiku급)

## 결정

1. **모델:** Claude 대신 Gemini를 쓴다. 기본은 대화·가벼운 작업 모두 `gemini-3.5-flash-lite`다. 모델 ID는 `[llm] chat_model`, `light_model`로 바꾼다.
2. **호출 경로 (B안):** Cloud 결제 계정을 연결한 Google Cloud 프로젝트로 호출한다.
   - Gemini API 무료 티어는 입력 내용이 Google 제품 개선에 쓰일 수 있다. 결제를 연결한 유료 티어는 그렇지 않다.
   - 기본 `backend = "api_key"`: 그 프로젝트에서 발급한 Gemini API 키(`GEMINI_API_KEY`)를 쓴다. `billing_enabled = true`를 확인해야 시작한다. 결제 연결 여부는 API로 확인할 수 없어서, 사람이 확인하고 설정으로 표시하게 했다.
   - 선택 `backend = "vertex"`: 같은 프로젝트의 Vertex AI를 서비스 계정 인증(`GOOGLE_APPLICATION_CREDENTIALS`)으로 쓴다.
3. **모델 호출 모듈:** 모델을 부르는 코드는 `app/llm/`에만 둔다.
   - 대화 루프·브리핑·채널은 `ChatModel` 약속과 `LLMError`/`TransientLLMError`만 안다. 제공사 SDK를 직접 import하지 않는다 (테스트로 강제).
   - `[llm] provider`로 제공사를 고른다. 선택된 어댑터만 불러온다.
   - 새 제공사를 붙이려면 `create(settings) -> LLM`을 가진 모듈을 만들고 `PROVIDERS`에 등록한다.
   - 시작할 때 인증과 모델 ID를 확인해서, 설정이 틀리면 첫 대화 전에 멈춘다.

## 설계 메모

- 대화 기록은 제공사 형식(Gemini `Content`) 그대로 저장하고 그대로 다시 보낸다. Gemini의 생각 서명(`thought_signature`)을 잃지 않기 위해서다. 그래서 제공사를 바꾸면 이전 대화 기록은 다시 보낼 수 없다. 마이그레이션 3에서 기존 기록을 보관 처리했다. 요약은 그대로 이어진다.
- SDK의 자동 함수 호출은 끈다. 도구 실행과 확인 버튼은 우리 레지스트리가 맡는다 (CLAUDE.md 7절).
- 오류로 모델 응답이 빠져 같은 역할의 턴이 이어지면, 어댑터가 하나로 합쳐 보낸다.
- 차단(안전 필터) 응답은 기록하지 않고, 사용자에게 "도와드리기 어렵다"고 답한다.

## 영향

- 의존성: `anthropic` 제거, `google-genai`(Apache-2.0) 추가.
- `.env`: `ANTHROPIC_API_KEY` 대신 `GEMINI_API_KEY`.
- ADR 0001·0003의 "가벼운 모델"은 이제 `[llm] light_model`이다.
