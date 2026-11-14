from .permissions import (
    is_admin_or_subadmin,
    is_admin_only,
    can_manage_validation_rules,
    can_edit_locked_identity,
    can_manage_mappings,
    can_manage_users,
    can_manage_roles,
    can_manage_lookups,
    can_delete_patient,
    can_export_data
)

def role_permissions(request):
    if not request.user.is_authenticated:
        return {
            'APP_TITLE': 'Radiotherapy Patient Scheduler',
            'APP_VERSION': 'v2.0 Django',
        }
    
    profile = getattr(request.user, 'profile', None)
    current_role = profile.role if profile else None
    
    return {
        'APP_TITLE': 'Radiotherapy Patient Scheduler',
        'APP_VERSION': 'v2.0 Django',
        'CURRENT_ROLE': current_role,
        'IS_ADMIN_OR_SUBADMIN': is_admin_or_subadmin(request.user),
        'IS_ADMIN_ONLY': is_admin_only(request.user),
        'CAN_MANAGE_VALIDATION_RULES': can_manage_validation_rules(request.user),
        'CAN_EDIT_LOCKED_IDENTITY': can_edit_locked_identity(request.user),
        'CAN_MANAGE_MAPPINGS': can_manage_mappings(request.user),
        'CAN_MANAGE_USERS': can_manage_users(request.user),
        'CAN_MANAGE_ROLES': can_manage_roles(request.user),
        'CAN_MANAGE_LOOKUPS': can_manage_lookups(request.user),
        'CAN_DELETE_PATIENT': can_delete_patient(request.user),
        'CAN_EXPORT_DATA': can_export_data(request.user),
    }
