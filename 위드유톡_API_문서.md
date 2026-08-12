# 위드유톡 — AI 추론 서버 API 문서

**버전** 0.2.0 · **최종 수정** 2026-08-12

AI 추론 서버는 위드유톡에서 전송되는 메시지의 **사이버불링 가능성**, **가해자·피해자 판별**,
**주변인 행동 유형**을 판정합니다. 앱은 이 API를 **직접 호출하지 않습니다.**
위드유톡 서버가 호출한 뒤 결과를 챗봇·넛지 개입 모듈로 전달하는 구조입니다.

---

## 0. v0.1.0 → v0.2.0 변경사항

이번 버전은 **오탐(false positive)을 크게 줄이고 가해자/피해자 정보를 추가**합니다.
앱 연동 관점에서 바뀐 점은 다음과 같습니다.

**요청(Request)에 필드 1개 추가**
- `new_message.is_defense_action` (boolean, 기본 `false`) — **"친구를 위로하는 메시지 보내기"** 등
  방어/위로 기능으로 전송되는 메시지에는 반드시 `true`로 보내주세요. (아래 §3-1)

**응답(Response)에 필드 추가**
- `attribution` — **가해자/피해자 판별 결과** (신규, §4-3-1)
- `suppressed`, `guard_reason` — 위로/방어성 발화로 억제되었는지 여부 (신규)

**동작 변경**
- **위로 메시지 오탐 수정.** "네 잘못이 아니야" 같은 위로 문구가 언어폭력으로 오판되어 개입 알림이
  가던 문제를 수정했습니다. `is_defense_action=true`이면 점수가 0으로 억제됩니다.
- **개입 확정(`confirm`) 조건 강화.** 이제 점수가 높기만 해서는 안 되고 **실제 표적(피해자)이 확인될 때만**
  `confirm`이 됩니다. 표적 없는 욕설(밴터)은 `suspect`(가해자 사전 경고)까지만 올라갑니다.
- **`intervention_level`의 `suspect` 티어는 유지되나, 향후 로드맵상 제거 예정입니다.**
  앱은 **전면 개입 판단을 `attribution.is_bullying` 기준으로 이전**해 주세요. (§4-3-2)

> ⚠️ **주의:** `attribution.aggressors` / `attribution.victim`은 **`is_bullying=true`일 때만 신뢰**하세요.
> `false`일 때 값은 디버깅용이며 개입 근거로 사용하면 안 됩니다.

---

## 1. Base URL

```
https://quote-lanes-adopted-disco.trycloudflare.com 
```

> ⚠️ **개발용 임시 URL입니다.** Cloudflare Quick Tunnel 방식이라 연구 서버에서 터널이 실행 중일 때만
> 동작하며, **재시작 시 URL이 변경됩니다.** 코드에 하드코딩하지 말고 설정값/환경변수로 관리하세요.
> 사용성 평가 전 고정 URL을 발급할 예정입니다.

FastAPI 자동 스키마(항상 최신 기준):

```
GET  /docs          → Swagger UI
GET  /openapi.json  → 기계 판독용 스키마
```

본 문서와 `/openapi.json`이 다르면 **`/openapi.json`이 기준입니다.**

---

## 2. 인증

| 상태 코드 | 의미 |
|---|---|
| `422` | 요청 본문 검증 실패 (§7) |
| `500` | 서버 내부 모델/앙상블 오류 |

---

## 3. 개인정보 처리 원칙 (필수)

최소정보 원칙에 따라 요청에는 **가명처리된 연구참여 코드만** 포함합니다.

**절대 전송 금지:** 실명, 신원 연결 가능한 별명, 전화번호, 학교/학반, 프로필 사진 URL

`participant_code`에는 연구참여 코드(예: `P07`)를 사용합니다. 서버는 코드↔실제 신원 매핑을 저장하지 않습니다.
응답의 `evidence`는 **내부 디버깅 전용이며 아동에게 노출하지 마세요.**

### 3-1. `is_defense_action` 사용 원칙

앱이 방어/위로 기능으로 **생성·전송한 메시지**에는 `is_defense_action=true`를 보냅니다.
이 플래그가 있으면 서버가 해당 메시지를 가해 후보에서 제외하고 점수를 0으로 억제합니다.
플래그가 없어도 내용 기반 가드가 위로성 문구를 일부 걸러내지만, **플래그가 훨씬 정확**합니다.

---

## 4. 엔드포인트

### `GET /health`

```json
{ "status": "ok", "ensemble_ready": true }
```

`ensemble_ready`가 `false`이면 모델 로딩 중입니다. 터널 재시작 후에는 이 값을 먼저 확인하세요.

---

### `POST /analyze`

최근 대화 맥락과 함께 신규 메시지 1건을 분석합니다.

