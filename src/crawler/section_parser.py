"""
개별 섹션(문단) 파서 - 교차참조, 질의회신, 각주 포함

KASB 기준서 페이지에서 li[data-paranum] 선택자로 문단을 추출하고,
각 문단의 텍스트/HTML/교차참조/질의회신/각주를 Paragraph 모델로 변환합니다.

주요 함수:
    - parse_section: 섹션 URL로 이동하여 문단 파싱
    - parse_section_from_current_page: 현재 페이지에서 직접 문단 파싱

내부 파이프라인:
    1. _expand_qna_buttons: "질의회신" 버튼 클릭으로 접이식 패널 펼치기
    2. _extract_paragraphs_js: JS로 li[data-paranum] 일괄 추출
       - .tooltip-content cloneNode 후 제거로 순수 텍스트 추출
       - HTML 테이블 → Markdown 변환
       - std-finder/mundan-finder 교차참조 DOM 순서 수집
    3. _build_cross_references: 원시 데이터 → CrossReference 모델
    4. _build_qna_references: 원시 데이터 → QnAReference 모델
    5. _build_footnote_references: 원시 데이터 → FootnoteReference 모델
"""
import asyncio
from typing import Optional

from playwright.async_api import Page

from src.config import BASE_URL, NETWORK_IDLE_TIMEOUT
from src.models import CrossReference, FootnoteReference, Paragraph, QnAReference
from src.utils import logger, clean_text, extract_standard_number, extract_paragraph_range, resolve_paragraph_ids, normalize_unicode_parens
from src.browser import navigate_with_retry


def _process_raw_text(raw_text: str) -> str:
    """JS에서 추출한 원시 텍스트를 정리합니다.

    \u2029 (Paragraph Separator): textParts 블록 경계 → \n으로 join
    \u2028 (Line Separator): smartTableToContent 단락 경계 → 각 단락 독립 clean_text 후 \n\n으로 join

    smartTableToContent 결과(\u2028 포함 세그먼트)와 일반 pip 세그먼트 사이에는
    \n\n(이중 개행)을 사용하여 블록 구분을 명확히 합니다.
    """
    if "\u2029" in raw_text:
        segments = raw_text.split("\u2029")
    else:
        segments = [raw_text]

    # 각 세그먼트를 처리하면서 smartTableToContent 여부를 함께 기록
    cleaned_parts = []  # (text, is_smart_table) 튜플 리스트
    for seg in segments:
        if "\u2028" in seg:
            # smartTableToContent 단락 경계: 각 단락을 독립적으로 clean_text 처리 후 \n\n으로 재결합
            paras = [clean_text(p) for p in seg.split("\u2028")]
            text = "\n\n".join(p for p in paras if p)
            is_smart_table = True
        else:
            text = clean_text(seg)
            is_smart_table = False
        if text:
            cleaned_parts.append((text, is_smart_table))

    # 조립: smartTableToContent 세그먼트 앞뒤에는 \n\n, 일반 pip 간에는 \n
    if not cleaned_parts:
        return ""

    result = cleaned_parts[0][0]
    for i in range(1, len(cleaned_parts)):
        prev_is_table = cleaned_parts[i - 1][1]
        curr_is_table = cleaned_parts[i][1]
        sep = "\n\n" if (prev_is_table or curr_is_table) else "\n"
        result += sep + cleaned_parts[i][0]

    return result


def _normalize_paranum(number: str) -> str:
    """KASB 내부 문단 접두사를 IFRS 표준 접두사로 변환합니다.

    KASB 웹사이트는 두 가지 불투명한 한글 접두사를 사용합니다:

    웩(U+C6E9) — Illustrative Examples(적용사례):
    - '웩의결' → '의결': 한국 고유 "회계기준위원회 의결" 섹션
    - '웩' + 나머지 → 'IE' + 나머지: IFRS 적용사례(IE) 접두사
    '웩의결'을 먼저 검사해야 이후 '웩' 패턴이 '의결' 부분을 덮어쓰지 않음.

    왝(U+C65D) — 각주/Comment 문단:
    - '왝' + 나머지 → '주' + 나머지: 본문에 삽입된 각주 내용 문단
    - 웹 UI 표시 레이블은 '(주N)' 형태지만 data-paranum은 '왝N'으로 저장됨
    """
    # 웩(U+C6E9): 적용사례(IE) 또는 의결 섹션
    if number.startswith("웩의결"):
        return number.replace("웩의결", "의결", 1)
    if number.startswith("웩"):
        return number.replace("웩", "IE", 1)
    # 왝(U+C65D): 각주/Comment 문단 → '주' 접두사
    if number.startswith("왝"):
        return number.replace("왝", "주", 1)
    return number


async def _expand_qna_buttons(page: Page) -> None:
    """
    페이지 내 모든 질의회신 접이식 버튼을 클릭하여 확장합니다.

    버튼 클릭 전에는 a[href^='/qnas/'] 링크가 DOM에 존재하지 않으므로,
    이 함수를 반드시 _extract_paragraphs_js 호출 전에 실행해야 합니다.
    클릭 후 애니메이션 완료까지 0.3~0.5초 대기합니다.

    Args:
        page: 현재 열려 있는 Playwright 페이지 객체.
    """
    try:
        # "질의회신" 텍스트를 포함한 버튼 찾기
        buttons = await page.query_selector_all("button")
        qna_buttons = []
        for btn in buttons:
            text = await btn.text_content() or ""
            if "질의회신" in text:
                qna_buttons.append(btn)

        if not qna_buttons:
            return

        logger.debug("질의회신 버튼 %d개 발견, 클릭 중...", len(qna_buttons))

        for btn in qna_buttons:
            try:
                # 버튼이 화면에 보이는지 확인
                is_visible = await btn.is_visible()
                if is_visible:
                    await btn.click()
                    await asyncio.sleep(0.3)  # 애니메이션 대기
            except Exception as e:
                logger.debug("버튼 클릭 실패 (무시): %s", e)

        # 확장 완료 대기
        await asyncio.sleep(0.5)

    except Exception as e:
        logger.warning("질의회신 버튼 확장 실패: %s", e)


