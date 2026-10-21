# mcp-saju

> **이 폴더는 [molpass/mcp-saju](https://github.com/molpass/mcp-saju)(MIT, 커밋 `0a11e91`)의 포크다.** 원본 라이선스는 [LICENSE](LICENSE)에 그대로 둔다. 사주 라이프 플래너를 위해 바꾼 점은 다음과 같다.
>
> - `calculate_saju`에 `now`(YYYY-MM-DD) 입력 추가. 세운·월운·만세력 기준일을 고정할 수 있다(그날 23:59 KST 기준).
> - 신규 도구 `get_luck_calendar(…출생 정보, from, months=12)`: 기준일이 속한 절기 월부터 N개 절기 월의 월운·세운 간지, 십성, 12운성, 시작일·종료일을 JSON으로 돌려준다. ssaju를 하루 단위로 조회해 절입일을 찾는다.
> - 회귀 테스트 `test/luck_calendar.test.js` 추가(2026-10-21 기준 12개월 표, 입동·입춘 경계, `now` 반영).
>
> 빌드: `npm install && npm run build` → 실행: `node dist/index.js` (stdio) → 테스트: `npm test`

[ssaju](https://www.npmjs.com/package/ssaju) 만세력 엔진을 래핑한 **stdio MCP 서버**. MCP 클라이언트(Claude Desktop·Claude Code 등)가 생년월일시로 사주·만세력을 계산하도록 `calculate_saju` 도구를 노출한다.

- 엔진: ssaju (MIT · TypeScript · 의존성 0 · 원국·십성·12운성·합충형파해·신살·격국·용신·대운·세운·월운·공망 한 번에 산출)
- 트랜스포트: stdio (클라이언트가 서브프로세스로 실행)
- 라이선스: MIT

## 설치 / 빌드

```bash
npm install
npm run build      # tsc → dist/index.js
```

> **git 필요**: 엔진 의존성 `ssaju`를 [`molpass/ssaju`](https://github.com/molpass/ssaju) 포크에서 git으로 가져온다(`serializeSaju` 포함, 커밋 핀 고정). 따라서 `npm install` 시 **git이 설치돼 있어야** 하며, 설치 과정에서 포크를 clone해 자동 빌드한다(빌드 devDependencies를 잠시 내려받음).

## MCP 클라이언트 등록

클라이언트의 MCP 서버 설정에 stdio 명령으로 추가한다.

```json
{
  "mcpServers": {
    "saju": {
      "command": "node",
      "args": ["/abs/path/mcp-saju/dist/index.js"]
    }
  }
}
```

## 도구: `calculate_saju`

생년월일시로 사주·만세력을 계산한다.

### 입력

| 파라미터 | 타입 | 기본값 | 설명 |
|---|---|---|---|
| `year` | number | (필수) | 연도 |
| `month` | number | (필수) | 월 1–12 |
| `day` | number | (필수) | 일 1–31 |
| `hour` | number | 12 | 시 0–23 |
| `minute` | number | 0 | 분 0–59 |
| `gender` | `"남"` \| `"여"` | (없음) | **대운 방향에 영향 → 정확한 결과 위해 제공 권장** |
| `calendar` | `"solar"` \| `"lunar"` | `"solar"` | 입력 달력 종류 |
| `leap` | boolean | false | 음력 윤달 여부 |
| `timezone` | string | `"Asia/Seoul"` | IANA 시간대 |
| `longitude` | number | (없음) | 진태양시 보정용 경도 |
| `applyLocalMeanTime` | boolean | (없음) | 진태양시 보정 적용 |
| `format` | `"compact"` \| `"markdown"` \| `"json"` | `"compact"` | 출력 형식 (compact=LLM용 압축, markdown=사람용 상세, json=구조화 데이터) |

### 출력

`format`에 따라 ssaju의 `toCompact()`(기본, ~950토큰) 또는 `toMarkdown()`(~2,170토큰) 문자열을, `json`이면 `serializeSaju()`로 직렬화한 구조화 JSON(함수 필드 제외)을 텍스트로 반환한다.

### 예시

입력:
```json
{ "year": 2001, "month": 11, "day": 3, "hour": 14, "minute": 20, "gender": "남" }
```

출력(첫 줄):
```
일간 庚(경)금+  강약: 강(78)  격: 인수격  용신: 己, 丙, 癸
```

## 검증

```bash
npm test
```

`test/smoke.test.js` — 빌드된 서버에 stdio JSON-RPC 1왕복(`initialize` → `tools/call`)을 보내 위 예시 입력의 결과에 **일간 庚 · 인수격 · 용신 己·丙·癸**가 그대로 나오는지 확인하는 회귀 테스트.

MCP Inspector로 수동 검증:

```bash
npx @modelcontextprotocol/inspector node dist/index.js
```

GUI에서 `calculate_saju`를 위 예시 입력으로 호출하면 동일 결과를 확인할 수 있다.

## About / 제작

**Hermes Agent용 MCP** — molpass의 바이브 코딩(vibe coding) 프로젝트.

- 아이디어·방향: **molpass (이정훈)** · https://zeolinex.com
- 기획: **Claude (Chat)**
- 개발: **Claude Code**

자가 호스팅 [Hermes Agent](https://github.com/NousResearch/hermes-agent)에 도구로 붙여 쓰는 MCP 서버입니다.

같은 모음:
- [mcp-saju](https://github.com/molpass/mcp-saju) — 사주명리 만세력
- [mcp-qr](https://github.com/molpass/mcp-qr) — QR 코드 생성
- [mcp-biorhythm](https://github.com/molpass/mcp-biorhythm) — 바이오리듬
- [mcp-astrology](https://github.com/molpass/mcp-astrology) — 서양 점성술 네이탈 차트
- [mcp-ziwei](https://github.com/molpass/mcp-ziwei) — 자미두수 명반
- [mcp-numerology](https://github.com/molpass/mcp-numerology) — 수비학
- [mcp-liuren](https://github.com/molpass/mcp-liuren) — 대육임
- [mcp-qimen](https://github.com/molpass/mcp-qimen) — 기문둔갑
- [mcp-taiyi](https://github.com/molpass/mcp-taiyi) — 태을신수
- [mcp-weather](https://github.com/molpass/mcp-weather) — 한국 날씨·미세먼지
- [mcp-newsfeed](https://github.com/molpass/mcp-newsfeed) — 한국 주요뉴스
