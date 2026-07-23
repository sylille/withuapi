# 위드유톡 — AI 추론 서버 API 문서

**버전** 0.1.0 · **최종 수정** 2026-07-23

AI 추론 서버는 위드유톡에서 전송되는 메시지의 사이버불링 가능성과 주변인 행동 유형을 판정합니다. 앱에서 이 API를 **직접 호출하지 않습니다.** 위드유톡 서버가 호출한 뒤, 결과를 챗봇 및 넛지 개입 모듈로 전달하는 구조입니다.

---

## 1. Base URL

```
https://would-statutes-veterans-important.trycloudflare.com
```

> ⚠️ **개발용 임시 URL입니다.** Cloudflare Quick Tunnel 방식이므로 연구 서버에서 터널 프로세스가 실행 중일 때만 동작하며, **터널을 재시작할 때마다 URL이 변경됩니다.** 코드에 하드코딩하지 마시고 설정값/환경변수로 관리하시는 쪽을 추천드립니다. 사용성 평가 전에 고정 URL을 발급할 예정입니다.

FastAPI가 자동 생성하는 스키마 문서 (항상 최신 기준):

```
GET  /docs          → Swagger UI
GET  /openapi.json  → 기계 판독용 스키마
```

본 문서와 `/openapi.json`의 내용이 다를 경우 **`/openapi.json`이 기준입니다.**

---

## 2. 인증

`/analyze` 요청에는 헤더에 공유 키를 추후 고정 URL 발급시 설정할 예정입니다.

`GET /health`는 키 없이 호출 가능합니다 (서버 상태 확인용).

| 상태 코드 | 의미 |
|---|---|
| `401` | `X-API-Key` 누락 또는 불일치 |
| `422` | 요청 본문 검증 실패 (§6 참고) |
| `500` | 서버 내부 모델/앙상블 오류 |

---

## 3. 개인정보 처리 원칙 (필수)

연구의 최소정보 원칙에 따라, 요청에는 **가명처리된 연구참여 코드만** 포함해야 합니다.

**절대 전송 금지:** 실명, 신원과 연결되는 별명, 전화번호, 학교명, 학반 정보, 프로필 사진 URL

`participant_code`에는 연구참여 코드(예: `P07`)를 사용합니다. 서버는 코드와 실제 신원 간의 매핑 정보를 저장하지 않습니다.

---

## 4. 엔드포인트

### `GET /health`

서버 생존 여부 및 모델 로딩 완료 여부를 확인합니다.

```json
{ "status": "ok", "ensemble_ready": true }
```

`ensemble_ready`가 `false`이면 서버는 켜져 있으나 모델이 아직 로딩 중이라는 의미이므로 잠시 후 재시도해 주세요. 터널 재시작 후에는 이 값을 먼저 확인한 뒤 요청을 보내시길 권장합니다.

---

### `POST /analyze`

최근 대화 맥락과 함께 신규 메시지 1건을 분석합니다.

#### 요청 본문 (Request body)

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `room_id` | string | ✅ | 채팅방 식별자 |
| `context` | array\<Message\> | — | 최근 대화 윈도우, **과거 → 최신 순서**. 직전 5~10개 메시지 권장. 기본값 `[]` |
| `new_message` | Message | ✅ | 분석 대상 메시지 |
| `has_image` | boolean | — | 이미지 포함 여부. 기본값 `false` |
| `left_chat` | boolean | — | 해당 사용자의 채팅방 이탈 여부. 기본값 `false` |
| `logs` | object \| null | — | 배제 탐지 모듈(모듈 C)용 메타데이터. 기본값 `null` |

**Message 객체**

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `participant_code` | string | ✅ | 가명처리된 발화자 ID |
| `text` | string | — | 메시지 본문. 기본값 `""` |
| `timestamp` | string \| null | — | ISO 8601 형식 권장 |
| `read_by_count` | int \| null | — | 읽은 인원 수 (배제·방관 신호용) |
| `response_latency_sec` | float \| null | — | 해당 발화자의 응답까지 걸린 시간(초) |

