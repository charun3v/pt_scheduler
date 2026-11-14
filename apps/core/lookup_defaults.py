from .models import LookupDefault, AuditLog

DEFAULT_LOOKUP_SEEDS = {
    'technique': ('3DCRT', 'name'),
    'special_technique': ('None', 'name'),
    'shift': ('Shift I', 'name'),
}


def get_default_lookup_id(category: str):
    """
    Returns the primary key of the default option for the given lookup category.
    """
    ld = LookupDefault.objects.filter(category=category).first()
    if ld and ld.default_id:
        val = ld.default_id.strip()
        try:
            return int(val)
        except (ValueError, TypeError):
            return val

    # Auto-seed initial default if defined
    if category in DEFAULT_LOOKUP_SEEDS:
        from .lookup_config import get_lookup_model
        model, cfg = get_lookup_model(category)
        if model and cfg:
            target_str, search_field = DEFAULT_LOOKUP_SEEDS[category]
            filter_kwargs = {f"{search_field}__icontains": target_str, "is_active": True}
            item = model.objects.filter(**filter_kwargs).first() or model.objects.filter(is_active=True).first()
            if item:
                LookupDefault.objects.update_or_create(
                    category=category,
                    defaults={'default_id': str(item.pk)}
                )
                return item.pk
    return None


def set_default_lookup_id(category: str, item_id, user=None):
    """Saves the default lookup option for category."""
    LookupDefault.objects.update_or_create(
        category=category,
        defaults={'default_id': str(item_id) if item_id is not None else ''}
    )
    if user:
        AuditLog.objects.create(
            user=user,
            action="LOOKUP_DEFAULT_SET",
            details=f"Set default option for dropdown '{category}' to ID {item_id}"
        )


def clear_default_lookup_id(category: str, user=None):
    """Clears the default option for category."""
    LookupDefault.objects.filter(category=category).delete()
    if user:
        AuditLog.objects.create(
            user=user,
            action="LOOKUP_DEFAULT_CLEAR",
            details=f"Cleared default option for dropdown '{category}'"
        )
