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
from src.utils import logger, clean_text, extract_standard_number, extract_paragraph_range, resolve_paragraph_ids
from src.browser import navigate_with_retry


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

            // li[data-paranum] 선택자로 모든 문단 항목 찾기
            const paragraphItems = document.querySelectorAll('li[data-paranum]');

            paragraphItems.forEach(li => {
                const paraNum = li.getAttribute('data-paranum');
                if (!paraNum) return;

                // 문단 내부 텍스트 영역
                // 일부 문단(IG 등)은 .para-inner-para가 두 개 존재:
                //   첫 번째는 빈 요소, 두 번째에 실제 내용이 있음
                // → 뒤에서부터 탐색하여 텍스트가 있는 첫 번째 것을 사용
                const allInnerParas = li.querySelectorAll('.para-inner-para');

                // .para-inner-para가 없는 경우: 재무제표 테이블 직접 포함 문단
                // (예: data-paranum="웩15" 등 — HTML 테이블만 있고 wrapper 없음)
                if (allInnerParas.length === 0) {
                    const table = li.querySelector('table');
                    if (!table) return;
                    const asciiText = tableToMarkdown(table);
                    if (asciiText) {
                        // 테이블 내 각주 참조도 추출
                        const tableFnRefs = [];
                        const seenTableFnIds = new Set();
                        table.querySelectorAll('sup').forEach(sup => {
                            const supText = sup.textContent?.trim() || '';
                            const fnMatch = supText.match(/^\\((한|주)\\d+\\)$/);
                            if (fnMatch) {
                                const id = supText.replace(/[()]/g, '');
                                if (!seenTableFnIds.has(id)) {
                                    seenTableFnIds.add(id);
                                    tableFnRefs.push({ id, display_text: supText });
                                }
                            }
                        });
                        results.push({
                            number: paraNum,
                            html: table.outerHTML,
                            text: asciiText,
                            std_refs: [],
                            para_refs: [],
                            qna_refs: [],
                            footnote_refs: tableFnRefs,
                        });
                    }
                    return;
                }

                // DOM 순서대로 pip + number-items를 인터리빙하여 텍스트 수집
                // (기존 2패스 방식 대신 1패스 DOM 순회로 순서 보존)
                const textParts = [];    // { type: 'pip'|'num', text: string } DOM 순서대로
                const allHtmls = [];
                const stdRefs = [];
                const paraRefs = [];
                const footnoteRefs = [];
                const seenFnIds = new Set();
                const numberItems = [];  // 각주(comment) 구분용

                const pipParent = allInnerParas[0]?.parentElement;

                // pip 하나를 처리: textParts/allHtmls/stdRefs/paraRefs/footnoteRefs 갱신
                const processPip = (pip) => {
                    allHtmls.push(pip.innerHTML);
                    const cloned = pip.cloneNode(true);
                    cloned.querySelectorAll('.tooltip-content').forEach(el => el.remove());
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

                    // 기준서/문단 교차참조 수집: DOM 순서대로 순회하여 std-finder → mundan-finder 연결
                    let lastStdNum = null;
                    pip.querySelectorAll('.std-finder, .mundan-finder').forEach(el => {
                        if (el.classList.contains('std-finder')) {
                            const displayText = el.textContent?.trim() || '';
                            const container = el.closest('div') || el.parentElement;
                            const tooltipEl = container?.querySelector('.tooltip-content');
                            const tooltip = tooltipEl?.textContent?.trim() || '';
                            const stdMatch = displayText.match(/제(\\d+)호/);
                            lastStdNum = stdMatch ? stdMatch[1] : null;
                            stdRefs.push({ display_text: displayText, tooltip });
                        } else if (el.classList.contains('mundan-finder')) {
                            paraRefs.push({ text: el.textContent?.trim() || '', associated_standard: lastStdNum });
                        }
                    });

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

                // number-item 하나를 처리: numberItems/textParts/stdRefs/paraRefs/footnoteRefs 갱신
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

                    // number-item 내 교차참조 수집
                    let itemLastStdNum = null;
                    item.querySelectorAll('.std-finder, .mundan-finder').forEach(el => {
                        if (el.classList.contains('std-finder')) {
                            const displayText = el.textContent?.trim() || '';
                            const container = el.closest('div') || el.parentElement;
                            const tooltipEl = container?.querySelector('.tooltip-content');
                            const tooltip = tooltipEl?.textContent?.trim() || '';
                            const stdMatch = displayText.match(/제(\\d+)호/);
                            itemLastStdNum = stdMatch ? stdMatch[1] : null;
                            stdRefs.push({ display_text: displayText, tooltip });
                        } else if (el.classList.contains('mundan-finder')) {
                            paraRefs.push({ text: el.textContent?.trim() || '', associated_standard: itemLastStdNum });
                        }
                    });

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

                // parent의 직접 children을 DOM 순서대로 처리 (<b> 등 래퍼 요소 재귀 포함)
                const processChildren = (parent) => {
                    for (const child of parent.children) {
                        if (child.classList.contains('para-inner-para')) {
                            processPip(child);
                        } else if (child.classList.contains('para-inner-number')) {
                            // number 블록: 하위 number-item들을 DOM 순서대로 처리
                            child.querySelectorAll('.para-inner-number-item').forEach(processNumberItem);
                        } else if (child.querySelector('.para-inner-para, .para-inner-number')) {
                            // <b> 등 래퍼 요소: 내부에 pip/number가 있으면 재귀 처리
                            processChildren(child);
                        }
                    }
                };

                if (pipParent) processChildren(pipParent);

                // text 조립: DOM 순서 텍스트 + 각주 내용은 맨 끝에
                // pip↔pip 사이는 '\\n\\n', number-item 관련 전환은 '\\n'
                let finalText = '';
                for (let i = 0; i < textParts.length; i++) {
                    if (i === 0) {
                        finalText = textParts[i].text;
                    } else {
                        const sep = (textParts[i].type === 'num' || textParts[i-1].type === 'num') ? '\\n' : '\\n\\n';
                        finalText += sep + textParts[i].text;
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
                    std_refs: stdRefs,
                    para_refs: paraRefs,
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
        display_text = std_ref.get("display_text", "")
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
            para_ref_text = para_ref.get("text", "")
            associated_standard = para_ref.get("associated_standard")
        else:
            para_ref_text = str(para_ref)
            associated_standard = None
        range_str = extract_paragraph_range(para_ref_text)
        ids = resolve_paragraph_ids(range_str) if range_str else []
        refs.append(CrossReference(
            type="paragraph",
            display_text=para_ref_text,
            standard_number=associated_standard,
            range=range_str,
            paragraph_ids=ids,
        ))

    return refs


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
            id=fn_id,
            display_text=fn.get("display_text", ""),
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

        text = clean_text(raw.get("text", ""))
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

        text = clean_text(raw.get("text", ""))
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