#### 응답 본문 (Response body)

| 필드 | 타입 | 설명 |
|---|---|---|
| `room_id` | string | 요청값 그대로 반환 |
| `cb_score` | float `0.0–1.0` | 앙상블 사이버불링 가능성 점수 |
| `cb_type` | string | `비해당` \| `언어적 폭력` \| `시각적 폭력` \| `배제` \| `composite` |
| `intervention_level` | string | `none` \| `suspect` \| `confirm` |
| `intervention_needed` | boolean | 앱 처리 편의용 플래그 |
| `bystander_behavior` | string \| null | `방어` \| `동조` \| `방관` — 사이버불링이 탐지된 경우에만 값이 채워짐 |
| `module_scores` | object | 모듈별 세부 점수 (§5 참고) |
| `evidence` | string | 판정 근거 요약. **내부 확인·디버깅 전용이며 아동에게 노출하지 마세요.** |

#### 개입 임계값

| `cb_score` | `intervention_level` | 앱 동작 |
|---|---|---|
| `0.75 미만` | `none` | 개입 없음 |
| `0.75 ~ 0.85` | `suspect` | 전송 전 경고, 10초 취소 기능 제공 |
| `0.85 이상` | `confirm` | 전체 개입: 상황 알림, 자동 캡처, 방어행동 선택지, 읽지 않은 채팅방 경고 표기 |

설계 문서의 .75 / .85 기준과 동일합니다. 이 값은 서버에서 설정 가능하므로(`SUSPECT_THRESHOLD`, `CONFIRM_THRESHOLD`), 앱에서 `cb_score`로 직접 판단하지 마시고 **`intervention_level` 값을 그대로 사용해 주세요.**

---

## 5. 모듈별 점수

`module_scores`는 파이프라인의 각 모듈 결과를 그대로 노출하여 판정 근거를 확인할 수 있게 합니다.

| 키 | 모듈 | 모델 | 상태 |
|---|---|---|---|
| `message` | A — 텍스트 기반 언어폭력 탐지 | KcELECTRA (미세조정) | ✅ 동작 |
| `context` | B — 대화 맥락 분석 | KLUE-RoBERTa (윈도우) | ✅ 동작 |
| `exclusion` | C — 배제 탐지 | 메타데이터 / 사회연결망 분석 | ⚠️ **미구현(stub)** |
| `bystander` | D — 주변인 행동 분류 | LLM few-shot | ✅ 동작 (비활성 시 `null`) |

**모듈 C는 아직 구현되지 않았습니다.** 호출 측에서 `logs.exclusion_score`로 값을 직접 넘기지 않는 한 항상 `0.0`을 반환합니다. 배제 탐지는 실제 메신저 로그 데이터가 확보되어야 개발 가능하므로, 현재 버전에서는 `cb_type: "배제"` 판정에 의존하지 말아 주세요.

값이 `null`이면 해당 요청에서 그 모듈이 실행되지 않았다는 의미입니다.

---

## 6. 호출 예시

### curl

요청 본문은 **파일로 분리**해서 보내시길 권장합니다. 여러 줄 JSON에 한글이 포함될 경우 셸 따옴표 처리 문제로 `json_invalid` 오류가 가장 자주 발생합니다.

```bash
export WITHU_URL="https://would-statutes-veterans-important.trycloudflare.com"
export WITHU_API_KEY="<발급받은 키>"

cat > /tmp/req.json << 'EOF'
{
  "room_id": "room_001",
  "context": [
    {"participant_code": "P03", "text": "오늘 급식 뭐야?"},
    {"participant_code": "P11", "text": "쟤한테 물어보지 마 냄새나"}
  ],
  "new_message": {"participant_code": "P07", "text": "ㅋㅋㅋ 진짜 존나 역겨움"},
  "has_image": false,
  "left_chat": false,
  "logs": null
}
EOF

# 전송 전 JSON 유효성 확인
python3 -m json.tool /tmp/req.json > /dev/null && echo "JSON OK"

curl -X POST "$WITHU_URL/analyze" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $WITHU_API_KEY" \
  --data-binary @/tmp/req.json
```

