from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from datetime import date, timedelta
from django.db.models import Count

from .models import Role, UserProfile, Consultant, AuditLog, FieldValidationConfig, LookupDefault
from .permissions import (
    is_admin_or_subadmin,
    is_admin_only,
    can_manage_users,
    can_manage_roles,
    can_manage_lookups,
    can_manage_validation_rules
)
from .lookup_config import LOOKUP_CONFIG, get_lookup_model
from .lookup_defaults import get_default_lookup_id, set_default_lookup_id, clear_default_lookup_id
from .validators import get_or_create_field_config
from apps.patients.models import Patient
from apps.scheduler.models import Machine, Shift, Technique, SpecialTechnique, TreatmentSchedule, MachineMapping


def login_view(request):
    if request.user.is_authenticated:
        return redirect('patient_register')

    if request.method == 'POST':
        uname = request.POST.get('username', '').strip()
        pword = request.POST.get('password', '').strip()

        user = authenticate(request, username=uname, password=pword)
        if user is not None:
            if not user.is_active:
                messages.error(request, "Your account is deactivated. Please contact an administrator.")
                return render(request, 'core/login.html')

            login(request, user)
            profile = getattr(user, 'profile', None)
            profile_role = profile.role.role_name if profile and profile.role else 'User'

            # Audit log
            AuditLog.objects.create(
                user=user,
                action="USER_LOGIN",
                details=f"User {user.username} ({profile_role}) logged in successfully.",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, f"Welcome back, {user.first_name or user.username} ({profile_role})!")
            # User requirement: on logging in - patient has to be registered
            return redirect('patient_register')
        else:
            messages.error(request, "Wrong Username or Password! Please verify your credentials.")

    # Get user list for convenient dropdown selection similar to MS Access Login
    users = User.objects.filter(is_active=True).order_by('username')
    return render(request, 'core/login.html', {'user_list': users})


def logout_view(request):
    if request.user.is_authenticated:
        AuditLog.objects.create(
            user=request.user,
            action="USER_LOGOUT",
            details=f"User {request.user.username} logged out.",
            ip_address=request.META.get('REMOTE_ADDR')
        )
    logout(request)
    messages.info(request, "You have been logged out safely.")
    return redirect('login')


@login_required
def dashboard_view(request):
    today = date.today()

    total_patients = Patient.objects.count()
    total_schedules = TreatmentSchedule.objects.count()
    pending_starts = TreatmentSchedule.objects.filter(date_of_start__isnull=True).count()
    today_starts = TreatmentSchedule.objects.filter(date_of_start=today).count()

    # Machine workload for today
    machine_counts = TreatmentSchedule.objects.filter(date_of_start=today)\
        .values('machine__name', 'machine__code')\
        .annotate(patient_count=Count('id'))\
        .order_by('machine__display_order')

    # Recent 10 registrations
    recent_patients = Patient.objects.select_related('primary_consultant').order_by('-created_at')[:10]

    # Upcoming starts this week
    week_end = today + timedelta(days=7)
    upcoming_starts = TreatmentSchedule.objects.filter(date_of_start__range=[today, week_end])\
        .select_related('patient', 'machine', 'shift')\
        .order_by('date_of_start')[:15]

    context = {
        'total_patients': total_patients,
        'total_schedules': total_schedules,
        'pending_starts': pending_starts,
        'today_starts': today_starts,
        'machine_counts': machine_counts,
        'recent_patients': recent_patients,
        'upcoming_starts': upcoming_starts,
        'today': today,
    }
    return render(request, 'core/dashboard.html', context)


@login_required
def user_list_view(request):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    users = User.objects.select_related('profile__role').order_by('username')
    roles = Role.objects.filter(is_active=True).order_by('display_order')
    return render(request, 'core/user_list.html', {'users': users, 'roles': roles})


