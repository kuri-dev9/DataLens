# UI 개발자 인수인계

| 항목 | 내용 |
|---|---|
| 작성 | 2026-09-21 |
| 대상 | DataLens 운영자 (전달하는 쪽) |
| 목적 | UI 개발자에게 무엇을 주고 무엇을 주지 않을지 정리 |

---

## 1. 전달할 것

| 파일 | 용도 |
|---|---|
| `docs/DATALENS-UI-GUIDE.md` | 사람이 읽는 연동 안내서. 먼저 읽힐 것 |
| `docs/API.md` | HTTP 엔드포인트 레퍼런스 |
| `docs/DATALENS-API-FOR-AI.md` | 코딩 에이전트에게 그대로 물릴 정밀 스펙 |
| `scripts/mock_server.py` | 목 서버. 실제 서버 없이 개발 가능 |
| `demo/datalens-demo.html` | 동작하는 참조 구현 |

이 다섯 개면 서버 접속 없이 UI를 완성할 수 있습니다. 저장소 전체를 주지 않아도 됩니다.

```sh
mkdir -p datalens-ui-kit/{docs,scripts,demo}
cp docs/DATALENS-UI-GUIDE.md docs/API.md docs/DATALENS-API-FOR-AI.md datalens-ui-kit/docs/
cp scripts/mock_server.py datalens-ui-kit/scripts/
cp demo/datalens-demo.html datalens-ui-kit/demo/
zip -r datalens-ui-kit.zip datalens-ui-kit
```

보내기 전에 한 번 확인하세요. 아무것도 안 나와야 합니다.

```sh
grep -rnE '10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|\.ts\.net|dl_' datalens-ui-kit/
```

## 2. 전달하지 말 것

| 항목 | 이유 |
|---|---|
| Tailscale IP (`100.x.x.x`), `*.ts.net` 주소 | 내부망 구조가 드러납니다 |
| 사내 IP (`192.168.x.x`, `10.x.x.x`) | 같은 이유 |
| 운영 `DATALENS_API_KEY` | 개발자는 목 서버의 `dev-key`면 충분합니다 |
| `.env` | 위 두 가지가 전부 들어 있습니다 |
| 브라우저 세션이 담긴 디버그 JSON | `request.url`에 실제 주소가 박혀 있습니다 |

데모 HTML에는 Base URL이 **비어 있고** 첫 실행 시 설정 창이 뜹니다. 주소는 각자
입력하는 값이라 파일 자체는 안전합니다. 다시 하드코딩하지 마세요.

## 3. 개발 단계별 접근 방법

### 1단계 — 목 서버 (접속 불필요, 권장 출발점)

UI 개발의 90%는 여기서 끝납니다. 설치할 패키지가 없습니다.

```sh
python3 scripts/mock_server.py            # http://localhost:18121
python3 scripts/mock_server.py --delay 5  # 실제 서버의 느린 응답 흉내
```

`http://localhost:18121/demo/` 를 열고 Base URL `http://localhost:18121`,
API key `dev-key`를 넣으면 바로 동작합니다. 질문에 `실패` 또는 `기억`을 넣으면
그 시나리오가 재현됩니다.

### 2단계 — 실제 서버 연동 (접속 필요)

여기서부터는 내부망을 드러내지 않고 열어줄 방법이 필요합니다. 위험도가 낮은 순서입니다.

**A. 리버스 프록시 + 공개 호스트명 (권장)**

nginx/Caddy를 앞에 두고 `https://datalens-dev.<회사도메인>` 같은 이름으로 노출합니다.
개발자는 내부 주소도 Tailscale도 모릅니다.

```
# Caddy 예시
datalens-dev.example.com {
    reverse_proxy <내부주소>:18121 {
        flush_interval -1        # SSE 버퍼링 금지. 없으면 스트리밍이 끊겨 보입니다
    }
}
```

`flush_interval -1`(nginx는 `proxy_buffering off;`)이 빠지면 토큰이 한꺼번에 몰려
나옵니다. SSE를 쓰는 이유가 사라지므로 반드시 넣으세요.

**B. Cloudflare Tunnel 같은 아웃바운드 터널**

인바운드 포트를 열지 않고 공개 호스트명을 얻습니다. 사내 방화벽을 못 건드릴 때 유용합니다.

**C. 개발자를 tailnet에 초대**

가장 쉽지만 내부망 구조가 그대로 보입니다. 외부 인력이라면 피하세요.

어느 쪽이든 **개발자 전용 API 키를 따로 발급**하고, 끝나면 회수하세요.

## 4. 개발자에게 전할 한 줄 요약

> `docs/DATALENS-UI-GUIDE.md`부터 읽으세요. `python3 scripts/mock_server.py`를 띄우면
> 서버 접속 없이 바로 개발할 수 있고, `demo/datalens-demo.html`이 동작하는 참조
> 구현입니다. 실제 서버 주소와 API 키는 통합 단계에서 따로 전달합니다.

## 5. 지금 구현 상태 (2026-09-21 기준)

동작을 확인한 것만 적습니다.

- 세션 생성/삭제, 메시지(JSON·SSE 양쪽), dataset 행/메타 조회, 카탈로그 조회
- 진행 트레이스 — 단계별 `intent`/`result`/`error`/`recovery`
- 실패 시 부분 결과 보존과 중단 지점(`failed_step`) 전달
- 성공한 풀이를 학습해 다음 번 탐색을 건너뛰는 기억 기능

성능 실측값입니다. UI 타임아웃과 로딩 화면은 이 숫자를 기준으로 잡으세요.

| 질문 유형 | 도구 호출 | 소요 |
|---|---:|---:|
| 단순 대화 | 0 | 약 20초 |
| 테이블 목록 | 1 | 약 18초 |
| 집계 조회 (기억 없음) | 9 | 약 187초 |
| 집계 조회 (기억 활용) | 2~3 | 측정 예정 |

마지막 줄은 아직 실측 전입니다. 기억 기능을 붙인 직후라 수치가 나오면 갱신합니다.