`-d` 대신 반드시 `--data-binary`를 사용하세요. `-d`는 개행을 제거하고 한글 등 멀티바이트 문자를 손상시킬 수 있습니다.

**응답 예시**

```json
{
  "room_id": "room_001",
  "cb_score": 0.93,
  "cb_type": "언어적 폭력",
  "intervention_level": "confirm",
  "intervention_needed": true,
  "bystander_behavior": "동조",
  "module_scores": {
    "message": 0.97,
    "context": 0.90,
    "exclusion": 0.0,
    "bystander": null
  },
  "evidence": "메시지 자체의 공격성이 높고 직전 대화 맥락에서도 사이버불링 정황이 확인됨"
}
```

---

### Python

```python
import os, requests

BASE = os.environ["WITHU_URL"]
HEADERS = {"X-API-Key": os.environ["WITHU_API_KEY"]}

def analyze(room_id, context, speaker, text, *,
            has_image=False, left_chat=False, logs=None):
    body = {
        "room_id": room_id,
        "context": [{"participant_code": s, "text": t} for s, t in context],
        "new_message": {"participant_code": speaker, "text": text},
        "has_image": has_image,
        "left_chat": left_chat,
        "logs": logs,
    }
    r = requests.post(f"{BASE}/analyze", json=body, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


# 1. 일반 메시지
print(analyze("room_001", [("P03", "오늘 급식 뭐야?")], "P07", "나 오늘 늦어"))

# 2. 언어적 폭력 + 주변인 동조 반응
ctx = [("P11", "쟤한테 물어보지 마 냄새나"), ("P03", "ㅇㅇ")]
print(analyze("room_001", ctx, "P07", "ㅋㅋㅋ 진짜 존나 역겨움"))

# 3. 이미지 전송
print(analyze("room_001", ctx, "P07", "", has_image=True))

# 4. 배제 신호를 호출 측에서 전달 (모듈 C stub)
print(analyze("room_001", ctx, "P07", "우리끼리 딴방 파자",
              logs={"exclusion_score": 0.8}))
```

반드시 `data=`가 아닌 **`json=body`**를 사용하세요. `requests`가 UTF-8 직렬화를 올바르게 처리합니다.

---

### Flutter / Dart

```dart
import 'dart:convert';
import 'package:http/http.dart' as http;

class WithUApi {
  final String baseUrl;   // --dart-define=WITHU_URL=...
  final String apiKey;    // --dart-define=WITHU_API_KEY=...

  WithUApi({required this.baseUrl, required this.apiKey});

  Future<Map<String, dynamic>> analyze({
    required String roomId,
    required List<Map<String, dynamic>> context,
    required String speaker,
    required String text,
    bool hasImage = false,
    bool leftChat = false,
    Map<String, dynamic>? logs,
  }) async {
    final res = await http
        .post(
          Uri.parse('$baseUrl/analyze'),
          headers: {
            'Content-Type': 'application/json; charset=utf-8',
            'X-API-Key': apiKey,
          },
          body: utf8.encode(jsonEncode({
            'room_id': roomId,
            'context': context,
            'new_message': {'participant_code': speaker, 'text': text},
            'has_image': hasImage,
            'left_chat': leftChat,
            'logs': logs,
          })),
        )
        .timeout(const Duration(seconds: 30));

    if (res.statusCode != 200) {
      throw Exception('analyze 실패 ${res.statusCode}: ${res.body}');
    }
    // 한글 응답은 반드시 bodyBytes를 utf8로 디코딩 (res.body 사용 금지)
    return jsonDecode(utf8.decode(res.bodyBytes)) as Map<String, dynamic>;
  }
}
```

사용 예:

