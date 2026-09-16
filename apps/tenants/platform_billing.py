"""Kora platform (SaaS) billing: tiers, day-14 census, true-up, credits."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from tenants.models import (
    PlatformBillingSettings,
    PlatformInvoice,
    School,
    SchoolSubscription,
    SchoolTermPeriod,
)

logger = logging.getLogger(__name__)

TWOPLACES = Decimal('0.01')


def _q(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TierQuote:
    tier: str
    rate: Decimal
    headcount: int
    raw_subtotal: Decimal
    floor_applied: Decimal
    full_amount: Decimal
    proration_factor: Decimal
    amount_due: Decimal


@dataclass(frozen=True)
class TrueUpPreview:
    locked_headcount: int
    live_headcount: int
    net_delta: int
    rate: Decimal
    factor: Decimal
    amount: Decimal
    action: str  # none | charge | credit
    ignored: bool
    reason: str


def active_student_count(school: School) -> int:
    from academics.models import Student

    return Student.objects.filter(school=school, is_active=True).count()


def resolve_tier(
    headcount: int,
    settings: PlatformBillingSettings | None = None,
) -> tuple[str, Decimal, Decimal]:
    cfg = settings or PlatformBillingSettings.load()
    n = max(0, int(headcount or 0))
    if n <= cfg.starter_max_students:
        return PlatformInvoice.Tier.STARTER, cfg.starter_rate, cfg.starter_floor
    if n <= cfg.growth_max_students:
        return PlatformInvoice.Tier.GROWTH, cfg.growth_rate, Decimal('0')
    return PlatformInvoice.Tier.SCALE, cfg.scale_rate, Decimal('0')


def quote_amount(
    *,
    headcount: int,
    proration_factor: Decimal = Decimal('1'),
    settings: PlatformBillingSettings | None = None,
) -> TierQuote:
    cfg = settings or PlatformBillingSettings.load()
    tier, rate, floor = resolve_tier(headcount, cfg)
    n = max(0, int(headcount or 0))
    raw = _q(rate * Decimal(n))
    full = max(raw, floor) if floor else raw
    floor_applied = full - raw if full > raw else Decimal('0')
    factor = Decimal(str(proration_factor))
    if factor < 0:
        factor = Decimal('0')
    if factor > 1:
        factor = Decimal('1')
    amount = _q(full * factor)
    return TierQuote(
        tier=tier,
        rate=rate,
        headcount=n,
        raw_subtotal=raw,
        floor_applied=_q(floor_applied),
        full_amount=_q(full),
        proration_factor=factor,
        amount_due=amount,
    )


def current_term_for_school(school: School, on: date | None = None) -> SchoolTermPeriod | None:
    day = on or timezone.localdate()
    return (
        SchoolTermPeriod.objects.filter(
            school=school,
            start_date__lte=day,
            end_date__gte=day,
        )
        .order_by('start_date')
        .first()
    )


def next_term_after(school: School, after: date) -> SchoolTermPeriod | None:
    return (
        SchoolTermPeriod.objects.filter(school=school, start_date__gt=after)
        .order_by('start_date')
        .first()
    )


def compute_proration_factor(
    term: SchoolTermPeriod,
    *,
    as_of: date,
    settings: PlatformBillingSettings | None = None,
) -> Decimal:
    cfg = settings or PlatformBillingSettings.load()
    total = term.duration_days
    if total <= 0:
        return Decimal('1')
    if as_of <= term.start_date:
        return Decimal('1')
    if as_of > term.end_date:
        return Decimal('0')
    remaining = (term.end_date - as_of).days + 1
    factor = Decimal(remaining) / Decimal(total)
    min_f = cfg.min_proration_factor
    if factor < min_f:
        factor = min_f
    if factor > 1:
        factor = Decimal('1')
    return factor.quantize(Decimal('0.0001'))


def _census_lock_date_for_term(
    term: SchoolTermPeriod,
    *,
    settings: PlatformBillingSettings,
    invoice_as_of: date,
) -> date:
    """Day-14 of term, or immediate lock if the invoice starts after that day."""
    planned = term.start_date + timedelta(days=settings.term_half_due_days)
    if planned > term.end_date:
        planned = term.end_date
    if invoice_as_of >= planned:
        return invoice_as_of
    return planned


def _billable_headcount(school: School) -> int:
    headcount = active_student_count(school)
    if headcount <= 0:
        return max(1, school.estimated_student_count or 1)
    return headcount


def _apply_credit_to_amount(
    school: School,
    amount: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return (amount_after_credit, credit_used). Mutates subscription credit."""
    try:
        sub = school.subscription
    except SchoolSubscription.DoesNotExist:
        return amount, Decimal('0')
    credit = _q(sub.credit_balance or Decimal('0'))
    if credit <= 0 or amount <= 0:
        return amount, Decimal('0')
    used = min(credit, amount)
    sub.credit_balance = _q(credit - used)
    sub.save(update_fields=['credit_balance', 'updated_at'])
    return _q(amount - used), used