async def _extract_paragraphs_js(page: Page) -> list[dict]:
    """
    JavaScript를 사용하여 페이지의 모든 문단 데이터를 한 번에 추출합니다.

    반환 구조:
    [
        {
            "number": "2",
            "html": "<b>...</b>",
            "text": "...",
            "std_refs": [{"display_text": "...", "tooltip": "..."}],
            "para_refs": ["문단 15~35"],
            "qna_refs": [{"qna_id": "...", "title": "...", "url": "...", "date": "..."}]
        }
    ]
    """
    return await page.evaluate("""
        () => {
            const results = [];

            // HTML 테이블 → Markdown 테이블 변환
            // 첫 행을 헤더로 처리하고 |---|--- 구분선을 추가
            function tableToMarkdown(table) {
                const rows = [];
                table.querySelectorAll('tr').forEach(tr => {
                    const row = Array.from(tr.querySelectorAll('th, td')).map(td =>
                        td.textContent.replace(/[\\r\\n\\t]+/g, ' ').replace(/\\s+/g, ' ').trim()
                    );
                    if (row.some(cell => cell !== '')) rows.push(row);
                });

                if (rows.length === 0) return '';

                const colCount = Math.max(...rows.map(r => r.length));
                const lines = [];
                rows.forEach((row, i) => {
                    // 모든 행의 열 수를 colCount에 맞게 패딩
                    const cells = Array(colCount).fill('').map((_, j) => row[j] || '');
                    lines.push('| ' + cells.join(' | ') + ' |');
                    // 헤더 행(첫 번째 행) 다음에 구분선 추가
                    if (i === 0) {
                        lines.push('| ' + Array(colCount).fill('---').join(' | ') + ' |');
                    }
                });
                return lines.join('\\n');
            }

            // div.temp 전용 스마트 테이블 변환
            // 테이블 유형을 먼저 판별(Pre-scan)한 후 적합한 방식으로 변환:
            //
            // [테이블 유형 분류]
            //   - ALL TEXT     : 숫자 셀이 전혀 없음 → 전체 평문 출력 (\\n\\n 구분)
            //   - PURE FINANCIAL: 긴 텍스트 행 < 3개 → 모든 행을 재무제표로 처리 (단일 마크다운 테이블)
            //   - MIXED        : 긴 텍스트 행 >= 3개 → 텍스트/재무제표 행 분리 출력
            //
            // [행 분류 기준 - MIXED 모드]
            //   1. spacer: tr.height < 8pt 또는 모든 셀이 비어있음 → 무시
            //   2. text:   숫자 전용 셀이 없고 비빈 셀이 왼쪽 1/3 열 영역에 있음
            //   3. financial: 그 외 (숫자 셀 있음, 또는 비빈 셀이 우측 열에만 있음)
            //
            // [출력]
            //   - text 행 → "\\n\\n"으로 결합한 평문 (마크다운 문단 구분)
            //   - financial 행 → 빈 열 제거 후 마크다운 테이블
            //   - 블록 사이 "\\n\\n"으로 구분
            function smartTableToContent(table) {
                // 1. 전체 열 수 계산 (colspan 합계의 최댓값)
                let totalCols = 0;
                table.querySelectorAll('tr').forEach(tr => {
                    let rowCols = 0;
                    tr.querySelectorAll('th, td').forEach(td => {
                        rowCols += parseInt(td.getAttribute('colspan') || '1', 10);
                    });
                    if (rowCols > totalCols) totalCols = rowCols;
                });
                if (totalCols === 0) return '';

                // 텍스트 행 판정: 비빈 셀이 전체 열의 왼쪽 1/3 영역에 있으면 텍스트 행
                const leftThreshold = Math.floor(totalCols / 3);

                // 재무제표 숫자 패턴: 1,000 / (90) / 6.6 / - 등
                const numericPattern = /^[\\d,.()\\ \\-]+$/;

                // spacer 행 판별 헬퍼 (height < 8pt 또는 모든 셀 비어있음)
                const isSpacerRow = (tr) => {
                    const heightStr = tr.style.height || '';
                    const heightPt = parseFloat(heightStr);
                    if (heightPt > 0 && heightPt < 8) return true;
                    const cells = Array.from(tr.querySelectorAll('th, td'));
                    return cells.every(td =>
                        td.textContent.replace(/[\\r\\n\\t]+/g, ' ').replace(/\\s+/g, ' ').trim() === ''
                    );
                };

                // 셀 텍스트 데이터 추출 헬퍼 (colspan 포함)
                const getCellData = (tr) => {
                    const cellData = [];
                    let colIdx = 0;
                    tr.querySelectorAll('th, td').forEach(td => {
                        const text = td.textContent.replace(/[\\r\\n\\t]+/g, ' ').replace(/\\s+/g, ' ').trim();
                        const colspan = parseInt(td.getAttribute('colspan') || '1', 10);
                        cellData.push({ text, colspan, startCol: colIdx });
                        colIdx += colspan;
                    });
                    return cellData;
                };

                // 2. Pre-scan: 테이블 유형 판별
                //    - numericRowExists: 숫자 셀이 있는 행 존재 여부
                //    - longTextRowCount: 숫자 셀 없는 행 중 최대 셀 길이 > 40ch인 행 수
                let numericRowExists = false;
                let longTextRowCount = 0;
                table.querySelectorAll('tr').forEach(tr => {
                    if (isSpacerRow(tr)) return;
                    const cellData = getCellData(tr);
                    if (cellData.every(c => c.text === '')) return;
                    const hasNumeric = cellData.some(c => c.text !== '' && numericPattern.test(c.text));
                    if (hasNumeric) {
                        numericRowExists = true;
                    } else {
                        const maxLen = Math.max(...cellData.map(c => c.text.length));
                        if (maxLen > 40) longTextRowCount++;
                    }
                });

                // 3. ALL TEXT 모드: 숫자 행이 전혀 없음 (IG11 등)
                if (!numericRowExists) {
                    const textLines = [];
                    table.querySelectorAll('tr').forEach(tr => {
                        if (isSpacerRow(tr)) return;
                        const cellData = getCellData(tr);
                        const nonEmpty = cellData.filter(c => c.text !== '');
                        if (nonEmpty.length === 0) return;
                        textLines.push(nonEmpty.map(c => c.text).join(' '));
                    });
                    return textLines.join('\\u2028');
                }

                // 4. 행 분류: PURE FINANCIAL vs MIXED 모드
                //    PURE FINANCIAL(longTextRowCount < 3): 모든 비spacer 행을 financial로 처리
                //    MIXED(longTextRowCount >= 3): 기존 leftThreshold 기반 text/financial 분리
                const isMixed = longTextRowCount >= 3;
                const classifiedRows = [];
                table.querySelectorAll('tr').forEach(tr => {
                    if (isSpacerRow(tr)) return;
                    const cellData = getCellData(tr);
                    if (cellData.every(c => c.text === '')) return;

                    const hasNumericCell = cellData.some(c => c.text !== '' && numericPattern.test(c.text));
                    const hasLeftContent = cellData.some(c => c.text !== '' && c.startCol <= leftThreshold);

                    if (isMixed && !hasNumericCell && hasLeftContent) {
                        // MIXED 모드: 텍스트 행
                        const text = cellData.filter(c => c.text !== '').map(c => c.text).join(' ');
                        classifiedRows.push({ type: 'text', text });
                    } else {
                        // PURE FINANCIAL 또는 MIXED의 재무제표 행: colspan 확장
                        const expanded = [];
                        cellData.forEach(c => {
                            expanded.push(c.text);
                            for (let i = 1; i < c.colspan; i++) expanded.push('');
                        });
                        classifiedRows.push({ type: 'financial', cells: expanded });
                    }
                });

                if (classifiedRows.length === 0) return '';

                // 5. 연속 같은 타입끼리 블록으로 묶기
                const blocks = [];
                let curBlock = null;
                classifiedRows.forEach(row => {
                    if (!curBlock || curBlock.type !== row.type) {
                        curBlock = { type: row.type, rows: [] };
                        blocks.push(curBlock);
                    }
                    curBlock.rows.push(row);
                });

                // 6. 블록별 출력 조립
                const parts = [];
                blocks.forEach(block => {
                    if (block.type === 'text') {
                        // 평문: 각 행을 이중 줄바꿈으로 연결 (마크다운 문단 구분)
                        parts.push(block.rows.map(r => r.text).join('\\u2028'));
                    } else {
                        // 재무제표: 빈 열 제거 후 마크다운 테이블
                        const allCells = block.rows.map(r => r.cells);
                        const maxCols = Math.max(...allCells.map(c => c.length));

                        // 전체 financial 행에서 항상 비어있는 열 인덱스 찾기
                        const emptyCols = new Set();
                        for (let j = 0; j < maxCols; j++) {
                            if (allCells.every(row => !(row[j] || '').trim())) {
                                emptyCols.add(j);
                            }
                        }

                        // 활성 열 인덱스 목록
                        const activeCols = [];
                        for (let j = 0; j < maxCols; j++) {
                            if (!emptyCols.has(j)) activeCols.push(j);
                        }
                        if (activeCols.length === 0) return;

                        const lines = [];
                        allCells.forEach((row, i) => {
                            const cells = activeCols.map(j => (row[j] || '').replace(/\\|/g, '\\\\|'));
                            lines.push('| ' + cells.join(' | ') + ' |');
                            if (i === 0) {
                                lines.push('| ' + activeCols.map(() => '---').join(' | ') + ' |');
                            }
                        });
                        parts.push(lines.join('\\n'));
                    }
                });

                // 블록 간 단락 경계 마커로 구분 (Python에서 개행으로 복원)
                return parts.join('\\u2028');
            }

            // React fiber 탐색으로 paraContent raw HTML 추출
            // li DOM 요소의 __reactFiber* 키를 찾아 부모 fiber를 타고 올라가며 paraContent prop을 탐색
            function getParaContent(li) {
                const fk = Object.keys(li).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
                if (!fk) return null;
                let fiber = li[fk];
                for (let d = 0; fiber && d < 15; d++, fiber = fiber.return) {
                    if (fiber.memoizedProps?.paraContent !== undefined)
                        return fiber.memoizedProps.paraContent;
                }
                return null;
            }

            // li[data-paranum] 선택자로 모든 문단 항목 찾기
            const paragraphItems = document.querySelectorAll('li[data-paranum]');

            paragraphItems.forEach(li => {
                const paraNum = li.getAttribute('data-paranum');
                if (!paraNum) return;

                // ★ 교차참조: li 레벨에서 한 번만 추출 (모든 케이스 공통)
                // std_refs: DOM에서 추출 (tooltip 보존)
                const stdRefsResult = [];
                li.querySelectorAll('.std-finder').forEach(el => {
                    const displayText = el.textContent?.trim() || '';
                    const container = el.closest('div') || el.parentElement;
                    const tooltipEl = container?.querySelector('.tooltip-content');
                    const tooltip = tooltipEl?.textContent?.trim() || '';
                    stdRefsResult.push({ display_text: displayText, tooltip });
                });
                // para_refs: paraContent React fiber에서 추출 (data-target-std, data-id 정확도 보장)
                const paraContent = getParaContent(li);
                const paraRefsResult = [];
                if (paraContent) {
                    const doc = new DOMParser().parseFromString(paraContent, 'text/html');
                    doc.querySelectorAll('.mundan-finder').forEach(el => {
                        const dataId = el.getAttribute('data-id');
                        if (!dataId) return;
                        paraRefsResult.push({
                            text: el.textContent?.trim() || '',
                            associated_standard: el.getAttribute('data-target-std') || null,
                            data_id: dataId,
                        });
                    });
                }

                // 문단 내부 텍스트 영역
                // 일부 문단(IG 등)은 .para-inner-para가 두 개 존재:
                //   첫 번째는 빈 요소, 두 번째에 실제 내용이 있음
                // → 뒤에서부터 탐색하여 텍스트가 있는 첫 번째 것을 사용
                const allInnerParas = li.querySelectorAll('.para-inner-para');

                // .para-inner-para가 없는 경우: 두 가지 대체 구조 처리
                if (allInnerParas.length === 0) {
                    const table = li.querySelector('table');
                    const paraDiv = li.querySelector('div.para');

                    if (table) {
                        // 케이스 1: 재무제표 테이블 직접 포함 문단
                        // (예: data-paranum="IE15", "IE15.1" 등 — 원시값 "웩15"는 _normalize_paranum에서 변환됨)
                        // div.sc-drlKqa.temp 래퍼가 있는 경우 다중 테이블 처리,
                        // 없는 경우 단순 단일 테이블 fallback 처리

                        // 원문자(⑴~⑳) 각주 참조 ID 추출 헬퍼
                        // U+2460(⑴) ~ U+2473(⑳) 범위
                        const circledNums = '⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽⑾⑿⒀⒁⒂⒃⒄⒅⒆⒇';

                        // sup 텍스트에서 footnote_references ID 추출
                        // (한N)/(주N) 또는 원문자 단일 문자를 인식
                        const extractFnId = (supText) => {
                            if (!supText) return null;
                            const stdMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                            if (stdMatch) return { id: supText.replace(/[()]/g, ''), display_text: supText };
                            if (supText.length === 1 && circledNums.includes(supText)) {
                                return { id: supText, display_text: supText };
                            }
                            return null;
                        };

                        // pipParent: div.sc-drlKqa.temp의 직계 부모 요소
                        // 없으면 li 자체를 부모로 사용
                        const firstTempDiv = li.querySelector('div.sc-drlKqa.temp');
                        const pipParent1 = firstTempDiv ? firstTempDiv.parentElement : null;

                        if (pipParent1) {
                            // ── 다중 테이블 처리 (IE15.1 등; 원시 "웩15.1"은 Python에서 변환됨) ────
                            // 테이블을 만날 때마다 이전 청크(테이블+직후 각주)를 확정(flush)하는 방식.
                            // 원문자 각주(⑴~⑳)는 테이블마다 다른 내용이므로 중복 허용;
                            // (한N)/(주N)만 seenFnIds1로 문단 내 중복 제거.
                            const htmlParts1 = [];
                            const textParts1 = [];
                            const allFnRefs1 = [];
                            const seenFnIds1 = new Set(); // (한N)/(주N)만 중복 제거

                            // 현재 처리 중인 테이블 청크 변수
                            let currentTableMd = null;
                            let currentTableHtml = null;
                            let currentFootnotes = []; // { label, content }
                            let currentFnRefs = [];    // 현재 테이블의 sup 각주 참조

                            // 현재 청크(테이블+각주)를 textParts1/htmlParts1에 확정
                            const flushChunk = () => {
                                if (currentTableMd === null) return;
                                let chunkText = currentTableMd;
                                if (currentFootnotes.length > 0) {
                                    chunkText += '\\n\\n' + currentFootnotes.map(item =>
                                        '[각주 ' + item.label + '] ' + item.content
                                    ).join('\\n\\n');
                                }
                                textParts1.push(chunkText);
                                htmlParts1.push(currentTableHtml);
                                // currentFnRefs는 이미 seenFnIds1 처리 완료된 상태로 추가
                                currentFnRefs.forEach(fn => allFnRefs1.push(fn));
                                currentTableMd = null;
                                currentTableHtml = null;
                                currentFootnotes = [];
                                currentFnRefs = [];
                            };

                            Array.from(pipParent1.children).forEach(child => {
                                const tag = child.tagName?.toUpperCase();
                                const cls = child.className || '';

                                if (tag === 'BR') return; // 줄 바꿈 무시

                                if (child.classList.contains('sc-drlKqa') && child.classList.contains('temp')) {
                                    // 새 테이블 컨테이너 → 이전 청크 flush 후 새 청크 시작
                                    flushChunk();
                                    const tbl = child.querySelector('table');
                                    if (!tbl) return;
                                    const md = smartTableToContent(tbl);
                                    if (!md) return;
                                    currentTableMd = md;
                                    currentTableHtml = tbl.outerHTML;
                                    // 테이블 내 sup 각주 참조 수집
                                    tbl.querySelectorAll('sup').forEach(sup => {
                                        const supText = sup.textContent?.trim() || '';
                                        const fn = extractFnId(supText);
                                        if (!fn) return;
                                        // (한N)/(주N): 문단 내 중복 제거
                                        // 원문자(⑴~⑳): 테이블마다 다른 내용이므로 중복 허용
                                        const isStdFn = /^(한|주)\\d+$/.test(fn.id);
                                        if (isStdFn) {
                                            if (!seenFnIds1.has(fn.id)) {
                                                seenFnIds1.add(fn.id);
                                                currentFnRefs.push(fn);
                                            }
                                        } else {
                                            currentFnRefs.push(fn);
                                        }
                                    });
                                } else if (/^idt-\\d/.test(cls) && child.classList.contains('comment')) {
                                    // 각주 설명 div → 현재 청크의 각주 목록에 추가
                                    const labelEl = child.children[0];
                                    const contentEl = child.children[1];
                                    const label = labelEl?.textContent?.trim() || '';
                                    const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                                    if (clonedContent) clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                                    const content = clonedContent?.textContent?.trim() || '';
                                    if (label || content) currentFootnotes.push({ label, content });
                                }
                            });

                            // 마지막 청크 flush
                            flushChunk();

                            // 각 청크에 이미 각주가 포함되어 있으므로 단순 join
                            const finalText1 = textParts1.join('\\n\\n');

                            if (finalText1) {
                                results.push({
                                    number: paraNum,
                                    html: htmlParts1.join('\\n'),
                                    text: finalText1,
                                    std_refs: stdRefsResult,
                                    para_refs: paraRefsResult,
                                    qna_refs: [],
                                    footnote_refs: allFnRefs1,
                                });
                            }
                        } else {
                            // ── 단순 단일 테이블 fallback (IE15 등; 원시 "웩15"는 Python에서 변환됨) ─
                            const asciiText = smartTableToContent(table);
                            if (asciiText) {
                                const tableFnRefs = [];
                                const seenTableFnIds = new Set();
                                table.querySelectorAll('sup').forEach(sup => {
                                    const supText = sup.textContent?.trim() || '';
                                    const fn = extractFnId(supText);
                                    if (fn && !seenTableFnIds.has(fn.id)) {
                                        seenTableFnIds.add(fn.id);
                                        tableFnRefs.push(fn);
                                    }
                                });
                                // idt-N.comment 각주 내용 수집
                                const footnoteItemsFb = [];
                                li.querySelectorAll('div').forEach(div => {
                                    if (!/^idt-\\d/.test(div.className || '')) return;
                                    if (!div.classList.contains('comment')) return;
                                    const labelEl = div.children[0];
                                    const contentEl = div.children[1];
                                    const label = labelEl?.textContent?.trim() || '';
                                    const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                                    if (clonedContent) clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                                    const content = clonedContent?.textContent?.trim() || '';
                                    if (label || content) footnoteItemsFb.push({ label, content });
                                });

                                let finalTextFb = asciiText;
                                if (footnoteItemsFb.length > 0) {
                                    finalTextFb += '\\n\\n' + footnoteItemsFb.map(item =>
                                        '[각주 ' + item.label + '] ' + item.content
                                    ).join('\\n\\n');
                                }

                                results.push({
                                    number: paraNum,
                                    html: table.outerHTML,
                                    text: finalTextFb,
                                    std_refs: [],
                                    para_refs: [],
                                    qna_refs: [],
                                    footnote_refs: tableFnRefs,
                                });
                            }
                        }
                        return;
                    } else if (paraDiv) {
                        // 케이스 2: div.para + div.idt-1 구조
                        // (예: CF 1.2, 1.4 — 본문 텍스트 + 하위항목 목록 + 각주)
                        // div.para: 텍스트 노드와 <sup> 각주가 인라인으로 섞인 본문
                        // div.idt-1: (번호) + 내용 쌍, .comment이면 각주 설명
                        const textParts2 = [];
                        const fnRefs2 = [];
                        const seenFnIds2 = new Set();

                        // div.para 본문 텍스트 추출 (tooltip + 각주 영역 제거 후)
                        // .para-inner-number 전체를 제거하여 각주 설명이 평문으로 누출되지 않도록 함
                        const clonedPara = paraDiv.cloneNode(true);
                        clonedPara.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                        clonedPara.querySelectorAll('.para-inner-number').forEach(el => el.remove());
                        const paraText = clonedPara.textContent?.trim() || '';
                        if (paraText) textParts2.push(paraText);

                        // div.para 내 각주 참조 (sup) 수집
                        paraDiv.querySelectorAll('sup').forEach(sup => {
                            const supText = sup.textContent?.trim() || '';
                            const fnMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                            if (fnMatch) {
                                const id = supText.replace(/[()]/g, '');
                                if (!seenFnIds2.has(id)) {
                                    seenFnIds2.add(id);
                                    fnRefs2.push({ id, display_text: supText });
                                }
                            }
                        });

                        // QnA 참조 수집
                        const qnaRefs2 = [];
                        li.querySelectorAll('a[href^="/qnas/"]').forEach(a => {
                            const href = a.getAttribute('href') || '';
                            const qnaId = href.split('/qnas/')[1] || '';
                            if (!qnaId) return;
                            const titleEl = a.querySelector('h5') || a.querySelector('span');
                            const title = (titleEl?.textContent?.trim() || a.textContent?.trim() || '')
                                .replace(/^[-\\s]+/, '').trim();
                            const dateEl = a.querySelector('time');
                            const date = dateEl?.getAttribute('datetime') || dateEl?.textContent?.trim() || '';
                            qnaRefs2.push({ qna_id: qnaId, title, url: href, date });
                        });

                        // div.idt-N 항목 처리: 일반 항목은 textParts에, comment는 각주로
                        const footnoteItems2 = [];
                        li.querySelectorAll('div[class^="idt-"]').forEach(idt => {
                            const isComment = idt.classList.contains('comment');
                            const children = Array.from(idt.children);
                            const label = children[0]?.textContent?.trim() || '';
                            const contentEl = children[1];
                            const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                            if (clonedContent) {
                                clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                            }
                            const content = clonedContent?.textContent?.trim() || '';

                            if (isComment) {
                                if (label || content) footnoteItems2.push({ label, content });
                            } else {
                                if (label || content) {
                                    textParts2.push((label ? label + ' ' : '') + content);
                                }
                            }
                        });

                        // .para-inner-number-item.comment / .para-inner-number-hanguel-item.comment 처리
                        // div.idt-1 구조가 아닌 para-inner-number 내부 comment 항목 (BC13A 등)
                        li.querySelectorAll('.para-inner-number-item.comment, .para-inner-number-hanguel-item.comment').forEach(item => {
                            const labelEl = item.querySelector('.para-number-para-num');
                            const contentEl = item.querySelector('.para-num-item-para-con');
                            const label = labelEl?.textContent?.trim() || '';
                            const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                            if (clonedContent) clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                            const content = clonedContent?.textContent?.trim() || '';
                            if (label || content) footnoteItems2.push({ label, content });
                        });

                        // 각주 내용을 맨 끝에 추가
                        if (footnoteItems2.length > 0) {
                            textParts2.push(
                                footnoteItems2.map(item => '[각주 ' + item.label + '] ' + item.content).join('\\n\\n')
                            );
                        }

                        const finalText2 = textParts2.join('\\n');
                        if (!finalText2) return;

                        results.push({
                            number: paraNum,
                            html: li.innerHTML,
                            text: finalText2,
                            std_refs: stdRefsResult,
                            para_refs: paraRefsResult,
                            qna_refs: qnaRefs2,
                            footnote_refs: fnRefs2,
                        });
                        return;
                    } else if (li.querySelector('.sc-erNlkL')) {
                        // 케이스 3: .sc-erNlkL 구조 — .para-inner-para 없이 직접 텍스트만 있는 문단
                        // (예: 한139.4 경과규정 단문, 139D 삭제 마커 등)
                        // 각주 comment 항목을 먼저 원본 li에서 수집 (139R, 139V 등)
                        const commentItems3 = li.querySelectorAll('.para-inner-number-item.comment, .para-inner-number-hanguel-item.comment');
                        const footnoteItems3 = [];
                        commentItems3.forEach(item => {
                            const labelEl = item.querySelector('.para-number-para-num');
                            const contentEl = item.querySelector('.para-num-item-para-con');
                            const label = labelEl?.textContent?.trim() || '';
                            const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                            if (clonedContent) clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                            const content = clonedContent?.textContent?.trim() || '';
                            if (label || content) footnoteItems3.push({ label, content });
                        });

                        // 각주 참조 (sup) 수집: <sup>(한4)</sup>, <sup>(주N)</sup> 형태
                        const fnRefs3 = [];
                        const seenFnIds3 = new Set();
                        li.querySelectorAll('sup').forEach(sup => {
                            const supText = sup.textContent?.trim() || '';
                            const fnMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                            if (fnMatch) {
                                const id = supText.replace(/[()]/g, '');
                                if (!seenFnIds3.has(id)) {
                                    seenFnIds3.add(id);
                                    fnRefs3.push({ id, display_text: supText });
                                }
                            }
                        });

                        const erEl = li.querySelector('.sc-erNlkL');
                        const cloned3 = erEl.cloneNode(true);
                        // tooltip + 각주 comment 항목 제거 → 순수 본문 텍스트만 추출
                        cloned3.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                        cloned3.querySelectorAll('.para-inner-number-item.comment, .para-inner-number-hanguel-item.comment').forEach(el => el.remove());
                        const text3 = cloned3.textContent?.trim() || '';
                        if (!text3 && footnoteItems3.length === 0) return;

                        // 각주 내용을 [각주 (한4)] 형식으로 맨 끝에 추가
                        let finalText3 = text3;
                        if (footnoteItems3.length > 0) {
                            finalText3 += (text3 ? '\\n\\n' : '') + footnoteItems3.map(item => '[각주 ' + item.label + '] ' + item.content).join('\\n\\n');
                        }
                        if (!finalText3) return;

                        // QnA 참조 수집
                        const qnaRefs3 = [];
                        li.querySelectorAll('a[href^="/qnas/"]').forEach(a => {
                            const href = a.getAttribute('href') || '';
                            const qnaId = href.split('/qnas/')[1] || '';
                            if (!qnaId) return;
                            const titleEl = a.querySelector('h5') || a.querySelector('span');
                            const title = (titleEl?.textContent?.trim() || a.textContent?.trim() || '')
                                .replace(/^[-\\s]+/, '').trim();
                            const dateEl = a.querySelector('time');
                            const date = dateEl?.getAttribute('datetime') || dateEl?.textContent?.trim() || '';
                            qnaRefs3.push({ qna_id: qnaId, title, url: href, date });
                        });

                        results.push({
                            number: paraNum,
                            html: erEl.innerHTML,
                            text: finalText3,
                            std_refs: stdRefsResult,
                            para_refs: paraRefsResult,
                            qna_refs: qnaRefs3,
                            footnote_refs: fnRefs3,
                        });
                        return;
                    } else {
                        // 알 수 없는 구조: 스킵
                        return;
                    }
                }

                // DOM 순서대로 pip + number-items를 인터리빙하여 텍스트 수집
                // (기존 2패스 방식 대신 1패스 DOM 순회로 순서 보존)
                const textParts = [];    // { type: 'pip'|'num', text: string } DOM 순서대로
                const allHtmls = [];
                const footnoteRefs = [];
                const seenFnIds = new Set();
                const numberItems = [];  // 각주(comment) 구분용

                const pipParent = allInnerParas[0]?.parentElement;

                // pip 하나를 처리: textParts/allHtmls/stdRefs/paraRefs/footnoteRefs 갱신
                const processPip = (pip) => {
                    // pip 내부에 .para-inner-number-item.comment가 있는 경우 (BC1 패턴):
                    // pip 전체 textContent에 각주 설명이 포함되지 않도록 먼저 추출 후 제거
                    pip.querySelectorAll('.para-inner-number-item.comment, .para-inner-number-hanguel-item.comment').forEach(item => {
                        const labelEl = item.querySelector('.para-number-para-num');
                        const contentEl = item.querySelector('.para-num-item-para-con');
                        const label = labelEl?.textContent?.trim() || '';
                        const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                        if (clonedContent) clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                        const content = clonedContent?.textContent?.trim() || '';
                        if (label || content) numberItems.push({ label, content, isComment: true, outerHTML: item.outerHTML });
                    });

                    allHtmls.push(pip.innerHTML);
                    const cloned = pip.cloneNode(true);
                    cloned.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                    // pip 내부 comment 항목 제거 (이미 numberItems에 추가됨)
                    cloned.querySelectorAll('.para-inner-number-item.comment, .para-inner-number-hanguel-item.comment').forEach(el => el.remove());
                    // Excel HTML 삽입 문단에서 CSS 규칙이 textContent에 포함되지 않도록 제거
                    cloned.querySelectorAll('style, head, meta, link, script').forEach(el => el.remove());
                    cloned.querySelectorAll('table').forEach(table => {
                        const ascii = tableToMarkdown(table);
                        if (ascii) {
                            table.replaceWith(document.createTextNode('\\n' + ascii + '\\n'));
                        }
                    });
                    const t = cloned.textContent?.trim();
                    if (t) textParts.push({ type: 'pip', text: t });

                    // 각주 참조 수집: <sup>(한N)</sup>, <sup>(주N)</sup> 형태
                    pip.querySelectorAll('sup').forEach(sup => {
                        const supText = sup.textContent?.trim() || '';
                        const fnMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                        if (fnMatch) {
                            const id = supText.replace(/[()]/g, '');
                            if (!seenFnIds.has(id)) {
                                seenFnIds.add(id);
                                footnoteRefs.push({ id, display_text: supText });
                            }
                        }
                    });
                };

                // number-item 하나를 처리: numberItems/textParts/footnoteRefs 갱신
                const processNumberItem = (item) => {
                    const labelEl = item.querySelector('.para-number-para-num');
                    const contentEl = item.querySelector('.para-num-item-para-con');
                    const label = labelEl?.textContent?.trim() || '';
                    // tooltip 제거 후 텍스트 추출 (pip과 동일 방식 — tooltip 누출 버그 수정)
                    const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                    if (clonedContent) {
                        clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                    }
                    const content = clonedContent?.textContent?.trim() || '';
                    const isComment = item.classList.contains('comment');

                    if (label || content) {
                        numberItems.push({ label, content, isComment, outerHTML: item.outerHTML });
                        // 일반 항목만 textParts에 DOM 순서대로 추가 (각주는 맨 끝에 별도 추가)
                        if (!isComment) {
                            textParts.push({ type: 'num', text: (label ? label + ' ' : '') + content });
                        }
                    }

                    // number-item 내 각주 참조 수집
                    item.querySelectorAll('sup').forEach(sup => {
                        const supText = sup.textContent?.trim() || '';
                        const fnMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                        if (fnMatch) {
                            const id = supText.replace(/[()]/g, '');
                            if (!seenFnIds.has(id)) {
                                seenFnIds.add(id);
                                footnoteRefs.push({ id, display_text: supText });
                            }
                        }
                    });
                };

                // idt-N 항목 하나를 처리: textParts 갱신
                // idt 구조: <div class="idt-N"><div>레이블</div><div>내용(std-finder 포함 가능)</div></div>
                // .comment 클래스가 있으면 각주로 처리 (139R의 .idt-1.comment 패턴)
                const processIdtItem = (idt) => {
                    const labelEl = idt.children[0];
                    const contentEl = idt.children[1];
                    const label = labelEl?.textContent?.trim() || '';
                    // tooltip 제거 후 텍스트 추출
                    const clonedContent = contentEl ? contentEl.cloneNode(true) : null;
                    if (clonedContent) {
                        clonedContent.querySelectorAll('.tooltip-content').forEach(el => el.remove());
                    }
                    const content = clonedContent?.textContent?.trim() || '';
                    const isIdtComment = idt.classList.contains('comment');
                    if (isIdtComment) {
                        // 각주: numberItems에 추가 (textParts에는 추가 안 함, 맨 끝에 [각주 ...] 형식으로 출력)
                        if (label || content) numberItems.push({ label, content, isComment: true, outerHTML: idt.outerHTML });
                    } else if (label || content) {
                        textParts.push({ type: 'num', text: (label ? label + ' ' : '') + content });
                    }
                };

                // .para-inner-number 블록의 children을 순회 (중첩 재귀 지원)
                // BC13H처럼 외부 number 블록 내부에 중첩된 number 블록이 있는 경우를 처리
                const processNumberBlock = (numberBlock) => {
                    for (const item of numberBlock.children) {
                        if (item.classList.contains('para-inner-number-item') ||
                            item.classList.contains('para-inner-number-hanguel-item')) {
                            processNumberItem(item);
                        } else if (/^idt-\\d/.test(item.className)) {
                            processIdtItem(item);
                        } else if (item.classList.contains('para-inner-number')) {
                            processNumberBlock(item); // 중첩 재귀
                        }
                    }
                };

                // parent의 직접 children을 DOM 순서대로 처리 (<b> 등 래퍼 요소 재귀 포함)
                const processChildren = (parent) => {
                    for (const child of parent.children) {
                        if (child.classList.contains('para-inner-para')) {
                            processPip(child);
                        } else if (child.classList.contains('para-inner-number')) {
                            processNumberBlock(child);
                        } else if (/^idt-\\d/.test(child.className)) {
                            // idt-1, idt-2 등 들여쓰기 하위 항목 (㈎/㈏, (1-1) 등)
                            processIdtItem(child);
                        } else if (child.classList.contains('para-inner-number-item') ||
                                   child.classList.contains('para-inner-number-hanguel-item')) {
                            // para-inner-number 래퍼 없이 para-inner-para와 형제로 위치한 경우
                            // (예: 문단 102/103 — (한2)/(한3) 각주 내용)
                            processNumberItem(child);
                        } else if (child.classList.contains('temp')) {
                            // div.sc-drlKqa.temp: 외부 .htm 삽입 테이블 (IG10/IG11 등)
                            // .para-inner-para와 형제로 위치하며, 별도 선택자 조건이 없어 기존 코드에서 무시됨
                            const tbl = child.querySelector('table');
                            if (tbl) {
                                const md = smartTableToContent(tbl);
                                if (md) {
                                    allHtmls.push(child.innerHTML);
                                    textParts.push({ type: 'pip', text: md });
                                }
                            }
                        } else if (child.querySelector('.para-inner-para, .para-inner-number')) {
                            // <b> 등 래퍼 요소: 내부에 pip/number가 있으면 재귀 처리
                            processChildren(child);
                        }
                    }
                };

                if (pipParent) processChildren(pipParent);

                // text 조립: DOM 순서 텍스트 + 각주 내용은 맨 끝에
                // 블록 경계: '\\u2029' (Unicode Paragraph Separator) 사용 — Python에서 분할 후 개별 clean_text 적용
                let finalText = '';
                for (let i = 0; i < textParts.length; i++) {
                    if (i === 0) {
                        finalText = textParts[i].text;
                    } else {
                        finalText += '\\u2029' + textParts[i].text;
                    }
                }

                // 각주 내용: [각주 (한2)] 설명...
                const footnoteItems = numberItems.filter(item => item.isComment);
                if (footnoteItems.length > 0) {
                    finalText += '\\n\\n' + footnoteItems.map(item =>
                        '[각주 ' + item.label + '] ' + item.content
                    ).join('\\n\\n');
                }

                // 4. HTML: PIP innerHTML + 하위항목 outerHTML
                const html = [...allHtmls, ...numberItems.map(item => item.outerHTML)].join('\\n');

                // 5. 질의회신 추출: 버튼 클릭 후 li 내부에 펼쳐짐이 검증됨
                const qnaRefs = [];
                li.querySelectorAll('a[href^="/qnas/"]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const qnaId = href.split('/qnas/')[1] || '';
                    if (!qnaId) return;
                    const titleEl = a.querySelector('h5') || a.querySelector('span');
                    const title = (titleEl?.textContent?.trim() || a.textContent?.trim() || '')
                        .replace(/^[-\\s]+/, '').trim();
                    const dateEl = a.querySelector('time');
                    const date = dateEl?.getAttribute('datetime') || dateEl?.textContent?.trim() || '';
                    qnaRefs.push({ qna_id: qnaId, title, url: href, date });
                });

                results.push({
                    number: paraNum,
                    html,
                    text: finalText,
                    std_refs: stdRefsResult,
                    para_refs: paraRefsResult,
                    qna_refs: qnaRefs,
                    footnote_refs: footnoteRefs,
                });
            });

            return results;
        }
    """)


