from sqlalchemy import JSON, BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class EducationPreference(Base):
    __tablename__ = "education_preferences"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    values: Mapped[dict] = mapped_column(JSON)


class EducationSignal(Base):
    __tablename__ = "education_signals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