@transaction.atomic
def approve_school(school: School, *, reviewer) -> SchoolSubscription:
    cfg = PlatformBillingSettings.load()
    now = timezone.now()
    school.status = School.Status.APPROVED
    school.is_active = True
    school.reviewed_at = now
    school.reviewed_by = reviewer
    school.rejection_reason = ''
    school.save(
        update_fields=[
            'status',
            'is_active',
            'reviewed_at',
            'reviewed_by',
            'rejection_reason',
            'updated_at',
        ]
    )
    trial_end = now + timedelta(days=cfg.trial_days)
    sub, _created = SchoolSubscription.objects.update_or_create(
        school=school,
        defaults={
            'billing_state': SchoolSubscription.BillingState.TRIAL,
            'trial_started_at': now,
            'trial_ends_at': trial_end,
            'locked_at': None,
            'lock_reason': '',
        },
    )
    return sub


@transaction.atomic
def reject_school(school: School, *, reviewer, reason: str) -> None:
    school.status = School.Status.REJECTED
    school.is_active = False
    school.reviewed_at = timezone.now()
    school.reviewed_by = reviewer
    school.rejection_reason = (reason or '').strip()
    school.save(
        update_fields=[
            'status',
            'is_active',
            'reviewed_at',
            'reviewed_by',
            'rejection_reason',
            'updated_at',
        ]
    )


def _ensure_invoice_for_term(
    school: School,
    term: SchoolTermPeriod,
    *,
    as_of: date,
    prorate: bool,
    is_first: bool,
    settings: PlatformBillingSettings,
) -> PlatformInvoice | None:
    existing = (
        PlatformInvoice.objects.filter(
            school=school,
            term_period=term,
            kind=PlatformInvoice.Kind.MAIN,
        )
        .exclude(status=PlatformInvoice.Status.VOID)
        .first()
    )
    if existing is not None:
        return existing

    factor = (
        compute_proration_factor(term, as_of=as_of, settings=settings)
        if prorate
        else Decimal('1')
    )
    if factor <= 0:
        return None

    headcount = _billable_headcount(school)
    quote = quote_amount(
        headcount=headcount,
        proration_factor=factor,
        settings=settings,
    )
    gross = quote.amount_due
    net, credit_used = _apply_credit_to_amount(school, gross)
    census_lock = _census_lock_date_for_term(
        term,
        settings=settings,
        invoice_as_of=as_of,
    )
    lock_now = as_of >= census_lock
    full_span = max(
        1,
        int(settings.term_full_due_days) - int(settings.term_half_due_days),
    )
    due_full = census_lock + timedelta(days=full_span)
    notes = []
    if prorate and factor < 1:
        notes.append('First term prorated for mid-term start.')
    if credit_used > 0:
        notes.append(f'Applied KSh {credit_used} subscription credit.')
    if lock_now:
        notes.append('Census locked at issue (invoice started on/after day 14).')
    else:
        notes.append(
            f'Provisional until census lock on {census_lock.isoformat()} '
            f'(day {settings.term_half_due_days} of term).'
        )

    invoice = PlatformInvoice.objects.create(
        school=school,
        term_period=term,
        kind=PlatformInvoice.Kind.MAIN,
        term_label=term.label,
        headcount=quote.headcount,
        tier=quote.tier,
        rate_per_student=quote.rate,
        subtotal=quote.raw_subtotal,
        floor_applied=quote.floor_applied,
        proration_factor=quote.proration_factor,
        amount_due=net,
        amount_paid=Decimal('0'),
        credit_applied=credit_used,
        due_half_at=census_lock,
        due_full_at=due_full,
        census_status=(
            PlatformInvoice.CensusStatus.LOCKED
            if lock_now
            else PlatformInvoice.CensusStatus.PROVISIONAL
        ),
        census_lock_date=census_lock,
        census_locked_at=timezone.now() if lock_now else None,
        status=PlatformInvoice.Status.OPEN,
        is_first_billable=is_first,
        notes=' '.join(notes),
    )
    if net <= 0:
        invoice.amount_due = Decimal('0')
        invoice.amount_paid = Decimal('0')
        invoice.status = PlatformInvoice.Status.PAID
        invoice.paid_at = timezone.now()
        invoice.save(
            update_fields=['amount_due', 'amount_paid', 'status', 'paid_at', 'updated_at']
        )
    return invoice