async def _extract_free_content_js(page: Page, section_id: str) -> list[dict]:
    """
    li[data-paranum]이 없는 자유형식 페이지에서 콘텐츠를 추출합니다.
    ("기타 참고사항" 하위: relationship, overview, revision 등)

    메인 콘텐츠 영역의 h1~h6, p, table, ul/ol을 DOM 순서대로 수집하여
    하나의 문단으로 합쳐 반환합니다.

    Returns:
        [{"number": section_id, "html": "...", "text": "...", "std_refs": [], ...}]
    """
    raw = await page.evaluate("""
        (sectionId) => {
            // HTML 테이블 → Markdown 테이블 변환 (section_parser와 동일)
            function tableToMarkdown(table) {
                const rows = [];
                table.querySelectorAll('tr').forEach(tr => {
                    const row = Array.from(tr.querySelectorAll('th, td')).map(td =>
                        td.textContent.replace(/[\\r\\n\\t]+/g, ' ').replace(/\\s+/g, ' ').trim()
                    );
                    if (row.some(cell => cell !== '')) rows.push(row);
                });
                if (rows.length === 0) return '';
                const colCount = Math.max(...rows.map(r => r.length));
                const lines = [];
                rows.forEach((row, i) => {
                    const cells = Array(colCount).fill('').map((_, j) => row[j] || '');
                    lines.push('| ' + cells.join(' | ') + ' |');
                    if (i === 0) {
                        lines.push('| ' + Array(colCount).fill('---').join(' | ') + ' |');
                    }
                });
                return lines.join('\\n');
            }

            // aside 제외 메인 콘텐츠 영역 탐색
            // aside와 같은 부모를 가진 형제 컨테이너, 또는 main/article 사용
            const aside = document.querySelector('aside');
            let container = null;

            if (aside) {
                // aside의 형제 요소 중 텍스트 콘텐츠가 가장 많은 것 선택
                const siblings = Array.from(aside.parentElement?.children || []);
                let maxLen = 0;
                for (const sib of siblings) {
                    if (sib === aside) continue;
                    const len = sib.textContent?.trim().length || 0;
                    if (len > maxLen) { maxLen = len; container = sib; }
                }
            }
            if (!container) container = document.querySelector('main') || document.querySelector('article');
            if (!container) container = document.body;

            // 콘텐츠 요소들을 DOM 순서대로 수집
            const textParts = [];
            const htmlParts = [];

            // aside 내부는 제외하고 순회
            const walker = document.createTreeWalker(
                container,
                NodeFilter.SHOW_ELEMENT,
                {
                    acceptNode(node) {
                        // aside 내부 요소 제외
                        if (node.closest('aside')) return NodeFilter.FILTER_REJECT;
                        // 원하는 태그만 허용
                        const tag = node.tagName.toLowerCase();
                        if (/^h[1-6]$/.test(tag) || tag === 'p' || tag === 'table' || tag === 'ul' || tag === 'ol') {
                            return NodeFilter.FILTER_ACCEPT;
                        }
                        return NodeFilter.FILTER_SKIP;
                    }
                }
            );

            // 이미 처리된 table의 하위 요소 중복 방지를 위한 Set
            const processedTables = new Set();

            let node;
            while ((node = walker.nextNode())) {
                const tag = node.tagName.toLowerCase();

                // table 내부의 중복 수집 방지
                if (node.closest('table') && node.tagName !== 'TABLE') continue;
                if (processedTables.has(node)) continue;

                if (/^h[1-6]$/.test(tag)) {
                    const level = parseInt(tag[1]);
                    const hashes = '#'.repeat(Math.max(2, level));  // 최소 ##
                    const text = node.textContent?.trim() || '';
                    if (text) {
                        textParts.push(hashes + ' ' + text);
                        htmlParts.push(node.outerHTML);
                    }
                } else if (tag === 'p') {
                    const text = node.textContent?.trim() || '';
                    if (text) {
                        textParts.push(text);
                        htmlParts.push(node.outerHTML);
                    }
                } else if (tag === 'table') {
                    processedTables.add(node);
                    const md = tableToMarkdown(node);
                    if (md) {
                        textParts.push(md);
                        htmlParts.push(node.outerHTML);
                    }
                } else if (tag === 'ul' || tag === 'ol') {
                    // 리스트: 항목을 "- 텍스트" 형태로 변환
                    const items = Array.from(node.querySelectorAll('li'))
                        .map(li => '- ' + (li.textContent?.trim() || ''))
                        .filter(s => s.length > 2);
                    if (items.length > 0) {
                        textParts.push(items.join('\\n'));
                        htmlParts.push(node.outerHTML);
                    }
                }
            }

            if (textParts.length === 0) return null;

            return {
                number: sectionId,
                html: htmlParts.join('\\n'),
                text: textParts.join('\\n\\n'),
                std_refs: [],
                para_refs: [],
                qna_refs: [],
            };
        }
    """, section_id)

    if not raw:
        return []
    return [raw]