@login_required
def user_create_view(request):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    roles = Role.objects.filter(is_active=True).order_by('display_order')

    if request.method == 'POST':
        uname = request.POST.get('username', '').strip().lower()
        fname = request.POST.get('first_name', '').strip()
        pword = request.POST.get('password', '').strip()
        role_id = request.POST.get('role_id')

        if not uname or not pword:
            messages.error(request, "Username and Password are required.")
            return render(request, 'core/user_form.html', {'roles': roles})

        if User.objects.filter(username=uname).exists():
            messages.error(request, f"Username '{uname}' already exists.")
            return render(request, 'core/user_form.html', {'roles': roles})

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id else None
        is_staff = bool(role_obj and role_obj.role_name.lower() in ('admin', 'subadmin'))

        user = User.objects.create_user(username=uname, first_name=fname, password=pword, is_staff=is_staff)
        UserProfile.objects.create(user=user, role=role_obj, user_level=role_obj.role_name.upper() if role_obj else 'USER')

        messages.success(request, f"User '{uname}' created successfully with role '{role_obj.role_name if role_obj else 'None'}'.")
        return redirect('user_list')

    return render(request, 'core/user_form.html', {'roles': roles})


@login_required
def user_edit_view(request, user_id):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    target_user = get_object_or_404(User, id=user_id)
    roles = Role.objects.filter(is_active=True).order_by('display_order')

    if request.method == 'POST':
        fname = request.POST.get('first_name', '').strip()
        new_pwd = request.POST.get('password', '').strip()
        role_id = request.POST.get('role_id')
        is_active = request.POST.get('is_active') == 'on'

        target_user.first_name = fname
        target_user.is_active = is_active
        if new_pwd:
            target_user.set_password(new_pwd)

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id else None
        target_user.is_staff = bool(role_obj and role_obj.role_name.lower() in ('admin', 'subadmin'))
        target_user.save()

        profile, _ = UserProfile.objects.get_or_create(user=target_user)
        profile.role = role_obj
        profile.user_level = role_obj.role_name.upper() if role_obj else 'USER'
        profile.save()

        messages.success(request, f"User '{target_user.username}' updated successfully.")
        return redirect('user_list')

    return render(request, 'core/user_form.html', {'target_user': target_user, 'roles': roles})