def create_post_trial_invoice(school: School, *, as_of: date | None = None) -> PlatformInvoice | None:
    cfg = PlatformBillingSettings.load()
    day = as_of or timezone.localdate()
    current = current_term_for_school(school, day)
    if current is None:
        nxt = next_term_after(school, day)
        if nxt is None:
            logger.warning('No term calendar for school=%s; cannot invoice', school.id)
            return None
        return _ensure_invoice_for_term(
            school,
            nxt,
            as_of=nxt.start_date,
            prorate=False,
            is_first=True,
            settings=cfg,
        )

    remaining = (current.end_date - day).days + 1
    if remaining < cfg.roll_into_next_term_days:
        nxt = next_term_after(school, current.end_date)
        if nxt is not None:
            return _ensure_invoice_for_term(
                school,
                nxt,
                as_of=nxt.start_date,
                prorate=False,
                is_first=True,
                settings=cfg,
            )

    return _ensure_invoice_for_term(
        school,
        current,
        as_of=day,
        prorate=True,
        is_first=True,
        settings=cfg,
    )


def ensure_open_term_invoice(school: School, *, as_of: date | None = None) -> PlatformInvoice | None:
    cfg = PlatformBillingSettings.load()
    day = as_of or timezone.localdate()
    term = current_term_for_school(school, day)
    if term is None:
        return None
    has_any = PlatformInvoice.objects.filter(
        school=school,
        kind=PlatformInvoice.Kind.MAIN,
    ).exclude(status=PlatformInvoice.Status.VOID).exists()
    return _ensure_invoice_for_term(
        school,
        term,
        as_of=term.start_date,
        prorate=False,
        is_first=not has_any,
        settings=cfg,
    )


@transaction.atomic
def lock_census_if_due(
    invoice: PlatformInvoice,
    *,
    as_of: date | None = None,
) -> PlatformInvoice:
    """
    On/after census_lock_date, freeze headcount + amount from active students.
    Uses the same tier/rate rules as issue time, with the locked headcount.
    """
    if invoice.kind != PlatformInvoice.Kind.MAIN:
        return invoice
    if invoice.census_status == PlatformInvoice.CensusStatus.LOCKED:
        return invoice
    if invoice.status == PlatformInvoice.Status.VOID:
        return invoice

    day = as_of or timezone.localdate()
    lock_day = invoice.census_lock_date
    if lock_day is None or day < lock_day:
        return invoice

    cfg = PlatformBillingSettings.load()
    headcount = _billable_headcount(invoice.school)
    quote = quote_amount(
        headcount=headcount,
        proration_factor=invoice.proration_factor,
        settings=cfg,
    )
    # Preserve credit already applied; only recompute gross then subtract prior credit.
    gross = quote.amount_due
    prior_credit = _q(invoice.credit_applied or Decimal('0'))
    net = _q(max(Decimal('0'), gross - prior_credit))
    already_paid = _q(invoice.amount_paid)

    invoice.headcount = quote.headcount
    invoice.tier = quote.tier
    invoice.rate_per_student = quote.rate
    invoice.subtotal = quote.raw_subtotal
    invoice.floor_applied = quote.floor_applied
    invoice.amount_due = net
    invoice.census_status = PlatformInvoice.CensusStatus.LOCKED
    invoice.census_locked_at = timezone.now()
    note = (
        f'Census locked on {day.isoformat()} at {quote.headcount} active students '
        f'(KSh {net} after KSh {prior_credit} credit).'
    )
    invoice.notes = f'{invoice.notes} {note}'.strip()

    if already_paid >= net and net >= 0:
        invoice.amount_paid = net
        invoice.status = PlatformInvoice.Status.PAID
        invoice.paid_at = invoice.paid_at or timezone.now()
    elif already_paid > 0:
        invoice.status = PlatformInvoice.Status.PARTIAL
    else:
        invoice.status = PlatformInvoice.Status.OPEN

    invoice.save()
    return invoice