def _build_cross_references(raw: dict) -> list[CrossReference]:
    """
    추출된 원시 데이터에서 CrossReference 목록을 생성합니다.

    std_refs → type="standard" CrossReference,
    para_refs → type="paragraph" CrossReference 로 변환합니다.
    para_refs는 {"text": ..., "associated_standard": ...} 딕셔너리 또는
    구형 문자열 형태를 모두 처리합니다.

    Args:
        raw: _extract_paragraphs_js가 반환한 단일 문단 딕셔너리.
             "std_refs"와 "para_refs" 키를 포함해야 합니다.

    Returns:
        CrossReference 모델 목록. std_refs → para_refs 순서로 정렬됩니다.
    """
    refs: list[CrossReference] = []

    # 기준서 참조
    for std_ref in raw.get("std_refs", []):
        display_text = normalize_unicode_parens(std_ref.get("display_text", ""))
        tooltip = std_ref.get("tooltip", "")
        standard_number = extract_standard_number(display_text)
        refs.append(CrossReference(
            type="standard",
            display_text=display_text,
            standard_number=standard_number,
            standard_title=tooltip or None,
        ))

    # 문단 참조
    # JS 수정 후: para_ref는 {"text": "...", "associated_standard": "1109"|null} 딕셔너리
    # 하위 호환: 기존 문자열 형태도 처리
    for para_ref in raw.get("para_refs", []):
        if isinstance(para_ref, dict):
            para_ref_text = normalize_unicode_parens(para_ref.get("text", ""))
            associated_standard = para_ref.get("associated_standard")
            data_id = para_ref.get("data_id")  # React fiber에서 추출한 정확한 data-id
        else:
            para_ref_text = normalize_unicode_parens(str(para_ref))
            associated_standard = None
            data_id = None
        if data_id:
            # React fiber data-id 직접 활용 → 텍스트 파싱 불필요
            range_str = data_id.strip()
            ids = resolve_paragraph_ids(range_str)
        else:
            range_str = extract_paragraph_range(para_ref_text)
            ids = resolve_paragraph_ids(range_str) if range_str else []
        refs.append(CrossReference(
            type="paragraph",
            display_text=para_ref_text,
            standard_number=associated_standard,
            range=range_str,
            paragraph_ids=ids,
        ))

    # 1차 dedup: type="standard"는 standard_number 기준, type="paragraph"는 (standard_number, range) 기준
    deduped: list[CrossReference] = []
    seen_std: set[str | None] = set()
    seen_para: set[tuple[str | None, str | None]] = set()

    for ref in refs:
        if ref.type == "standard":
            if ref.standard_number in seen_std:
                continue
            seen_std.add(ref.standard_number)
        elif ref.type == "paragraph":
            key = (ref.standard_number, ref.range)
            if key in seen_para:
                continue
            seen_para.add(key)
        deduped.append(ref)

    # 2차 dedup: paragraph_ids가 이미 커버된 집합의 부분집합인 경우 제거
    # 예: ["7","93"] 가 먼저 커버되면 ["7"], ["93"] 는 제거됨
    # (동일 span이 분할 표현으로 DOM에 여러 번 등장하는 경우 처리)
    covered: dict[str | None, set[str]] = {}  # standard_number → 커버된 paragraph_ids 집합
    para_refs_final: list[CrossReference] = []
    para_deduped = [r for r in deduped if r.type == "paragraph"]
    # paragraph_ids 개수 내림차순 정렬 (포괄적인 ref 먼저 처리)
    para_deduped_sorted = sorted(para_deduped, key=lambda r: -len(r.paragraph_ids))

    for ref in para_deduped_sorted:
        std = ref.standard_number
        if std not in covered:
            covered[std] = set()
        new_ids = set(ref.paragraph_ids)
        if new_ids and new_ids.issubset(covered[std]):
            continue  # 이미 커버된 부분집합 → 스킵
        covered[std].update(new_ids)
        para_refs_final.append(ref)

    # 원래 DOM 순서 복원
    order = {id(r): i for i, r in enumerate(deduped)}
    para_refs_final.sort(key=lambda r: order[id(r)])

    # 최종 목록: standard refs + 2차 dedup된 paragraph refs
    std_refs = [r for r in deduped if r.type == "standard"]
    return std_refs + para_refs_final


