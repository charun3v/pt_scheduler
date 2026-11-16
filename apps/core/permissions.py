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
    """Only Admin (not SubAdmin) can manage roles."""
    return is_admin_only(user)


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


def is_frontdesk_nursing_or_physics(user) -> bool:
    """Checks if the user belongs to Front Desk, Nursing, or Physics roles."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return False
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        r_name = profile.role.role_name.strip().lower()
        if any(keyword in r_name for keyword in ('front desk', 'front office', 'nursing', 'nurse', 'physics')):
            return True
    return False


def can_register_patient(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_register_patient)
    return True  # default clinical user


def can_schedule_patient(user) -> bool:
    """Front desk, Nursing, and Physics staff cannot schedule patients."""
    if is_frontdesk_nursing_or_physics(user):
        return False
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_schedule_patient)
    return True


def can_edit_patient(user) -> bool:
    """Front desk, Nursing, and Physics staff can view patient details but cannot edit or add."""
    if is_frontdesk_nursing_or_physics(user):
        return False
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_register_patient or profile.role.cap_edit_locked_identity)
    return True


def can_print_schedules(user) -> bool:
    """Admin, SubAdmin, Doctors, Residents, Front Desk, Nursing, and Physics can take printouts."""
    if not user or not user.is_authenticated:
        return False
    if is_admin_or_subadmin(user):
        return True
    if is_frontdesk_nursing_or_physics(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_view_reports or profile.role.cap_view_patients)
    return True


def can_delete_patient(user) -> bool:
    """Only Admin (not SubAdmin) has the right to delete patient records."""
    return is_admin_only(user)


def can_remove_from_schedule(user) -> bool:
    """Only Admin (not SubAdmin) has the right to remove a patient from the schedule list."""
    return is_admin_only(user)


def can_manual_schedule(user) -> bool:
    """Manual treatment scheduling in Excel grid format is available to Admin and SubAdmin."""
    return is_admin_or_subadmin(user)


def can_export_data(user) -> bool:
    if is_admin_or_subadmin(user):
        return True
    profile = getattr(user, 'profile', None)
    if profile and profile.role and profile.role.is_active:
        return bool(profile.role.cap_export_data)
    return True