#### 4-1. 요청 본문

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `room_id` | string | ✅ | 채팅방 식별자 |
| `context` | array\<Message\> | — | 최근 대화 윈도우, **과거 → 최신 순서**. 직전 5~10개 권장. 기본값 `[]` |
| `new_message` | Message | ✅ | 분석 대상 메시지 |
| `has_image` | boolean | — | 이미지 포함 여부. 기본값 `false` |
| `left_chat` | boolean | — | 사용자 채팅방 이탈 여부. 기본값 `false` |
| `logs` | object \| null | — | 배제 탐지 모듈(C)용 메타데이터. 기본값 `null` |

**Message 객체**

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `participant_code` | string | ✅ | 가명처리된 발화자 ID |
| `text` | string | — | 메시지 본문. 기본값 `""` |
| `timestamp` | string \| null | — | ISO 8601 권장 |
| `read_by_count` | int \| null | — | 읽은 인원 수 (배제·방관 신호용) |
| `response_latency_sec` | float \| null | — | 응답까지 걸린 시간(초) |
| `is_defense_action` | boolean | — | **[신규]** 방어/위로 기능 발화 여부. 기본값 `false` (§3-1) |

#### 4-2. 응답 본문

| 필드 | 타입 | 설명 |
|---|---|---|
| `room_id` | string | 요청값 그대로 |
| `cb_score` | float `0.0–1.0` | 앙상블 사이버불링 가능성 점수 (**가드 적용 후**) |
| `cb_type` | string | `비해당` \| `언어적 폭력` \| `시각적 폭력` \| `배제` \| `composite` |
| `intervention_level` | string | `none` \| `suspect` \| `confirm` (§4-3-2) |
| `intervention_needed` | boolean | `confirm`일 때만 `true` |
| `attribution` | object | **[신규]** 가해자/피해자 판별 (§4-3-1) |
| `suppressed` | boolean | **[신규]** 위로/방어성 발화로 점수가 억제됐는지 |
| `guard_reason` | string | **[신규]** `defense_action` \| `prosocial_content` \| `none` |
| `bystander_behavior` | string \| null | `방어` \| `동조` \| `방관` — 사이버불링 탐지 시에만 |
| `module_scores` | object | 모듈별 세부 점수 (§5) |
| `evidence` | string | 판정 근거 요약. **내부 전용, 아동 노출 금지.** |

#### 4-3-1. `attribution` 객체

| 필드 | 타입 | 설명 |
|---|---|---|
| `is_bullying` | boolean | 실제 사건 성립 여부. **역할 정보는 이 값이 `true`일 때만 신뢰** |
| `aggressors` | array\<string\> | 가해자 participant_code 목록 |
| `victim` | string \| null | 피해자 participant_code |
| `victim_reason` | string | 피해자 지목 근거: `explicit_target` \| `name_mention` \| `distress_signal` \| `turn_adjacency` \| `no_signal` |
| `confidence` | float `0.0–1.0` | 판별 신뢰도 (가해자/피해자 분리도) |
| `drop_reason` | string | `is_bullying=false`일 때 제외 사유 (디버깅용): `no_target` \| `low_confidence` \| `weak_target` \| `not_sustained` \| `diffuse_aggression` \| `mutual_banter` 등 |

#### 4-3-2. 개입 판단 (중요)

두 신호를 **분리**해서 사용하세요.

- **메시지 단위** (`cb_score`, `cb_type`, `suppressed`) — "이 메시지가 공격적인가?"
  → 빨간 강조, 전송 전 경고에 사용. (동기 호출, 빠름)
- **사건 단위** (`attribution.is_bullying`, `aggressors`, `victim`) — "가해자→피해자 사건이 있는가?"
  → 전면 개입, 방어행동 선택지, 역할 기반 기능에 사용. (비동기 호출)

`intervention_level` 매핑:

| 값 | 조건 | 앱 동작(권장) |
|---|---|---|
| `none` | 공격성 낮음 또는 위로성 억제됨 | 개입 없음 |
| `suspect` | 메시지 공격성 높음(가해자 대상) · 표적 미확인 | 전송 전 경고, 10초 취소 |
| `confirm` | 공격성 높음 **AND** `is_bullying=true` | 전면 개입: 상황 알림·자동 캡처·방어행동·읽지않음 표기 |

> **권장 마이그레이션:** 전면 개입 트리거를 `intervention_level == 'confirm'` 대신
> **`attribution.is_bullying == true`**로 이전하세요. `suspect`(사전 경고)는 당분간 유지되지만,
> `.75/.85` 임계 티어 자체는 로드맵상 제거 예정입니다.

---

## 5. 모듈별 점수

| 키 | 모듈 | 모델 | 상태 |
|---|---|---|---|
| `message` | A — 텍스트 언어폭력 | KcELECTRA(미세조정) | ✅ 동작 |
| `context` | B — 대화 맥락 | KLUE-RoBERTa(윈도우) | ✅ 동작 |
| `exclusion` | C — 배제 탐지 | 메타데이터/SNA | ⚠️ 미구현(stub) |
| `bystander` | D — 주변인 행동 | LLM few-shot | ✅ 동작 (비활성 시 `null`) |

