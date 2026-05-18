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
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
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
    # status: "draft" | "published"
    status: Mapped[str] = mapped_column(String(20), default="published")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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


class DealCache(Base):
    __tablename__ = "deal_cache"

    deal_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    operator_name: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))
    deal_date: Mapped[datetime | None] = mapped_column(DateTime)
    stage: Mapped[str | None] = mapped_column(String(200))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
