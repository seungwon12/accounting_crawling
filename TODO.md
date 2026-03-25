# TODO 목록

---

## 이슈 1: 섹션 제목 교차참조 하위 전파 (미구현)

### 현상

섹션 제목 자체에 교차참조가 포함되는 경우가 있습니다.

예시:
- 섹션 "적어도 12개월 이상 부채의 결제를 연기할 수 있는 권리(문단 69⑷)"
  → 제목에 "문단 69⑷" 참조가 있지만 하위 문단들의 `cross_references`에 전파되지 않음

### 기대 동작

섹션 제목에 포함된 교차참조(`cross_references`)를 해당 섹션의 하위 문단들에 전파.

### 구현 위치

`src/crawler/orchestrator.py` — `crawl_standard` 후처리 단계에 추가 함수 필요.

### 구현 시 고려사항

- 섹션 제목은 `TocItem.title` 필드에 저장됨
- 하위 문단 판별: `Paragraph.toc_path`가 해당 섹션을 포함하는지로 판단
- 중복 추가 방지: 이미 동일한 교차참조가 있으면 건너뜀
- 전파 방향: 섹션 → 직계 하위 문단만 전파 vs. 전체 하위 트리 전파 여부 결정 필요
- 섹션 제목의 교차참조 파싱: 현재 JS 파서가 TOC 제목까지 파싱하는지 확인 필요

### 우선순위

낮음 — 데이터 품질에 미치는 영향이 제한적임.

---

## 교차 기준서 범위 전개 기능

## 문제 설명

교차참조 `paragraph_ids`에 **타 기준서의 범위 표기**(예: `"44A~44E"`)가
전개되지 않고 원본 문자열 그대로 남아 있는 경우가 있습니다.

### 구체적 사례 (현재 확인된 폴백 케이스 3건)

| 기준서 | 문단 | 참조 대상 기준서 | 미전개 범위 | 실제 전개 기대값 |
|--------|------|-----------------|------------|-----------------|
| 1001   | IG10 | **1007**        | `44A~44E`  | `["44A","44B","44C","44D","44E"]` |
| 1016   | 6    | **1041**        | `5A~5B`    | `["5A","5B"]` |
| 1016   | 78   | **1036**        | `73~`      | `["73", "74", ...]` (열린 범위) |

#### 1001호 IG10 상세

```json
{
  "type": "paragraph",
  "display_text": "문단 44A~44E",
  "standard_number": "1007",
  "range": "44A~44E",
  "paragraph_ids": ["44A~44E"]   // ← 전개되지 않은 상태
}
```

기대하는 최종 상태:

```json
{
  "paragraph_ids": ["44A", "44B", "44C", "44D", "44E"]
}
```

실제로 1007호 JSON에는 `44A`, `44B`, `44C`, `44D`, `44E` 문단이 인덱스
50~54에 순서대로 존재합니다.

---

## 현재 동작 — `expand_paragraph_ranges` 폴백 경로

`src/utils.py`의 `expand_paragraph_ranges` 함수는 `all_paragraph_numbers`(현재
기준서 문단 번호 리스트)를 받아 범위를 전개합니다.

```python
# src/utils.py:189
if start in all_paragraph_numbers and end in all_paragraph_numbers:
    # 정상 전개
    ...
else:
    # 타 기준서 참조 — 원본 유지 (폴백)
    if item not in seen:
        result.append(item)   # "44A~44E" 가 그대로 저장됨
        seen.add(item)
```

호출 지점(`src/crawler/orchestrator.py:172`):

```python
def _expand_cross_reference_ranges(paragraphs: list[Paragraph]) -> None:
    all_numbers = [p.number for p in paragraphs]  # 현재 기준서만!
    for para in paragraphs:
        for ref in para.cross_references:
            if ref.type == "paragraph" and ref.paragraph_ids:
                ref.paragraph_ids = expand_paragraph_ranges(ref.paragraph_ids, all_numbers)
```

`all_numbers`가 **현재 기준서의 문단 번호만** 담고 있으므로, 타 기준서에 속하는
`44A`나 `44E`는 조회되지 않아 폴백 경로로 빠집니다.

---

## 원인