def preview_true_up(
    main: PlatformInvoice,
    *,
    live_headcount: int | None = None,
    settings: PlatformBillingSettings | None = None,
) -> TrueUpPreview:
    cfg = settings or PlatformBillingSettings.load()
    live = (
        live_headcount
        if live_headcount is not None
        else active_student_count(main.school)
    )
    locked = int(main.headcount or 0)
    delta = live - locked
    rate = main.rate_per_student
    factor = cfg.true_up_factor
    amount = _q(abs(Decimal(delta)) * rate * factor)

    if main.census_status != PlatformInvoice.CensusStatus.LOCKED:
        return TrueUpPreview(
            locked_headcount=locked,
            live_headcount=live,
            net_delta=delta,
            rate=rate,
            factor=factor,
            amount=Decimal('0'),
            action='none',
            ignored=True,
            reason='Census not locked yet — true-up runs after day 14 and at term end.',
        )
    if abs(delta) < cfg.true_up_min_delta:
        return TrueUpPreview(
            locked_headcount=locked,
            live_headcount=live,
            net_delta=delta,
            rate=rate,
            factor=factor,
            amount=Decimal('0'),
            action='none',
            ignored=True,
            reason=(
                f'Net change ({delta:+d}) is under the {cfg.true_up_min_delta}-student '
                'threshold — treated as normal churn.'
            ),
        )
    if delta > 0:
        return TrueUpPreview(
            locked_headcount=locked,
            live_headcount=live,
            net_delta=delta,
            rate=rate,
            factor=factor,
            amount=amount,
            action='charge',
            ignored=False,
            reason=(
                f'Net +{delta} students since census → end-of-term add-on at '
                f'{factor}× the locked per-student rate.'
            ),
        )
    return TrueUpPreview(
        locked_headcount=locked,
        live_headcount=live,
        net_delta=delta,
        rate=rate,
        factor=factor,
        amount=amount,
        action='credit',
        ignored=False,
        reason=(
            f'Net {delta} students since census → KSh {amount} credit on the next '
            'term invoice (no cash refund).'
        ),
    )


@transaction.atomic
def settle_term_true_up(
    main: PlatformInvoice,
    *,
    as_of: date | None = None,
) -> PlatformInvoice | None:
    """
    After term end: net delta vs locked census.
    Growth → true-up invoice; loss → subscription credit.
    """
    if main.kind != PlatformInvoice.Kind.MAIN:
        return None
    if main.true_up_settled_at is not None:
        return None
    if main.status == PlatformInvoice.Status.VOID:
        return None

    day = as_of or timezone.localdate()
    term = main.term_period
    if term is not None and day <= term.end_date:
        return None

    lock_census_if_due(main, as_of=day)
    main.refresh_from_db()
    if main.census_status != PlatformInvoice.CensusStatus.LOCKED:
        # Force lock if term ended without census somehow.
        main.census_status = PlatformInvoice.CensusStatus.LOCKED
        main.census_locked_at = main.census_locked_at or timezone.now()
        main.save(update_fields=['census_status', 'census_locked_at', 'updated_at'])

    cfg = PlatformBillingSettings.load()
    preview = preview_true_up(main, settings=cfg)
    main.true_up_settled_at = timezone.now()
    main.save(update_fields=['true_up_settled_at', 'updated_at'])

    if preview.ignored or preview.action == 'none':
        main.notes = f'{main.notes} Term-end true-up: {preview.reason}'.strip()
        main.save(update_fields=['notes', 'updated_at'])
        return None

    if preview.action == 'credit':
        sub = main.school.subscription
        sub.credit_balance = _q((sub.credit_balance or Decimal('0')) + preview.amount)
        sub.save(update_fields=['credit_balance', 'updated_at'])
        main.notes = (
            f'{main.notes} Term-end credit KSh {preview.amount} for net '
            f'{preview.net_delta} students (next-term credit).'
        ).strip()
        main.save(update_fields=['notes', 'updated_at'])
        return None

    # charge
    due_full = day + timedelta(days=cfg.term_full_due_days)
    invoice = PlatformInvoice.objects.create(
        school=main.school,
        term_period=main.term_period,
        related_invoice=main,
        kind=PlatformInvoice.Kind.TRUE_UP,
        term_label=f'{main.term_label} true-up',
        headcount=preview.live_headcount,
        headcount_delta=preview.net_delta,
        tier=main.tier,
        rate_per_student=main.rate_per_student,
        subtotal=preview.amount,
        floor_applied=Decimal('0'),
        proration_factor=preview.factor,
        amount_due=preview.amount,
        amount_paid=Decimal('0'),
        due_half_at=day,
        due_full_at=due_full,
        census_status=PlatformInvoice.CensusStatus.LOCKED,
        census_lock_date=day,
        census_locked_at=timezone.now(),
        status=PlatformInvoice.Status.OPEN,
        is_first_billable=False,
        notes=(
            f'End-of-term true-up: census {preview.locked_headcount} → '
            f'{preview.live_headcount} (net +{preview.net_delta}) at '
            f'{preview.factor}× KSh {preview.rate}/student.'
        ),
    )
    return invoice


