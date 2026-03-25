# K-IFRS 크롤러 데이터 스키마

이 문서는 크롤러가 생성하는 세 종류의 JSON 출력 스키마를 정의합니다.

| 출력 경로 | 설명 | 생성 모듈 |
|-----------|------|-----------|
| `output/standards/{id}.json` | 기준서 전체 데이터 | `src/main.py` |
| `output/qnas/{docNumber}.json` | QnA 상세 데이터 | `src/qna_main.py` |
| `output/qnas/_index.json` | QnA 요약 인덱스 | `src/qna_main.py` |
| `output/metadata/{id}.json` | RAG용 기준서 메타데이터 | `src/metadata_generator.py` |

---

# Part 1: 기준서 JSON 스키마

## 개요

`output/standards/` 디렉토리에 기준서별 JSON 파일이 저장됩니다.

- **파일 위치**: `output/standards/{standard_id}.json` (예: `output/standards/1001.json`)
- **인코딩**: UTF-8
- **구조**: 기준서 1개 = JSON 파일 1개
- **모델 정의**: `src/models.py` (Pydantic v2)

---

## 최상위 구조 (`Standard`)

```json
{
  "standard_id": "1001",
  "standard_type": "기업회계기준서",
  "title": "재무제표 표시",
  "url": "https://db.kasb.or.kr/s/1001",
  "crawled_at": "2026-03-05T06:46:02.713654+00:00",
  "toc": [...],
  "paragraphs": [...],
  "cross_references_index": {...},
  "qna_index": {...}
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `standard_id` | `str` | 기준서 번호 (예: `"1001"`, `"2115"`) |
| `standard_type` | `str` | 유형 (예: `"기업회계기준서"`, `"기업회계기준해석서"`) |
| `title` | `str` | 한글 제목 (예: `"재무제표 표시"`) |
| `url` | `str` | 기준서 원본 URL |
| `crawled_at` | `str` | 크롤링 시각 (ISO 8601, UTC+0) |
| `toc` | `TocItem[]` | 목차 트리 (최상위 노드 목록) |
| `paragraphs` | `Paragraph[]` | 모든 문단 리스트 (평탄화된 순서) |
| `cross_references_index` | `dict[str, str[]]` | 교차참조 역인덱스 |
| `qna_index` | `dict[str, str[]]` | 질의회신 역인덱스 |

---

## TOC 구조 (`TocItem`)

목차는 재귀적 트리 구조입니다. `children` 필드로 하위 노드를 포함합니다.

```json
{
  "level": 2,
  "title": "적용범위",
  "section_id": "mA9o3F",
  "href": "/s/1001/mA9o3F",
  "paragraph_range": "2 ~ 6",
  "children": []
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `level` | `int \| null` | 계층 깊이 (0~5). `null`은 저작권 등 특수 항목 |
| `title` | `str` | 섹션 제목 (문단 범위 제외, 불릿 기호 자동 제거됨) |
| `section_id` | `str` | 섹션 고유 식별자 (URL 경로 마지막 부분) |
| `href` | `str` | 원본 href (상대 경로) |
| `paragraph_range` | `str \| null` | 해당 섹션의 문단 범위 (예: `"2 ~ 6"`, `"BC1"`) |
| `children` | `TocItem[]` | 하위 목차 항목 (재귀) |

**level 값 의미:**
- `null`: 저작권, 서문 등 특수 항목
- `0`: 최상위 그룹 (예: 본문, 결론도출근거, 적용지침)
- `2~5`: 일반 섹션 (숫자가 클수록 깊은 계층)

**트리 구조 예시:**
```
level=0  본문
  level=2  목적 (문단 1)
  level=2  적용범위 (문단 2~6)
  level=2  재무제표 (문단 9~46)
    level=3  재무제표의 목적 (문단 9)
    level=3  일반사항 (문단 15~46)
      level=4  공정한 표시 (문단 15~24)
      level=4  계속기업 (문단 25~26)
```

---

## 문단 구조 (`Paragraph`)

문단은 최소 수집 단위이며, `paragraphs` 배열에 순서대로 저장됩니다.

```json
{
  "number": "4",
  "section_id": "mA9o3F",
  "section_title": "적용범위",
  "toc_path": "본문|적용범위",
  "text": "이 기준서는 기업회계기준서 제1034호 '중간재무보고'에 따라...",
  "html": "이 기준서는 <div class=\"sc-eEieub ...\">...</div>...",
  "cross_references": [...],
  "qna_references": [...],
  "footnote_references": [...]
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `number` | `str` | 문단 번호 (예: `"2"`, `"BC1"`, `"한138.6"`, `"IE15"`, `"의결"`, `"주1"`) — KASB 원시 접두사는 크롤링 시 자동 변환됨: `웩`(U+C6E9)→`IE`(적용사례), `왝`(U+C65D)→`주`(각주) |
| `section_id` | `str` | 소속 섹션 ID (TOC의 `section_id`와 매핑) |
| `section_title` | `str` | 소속 섹션 제목 |
| `toc_path` | `str` | 루트부터 현재 섹션까지의 경로 (예: `"본문|재무제표|일반사항"`) |
| `text` | `str` | 순수 텍스트 (아래 상세 설명 참고) |
| `html` | `str` | 원본 HTML (아래 상세 설명 참고) |
| `cross_references` | `CrossReference[]` | 이 문단에 포함된 교차참조 목록 |
| `qna_references` | `QnAReference[]` | 이 문단에 연결된 질의회신 목록 |
| `footnote_references` | `FootnoteReference[]` | 이 문단에 포함된 각주 기호 목록 |

### `text` 필드 상세

`text`는 다음 요소들을 통합한 순수 텍스트입니다:

- **본문**: `.tooltip-content` 텍스트를 제거한 순수 텍스트
- **Markdown 테이블**: HTML 표(`<table>`)를 Markdown 형식으로 변환 (예: `| 수익 | X |`)
- **하위 항목**: `(1)`, `(2)` 형태의 번호가 붙은 중첩 항목 포함
- **각주 내용**: `[각주 (한2)] 본문...` 형식으로 본문 끝에 통합

### `html` 필드 상세

`html`은 원본 HTML 마크업을 그대로 포함합니다:

- 사이트 전용 CSS 클래스 포함: `.std-finder` (기준서 참조), `.mundan-finder` (문단 참조)
- tooltip 래퍼 div: `<div class="sc-eEieub">` 등
- 테이블은 원본 HTML 그대로 포함 (외부 HTM 파일 로드 형태일 수 있음)
- **텍스트 분석 목적에는 `text` 필드 사용을 권장**

---

## 교차참조 (`CrossReference`)

문단 내에서 다른 기준서 또는 다른 문단을 참조하는 링크입니다.

### 기준서 참조 (`type: "standard"`)

```json
{
  "type": "standard",
  "display_text": "기업회계기준서 제1034호",
  "standard_number": "1034",
  "standard_title": "중간재무보고",
  "range": null
}
```

### 문단 참조 (`type: "paragraph"`)

```json
{
  "type": "paragraph",
  "display_text": "문단 15~35",
  "standard_number": null,
  "standard_title": null,
  "range": "15~35"
}
```

| 필드 | 타입 | 조건 | 설명 |
|------|------|------|------|
| `type` | `str` | 항상 | `"standard"` 또는 `"paragraph"` |
| `display_text` | `str` | 항상 | 화면에 표시되는 텍스트 |
| `standard_number` | `str \| null` | `type="standard"`일 때 | 참조 기준서 번호 (예: `"1034"`) |
| `standard_title` | `str \| null` | `type="standard"`일 때 | 기준서 한글 제목 (예: `"중간재무보고"`) |
| `range` | `str \| null` | `type="paragraph"`일 때 | 문단 범위 (예: `"15~35"`, `"25"`) |

---

## 질의회신 참조 (`QnAReference`)

문단에 연결된 KASB 질의회신(Q&A) 항목입니다.

```json
{
  "qna_id": "2020-I-KQA009",
  "title": "(별도재무제표) 지배종속기업 간 합병 시 비교 표시되는 전기 재무제표",
  "url": "/qnas/2020-I-KQA009",
  "date": "2020-06-30"
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `qna_id` | `str` | 질의회신 고유 ID (예: `"2020-I-KQA009"`, `"SSI-35569"`) |
| `title` | `str` | 질의회신 제목 |
| `url` | `str` | 질의회신 페이지 URL (상대 경로) |
| `date` | `str` | 발행일 (YYYY-MM-DD 형식) |

---

## 각주 참조 (`FootnoteReference`)

문단 본문 또는 각주에서 등장하는 각주 기호입니다.

```json
{
  "id": "한2",
  "display_text": "(한2)"
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `id` | `str` | 각주 식별자 (예: `"한2"`, `"주1"`) |
| `display_text` | `str` | 본문에 표시되는 텍스트 (예: `"(한2)"`, `"(주1)"`) |

**각주 종류:**
- `한N`: 한국채택국제회계기준 번역 관련 각주
- `주N`: 일반 각주

---

## 인덱스 구조

### `cross_references_index`

**기준서 번호 → 해당 기준서를 참조하는 문단 번호 목록**

```json
{
  "1034": ["4", "한10.1"],
  "1110": ["4", "139H"],
  "1027": ["4", "139A"],
  "1008": ["7", "40C", "44", "89", "110", "139L"]
}
```

- **키**: 참조된 기준서 번호 (문자열)
- **값**: 해당 기준서를 참조하는 현재 기준서의 문단 번호 목록
- **활용**: "1034호를 참조하는 문단이 어디인가?" 조회 시 O(1)

### `qna_index`

**질의회신 ID → 해당 QnA가 연결된 문단 번호 목록**

```json
{
  "2020-I-KQA009": ["4", "38"],
  "2017-I-KQA001": ["7", "88", "89"],
  "SSI-35569": ["7", "96"]
}
```

- **키**: 질의회신 ID
- **값**: 해당 QnA가 연결된 문단 번호 목록
- **활용**: "이 QnA가 어느 문단과 관련 있는가?" 조회 시 O(1)

---

## 활용 패턴 예시

### 특정 섹션의 문단 조회

```python
import json

with open("output/standards/1001.json") as f:
    data = json.load(f)

# toc_path로 특정 섹션 문단 필터링
paragraphs = [p for p in data["paragraphs"] if "적용범위" in p["toc_path"]]
```

### 교차참조 역인덱스 활용

```python
# 기준서 1034를 참조하는 문단 번호 목록
refs_to_1034 = data["cross_references_index"].get("1034", [])

# 해당 문단의 전체 데이터 조회
para_map = {p["number"]: p for p in data["paragraphs"]}
related_paragraphs = [para_map[n] for n in refs_to_1034 if n in para_map]
```

### RAG 임베딩용 텍스트 구성

```python
for p in data["paragraphs"]:
    # toc_path를 맥락 prefix로 활용
    context = p["toc_path"].replace("|", " > ")
    chunk = f"[{context}] 문단 {p['number']}\n{p['text']}"
```

---

## 통계 (기준서 1001 기준)

| 항목 | 값 |
|------|-----|
| 문단 수 (`paragraphs`) | 492개 |
| TOC 항목 수 (재귀 합산) | 168개 (최상위 5개) |
| 교차참조 총계 | 536건 (기준서 참조: 227, 문단 참조: 309) |
| 교차참조 인덱스 키 수 | 41개 기준서 |
| 질의회신 인덱스 키 수 | 75개 QnA |
| QnA 참조 총계 | 258건 |
| 각주 참조 총계 | 54건 |

---

# Part 2: QnA JSON 스키마

## 개요

`output/qnas/` 디렉토리에 QnA별 JSON 파일과 전체 인덱스 파일이 저장됩니다.

- **파일 위치**: `output/qnas/{docNumber}.json` (예: `output/qnas/2020-I-KQA009.json`)
- **인코딩**: UTF-8
- **모델 정의**: `src/qna_models.py` (Pydantic v2)

---

## 최상위 구조 (`QnADetail`)

```json
{
  "qna_id": "2020-I-KQA009",
  "db_id": 12345,
  "type_code": 11,
  "type_name": "K-IFRS 회계기준원",
  "title": "(별도재무제표) 지배종속기업 간 합병 시 비교 표시되는 전기 재무제표",
  "reference": "기업회계기준서 제1001호 문단 38",
  "date": "2020-06-30",
  "tags": ["별도재무제표", "합병"],
  "question": "...",
  "answer": "...",
  "sections": [...],
  "related_standards": [...],
  "similar_qna_ids": ["2019-I-KQA005"],
  "footnotes": [],
  "crawled_at": "2026-03-06T10:00:00.000000+00:00"
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `qna_id` | `str` | docNumber (예: `"2020-I-KQA009"`, `"SSI-35569"`) |
| `db_id` | `int` | 내부 DB ID |
| `type_code` | `int` | 타입 코드 (11/12/13/14/15/24/25) |
| `type_name` | `str` | 타입 이름 (예: `"K-IFRS 회계기준원"`) |
| `title` | `str` | 질의회신 제목 |
| `reference` | `str \| null` | 레퍼런스 텍스트 (없으면 null) |
| `date` | `str` | 발행일 (YYYY-MM-DD) |
| `tags` | `str[]` | 색인어 목록 |
| `question` | `str` | 질의 섹션 텍스트 (VectorDB 임베딩용) |
| `answer` | `str` | 회신 섹션 텍스트 (VectorDB 임베딩용) |
| `sections` | `ContentSection[]` | 모든 섹션 목록 |
| `related_standards` | `RelatedStandard[]` | 관련 기준서-문단 매핑 |
| `similar_qna_ids` | `str[]` | 유사 QnA ID 목록 |
| `footnotes` | `dict[]` | 각주 (원본 구조 보존) |
| `crawled_at` | `str` | 수집 시각 (ISO 8601, UTC+0) |

---

## 섹션 구조 (`ContentSection`)

contentHtml을 h2/h3 태그 기준으로 분할한 개별 섹션입니다.

```json
{
  "heading": "배경 및 질의",
  "text": "A사는 B사를 흡수합병하였습니다...",
  "html": "<p class='number-content'>...</p>"
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `heading` | `str` | 섹션 제목 (예: `"배경 및 질의"`, `"회신"`, `"판단근거"`) |
| `text` | `str` | 정리된 순수 텍스트 (번호 항목 제외) |
| `html` | `str` | 원본 HTML |

**주요 heading 종류:**
- `"배경 및 질의"`, `"질의"`, `"사실관계"`, `"배경"` → `question` 필드에 합산
- `"회신"` → `answer` 필드에 합산
- `"판단근거"`, `"참고자료"` → `sections`에만 포함

---

## 관련 기준서 (`RelatedStandard`)

```json
{
  "standard_number": "1001",
  "paragraphs": ["4", "38"]
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `standard_number` | `str` | 기준서 번호 (예: `"1001"`) |
| `paragraphs` | `str[]` | 관련 문단 번호 목록 |

---

## QnA 인덱스 (`_index.json`)

전체 QnA 요약 인덱스 파일로, 상세 내용 없이 빠른 검색을 지원합니다.

```json
{
  "total": 2243,
  "items": [
    {
      "qna_id": "2020-I-KQA009",
      "db_id": 12345,
      "type_code": 11,
      "type_name": "K-IFRS 회계기준원",
      "title": "...",
      "date": "2020-06-30",
      "tags": ["별도재무제표", "합병"],
      "related_standards": ["1001", "1027"],
      "similar_qna_ids": ["2019-I-KQA005"]
    }
  ]
}
```

---

# Part 3: 메타데이터 JSON 스키마 (RAG용)

## 개요

`output/metadata/` 디렉토리에 기준서별 경량 메타데이터 파일이 저장됩니다.

- **파일 위치**: `output/metadata/{standard_id}.json`
- **생성 도구**: `src/metadata_generator.py` (기준서 크롤링 완료 후 실행)

---

## 구조

```json
{
  "standard_id": "1001",
  "standard_type": "기업회계기준서",
  "title": "재무제표 표시",
  "purpose": "이 기준서의 목적은 일반목적 재무제표의...",
  "scope": "이 기준서는 한국채택국제회계기준에 따라...",
  "toc_outline": ["결론도출근거", "공정한 표시와 한국채택국제회계기준의 준수", ...],
  "paragraph_count": 1105,
  "references_to": ["1002", "1007", "1008", ...],
  "referenced_by": ["1002", "1007", "1016", ...]
}
```

| 필드 | 타입 | 설명 |
|------|------|------|
| `standard_id` | `str` | 기준서 번호 |
| `standard_type` | `str` | 유형 (예: `"기업회계기준서"`) |
| `title` | `str` | 한글 제목 |
| `purpose` | `str` | 목적 섹션 텍스트 (toc_path 두 번째 세그먼트 = "목적") |
| `scope` | `str` | 적용범위 섹션 텍스트 (toc_path 두 번째 세그먼트 = "적용범위") |
| `toc_outline` | `str[]` | TOC 고유 키워드 목록 (level=null 제외, 중복 없는 정렬) |
| `paragraph_count` | `int` | 총 문단 수 |
| `references_to` | `str[]` | 이 기준서가 참조하는 기준서 번호 목록 |
| `referenced_by` | `str[]` | 이 기준서를 참조하는 다른 기준서 번호 목록 (역참조) |

### `referenced_by` 계산 방식

전체 기준서 `references_to`를 순회하여 역참조를 집계합니다.
`A.references_to`에 `B`가 있으면 → `B.referenced_by`에 `A` 추가.