`crawl_standard`는 단일 기준서를 독립적으로 처리합니다.
섹션 파싱 → 후처리 → JSON 저장이 하나의 흐름으로 완결되기 때문에,
후처리 시점(`_expand_cross_reference_ranges` 호출 시점)에는
**타 기준서 문단 목록이 메모리에 없고** 아직 저장도 안 됐을 수 있습니다.

---

## 해결 방안

### 방안 A — 포스트프로세싱 스크립트 (권장)

전체 크롤링 완료 후 `output/standards/` 폴더의 JSON을 읽어
한 번에 교차 기준서 범위를 전개합니다.

**장점**
- 크롤러 코드 변경 최소화
- 이미 완료된 크롤링 결과를 재처리 가능
- 단독 실행·재실행 가능한 멱등 스크립트

**구현 스텝**

1. `output/standards/*.json`을 모두 읽어 `{standard_id: [번호 목록]}` 딕셔너리 구성
2. 각 기준서 JSON을 순회하며 `paragraph_ids`에 `~`가 포함된 항목을 탐색
3. `ref.standard_number`로 타 기준서 번호 목록을 조회하여 범위 전개
4. 전개 결과로 `paragraph_ids` 교체 후 JSON 재저장

```bash
# 실행 예시 (구현 후)
.venv/bin/python3 scripts/expand_cross_standard_ranges.py
.venv/bin/python3 scripts/expand_cross_standard_ranges.py --dry-run   # 미리보기
```

**핵심 로직 (의사코드)**

```python
# 전체 기준서 문단 번호 맵 로드
all_numbers_map: dict[str, list[str]] = {}
for json_file in sorted(Path("output/standards").glob("*.json")):
    data = json.loads(json_file.read_text(encoding="utf-8"))
    all_numbers_map[data["standard_id"]] = [p["number"] for p in data["paragraphs"]]

# 폴백 항목 전개
for json_file in ...:
    data = ...
    modified = False
    for para in data["paragraphs"]:
        for ref in para["cross_references"]:
            if ref["type"] != "paragraph":
                continue
            target_std = ref.get("standard_number")
            if not target_std or target_std == data["standard_id"]:
                continue
            target_numbers = all_numbers_map.get(target_std, [])
            if not target_numbers:
                continue
            new_ids = expand_paragraph_ranges(ref["paragraph_ids"], target_numbers)
            if new_ids != ref["paragraph_ids"]:
                ref["paragraph_ids"] = new_ids
                modified = True
    if modified:
        json_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), ...)
```

---

### 방안 B — 크롤러 내부 수정 (복잡도 높음)

`crawl_all`에서 모든 기준서 크롤링 완료 후 2-패스로 처리:

- 1st pass: 모든 기준서 크롤링 → JSON 저장
- 2nd pass: JSON 다시 읽어 교차 기준서 범위 전개 → 재저장

방안 A와 실질적으로 동일하지만 크롤러와 결합도가 높아져 유지보수 어려움.

---

## 영향 범위

현재 확인된 폴백 케이스는 **3건**으로 범위가 매우 좁습니다:

```
1001/IG10  → 1007호 44A~44E  (1007호에 존재 확인됨)
1016/6     → 1041호 5A~5B    (1041호 크롤링 완료 시 처리 가능)
1016/78    → 1036호 73~      (열린 범위: end가 없으므로 별도 처리 필요)
```

### 타 기준서에서 같은 패턴이 발생하는 조건

다음 조건이 겹칠 때 발생합니다:

1. 문단 내 `.std-finder`(타 기준서 링크) + `.mundan-finder`(문단 범위) 조합
2. 참조 대상 기준서가 **알파벳 접미사가 붙은 문단**(예: `44A`, `44E`, `5A`)을 가짐
3. 현재 기준서에는 동일 번호가 존재하지 않아 폴백 발동

K-IFRS 개정 시 추가 문단은 `44A`, `44B` 식으로 삽입되는 경우가 많아
향후 기준서 개정에 따라 유사 케이스가 증가할 수 있습니다.

### 열린 범위 (`73~`) 처리 고려 사항

