"""
CLI 진입점 - K-IFRS 회계기준 크롤러

KASB 데이터베이스에서 K-IFRS 기준서/해석서/개념체계를 크롤링하여
output/standards/{id}.json 형태로 저장합니다.

실행 예시::

    # 특정 기준서 단건 크롤링
    .venv/bin/python3 -m src.main --standard 1001

    # 전체 크롤링 (61개 기준서/해석서/개념체계)
    .venv/bin/python3 -m src.main

    # 중단된 곳에서 재개
    .venv/bin/python3 -m src.main --resume

    # 브라우저 창 표시 (디버깅)
    .venv/bin/python3 -m src.main --standard 1001 --no-headless

CLI 옵션:
    --standard      특정 기준서 번호 (예: 1001, 2101, CF)
    --resume        체크포인트에서 재개
    --no-headless   브라우저 창 표시
    --verbose, -v   상세 로그 출력
    --reset-checkpoint  체크포인트 초기화
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    """CLI 인수를 파싱합니다."""
    parser = argparse.ArgumentParser(
        description="K-IFRS 회계기준 크롤러 - 한국회계기준원(KASB) 기준서를 JSON으로 수집합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  # 특정 기준서 크롤링
  python -m src.main --standard 1001

  # 전체 기준서 크롤링
  python -m src.main

  # 중단된 곳에서 재개
  python -m src.main --resume

  # 출력 디렉토리 지정
  python -m src.main --standard 1001 --output-dir /tmp/output

  # 브라우저 창 표시 (디버깅용)
  python -m src.main --standard 1001 --no-headless

  # 상세 로그
  python -m src.main --standard 1001 --verbose
        """,
    )

    parser.add_argument(
        "--standard",
        type=str,
        default=None,
        help="특정 기준서 번호 (예: 1001, 2101, CF). 생략하면 전체 크롤링.",
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
        default=Path("output/standards"),
        help="출력 디렉토리 경로 (기본값: output/standards)",
    )
    parser.add_argument(
        "--checkpoint-file",
        type=Path,
        default=Path("checkpoints/progress.json"),
        help="체크포인트 파일 경로 (기본값: checkpoints/progress.json)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        default=False,
        help="브라우저 창을 표시합니다 (디버깅용).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="상세 로그를 출력합니다.",
    )
    parser.add_argument(
        "--reset-checkpoint",
        action="store_true",
        default=False,
        help="체크포인트를 초기화하고 처음부터 시작합니다.",
    )

    return parser.parse_args()


async def main() -> int:
    """
    메인 진입점.

    워크플로우:
        1. CLI 인수 파싱
        2. 로그 레벨 설정 (--verbose 시 DEBUG)
        3. CheckpointManager 초기화 (--reset-checkpoint 시 리셋)
        4. crawler.orchestrator.crawl_all 호출

    Returns:
        종료 코드. 0: 성공, 1: 사용자 중단(KeyboardInterrupt), 2: 치명적 오류.
    """
    args = parse_args()

    # 로그 레벨 설정
    from src.utils import logger, setup_logging
    if args.verbose:
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers:
            handler.setLevel(logging.DEBUG)

    # 체크포인트 초기화
    from src.checkpoint import CheckpointManager
    checkpoint = CheckpointManager(args.checkpoint_file)

    if args.reset_checkpoint:
        checkpoint.reset()
        logger.info("체크포인트가 초기화되었습니다.")

    # 출력 디렉토리 생성
    args.output_dir.mkdir(parents=True, exist_ok=True)

    headless = not args.no_headless

    logger.info("K-IFRS 회계기준 크롤러 시작")
    logger.info("출력 디렉토리: %s", args.output_dir)
    logger.info("헤드리스 모드: %s", headless)

    if args.standard:
        logger.info("크롤링 대상: 기준서 %s", args.standard)
    else:
        logger.info("크롤링 대상: 전체 기준서/해석서/개념체계")

    if args.resume:
        logger.info("재개 모드: 완료된 기준서는 스킵합니다")

    # 크롤링 실행
    from src.crawler.orchestrator import crawl_all

    try:
        await crawl_all(
            output_dir=args.output_dir,
            checkpoint=checkpoint,
            headless=headless,
            target_standard=args.standard,
            resume=args.resume,
        )
        logger.info("크롤링이 성공적으로 완료되었습니다.")

        # 전체 크롤링 완료 후 교차 기준서 범위 전개 포스트프로세싱
        # 단건 크롤링 시에는 타 기준서가 갱신되지 않았으므로 스킵
        if not args.standard:
            from src.postprocessor import process_all
            logger.info("교차 기준서 범위 전개 포스트프로세싱 시작...")
            process_all(standards_dir=args.output_dir, target_standard=None, preview=False)

        return 0

    except KeyboardInterrupt:
        logger.info("사용자에 의해 중단되었습니다. --resume으로 재개할 수 있습니다.")
        return 1

    except Exception as e:
        logger.error("크롤링 중 치명적 오류 발생: %s", e)
        import traceback
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
