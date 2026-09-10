"""Helpers for staff invitations and user notifications."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from tenants.models import Notification, SchoolMembership, StaffInvitation

User = get_user_model()


def notify_user(*, user, title, body='', kind=Notification.Kind.NOTICE, school=None, link=''):
    return Notification.objects.create(
        user=user,
        school=school,
        kind=kind,
        title=title,
        body=body,
        link=link or '',
    )


def notify_school_admins(*, school, title, body='', link='', exclude_user=None):
    admin_ids = SchoolMembership.objects.filter(
        school=school,
        is_admin=True,
        user__is_active=True,
    ).values_list('user_id', flat=True)
    users = User.objects.filter(id__in=admin_ids)
    if exclude_user is not None:
        users = users.exclude(pk=exclude_user.pk)
    for user in users:
        notify_user(
            user=user,
            school=school,
            kind=Notification.Kind.INVITATION_RESPONSE,
            title=title,
            body=body,
            link=link,
        )


def notify_invitee_of_invitation(invitation):
    invitee = User.objects.filter(email__iexact=invitation.email).first()
    if invitee is None:
        return None
    return notify_user(
        user=invitee,
        school=invitation.school,
        kind=Notification.Kind.INVITATION,
        title=f'Invitation to join {invitation.school.name}',
        body='You have a pending staff invitation. Open your inbox to accept or decline.',
        link=reverse('tenants:inbox'),
    )


@transaction.atomic
def accept_invitation(*, invitation, user):
    if user.email.lower() != invitation.email.lower():
        raise ValueError('This invitation was sent to a different email address.')
    if invitation.status != StaffInvitation.Status.PENDING:
        raise ValueError('This invitation is no longer pending.')
    if invitation.is_expired:
        raise ValueError('This invitation has expired.')

    membership, created = SchoolMembership.objects.get_or_create(
        user=user,
        school=invitation.school,
        defaults={
            'is_admin': invitation.role_admin,
            'is_teacher': invitation.role_teacher,
        },
    )
    if not created:
        membership.is_admin = membership.is_admin or invitation.role_admin
        membership.is_teacher = membership.is_teacher or invitation.role_teacher
        membership.full_clean()
        membership.save()

    invitation.status = StaffInvitation.Status.ACCEPTED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=['status', 'responded_at'])

    notify_school_admins(
        school=invitation.school,
        title=f'{user.email} accepted the invitation',
        body=(
            f'{user.get_full_name() or user.email} joined '
            f'{invitation.school.name}.'
        ),
        link=reverse('dashboard:teachers'),
        exclude_user=user,
    )
    return membership


@transaction.atomic
def reject_invitation(*, invitation, user):
    if user.email.lower() != invitation.email.lower():
        raise ValueError('This invitation was sent to a different email address.')
    if invitation.status != StaffInvitation.Status.PENDING:
        raise ValueError('This invitation is no longer pending.')

    invitation.status = StaffInvitation.Status.REJECTED
    invitation.responded_at = timezone.now()
    invitation.save(update_fields=['status', 'responded_at'])

    notify_school_admins(
        school=invitation.school,
        title=f'{user.email} declined the invitation',
        body=f'{user.get_full_name() or user.email} declined to join {invitation.school.name}.',
        link=reverse('dashboard:teachers'),
        exclude_user=user,
    )
    return invitation
