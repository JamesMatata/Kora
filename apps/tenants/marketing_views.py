"""Public marketing pages (pricing, how it works, privacy)."""

from django.shortcuts import render
from django.views import View

from tenants.models import PlatformBillingSettings
from tenants.platform_billing import quote_amount


class PricingView(View):
    template_name = 'marketing/pricing.html'

    def get(self, request):
        cfg = PlatformBillingSettings.load()
        tiers = [
            {
                'name': 'Starter',
                'range': f'1 – {cfg.starter_max_students} students',
                'rate': cfg.starter_rate,
                'floor': cfg.starter_floor,
                'example': quote_amount(headcount=250, settings=cfg),
                'blurb': 'Most private primary and junior schools.',
            },
            {
                'name': 'Growth',
                'range': (
                    f'{cfg.starter_max_students + 1} – '
                    f'{cfg.growth_max_students} students'
                ),
                'rate': cfg.growth_rate,
                'floor': None,
                'example': quote_amount(headcount=500, settings=cfg),
                'blurb': 'Growing campuses with more streams and staff.',
            },
            {
                'name': 'Scale',
                'range': f'{cfg.growth_max_students + 1}+ students',
                'rate': cfg.scale_rate,
                'floor': None,
                'example': quote_amount(headcount=900, settings=cfg),
                'blurb': 'Large schools and multi-stream campuses.',
            },
        ]
        return render(
            request,
            self.template_name,
            {
                'billing_settings': cfg,
                'tiers': tiers,
                'page_title': 'Pricing',
            },
        )


class HowItWorksView(View):
    template_name = 'marketing/how_it_works.html'

    def get(self, request):
        return render(
            request,
            self.template_name,
            {'page_title': 'How it works'},
        )


class PrivacyView(View):
    template_name = 'marketing/privacy.html'

    def get(self, request):
        return render(
            request,
            self.template_name,
            {'page_title': 'Privacy & trust'},
        )
