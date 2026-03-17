"""
CLI 진입점 - KASB QnA 크롤러

KASB REST API를 통해 K-IFRS 및 일반기업회계기준 질의응답 약 2,243건을 수집하여
output/qnas/{docNumber}.json 형태로 저장하고, _index.json을 생성합니다.

기준서 크롤러(src/main.py)와 달리 httpx AsyncClient 기반 REST API를 사용하여
브라우저 없이 실행됩니다.

실행 예시::

    # 전체 수집 (약 2,243건)
    .venv/bin/python3 -m src.qna_main

    # 중단된 곳에서 재개
    .venv/bin/python3 -m src.qna_main --resume

    # 단건 테스트
    .venv/bin/python3 -m src.qna_main --qna-id 2020-I-KQA009

    # 상세 로그
    .venv/bin/python3 -m src.qna_main --verbose

CLI 옵션:
    --qna-id    특정 QnA docNumber (예: 2020-I-KQA009)
    --resume    체크포인트에서 재개
    --verbose, -v  상세 로그 출력
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """CLI 인수를 파싱합니다."""
    parser = argparse.ArgumentParser(
        description="KASB QnA 크롤러 - REST API로 질의응답 2,243건을 JSON으로 수집합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  # 전체 크롤링
  python -m src.qna_main

  # 중단된 곳에서 재개
  python -m src.qna_main --resume

  # 단건 테스트
  python -m src.qna_main --qna-id 2020-I-KQA009

  # 상세 로그
  python -m src.qna_main --verbose
        """,
    )

    parser.add_argument(
        "--qna-id",
        type=str,
        default=None,
        help="특정 QnA docNumber (예: 2020-I-KQA009). 생략하면 전체 수집.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="이전 체크포인트에서 재개합니다.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/qnas"),
        help="출력 디렉토리 경로 (기본값: output/qnas)",
    )
    parser.add_argument(
        "--checkpoint-file",
        type=Path,
        default=Path("checkpoints/qna_progress.json"),
        help="체크포인트 파일 경로 (기본값: checkpoints/qna_progress.json)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="상세 로그를 출력합니다.",
    )

    return parser.parse_args()


async def main() -> int:
    """
    메인 진입점.

    워크플로우:
        1. CLI 인수 파싱
        2. 로그 레벨 설정 (--verbose 시 DEBUG)
        3. qna_crawler.orchestrator.crawl_qna 호출

    Returns:
        종료 코드. 0: 성공, 1: 사용자 중단(KeyboardInterrupt), 2: 치명적 오류.
    """
    args = parse_args()

    # 로그 레벨 설정
    from src.utils import logger
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)

    logger.info("KASB QnA 크롤러 시작")
    logger.info("출력 디렉토리: %s", args.output_dir)

    if args.qna_id:
        logger.info("단건 수집: %s", args.qna_id)
    elif args.resume:
        logger.info("재개 모드: 완료된 QnA는 스킵합니다")
    else:
        logger.info("전체 수집 모드 (약 2,243건)")

    from src.qna_crawler.orchestrator import crawl_qna

    try:
        await crawl_qna(
            output_dir=args.output_dir,
            checkpoint_path=args.checkpoint_file,
            target_qna_id=args.qna_id,
            resume=args.resume,
        )
        logger.info("QnA 수집이 성공적으로 완료되었습니다.")
        return 0

    except KeyboardInterrupt:
        logger.info("사용자에 의해 중단되었습니다. --resume으로 재개할 수 있습니다.")
        return 1

    except Exception as e:
        logger.error("수집 중 치명적 오류 발생: %s", e)
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
