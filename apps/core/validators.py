import re
from .models import FieldValidationConfig


DEFAULT_VALIDATION_RULES = {
    'mr_no': {
        'field_label': 'Hospital / MR Number',
        'validation_mode': 'range',
        'exact_length': None,
        'min_length': 4,
        'max_length': 10,
        'numeric_only': True,
        'allow_blank': False,
        'custom_regex': None,
        'custom_error_message': 'MR Number must be numeric and between 4 and 10 digits.',
    },
    'phone_no': {
        'field_label': 'Phone Number',
        'validation_mode': 'range',
        'exact_length': None,
        'min_length': 10,
        'max_length': 15,
        'numeric_only': True,
        'allow_blank': True,
        'custom_regex': None,
        'custom_error_message': 'Phone number must be numeric and between 10 and 15 digits.',
    },
}


def get_or_create_field_config(field_name: str) -> FieldValidationConfig:
    """Retrieves or auto-initializes the validation rule for a field."""
    cfg = FieldValidationConfig.objects.filter(field_name=field_name).first()
    if not cfg:
        defaults = DEFAULT_VALIDATION_RULES.get(field_name, {
            'field_label': field_name.replace('_', ' ').title(),
            'validation_mode': 'range',
            'min_length': 1,
            'max_length': 50,
            'numeric_only': False,
            'allow_blank': True,
        })
        cfg = FieldValidationConfig.objects.create(
            field_name=field_name,
            **defaults
        )
    return cfg


def validate_field_value(field_name: str, raw_value) -> tuple[bool, str | None]:
    """
    Validates a raw value against the admin-configured rule for field_name.
    Returns (is_valid: bool, error_message: str | None).
    """
    cfg = get_or_create_field_config(field_name)
    if not cfg.is_active:
        return True, None

    val_str = str(raw_value).strip() if raw_value is not None else ""

    # Check blank
    if not val_str:
        if not cfg.allow_blank:
            msg = cfg.custom_error_message or f"{cfg.field_label} is required and cannot be blank."
            return False, msg
        return True, None

    # Check numeric-only
    if cfg.numeric_only:
        # Strip optional leading '+' for phone numbers if applicable, or check purely digits
        digits_to_check = val_str
        if field_name == 'phone_no' and digits_to_check.startswith('+'):
            digits_to_check = digits_to_check[1:]

        if not digits_to_check.isdigit():
            msg = cfg.custom_error_message or f"{cfg.field_label} must contain only numbers (digits 0-9)."
            return False, msg

    # Check length
    val_len = len(val_str)

    if cfg.validation_mode == 'exact':
        if cfg.exact_length and val_len != cfg.exact_length:
            msg = cfg.custom_error_message or f"{cfg.field_label} must be exactly {cfg.exact_length} characters long (currently {val_len})."
            return False, msg

    elif cfg.validation_mode == 'range':
        if cfg.min_length and val_len < cfg.min_length:
            msg = cfg.custom_error_message or f"{cfg.field_label} must be at least {cfg.min_length} characters long (currently {val_len})."
            return False, msg
        if cfg.max_length and val_len > cfg.max_length:
            msg = cfg.custom_error_message or f"{cfg.field_label} cannot exceed {cfg.max_length} characters (currently {val_len})."
            return False, msg

    # Check custom regex if provided
    if cfg.custom_regex:
        try:
            if not re.search(cfg.custom_regex, val_str):
                msg = cfg.custom_error_message or f"{cfg.field_label} format does not match the required pattern."
                return False, msg
        except re.error:
            pass  # Ignore invalid admin regex

    return True, None
