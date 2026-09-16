"""Parent identity and multi-child conversation context resolution."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from academics.models import Student
from communications.models import ConversationSession, ParentContact
from communications.services.twilio_service import (
    is_sandbox_peer_id,
    normalize_whatsapp_identity,
)


def normalize_incoming_phone(incoming_phone: str) -> str:
    """Normalize Twilio WhatsApp From (E.164 or sandbox peer id)."""
    return normalize_whatsapp_identity(incoming_phone)


@transaction.atomic
def resolve_parent_identity(school, incoming_phone: str) -> ParentContact:
    """
    Resolve or create a ParentContact for this school + WhatsApp identity.

    Incoming may be E.164 (+254…) or a Twilio Sandbox peer id (KE.…).
    """
    identity = normalize_incoming_phone(incoming_phone)

    if is_sandbox_peer_id(identity):
        contact = (
            ParentContact.objects.select_for_update()
            .filter(school=school, whatsapp_peer_id__iexact=identity)
            .first()
        )
        if contact is not None:
            return contact
        # Fall through: create a peer-linked contact without inventing an E.164.
        # Roster linking still happens via prepare_agent_test / admission flow.
        return ParentContact.objects.create(
            school=school,
            # Placeholder unique E.164-ish token so validator passes; not used for WA.
            # Use a synthetic reserved range that won't match real roster phones.
            phone_number=_synthetic_phone_for_peer(identity),
            whatsapp_peer_id=identity,
            parent_name='',
            is_verified=False,
        )

    contact = (
        ParentContact.objects.select_for_update()
        .filter(school=school, phone_number=identity)
        .first()
    )
    if contact is not None:
        return contact

    students = list(
        Student.objects.filter(school=school, parent_phone=identity).order_by(
            'admission_number'
        )
    )

    if students:
        parent_name = (students[0].parent_name or '').strip()
        contact = ParentContact.objects.create(
            school=school,
            phone_number=identity,
            parent_name=parent_name,
            is_verified=True,
        )
        contact.students.set(students)
        return contact

    return ParentContact.objects.create(
        school=school,
        phone_number=identity,
        parent_name='',
        is_verified=False,
    )


def _synthetic_phone_for_peer(peer_id: str) -> str:
    """
    Build a unique synthetic E.164 for sandbox-only ParentContacts.

    Real M-Pesa/STK must use the roster parent_phone on the student, not this.
    """
    digits = ''.join(ch for ch in peer_id if ch.isdigit())[-12:].rjust(12, '0')
    # +299 is unused in real telephony — marks sandbox-only contacts.
    return f'+299{digits}'


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


def _admission_confirm_prompt(school) -> str:
    return (
        f'Welcome to *{school.name}*.\n\n'
        'For security, please reply with the student *admission number* '
        'you are asking about.\n\n'
        'We only continue once it matches a student linked to this WhatsApp number.'
    )


def _admission_mismatch_prompt() -> str:
    return (
        'That admission number is not linked to this WhatsApp number. '
        'Please check the number on the school roster and try again, '
        'or reply *talk to the school* if you need help.'
    )


def _unknown_parent_prompt(school) -> str:
    return (
        f'Welcome to *{school.name}*. We could not find a student linked to this '
        'phone number. Please reply with the Student Admission Number. '
        'Linking only works when this WhatsApp number matches the parent phone '
        'on the school roster.'
    )


def _looks_like_admission_attempt(message_body: str) -> bool:
    """True when the message is a single token that could be an admission number."""
    text = (message_body or '').strip()
    if not text or len(text) > 40:
        return False
    if any(ch.isspace() for ch in text):
        return False
    return True


def _parse_child_menu_choice(message_body: str, count: int) -> int | None:
    """1-based index when body is a bare digit selecting a child (multi-student reminder)."""
    text = (message_body or '').strip()
    if not text.isdigit():
        return None
    choice = int(text)
    if 1 <= choice <= count:
        return choice
    return None


def _match_linked_admission(
    students: list[Student],
    message_body: str,
) -> Student | None:
    """Match body to a linked student's admission number (exact, case-insensitive)."""
    admission = (message_body or '').strip()
    if not admission:
        return None
    needle = admission.casefold()
    for student in students:
        if (student.admission_number or '').strip().casefold() == needle:
            return student
    return None


def confirm_session_admission(
    session: ConversationSession,
    student: Student,
) -> None:
    """Lock active student and mark admission confirmed on this session."""
    session.active_student = student
    session.admission_confirmed_at = timezone.now()
    session.save(
        update_fields=['active_student', 'admission_confirmed_at', 'last_message_at']
    )