모듈 C는 미구현으로, `logs.exclusion_score`를 직접 넘기지 않으면 항상 `0.0`입니다.
값이 `null`이면 해당 요청에서 그 모듈이 실행되지 않았다는 의미입니다.

---

## 6. 호출 예시

### 6-1. Python

```python
import os, requests

BASE = os.environ["WITHU_URL"]
HEADERS = {"X-API-Key": os.environ["WITHU_API_KEY"]}

def analyze(room_id, context, speaker, text, *,
            has_image=False, left_chat=False, logs=None, is_defense_action=False):
    body = {
        "room_id": room_id,
        "context": [{"participant_code": s, "text": t} for s, t in context],
        "new_message": {"participant_code": speaker, "text": text,
                        "is_defense_action": is_defense_action},
        "has_image": has_image, "left_chat": left_chat, "logs": logs,
    }
    r = requests.post(f"{BASE}/analyze", json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

# 1) 언어폭력 + 피해자 지목
ctx = [("P11", "하늘 너 진짜 냄새나"), ("하늘", "왜그래")]
res = analyze("room_001", ctx, "P11", "하늘 너 같은 애는 나가라")
print(res["attribution"])   # {'is_bullying': True, 'aggressors': ['P11'], 'victim': '하늘', ...}

# 2) 위로 메시지 (방어 기능) → 억제됨
print(analyze("room_001", [], "P05", "네 잘못이 아니야.", is_defense_action=True))
# cb_score=0.0, suppressed=True, intervention_level='none'
```

`data=`가 아닌 **`json=body`**를 사용하세요 (UTF-8 직렬화).

### 6-2. Flutter / Dart

```dart
final res = await api.analyze(
  roomId: 'room_001',
  context: [ {'participant_code': 'P11', 'text': '하늘 너 냄새나'} ],
  speaker: 'P11',
  text: '하늘 너 같은 애는 나가라',
);

final attr = res['attribution'] as Map<String, dynamic>;

// 전면 개입은 is_bullying 기준으로 판단 (권장)
if (attr['is_bullying'] == true) {
  showFullIntervention(
    aggressors: List<String>.from(attr['aggressors']),
    victim: attr['victim'],
  );
} else if (res['intervention_level'] == 'suspect') {
  showPreSendWarningWithCancel(res);   // 전송 전 경고 · 10초 취소
}
// 한글 응답은 utf8.decode(res.bodyBytes)로 디코딩
```

> 요청 본문은 파일 분리 + `--data-binary`(curl) 권장. `-d`는 개행 제거·멀티바이트 손상 위험.

---

## 7. 응답 속도 및 연동 방식

- 모듈 A·B는 GPU 로컬 실행으로 수십 ms, **모듈 D는 외부 LLM 호출로 `/analyze` 전체 1~3초** 소요.
- 권장: **동기 호출**(전송 전 경고, `cb_score`·`cb_type`; 주변인 모듈 끔 `ENABLE_BYSTANDER=0`) +
  **비동기 호출**(전송 후 `attribution`·주변인 포함 재호출).
- 클라이언트 타임아웃 30초. **실패 시 열어두기(fail open):** API 오류/타임아웃 시 메시지를 차단하지 말고
  정상 전송하되 실패 로그를 연구팀에 전달. **AI 장애로 대화가 막히는 것이 탐지 실패보다 큰 문제입니다.**

가드·게이트는 오탐만 줄이는 방향으로 동작하므로, 불확실하면 `is_bullying=false`로 안전하게 미개입합니다.

---

## 8. 오류 처리

| 증상 | 원인 |
|---|---|
| `json_invalid` / "Extra data" | 본문 형식 오류(셸 따옴표, `-d` 중복 등) |
| `422` + `field required` | `room_id` 또는 `new_message` 누락 |
| `401` | `X-API-Key` 누락/불일치 |
| 연결 거부 / 502 | 터널 종료·재시작 → 연구팀에 새 URL 요청 |
| 한글 깨짐 | UTF-8 명시적 디코딩 필요(`utf8.decode(res.bodyBytes)`) |

---

## 9. 현재 버전의 한계 (v0.2.0)

- **모듈 C(배제) 미구현.** 개발 완료 전까지 배제 탐지 미동작.
- **시각적 폭력 미구현.** `has_image`는 수신·전달되나 이미지 분석 모델 미연결.
- **역할 판별의 표적 신호.** 가해자가 피해자를 **호명하지 않거나** 피해자가 윈도우에 없으면(예: 부재중)
  `is_bullying=false`가 될 수 있습니다. 이 경우 메시지 자체는 `언어적 폭력`으로 표시되지만 사건은 미성립입니다.
- **`intervention_level` 티어 제거 예정.** §4-3-2 마이그레이션 참고.
- **요청 수 제한 없음.** 임시 터널 환경이므로 부하 테스트 금지.
