from datetime import datetime
from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    bitrix_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="evaluator")
    checklists: Mapped[list["Checklist"]] = relationship(back_populates="created_by_user")


class Checklist(Base):
    __tablename__ = "checklists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # status: "draft" | "active" | "archived"
    status: Mapped[str] = mapped_column(String(20), default="active")
    autofail_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # calculation: "weighted" | "average"
    calculation: Mapped[str] = mapped_column(String(20), default="weighted")
    # departments: comma-separated department names this checklist is assigned to
    departments: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    created_by_user: Mapped["User | None"] = relationship(back_populates="checklists")
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="checklist", order_by="Block.order_index", cascade="all, delete-orphan"
    )
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="checklist")


class Block(Base):
    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checklist_id: Mapped[int] = mapped_column(ForeignKey("checklists.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    weight: Mapped[int] = mapped_column(Integer, default=0)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    checklist: Mapped["Checklist"] = relationship(back_populates="blocks")
    criteria: Mapped[list["Criterion"]] = relationship(
        back_populates="block", order_by="Criterion.order_index", cascade="all, delete-orphan"
    )


class Criterion(Base):
    __tablename__ = "criteria"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    block_id: Mapped[int] = mapped_column(ForeignKey("blocks.id"), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    is_autofail: Mapped[bool] = mapped_column(Boolean, default=False)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    score_type: Mapped[str] = mapped_column(String(20), default="binary")
    score_max: Mapped[int] = mapped_column(Integer, default=5)

    block: Mapped["Block"] = relationship(back_populates="criteria")
    evaluation_items: Mapped[list["EvaluationItem"]] = relationship(back_populates="criterion")


class Evaluation(Base):
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checklist_id: Mapped[int] = mapped_column(ForeignKey("checklists.id"), nullable=False)
    deal_id: Mapped[str | None] = mapped_column(String(50))
    deal_url: Mapped[str | None] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(50))
    operator_name: Mapped[str] = mapped_column(String(200), nullable=False)
    eval_date: Mapped[datetime | None] = mapped_column(DateTime)
    week_num: Mapped[int | None] = mapped_column(Integer)
    week_year: Mapped[int | None] = mapped_column(Integer)
    month: Mapped[str | None] = mapped_column(String(20))
    department: Mapped[str | None] = mapped_column(String(200))
    # stage values: "сделка успешна" | "не смог продать" | other (in-progress)
    stage: Mapped[str | None] = mapped_column(String(200))
    total_score: Mapped[float | None] = mapped_column(Float)
    evaluator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    general_comment: Mapped[str | None] = mapped_column(Text)
    client_category: Mapped[str | None] = mapped_column(String(10))
    # status: "draft" | "published"
    status: Mapped[str] = mapped_column(String(20), default="published")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # status: "draft" | "published"
    is_calibration: Mapped[bool] = mapped_column(Boolean, default=False)

    checklist: Mapped["Checklist"] = relationship(back_populates="evaluations")
    evaluator: Mapped["User | None"] = relationship(back_populates="evaluations")
    items: Mapped[list["EvaluationItem"]] = relationship(
        back_populates="evaluation", cascade="all, delete-orphan"
    )


class EvaluationItem(Base):
    __tablename__ = "evaluation_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), nullable=False)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id"), nullable=False)
    # value: "yes" | "no" | "na"
    value: Mapped[str] = mapped_column(String(3), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)

    evaluation: Mapped["Evaluation"] = relationship(back_populates="items")
    criterion: Mapped["Criterion"] = relationship(back_populates="evaluation_items")


class CalibrationSession(Base):
    __tablename__ = "calibration_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    session_date: Mapped[datetime | None] = mapped_column(DateTime)
    source_evaluation_id: Mapped[int] = mapped_column(ForeignKey("evaluations.id"), nullable=False)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    # status: "open" | "closed"
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_evaluation: Mapped["Evaluation"] = relationship(foreign_keys=[source_evaluation_id])
    created_by: Mapped["User | None"] = relationship(foreign_keys=[created_by_id])
    participants: Mapped[list["CalibrationParticipant"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    resolutions: Mapped[list["CalibrationItemResolution"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class CalibrationParticipant(Base):
    __tablename__ = "calibration_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("calibration_sessions.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    total_score: Mapped[float | None] = mapped_column(Float)
    general_comment: Mapped[str | None] = mapped_column(Text)
    # status: "pending" | "completed"
    status: Mapped[str] = mapped_column(String(20), default="pending")
    invited_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    session: Mapped["CalibrationSession"] = relationship(back_populates="participants")
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    answers: Mapped[list["CalibrationAnswerItem"]] = relationship(
        back_populates="participant", cascade="all, delete-orphan"
    )


class CalibrationAnswerItem(Base):
    __tablename__ = "calibration_answer_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("calibration_participants.id"), nullable=False)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id"), nullable=False)
    # value: "yes" | "no" | "na"
    value: Mapped[str] = mapped_column(String(3), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)

    participant: Mapped["CalibrationParticipant"] = relationship(back_populates="answers")
    criterion: Mapped["Criterion"] = relationship()


class CalibrationItemResolution(Base):
    __tablename__ = "calibration_item_resolutions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("calibration_sessions.id"), nullable=False)
    criterion_id: Mapped[int] = mapped_column(ForeignKey("criteria.id"), nullable=False)
    # final_value: "yes" | "no" | "na"
    final_value: Mapped[str | None] = mapped_column(String(3))
    comment: Mapped[str | None] = mapped_column(Text)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    session: Mapped["CalibrationSession"] = relationship(back_populates="resolutions")
    criterion: Mapped["Criterion"] = relationship()
    resolved_by: Mapped["User | None"] = relationship(foreign_keys=[resolved_by_id])


class EvaluationTarget(Base):
    __tablename__ = "evaluation_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    department: Mapped[str | None] = mapped_column(String(200))
    checklist_id: Mapped[int | None] = mapped_column(ForeignKey("checklists.id"))
    target_per_employee: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class DealCache(Base):
    __tablename__ = "deal_cache"

    deal_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    operator_name: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))
    deal_date: Mapped[datetime | None] = mapped_column(DateTime)
    presentation_date: Mapped[datetime | None] = mapped_column(DateTime)
    close_date: Mapped[datetime | None] = mapped_column(DateTime)   # CLOSEDATE из Битрикс
    stage: Mapped[str | None] = mapped_column(String(200))
    client_category: Mapped[str | None] = mapped_column(String(10))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
