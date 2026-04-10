"""
PostgreSQL 접속 설정 모듈

.env 파일에서 DATABASE_URL을 로드하고 SQLAlchemy 엔진/세션을 생성합니다.

사용법:
    from src.db_config import engine, SessionLocal, init_db

    # 테이블 생성 (최초 1회)
    init_db()

    # 세션 사용
    with SessionLocal() as session:
        session.add(...)
        session.commit()
"""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

# .env 파일 로드 (프로젝트 루트 기준)
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise EnvironmentError(
        "DATABASE_URL 환경변수가 설정되지 않았습니다. "
        ".env 파일을 확인하세요. (.env.example 참조)"
    )

# SQLAlchemy 엔진 생성
# pool_pre_ping=True: 연결 유효성을 자동으로 확인
engine = create_engine(DATABASE_URL, pool_pre_ping=True, echo=False)

# 세션 팩토리 생성
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    """모든 ORM 모델의 기반 클래스"""
    pass


def init_db() -> None:
    """DB 테이블을 생성합니다 (이미 존재하면 건너뜀)."""
    # db_models 임포트 시 모든 테이블 정의가 Base.metadata에 등록됨
    from src import db_models  # noqa: F401
    Base.metadata.create_all(bind=engine)


def drop_and_recreate_db() -> None:
    """기존 테이블을 모두 삭제하고 재생성합니다. --drop 옵션 전용."""
    from src import db_models  # noqa: F401
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
