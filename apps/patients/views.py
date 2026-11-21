from datetime import datetime, date
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q

from django.utils import timezone
from .models import Patient, ClinicalUnit, SEX_CHOICES, DEFAULT_SITES
from apps.core.models import Consultant, AuditLog
from apps.scheduler.models import TreatmentSchedule
from apps.core.permissions import (
    can_edit_locked_identity, can_delete_patient, can_register_patient,
    is_admin_only, can_edit_patient, can_schedule_patient
)
from apps.core.validators import validate_field_value


@login_required
def patient_register_view(request):
    """
    Patient Registration View (Stage 1).
    Strictly captures: Patient Name, MR No, RT No, Age, Sex, Diagnosis,
    Primary consultant, Phone Number.
    Site of treatment is captured during Treatment Scheduling (Stage 2).
    """
    if not can_register_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to register patients.")
        return redirect('dashboard')

    consultants = Consultant.objects.filter(is_active=True).order_by('display_order', 'name')
    units = ClinicalUnit.objects.filter(is_active=True).order_by('display_order')
    today_date = timezone.localdate().strftime('%Y-%m-%d')

    if request.method == 'POST':
        mr_no_raw = request.POST.get('mr_no', '').strip()
        pt_name = request.POST.get('pt_name', '').strip()
        date_of_reg_raw = request.POST.get('date_of_registration', '').strip()
        rt_no = request.POST.get('rt_no', '').strip()
        age_raw = request.POST.get('age', '').strip()
        sex = request.POST.get('sex', '').strip()
        diagnosis = request.POST.get('diagnosis', '').strip()
        consultant_id = request.POST.get('primary_consultant')
        phone_no = request.POST.get('phone_no', '').strip()
        alt_phone_no = request.POST.get('alt_phone_no', '').strip()

        # Configurable field validations for MR No, RT No, and Phone No
        is_mr_valid, mr_err = validate_field_value('mr_no', mr_no_raw)
        if not is_mr_valid:
            messages.error(request, mr_err)
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        if not rt_no:
            messages.error(request, "RT Number is mandatory.")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        is_rt_valid, rt_err = validate_field_value('rt_no', rt_no)
        if not is_rt_valid:
            messages.error(request, rt_err)
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        if not phone_no:
            messages.error(request, "Phone Number is mandatory.")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        if not pt_name:
            messages.error(request, "Patient Name is mandatory.")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        try:
            mr_no = int(mr_no_raw)
        except ValueError:
            messages.error(request, "MR Number must be a valid integer number (e.g. 20260000123).")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'units': units,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
                'today_date': today_date,
            })

        # Check uniqueness of MR No
        existing = Patient.objects.filter(mr_no=mr_no).first()
        if existing:
            if can_schedule_patient(request.user):
                messages.warning(
                    request,
                    f"Patient with MR No. {mr_no} is already registered ({existing.pt_name}). You can schedule treatment directly below."
                )
                return redirect('schedule_patient', patient_id=existing.id)
            else:
                messages.warning(
                    request,
                    f"Patient with MR No. {mr_no} is already registered ({existing.pt_name})."
                )
                return redirect('patient_detail', patient_id=existing.id)

        age = int(float(age_raw)) if age_raw else None
        unit = request.POST.get('unit', 'I').strip() or 'I'
        consultant_obj = Consultant.objects.filter(id=consultant_id).first() if consultant_id else None

        reg_date = None
        if date_of_reg_raw:
            try:
                reg_date = datetime.strptime(date_of_reg_raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        if not reg_date:
            reg_date = timezone.localdate()

        patient = Patient.objects.create(
            mr_no=mr_no,
            pt_name=pt_name,
            date_of_registration=reg_date,
            rt_no=rt_no,
            unit=unit,
            age=age,
            sex=sex or 'M',
            diagnosis=diagnosis or 'Under Evaluation',
            site_of_treatment=None,  # Moved to Scheduling form
            primary_consultant=consultant_obj,
            phone_no=phone_no,
            alt_phone_no=alt_phone_no,
            created_by=request.user
        )

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_REGISTER",
            mr_no=str(patient.mr_no),
            details=f"Registered patient {patient.pt_name} (MR: {patient.mr_no}, Reg Date: {patient.date_of_registration.strftime('%d-%m-%Y') if patient.date_of_registration else '-'})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {patient.pt_name} (MR: {patient.mr_no}) registered successfully!")

        # Scheduling choice: schedule now or later
        if 'schedule_now' in request.POST and can_schedule_patient(request.user):
            return redirect('schedule_patient', patient_id=patient.id)
        else:
            return redirect('patient_detail', patient_id=patient.id)

    context = {
        'consultants': consultants,
        'units': units,
        'sex_choices': SEX_CHOICES,
        'form_data': {},
        'today_date': today_date,
    }
    return render(request, 'patients/patient_register.html', context)


