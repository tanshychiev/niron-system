from django.contrib.auth.models import Permission, User
from django.db.models.signals import m2m_changed
from django.dispatch import receiver


ADMIN_GROUP_NAMES = {
    "ADMIN",
    "ADMIN FULL CONTROL",
    "ADMINISTRATOR",
}


def _normalized_role_name(group):
    if not group:
        return ""
    return " ".join((group.name or "").strip().upper().split())


def _is_admin_role(group):
    return _normalized_role_name(group) in ADMIN_GROUP_NAMES


def _sync_flags(user):
    selected_group = user.groups.order_by("name").first()
    admin_access = _is_admin_role(selected_group)

    if admin_access:
        selected_group.permissions.set(Permission.objects.all())

    User.objects.filter(pk=user.pk).update(
        is_staff=admin_access,
        is_superuser=admin_access,
    )

    for cache_name in (
        "_perm_cache",
        "_user_perm_cache",
        "_group_perm_cache",
    ):
        if hasattr(user, cache_name):
            delattr(user, cache_name)


@receiver(m2m_changed, sender=User.groups.through)
def sync_user_role_flags(sender, instance, action, reverse, **kwargs):
    if reverse:
        if action in {"post_add", "post_remove", "post_clear"}:
            for user in instance.user_set.all():
                _sync_flags(user)
        return

    if action in {"post_add", "post_remove", "post_clear"}:
        _sync_flags(instance)