def clear_session_admission(session: ConversationSession) -> None:
    """Clear active student + confirmation (e.g. unlinked parent)."""
    session.active_student = None
    session.admission_confirmed_at = None
    session.save(
        update_fields=['active_student', 'admission_confirmed_at', 'last_message_at']
    )


def _roster_phones_match(whatsapp_phone: str, roster_phone: str) -> bool:
    a = ''.join(ch for ch in (whatsapp_phone or '') if ch.isdigit())
    b = ''.join(ch for ch in (roster_phone or '') if ch.isdigit())
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 9 and len(b) >= 9 and a[-9:] == b[-9:]


def try_link_by_admission_number(
    school,
    parent_contact: ParentContact,
    message_body: str,
) -> Student | None:
    """
    Link a parent contact to a student when they reply with an admission number.

    Only succeeds when the WhatsApp number matches the student's roster
    parent_phone (normalized). Admission number alone is not enough.
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

    if not _roster_phones_match(parent_contact.phone_number, student.parent_phone):
        return None

    parent_contact.students.add(student)
    updates = ['is_verified', 'updated_at']
    parent_contact.is_verified = True
    if not (parent_contact.parent_name or '').strip() and student.parent_name:
        parent_contact.parent_name = student.parent_name.strip()
        updates.append('parent_name')
    parent_contact.save(update_fields=updates)
    return student


@dataclass
class IdentityResolution:
    """Outcome of inbound parent identity + session resolution."""

    parent_contact: ParentContact
    session: ConversationSession
    linked_students: list = field(default_factory=list)
    active_student: Student | None = None
    reply_message: str | None = None
    can_proceed: bool = False
    case: str = ''  # A | B | C | confirm


@transaction.atomic
def resolve_conversation_context(
    school,
    incoming_phone: str,
    message_body: str = '',
) -> IdentityResolution:
    """
    Resolve parent contact, open session, and confirmed active student.

    Phone match alone is not enough to discuss balances. After students are
    linked to the WhatsApp number, the parent must reply with a matching
    admission number before the agent proceeds (does not list names first).

    Cases:
      A — no linked students → prompt for admission (cannot proceed)
      confirm — linked but admission not confirmed → ask / verify adm
      B/C — admission confirmed for active student → can proceed
    """
    parent_contact = resolve_parent_identity(school, incoming_phone)
    session = get_or_create_active_session(school, parent_contact)

    students = list(
        parent_contact.students.filter(school=school, is_active=True).order_by(
            'admission_number'
        )
    )
    linked_ids = {s.pk for s in students}

    # Case A: unknown / unlinked parent
    if len(students) == 0:
        if session.active_student_id or session.admission_confirmed_at:
            clear_session_admission(session)
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=[],
            active_student=None,
            reply_message=_unknown_parent_prompt(school),
            can_proceed=False,
            case='A',
        )

    # Already confirmed for a still-linked student — allow switch via new adm #.
    if (
        session.admission_confirmed_at
        and session.active_student_id
        and session.active_student_id in linked_ids
    ):
        switch = _match_linked_admission(students, message_body)
        if switch is not None and switch.pk != session.active_student_id:
            confirm_session_admission(session, switch)
            return IdentityResolution(
                parent_contact=parent_contact,
                session=session,
                linked_students=students,
                active_student=switch,
                reply_message=None,
                can_proceed=True,
                case='C' if len(students) > 1 else 'B',
            )
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=students,
            active_student=session.active_student,
            reply_message=None,
            can_proceed=True,
            case='C' if len(students) > 1 else 'B',
        )

    # Stale confirmation / student no longer linked
    if session.active_student_id and session.active_student_id not in linked_ids:
        clear_session_admission(session)

    # Phone is linked — require admission confirmation (no name leak on cold inbound).
    # After a multi-child reminder, a bare 1..N also selects that child.
    matched = _match_linked_admission(students, message_body)
    if matched is None and len(students) > 1:
        choice = _parse_child_menu_choice(message_body, len(students))
        if choice is not None:
            matched = students[choice - 1]
    if matched is not None:
        confirm_session_admission(session, matched)
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=students,
            active_student=matched,
            reply_message=None,
            can_proceed=True,
            case='confirm',
        )

    body = (message_body or '').strip()
    if body and _looks_like_admission_attempt(body):
        return IdentityResolution(
            parent_contact=parent_contact,
            session=session,
            linked_students=students,
            active_student=None,
            reply_message=_admission_mismatch_prompt(),
            can_proceed=False,
            case='confirm',
        )

    return IdentityResolution(
        parent_contact=parent_contact,
        session=session,
        linked_students=students,
        active_student=None,
        reply_message=_admission_confirm_prompt(school),
        can_proceed=False,
        case='confirm',
    )
