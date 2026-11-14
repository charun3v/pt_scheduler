"""
Centralized Role-Based Access Control (RBAC) & Authority Checks.
Admin and SubAdmin have administrative control as per user specifications.
"""

def is_admin_or_subadmin(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        r_name = profile.role.role_name.strip().lower()
        if r_name in ('admin', 'subadmin'):
            return True
        if profile.role.cap_manage_users and profile.role.cap_manage_mappings:
            return True
    if user.is_staff:
        return True
    return False


def can_edit_locked_identity(user) -> bool:
    """Once registered, MR No and Patient Name can only be edited by Admin/SubAdmin."""
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_edit_locked_identity)
    return False


def can_manage_mappings(user) -> bool:
    """Can map Machines with Shifts, Techniques, Special Techniques, and Sites."""
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_manage_mappings)
    return False


def can_manage_users(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_manage_users)
    return False


def can_manage_roles(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_manage_roles)
    return False


def is_admin_only(user) -> bool:
    """Strictly checks for Administrator role (excluding SubAdmin)."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        r_name = profile.role.role_name.strip().lower()
        if r_name == 'admin':
            return True
    return False


def can_manage_validation_rules(user) -> bool:
    """Only Admin (not SubAdmin) can configure character length and format validation rules."""
    return is_admin_only(user)


def can_manage_lookups(user) -> bool:
    """Only Admin (not SubAdmin) can add, edit, or inactivate dropdown reference options."""
    return is_admin_only(user)


def can_register_patient(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_register_patient)
    return True  # default clinical user


def can_schedule_patient(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_schedule_patient)
    return True


def can_delete_patient(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_delete_records)
    return False


def can_export_data(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_export_data)
    return True