@login_required
def patient_edit_view(request, patient_id):
    """
    Patient Edit View.
    Rule: MR No and Patient Name are LOCKED. Only Admin / SubAdmin can edit them.
    Age, Diagnosis, Site, Phone, etc. can be edited by authorized users.
    Front desk, nursing, and physics staff have read-only access.
    """
    if not can_edit_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to edit patient demographics.")
        return redirect('patient_detail', patient_id=patient_id)

    patient = get_object_or_404(Patient, id=patient_id)
    can_edit_identity = can_edit_locked_identity(request.user)
    consultants = Consultant.objects.filter(is_active=True).order_by('display_order', 'name')

    if request.method == 'POST':
        # Identity fields: only update if admin/subadmin
        if can_edit_identity:
            new_mr_raw = request.POST.get('mr_no', '').strip()
            new_name = request.POST.get('pt_name', '').strip()
            if new_mr_raw:
                is_mr_valid, mr_err = validate_field_value('mr_no', new_mr_raw)
                if not is_mr_valid:
                    messages.error(request, mr_err)
                    return redirect('patient_edit', patient_id=patient.id)
                try:
                    new_mr = int(new_mr_raw)
                    if new_mr != patient.mr_no and Patient.objects.filter(mr_no=new_mr).exclude(id=patient.id).exists():
                        messages.error(request, f"MR No {new_mr} already belongs to another patient!")
                        return redirect('patient_edit', patient_id=patient.id)
                    patient.mr_no = new_mr
                except ValueError:
                    messages.error(request, "MR Number must be a valid integer.")
                    return redirect('patient_edit', patient_id=patient.id)
            if new_name:
                patient.pt_name = new_name

        phone_no = request.POST.get('phone_no', '').strip()
        rt_no = request.POST.get('rt_no', '').strip()
        alt_phone_no = request.POST.get('alt_phone_no', '').strip()

        if not phone_no:
            messages.error(request, "Phone Number is mandatory.")
            return redirect('patient_edit', patient_id=patient.id)

        if not rt_no:
            messages.error(request, "RT Number is mandatory.")
            return redirect('patient_edit', patient_id=patient.id)

        is_rt_valid, rt_err = validate_field_value('rt_no', rt_no)
        if not is_rt_valid:
            messages.error(request, rt_err)
            return redirect('patient_edit', patient_id=patient.id)

        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return redirect('patient_edit', patient_id=patient.id)
        if alt_phone_no:
            is_alt_valid, alt_err = validate_field_value('phone_no', alt_phone_no)
            if not is_alt_valid:
                messages.error(request, f"Alternate Phone: {alt_err}")
                return redirect('patient_edit', patient_id=patient.id)

        # Other clinical fields
        date_of_reg_raw = request.POST.get('date_of_registration', '').strip()
        if date_of_reg_raw:
            try:
                patient.date_of_registration = datetime.strptime(date_of_reg_raw, '%Y-%m-%d').date()
            except ValueError:
                pass

        unit = request.POST.get('unit', '').strip()
        if unit:
            patient.unit = unit
        age_raw = request.POST.get('age', '').strip()
        patient.age = int(float(age_raw)) if age_raw else None
        patient.rt_no = rt_no
        patient.sex = request.POST.get('sex', patient.sex)
        patient.diagnosis = request.POST.get('diagnosis', '').strip()
        patient.site_of_treatment = request.POST.get('site_of_treatment', patient.site_of_treatment)
        patient.phone_no = phone_no
        patient.alt_phone_no = alt_phone_no or None

        consultant_id = request.POST.get('primary_consultant')
        patient.primary_consultant = Consultant.objects.filter(id=consultant_id).first() if consultant_id else None

        patient.save()

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_UPDATE",
            mr_no=str(patient.mr_no),
            details=f"Updated demographics for {patient.pt_name} (MR: {patient.mr_no})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {patient.pt_name} updated successfully.")
        return redirect('patient_detail', patient_id=patient.id)

    context = {
        'patient': patient,
        'can_edit_identity': can_edit_identity,
        'consultants': consultants,
        'units': ClinicalUnit.objects.filter(is_active=True).order_by('display_order', 'name'),
        'sex_choices': SEX_CHOICES,
        'site_choices': DEFAULT_SITES,
    }
    return render(request, 'patients/patient_edit.html', context)


