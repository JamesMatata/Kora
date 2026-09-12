# Get started UI (planned — not built yet)

## Goal

When an admin (or bursar) opens Kora for a school the **first time**, show a short **Get started** experience so setup feels guided — without putting “Get started” permanently in the sidebar.

## Product decision (locked for later)

- **Not** a sidebar nav item (removed from daily navigation).
- Prefer a **first-visit modal / checklist drawer** on Overview (or after school select).
- Existing page at `/onboarding/` can stay as the deep content / fallback until the modal ships.
- After the checklist is complete (or dismissed), do **not** nag every login; optional “Resume setup” from Settings is enough.

## Suggested behaviour

1. Detect incomplete onboarding (same steps as today’s checklist: classes, staff, Paybill/Daraja, fee plans/invoices, optional Twilio).
2. First visit for that school membership: open modal with 4–6 steps and primary CTAs.
3. Persist dismiss / complete on membership or school prefs (e.g. `onboarding_dismissed_at`).
4. Allow reopen from Settings → “Setup checklist” (not from top of sidebar).

## Out of scope for now

- Do not implement the modal in this iteration.
- Keep `/onboarding/` available if linked from docs or Settings later.