def _build_qna_references(raw: dict) -> list[QnAReference]:
    """
    추출된 원시 데이터에서 QnAReference 목록을 생성합니다.

    Args:
        raw: _extract_paragraphs_js가 반환한 단일 문단 딕셔너리.
             "qna_refs" 키에 qna_id/title/url/date 딕셔너리 목록이 있어야 합니다.

    Returns:
        QnAReference 모델 목록. qna_id가 없는 항목은 건너뜁니다.
    """
    refs: list[QnAReference] = []
    for qna_raw in raw.get("qna_refs", []):
        qna_id = qna_raw.get("qna_id", "")
        if not qna_id:
            continue
        refs.append(QnAReference(
            qna_id=qna_id,
            title=qna_raw.get("title", ""),
            url=qna_raw.get("url", ""),
            date=qna_raw.get("date", ""),
        ))
    return refs


def _build_footnote_references(raw: dict) -> list[FootnoteReference]:
    """
    추출된 원시 데이터에서 FootnoteReference 목록을 생성합니다.

    Args:
        raw: _extract_paragraphs_js가 반환한 단일 문단 딕셔너리.
             "footnote_refs" 키에 id/display_text 딕셔너리 목록이 있어야 합니다.

    Returns:
        FootnoteReference 모델 목록. "(한N)", "(주N)" 형태만 포함됩니다.
    """
    refs: list[FootnoteReference] = []
    for fn in raw.get("footnote_refs", []):
        fn_id = fn.get("id", "")
        if not fn_id:
            continue
        refs.append(FootnoteReference(
            id=normalize_unicode_parens(fn_id),
            display_text=normalize_unicode_parens(fn.get("display_text", "")),
        ))
    return refs


