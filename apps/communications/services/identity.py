"""Parent identity and multi-child conversation context resolution."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from academics.models import Student
from communications.models import ConversationSession, ParentContact
from communications.services.twilio_service import _normalize_e164


def normalize_incoming_phone(incoming_phone: str) -> str:
    """Strip whatsapp: prefix and normalize to E.164 (+254…)."""
    return _normalize_e164(incoming_phone)


@transaction.atomic
def resolve_parent_identity(school, incoming_phone: str) -> ParentContact:
    """
    Resolve or create a ParentContact for this school + phone.

    If unknown, auto-link students that already list this parent_phone.
    """
    phone = normalize_incoming_phone(incoming_phone)

    contact = (
        ParentContact.objects.select_for_update()
        .filter(school=school, phone_number=phone)
        .first()
    )
    if contact is not None:
        return contact

    students = list(
        Student.objects.filter(school=school, parent_phone=phone).order_by(
            'admission_number'
        )
    )

    if students:
        parent_name = (students[0].parent_name or '').strip()
        contact = ParentContact.objects.create(
            school=school,
            phone_number=phone,
            parent_name=parent_name,
            is_verified=True,
        )
        contact.students.set(students)
        return contact

    return ParentContact.objects.create(
        school=school,
        phone_number=phone,
        parent_name='',
        is_verified=False,
    )


def get_or_create_active_session(school, parent_contact: ParentContact) -> ConversationSession:
    """Fetch the open session for this parent, or start a new BOT_ACTIVE one."""
    session = (
        ConversationSession.objects.filter(
            school=school,
            parent_contact=parent_contact,
        )
        .exclude(status=ConversationSession.Status.CLOSED)
        .order_by('-last_message_at', '-created_at')
        .first()
    )
    if session is not None:
        return session

    return ConversationSession.objects.create(
        school=school,
        parent_contact=parent_contact,
        status=ConversationSession.Status.BOT_ACTIVE,
    )


def _sibling_selection_prompt(students: list[Student]) -> str:
    lines = [
        'Hello! Please select which student you are inquiring about:',
    ]
    for index, student in enumerate(students, start=1):
        lines.append(
            f'{index}. {student.full_name} (Adm: {student.admission_number})'
        )
    return '\n'.join(lines)


def _unknown_parent_prompt(school) -> str:
    return (
        f'Welcome to {school.name}. We could not find a student linked to this '
        'phone number. Please reply with the Student Admission Number to link '
        'your account.'
    )


def _parse_sibling_choice(message_body: str, count: int) -> int | None:
    """Return 1-based index if body is a bare sibling choice, else None."""
    text = (message_body or '').strip()
    if not text.isdigit():
        return None
    choice = int(text)
    if 1 <= choice <= count:
        return choice
    return None


@dataclass
class IdentityResolution:
    """Outcome of inbound parent identity + session resolution."""

    parent_contact: ParentContact
    session: ConversationSession
    linked_students: list = field(default_factory=list)
    active_student: Student | None = None
    reply_message: str | None = None
    can_proceed: bool = False
    case: str = ''  # A | B | C


def try_link_by_admission_number(
    school,
    parent_contact: ParentContact,
    message_body: str,
) -> Student | None:
    """
    Link a parent contact to a student when they reply with an admission number.
    """
    admission = (message_body or '').strip()
    if not admission:
        return None

    student = (
        Student.objects.filter(school=school, admission_number__iexact=admission)
        .order_by('admission_number')
        .first()
    )
    if student is None:
        return None

    parent_contact.students.add(student)
    updates = ['is_verified', 'updated_at']
    parent_contact.is_verified = True
    if not (parent_contact.parent_name or '').strip() and student.parent_name:
        parent_contact.parent_name = student.parent_name.strip()
        updates.append('parent_name')
    parent_contact.save(update_fields=updates)
    return student


@transaction.atomic
def resolve_conversation_context(
    school,
    incoming_phone: str,
    message_body: str = '',
) -> IdentityResolution:
    """
    Resolve parent contact, open session, and multi-child active student.

    Cases:
      A — no linked students → prompt for admission number (cannot proceed)
      B — one student → lock active_student (can proceed)
      C — siblings → lock if selected / choosing index, else selection prompt
    """
    parent_contact = resolve_parent_identity(school, incoming_phone)
    session = get_or_create_active_session(school, parent_contact)

    students = list(
        parent_contact.students.filter(school=school).order_by('admission_number')
    )

    # Case A: unknown / unlinked parent
    if len(students) == 0:
        if session.active_student_id:
            session.active_student = None
            session.save(update_fields=['active_student', 'last_message_at'])
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=[],
            active_student=None,
            reply_message=_unknown_parent_prompt(school),
            can_proceed=False,
            case='A',
        )

    # Case B: single student
    if len(students) == 1:
        student = students[0]
        if session.active_student_id != student.pk:
            session.active_student = student
            session.save(update_fields=['active_student', 'last_message_at'])
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=students,
            active_student=student,
            reply_message=None,
            can_proceed=True,
            case='B',
        )

    # Case C: multiple students (siblings)
    if session.active_student_id:
        # Ensure locked student is still one of the linked siblings
        linked_ids = {s.pk for s in students}
        if session.active_student_id in linked_ids:
            return IdentityResolution(
                parent_contact=parent_contact,
                session=session,
                linked_students=students,
                active_student=session.active_student,
                reply_message=None,
                can_proceed=True,
                case='C',
            )
        session.active_student = None
        session.save(update_fields=['active_student', 'last_message_at'])

    choice = _parse_sibling_choice(message_body, len(students))
    if choice is not None:
        student = students[choice - 1]
        session.active_student = student
        session.save(update_fields=['active_student', 'last_message_at'])
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=students,
            active_student=student,
            reply_message=None,
            can_proceed=True,
            case='C',
        )

    return IdentityResolution(
        parent_contact=parent_contact,
        session=session,
        linked_students=students,
        active_student=None,
        reply_message=_sibling_selection_prompt(students),
        can_proceed=False,
        case='C',
    )