@login_required
def patient_detail_view(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    raw_schedules = list(patient.schedules.select_related(
        'machine', 'shift', 'technique', 'special_technique', 'intent', 'tagged_to'
    ).order_by('id'))

    # Auto-heal / sync any existing one-way clubbed courses to be mutual (both ways)
    needs_refresh = False
    for s in raw_schedules:
        if s.tagged_to and not s.tagged_to.tagged_to:
            s.tagged_to.tagged_to = s
            s.tagged_to.save(update_fields=['tagged_to'])
            needs_refresh = True
        elif not s.tagged_to and s.tagged_courses.exists():
            s.tagged_to = s.tagged_courses.first()
            s.save(update_fields=['tagged_to'])
            needs_refresh = True

    if needs_refresh:
        raw_schedules = list(patient.schedules.select_related(
            'machine', 'shift', 'technique', 'special_technique', 'intent', 'tagged_to'
        ).order_by('id'))

    # Group clubbed courses together in table display order
    schedules = []
    visited_ids = set()
    for s in raw_schedules:
        if s.id in visited_ids:
            continue
        schedules.append(s)
        visited_ids.add(s.id)
        if s.tagged_to and s.tagged_to.id not in visited_ids:
            schedules.append(s.tagged_to)
            visited_ids.add(s.tagged_to.id)

    # Determine club_position for thin black connector line on left of clubbed rows
    for i, s in enumerate(schedules):
        if s.tagged_to:
            partner = s.tagged_to
            partner_idx = next((idx for idx, other in enumerate(schedules) if other.id == partner.id), None)
            if partner_idx is not None:
                if i < partner_idx:
                    s.club_position = 'top'
                elif i > partner_idx:
                    s.club_position = 'bottom'
                else:
                    s.club_position = None
            else:
                s.club_position = None
        else:
            s.club_position = None

    # Clinical Tracking Milestones:
    # 1. Registered date
    registered_date_display = patient.date_of_registration.strftime("%d-%b-%Y") if patient.date_of_registration else (patient.created_at.strftime("%d-%b-%Y") if patient.created_at else "-")

    # 2. Regular slot (EBRT) taken on
    reg_sched = next((s for s in raw_schedules if not s.is_emergency_slot and s.treatment_type == 'Teletherapy' and s.date_of_start), None)
    em_sched = next((s for s in raw_schedules if s.is_emergency_slot), None)

    if reg_sched and reg_sched.date_of_start:
        regular_slot_date_display = reg_sched.date_of_start.strftime("%d-%b-%Y")
    elif em_sched and em_sched.original_scheduled_date:
        regular_slot_date_display = em_sched.original_scheduled_date.strftime("%d-%b-%Y")
    elif any(s.date_of_start and not s.is_emergency_slot for s in raw_schedules):
        any_reg = next(s for s in raw_schedules if s.date_of_start and not s.is_emergency_slot)
        regular_slot_date_display = any_reg.date_of_start.strftime("%d-%b-%Y")
    else:
        regular_slot_date_display = "Not taken"

    # 3. Shifted to Emergency / Vacant slot on (if taken)
    if em_sched:
        em_date = em_sched.intended_start_date or em_sched.date_of_start
        if not em_date and em_sched.created_at:
            em_date = em_sched.created_at.date()
        emergency_slot_date_display = em_date.strftime("%d-%b-%Y") if em_date else "Yes"
    else:
        emergency_slot_date_display = None

    audit_logs = AuditLog.objects.filter(mr_no=str(patient.mr_no)).select_related('user').order_by('-log_datetime')[:50]

    from apps.scheduler.models import TreatmentCompletionStatus
    latest_schedule = schedules[-1] if schedules else None
    has_uncompleted_previous_course = bool(latest_schedule and not latest_schedule.actual_treatment_status)
    completion_statuses = TreatmentCompletionStatus.objects.filter(is_active=True).order_by('display_order', 'name')

    context = {
        'patient': patient,
        'schedules': schedules,
        'audit_logs': audit_logs,
        'registered_date_display': registered_date_display,
        'regular_slot_date_display': regular_slot_date_display,
        'emergency_slot_date_display': emergency_slot_date_display,
        'can_edit_identity': can_edit_locked_identity(request.user),
        'is_admin_user': is_admin_only(request.user),
        'latest_schedule': latest_schedule,
        'has_uncompleted_previous_course': has_uncompleted_previous_course,
        'completion_statuses': completion_statuses,
    }
    return render(request, 'patients/patient_detail.html', context)


@login_required
def patient_search_view(request):
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()

    has_searched = bool(query or status_filter)

    if not has_searched:
        patients = Patient.objects.none()
    else:
        patients = Patient.objects.select_related('primary_consultant').prefetch_related('schedules__machine', 'schedules__shift').all()

        if query:
            if query.isdigit():
                patients = patients.filter(Q(mr_no=int(query)) | Q(phone_no__icontains=query))
            else:
                patients = patients.filter(
                    Q(pt_name__icontains=query) |
                    Q(diagnosis__icontains=query) |
                    Q(rt_no__icontains=query) |
                    Q(primary_consultant__name__icontains=query)
                )

        if status_filter == 'unscheduled':
            patients = patients.filter(schedules__isnull=True)
        elif status_filter == 'scheduled':
            patients = patients.filter(schedules__isnull=False).distinct()

    paginator = Paginator(patients, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number) if has_searched else None

    if page_obj:
        for p in page_obj:
            schedules = list(p.schedules.all())
            active_candidates = [
                s for s in schedules
                if (s.status in ('Scheduled', 'In Progress', 'Emergency', 'Vacant') or s.is_emergency_slot)
            ]
            draft_candidates = [s for s in schedules if s.status == 'Draft']
            completed_candidates = [
                s for s in schedules
                if s.status == 'Completed' or (s.actual_treatment_status and s.actual_treatment_status.is_completed)
            ]

            if active_candidates:
                active_sched = active_candidates[0]
            elif draft_candidates:
                active_sched = draft_candidates[0]
            elif completed_candidates:
                active_sched = completed_candidates[0]
            elif schedules:
                active_sched = next((s for s in schedules if s.date_of_start), schedules[0])
            else:
                active_sched = None

            if active_sched:
                p.dashboard_schedule_status = active_sched.status or ('Emergency' if active_sched.is_emergency_slot else 'Scheduled')
                p.dashboard_schedule_date = active_sched.date_of_start.strftime("%d-%b-%Y") if active_sched.date_of_start else ("Intended: " + active_sched.intended_start_date.strftime("%d-%b-%Y") if active_sched.intended_start_date else "Unscheduled")
                p.dashboard_machine = active_sched.machine.name if active_sched.machine else "Unassigned"
            else:
                p.dashboard_schedule_status = "Pending"
                p.dashboard_schedule_date = "-"
                p.dashboard_machine = "-"

            # Clinical Tracking Milestones:
            # 1. Patient registered on:
            p.registered_date_display = p.date_of_registration.strftime("%d-%b-%Y") if p.date_of_registration else (p.created_at.strftime("%d-%b-%Y") if p.created_at else "-")

            # 2. Regular slot (EBRT) taken on:
            reg_sched = next((s for s in schedules if not s.is_emergency_slot and s.treatment_type == 'Teletherapy' and s.date_of_start), None)
            em_sched = next((s for s in schedules if s.is_emergency_slot), None)

            if reg_sched and reg_sched.date_of_start:
                p.regular_slot_display = reg_sched.date_of_start.strftime("%d-%b-%Y")
            elif em_sched and em_sched.original_scheduled_date:
                p.regular_slot_display = em_sched.original_scheduled_date.strftime("%d-%b-%Y")
            elif any(s.date_of_start and not s.is_emergency_slot for s in schedules):
                any_reg = next(s for s in schedules if s.date_of_start and not s.is_emergency_slot)
                p.regular_slot_display = any_reg.date_of_start.strftime("%d-%b-%Y")
            else:
                p.regular_slot_display = "Not taken"

            # 3. Shifted to Emergency / Vacant slot on (if taken):
            if em_sched:
                em_date = em_sched.intended_start_date or em_sched.date_of_start
                if not em_date and em_sched.created_at:
                    em_date = em_sched.created_at.date()
                p.emergency_slot_display = em_date.strftime("%d-%b-%Y") if em_date else "Yes"
            else:
                p.emergency_slot_display = None

    context = {
        'page_obj': page_obj,
        'has_searched': has_searched,
        'query': query,
        'status_filter': status_filter,
        'total_count': paginator.count if has_searched else 0,
    }
    return render(request, 'patients/patient_search.html', context)


@login_required
def patient_delete_view(request, patient_id):
    """
    Permanently delete a patient record and all associated treatment schedules.
    Strictly restricted to Admin (not SubAdmin).
    """
    if not can_delete_patient(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to delete patient records.")
        return redirect('dashboard')

    patient = get_object_or_404(Patient, id=patient_id)

    if request.method == 'POST':
        mr_no = patient.mr_no
        pt_name = patient.pt_name
        rt_no = patient.rt_no or '-'
        schedules_count = patient.schedules.count()

        patient.delete()

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_DELETE",
            mr_no=str(mr_no),
            details=f"Permanently deleted patient {pt_name} (MR: {mr_no}, RT: {rt_no}) and {schedules_count} schedule course(s).",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {pt_name} (MR: {mr_no}) was deleted successfully.")
        return redirect('dashboard')

    return render(request, 'patients/patient_confirm_delete.html', {'patient': patient})


@login_required
def patient_hide_dashboard_view(request, patient_id):
    """
    Hide a patient from the Main Dashboard queue.
    Strictly restricted to Admin (not SubAdmin).
    Requires a comment / reason.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to hide patients from the dashboard.")
        return redirect('dashboard')

    patient = get_object_or_404(Patient, id=patient_id)

    if request.method == 'POST':
        comment = request.POST.get('comment', '').strip()
        if not comment:
            messages.error(request, "A comment / reason is required to hide a patient from the Main Dashboard.")
            return redirect('dashboard')

        patient.is_hidden_from_dashboard = True
        patient.hidden_reason = comment
        patient.hidden_at = timezone.now()
        patient.hidden_by = request.user
        patient.save()

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_DASHBOARD_HIDDEN",
            mr_no=str(patient.mr_no),
            details=f"Patient {patient.pt_name} (MR: {patient.mr_no}) hidden from main dashboard by {request.user.username}. Reason: {comment}",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {patient.pt_name} (MR: {patient.mr_no}) has been hidden from the main dashboard.")

    return redirect('dashboard')


@login_required
def patient_unhide_dashboard_view(request, patient_id):
    """
    Restore a hidden patient back to the Main Dashboard queue.
    Strictly restricted to Admin (not SubAdmin).
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to unhide patients.")
        return redirect('dashboard')

    patient = get_object_or_404(Patient, id=patient_id)

    if request.method == 'POST':
        patient.is_hidden_from_dashboard = False
        patient.save()

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_DASHBOARD_UNHIDDEN",
            mr_no=str(patient.mr_no),
            details=f"Patient {patient.pt_name} (MR: {patient.mr_no}) unhidden and restored to main dashboard by {request.user.username}.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {patient.pt_name} (MR: {patient.mr_no}) has been restored to the main dashboard.")

    next_url = request.GET.get('next') or request.POST.get('next')
    if next_url:
        return redirect(next_url)
    return redirect('dashboard')


@login_required
def patient_batch_hide_dashboard_view(request):
    """
    Batch hide multiple non-scheduled patients from the Main Dashboard queue.
    Strictly restricted to Admin (not SubAdmin).
    Requires a comment / reason.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to hide patients from the dashboard.")
        return redirect('dashboard')

    if request.method == 'POST':
        comment = request.POST.get('comment', '').strip()
        if not comment:
            messages.error(request, "A comment / reason is required to hide patients from the Main Dashboard.")
            return redirect('dashboard')

        raw_ids = request.POST.getlist('selected_patient_ids')
        if not raw_ids:
            raw_ids_str = request.POST.get('patient_ids', '')
            if raw_ids_str:
                raw_ids = [i.strip() for i in raw_ids_str.split(',') if i.strip()]

        patient_ids = [int(i) for i in raw_ids if str(i).isdigit()]
        if not patient_ids:
            messages.error(request, "No patients were selected to hide.")
            return redirect('dashboard')

        patients = list(Patient.objects.filter(id__in=patient_ids))
        now = timezone.now()
        for p in patients:
            p.is_hidden_from_dashboard = True
            p.hidden_reason = comment
            p.hidden_at = now
            p.hidden_by = request.user
            p.save()

        mr_list_str = ', '.join(str(p.mr_no) for p in patients[:10])
        if len(patients) > 10:
            mr_list_str += f"... (+{len(patients) - 10} more)"

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_DASHBOARD_BATCH_HIDDEN",
            details=f"Admin {request.user.username} batch hid {len(patients)} patients from dashboard. MRs: [{mr_list_str}]. Reason: {comment}",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Successfully hid {len(patients)} patient(s) from the main dashboard.")

    return redirect('dashboard')


@login_required
def patient_hide_by_simulation_date_view(request):
    """
    Bulk hide unscheduled patients from the Main Dashboard whose simulation date is before a specified cutoff date.
    Strictly restricted to Admin (not SubAdmin).
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to hide patients from the dashboard.")
        return redirect('dashboard')

    if request.method == 'POST':
        cutoff_str = request.POST.get('cutoff_date', '').strip()
        comment = request.POST.get('comment', '').strip()

        if not cutoff_str:
            messages.error(request, "A simulation cutoff date is required.")
            return redirect('dashboard')

        try:
            cutoff_date = datetime.strptime(cutoff_str, '%Y-%m-%d').date()
        except ValueError:
            messages.error(request, f"Invalid date format: '{cutoff_str}'. Expected YYYY-MM-DD.")
            return redirect('dashboard')

        if not comment:
            comment = f"Bulk hidden: simulated before {cutoff_date.strftime('%d-%m-%Y')}"

        active_tele_patient_ids = TreatmentSchedule.objects.filter(
            treatment_type='Teletherapy',
            date_of_start__isnull=False,
            status__in=['Scheduled', 'In Progress']
        ).values_list('patient_id', flat=True)

        unstarted_tele_patient_ids = TreatmentSchedule.objects.filter(
            treatment_type='Teletherapy',
            date_of_start__isnull=True,
            is_emergency_slot=False
        ).exclude(
            status__in=['Completed', 'Cancelled']
        ).values_list('patient_id', flat=True)

        unsched_visible = Patient.objects.filter(
            is_hidden_from_dashboard=False
        ).filter(
            Q(schedules__isnull=True) | Q(id__in=unstarted_tele_patient_ids)
        ).exclude(
            id__in=active_tele_patient_ids
        ).distinct()

        has_recent_sim = Patient.objects.filter(
            schedules__treatment_type='Teletherapy',
            schedules__date_of_sim__gte=cutoff_date
        ).values_list('id', flat=True)

        to_hide_ids = list(unsched_visible.filter(
            schedules__treatment_type='Teletherapy',
            schedules__date_of_sim__lt=cutoff_date
        ).exclude(
            id__in=has_recent_sim
        ).distinct().values_list('id', flat=True))

        if not to_hide_ids:
            messages.info(request, f"No visible unscheduled patients found with simulation date before {cutoff_date.strftime('%d-%m-%Y')}.")
            return redirect('dashboard')

        now = timezone.now()
        updated_count = Patient.objects.filter(id__in=to_hide_ids).update(
            is_hidden_from_dashboard=True,
            hidden_reason=comment,
            hidden_at=now,
            hidden_by=request.user
        )

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_DASHBOARD_BATCH_HIDDEN",
            details=f"Admin {request.user.username} bulk hid {updated_count} unscheduled patients simulated before {cutoff_date.strftime('%d-%m-%Y')} from dashboard. Reason: {comment}",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Successfully hid {updated_count} unscheduled patient(s) simulated before {cutoff_date.strftime('%d-%m-%Y')} from the main dashboard.")

    return redirect('dashboard')




