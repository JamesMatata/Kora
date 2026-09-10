from django.contrib.auth.base_user import BaseUserManager
from django.db import models

from tenants.context import get_current_school_id


class UserManager(BaseUserManager):
    """Manager for email-based authentication."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('Users must have an email address.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self._create_user(email, password, **extra_fields)


class TenantAwareQuerySet(models.QuerySet):
    def for_school(self, school):
        school_id = getattr(school, 'pk', school)
        return self.filter(school_id=school_id)


class TenantAwareManager(models.Manager):
    """
    Filters querysets by the active school context when one is set.

    Use `.unscoped()` or `.for_school(school)` when explicit cross-tenant
    or targeted access is required (e.g. platform jobs, invitations).
    """

    def get_queryset(self):
        qs = TenantAwareQuerySet(self.model, using=self._db)
        school_id = get_current_school_id()
        if school_id is not None:
            return qs.filter(school_id=school_id)
        return qs

    def unscoped(self):
        return TenantAwareQuerySet(self.model, using=self._db)

    def for_school(self, school):
        return self.unscoped().for_school(school)