@transaction.atomic
def record_platform_payment(
    invoice: PlatformInvoice,
    *,
    amount: Decimal,
) -> PlatformInvoice:
    amount = _q(Decimal(amount))
    if amount <= 0:
        raise ValueError('Payment amount must be positive.')
    invoice.amount_paid = _q(invoice.amount_paid + amount)
    if invoice.amount_paid >= invoice.amount_due:
        invoice.amount_paid = invoice.amount_due
        invoice.status = PlatformInvoice.Status.PAID
        invoice.paid_at = timezone.now()
    elif invoice.amount_paid > 0:
        invoice.status = PlatformInvoice.Status.PARTIAL
    invoice.save(
        update_fields=['amount_paid', 'status', 'paid_at', 'updated_at']
    )
    refresh_subscription_state(invoice.school)
    return invoice


def refresh_subscription_state(school: School) -> SchoolSubscription | None:
    try:
        sub = school.subscription
    except SchoolSubscription.DoesNotExist:
        return None

    cfg = PlatformBillingSettings.load()
    now = timezone.now()
    today = timezone.localdate()

    if school.status != School.Status.APPROVED:
        return sub

    if sub.trial_ends_at and now < sub.trial_ends_at:
        sub.billing_state = SchoolSubscription.BillingState.TRIAL
        sub.locked_at = None
        sub.lock_reason = ''
        sub.save(
            update_fields=[
                'billing_state',
                'locked_at',
                'lock_reason',
                'updated_at',
            ]
        )
        return sub

    open_invoices = list(
        PlatformInvoice.objects.filter(school=school)
        .exclude(status__in=[PlatformInvoice.Status.PAID, PlatformInvoice.Status.VOID])
        .order_by('created_at')
    )
    if not open_invoices and not PlatformInvoice.objects.filter(school=school).exists():
        create_post_trial_invoice(school, as_of=today)
        open_invoices = list(
            PlatformInvoice.objects.filter(school=school)
            .exclude(status__in=[PlatformInvoice.Status.PAID, PlatformInvoice.Status.VOID])
            .order_by('created_at')
        )

    # Lock due censuses before payment checks.
    for inv in list(
        PlatformInvoice.objects.filter(
            school=school,
            kind=PlatformInvoice.Kind.MAIN,
            census_status=PlatformInvoice.CensusStatus.PROVISIONAL,
        )
    ):
        lock_census_if_due(inv, as_of=today)

    open_invoices = list(
        PlatformInvoice.objects.filter(school=school)
        .exclude(status__in=[PlatformInvoice.Status.PAID, PlatformInvoice.Status.VOID])
        .order_by('created_at')
    )

    lock_reason = ''
    state = SchoolSubscription.BillingState.OK

    first = (
        PlatformInvoice.objects.filter(
            school=school,
            is_first_billable=True,
            kind=PlatformInvoice.Kind.MAIN,
        )
        .exclude(status=PlatformInvoice.Status.VOID)
        .order_by('created_at')
        .first()
    )
    if first and first.status != PlatformInvoice.Status.PAID and sub.trial_ends_at:
        grace_end = (sub.trial_ends_at + timedelta(days=cfg.post_trial_grace_days)).date()
        if today > grace_end and first.amount_paid < first.amount_due:
            state = SchoolSubscription.BillingState.LOCKED
            lock_reason = 'Trial ended and first invoice unpaid after grace period.'
        elif today > sub.trial_ends_at.date() and first.amount_paid < first.amount_due:
            state = SchoolSubscription.BillingState.GRACE

    for inv in open_invoices:
        if state == SchoolSubscription.BillingState.LOCKED:
            break
        # Half-due only enforced after census lock for main invoices.
        if inv.kind == PlatformInvoice.Kind.MAIN and not inv.is_census_locked:
            continue
        if inv.due_half_at and today > inv.due_half_at:
            if inv.amount_paid < inv.half_due_amount:
                state = SchoolSubscription.BillingState.LOCKED
                lock_reason = (
                    f'Less than 50% of {inv.term_label} paid by the census / half-due date.'
                )
                break
        if inv.due_full_at and today > inv.due_full_at:
            if inv.amount_paid < inv.amount_due:
                state = SchoolSubscription.BillingState.LOCKED
                lock_reason = f'{inv.term_label} not fully paid by the full-due date.'
                break
        if inv.due_half_at and today >= inv.due_half_at and inv.amount_paid < inv.amount_due:
            if state != SchoolSubscription.BillingState.LOCKED:
                state = SchoolSubscription.BillingState.GRACE

    sub.billing_state = state
    if state == SchoolSubscription.BillingState.LOCKED:
        if not sub.locked_at:
            sub.locked_at = now
        sub.lock_reason = lock_reason
    else:
        sub.locked_at = None
        sub.lock_reason = ''
    term = current_term_for_school(school, today)
    sub.current_term_label = term.label if term else ''
    sub.save(
        update_fields=[
            'billing_state',
            'locked_at',
            'lock_reason',
            'current_term_label',
            'updated_at',
        ]
    )
    return sub


