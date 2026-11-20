from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
import json
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from datetime import date, timedelta
from django.db.models import Count, Q, Max
from django.core.paginator import Paginator

from .models import Role, UserProfile, Consultant, AuditLog, FieldValidationConfig, LookupDefault
from .permissions import (
    is_admin_or_subadmin,
    is_admin_only,
    can_manage_users,
    can_manage_roles,
    can_manage_lookups,
    can_manage_validation_rules,
    can_view_pending_patients
)
from .lookup_config import LOOKUP_CONFIG, get_lookup_model
from .lookup_defaults import get_default_lookup_id, set_default_lookup_id, clear_default_lookup_id
from .validators import get_or_create_field_config
from apps.patients.models import Patient
from apps.scheduler.models import Machine, Shift, Technique, SpecialTechnique, TreatmentSchedule, MachineMapping


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        uname = request.POST.get('username', '').strip()
        pword = request.POST.get('password', '').strip()

        user = authenticate(request, username=uname, password=pword)
        if user is not None:
            if not user.is_active:
                profile = getattr(user, 'profile', None)
                if profile and not profile.is_approved:
                    messages.warning(request, "Your account is awaiting Admin approval. Please wait for Admin approval before logging in.")
                else:
                    messages.error(request, "Your account is deactivated. Please contact an Administrator.")
                return render(request, 'core/login.html', {'user_list': User.objects.filter(is_active=True).order_by('username')})

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
            return redirect('dashboard')
        else:
            # Check if this user exists with valid password but is pending approval or inactive
            existing_user = User.objects.filter(username__iexact=uname).first()
            if existing_user and existing_user.check_password(pword):
                profile = getattr(existing_user, 'profile', None)
                if profile and not profile.is_approved:
                    messages.warning(request, "Your account is awaiting Admin approval. Please wait for Admin approval before logging in.")
                    return render(request, 'core/login.html', {'user_list': User.objects.filter(is_active=True).order_by('username')})
                elif not existing_user.is_active:
                    messages.error(request, "Your account is deactivated. Please contact an Administrator.")
                    return render(request, 'core/login.html', {'user_list': User.objects.filter(is_active=True).order_by('username')})

            messages.error(request, "Wrong Username or Password! Please verify your credentials.")

    # Get user list for convenient dropdown selection similar to MS Access Login
    users = User.objects.filter(is_active=True).order_by('username')
    return render(request, 'core/login.html', {'user_list': users})