`1016/78`의 `73~`은 end 토큰이 없는 열린 범위입니다.
`expand_paragraph_ranges`의 현재 로직(`"~" not in item` 분기)에서도
`start~`에서 `end`가 빈 문자열이 되어 폴백됩니다.

포스트프로세싱 스크립트에서 이 경우는
"start 이후 해당 기준서의 마지막 문단까지" 혹은 "start만 반환" 중
정책을 결정한 후 별도 처리해야 합니다.

---

## 구현 우선순위

- [ ] `scripts/expand_cross_standard_ranges.py` 작성 (방안 A)
  - `--dry-run` 옵션으로 미리보기 지원
  - 변경된 기준서/문단/변경 전후 출력
- [ ] 열린 범위(`73~`) 처리 정책 결정
- [ ] 처리 후 `output/standards/1001.json`, `1016.json` 재검증

---

## 하드코딩 상수 정리

> 다른 기준서 크롤링 결과 검증 완료 후 진행 예정. 현재는 태스크 등록만.

소스 코드 전반에서 총 **21개** 하드코딩 항목 발견. 우선순위별 3단계로 분류.

---

### 우선순위 높음 (6건) — 사이트 변경 시 즉시 크롤러 오류 유발

- [ ] **[H1] User-Agent 중복 정의 제거**
  - `browser.py:61-65` (Playwright), `api_client.py:41-45` (httpx)에 동일 문자열 중복
  - 해결: `config.py`에 `USER_AGENT: Final[str]` 추가 후 양쪽에서 import
- [ ] **[H2] CSS 클래스명 `.sc-drlKqa.temp` 상수화**
  - `section_parser.py:373, 394, 441` — Styled-Components 해시 클래스; 사이트 재배포 시 변경 가능
  - 해결: `config.py` `SELECTORS` 딕셔너리에 `"styled_temp_wrapper"` 키로 추가
- [ ] **[H3] CSS 클래스명 `.sc-erNlkL` 상수화**
  - `section_parser.py:674` — 텍스트 전용 문단 구조 감지용
  - 해결: `config.py` `SELECTORS`에 `"styled_text_only"` 키로 추가
- [ ] **[H4] 기준서 출력/체크포인트 경로 상수화**
  - `main.py:77` → `Path("output/standards")`, `main.py:83` → `Path("checkpoints/progress.json")`
  - 해결: `config.py`에 `OUTPUT_STANDARDS_DIR`, `CHECKPOINT_FILE` 추가
- [ ] **[H5] QnA 출력/체크포인트 경로 상수화**
  - `qna_main.py:72` → `Path("output/qnas")`, `qna_main.py:78` → `Path("checkpoints/qna_progress.json")`
  - 해결: `config.py` 또는 `qna_config.py`에 `OUTPUT_QNAS_DIR`, `QNA_CHECKPOINT_FILE` 추가
- [ ] **[H6] 타임아웃/딜레이 설정 통합**
  - `config.py`(기준서: RETRY_DELAYS, PAGE_LOAD_TIMEOUT 등)와 `qna_config.py`(QNA_RETRY_DELAYS, QNA_TIMEOUT 등) 분산
  - 해결: `qna_config.py`에서 `config.py` 기본값을 import해 재활용, 또는 공통 섹션 문서화

---

### 우선순위 중간 (9건) — 유지보수성·일관성 저하