async def parse_section(
    page: Page,
    section_url: str,
    section_id: str,
    section_title: str,
    toc_path: str,
    is_free_content: bool = False,
) -> list[Paragraph]:
    """
    특정 섹션 URL의 문단들을 파싱합니다.

    Args:
        page: Playwright 페이지 객체
        section_url: 섹션 URL
        section_id: 섹션 고유 ID
        section_title: 섹션 제목
        toc_path: 목차 경로
        is_free_content: True이면 li[data-paranum] 없는 자유형식 HTML 파싱

    Returns:
        문단 목록
    """
    # URL이 상대 경로면 절대 경로로 변환
    if section_url.startswith("/"):
        full_url = f"{BASE_URL}{section_url}"
    else:
        full_url = section_url

    logger.debug("섹션 파싱: %s (%s)", section_title, full_url)

    # 페이지 이동
    success = await navigate_with_retry(page, full_url)
    if not success:
        logger.error("섹션 페이지 이동 실패: %s", full_url)
        return []

    if is_free_content:
        # 자유형식 페이지: li[data-paranum] 없이 h3/p/table 직접 파싱
        raw_paragraphs = await _extract_free_content_js(page, section_id)
    else:
        # 질의회신 버튼 확장 (접이식 패널 열기)
        await _expand_qna_buttons(page)
        # 모든 문단 데이터 일괄 추출
        raw_paragraphs = await _extract_paragraphs_js(page)

        # li[data-paranum]이 없는데 is_free_content=False인 경우 자유형식으로 재시도
        if not raw_paragraphs:
            logger.debug("li[data-paranum] 없음, 자유형식 파싱 재시도: %s", section_title)
            raw_paragraphs = await _extract_free_content_js(page, section_id)

    if not raw_paragraphs:
        logger.debug("문단 없음: %s", section_title)
        return []

    logger.debug("문단 %d개 추출됨: %s", len(raw_paragraphs), section_title)

    paragraphs = []
    for raw in raw_paragraphs:
        number = raw.get("number", "")
        if not number:
            continue
        number = _normalize_paranum(number)

        raw_text = raw.get("text", "")
        text = _process_raw_text(raw_text)
        html = raw.get("html", "")
        cross_refs = _build_cross_references(raw)
        qna_refs = _build_qna_references(raw)
        footnote_refs = _build_footnote_references(raw)

        paragraphs.append(Paragraph(
            number=number,
            section_id=section_id,
            section_title=section_title,
            toc_path=toc_path,
            text=text,
            html=html,
            cross_references=cross_refs,
            qna_references=qna_refs,
            footnote_references=footnote_refs,
        ))

    return paragraphs