def enforce_all_schools() -> dict[str, int]:
    """Cron: invoices, census locks, term-end true-ups, soft-locks."""
    stats = {
        'schools': 0,
        'invoices': 0,
        'census_locks': 0,
        'true_ups': 0,
        'locked': 0,
    }
    today = timezone.localdate()
    schools = School.objects.filter(
        is_active=True,
        status=School.Status.APPROVED,
    ).select_related('subscription')

    for school in schools.iterator():
        stats['schools'] += 1
        try:
            sub = school.subscription
        except SchoolSubscription.DoesNotExist:
            continue

        if sub.trial_ends_at and timezone.now() >= sub.trial_ends_at:
            before = PlatformInvoice.objects.filter(school=school).count()
            has_main = (
                PlatformInvoice.objects.filter(
                    school=school,
                    kind=PlatformInvoice.Kind.MAIN,
                )
                .exclude(status=PlatformInvoice.Status.VOID)
                .exists()
            )
            if not has_main:
                create_post_trial_invoice(school, as_of=today)
            else:
                ensure_open_term_invoice(school, as_of=today)
            after = PlatformInvoice.objects.filter(school=school).count()
            stats['invoices'] += max(0, after - before)

        for inv in PlatformInvoice.objects.filter(
            school=school,
            kind=PlatformInvoice.Kind.MAIN,
            census_status=PlatformInvoice.CensusStatus.PROVISIONAL,
        ):
            before_status = inv.census_status
            lock_census_if_due(inv, as_of=today)
            inv.refresh_from_db()
            if (
                before_status == PlatformInvoice.CensusStatus.PROVISIONAL
                and inv.census_status == PlatformInvoice.CensusStatus.LOCKED
            ):
                stats['census_locks'] += 1

        # Settle true-ups for terms that have ended.
        for main in PlatformInvoice.objects.filter(
            school=school,
            kind=PlatformInvoice.Kind.MAIN,
            true_up_settled_at__isnull=True,
        ).exclude(status=PlatformInvoice.Status.VOID):
            term = main.term_period
            if term is None:
                continue
            if today <= term.end_date:
                continue
            created = settle_term_true_up(main, as_of=today)
            if created is not None:
                stats['true_ups'] += 1

        refresh_subscription_state(school)
        school.refresh_from_db()
        try:
            if school.subscription.is_locked:
                stats['locked'] += 1
        except SchoolSubscription.DoesNotExist:
            pass

    return stats
