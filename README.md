# K-IFRS 회계기준 크롤러

KASB(한국회계기준원) 데이터베이스에서 K-IFRS 기준서/해석서/개념체계 및 질의회신(QnA)을 자동 수집하여 JSON으로 저장하는 크롤러입니다.

## 수집 대상

| 분류 | 건수 | 예시 |
|------|------|------|
| K-IFRS 기업회계기준서 | 41개 | 1001, 1007, ..., 1116 |
| K-IFRS 기업회계기준해석서 | 19개 | 2101, 4101, ..., 4110 |
| K-IFRS 개념체계 | 1개 | CF |
| QnA (질의응답) | ~2,243건 | K-IFRS + 일반기업회계기준 |

---

## 아키텍처

### 디렉토리 구조

```
src/
├── config.py               # 기준서 크롤러 설정 상수 (URL, 셀렉터, 번호 목록)
├── qna_config.py           # QnA 크롤러 설정 상수 (API URL, 타입 코드)
├── models.py               # 기준서 Pydantic 모델 (Standard, Paragraph, ...)
├── qna_models.py           # QnA Pydantic 모델 (QnADetail, ContentSection, ...)
├── utils.py                # 공통 유틸 (로깅, 텍스트 정리, 범위 전개)
├── browser.py              # Playwright BrowserManager, navigate_with_retry
├── checkpoint.py           # 기준서 크롤링 체크포인트 (checkpoints/progress.json)
├── main.py                 # 기준서 크롤러 CLI 진입점
├── qna_main.py             # QnA 크롤러 CLI 진입점
├── metadata_generator.py   # RAG용 메타데이터 생성기 (독립 실행)
├── crawler/
│   ├── toc_parser.py       # aside a[href] + level 속성 → 스택 기반 트리 구축
│   ├── section_parser.py   # li[data-paranum] 파싱 + 교차참조/QnA/각주 추출
│   └── orchestrator.py     # 기준서 크롤링 전체 워크플로우
└── qna_crawler/
    ├── api_client.py       # httpx 기반 QnA REST API 클라이언트
    ├── parser.py           # contentHtml → ContentSection 변환
    └── orchestrator.py     # QnA 수집 전체 워크플로우
```

### 모듈 의존성

```
main.py
└── crawler/orchestrator
    ├── crawler/toc_parser
    ├── crawler/section_parser
    ├── browser
    └── checkpoint

qna_main.py
└── qna_crawler/orchestrator
    ├── qna_crawler/api_client
    └── qna_crawler/parser

metadata_generator.py  (독립 실행, crawler와 무관)

공통: config, qna_config, models, qna_models, utils
```

---

## 크롤러 동작 상세

### 기준서 크롤러 (`src/main.py`)

Playwright Chromium 브라우저를 자동화하여 기준서 페이지를 파싱합니다.

**워크플로우:**

1. **페이지 이동** — `https://db.kasb.or.kr/s/{id}` (리다이렉트 처리)
2. **제목 추출** — `span` 내 "XXXX - 제목" 패턴 정규식 파싱
3. **TOC 파싱** — `aside a[href]` + `level` 속성 기반 스택 트리 구축
   - 완전 평탄한 `<ol>` 구조를 level 속성만으로 계층화
   - null level(저작권 등), 소수점 level(1.5), 실무적용지침 등 특수 케이스 처리
4. **섹션별 문단 파싱** — `li[data-paranum]` 선택자로 모든 문단 추출
   - `.tooltip-content` cloneNode 후 제거로 순수 텍스트 추출
   - HTML 테이블 → Markdown 테이블 변환
   - `.std-finder`/`.mundan-finder` 교차참조 DOM 순서 수집
   - "질의회신" 버튼 클릭으로 접이식 패널 펼쳐 QnA 링크 추출
5. **후처리**
   - 문단 번호 기반 중복 제거 (toc_path 깊이 우선)
   - 자기 기준서 내 문단 참조에 `standard_number` 채우기
   - 교차참조 범위(예: "15~35") → 문서 순서 기반 개별 문단 ID 전개
6. **인덱스 생성** — 기준서별 `cross_references_index`, `qna_index`
7. **JSON 저장** + **체크포인트 기록**

**특징:**
- `--resume` 옵션으로 중단 후 체크포인트 기반 재개
- 실패한 기준서 1회 자동 재시도 (지수 백오프)
- `--no-headless` 옵션으로 브라우저 창 표시 (디버깅)

### QnA 크롤러 (`src/qna_main.py`)

브라우저 없이 httpx AsyncClient로 REST API를 호출합니다.

**워크플로우:**

1. **목록 수집** — `GET /api/qnas/v2?types=all&page={n}&rows=100` 페이지네이션
2. **재개 모드** — 체크포인트 + 기존 파일 기반으로 완료된 항목 제외
3. **상세 수집** — `GET /api/qnas/v2/{docNumber}` 건별 호출
4. **파싱** — `contentHtml` → h2/h3 기준 섹션 분리 → question/answer 분류
5. **저장** — `{docNumber}.json` + `_index.json`