async def parse_section_from_current_page(
    page: Page,
    section_id: str,
    section_title: str,
    toc_path: str,
) -> list[Paragraph]:
    """
    현재 페이지에서 직접 문단을 파싱합니다 (페이지 이동 없이).

    같은 기준서 내 여러 섹션이 단일 URL에 렌더링될 때 사용합니다.
    parse_section과 달리 navigate_with_retry를 호출하지 않으므로
    현재 페이지 상태를 그대로 파싱합니다.

    Args:
        page: 이미 대상 URL로 이동된 Playwright 페이지 객체.
        section_id: 섹션 고유 ID (toc_parser._make_section_id 결과).
        section_title: 섹션 제목 (Paragraph.section_title에 저장).
        toc_path: 목차 경로 문자열 (예: "본문|적용범위").

    Returns:
        Paragraph 모델 목록. 현재 페이지의 모든 li[data-paranum]을 반환합니다.
    """
    await _expand_qna_buttons(page)
    raw_paragraphs = await _extract_paragraphs_js(page)

    paragraphs = []
    for raw in raw_paragraphs:
        number = raw.get("number", "")
        if not number:
            continue
        number = _normalize_paranum(number)

        raw_text = raw.get("text", "")
        text = _process_raw_text(raw_text)
        html = raw.get("html", "")
        cross_refs = _build_cross_references(raw)
        qna_refs = _build_qna_references(raw)
        footnote_refs = _build_footnote_references(raw)

        paragraphs.append(Paragraph(
            number=number,
            section_id=section_id,
            section_title=section_title,
            toc_path=toc_path,
            text=text,
            html=html,
            cross_references=cross_refs,
            qna_references=qna_refs,
            footnote_references=footnote_refs,
        ))

    return paragraphs