@login_required
def role_dashboard_view(request):
    if not can_manage_roles(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    roles = Role.objects.all().order_by('display_order')
    return render(request, 'core/role_dashboard.html', {'roles': roles})


# --- Gynae-Style Centralized Dropdown & Lookup Tables Manager (Admin Only) ---

@login_required
def lookup_manager_view(request, category='machine'):
    """
    Replicates Gynae database Dropdown Options & Clinical Reference Tables Manager.
    Allows Admin to Add, Edit, and Inactivate dropdown values with display order and defaults.
    Strictly blocked for SubAdmin.
    """
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied. Dropdown management requires Administrator privileges (SubAdmin restricted).")
        return redirect('dashboard')

    if category not in LOOKUP_CONFIG:
        category = 'machine'

    model, cfg = get_lookup_model(category)
    default_id = get_default_lookup_id(category)

    raw_items = model.objects.all().order_by('display_order', cfg['name_field'])
    items = []
    default_item = None

    for it in raw_items:
        is_def = (str(it.pk) == str(default_id)) if default_id is not None else False
        item_dict = {
            'pk': it.pk,
            'name': getattr(it, cfg['name_field']),
            'secondary': getattr(it, cfg['secondary_field']) if cfg.get('secondary_field') else None,
            'extra': getattr(it, cfg['extra_field']) if cfg.get('extra_field') else None,
            'display_order': getattr(it, 'display_order', 1),
            'is_active': getattr(it, 'is_active', True),
            'is_default': is_def,
        }
        items.append(item_dict)
        if is_def:
            default_item = item_dict

    context = {
        'category': category,
        'cfg': cfg,
        'items': items,
        'default_id': default_id,
        'default_item': default_item,
        'lookup_categories': LOOKUP_CONFIG,
    }
    return render(request, 'core/lookup_manager.html', context)


@login_required
def lookup_save_view(request, category, item_id=None):
    """Add or edit an option in the selected lookup table."""
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied. Only Administrators can modify dropdown options.")
        return redirect('dashboard')

    model, cfg = get_lookup_model(category)
    if not model:
        return redirect('lookup_manager', category='machine')

    pk_field = cfg['pk']
    name_field = cfg['name_field']
    sec_field = cfg.get('secondary_field')
    extra_field = cfg.get('extra_field')

    if request.method == 'POST':
        if not item_id:
            raw_id = request.POST.get('item_id')
            if raw_id:
                try:
                    item_id = int(raw_id)
                except (ValueError, TypeError):
                    item_id = None

        item = get_object_or_404(model, **{pk_field: item_id}) if item_id else None
        name_val = request.POST.get('name_value', '').strip()
        sec_val = request.POST.get('secondary_value', '').strip() if sec_field else None
        extra_val = request.POST.get('extra_value', '').strip() if extra_field else None
        order_val = int(request.POST.get('display_order') or 1)
        active_val = bool(request.POST.get('is_active'))
        is_default_checked = bool(request.POST.get('is_default'))

        if not name_val:
            messages.error(request, f"{cfg['name_label']} cannot be empty.")
            return redirect('lookup_manager', category=category)

        if not item:
            # Create new option
            create_kwargs = {
                name_field: name_val,
                'display_order': order_val,
                'is_active': active_val,
            }
            if sec_field and sec_val:
                create_kwargs[sec_field] = sec_val
            if extra_field and extra_val:
                create_kwargs[extra_field] = extra_val

            item = model.objects.create(**create_kwargs)

            # If a machine was created, ensure it has a MachineMapping
            if category == 'machine':
                MachineMapping.objects.get_or_create(machine=item)

            AuditLog.objects.create(
                user=request.user,
                action="LOOKUP_ADD",
                details=f"Added option '{name_val}' to {cfg['title']}",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            if is_default_checked:
                set_default_lookup_id(category, item.pk, user=request.user)

            messages.success(request, f"Option '{name_val}' added to {cfg['title']} successfully.")
        else:
            setattr(item, name_field, name_val)
            if sec_field:
                setattr(item, sec_field, sec_val)
            if extra_field and extra_val:
                setattr(item, extra_field, extra_val)
            item.display_order = order_val
            item.is_active = active_val
            item.save()

            AuditLog.objects.create(
                user=request.user,
                action="LOOKUP_UPDATE",
                details=f"Updated option '{name_val}' (ID: {item.pk}) in {cfg['title']}",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            cur_def = get_default_lookup_id(category)
            if is_default_checked:
                set_default_lookup_id(category, item.pk, user=request.user)
            elif cur_def is not None and str(cur_def) == str(item.pk):
                clear_default_lookup_id(category, user=request.user)

            messages.success(request, f"Option '{name_val}' updated successfully.")

    return redirect('lookup_manager', category=category)


@login_required
def lookup_toggle_active_view(request, category, item_id):
    """Toggles active/inactive status for a dropdown option."""
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied. Only Administrators can change option status.")
        return redirect('dashboard')

    model, cfg = get_lookup_model(category)
    if not model:
        return redirect('lookup_manager', category='machine')

    pk_field = cfg['pk']
    item = get_object_or_404(model, **{pk_field: item_id})
    item.is_active = not item.is_active
    item.save()

    AuditLog.objects.create(
        user=request.user,
        action="LOOKUP_TOGGLE_ACTIVE",
        details=f"Toggled option '{getattr(item, cfg['name_field'])}' in {cfg['title']} to active={item.is_active}",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    status_str = "Active" if item.is_active else "Inactive"
    messages.success(request, f"Option '{getattr(item, cfg['name_field'])}' status is now {status_str}.")
    return redirect('lookup_manager', category=category)


@login_required
def lookup_set_default_view(request, category, item_id):
    """Sets designated option as default."""
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    model, cfg = get_lookup_model(category)
    if not model:
        return redirect('lookup_manager', category='machine')

    pk_field = cfg['pk']
    item = get_object_or_404(model, **{pk_field: item_id})
    set_default_lookup_id(category, getattr(item, pk_field), user=request.user)
    messages.success(request, f"Option '{getattr(item, cfg['name_field'])}' set as default for {cfg['title']}.")
    return redirect('lookup_manager', category=category)


@login_required
def lookup_clear_default_view(request, category):
    """Clears default option for category."""
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    _, cfg = get_lookup_model(category)
    title = cfg['title'] if cfg else category
    clear_default_lookup_id(category, user=request.user)
    messages.info(request, f"Default option cleared for {title}.")
    return redirect('lookup_manager', category=category)


@login_required
def lookup_quick_default_view(request, category):
    """Selects default option via quick dropdown."""
    if not can_manage_lookups(request.user):
        messages.error(request, "Access denied.")
        return redirect('dashboard')

    if request.method == 'POST':
        item_id = request.POST.get('default_item_id')
        model, cfg = get_lookup_model(category)
        if item_id:
            pk_field = cfg['pk']
            item = get_object_or_404(model, **{pk_field: item_id})
            set_default_lookup_id(category, getattr(item, pk_field), user=request.user)
            messages.success(request, f"Default for {cfg['title']} set to '{getattr(item, cfg['name_field'])}'.")
        else:
            clear_default_lookup_id(category, user=request.user)
            messages.info(request, f"Default option cleared for {cfg['title']}.")

    return redirect('lookup_manager', category=category)


# --- Configurable Field Validation Management (Admin Only) ---

@login_required
def validation_config_view(request):
    """
    Field Validation Configuration Page.
    Only Administrator (not SubAdmin) can configure character length and format validation
    for MR Number, Phone Number, etc.
    """
    if not can_manage_validation_rules(request.user):
        messages.error(request, "Access denied. Only Administrators can configure field validations (SubAdmin restricted).")
        return redirect('dashboard')

    mr_config = get_or_create_field_config('mr_no')
    phone_config = get_or_create_field_config('phone_no')

    if request.method == 'POST':
        # Update MR No configuration
        mr_mode = request.POST.get('mr_validation_mode', 'range')
        mr_exact = request.POST.get('mr_exact_length')
        mr_min = request.POST.get('mr_min_length')
        mr_max = request.POST.get('mr_max_length')
        mr_numeric = request.POST.get('mr_numeric_only') == '1'
        mr_allow_blank = request.POST.get('mr_allow_blank') == '1'
        mr_custom_err = request.POST.get('mr_custom_error_message', '').strip()

        mr_config.validation_mode = mr_mode
        mr_config.exact_length = int(mr_exact) if mr_exact and mr_exact.strip().isdigit() else None
        mr_config.min_length = int(mr_min) if mr_min and mr_min.strip().isdigit() else None
        mr_config.max_length = int(mr_max) if mr_max and mr_max.strip().isdigit() else None
        mr_config.numeric_only = mr_numeric
        mr_config.allow_blank = mr_allow_blank
        mr_config.custom_error_message = mr_custom_err or None
        mr_config.save()

        # Update Phone No configuration
        ph_mode = request.POST.get('ph_validation_mode', 'range')
        ph_exact = request.POST.get('ph_exact_length')
        ph_min = request.POST.get('ph_min_length')
        ph_max = request.POST.get('ph_max_length')
        ph_numeric = request.POST.get('ph_numeric_only') == '1'
        ph_allow_blank = request.POST.get('ph_allow_blank') == '1'
        ph_custom_err = request.POST.get('ph_custom_error_message', '').strip()

        phone_config.validation_mode = ph_mode
        phone_config.exact_length = int(ph_exact) if ph_exact and ph_exact.strip().isdigit() else None
        phone_config.min_length = int(ph_min) if ph_min and ph_min.strip().isdigit() else None
        phone_config.max_length = int(ph_max) if ph_max and ph_max.strip().isdigit() else None
        phone_config.numeric_only = ph_numeric
        phone_config.allow_blank = ph_allow_blank
        phone_config.custom_error_message = ph_custom_err or None
        phone_config.save()

        AuditLog.objects.create(
            user=request.user,
            action="FIELD_VALIDATION_CONFIG_UPDATE",
            details="Updated validation rules for MR Number and Phone Number.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, "Field validation configurations updated successfully!")
        return redirect('validation_config')

    context = {
        'mr_config': mr_config,
        'phone_config': phone_config,
    }
    return render(request, 'core/validation_config.html', context)