- [ ] **[M1] HTTP Referer 하드코딩 제거** — `api_client.py:43`: `"Referer": "https://db.kasb.or.kr/"` → `BASE_URL` 참조
- [ ] **[M2] Accept-Language 헤더 고정** — `api_client.py:42`: `"ko-KR,ko;q=0.9"` → `qna_config.py` 상수로 분리
- [ ] **[M3] 뷰포트 크기 상수화** — `browser.py:60`: `{"width": 1280, "height": 900}` → `config.py`에 `BROWSER_VIEWPORT` 추가
- [ ] **[M4] 기준서 제목 정규식 JS 인라인 제거** — `orchestrator.py:113` JS 코드 내 `/^([0-9]{4,5}|CF)\s*-\s*(.+)$/` → 별도 상수 또는 주석 강화
- [ ] **[M5] "공유하기" 텍스트 필터 상수화** — `orchestrator.py:131` JS 코드 내 `'공유하기'` 리터럴 → `config.py` 또는 상수 블록으로 분리
- [ ] **[M6] QnA 페이징 크기 문서화** — `qna_config.py:26`: `QNA_LIST_ROWS = 100` (이미 상수) → API 최대값 근거 주석 추가
- [ ] **[M7] 섹션 분류 키워드 문서화** — `qna_config.py:60-63`: `QUESTION_HEADINGS`, `ANSWER_HEADINGS` (이미 상수) → 추출 실패 시 경고 로그 강화
- [ ] **[M8] 타입 코드 매핑 문서화** — `qna_config.py:38-46`: `QNA_TYPE_MAP` (이미 상수) → 각 코드의 공식 출처 주석 추가
- [ ] **[M9] orchestrator 내 로컬 매핑 제거** — `crawler/orchestrator.py` 내 config.py와 중복 정의된 매핑 → import로 대체

---

### 우선순위 낮음 (6건) — 관용적 사용, 문서화로 충분

- [ ] **[L1] MD5 해시 길이 `8`** — `toc_parser.py:41`: 섹션 ID 생성 시 첫 8자 사용 → 의미 주석 추가
- [ ] **[L2] 기준서 유형 판별 `"1"`, `"2"`** — `orchestrator.py:70, 72`: 번호 첫 자리로 기준서/해석서 구분 → 주석 보강
- [ ] **[L3] 마크다운 최소 헤더 레벨 `2`** — `section_parser.py:1104`: `##` 이상 레벨 고정 → 상수화 또는 주석
- [ ] **[L4] API 응답 필드 이름** — `qna_crawler/orchestrator.py:153-154`: dict 키 직접 참조 → 변경 시 영향 범위 주석
- [ ] **[L5] 테이블 파싱 임계값** — `section_parser.py` 내 열 수 기반 판별 숫자 → 주석 보강
- [ ] **[L6] 브라우저 재시도 횟수** — `browser.py` 내 navigate_with_retry 기본값 → `config.py` 상수화 검토

---

### 수정 방안

**A안 (최소 범위) — 높음 6건만 수정**

사이트 변경 시 즉시 깨지는 항목만 처리. 예상 작업: `config.py` 수정 5~10줄, 각 파일 import 교체.

```bash
# 수정 대상 파일 (A안)
src/config.py               # USER_AGENT, OUTPUT_*_DIR, CHECKPOINT_FILE, SELECTORS 추가
src/browser.py              # USER_AGENT import
src/qna_crawler/api_client.py  # USER_AGENT import
src/crawler/section_parser.py  # SELECTORS["styled_temp_wrapper"], SELECTORS["styled_text_only"] 참조
src/main.py                 # 경로 상수 import
src/qna_main.py             # 경로 상수 import
```

**B안 (권장) — 높음 + 중간 (15건) 수정**

A안에 M1~M3(Referer, Accept-Language, 뷰포트) 추가. 유지보수성 크게 향상.

```bash
# 추가 수정 파일 (B안)
src/qna_config.py           # BASE_URL Referer 참조, ACCEPT_LANGUAGE 상수
src/browser.py              # BROWSER_VIEWPORT 상수
```

---

### 수정 대상 파일 목록

| 파일 | 관련 항목 | 우선순위 |
|------|-----------|---------|
| `src/config.py` | USER_AGENT, 경로 상수, SELECTORS 추가 | 높음 |
| `src/browser.py` | UA import, 뷰포트 상수화 | 높음+중간 |
| `src/qna_crawler/api_client.py` | UA import, Referer 동적화 | 높음+중간 |
| `src/crawler/section_parser.py` | CSS 클래스 SELECTORS 참조 | 높음 |
| `src/main.py` | 경로 상수 import | 높음 |
| `src/qna_main.py` | 경로 상수 import | 높음 |
| `src/qna_config.py` | BASE_URL 참조, 헤더 상수 | 중간 |
| `src/crawler/orchestrator.py` | 로컬 매핑 제거, JS 정규식 주석 | 중간+낮음 |
| `src/crawler/toc_parser.py` | 해시 길이 주석 | 낮음 |
