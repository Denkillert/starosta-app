from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import (
    AttendanceStatus,
    DocStatus,
    MemberStatus,
    OccurrenceStatus,
    Role,
)
from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Group(Base, TimestampMixin):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    university: Mapped[str] = mapped_column(String(160), default="ПолесГУ", nullable=False)
    invite_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    current_term: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    lessons: Mapped[list[Lesson]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class Membership(Base, TimestampMixin):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "group_id", name="uq_membership_user_group"),
        Index("ix_membership_group_status", "group_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(16), default=Role.STUDENT, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=MemberStatus.ACTIVE, nullable=False
    )
    student_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Форма обучения: budget (бюджет) или paid (платно).
    funding: Mapped[str] = mapped_column(String(16), default="budget", nullable=False)
    # Подгруппа для лабораторных: 1, 2 или NULL (не указана).
    subgroup: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # TRUE — участник вступил по коду приглашения, но староста ещё не проверил
    # ФИО/подгруппу. Такие заявки показываются отдельным блоком.
    needs_review: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    group: Mapped[Group] = relationship(back_populates="memberships")
    user: Mapped[User | None] = relationship(back_populates="memberships")
    attendance: Mapped[list[Attendance]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )

    @property
    def is_staff(self) -> bool:
        return self.role in Role.STAFF


document_students = Table(
    "document_students",
    Base.metadata,
    Column(
        "document_id",
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "membership_id",
        ForeignKey("memberships.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Subject(Base, TimestampMixin):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    lessons: Mapped[list[Lesson]] = relationship(back_populates="subject")


class Lesson(Base, TimestampMixin):
    """Шаблон пары: «каждый вторник 10:05–11:25, недели 2, 4, 6…»."""

    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint("group_id", "term", "source_key", name="uq_lesson_source"),
        Index("ix_lesson_group_term", "group_id", "term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    term: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = Пн
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    room: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    teacher: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    subgroup: Mapped[str | None] = mapped_column(String(32), nullable=True)
    weeks: Mapped[list[int]] = mapped_column(JSON, default=list, nullable=False)
    source_key: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    group: Mapped[Group] = relationship(back_populates="lessons")
    subject: Mapped[Subject] = relationship(back_populates="lessons")
    occurrences: Mapped[list[LessonOccurrence]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )


class LessonOccurrence(Base, TimestampMixin):
    """Конкретная дата пары. Здесь же ручные отмены и переносы."""

    __tablename__ = "lesson_occurrences"
    __table_args__ = (
        UniqueConstraint("lesson_id", "date", name="uq_occurrence_lesson_date"),
        Index("ix_occurrence_group_date", "group_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=OccurrenceStatus.SCHEDULED, nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    moved_to_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    lesson: Mapped[Lesson] = relationship(back_populates="occurrences")
    attendance: Mapped[list[Attendance]] = relationship(
        back_populates="occurrence", cascade="all, delete-orphan"
    )


class Document(Base, TimestampMixin):
    """Распоряжение, заявление или справка — единая сущность."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_document_group_type_status", "group_id", "type", "status"),
        Index("ix_document_student", "student_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=DocStatus.DRAFT, nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    student_id: Mapped[int | None] = mapped_column(
        ForeignKey("memberships.id", ondelete="SET NULL"), nullable=True
    )
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[date | None] = mapped_column(Date, default=date.today, nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Время начала/окончания освобождения — чтобы распоряжение,
    # выданное в середине дня, не покрывало уже прошедшие пары.
    valid_from_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    valid_to_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lesson_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    group: Mapped[Group] = relationship(back_populates="documents")
    student: Mapped[Membership | None] = relationship(foreign_keys=[student_id])
    students: Mapped[list[Membership]] = relationship(
        secondary=document_students,
        lazy="selectin",
    )
    author: Mapped[User | None] = relationship(foreign_keys=[author_id])


class Attendance(Base, TimestampMixin):
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("occurrence_id", "member_id", name="uq_attendance_occ_member"),
        Index("ix_attendance_member", "member_id"),
        Index("ix_attendance_document", "document_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurrence_id: Mapped[int] = mapped_column(
        ForeignKey("lesson_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    member_id: Mapped[int] = mapped_column(
        ForeignKey("memberships.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default=AttendanceStatus.NOT_ANSWERED, nullable=False
    )
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="head", nullable=False)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    # Какой статус был до автоматической «уважительной» по документу.
    previous_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    marked_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    marked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    occurrence: Mapped[LessonOccurrence] = relationship(back_populates="attendance")
    member: Mapped[Membership] = relationship(back_populates="attendance")


class ScheduleSnapshot(Base, TimestampMixin):
    __tablename__ = "schedule_snapshots"
    __table_args__ = (Index("ix_snapshot_group_term", "group_id", "term"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    content_hash: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    lessons_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    weeks_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="ok", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_group_created", "group_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
