from __future__ import annotations


class Role:
    HEAD = "head"
    DEPUTY = "deputy"
    STUDENT = "student"

    ALL = (HEAD, DEPUTY, STUDENT)
    STAFF = (HEAD, DEPUTY)
    LABELS = {HEAD: "Староста", DEPUTY: "Заместитель", STUDENT: "Студент"}


class MemberStatus:
    ACTIVE = "active"
    LEFT = "left"

    LABELS = {ACTIVE: "В группе", LEFT: "Выбыл"}


class Funding:
    BUDGET = "budget"
    PAID = "paid"

    ALL = (BUDGET, PAID)
    LABELS = {BUDGET: "Бюджет", PAID: "Платно"}
    SHORT = {BUDGET: "Б", PAID: "П"}


class AttendanceStatus:
    PRESENT = "present"
    LATE = "late"
    ABSENT = "absent"
    EXCUSED = "excused"
    NOT_ANSWERED = "not_answered"

    ALL = (PRESENT, LATE, ABSENT, EXCUSED, NOT_ANSWERED)
    LABELS = {
        PRESENT: "Был",
        LATE: "Опоздал",
        ABSENT: "Не был",
        EXCUSED: "Уважительная",
        NOT_ANSWERED: "Не ответил",
    }


class OccurrenceStatus:
    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    MOVED = "moved"

    LABELS = {SCHEDULED: "По расписанию", CANCELLED: "Отменена", MOVED: "Перенесена"}


class DocType:
    ORDER = "order"
    STATEMENT = "statement"
    CERTIFICATE = "certificate"

    ALL = (ORDER, STATEMENT, CERTIFICATE)
    LABELS = {
        ORDER: "Распоряжение",
        STATEMENT: "Заявление",
        CERTIFICATE: "Справка",
    }


class DocStatus:
    DRAFT = "draft"
    ACTIVE = "active"
    REVOKED = "revoked"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"

    ALL = (DRAFT, ACTIVE, REVOKED, PENDING, APPROVED, REJECTED, ARCHIVED)
    LABELS = {
        DRAFT: "Черновик",
        ACTIVE: "Действует",
        REVOKED: "Отозван",
        PENDING: "На рассмотрении",
        APPROVED: "Одобрено",
        REJECTED: "Отклонено",
        ARCHIVED: "В архиве",
    }