def register_account_view(request):
    """
    Public registration page accessible from Login screen.
    Prompts for: Prefix (Dr., Mr, Mrs, Miss), Name, Username, Create Password, Retype Password, Desired Role.
    Upon completion, saves account as inactive and pending approval (is_approved=False, is_active=False).
    Displays message: "Wait for Admin approval".
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    prefixes = ['Dr.', 'Mr', 'Mrs', 'Miss']
    # Exclude Administrator and SubAdministrator from public self-registration (residents and staff cannot apply as admin)
    roles = Role.objects.filter(is_active=True).exclude(role_name__in=['Admin', 'SubAdmin']).order_by('display_order')

    if request.method == 'POST':
        prefix = request.POST.get('prefix', 'Dr.').strip()
        first_name = request.POST.get('first_name', '').strip()
        username = request.POST.get('username', '').strip().lower()
        password = request.POST.get('password', '').strip()
        confirm_password = request.POST.get('confirm_password', '').strip()
        role_id = request.POST.get('role_id', '').strip()

        errors = []
        if not first_name:
            errors.append("Full Name is required.")
        if not username:
            errors.append("Username / Login ID is required.")
        if not password:
            errors.append("Password is required.")
        if password != confirm_password:
            errors.append("Passwords do not match. Please retype carefully.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters long.")
        if User.objects.filter(username__iexact=username).exists():
            errors.append(f"Username '{username}' already exists. Please choose a different username.")

        if errors:
            for err in errors:
                messages.error(request, err)
            return render(request, 'core/register_account.html', {
                'prefixes': prefixes,
                'roles': roles,
                'selected_prefix': prefix,
                'first_name': first_name,
                'username': username,
                'selected_role_id': int(role_id) if role_id.isdigit() else None,
            })

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id.isdigit() else None
        # Enforce that public self-registrations cannot take Admin or SubAdmin roles
        if not role_obj or role_obj.role_name.lower() in ('admin', 'subadmin'):
            role_obj = Role.objects.filter(role_name__icontains='Resident').first() or Role.objects.first()

        new_user = User.objects.create_user(
            username=username,
            first_name=first_name,
            password=password,
            is_active=False  # Crucial: login blocked until approved
        )
        UserProfile.objects.create(
            user=new_user,
            prefix=prefix,
            role=role_obj,
            user_level=role_obj.role_name.upper() if role_obj else 'USER',
            is_approved=False,
            is_hidden_from_dashboard=False
        )

        AuditLog.objects.create(
            user=new_user,
            action="USER_REGISTRATION_SUBMITTED",
            details=f"New user registration submitted for {prefix} {first_name} (Username: {username}, Role: {role_obj.role_name if role_obj else 'None'}). Awaiting Admin approval.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, "Account registration submitted successfully! Please wait for Admin approval before logging in.")
        return redirect('login')

    return render(request, 'core/register_account.html', {
        'prefixes': prefixes,
        'roles': roles,
        'selected_prefix': 'Dr.',
    })


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
    """
    Radiotherapy Clinical Dashboard.
    Provides two primary clinical options: Register patients, Schedule patients.
    Displays a table for non-scheduled patients with pagination of 15 patients.
    Admin (not SubAdmin) can manage pending user accounts, approve them, and hide/show them.
    """
    query = request.GET.get('q', '').strip()
    show_hidden = request.GET.get('show_hidden') == '1'
    show_hidden_accounts = request.GET.get('show_hidden_accounts') == '1'
    user_is_admin = is_admin_only(request.user)

    if show_hidden and user_is_admin:
        base_filter = Q(is_hidden_from_dashboard=True)
    else:
        base_filter = Q(is_hidden_from_dashboard=False)

    if query:
        # When searching, search across all patients (both scheduled and pending)
        # to allow staff (Front desk, Nursing, Physics, Doctors) to search and check schedule status and date!
        patient_qs = Patient.objects.filter(base_filter).distinct().select_related(
            'primary_consultant'
        ).prefetch_related('schedules__machine', 'schedules__shift').order_by('-created_at')
        if query.isdigit():
            patient_qs = patient_qs.filter(
                Q(mr_no=int(query)) | Q(phone_no__icontains=query)
            )
        else:
            patient_qs = patient_qs.filter(
                Q(pt_name__icontains=query) |
                Q(rt_no__icontains=query) |
                Q(diagnosis__icontains=query) |
                Q(primary_consultant__name__icontains=query)
            )
    else:
        # Default view: Non-scheduled patients (pending patients awaiting scheduling)
        # Front desk and nursing need not see the Pending Patients list
        if not can_view_pending_patients(request.user):
            patient_qs = Patient.objects.none()
        else:
            patient_qs = Patient.objects.filter(
                base_filter & (Q(schedules__isnull=True) | Q(schedules__date_of_start__isnull=True))
            ).distinct().select_related('primary_consultant').prefetch_related('schedules__machine', 'schedules__shift').order_by('-created_at')

    hidden_count = Patient.objects.filter(
        is_hidden_from_dashboard=True
    ).filter(
        Q(schedules__isnull=True) | Q(schedules__date_of_start__isnull=True)
    ).distinct().count() if user_is_admin else 0

    paginator = Paginator(patient_qs, 15)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Annotate schedule status and schedule date for display
    for p in page_obj:
        scheds = list(p.schedules.all())
        active_candidates = [
            s for s in scheds
            if (s.status in ('Scheduled', 'In Progress', 'Emergency', 'Vacant') or s.is_emergency_slot)
        ]
        draft_candidates = [s for s in scheds if s.status == 'Draft']
        completed_candidates = [
            s for s in scheds
            if s.status == 'Completed' or (s.actual_treatment_status and s.actual_treatment_status.is_completed)
        ]

        if active_candidates:
            active_sched = active_candidates[0]
        elif draft_candidates:
            active_sched = draft_candidates[0]
        elif completed_candidates:
            active_sched = completed_candidates[0]
        elif scheds:
            active_sched = next((s for s in scheds if s.date_of_start), scheds[0])
        else:
            active_sched = None

        if active_sched and active_sched.date_of_start:
            p.dashboard_schedule_status = active_sched.status or ('Emergency' if active_sched.is_emergency_slot else 'Scheduled')
            p.dashboard_schedule_date = active_sched.date_of_start.strftime("%d-%b-%Y")
            p.dashboard_machine = active_sched.machine.name if active_sched.machine else ''
            p.is_scheduled_active = True
        elif active_sched:
            p.dashboard_schedule_status = active_sched.status or 'Pending'
            p.dashboard_schedule_date = 'Unscheduled'
            p.dashboard_machine = active_sched.machine.name if active_sched.machine else ''
            p.is_scheduled_active = (active_sched.status in ('Completed', 'In Progress'))
        else:
            p.dashboard_schedule_status = 'Pending'
            p.dashboard_schedule_date = 'Unscheduled'
            p.dashboard_machine = ''
            p.is_scheduled_active = False

    # Pending User Account Approvals for Admin (strictly Admin only, not SubAdmin)
    pending_accounts = []
    hidden_accounts_count = 0
    active_pending_count = 0
    total_pending_count = 0

    if user_is_admin:
        pending_base = UserProfile.objects.select_related('user', 'role').filter(is_approved=False)
        total_pending_count = pending_base.count()
        hidden_accounts_count = pending_base.filter(is_hidden_from_dashboard=True).count()
        active_pending_count = pending_base.filter(is_hidden_from_dashboard=False).count()

        if show_hidden_accounts:
            pending_accounts = pending_base.filter(is_hidden_from_dashboard=True).order_by('-user__date_joined')
        else:
            pending_accounts = pending_base.filter(is_hidden_from_dashboard=False).order_by('-user__date_joined')

    context = {
        'page_obj': page_obj,
        'query': query,
        'show_hidden': show_hidden,
        'hidden_count': hidden_count,
        'total_non_scheduled': paginator.count,
        'pending_accounts': pending_accounts,
        'show_hidden_accounts': show_hidden_accounts,
        'hidden_accounts_count': hidden_accounts_count,
        'active_pending_count': active_pending_count,
        'total_pending_count': total_pending_count,
        'all_active_roles': Role.objects.filter(is_active=True).order_by('display_order') if user_is_admin else [],
        'prefixes': ['Dr.', 'Mr', 'Mrs', 'Miss'],
    }
    return render(request, 'core/dashboard.html', context)


@login_required
def user_list_view(request):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    query = request.GET.get('q', '').strip()
    role_id = request.GET.get('role_id', '').strip()
    status_filter = request.GET.get('status', '').strip()
    show_hidden_accounts = request.GET.get('show_hidden_accounts') == '1'

    users_qs = User.objects.select_related('profile__role').order_by('username')

    if query:
        users_qs = users_qs.filter(
            Q(username__icontains=query) |
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query)
        )

    if role_id:
        users_qs = users_qs.filter(profile__role__role_id=role_id)

    if status_filter == 'active':
        users_qs = users_qs.filter(is_active=True, profile__is_approved=True)
    elif status_filter == 'inactive':
        users_qs = users_qs.filter(is_active=False, profile__is_approved=True)
    elif status_filter == 'pending':
        users_qs = users_qs.filter(profile__is_approved=False)
    elif status_filter == 'hidden':
        users_qs = users_qs.filter(profile__is_hidden_from_dashboard=True)

    if show_hidden_accounts:
        users_qs = users_qs.filter(profile__is_hidden_from_dashboard=True)

    roles = Role.objects.filter(is_active=True).order_by('display_order')
    is_admin = is_admin_only(request.user)

    pending_approvals_count = UserProfile.objects.filter(is_approved=False).count()
    hidden_users_count = UserProfile.objects.filter(is_hidden_from_dashboard=True).count()

    context = {
        'users': users_qs,
        'roles': roles,
        'query': query,
        'selected_role_id': int(role_id) if role_id.isdigit() else None,
        'status_filter': status_filter,
        'is_admin': is_admin,
        'show_hidden_accounts': show_hidden_accounts,
        'hidden_users_count': hidden_users_count,
        'pending_approvals_count': pending_approvals_count,
        'total_count': users_qs.count(),
    }
    return render(request, 'core/user_list.html', context)


@login_required
def user_approve_view(request, user_id):
    """
    Approve pending user account registration. Strictly Admin only (not SubAdmin).
    Shows submitted profile details before approval to verify role and prevent unauthorized admin rights.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access denied. Only Administrators can approve user accounts.")
        return redirect('dashboard')

    target_user = get_object_or_404(User, id=user_id)
    profile, _ = UserProfile.objects.get_or_create(user=target_user)
    roles = Role.objects.filter(is_active=True).order_by('display_order')
    prefixes = ['Dr.', 'Mr', 'Mrs', 'Miss']

    if request.method == 'POST':
        prefix = request.POST.get('prefix', profile.prefix or 'Dr.').strip()
        fname = request.POST.get('first_name', target_user.first_name).strip()
        role_id = request.POST.get('role_id', '').strip()

        if fname:
            target_user.first_name = fname

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id.isdigit() else profile.role
        if not role_obj:
            role_obj = profile.role

        target_user.is_staff = bool(role_obj and role_obj.role_name.lower() in ('admin', 'subadmin'))
        target_user.is_active = True
        target_user.save()

        orig_role = profile.role.role_name if profile.role else 'None'
        profile.prefix = prefix
        profile.role = role_obj
        profile.user_level = role_obj.role_name.upper() if role_obj else 'USER'
        profile.is_approved = True
        profile.approved_by = request.user
        profile.approved_at = timezone.now()
        profile.save()

        AuditLog.objects.create(
            user=request.user,
            action="USER_APPROVED",
            details=f"Admin {request.user.username} approved user {target_user.username} ({profile.display_name}) with assigned role: {role_obj.role_name if role_obj else 'None'} (Requested: {orig_role}).",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"User account '{profile.display_name}' ({target_user.username}) approved and activated with role '{role_obj.role_name if role_obj else 'None'}'.")
        next_url = request.GET.get('next') or request.POST.get('next') or request.META.get('HTTP_REFERER')
        if next_url and ('/dashboard' in next_url or '/users' in next_url):
            return redirect(next_url)
        return redirect('dashboard')

    # If GET with direct=1 (e.g. programmatic / backward compatibility):
    if request.GET.get('direct') == '1':
        target_user.is_active = True
        target_user.save()
        profile.is_approved = True
        profile.approved_by = request.user
        profile.approved_at = timezone.now()
        profile.save()
        AuditLog.objects.create(
            user=request.user,
            action="USER_APPROVED",
            details=f"Admin {request.user.username} approved and activated user account {target_user.username} ({profile.display_name}).",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        messages.success(request, f"User account '{profile.display_name}' ({target_user.username}) approved and activated successfully.")
        next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
        if next_url and ('/dashboard' in next_url or '/users' in next_url):
            return redirect(next_url)
        return redirect('dashboard')

    # Normal GET: Render the review & approve confirmation template
    return render(request, 'core/user_approve_confirm.html', {
        'target_user': target_user,
        'profile': profile,
        'roles': roles,
        'prefixes': prefixes,
        'next_url': request.GET.get('next') or request.META.get('HTTP_REFERER') or '/dashboard/',
    })


@login_required
def user_toggle_hide_dashboard_view(request, user_id):
    """
    Hide or Unhide a user account from dashboard. Strictly Admin only (not SubAdmin).
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access denied. Only Administrators can hide accounts from dashboard.")
        return redirect('dashboard')

    target_user = get_object_or_404(User, id=user_id)
    if target_user.id == request.user.id:
        messages.error(request, "You cannot hide your own administrative account.")
        return redirect('dashboard')

    profile, _ = UserProfile.objects.get_or_create(user=target_user)
    profile.is_hidden_from_dashboard = not profile.is_hidden_from_dashboard

    if profile.is_hidden_from_dashboard:
        profile.hidden_by = request.user
        profile.hidden_at = timezone.now()
        status_action = "USER_DASHBOARD_HIDDEN"
        msg = f"User '{profile.display_name}' ({target_user.username}) has been hidden from dashboard."
    else:
        profile.hidden_by = None
        profile.hidden_at = None
        status_action = "USER_DASHBOARD_UNHIDDEN"
        msg = f"User '{profile.display_name}' ({target_user.username}) is now visible on dashboard."
    profile.save()

    AuditLog.objects.create(
        user=request.user,
        action=status_action,
        details=f"Admin {request.user.username} toggled dashboard visibility for user {target_user.username} (Hidden: {profile.is_hidden_from_dashboard}).",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    messages.success(request, msg)
    next_url = request.GET.get('next') or request.META.get('HTTP_REFERER')
    if next_url and ('/dashboard' in next_url or '/users' in next_url):
        return redirect(next_url)
    return redirect('dashboard')


@login_required
def user_toggle_active_view(request, user_id):
    """
    Toggle user active/inactive status. Strictly restricted to Admin only.
    Inactive users are blocked from login and hidden from clinical dropdowns.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access denied. Only Administrators can activate or inactivate user accounts.")
        return redirect('user_list')

    target_user = get_object_or_404(User, id=user_id)
    if target_user.id == request.user.id:
        messages.error(request, "You cannot deactivate your own administrative account.")
        return redirect('user_list')

    target_user.is_active = not target_user.is_active
    target_user.save()

    status_str = "activated" if target_user.is_active else "deactivated"
    AuditLog.objects.create(
        user=request.user,
        action=f"USER_{status_str.upper()}",
        details=f"Admin {request.user.username} {status_str} account for user {target_user.username}.",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    messages.success(request, f"User '{target_user.username}' has been successfully {status_str}.")
    return redirect('user_list')


@login_required
def user_create_view(request):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    roles = Role.objects.filter(is_active=True).order_by('display_order')
    prefixes = ['Dr.', 'Mr', 'Mrs', 'Miss']

    if request.method == 'POST':
        uname = request.POST.get('username', '').strip().lower()
        prefix = request.POST.get('prefix', 'Dr.').strip()
        fname = request.POST.get('first_name', '').strip()
        pword = request.POST.get('password', '').strip()
        role_id = request.POST.get('role_id')

        if not uname or not pword:
            messages.error(request, "Username and Password are required.")
            return render(request, 'core/user_form.html', {'roles': roles, 'prefixes': prefixes})

        if User.objects.filter(username=uname).exists():
            messages.error(request, f"Username '{uname}' already exists.")
            return render(request, 'core/user_form.html', {'roles': roles, 'prefixes': prefixes})

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id else None
        is_staff = bool(role_obj and role_obj.role_name.lower() in ('admin', 'subadmin'))

        user = User.objects.create_user(username=uname, first_name=fname, password=pword, is_staff=is_staff)
        UserProfile.objects.create(
            user=user,
            prefix=prefix,
            role=role_obj,
            user_level=role_obj.role_name.upper() if role_obj else 'USER'
        )

        AuditLog.objects.create(
            user=request.user,
            action="USER_CREATE",
            details=f"Created user {uname} with role {role_obj.role_name if role_obj else 'None'} and prefix {prefix}",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"User '{prefix} {fname or uname}' created successfully with role '{role_obj.role_name if role_obj else 'None'}'.")
        return redirect('user_list')

    return render(request, 'core/user_form.html', {'roles': roles, 'prefixes': prefixes})


@login_required
def user_edit_view(request, user_id):
    if not can_manage_users(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    target_user = get_object_or_404(User, id=user_id)
    roles = Role.objects.filter(is_active=True).order_by('display_order')
    prefixes = ['Dr.', 'Mr', 'Mrs', 'Miss']
    user_is_admin = is_admin_only(request.user)

    if request.method == 'POST':
        prefix = request.POST.get('prefix', 'Dr.').strip()
        fname = request.POST.get('first_name', '').strip()
        new_pwd = request.POST.get('password', '').strip()
        role_id = request.POST.get('role_id')

        target_user.first_name = fname
        
        # Only Admin can toggle active status
        if user_is_admin and target_user.id != request.user.id:
            target_user.is_active = (request.POST.get('is_active') == 'on')

        if new_pwd:
            target_user.set_password(new_pwd)

        role_obj = Role.objects.filter(role_id=role_id).first() if role_id else None
        target_user.is_staff = bool(role_obj and role_obj.role_name.lower() in ('admin', 'subadmin'))
        target_user.save()

        profile, _ = UserProfile.objects.get_or_create(user=target_user)
        profile.prefix = prefix
        profile.role = role_obj
        profile.user_level = role_obj.role_name.upper() if role_obj else 'USER'
        profile.save()

        AuditLog.objects.create(
            user=request.user,
            action="USER_UPDATE",
            details=f"Updated user account {target_user.username} (Prefix: {prefix}, Active: {target_user.is_active})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"User '{target_user.username}' updated successfully.")
        return redirect('user_list')

    return render(request, 'core/user_form.html', {
        'target_user': target_user,
        'roles': roles,
        'prefixes': prefixes,
        'is_admin': user_is_admin
    })


@login_required
def role_dashboard_view(request):
    """
    Role-Based Access Control (RBAC) Management Dashboard.
    Strictly restricted to Admin.
    Lists all system roles with their active status, user counts, and key capabilities.
    Provides direct access to edit permissions and access capabilities for each role.
    """
    if not can_manage_roles(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    roles = Role.objects.all().order_by('display_order', 'role_name')
    for r in roles:
        r.users_count = User.objects.filter(profile__role=r).count()
        r.active_users_count = User.objects.filter(profile__role=r, is_active=True).count()

    total_roles = roles.count()
    active_roles = sum(1 for r in roles if r.is_active)
    sched_roles = sum(1 for r in roles if r.cap_schedule_patient)

    context = {
        'roles': roles,
        'total_roles': total_roles,
        'active_roles': active_roles,
        'sched_roles': sched_roles,
    }
    return render(request, 'core/role_dashboard.html', context)


@login_required
def role_edit_view(request, role_id=None):
    """
    Dedicated Role & Permissions Editor Dashboard.
    Allows Administrators to configure granular capabilities and access rights for any role.
    """
    if not can_manage_roles(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    is_new = (role_id is None)
    if is_new:
        role = None
        assigned_users = []
    else:
        role = get_object_or_404(Role, role_id=role_id)
        assigned_users = User.objects.filter(profile__role=role).select_related('profile').order_by('first_name', 'username')

    if request.method == 'POST':
        role_name = request.POST.get('role_name', '').strip()
        role_description = request.POST.get('role_description', '').strip()
        display_order_raw = request.POST.get('display_order', '1').strip()
        is_active = (request.POST.get('is_active') == '1')

        try:
            display_order = int(display_order_raw)
        except ValueError:
            display_order = 1

        if not role_name:
            messages.error(request, "Role Name is required.")
            return render(request, 'core/role_form.html', {
                'role': role,
                'is_new': is_new,
                'assigned_users': assigned_users,
                'form_data': request.POST,
            })

        # Name uniqueness
        name_qs = Role.objects.filter(role_name__iexact=role_name)
        if not is_new:
            name_qs = name_qs.exclude(role_id=role.role_id)
        if name_qs.exists():
            messages.error(request, f"A role named '{role_name}' already exists.")
            return render(request, 'core/role_form.html', {
                'role': role,
                'is_new': is_new,
                'assigned_users': assigned_users,
                'form_data': request.POST,
            })

        # Capability checkboxes
        cap_login = (request.POST.get('cap_login') == '1')
        cap_view_patients = (request.POST.get('cap_view_patients') == '1')
        cap_view_pending_queue = (request.POST.get('cap_view_pending_queue') == '1')
        cap_register_patient = (request.POST.get('cap_register_patient') == '1')
        cap_schedule_patient = (request.POST.get('cap_schedule_patient') == '1')
        cap_edit_patient = (request.POST.get('cap_edit_patient') == '1')
        cap_edit_locked_identity = (request.POST.get('cap_edit_locked_identity') == '1')
        cap_manual_schedule = (request.POST.get('cap_manual_schedule') == '1')
        cap_manage_mappings = (request.POST.get('cap_manage_mappings') == '1')
        cap_manage_lookups = (request.POST.get('cap_manage_lookups') == '1')
        cap_manage_users = (request.POST.get('cap_manage_users') == '1')
        cap_manage_roles = (request.POST.get('cap_manage_roles') == '1')
        cap_delete_records = (request.POST.get('cap_delete_records') == '1')
        cap_export_data = (request.POST.get('cap_export_data') == '1')
        cap_view_reports = (request.POST.get('cap_view_reports') == '1')

        # Admin safety lock
        if not is_new and (role.role_id == 1 or role.role_name.lower() == 'admin'):
            is_active = True
            cap_login = True
            cap_manage_roles = True
            cap_manage_users = True

        if is_new:
            new_id = (Role.objects.aggregate(Max('role_id'))['role_id__max'] or 0) + 1
            role = Role.objects.create(
                role_id=new_id,
                role_name=role_name,
                role_description=role_description,
                display_order=display_order,
                is_active=is_active,
                cap_login=cap_login,
                cap_view_patients=cap_view_patients,
                cap_view_pending_queue=cap_view_pending_queue,
                cap_register_patient=cap_register_patient,
                cap_schedule_patient=cap_schedule_patient,
                cap_edit_patient=cap_edit_patient,
                cap_edit_locked_identity=cap_edit_locked_identity,
                cap_manual_schedule=cap_manual_schedule,
                cap_manage_mappings=cap_manage_mappings,
                cap_manage_lookups=cap_manage_lookups,
                cap_manage_users=cap_manage_users,
                cap_manage_roles=cap_manage_roles,
                cap_delete_records=cap_delete_records,
                cap_export_data=cap_export_data,
                cap_view_reports=cap_view_reports,
            )
            AuditLog.objects.create(
                user=request.user,
                action="ROLE_CREATE",
                details=f"Created new role '{role.role_name}' (ID: {role.role_id}) with custom capabilities.",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f"New role '{role.role_name}' created successfully!")
        else:
            role.role_name = role_name
            role.role_description = role_description
            role.display_order = display_order
            role.is_active = is_active
            role.cap_login = cap_login
            role.cap_view_patients = cap_view_patients
            role.cap_view_pending_queue = cap_view_pending_queue
            role.cap_register_patient = cap_register_patient
            role.cap_schedule_patient = cap_schedule_patient
            role.cap_edit_patient = cap_edit_patient
            role.cap_edit_locked_identity = cap_edit_locked_identity
            role.cap_manual_schedule = cap_manual_schedule
            role.cap_manage_mappings = cap_manage_mappings
            role.cap_manage_lookups = cap_manage_lookups
            role.cap_manage_users = cap_manage_users
            role.cap_manage_roles = cap_manage_roles
            role.cap_delete_records = cap_delete_records
            role.cap_export_data = cap_export_data
            role.cap_view_reports = cap_view_reports
            role.save()

            AuditLog.objects.create(
                user=request.user,
                action="ROLE_UPDATE",
                details=f"Updated access capabilities for role '{role.role_name}' (ID: {role.role_id}).",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f"Access permissions and capabilities for role '{role.role_name}' updated successfully!")

        return redirect('role_dashboard')

    context = {
        'role': role,
        'is_new': is_new,
        'assigned_users': assigned_users,
    }
    return render(request, 'core/role_form.html', context)


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

    cfg = dict(cfg)
    if category == 'machine':
        from apps.scheduler.models import MachineType
        active_types = list(MachineType.objects.filter(is_active=True).order_by('display_order', 'name').values_list('name', flat=True))
        for fallback in ['LINAC', 'Brachytherapy', 'Orthovoltage', 'Intra Op', 'PBT']:
            if fallback not in active_types:
                active_types.append(fallback)
        cfg['extra_choices'] = [(t, t) for t in active_types]

    raw_items = model.objects.all().order_by('display_order', cfg['name_field'])
    items = []
    default_item = None
    extra2_field = cfg.get('extra2_field')

    for it in raw_items:
        is_def = (str(it.pk) == str(default_id)) if default_id is not None else False
        item_dict = {
            'pk': it.pk,
            'name': getattr(it, cfg['name_field']),
            'secondary': getattr(it, cfg['secondary_field']) if cfg.get('secondary_field') else None,
            'extra': getattr(it, cfg['extra_field']) if cfg.get('extra_field') else None,
            'extra2': getattr(it, extra2_field) if extra2_field else None,
            'treatment_mode': getattr(it, 'treatment_mode', None),
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
    extra2_field = cfg.get('extra2_field')

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
        extra2_val = request.POST.get('extra2_value', '').strip() if extra2_field else None
        if not extra2_val and category == 'machine':
            extra2_val = request.POST.get('treatment_mode', '').strip()
        order_val = int(request.POST.get('display_order') or 1)
        active_val = bool(request.POST.get('is_active'))
        is_default_checked = bool(request.POST.get('is_default'))

        if not name_val:
            messages.error(request, f"{cfg['name_label']} cannot be empty.")
            return redirect('lookup_manager', category=category)

        if not item:
            # Check unique name
            if model.objects.filter(**{f"{name_field}__iexact": name_val}).exists():
                messages.error(request, f"An option named '{name_val}' already exists in {cfg['title']}.")
                return redirect('lookup_manager', category=category)

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
            if extra2_field and extra2_val:
                create_kwargs[extra2_field] = extra2_val
            elif category == 'machine':
                create_kwargs['treatment_mode'] = 'Teletherapy'

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
            if extra2_field and extra2_val:
                setattr(item, extra2_field, extra2_val)
            elif category == 'machine' and request.POST.get('treatment_mode'):
                item.treatment_mode = request.POST.get('treatment_mode').strip()
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


@login_required
def lookup_reorder_view(request, category):
    """
    Dynamically reorders options in the specified lookup category via drag-and-drop.
    Updates display_order for all provided item IDs sequentially.
    """
    if not can_manage_lookups(request.user):
        return JsonResponse({'success': False, 'error': "Access denied. Administrator privileges required."}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': "POST method required."}, status=405)

    model, cfg = get_lookup_model(category)
    if not model or not cfg:
        return JsonResponse({'success': False, 'error': f"Invalid lookup category '{category}'."}, status=400)

    try:
        if request.body:
            try:
                data = json.loads(request.body.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                data = {}
        else:
            data = {}
        order_list = data.get('item_ids') or request.POST.getlist('item_ids[]') or request.POST.getlist('item_ids')
    except Exception as e:
        return JsonResponse({'success': False, 'error': f"Invalid request payload: {str(e)}"}, status=400)

    if not order_list:
        return JsonResponse({'success': False, 'error': "No item IDs provided for reordering."}, status=400)

    pk_field = cfg['pk']
    updated_count = 0

    for index, raw_id in enumerate(order_list):
        try:
            item_pk = int(raw_id)
        except (ValueError, TypeError):
            continue

        try:
            item = model.objects.get(**{pk_field: item_pk})
            new_order = index + 1
            if getattr(item, 'display_order', None) != new_order:
                item.display_order = new_order
                item.save(update_fields=['display_order'])
                updated_count += 1
        except model.DoesNotExist:
            continue

    AuditLog.objects.create(
        user=request.user,
        action="LOOKUP_REORDER",
        details=f"Dynamically reordered {len(order_list)} options in {cfg['title']} via drag-and-drop.",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    return JsonResponse({
        'success': True,
        'message': f"Order for {cfg['title']} updated successfully.",
        'category': category,
        'count': len(order_list),
        'updated_count': updated_count
    })


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
    rt_config = get_or_create_field_config('rt_no')
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
        if mr_exact is not None and mr_exact.strip().isdigit():
            mr_config.exact_length = int(mr_exact.strip())
        elif mr_mode == 'exact' and mr_exact == '':
            mr_config.exact_length = None

        if mr_min is not None and mr_min.strip().isdigit():
            mr_config.min_length = int(mr_min.strip())
        elif mr_mode == 'range' and mr_min == '':
            mr_config.min_length = None

        if mr_max is not None and mr_max.strip().isdigit():
            mr_config.max_length = int(mr_max.strip())
        elif mr_mode == 'range' and mr_max == '':
            mr_config.max_length = None

        mr_config.numeric_only = mr_numeric
        mr_config.allow_blank = mr_allow_blank
        mr_config.custom_error_message = mr_custom_err or None
        mr_config.save()

        # Update RT No configuration
        if 'rt_validation_mode' in request.POST:
            rt_mode = request.POST.get('rt_validation_mode', 'range')
            rt_exact = request.POST.get('rt_exact_length')
            rt_min = request.POST.get('rt_min_length')
            rt_max = request.POST.get('rt_max_length')
            rt_numeric = request.POST.get('rt_numeric_only') == '1'
            rt_allow_blank = request.POST.get('rt_allow_blank') == '1'
            rt_custom_err = request.POST.get('rt_custom_error_message', '').strip()

            rt_config.validation_mode = rt_mode
            if rt_exact is not None and rt_exact.strip().isdigit():
                rt_config.exact_length = int(rt_exact.strip())
            elif rt_mode == 'exact' and rt_exact == '':
                rt_config.exact_length = None

            if rt_min is not None and rt_min.strip().isdigit():
                rt_config.min_length = int(rt_min.strip())
            elif rt_mode == 'range' and rt_min == '':
                rt_config.min_length = None

            if rt_max is not None and rt_max.strip().isdigit():
                rt_config.max_length = int(rt_max.strip())
            elif rt_mode == 'range' and rt_max == '':
                rt_config.max_length = None

            rt_config.numeric_only = rt_numeric
            rt_config.allow_blank = rt_allow_blank
            rt_config.custom_error_message = rt_custom_err or None
            rt_config.save()

        # Update Phone No configuration
        ph_mode = request.POST.get('ph_validation_mode', 'range')
        ph_exact = request.POST.get('ph_exact_length')
        ph_min = request.POST.get('ph_min_length')
        ph_max = request.POST.get('ph_max_length')
        ph_numeric = request.POST.get('ph_numeric_only') == '1'
        ph_allow_blank = request.POST.get('ph_allow_blank') == '1'
        ph_custom_err = request.POST.get('ph_custom_error_message', '').strip()

        phone_config.validation_mode = ph_mode
        if ph_exact is not None and ph_exact.strip().isdigit():
            phone_config.exact_length = int(ph_exact.strip())
        elif ph_mode == 'exact' and ph_exact == '':
            phone_config.exact_length = None

        if ph_min is not None and ph_min.strip().isdigit():
            phone_config.min_length = int(ph_min.strip())
        elif ph_mode == 'range' and ph_min == '':
            phone_config.min_length = None

        if ph_max is not None and ph_max.strip().isdigit():
            phone_config.max_length = int(ph_max.strip())
        elif ph_mode == 'range' and ph_max == '':
            phone_config.max_length = None

        phone_config.numeric_only = ph_numeric
        phone_config.allow_blank = ph_allow_blank
        phone_config.custom_error_message = ph_custom_err or None
        phone_config.save()

        AuditLog.objects.create(
            user=request.user,
            action="FIELD_VALIDATION_CONFIG_UPDATE",
            details="Updated validation rules for MR Number, RT Number, and Phone Number.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, "Field validation configurations updated successfully!")
        return redirect('validation_config')

    context = {
        'mr_config': mr_config,
        'rt_config': rt_config,
        'phone_config': phone_config,
    }
    return render(request, 'core/validation_config.html', context)