**특징:**
- `verify=False` 필수 (KASB 서버 자체 서명 인증서)
- 랜덤 딜레이(0.3~0.5초)로 서버 부하 방지
- `--qna-id` 옵션으로 단건 테스트 지원

### 메타데이터 생성기 (`src/metadata_generator.py`)

기준서 크롤링 완료 후 RAG(Retrieval-Augmented Generation) Agent용 경량 메타데이터를 추출합니다.

- TOC 키워드 추출 (중복 없는 정렬된 목록)
- 목적/적용범위 섹션 텍스트 추출
- 교차참조 역인덱스 집계 (`references_to`, `referenced_by`)
- 크롤러와 완전히 독립적으로 실행 가능

---

## 설치 및 실행

### 환경 설정

```bash
# 1. 가상환경 생성
python3 -m venv .venv

# 2. 의존성 설치
.venv/bin/pip install -r requirements.txt

# 3. Playwright Chromium 브라우저 설치 (기준서 크롤러용)
.venv/bin/playwright install chromium
```

> **중요**: 항상 `.venv/bin/python3`를 사용합니다. 시스템 전역 `python3` 사용 금지.

### 기준서 크롤러 실행

```bash
# 특정 기준서 단건
.venv/bin/python3 -m src.main --standard 1001

# 전체 크롤링 (61개)
.venv/bin/python3 -m src.main

# 중단 후 재개
.venv/bin/python3 -m src.main --resume

# 브라우저 창 표시 (디버깅)
.venv/bin/python3 -m src.main --standard 1001 --no-headless

# 상세 로그
.venv/bin/python3 -m src.main --standard 1001 --verbose

# 체크포인트 초기화 후 처음부터
.venv/bin/python3 -m src.main --reset-checkpoint
```

### QnA 크롤러 실행

```bash
# 전체 수집 (~2,243건)
.venv/bin/python3 -m src.qna_main

# 중단 후 재개
.venv/bin/python3 -m src.qna_main --resume

# 단건 테스트
.venv/bin/python3 -m src.qna_main --qna-id 2020-I-KQA009

# 상세 로그
.venv/bin/python3 -m src.qna_main --verbose
```

### 메타데이터 생성기 실행

```bash
# 전체 기준서 메타데이터 생성 (기준서 크롤링 완료 후)
.venv/bin/python3 -m src.metadata_generator

# 단건 생성
.venv/bin/python3 -m src.metadata_generator --standard 1001

# 미리보기 (파일 저장 없이 stdout 출력)
.venv/bin/python3 -m src.metadata_generator --standard 1001 --preview
```

---

## 출력 구조

```
output/
├── standards/
│   ├── 1001.json       # 기준서 JSON (상세 스키마: STANDARD_DATA_SCHEMA.md)
│   ├── 1002.json
│   └── ...
├── qnas/
│   ├── _index.json     # 전체 QnA 요약 인덱스 (빠른 검색용)
│   ├── 2020-I-KQA009.json
│   └── ...
└── metadata/
    ├── 1001.json       # RAG용 경량 메타데이터
    └── ...

checkpoints/
├── progress.json       # 기준서 크롤러 체크포인트
└── qna_progress.json   # QnA 크롤러 체크포인트
```

상세 스키마는 [STANDARD_DATA_SCHEMA.md](STANDARD_DATA_SCHEMA.md)를 참조하세요.

---

## 주요 기술적 특이사항

### TOC 평탄 구조 → 스택 트리 변환

KASB 사이트의 TOC는 중첩 없는 완전 평탄한 `<ol>` 구조이며, 각 `a[href]` 요소의 `level` 속성(0~5, 소수점 포함)만이 유일한 계층 정보입니다. 스택 기반 알고리즘으로 계층 트리를 동적으로 구축합니다.

특수 케이스:
- `null` level: 저작권 등 특수 항목 → 항상 루트, 스택에 추가 안 함
- 소수점 level(예: `1.5`): `int()` 아닌 `float()` 변환 필수
- 실무적용지침: level=0이 level=2+ 자식을 가진 후 level=1이 오면 루트(형제)로 처리

### tooltip 텍스트 제거

`.std-finder` 교차참조 요소는 `.tooltip-content`를 자식으로 포함합니다. `cloneNode(true)` 후 `.tooltip-content`를 제거하여 tooltip 텍스트가 본문에 포함되지 않도록 처리합니다.

### HTML 테이블 → Markdown 변환

JavaScript 내 `tableToMarkdown()` 함수로 `<table>`을 Markdown 테이블 형식(`| 헤더 | ... |`)으로 변환합니다. 첫 행을 헤더로 처리하고 `|---|---|` 구분선을 자동 삽입합니다.

### 교차참조 범위 전개

`"문단 15~35"` 같은 범위 표기를 현재 기준서의 실제 문단 순서(BC1, 40A 등 비숫자 포함)를 기준으로 개별 문단 번호 목록으로 전개합니다. 타 기준서 참조인 경우 원본 표기를 유지합니다.

### QnA 접이식 패널

문단에 연결된 QnA 링크(`a[href^='/qnas/']`)는 "질의회신" 버튼을 클릭해야 DOM에 나타납니다. `section_parser._expand_qna_buttons()`로 모든 버튼을 클릭한 후 파싱을 진행합니다.