```dart
final result = await api.analyze(
  roomId: 'room_001',
  context: [
    {'participant_code': 'P11', 'text': '쟤한테 물어보지 마 냄새나'},
  ],
  speaker: 'P07',
  text: 'ㅋㅋㅋ 진짜 존나 역겨움',
);

switch (result['intervention_level']) {
  case 'confirm':
    showFullIntervention(result);   // 상황 알림 · 자동 캡처 · 방어행동 선택지
    break;
  case 'suspect':
    showPreSendWarningWithCancel(result);  // 전송 전 경고 · 10초 취소
    break;
  default:
    break;  // 개입 없음
}
```

---

## 7. 응답 속도 및 연동 방식

모듈 D가 외부 LLM API를 호출하기 때문에 `/analyze` 전체 응답에는 **약 1~3초**가 소요됩니다. 모듈 A·B는 서버 GPU에서 로컬 실행되므로 수십 밀리초 수준으로 빠릅니다.

따라서 메시지 전송을 그대로 블로킹하기에는 느립니다. 다음과 같이 분리하시길 권장합니다.

1. **동기 호출** — 전송 전 경고 판단용. 1초 미만 응답이 필요하면 주변인 모듈을 끈 상태(`ENABLE_BYSTANDER=0`)로 A+B 결과만 받습니다.
2. **비동기 호출** — 메시지 전송 이후 주변인 모듈을 포함해 재호출하여 방관·동조·방어 개입을 처리합니다.

클라이언트 타임아웃은 30초로 설정하고, **실패 시에는 열어두는 방식(fail open)**으로 처리해 주세요. API 오류나 타임아웃이 발생하면 메시지를 차단하지 말고 정상 전송하되, 실패 로그를 남겨 연구팀에 전달해 주시면 됩니다. AI 서버 장애로 아동의 대화가 막히는 것이 탐지 실패보다 더 큰 문제입니다.

---

## 8. 오류 처리

```json
{
  "detail": [
    {"type": "json_invalid", "loc": ["body", 314], "msg": "JSON decode error"}
  ]
}
```

| 증상 | 원인 |
|---|---|
| `json_invalid` / "Extra data" | 본문 형식 오류. 주로 셸 따옴표 처리 문제 또는 `-d` 플래그를 두 번 사용해 본문이 이어붙은 경우 |
| `422` + `field required` | `room_id` 또는 `new_message` 누락 |
| `401` | `X-API-Key` 누락 또는 불일치 |
| 연결 거부 / 502 | 터널이 종료·재시작됨 → 연구팀에 새 URL 요청 |
| 한글이 깨져서 표시됨 | UTF-8 명시적 디코딩 필요 (`utf8.decode(res.bodyBytes)`) |

---

## 9. 현재 버전의 한계 (v0.1.0)

- **모듈 C(배제)는 미구현 상태입니다.** 실제 메신저 로그가 수집되기 전까지 실질적인 배제 탐지는 동작하지 않습니다.
- **시각적 폭력 탐지는 구현되지 않았습니다.** `has_image` 값은 수신·전달되지만 이미지 분석 모델은 아직 연결되어 있지 않습니다. 앱 내 인체 감지 및 전송 동의 확인 기능은 별도의 클라이언트 기능입니다.
- **학습 데이터의 한계.** 도메인 말뭉치에 포함된 사이버불링 이벤트는 5건, 주변인 행동이 라벨링된 반응은 67건입니다. 검증에 사용한 held-out 테스트 이벤트도 1건뿐이므로, **도메인 성능 지표는 방향성 참고용**입니다. 실제 현장 성능은 파일럿 데이터 확보 전까지 검증되지 않은 것으로 간주해 주세요.
- **신조어 RAG 데이터베이스는 아직 연결되지 않았습니다.** 새로운 은어·신조어 반영에는 현재 재학습이 필요합니다.
- **요청 수 제한(rate limiting)이 없습니다.** 임시 터널 환경이므로 부하 테스트는 진행하지 말아 주세요.
