from apps.reports.views import get_consultant_signing_options
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from datetime import date, datetime, timedelta, time as dt_time
from decimal import Decimal

from .models import (
    Machine, Shift, Technique, SpecialTechnique, SimulatedFor,
    TreatmentSite, TreatmentIntent, MachineMapping, TreatmentSchedule, Holiday, EmergencyReason,
    BrachyApplicator, BrachyAnesthesia, BrachytherapyAppointment, TreatmentCompletionStatus
)
import os
import json
import re
import csv
import io
import openpyxl
import calendar
from apps.patients.models import Patient, DEFAULT_SITES
from apps.core.models import AuditLog, PrintTemplateConfig, Designation, Consultant
from apps.core.permissions import (
    is_admin_or_subadmin, can_manage_mappings, can_remove_from_schedule,
    can_schedule_patient, can_view_pending_patients, is_consultant_or_admin, is_resident,
    is_admin_only, can_edit_patient, get_four_months_ago
)
from apps.core.validators import validate_field_value


@login_required
def schedule_patient_view(request, patient_id):
    """
    Core Radiotherapy Scheduling View.
    Top card shows Patient Registration details:
      - Readonly: MR No, Patient Name, RT No, Sex, Primary Consultant
      - Editable inline: Age, Diagnosis, Site of Treatment, Phone Number
    Scheduling Form captures:
      - Modality (Teletherapy / Brachytherapy)
      - Simulation date, Intent, Dose, Fractions, Technique, Special Technique,
        Junior Resident (dropdown of JR users), Simulated for, Machine, Shift,
        Treatment start date, Tag with previous course, Notes.
    Actions:
      - 'Save as Draft': saves without scheduling on the active start list
      - 'Approve & Schedule': validates all mandatory parameters and schedules on active list
    """
    patient = get_object_or_404(Patient, id=patient_id)
    if not can_schedule_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to schedule patients.")
        return redirect('patient_detail', patient_id=patient.id)
    machines = Machine.objects.filter(is_active=True, treatment_mode='Teletherapy').order_by('display_order', 'name')
    shifts = Shift.objects.filter(is_active=True).order_by('display_order', 'name')
    techniques = Technique.objects.filter(is_active=True).order_by('display_order', 'name')
    special_techniques = SpecialTechnique.objects.filter(is_active=True).order_by('display_order', 'name')
    simulated_for_list = SimulatedFor.objects.filter(is_active=True).order_by('display_order', 'name')
    treatment_sites = TreatmentSite.objects.filter(is_active=True).order_by('display_order', 'name')
    treatment_intents = TreatmentIntent.objects.filter(is_active=True).order_by('display_order', 'name')

    # Junior Resident users
    jr_users = User.objects.filter(
        Q(profile__role__role_name__icontains='Resident') | 
        Q(profile__role__role_description__icontains='Resident'),
        is_active=True
    ).order_by('first_name', 'username')

    # Determine default Junior Resident (current logged in user if JR, else blank)
    current_profile = getattr(request.user, 'profile', None)
    user_is_jr = bool(current_profile and current_profile.role and 'resident' in current_profile.role.role_name.lower())
    default_jr_val = (request.user.get_full_name() or request.user.username) if user_is_jr else ''

    today = timezone.localdate()

    # Handle specific course selection or adding a new course
    req_schedule_id = request.GET.get('schedule_id') or request.POST.get('schedule_id')
    is_new_course = (request.GET.get('new_course') == '1') or (request.POST.get('new_course') == '1')

    if req_schedule_id:
        existing_schedule = patient.schedules.filter(id=req_schedule_id).first()
        # If user is not Admin/SubAdmin and tries to edit an existing scheduled course:
        if existing_schedule and existing_schedule.status in ('Scheduled', 'In Progress', 'Completed', 'Emergency', 'Vacant'):
            if not is_admin_or_subadmin(request.user):
                messages.error(request, "Access restricted. Only Admin and SubAdmin are authorized to edit existing scheduled treatment courses.")
                return redirect('patient_detail', patient_id=patient.id)
    elif is_new_course:
        existing_schedule = None
    else:
        # Check if there is an active upcoming schedule that hasn't started yet (date_of_start is None or date_of_start >= today)
        active_candidates = [
            s for s in patient.schedules.filter(status__in=['Draft', 'Pending', 'Scheduled']).order_by('-id')
            if not s.date_of_start or s.date_of_start >= today
        ]
        if active_candidates:
            cand = active_candidates[0]
            if cand.status == 'Scheduled' and not is_admin_or_subadmin(request.user):
                # Patient already has an active scheduled course; non-admin cannot edit in place -> open a new course
                messages.info(request, f"Patient already has an active scheduled course (#{cand.id}) starting {cand.date_of_start.strftime('%d-%b-%Y')}. Opening a new course.")
                existing_schedule = None
                is_new_course = True
            else:
                existing_schedule = cand
        else:
            # If all existing courses have passed their date_of_start (date_of_start < today) or are completed:
            # A new course has to be opened and a new slot has to be taken!
            existing_schedule = None
            is_new_course = True

    if existing_schedule and existing_schedule.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{existing_schedule.id} is frozen (> 1 month past scheduled completion). Only Administrators can edit frozen courses.")
        return redirect('patient_detail', patient_id=patient.id)

    previous_schedules = patient.schedules.all()
    if existing_schedule:
        previous_schedules = previous_schedules.exclude(id=existing_schedule.id)

    latest_prev = previous_schedules.order_by('-id').first()
    prev_course_needs_completion = bool(latest_prev and not latest_prev.actual_treatment_status)

    if request.method == 'POST':
        form_action = request.POST.get('form_action', 'approve_schedule')
        is_draft_action = (form_action == 'draft')

        def parse_d(val):
            if not val:
                return None
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
                try:
                    return datetime.strptime(val, fmt).date()
                except ValueError:
                    continue
            return None

        # Handle previous course completion if submitted
        prev_course_id = request.POST.get('prev_course_id')
        if prev_course_id:
            prev_s = patient.schedules.filter(id=prev_course_id).first()
            if prev_s and not prev_s.actual_treatment_status:
                prev_comp = request.POST.get('prev_is_completed', '').strip().lower()
                prev_st_date = parse_d(request.POST.get('prev_actual_start_date', '').strip())
                prev_end_date = parse_d(request.POST.get('prev_actual_end_date', '').strip())
                prev_st_id = request.POST.get('prev_completion_status_id', '').strip()
                prev_notes = request.POST.get('prev_status_notes', '').strip()

                # EBRT constraint: actual start date must be >= CT simulation date
                if prev_comp == 'yes' and prev_st_date and prev_s.date_of_sim and (prev_s.treatment_type == 'Teletherapy' or prev_s.date_of_sim):
                    if prev_st_date < prev_s.date_of_sim:
                        messages.error(
                            request,
                            f"Previous course Actual Start Date ({prev_st_date.strftime('%d-%b-%Y')}) "
                            f"cannot be earlier than CT Simulation Date ({prev_s.date_of_sim.strftime('%d-%b-%Y')}) for EBRT."
                        )
                        return redirect('schedule_patient', patient_id=patient.id)

                if prev_comp == 'yes':
                    comp_obj = TreatmentCompletionStatus.objects.filter(is_completed=True).first()
                    if not comp_obj:
                        comp_obj, _ = TreatmentCompletionStatus.objects.get_or_create(
                            name='Completed', defaults={'is_completed': True, 'display_order': 1}
                        )
                    prev_s.actual_treatment_status = comp_obj
                    prev_s.status = 'Completed'
                    prev_s.actual_start_date = prev_st_date or prev_s.date_of_start
                    prev_s.actual_end_date = prev_end_date
                    if prev_end_date:
                        prev_s.date_of_end = prev_end_date
                elif prev_comp == 'no':
                    if prev_st_id:
                        prev_s.actual_treatment_status = TreatmentCompletionStatus.objects.filter(id=prev_st_id).first()
                    else:
                        prev_s.actual_treatment_status = TreatmentCompletionStatus.objects.filter(is_completed=False).first()
                    st_obj = prev_s.actual_treatment_status
                    if st_obj and st_obj.name.lower() == 'not started':
                        prev_s.status = 'Pending'
                    elif st_obj and st_obj.name.lower() in ('defaulted', 'discontinued'):
                        prev_s.status = 'Cancelled'
                    elif st_obj and st_obj.name.lower() == 'incomplete':
                        prev_s.status = 'In Progress'
                    if prev_st_date:
                        prev_s.actual_start_date = prev_st_date
                    if prev_end_date:
                        prev_s.actual_end_date = prev_end_date
                        prev_s.date_of_end = prev_end_date
                if prev_notes:
                    prev_s.actual_status_notes = prev_notes
                prev_s.save()
                AuditLog.objects.create(
                    user=request.user,
                    action="ACTUAL_STATUS_UPDATED",
                    mr_no=str(patient.mr_no),
                    details=f"Previous Course #{prev_s.id} completion status recorded before scheduling new course: Status={prev_s.actual_treatment_status.name if prev_s.actual_treatment_status else 'None'}",
                    ip_address=request.META.get('REMOTE_ADDR')
                )

        # 1. Update top-card editable patient registration fields
        age_raw = request.POST.get('age', '').strip()
        patient.age = int(float(age_raw)) if age_raw else None
        patient.diagnosis = request.POST.get('diagnosis', patient.diagnosis).strip()
        site_val = request.POST.get('site_of_treatment', patient.site_of_treatment or '').strip()
        if not site_val:
            messages.error(request, "Site of Treatment is mandatory.")
            return redirect('schedule_patient', patient_id=patient.id)
        patient.site_of_treatment = site_val

        old_phone = patient.phone_no or ''
        phone_no = request.POST.get('phone_no', '').strip()
        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return redirect('schedule_patient', patient_id=patient.id)
        
        # Log phone number change if modified
        if old_phone != phone_no:
            AuditLog.objects.create(
                user=request.user,
                action="PATIENT_PHONE_CHANGE",
                mr_no=str(patient.mr_no),
                details=f"Phone number updated from '{old_phone or 'None'}' to '{phone_no}' for patient {patient.pt_name} (MR: {patient.mr_no})",
                ip_address=request.META.get('REMOTE_ADDR')
            )
        patient.phone_no = phone_no
        patient.save()

        # 2. Extract scheduling fields
        treatment_type = request.POST.get('treatment_type', 'Teletherapy').strip()
        sim_date_raw = request.POST.get('date_of_sim', '').strip()
        intent_id = request.POST.get('intent')
        dose_raw = request.POST.get('dose_planned', '').strip()
        fractions_raw = request.POST.get('no_of_fractions', '').strip()
        tech_id = request.POST.get('technique')
        spec_tech_id = request.POST.get('special_technique')
        jr_resident = request.POST.get('jr_resident', '').strip()
        sim_for_id = request.POST.get('simulated_for')
        machine_id = request.POST.get('machine')
        shift_id = request.POST.get('shift')
        start_date_raw = request.POST.get('date_of_start', '').strip()
        tagged_to_id = request.POST.get('tagged_to', '').strip()
        custom_reason = request.POST.get('custom_reason', '').strip()
        notes = request.POST.get('notes', '').strip()

        if custom_reason:
            if notes:
                notes = f"[Custom Date Reason: {custom_reason}] {notes}"
            else:
                notes = f"[Custom Date Reason: {custom_reason}]"

        def parse_d(val):
            if not val:
                return None
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except ValueError:
                return None

        sim_date = parse_d(sim_date_raw)
        start_date = parse_d(start_date_raw)

        dose = Decimal(dose_raw) if dose_raw else None
        fractions = int(float(fractions_raw)) if fractions_raw else None

        intent_obj = TreatmentIntent.objects.filter(id=intent_id, is_active=True).first() if intent_id else None
        tech_obj = Technique.objects.filter(id=tech_id, is_active=True).first() if tech_id else None
        spec_tech_obj = SpecialTechnique.objects.filter(id=spec_tech_id, is_active=True).first() if spec_tech_id else None
        sim_for_obj = SimulatedFor.objects.filter(id=sim_for_id, is_active=True).first() if sim_for_id else None
        mach_obj = Machine.objects.filter(id=machine_id, is_active=True).first() if machine_id else None
        shift_obj = Shift.objects.filter(id=shift_id, is_active=True).first() if shift_id else None
        tagged_to_obj = TreatmentSchedule.objects.filter(id=tagged_to_id, patient=patient).first() if tagged_to_id else None

        # Detect changes in schedule dates for audit logging
        if existing_schedule:
            old_start_date = existing_schedule.date_of_start
            old_sim_date = existing_schedule.date_of_sim

            if old_start_date != start_date:
                AuditLog.objects.create(
                    user=request.user,
                    action="SCHEDULE_DATE_CHANGE",
                    mr_no=str(patient.mr_no),
                    details=f"Treatment Start Date updated from '{old_start_date or 'Pending'}' to '{start_date or 'Pending'}' for {patient.pt_name} (MR: {patient.mr_no}) on {mach_obj or 'Unassigned'}",
                    ip_address=request.META.get('REMOTE_ADDR')
                )

            if old_sim_date != sim_date:
                AuditLog.objects.create(
                    user=request.user,
                    action="SIMULATION_DATE_CHANGE",
                    mr_no=str(patient.mr_no),
                    details=f"CT Simulation Date changed from '{old_sim_date or 'None'}' to '{sim_date or 'None'}' for {patient.pt_name} (MR: {patient.mr_no})",
                    ip_address=request.META.get('REMOTE_ADDR')
                )

        schedule_action = "created"
        if existing_schedule:
            sched = existing_schedule
            schedule_action = "updated"
        else:
            sched = TreatmentSchedule(patient=patient, created_by=request.user)

        # Handle 'Save as Draft' action
        if is_draft_action:
            sched.treatment_type = treatment_type
            sched.site_of_treatment = site_val
            sched.intent = intent_obj
            sched.date_of_sim = sim_date
            sched.dose_planned = dose
            sched.no_of_fractions = fractions
            sched.technique = tech_obj
            sched.special_technique = spec_tech_obj
            sched.jr_resident = jr_resident
            sched.simulated_for = sim_for_obj
            sched.machine = mach_obj
            sched.shift = shift_obj
            sched.date_of_start = start_date
            sched.tagged_to = tagged_to_obj
            sched.notes = notes
            sched.status = 'Draft'
            sched.save()
            if tagged_to_obj:
                tagged_to_obj.tagged_to = sched
                tagged_to_obj.save(update_fields=['tagged_to'])

            AuditLog.objects.create(
                user=request.user,
                action="SCHEDULE_DRAFT_SAVED",
                mr_no=str(patient.mr_no),
                details=f"Saved Draft treatment course for {patient.pt_name} (MR: {patient.mr_no}) on {mach_obj or 'Unassigned'}",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(
                request,
                f"Treatment course saved as Draft for {patient.pt_name} (MR: {patient.mr_no}). Patient is in Draft status and will NOT appear on the daily machine list until approved."
            )
            return redirect('patient_detail', patient_id=patient.id)

        # Handle 'Approve & Schedule' action: Validate ALL mandatory scheduling fields
        if not site_val:
            messages.error(request, "Site of Treatment is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not sim_date:
            messages.error(request, "CT Simulation Date is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not intent_obj:
            messages.error(request, "Treatment Intent (Radical, Palliative, Adjuvant, Neo-adjuvant) is mandatory.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not tech_obj:
            messages.error(request, "Treatment Technique is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not dose:
            messages.error(request, "Planned Dose (Gy) is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not fractions:
            messages.error(request, "Number of fractions is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not mach_obj:
            messages.error(request, "Machine selection is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if not start_date:
            messages.error(request, "Treatment Start Date is mandatory for scheduling.")
            return redirect('schedule_patient', patient_id=patient.id)

        if sim_date and start_date and start_date < sim_date:
            messages.error(
                request,
                f"Treatment Start Date ({start_date.strftime('%d-%m-%Y')}) cannot be earlier than CT Simulation Date ({sim_date.strftime('%d-%m-%Y')}). Date of start must be on or after Date of simulation."
            )
            return redirect('schedule_patient', patient_id=patient.id)

        sched.treatment_type = treatment_type
        sched.site_of_treatment = site_val
        sched.intent = intent_obj
        sched.date_of_sim = sim_date
        sched.dose_planned = dose
        sched.no_of_fractions = fractions
        sched.technique = tech_obj
        sched.special_technique = spec_tech_obj
        sched.jr_resident = jr_resident
        sched.simulated_for = sim_for_obj
        sched.machine = mach_obj
        sched.shift = shift_obj
        sched.date_of_start = start_date
        sched.tagged_to = tagged_to_obj
        sched.notes = notes
        sched.status = 'Scheduled'
        sched.save()
        if tagged_to_obj:
            tagged_to_obj.tagged_to = sched
            tagged_to_obj.save(update_fields=['tagged_to'])

        AuditLog.objects.create(
            user=request.user,
            action="SCHEDULE_APPROVED",
            mr_no=str(patient.mr_no),
            details=f"Approved & Scheduled treatment for {patient.pt_name} (MR: {patient.mr_no}) on {mach_obj.name} (Start: {start_date}, Intent: {intent_obj.name}, JR: {jr_resident or 'None'})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(
            request,
            f"Treatment schedule approved and scheduled successfully for {patient.pt_name} (MR: {patient.mr_no}) on {mach_obj.name}!"
        )
        return redirect('patient_detail', patient_id=patient.id)

    default_technique = techniques.filter(name__iexact='IMRT').first() or techniques.first()
    default_special_technique = special_techniques.filter(name__iexact='None').first() or special_techniques.first()
    default_intent = treatment_intents.filter(name__iexact='Radical').first() or treatment_intents.first()

    jr_user_names = [u.get_full_name() or u.username for u in jr_users]

    holiday_map = {h.date.isoformat(): (h.comment or 'Hospital Holiday') for h in Holiday.objects.all()}

    context = {
        'patient': patient,
        'schedule': existing_schedule,
        'previous_schedules': previous_schedules,
        'machines': machines,
        'shifts': shifts,
        'techniques': techniques,
        'special_techniques': special_techniques,
        'treatment_intents': treatment_intents,
        'default_technique': default_technique,
        'default_special_technique': default_special_technique,
        'default_intent': default_intent,
        'simulated_for_list': simulated_for_list,
        'treatment_sites': treatment_sites,
        'jr_users': jr_users,
        'jr_user_names': jr_user_names,
        'default_jr_val': default_jr_val,
        'is_new_course': is_new_course,
        'holiday_map': holiday_map,
        'holiday_map_json': json.dumps(holiday_map),
        'today': date.today().isoformat(),
        'latest_prev': latest_prev,
        'prev_course_needs_completion': prev_course_needs_completion,
        'completion_statuses': TreatmentCompletionStatus.objects.filter(is_active=True).order_by('display_order', 'name'),
    }
    return render(request, 'scheduler/schedule_patient.html', context)


@login_required
def get_machine_options_api(request, machine_id):
    """
    API for Dynamic Cascading Dropdowns:
    Returns mapped Shifts, Techniques, Special Techniques, and Sites for selected Machine.
    Capacity rule: max_daily_starts = count of active mapped shifts.
    """
    machine = get_object_or_404(Machine, id=machine_id)
    mapping = getattr(machine, 'mapping', None)

    if mapping and mapping.is_active:
        active_shifts = mapping.allowed_shifts.filter(is_active=True).order_by('display_order', 'name')
        active_techs = mapping.allowed_techniques.filter(is_active=True).order_by('display_order', 'name')
        active_spec_techs = mapping.allowed_special_techniques.filter(is_active=True).order_by('display_order', 'name')
        
        shifts = [{'id': s.id, 'name': s.name} for s in active_shifts]
        techniques = [{'id': t.id, 'name': t.name} for t in active_techs]
        special_techniques = [{'id': st.id, 'name': st.name} for st in active_spec_techs]
        sites = mapping.get_allowed_sites_list()
        max_daily_starts = active_shifts.count()
    else:
        active_shifts = Shift.objects.filter(is_active=True).order_by('display_order', 'name')
        shifts = [{'id': s.id, 'name': s.name} for s in active_shifts]
        techniques = [{'id': t.id, 'name': t.name} for t in Technique.objects.filter(is_active=True).order_by('display_order', 'name')]
        special_techniques = [{'id': st.id, 'name': st.name} for st in SpecialTechnique.objects.filter(is_active=True).order_by('display_order', 'name')]
        sites = [s.name for s in TreatmentSite.objects.filter(is_active=True).order_by('display_order', 'name')]
        max_daily_starts = active_shifts.count()

    return JsonResponse({
        'machine_id': machine.id,
        'machine_name': machine.name,
        'shifts': shifts,
        'techniques': techniques,
        'special_techniques': special_techniques,
        'sites': sites,
        'max_daily_starts': max_daily_starts,
    })


@login_required
def get_nearest_free_slot_api(request):
    """
    Nearest Available Free Slot Engine:
    Evaluates calendar dates starting from search_date (skipping Sundays).
    Capacity Rule: Maximum number of new patients allowed for auto scheduling = number of active shifts.
    """
    machine_id = request.GET.get('machine_id')
    shift_id = request.GET.get('shift_id')
    start_from_raw = request.GET.get('start_from')

    if not machine_id:
        return JsonResponse({'error': 'Please select a Machine first.'}, status=400)

    machine = get_object_or_404(Machine, id=machine_id)
    shift = Shift.objects.filter(id=shift_id, is_active=True).first() if shift_id else None

    # Determine max starts per day = number of active shifts
    mapping = getattr(machine, 'mapping', None)
    if mapping and mapping.is_active:
        active_shifts = mapping.allowed_shifts.filter(is_active=True)
    else:
        active_shifts = Shift.objects.filter(is_active=True)

    max_daily_starts = active_shifts.count()

    if max_daily_starts == 0:
        return JsonResponse({
            'found': False,
            'message': f"No active shifts are currently configured for {machine.name}. Daily auto-scheduling quota is 0."
        })

    if shift_id and not shift:
        return JsonResponse({
            'found': False,
            'message': f"The requested shift is inactive or does not exist."
        })

    if shift and mapping and not mapping.allowed_shifts.filter(id=shift.id, is_active=True).exists():
        return JsonResponse({
            'found': False,
            'message': f"Shift '{shift.name}' is currently not mapped to {machine.name}."
        })

    # Starting search date
    today = date.today()
    if start_from_raw:
        try:
            search_date = datetime.strptime(start_from_raw, "%Y-%m-%d").date()
            if search_date < today:
                search_date = today + timedelta(days=1)
        except ValueError:
            search_date = today + timedelta(days=1)
    else:
        search_date = today + timedelta(days=1)

    available_slots = []
    curr_date = search_date
    holiday_dates = set(Holiday.objects.values_list('date', flat=True))

    # Search up to 60 days ahead
    for _ in range(60):
        # Skip Sundays (weekday 6) and scheduled Holidays
        if curr_date.weekday() != 6 and curr_date not in holiday_dates:
            machine_total_booked = TreatmentSchedule.objects.filter(machine=machine, date_of_start=curr_date).count()

            if shift:
                shift_booked = TreatmentSchedule.objects.filter(machine=machine, shift=shift, date_of_start=curr_date).count()
                # 1 shift allows 1 start per day, machine allows max_daily_starts
                is_free = (shift_booked < 1) and (machine_total_booked < max_daily_starts)
                remaining = max(0, 1 - shift_booked) if is_free else 0
            else:
                is_free = (machine_total_booked < max_daily_starts)
                remaining = max_daily_starts - machine_total_booked

            if is_free:
                available_slots.append({
                    'date': curr_date.isoformat(),
                    'formatted': curr_date.strftime("%a, %d %b %Y"),
                    'booked_count': machine_total_booked,
                    'max_quota': max_daily_starts,
                    'remaining': remaining
                })
                if len(available_slots) >= 5:
                    break
        curr_date += timedelta(days=1)

    if not available_slots:
        return JsonResponse({
            'found': False,
            'message': 'No free slot found in the next 60 days matching the active shift capacity.'
        })

    nearest = available_slots[0]
    return JsonResponse({
        'found': True,
        'machine_name': machine.name,
        'shift_name': shift.name if shift else 'Any Active Shift',
        'nearest_slot': nearest,
        'alternative_slots': available_slots[1:],
    })


@login_required
def get_mapped_machines_slots_api(request):
    """
    Dynamic Slot Matrix for Mapped Machines:
    Evaluates all active machines mapped to the selected Technique & Special Technique.
    For each capable machine, finds the earliest available free slot starting from the simulation date,
    along with quota status, recommended shift, and active shifts for custom selection.
    """
    tech_id = request.GET.get('technique_id')
    spec_tech_id = request.GET.get('special_technique_id')
    sim_date_raw = request.GET.get('sim_date')

    today = date.today()
    if sim_date_raw:
        try:
            sim_date = datetime.strptime(sim_date_raw, "%Y-%m-%d").date()
            start_search = max(sim_date + timedelta(days=1), today + timedelta(days=1))
        except ValueError:
            start_search = today + timedelta(days=1)
    else:
        start_search = today + timedelta(days=1)

    tech_obj = Technique.objects.filter(id=tech_id, is_active=True).first() if tech_id else None
    spec_tech_obj = SpecialTechnique.objects.filter(id=spec_tech_id, is_active=True).first() if spec_tech_id else None

    holiday_dates = set(Holiday.objects.values_list('date', flat=True))
    machines = Machine.objects.filter(is_active=True, treatment_mode='Teletherapy').order_by('display_order', 'name')
    results = []

    for mach in machines:
        mapping = getattr(mach, 'mapping', None)

        # Check mapping for technique and special technique
        if mapping and mapping.is_active:
            if tech_obj and not mapping.allowed_techniques.filter(id=tech_obj.id).exists():
                continue
            if spec_tech_obj and spec_tech_obj.name.lower() != 'none':
                if not mapping.allowed_special_techniques.filter(id=spec_tech_obj.id).exists():
                    continue
            active_shifts = list(mapping.allowed_shifts.filter(is_active=True).order_by('display_order', 'name'))
            max_daily_starts = mapping.max_daily_starts or len(active_shifts) or 1
        else:
            active_shifts = list(Shift.objects.filter(is_active=True).order_by('display_order', 'name'))
            max_daily_starts = len(active_shifts) or 1

        if not active_shifts:
            continue

        # Find earliest available free slot (skipping Sundays and scheduled Holidays)
        # Capacity rule: Exactly 1 patient per shift accommodated per day
        curr_date = start_search
        found_slot = None

        for _ in range(60):
            if curr_date.weekday() != 6 and curr_date not in holiday_dates:  # Skip Sunday and Holidays
                # Auto-designate the first open shift on this date with 0 bookings (< 1 patient/shift limit)
                free_shift = None
                for sh in active_shifts:
                    sh_booked = TreatmentSchedule.objects.filter(machine=mach, shift=sh, date_of_start=curr_date).count()
                    if sh_booked < 1:
                        free_shift = sh
                        break

                if free_shift:
                    found_slot = {
                        'date': curr_date.isoformat(),
                        'date_formatted': curr_date.strftime("%a, %d %b %Y"),
                        'shift_id': free_shift.id,
                        'shift_name': free_shift.name,
                    }
                    break
            curr_date += timedelta(days=1)

        shifts_data = [{'id': s.id, 'name': s.name} for s in active_shifts]

        results.append({
            'machine_id': mach.id,
            'machine_name': mach.name,
            'machine_code': mach.code or mach.name,
            'machine_type': getattr(mach, 'machine_type', 'LINAC'),
            'max_daily_starts': max_daily_starts,
            'slot': found_slot,
            'shifts': shifts_data,
        })

    return JsonResponse({
        'success': True,
        'technique_name': tech_obj.name if tech_obj else 'None',
        'special_technique_name': spec_tech_obj.name if spec_tech_obj else 'None',
        'machines': results
    })



@login_required
def machine_mappings_view(request):
    """
    Admin & SubAdmin Dynamic Mapping Manager:
    Configure which Shifts, Techniques, Special Techniques, and Sites apply to each Machine,
    along with Option A daily start quota.
    Exclusively for Teletherapy machines (Brachytherapy does not use shifts or techniques).
    """
    if not can_manage_mappings(request.user):
        messages.error(request, "Access restricted to Administrators / SubAdmins.")
        return redirect('dashboard')

    mappings = MachineMapping.objects.filter(machine__treatment_mode='Teletherapy')\
        .select_related('machine')\
        .prefetch_related('allowed_shifts', 'allowed_techniques', 'allowed_special_techniques')\
        .order_by('machine__display_order')

    return render(request, 'scheduler/machine_mappings.html', {'mappings': mappings})


@login_required
def edit_machine_mapping_view(request, mapping_id):
    """
    Edit mapping for a specific machine.
    """
    if not can_manage_mappings(request.user):
        messages.error(request, "Access restricted to Administrators / SubAdmins.")
        return redirect('dashboard')

    mapping = get_object_or_404(MachineMapping, id=mapping_id)
    if mapping.machine.treatment_mode != 'Teletherapy':
        messages.warning(request, "Dynamic Machine Mappings are exclusively for Teletherapy equipment. Brachytherapy machines do not use shifts, techniques, or special techniques.")
        return redirect('machine_mappings')

    all_shifts = Shift.objects.all().order_by('display_order')
    all_techs = Technique.objects.all().order_by('display_order')
    all_special_techs = SpecialTechnique.objects.all().order_by('display_order')
    all_treatment_sites = TreatmentSite.objects.filter(is_active=True).order_by('display_order', 'name')
    current_sites_list = mapping.get_allowed_sites_list()

    if request.method == 'POST':
        shift_ids = request.POST.getlist('shifts')
        tech_ids = request.POST.getlist('techniques')
        special_tech_ids = request.POST.getlist('special_techniques')
        
        # Multi-select dropdown anatomical treatment sites
        site_inputs = request.POST.getlist('allowed_sites')
        if len(site_inputs) == 1 and ',' in site_inputs[0]:
            site_list = [s.strip() for s in site_inputs[0].split(',') if s.strip()]
        else:
            site_list = [s.strip() for s in site_inputs if s.strip()]
        sites = ", ".join(site_list)
        
        quota_raw = request.POST.get('max_daily_starts', '3').strip()

        mapping.allowed_shifts.set(Shift.objects.filter(id__in=shift_ids))
        mapping.allowed_techniques.set(Technique.objects.filter(id__in=tech_ids))
        mapping.allowed_special_techniques.set(SpecialTechnique.objects.filter(id__in=special_tech_ids))
        mapping.allowed_sites = sites
        
        # User Rule: Maximum number of new patients allowed for auto scheduling = number of shifts
        active_shifts_count = mapping.allowed_shifts.filter(is_active=True).count()
        mapping.max_daily_starts = active_shifts_count if active_shifts_count > 0 else 1
        mapping.save()

        AuditLog.objects.create(
            user=request.user,
            action="MAPPING_UPDATE",
            details=f"Updated machine mapping for {mapping.machine.name} (Max daily quota: {mapping.max_daily_starts})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Configuration updated for {mapping.machine.name}!")
        return redirect('machine_mappings')

    context = {
        'mapping': mapping,
        'all_shifts': all_shifts,
        'all_techs': all_techs,
        'all_special_techs': all_special_techs,
        'all_treatment_sites': all_treatment_sites,
        'current_sites_list': current_sites_list,
    }
    return render(request, 'scheduler/edit_machine_mapping.html', context)


@login_required
def unscheduled_patients_view(request):
    """Queue of registered patients who have no treatment schedule booked yet."""
    if not can_view_pending_patients(request.user):
        messages.error(request, "Access restricted. You do not have permission to view pending patient lists.")
        return redirect('dashboard')
    patients = Patient.objects.filter(schedules__isnull=True).order_by('-created_at')
    return render(request, 'scheduler/unscheduled_patients.html', {'patients': patients})


@login_required
def schedule_delete_view(request, schedule_id):
    """
    Remove a patient from the schedule list (deletes the TreatmentSchedule course).
    Strictly restricted to Admin (not SubAdmin).
    """
    if not can_remove_from_schedule(request.user):
        messages.error(request, "Access restricted. Only Administrators have permission to remove patients from the schedule list.")
        return redirect('dashboard')

    schedule = get_object_or_404(TreatmentSchedule, id=schedule_id)
    patient = schedule.patient
    patient_id = patient.id
    mr_no = patient.mr_no
    pt_name = patient.pt_name
    machine_name = schedule.machine.name if schedule.machine else 'Unassigned'
    start_date_str = str(schedule.date_of_start) if schedule.date_of_start else 'Pending'

    if schedule.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{schedule.id} is frozen (> 1 month past scheduled completion). Only Administrators can delete frozen courses.")
        return redirect('patient_detail', patient_id=patient.id)

    if request.method == 'POST':
        schedule.delete()

        AuditLog.objects.create(
            user=request.user,
            action="SCHEDULE_REMOVE",
            mr_no=str(mr_no),
            details=f"Removed treatment schedule for {pt_name} (MR: {mr_no}) on {machine_name}, start date: {start_date_str}.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(
            request,
            f"Treatment schedule for {pt_name} (MR: {mr_no}) on {machine_name} was removed. Patient is now non-scheduled."
        )

        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url:
            return redirect(next_url)
        return redirect('patient_detail', patient_id=patient_id)

    return render(request, 'scheduler/schedule_confirm_delete.html', {
        'schedule': schedule,
        'patient': patient,
        'next': request.GET.get('next', '')
    })


@login_required
def tag_treatment_course_view(request, schedule_id):
    """
    Tag / Club a treatment course to another previous course of the same patient.
    Can also untag to make it independent.
    """
    schedule = get_object_or_404(TreatmentSchedule, id=schedule_id)
    patient = schedule.patient

    if not can_schedule_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to modify treatment courses.")
        return redirect('patient_detail', patient_id=patient.id)

    if schedule.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{schedule.id} is frozen (> 1 month past scheduled completion). Only Administrators can modify course clubbing.")
        return redirect('patient_detail', patient_id=patient.id)

    if request.method == 'POST':
        tagged_to_id = request.POST.get('tagged_to_id', '').strip()
        if tagged_to_id:
            target = get_object_or_404(TreatmentSchedule, id=tagged_to_id, patient=patient)
            if target.id == schedule.id:
                messages.error(request, "A course cannot be tagged to itself.")
                return redirect('patient_detail', patient_id=patient.id)

            # Unlink previous partners if switching
            if schedule.tagged_to and schedule.tagged_to != target:
                prev_partner = schedule.tagged_to
                if prev_partner.tagged_to == schedule:
                    prev_partner.tagged_to = None
                    prev_partner.save(update_fields=['tagged_to'])
            if target.tagged_to and target.tagged_to != schedule:
                prev_target_partner = target.tagged_to
                if prev_target_partner.tagged_to == target:
                    prev_target_partner.tagged_to = None
                    prev_target_partner.save(update_fields=['tagged_to'])

            # Mutual two-way clubbing (both ways!)
            schedule.tagged_to = target
            target.tagged_to = schedule
            schedule.save(update_fields=['tagged_to'])
            target.save(update_fields=['tagged_to'])

            AuditLog.objects.create(
                user=request.user,
                action="COURSE_TAGGED",
                mr_no=str(patient.mr_no),
                details=f"Course #{schedule.id} ({schedule.machine_display}) mutually clubbed with Course #{target.id} ({target.machine_display}) for {patient.pt_name}",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f"Course #{schedule.id} and Course #{target.id} are now mutually clubbed together!")
        else:
            # Unlink both ways
            old_partner = schedule.tagged_to
            schedule.tagged_to = None
            schedule.save(update_fields=['tagged_to'])
            if old_partner:
                if old_partner.tagged_to == schedule:
                    old_partner.tagged_to = None
                    old_partner.save(update_fields=['tagged_to'])
            for child in schedule.tagged_courses.all():
                child.tagged_to = None
                child.save(update_fields=['tagged_to'])

            AuditLog.objects.create(
                user=request.user,
                action="COURSE_UNTAGGED",
                mr_no=str(patient.mr_no),
                details=f"Course #{schedule.id} unclubbed and set as independent course for {patient.pt_name}",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, f"Course #{schedule.id} is now an independent course.")

    return redirect('patient_detail', patient_id=patient.id)


@login_required
def update_actual_treatment_status_view(request, schedule_id):
    """
    Records or updates actual treatment start date, actual end date, and completion status.
    If course is frozen (> 1 month past scheduled completion), strictly restricted to Administrator.
    """
    schedule = get_object_or_404(TreatmentSchedule, id=schedule_id)
    patient = schedule.patient

    if not can_schedule_patient(request.user) and not can_edit_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to update treatment status.")
        return redirect('patient_detail', patient_id=patient.id)

    if schedule.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{schedule.id} is frozen (> 1 month past scheduled completion). Only Administrators can modify frozen courses.")
        return redirect('patient_detail', patient_id=patient.id)

    if request.method == 'POST':
        is_completed_val = request.POST.get('is_completed', '').strip().lower()  # 'yes' or 'no'
        actual_start_raw = request.POST.get('actual_start_date', '').strip()
        actual_end_raw = request.POST.get('actual_end_date', '').strip()
        status_id = request.POST.get('completion_status_id', '').strip()
        notes = request.POST.get('actual_status_notes', '').strip()

        def parse_d(val):
            if not val:
                return None
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
                try:
                    return datetime.strptime(val, fmt).date()
                except ValueError:
                    continue
            return None

        act_start = parse_d(actual_start_raw)
        act_end = parse_d(actual_end_raw)

        # EBRT constraint: actual start date must be >= CT simulation date
        if is_completed_val == 'yes' and act_start and schedule.date_of_sim and (schedule.treatment_type == 'Teletherapy' or schedule.date_of_sim):
            if act_start < schedule.date_of_sim:
                messages.error(
                    request,
                    f"Actual Start Date ({act_start.strftime('%d-%b-%Y')}) cannot be earlier than "
                    f"CT Simulation Date ({schedule.date_of_sim.strftime('%d-%b-%Y')}) for EBRT."
                )
                return redirect('patient_detail', patient_id=patient.id)

        if is_completed_val == 'yes':
            comp_status = TreatmentCompletionStatus.objects.filter(is_completed=True).first()
            if not comp_status:
                comp_status, _ = TreatmentCompletionStatus.objects.get_or_create(
                    name='Completed', defaults={'is_completed': True, 'display_order': 1}
                )
            schedule.actual_treatment_status = comp_status
            schedule.status = 'Completed'
            schedule.actual_start_date = act_start or schedule.actual_start_date or schedule.date_of_start
            schedule.actual_end_date = act_end or schedule.actual_end_date
            if act_end:
                schedule.date_of_end = act_end
            if notes:
                schedule.actual_status_notes = notes
        elif is_completed_val == 'no':
            if status_id:
                st_obj = TreatmentCompletionStatus.objects.filter(id=status_id).first()
            else:
                st_obj = TreatmentCompletionStatus.objects.filter(is_completed=False).first()
            schedule.actual_treatment_status = st_obj
            if st_obj and st_obj.name.lower() == 'not started':
                schedule.status = 'Pending'
            elif st_obj and st_obj.name.lower() in ('defaulted', 'discontinued'):
                schedule.status = 'Cancelled'
            elif st_obj and st_obj.name.lower() == 'incomplete':
                schedule.status = 'In Progress'
            if act_start:
                schedule.actual_start_date = act_start
            if act_end:
                schedule.actual_end_date = act_end
                schedule.date_of_end = act_end
            if notes:
                schedule.actual_status_notes = notes
        else:
            if status_id:
                st_obj = TreatmentCompletionStatus.objects.filter(id=status_id).first()
                schedule.actual_treatment_status = st_obj
                if st_obj and st_obj.is_completed:
                    schedule.status = 'Completed'
            if act_start:
                schedule.actual_start_date = act_start
            if act_end:
                schedule.actual_end_date = act_end
                schedule.date_of_end = act_end
            if notes:
                schedule.actual_status_notes = notes

        schedule.save()

        AuditLog.objects.create(
            user=request.user,
            action="ACTUAL_STATUS_UPDATED",
            mr_no=str(patient.mr_no),
            details=f"Course #{schedule.id} actual status updated: Status={schedule.actual_treatment_status.name if schedule.actual_treatment_status else 'None'}, Start={schedule.actual_start_date}, End={schedule.actual_end_date} for {patient.pt_name}",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Actual treatment status updated for Course #{schedule.id}.")

        next_url = request.POST.get('next_url') or request.GET.get('next_url')
        if next_url:
            return redirect(next_url)

        return redirect('patient_detail', patient_id=patient.id)

    statuses = TreatmentCompletionStatus.objects.filter(is_active=True).order_by('display_order', 'name')
    context = {
        'patient': patient,
        'schedule': schedule,
        'statuses': statuses,
    }
    return render(request, 'scheduler/actual_status_form.html', context)


@login_required
def holiday_manager_view(request):
    """
    Holiday Management View:
    Accessible by Admin & SubAdmin.
    Allows adding dates with an optional comment, and deleting holidays.
    Holidays are excluded from auto-scheduling machine slots,
    and display a warning symbol during manual scheduling.
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Hospital holidays can only be managed by Administrators and SubAdministrators.")
        return redirect('dashboard')

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'add':
            date_raw = (request.POST.get('date') or request.POST.get('holiday_date') or '').strip()
            comment = request.POST.get('comment', '').strip()

            if not date_raw:
                messages.error(request, "Holiday date is required.")
                return redirect('holiday_manager')

            try:
                hol_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
            except ValueError:
                messages.error(request, "Invalid date format.")
                return redirect('holiday_manager')

            if Holiday.objects.filter(date=hol_date).exists():
                messages.warning(request, f"Holiday on {hol_date.strftime('%d-%b-%Y')} is already registered.")
                return redirect('holiday_manager')

            Holiday.objects.create(
                date=hol_date,
                comment=comment,
                created_by=request.user
            )

            AuditLog.objects.create(
                user=request.user,
                action="HOLIDAY_ADDED",
                details=f"Added hospital holiday on {hol_date.strftime('%d-%b-%Y')}: {comment or 'No description'}",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, f"Holiday on {hol_date.strftime('%d-%b-%Y')} ({comment or 'No description'}) added successfully.")
            return redirect('holiday_manager')

        elif action == 'delete':
            holiday_id = request.POST.get('holiday_id')
            hol = get_object_or_404(Holiday, id=holiday_id)
            h_date_str = hol.date.strftime('%d-%b-%Y')
            h_comment = hol.comment or ''
            hol.delete()

            AuditLog.objects.create(
                user=request.user,
                action="HOLIDAY_REMOVED",
                details=f"Removed hospital holiday for {h_date_str} ({h_comment})",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            messages.success(request, f"Holiday for {h_date_str} removed.")
            return redirect('holiday_manager')

    holidays = Holiday.objects.all().order_by('date')
    today = date.today()

    context = {
        'holidays': holidays,
        'today': today.isoformat(),
        'total_holidays': holidays.count(),
    }
    return render(request, 'scheduler/holidays.html', context)


@login_required
def emergency_slot_view(request, schedule_id):
    """
    Assign or Edit patient course for Emergency / Vacant Slot Filling.
    Removes patient from the normal daily start schedule list and logs the transition.
    Prepopulates patient details, current scheduled date, machine, shift, and junior resident.
    Provides editable dropdown for Emergency Reason: [Spinal cord compression, Bleeding, Pain, SVCO, Others].
    Completely separate from the regular treatment scheduler.
    """
    sched = get_object_or_404(
        TreatmentSchedule.objects.select_related('patient', 'machine', 'shift', 'technique', 'patient__primary_consultant'),
        id=schedule_id
    )
    patient = sched.patient

    if not can_schedule_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to assign or modify emergency slots.")
        return redirect('patient_detail', patient_id=patient.id)

    if sched.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{sched.id} is frozen (> 1 month past scheduled completion). Cannot change to Emergency / Vacant slot.")
        return redirect('patient_detail', patient_id=patient.id)

    if sched.treatment_type == 'Brachytherapy':
        messages.error(request, "Emergency slot option is not applicable for Brachytherapy courses.")
        return redirect('patient_detail', patient_id=patient.id)

    if not sched.is_emergency_slot:
        if not sched.can_add_to_emergency:
            messages.error(request, f"Treatment Course #{sched.id} cannot be added to an Emergency / Vacant slot because this course is completed or its scheduled start date has passed.")
            return redirect('patient_detail', patient_id=patient.id)
    else:
        if not is_admin_or_subadmin(request.user):
            messages.error(request, "Access restricted. Only Admin and SubAdmin are authorized to edit Emergency / Vacant slot details.")
            return redirect('patient_detail', patient_id=patient.id)

    all_machines = Machine.objects.filter(is_active=True, treatment_mode='Teletherapy').order_by('display_order', 'name')
    all_shifts = Shift.objects.filter(is_active=True).order_by('display_order')
    jr_users = User.objects.filter(
        Q(profile__role__role_name__icontains='Resident') | 
        Q(profile__role__role_description__icontains='Resident'),
        is_active=True
    ).select_related('profile').order_by('first_name', 'username')

    db_reasons = list(EmergencyReason.objects.filter(is_active=True).order_by('display_order').values_list('name', flat=True))
    emergency_reasons = db_reasons if db_reasons else [
        'Spinal cord compression',
        'Bleeding',
        'Pain',
        'SVCO',
        'Others'
    ]

    next_url = request.GET.get('next') or request.POST.get('next') or ''

    if request.method == 'POST':
        slot_category = request.POST.get('slot_category', 'Emergency').strip()
        if slot_category not in ('Emergency', 'Vacant'):
            slot_category = 'Emergency'

        intended_date_raw = request.POST.get('intended_start_date', '').strip()
        emergency_reason = request.POST.get('emergency_reason', '').strip()
        custom_reason = request.POST.get('custom_emergency_reason', '').strip()
        if emergency_reason == 'Others' and custom_reason:
            emergency_reason = custom_reason
        elif not emergency_reason and custom_reason:
            emergency_reason = custom_reason
        machine_id_val = request.POST.get('machine_id', '').strip()
        shift_id_val = request.POST.get('shift_id', '').strip()
        jr_resident_val = request.POST.get('jr_resident', '').strip()
        notes = request.POST.get('notes', '').strip()

        def parse_d(val):
            if not val:
                return None
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except ValueError:
                return None

        intended_date = parse_d(intended_date_raw)
        today_date = date.today()
        scheduled_date = sched.original_scheduled_date or sched.date_of_start

        if intended_date:
            is_changing_date = (intended_date != sched.intended_start_date)
            if not sched.is_emergency_slot or is_changing_date:
                if intended_date < today_date:
                    messages.error(
                        request,
                        f"Intended date can only be on or after the current date ({today_date.strftime('%d-%b-%Y')})."
                    )
                    return redirect(request.get_full_path())
                if scheduled_date and scheduled_date > today_date and intended_date >= scheduled_date:
                    messages.error(
                        request,
                        f"Intended date must be before the scheduled start date ({scheduled_date.strftime('%d-%b-%Y')})."
                    )
                    return redirect(request.get_full_path())

        if slot_category == 'Emergency' and not emergency_reason:
            emergency_reason = 'Emergency clinical indication'

        # Store original scheduled date if not already stored
        if not sched.original_scheduled_date:
            sched.original_scheduled_date = sched.date_of_start

        # Update machine and shift if provided
        if machine_id_val:
            try:
                mach_id_int = int(machine_id_val)
                mach_cand = Machine.objects.filter(id=mach_id_int, treatment_mode='Teletherapy', is_active=True).first()
                if mach_cand:
                    sched.machine = mach_cand
            except (ValueError, TypeError):
                pass
        elif machine_id_val == '':
            sched.machine = None

        if shift_id_val:
            try:
                sched.shift_id = int(shift_id_val)
            except (ValueError, TypeError):
                pass
        elif shift_id_val == '':
            sched.shift = None

        if jr_resident_val:
            sched.jr_resident = jr_resident_val

        # Update schedule to Emergency / Vacant slot status
        was_already_emergency = sched.is_emergency_slot
        sched.is_emergency_slot = True
        sched.slot_category = slot_category
        sched.emergency_reason = emergency_reason if slot_category == 'Emergency' else ''
        sched.intended_start_date = intended_date
        sched.status = slot_category  # 'Emergency' or 'Vacant'
        
        # When shifted to Emergency/Vacant list, normal date_of_start is cleared
        # so patient is removed from the normal schedule list as requested
        old_scheduled_date_str = sched.date_of_start.strftime("%d-%b-%Y") if sched.date_of_start else "None"
        sched.date_of_start = None

        stamp = timezone.now().strftime("%d-%b-%Y %H:%M")
        action_type = "EMERGENCY_SLOT_UPDATE" if was_already_emergency else "EMERGENCY_SLOT_ASSIGNED"
        log_entry = f"[{stamp}] {slot_category} slot update (Reason: {emergency_reason or 'None'}, Intended Start: {intended_date or 'TBD'}, Machine: {sched.machine.name if sched.machine else 'Unassigned'}, Shift: {sched.shift.name if sched.shift else 'Unassigned'}, JR: {sched.jr_resident or 'None'})."
        if notes:
            log_entry += f" Notes: {notes}"
        sched.notes = f"{sched.notes}\n{log_entry}".strip() if sched.notes else log_entry
        sched.save()

        AuditLog.objects.create(
            user=request.user,
            action=action_type,
            mr_no=str(patient.mr_no),
            details=f"Patient {patient.pt_name} (MR: {patient.mr_no}) {slot_category} slot (Machine: {sched.machine.name if sched.machine else 'None'}, Intended Start: {intended_date}, Reason: {emergency_reason or 'None'}).",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        msg = "Emergency / Vacant slot details updated successfully." if was_already_emergency else "Patient has been shifted to Emergency / Vacant slot list - will be removed from the normal schedule list"
        messages.success(request, msg)

        if next_url:
            return redirect(next_url)
        if was_already_emergency:
            return redirect('emergency_slots_report')
        return redirect('patient_detail', patient_id=patient.id)

    today_date = date.today()
    today = today_date.isoformat()
    scheduled_date = sched.original_scheduled_date or sched.date_of_start
    max_intended_date = None
    if scheduled_date and scheduled_date > today_date:
        max_intended_date = (scheduled_date - timedelta(days=1)).isoformat()

    if sched.intended_start_date and sched.intended_start_date >= today_date:
        if max_intended_date and sched.intended_start_date.isoformat() > max_intended_date:
            default_intended_date = today
        else:
            default_intended_date = sched.intended_start_date.isoformat()
    else:
        default_intended_date = today

    jr_user_names = [u.get_full_name().strip() or u.username for u in jr_users]

    context = {
        'sched': sched,
        'patient': patient,
        'all_machines': all_machines,
        'all_shifts': all_shifts,
        'jr_users': jr_users,
        'jr_user_names': jr_user_names,
        'emergency_reasons': emergency_reasons,
        'today': today,
        'today_date': today_date,
        'scheduled_date': scheduled_date,
        'max_intended_date': max_intended_date,
        'default_intended_date': default_intended_date,
        'next_url': next_url,
        'is_editing': sched.is_emergency_slot,
    }
    return render(request, 'scheduler/emergency_slot_form.html', context)


@login_required
def revert_emergency_slot_view(request, schedule_id):
    """
    Revert a patient course from Emergency / Vacant slot back to normal schedule status.
    """
    sched = get_object_or_404(TreatmentSchedule, id=schedule_id)
    patient = sched.patient

    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Only Admin and SubAdmin are authorized to revert emergency slots.")
        return redirect('patient_detail', patient_id=patient.id)

    if sched.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Treatment Course #{sched.id} is frozen (> 1 month past scheduled completion). Cannot revert frozen course.")
        return redirect('patient_detail', patient_id=patient.id)

    if sched.treatment_type == 'Brachytherapy':
        messages.error(request, "Emergency slot option is not applicable for Brachytherapy courses.")
        return redirect('patient_detail', patient_id=patient.id)

    if request.method == 'POST':
        prev_cat = sched.slot_category or 'Emergency'
        sched.is_emergency_slot = False
        sched.slot_category = None
        sched.emergency_reason = None
        if sched.original_scheduled_date:
            sched.date_of_start = sched.original_scheduled_date
            sched.status = 'Scheduled'
        else:
            sched.status = 'Pending'

        sched.save()

        AuditLog.objects.create(
            user=request.user,
            action="EMERGENCY_SLOT_REVERTED",
            mr_no=str(patient.mr_no),
            details=f"Reverted {prev_cat} slot for {patient.pt_name} (MR: {patient.mr_no}) back to normal schedule queue.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Course #{sched.id} reverted from {prev_cat} slot to normal schedule queue.")
        return redirect('patient_detail', patient_id=patient.id)

    return redirect('patient_detail', patient_id=patient.id)


# --- Brachytherapy Multi-Date Scheduling & Visual Monthly Calendar ---

def get_month_calendar_data(year, month, user=None):
    """
    Constructs a 7-column (Monday through Sunday) grid of weeks for the specified month and year,
    annotated with all active BrachytherapyAppointment records.
    Restricts historical lookback to the past 4 months for non-administrators.
    """
    cal = calendar.Calendar(firstweekday=0)  # Monday=0, Sunday=6
    month_days = cal.monthdatescalendar(year, month)

    first_date = month_days[0][0]
    last_date = month_days[-1][-1]

    appts = BrachytherapyAppointment.objects.filter(
        appointment_date__gte=first_date,
        appointment_date__lte=last_date
    ).select_related(
        'patient', 'applicator_type', 'anesthesia_type', 'created_by', 'schedule__machine'
    ).order_by('appointment_date', 'fraction_number', 'patient__pt_name')

    # Hospital Holidays in visible calendar range
    holidays = Holiday.objects.filter(
        date__gte=first_date,
        date__lte=last_date
    )
    holiday_map = {h.date.isoformat(): (h.comment or 'Hospital Holiday') for h in holidays}

    appts_by_date = {}
    for a in appts:
        d_str = a.appointment_date.isoformat()
        if d_str not in appts_by_date:
            appts_by_date[d_str] = []
        appts_by_date[d_str].append({
            'id': a.id,
            'patient_id': a.patient.id,
            'patient_name': a.patient.pt_name,
            'age_sex': a.patient.age_sex_display,
            'phone_no': a.patient.phone_no or '-',
            'rt_no': a.patient.rt_no or '-',
            'mr_no': a.patient.mr_no,
            'diagnosis': a.patient.diagnosis or '',
            'site': a.site_of_treatment or a.patient.site_of_treatment or '',
            'machine': a.schedule.machine.name if (a.schedule and a.schedule.machine) else (a.schedule.machine_display if a.schedule else ''),
            'machine_code': a.schedule.machine.code if (a.schedule and a.schedule.machine) else '',
            'applicator': a.applicator_type.name if a.applicator_type else 'Brachytherapy',
            'applicator_code': a.applicator_type.code if a.applicator_type else '',
            'color': a.applicator_type.color_code if a.applicator_type else '#1976d2',
            'text_color': a.applicator_type.text_color if a.applicator_type else '#ffffff',
            'anesthesia': a.anesthesia_type.name if a.anesthesia_type else 'Not specified',
            'fraction': a.fraction_number,
            'fractions_treated': a.fractions_treated or 1,
            'total_applications': a.schedule.total_brachy_applications if a.schedule else a.patient.brachy_appointments.count(),
            'fractions_delivered': a.schedule.total_brachy_fractions_delivered if a.schedule else sum(p.fractions_treated or 1 for p in a.patient.brachy_appointments.filter(status='Completed')),
            'date': a.appointment_date.strftime('%d-%b-%Y'),
            'date_iso': d_str,
            'status': a.status,
            'remarks': a.remarks or '',
        })

    today = timezone.localdate()
    four_months_ago = get_four_months_ago(today)
    is_admin = is_admin_only(user) if user else True

    min_cal_year = four_months_ago.year
    min_cal_month = four_months_ago.month

    weeks = []
    for week in month_days:
        week_data = []
        for d in week:
            d_str = d.isoformat()
            d_appts = appts_by_date.get(d_str, [])
            is_hol = d_str in holiday_map
            hol_comm = holiday_map.get(d_str, '')
            week_data.append({
                'date_iso': d_str,
                'date_formatted': d.strftime('%d-%b-%Y'),
                'day': d.day,
                'weekday': d.weekday(),
                'is_current_month': (d.month == month),
                'is_today': (d == today),
                'is_past': (d < today),
                'is_holiday': is_hol,
                'holiday_comment': hol_comm,
                'appointments': d_appts,
                'appointment_count': len(d_appts),
            })
        weeks.append(week_data)

    prev_month_date = date(year, month, 1) - timedelta(days=1)
    next_month_date = date(year, month, 28) + timedelta(days=5)

    if not is_admin:
        can_go_prev = (prev_month_date.year, prev_month_date.month) >= (min_cal_year, min_cal_month)
    else:
        can_go_prev = True

    return {
        'year': year,
        'month': month,
        'month_name': calendar.month_name[month],
        'month_and_year': f"{calendar.month_name[month]} {year}".upper(),
        'weeks': weeks,
        'holiday_map': holiday_map,
        'holiday_map_json': json.dumps(holiday_map),
        'prev_year': prev_month_date.year,
        'prev_month': prev_month_date.month,
        'next_year': next_month_date.year,
        'next_month': next_month_date.month,
        'today_iso': today.isoformat(),
        'all_months': [(m, calendar.month_name[m]) for m in range(1, 13)],
        'all_years': list(range(min_cal_year if not is_admin else today.year - 1, today.year + 4)),
        'can_go_prev': can_go_prev,
        'min_calendar_year': min_cal_year if not is_admin else None,
        'min_calendar_month': min_cal_month if not is_admin else None,
        'min_calendar_date': four_months_ago.isoformat() if not is_admin else None,
        'is_admin': is_admin,
    }


@login_required
def brachy_calendar_month_api(request):
    """
    JSON API for dynamic AJAX calendar navigation across months without page reloads.
    Returns calendar weeks grid and appointments for requested month and year.
    Enforces 4-month historical lookback limit for non-administrators.
    """
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month

    if month < 1 or month > 12:
        month = today.month

    four_months_ago = get_four_months_ago(today)
    is_admin = is_admin_only(request.user)

    if not is_admin:
        min_year = four_months_ago.year
        min_month = four_months_ago.month
        if (year, month) < (min_year, min_month):
            return JsonResponse({
                'success': False,
                'error': f"Calendar access restricted: Non-administrative users can only access calendar schedules from the past 4 months (from {calendar.month_name[min_month]} {min_year} onwards). Older dates are restricted to Administrators."
            }, status=403)

    data = get_month_calendar_data(year, month, user=request.user)
    return JsonResponse({'success': True, 'calendar': data})


@login_required
def brachy_calendar_view(request):
    """
    Standalone Departmental Brachytherapy Visual Monthly Calendar.
    Displays all scheduled brachytherapy appointments with color coding, tooltips, +N more popovers.
    Enforces 4-month historical navigation limit for non-administrators.
    """
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month

    if month < 1 or month > 12:
        year, month = today.year, today.month

    four_months_ago = get_four_months_ago(today)
    is_admin = is_admin_only(request.user)

    if not is_admin:
        min_year = four_months_ago.year
        min_month = four_months_ago.month
        if (year, month) < (min_year, min_month):
            messages.warning(
                request,
                f"Calendar access restricted: Non-administrative users can only access calendar schedules from the past 4 months (from {calendar.month_name[min_month]} {min_year} onwards). Older dates are restricted to Administrators."
            )
            year, month = min_year, min_month

    calendar_data = get_month_calendar_data(year, month, user=request.user)
    applicators = BrachyApplicator.objects.filter(is_active=True).order_by('display_order', 'name')

    context = {
        'calendar': calendar_data,
        'applicators': applicators,
        'today': today,
    }
    return render(request, 'scheduler/brachy_calendar.html', context)


@login_required
def brachy_schedule_patient_view(request, patient_id):
    """
    Brachytherapy Patient Scheduling View.
    Displays Patient Details banner, Brachytherapy parameters form (Applicator, Anaesthesia, Site, Remarks),
    interactive visual monthly calendar with multi-date selection, max 4 appointments restriction
    (with consultant authorization for >4), and confirmation summary step.
    """
    if not can_schedule_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to schedule patients.")
        return redirect('patient_detail', patient_id=patient_id)

    patient = get_object_or_404(Patient, id=patient_id)
    today = timezone.localdate()

    existing_brachy = patient.schedules.filter(treatment_type='Brachytherapy').first()
    if existing_brachy and existing_brachy.is_frozen and not is_admin_only(request.user):
        messages.error(request, f"Brachytherapy Course #{existing_brachy.id} is frozen (> 1 month past scheduled completion). Only Administrators can modify frozen courses.")
        return redirect('patient_detail', patient_id=patient.id)

    applicators = BrachyApplicator.objects.filter(is_active=True).order_by('display_order', 'name')
    anesthesias = BrachyAnesthesia.objects.filter(is_active=True).order_by('display_order', 'name')
    sites = TreatmentSite.objects.filter(is_active=True).order_by('display_order', 'name')
    brachy_machines = Machine.objects.filter(is_active=True, treatment_mode='Brachytherapy').order_by('display_order', 'name')
    # Prepopulate treatment site from patient's previous entry
    default_site = patient.site_of_treatment or ''
    if not default_site:
        latest_sched = patient.schedules.filter(site_of_treatment__isnull=False).exclude(site_of_treatment='').first()
        if latest_sched:
            default_site = latest_sched.site_of_treatment

    sites_names = [s.name.lower() for s in sites]
    default_site_not_in_sites = bool(default_site and default_site.lower() not in sites_names)

    # Existing active brachytherapy appointments for this patient
    # As long as a Brachy course is active, a new application scheduled will be the prev App no + 1
    course_appts = patient.brachy_appointments.all()
    if existing_brachy:
        course_appts = course_appts.filter(Q(schedule=existing_brachy) | Q(schedule__isnull=True))

    last_appt_in_course = course_appts.order_by('-fraction_number').first()
    prev_max_app_num = last_appt_in_course.fraction_number if last_appt_in_course else 0

    existing_appts = course_appts.exclude(status='Cancelled').select_related('applicator_type', 'anesthesia_type').order_by('appointment_date')

    # Uncompleted previous brachytherapy appointments (status not entered for an appointment whose date has arrived)
    uncompleted_prev_appts = patient.brachy_appointments.filter(
        completion_status__isnull=True,
        status='Scheduled',
        appointment_date__lte=today
    ).select_related('applicator_type', 'anesthesia_type').order_by('fraction_number', 'appointment_date')
    uncompleted_prev_appt = uncompleted_prev_appts.first()

    completion_statuses = TreatmentCompletionStatus.objects.filter(is_active=True).order_by('display_order', 'name')

    # Latest remarks/notes for further application
    initial_remarks = ""
    last_with_rem = patient.brachy_appointments.exclude(remarks__isnull=True).exclude(remarks='').order_by('-appointment_date', '-id').first()
    if last_with_rem and last_with_rem.remarks:
        initial_remarks = last_with_rem.remarks
    elif existing_brachy and existing_brachy.notes:
        initial_remarks = existing_brachy.notes

    # User role privileges
    user_is_consultant = is_consultant_or_admin(request.user)
    user_is_res = is_resident(request.user)

    # Active consultants for supervisor authorization dropdown
    active_consultants = User.objects.filter(
        Q(profile__role__role_name__icontains='Consultant') |
        Q(profile__role__role_name__icontains='Faculty') |
        Q(profile__role__role_id__in=[1, 2, 3]) |
        Q(is_superuser=True),
        is_active=True
    ).distinct().order_by('first_name', 'username')

    if request.method == 'POST':
        is_ajax = (request.headers.get('x-requested-with') == 'XMLHttpRequest' or 
                   request.content_type == 'application/json' or
                   request.POST.get('is_ajax') == '1')
        
        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body.decode('utf-8'))
            except Exception:
                data = {}
        else:
            data = request.POST

        selected_dates_raw = data.get('selected_dates')
        if isinstance(selected_dates_raw, str):
            try:
                selected_dates_list = json.loads(selected_dates_raw)
            except Exception:
                selected_dates_list = [d.strip() for d in selected_dates_raw.split(',') if d.strip()]
        elif isinstance(selected_dates_raw, (list, tuple)):
            selected_dates_list = selected_dates_raw
        else:
            selected_dates_list = request.POST.getlist('selected_dates[]') or request.POST.getlist('selected_dates')

        # Clean and parse dates
        parsed_dates = []
        for d_str in selected_dates_list:
            d_str = str(d_str).strip()
            if not d_str:
                continue
            try:
                p_date = datetime.strptime(d_str, '%Y-%m-%d').date()
                parsed_dates.append(p_date)
            except ValueError:
                pass

        if not parsed_dates:
            err_msg = "Please select at least one appointment date from the calendar."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('brachy_schedule_patient', patient_id=patient.id)

        # Remove duplicates and sort chronologically
        parsed_dates = sorted(list(set(parsed_dates)))

        applicator_id = data.get('applicator_id')
        anesthesia_id = data.get('anesthesia_id')
        site_of_treatment = (data.get('site_of_treatment') or default_site or '').strip()
        remarks = (data.get('remarks') or '').strip()

        applicator = BrachyApplicator.objects.filter(id=applicator_id).first() if applicator_id else None
        anesthesia = BrachyAnesthesia.objects.filter(id=anesthesia_id).first() if anesthesia_id else None

        if not applicator:
            err_msg = "Please select a valid Applicator Type."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('brachy_schedule_patient', patient_id=patient.id)

        if not anesthesia:
            err_msg = "Please select a valid Anaesthesia Type."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('brachy_schedule_patient', patient_id=patient.id)

        # Same-date selection validation against existing active appointments
        existing_date_set = set(existing_appts.values_list('appointment_date', flat=True))
        conflicts = [d.strftime('%d-%b-%Y') for d in parsed_dates if d in existing_date_set]
        if conflicts:
            err_msg = f"Duplicate appointment date detected: {', '.join(conflicts)} already scheduled for this patient."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('brachy_schedule_patient', patient_id=patient.id)

        # Total appointment count & 4-appointment rule
        total_appts = len(existing_appts) + len(parsed_dates)
        authorized_by = None
        is_consultant_authorized = False

        if total_appts > 4:
            if user_is_consultant:
                authorized_by = request.user
                is_consultant_authorized = True
            else:
                # Resident attempting > 4 appointments
                consultant_id = data.get('consultant_id')
                consultant_pw = data.get('consultant_password')

                auth_ok = False
                if consultant_id:
                    consultant_user = User.objects.filter(id=consultant_id, is_active=True).first()
                    if consultant_user and is_consultant_or_admin(consultant_user):
                        if consultant_pw:
                            from django.contrib.auth import authenticate
                            auth_user = authenticate(username=consultant_user.username, password=consultant_pw)
                            if auth_user:
                                auth_ok = True
                                authorized_by = consultant_user
                                is_consultant_authorized = True
                        else:
                            auth_ok = True
                            authorized_by = consultant_user
                            is_consultant_authorized = True

                if not auth_ok:
                    err_msg = "Maximum of four brachytherapy appointments permitted. Additional appointments require consultant authorization."
                    if is_ajax:
                        return JsonResponse({'success': False, 'error': err_msg, 'requires_authorization': True}, status=403)
                    messages.error(request, err_msg)
                    return redirect('brachy_schedule_patient', patient_id=patient.id)

        # Requirement 1: If patient already has a previous application whose date has arrived (<= today)
        # and its status is not entered, ask/require completion status of previous application before scheduling next application.
        uncompleted_prev = patient.brachy_appointments.filter(
            completion_status__isnull=True,
            status='Scheduled',
            appointment_date__lte=today
        ).order_by('fraction_number', 'appointment_date').first()

        if uncompleted_prev:
            err_msg = f"Application status for App {uncompleted_prev.fraction_number} ({uncompleted_prev.appointment_date.strftime('%d-%b-%Y')}) is not entered. Please record its completion status before scheduling subsequent applications."
            if is_ajax:
                return JsonResponse({
                    'success': False,
                    'error': err_msg,
                    'requires_prev_status': True,
                    'prev_appt_id': uncompleted_prev.id,
                    'prev_fraction': uncompleted_prev.fraction_number,
                    'prev_date': uncompleted_prev.appointment_date.strftime('%d-%b-%Y'),
                    'prev_remarks': uncompleted_prev.remarks or initial_remarks or '',
                }, status=400)
            messages.error(request, err_msg)
            return redirect('brachy_schedule_patient', patient_id=patient.id)

        # Selected Brachytherapy Machine
        machine_id = data.get('machine_id')
        machine_obj = None
        if machine_id:
            machine_obj = Machine.objects.filter(id=machine_id, treatment_mode='Brachytherapy', is_active=True).first()
        elif brachy_machines.exists():
            machine_obj = brachy_machines.first()

        # Persist schedule course
        brachy_schedule = patient.schedules.filter(
            treatment_type='Brachytherapy',
            status__in=['Scheduled', 'Draft', 'Pending']
        ).first()

        all_course_dates = sorted(list(existing_date_set | set(parsed_dates)))
        start_date = all_course_dates[0]
        end_date = all_course_dates[-1]

        if not brachy_schedule:
            brachy_schedule = TreatmentSchedule.objects.create(
                patient=patient,
                treatment_type='Brachytherapy',
                machine=machine_obj,
                applicator_type=applicator,
                anesthesia_type=anesthesia,
                site_of_treatment=site_of_treatment,
                notes=remarks,
                date_of_start=start_date,
                date_of_end=end_date,
                no_of_fractions=len(all_course_dates),
                status='Scheduled',
                created_by=request.user,
            )
        else:
            if machine_obj:
                brachy_schedule.machine = machine_obj
            brachy_schedule.applicator_type = applicator
            brachy_schedule.anesthesia_type = anesthesia
            brachy_schedule.site_of_treatment = site_of_treatment
            if remarks:
                brachy_schedule.notes = (brachy_schedule.notes + "\n" + remarks).strip() if brachy_schedule.notes else remarks
            brachy_schedule.date_of_start = min(brachy_schedule.date_of_start or start_date, start_date)
            brachy_schedule.date_of_end = max(brachy_schedule.date_of_end or end_date, end_date)
            brachy_schedule.no_of_fractions = len(all_course_dates)
            brachy_schedule.status = 'Scheduled'
            brachy_schedule.save()

        # Determine starting application number for new bookings:
        # As long as a Brachy course is active, a new application scheduled will be prev App no + 1
        last_appt_in_course = patient.brachy_appointments.filter(
            Q(schedule=brachy_schedule) | Q(schedule__isnull=True)
        ).order_by('-fraction_number').first()
        effective_existing_count = last_appt_in_course.fraction_number if last_appt_in_course else 0

        # Create BrachytherapyAppointment records
        created_appts = []
        curr_frac = effective_existing_count + 1
        for p_date in parsed_dates:
            appt = BrachytherapyAppointment.objects.create(
                schedule=brachy_schedule,
                patient=patient,
                fraction_number=curr_frac,
                appointment_date=p_date,
                applicator_type=applicator,
                anesthesia_type=anesthesia,
                site_of_treatment=site_of_treatment,
                remarks=remarks,
                status='Scheduled',
                created_by=request.user,
                authorized_by=authorized_by,
                is_consultant_authorized=is_consultant_authorized,
            )
            created_appts.append(appt)
            curr_frac += 1

        recalculate_patient_brachy_fractions(patient)

        # Audit logging
        auth_note = f" (Authorized by Consultant {authorized_by.get_full_name() or authorized_by.username})" if is_consultant_authorized and authorized_by else ""
        AuditLog.objects.create(
            user=request.user,
            action="BRACHY_APPOINTMENT_SCHEDULE",
            mr_no=str(patient.mr_no),
            details=f"Scheduled {len(created_appts)} brachytherapy fraction(s) on: {', '.join(d.strftime('%d-%b-%Y') for d in parsed_dates)}. Applicator: {applicator.name}, Anaesthesia: {anesthesia.name}{auth_note}.",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        success_msg = f"Successfully scheduled {len(created_appts)} Brachytherapy appointments for {patient.pt_name} (MR: {patient.mr_no})."
        messages.success(request, success_msg)

        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': success_msg,
                'redirect_url': f'/patients/{patient.id}/'
            })

        return redirect('patient_detail', patient_id=patient.id)

    # Initial GET rendering
    target_date_str = request.GET.get('target_date', '').strip()
    preselect_target_date = ''
    if target_date_str:
        try:
            target_date_obj = datetime.strptime(target_date_str, '%Y-%m-%d').date()
            preselect_target_date = target_date_str
            if 'year' not in request.GET and 'month' not in request.GET:
                year, month = target_date_obj.year, target_date_obj.month
        except ValueError:
            pass

    try:
        if 'year' in request.GET or 'month' in request.GET or not preselect_target_date:
            year = int(request.GET.get('year', today.year))
            month = int(request.GET.get('month', today.month))
    except (ValueError, TypeError):
        year, month = today.year, today.month

    if month < 1 or month > 12:
        year, month = today.year, today.month

    calendar_data = get_month_calendar_data(year, month, user=request.user)

    selected_machine_id = None
    if existing_brachy and existing_brachy.machine_id:
        selected_machine_id = existing_brachy.machine_id
    elif brachy_machines.exists():
        selected_machine_id = brachy_machines.first().id

    context = {
        'patient': patient,
        'brachy_machines': brachy_machines,
        'selected_machine_id': selected_machine_id,
        'applicators': applicators,
        'anesthesias': anesthesias,
        'sites': sites,
        'default_site': default_site,
        'default_site_not_in_sites': default_site_not_in_sites,
        'existing_appts': existing_appts,
        'existing_appts_count': prev_max_app_num,
        'total_applications_count': course_appts.count(),
        'total_fractions_delivered': sum(a.fractions_treated or 1 for a in course_appts.filter(status='Completed')),
        'uncompleted_prev_appt': uncompleted_prev_appt,
        'completion_statuses': completion_statuses,
        'initial_remarks': initial_remarks,
        'preselect_target_date': preselect_target_date,
        'calendar': calendar_data,
        'user_is_consultant': user_is_consultant,
        'user_is_resident': user_is_res,
        'active_consultants': active_consultants,
        'current_user_name': request.user.get_full_name() or request.user.username,
        'today': today,
    }
    return render(request, 'scheduler/brachy_schedule_patient.html', context)


def recalculate_patient_brachy_fractions(patient):
    """
    Recalculates application / fraction numbers for all brachytherapy appointments of a patient.
    Clinical Rule:
    - If the selection is "Completed", that application (App x) is considered completed,
      and the next application would be App (x+1).
    - If an application is Incomplete, Defaulted, or Cancelled, it is NOT completed;
      hence the next application attempt remains at that required application number.
    """
    appts = list(patient.brachy_appointments.all().order_by('appointment_date', 'id'))
    completed_so_far = 0
    for a in appts:
        is_comp = (a.status == 'Completed') or (a.completion_status and a.completion_status.is_completed)
        if is_comp:
            completed_so_far += 1
            if a.fraction_number != completed_so_far:
                a.fraction_number = completed_so_far
                a.save(update_fields=['fraction_number'])
        elif a.status in ['Incomplete', 'Defaulted', 'Discontinued', 'Cancelled']:
            # Non-completed application: stays as the application attempted
            target = completed_so_far + 1
            if a.fraction_number != target:
                a.fraction_number = target
                a.save(update_fields=['fraction_number'])
        else:
            # Scheduled / Upcoming
            completed_so_far += 1
            target = completed_so_far
            if a.fraction_number != target:
                a.fraction_number = target
                a.save(update_fields=['fraction_number'])


def get_appointment_brachy_fraction_info(a):
    """
    Computes Fraction No and checks for unfinalized prior appointments in this patient's brachytherapy course.
    """
    prior_appts = a.patient.brachy_appointments.filter(
        Q(appointment_date__lt=a.appointment_date) |
        Q(appointment_date=a.appointment_date, fraction_number__lt=a.fraction_number)
    ).exclude(status='Cancelled').select_related('applicator_type', 'anesthesia_type', 'completion_status').order_by('appointment_date', 'fraction_number')

    incomplete_prior_list = []
    if not prior_appts.exists() or a.fraction_number == 1:
        fraction_no_disp = 1
        has_unfinalized_prev = False
    else:
        unfinalized_prior = [
            p for p in prior_appts
            if not p.completion_status or (not p.completion_status.is_completed and p.status != 'Completed')
        ]
        if unfinalized_prior:
            has_unfinalized_prev = True
            fraction_no_disp = None
            for p in unfinalized_prior:
                incomplete_prior_list.append({
                    'id': p.id,
                    'patient_id': p.patient_id,
                    'patient_name': p.patient.pt_name,
                    'mr_no': p.patient.mr_no,
                    'fraction_number': p.fraction_number,
                    'appointment_date': p.appointment_date.strftime('%d-%b-%Y'),
                    'appointment_date_iso': p.appointment_date.isoformat(),
                    'applicator_name': p.applicator_type.name if p.applicator_type else 'Brachytherapy',
                    'anesthesia_name': p.anesthesia_type.name if p.anesthesia_type else '-',
                    'status': p.status,
                    'completion_status_id': p.completion_status_id or '',
                    'fractions_treated': p.fractions_treated or 1,
                    'remarks': p.remarks or '',
                })
        else:
            has_unfinalized_prev = False
            total_prev_fractions = sum(p.fractions_treated or 1 for p in prior_appts)
            fraction_no_disp = total_prev_fractions + 1

    return fraction_no_disp, has_unfinalized_prev, incomplete_prior_list


def is_brachy_status_active_now(appt_date):
    """
    Clinical rule: The application status of the current application should only be active
    on the current day (the day of the application scheduled), 1000 hrs.
    Past dates: already reached/active.
    Today: active only at or after 10:00 AM (1000 hrs).
    Future dates: not active yet.
    """
    now = timezone.localtime()
    today = now.date()
    ten_am = dt_time(10, 0)
    if appt_date < today:
        return True, False
    elif appt_date == today:
        is_active = (now.time() >= ten_am)
        is_today_before_10 = (now.time() < ten_am)
        return is_active, is_today_before_10
    else:
        return False, False


@login_required
def brachy_daily_patients_view(request):
    """
    Renders the Daily Scheduled Patients List for a requested date.
    Displays columns: SL No, MR No, Name, Age/Sex, Diagnosis, Applicator type, Anesthesia type, Brachy Machine, Consultant, Unit, Brachy Treatment Status, Actions, Remarks/Notes.
    Includes option to filter by date, edit applicator details, and record/update treatment status.
    """
    date_str = request.GET.get('date', '').strip()
    today = timezone.localdate()
    target_date = today
    if date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
            try:
                target_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

    appts = BrachytherapyAppointment.objects.filter(
        appointment_date=target_date
    ).exclude(status='Cancelled').select_related(
        'patient', 'patient__primary_consultant',
        'applicator_type', 'anesthesia_type',
        'completion_status',
        'schedule', 'schedule__machine'
    ).order_by('fraction_number', 'patient__pt_name')

    applicators = BrachyApplicator.objects.filter(is_active=True).order_by('display_order', 'name')
    anesthesias = BrachyAnesthesia.objects.filter(is_active=True).order_by('display_order', 'name')
    brachy_machines = Machine.objects.filter(
        is_active=True,
        treatment_mode='Brachytherapy'
    ).order_by('display_order', 'name')
    completion_statuses = TreatmentCompletionStatus.objects.filter(is_active=True).order_by('display_order', 'name')

    is_date_reached, is_today_before_10 = is_brachy_status_active_now(target_date)

    has_incomplete_prev_appts = False

    rows = []
    for idx, a in enumerate(appts, 1):
        app_type_disp = '-'
        if a.applicator_type:
            app_type_disp = a.applicator_type.applicator_type or a.applicator_type.name

        consultant_name = a.patient.primary_consultant.name if a.patient.primary_consultant else 'Unassigned'
        unit_disp = str(a.patient.unit).replace('Unit', '').strip() if a.patient.unit else "I"
        if not unit_disp:
            unit_disp = "I"

        machine_name = 'Brachy Unit'
        if a.schedule and a.schedule.machine:
            machine_name = a.schedule.machine.name
        elif a.schedule and a.schedule.machine_display:
            machine_name = a.schedule.machine_display

        status_disp = a.status
        if a.completion_status:
            status_disp = a.completion_status.name

        remarks_val = a.remarks
        if not remarks_val:
            last_app_rem = a.patient.brachy_appointments.exclude(remarks__isnull=True).exclude(remarks='').order_by('-appointment_date', '-id').first()
            if last_app_rem and last_app_rem.remarks:
                remarks_val = last_app_rem.remarks
            elif a.schedule and a.schedule.notes:
                remarks_val = a.schedule.notes
        remarks_val = remarks_val or ''

        # Fraction No & unfinalized prior calculations
        fraction_no_disp, has_unfinalized_prev, incomplete_prior_list = get_appointment_brachy_fraction_info(a)
        if has_unfinalized_prev:
            has_incomplete_prev_appts = True

        appt_is_date_reached, appt_is_today_before_10 = is_brachy_status_active_now(a.appointment_date)

        rows.append({
            'sl_no': idx,
            'id': a.id,
            'patient_id': a.patient.id,
            'patient_name': a.patient.pt_name,
            'mr_no': a.patient.mr_no,
            'rt_no': a.patient.rt_no or '-',
            'age_sex': a.patient.age_sex_display,
            'fraction_no': fraction_no_disp,
            'has_unfinalized_prev': has_unfinalized_prev,
            'incomplete_prior': incomplete_prior_list,
            'incomplete_prior_json': json.dumps(incomplete_prior_list),
            'diagnosis': a.patient.diagnosis or 'Not specified',
            'applicator_name': a.applicator_type.name if a.applicator_type else 'Brachytherapy',
            'applicator_id': a.applicator_type_id,
            'applicator_type': app_type_disp,
            'color': a.applicator_type.color_code if a.applicator_type else '#1976d2',
            'anesthesia_name': a.anesthesia_type.name if a.anesthesia_type else '-',
            'anesthesia_id': a.anesthesia_type_id,
            'machine_name': machine_name,
            'machine_id': a.schedule.machine_id if a.schedule else None,
            'consultant': consultant_name,
            'unit': unit_disp,
            'fraction': a.fraction_number,
            'fractions_treated': a.fractions_treated or 1,
            'remarks': remarks_val,
            'status': a.status,
            'status_display': status_disp,
            'completion_status_id': a.completion_status_id,
            'is_completed': (a.status == 'Completed') or (a.completion_status and a.completion_status.is_completed),
            'is_date_reached': appt_is_date_reached,
            'is_today_before_10': appt_is_today_before_10,
        })

    context = {
        'target_date': target_date,
        'date_iso': target_date.isoformat(),
        'date_formatted': target_date.strftime('%d-%b-%Y'),
        'rows': rows,
        'count': len(rows),
        'applicators': applicators,
        'anesthesias': anesthesias,
        'brachy_machines': brachy_machines,
        'completion_statuses': completion_statuses,
        'can_edit': is_admin_or_subadmin(request.user),
        'is_date_reached': is_date_reached,
        'is_today_before_10': is_today_before_10,
        'has_incomplete_prev_appts': has_incomplete_prev_appts,
        'today_iso': today.isoformat(),
    }

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('format') == 'json':
        comp_statuses = list(completion_statuses.values('id', 'name', 'is_completed'))
        return JsonResponse({
            'success': True,
            'date_iso': target_date.isoformat(),
            'date_formatted': target_date.strftime('%d-%b-%Y'),
            'is_date_reached': is_date_reached,
            'is_today_before_10': is_today_before_10,
            'has_incomplete_prev_appts': has_incomplete_prev_appts,
            'count': len(rows),
            'appointments': rows,
            'completion_statuses': comp_statuses,
        })

    return render(request, 'scheduler/brachy_daily_patients.html', context)


@login_required
def brachy_daily_patients_api(request):
    """
    JSON API endpoint returning the list of patients scheduled for a date.
    Query param: ?date=YYYY-MM-DD
    """
    date_str = request.GET.get('date', '').strip()
    today = timezone.localdate()
    target_date = today
    if date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
            try:
                target_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

    appts = BrachytherapyAppointment.objects.filter(
        appointment_date=target_date
    ).exclude(status='Cancelled').select_related(
        'patient', 'patient__primary_consultant',
        'applicator_type', 'anesthesia_type',
        'completion_status',
        'schedule', 'schedule__machine'
    ).order_by('fraction_number', 'patient__pt_name')

    has_incomplete_prev_appts = False

    rows = []
    for idx, a in enumerate(appts, 1):
        app_type_disp = '-'
        if a.applicator_type:
            app_type_disp = a.applicator_type.applicator_type or a.applicator_type.name

        consultant_name = a.patient.primary_consultant.name if a.patient.primary_consultant else 'Unassigned'
        unit_disp = str(a.patient.unit).replace('Unit', '').strip() if a.patient.unit else "I"
        if not unit_disp:
            unit_disp = "I"

        machine_name = 'Brachy Unit'
        if a.schedule and a.schedule.machine:
            machine_name = a.schedule.machine.name
        elif a.schedule and a.schedule.machine_display:
            machine_name = a.schedule.machine_display

        status_disp = a.status
        if a.completion_status:
            status_disp = a.completion_status.name

        remarks_val = a.remarks
        if not remarks_val:
            last_app_rem = a.patient.brachy_appointments.exclude(remarks__isnull=True).exclude(remarks='').order_by('-appointment_date', '-id').first()
            if last_app_rem and last_app_rem.remarks:
                remarks_val = last_app_rem.remarks
            elif a.schedule and a.schedule.notes:
                remarks_val = a.schedule.notes
        remarks_val = remarks_val or ''

        # Fraction No & unfinalized prior calculations
        fraction_no_disp, has_unfinalized_prev, incomplete_prior_list = get_appointment_brachy_fraction_info(a)
        if has_unfinalized_prev:
            has_incomplete_prev_appts = True

        appt_is_date_reached, appt_is_today_before_10 = is_brachy_status_active_now(a.appointment_date)

        rows.append({
            'sl_no': idx,
            'id': a.id,
            'patient_id': a.patient.id,
            'patient_name': a.patient.pt_name,
            'mr_no': a.patient.mr_no,
            'rt_no': a.patient.rt_no or '-',
            'age_sex': a.patient.age_sex_display,
            'fraction_no': fraction_no_disp,
            'has_unfinalized_prev': has_unfinalized_prev,
            'incomplete_prior': incomplete_prior_list,
            'incomplete_prior_json': json.dumps(incomplete_prior_list),
            'diagnosis': a.patient.diagnosis or 'Not specified',
            'applicator_name': a.applicator_type.name if a.applicator_type else 'Brachytherapy',
            'applicator_id': a.applicator_type_id,
            'applicator_type': app_type_disp,
            'color': a.applicator_type.color_code if a.applicator_type else '#1976d2',
            'anesthesia_name': a.anesthesia_type.name if a.anesthesia_type else '-',
            'anesthesia_id': a.anesthesia_type_id,
            'machine_name': machine_name,
            'machine_id': a.schedule.machine_id if a.schedule else None,
            'consultant': consultant_name,
            'unit': unit_disp,
            'fraction': a.fraction_number,
            'fractions_treated': a.fractions_treated or 1,
            'remarks': remarks_val,
            'status': a.status,
            'status_display': status_disp,
            'completion_status_id': a.completion_status_id,
            'is_completed': (a.status == 'Completed') or (a.completion_status and a.completion_status.is_completed),
            'is_date_reached': appt_is_date_reached,
            'is_today_before_10': appt_is_today_before_10,
        })

    comp_statuses = list(
        TreatmentCompletionStatus.objects.filter(is_active=True)
        .order_by('display_order', 'name')
        .values('id', 'name', 'is_completed')
    )

    is_date_reached, is_today_before_10 = is_brachy_status_active_now(target_date)

    return JsonResponse({
        'success': True,
        'date_iso': target_date.isoformat(),
        'date_formatted': target_date.strftime('%d-%b-%Y'),
        'is_date_reached': is_date_reached,
        'is_today_before_10': is_today_before_10,
        'has_incomplete_prev_appts': has_incomplete_prev_appts,
        'count': len(rows),
        'appointments': rows,
        'completion_statuses': comp_statuses,
    })


@login_required
def brachy_update_appointment_status_api(request, appointment_id):
    """
    API endpoint to record or update Brachytherapy Treatment Status for an appointment.
    Dropdown options sourced from TreatmentCompletionStatus in reference lookup tables.
    If 'Completed' is selected:
      - Marks the appointment as Completed (App x).
      - Re-indexes patient's brachytherapy fractions so the next application is App (x+1).
    """
    if not can_schedule_patient(request.user) and not can_edit_patient(request.user):
        return JsonResponse({'success': False, 'error': "Access restricted. Scheduling permission required."}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': "Invalid request method."}, status=405)

    appt = get_object_or_404(BrachytherapyAppointment, id=appointment_id)

    # Clinical rule: The application status of the current application should only be active
    # on the current day (the day of the application scheduled), 1000 hrs.
    now = timezone.localtime()
    today = now.date()
    ten_am = dt_time(10, 0)
    if appt.appointment_date > today:
        return JsonResponse({
            'success': False,
            'error': f"Application status for App {appt.fraction_number} cannot be recorded before its scheduled date ({appt.appointment_date.strftime('%d-%b-%Y')})."
        }, status=400)
    elif appt.appointment_date == today and now.time() < ten_am and request.POST.get('force') != '1' and not is_admin_only(request.user):
        return JsonResponse({
            'success': False,
            'error': f"Application status for App {appt.fraction_number} can only be recorded on the scheduled date ({appt.appointment_date.strftime('%d-%b-%Y')}) from 10:00 hrs."
        }, status=400)

    status_id = request.POST.get('status_id')
    status_name = request.POST.get('status_name')
    remarks = request.POST.get('remarks')
    fractions_treated_raw = request.POST.get('fractions_treated')

    status_obj = None
    if status_id:
        status_obj = TreatmentCompletionStatus.objects.filter(id=status_id).first()
    if not status_obj and status_name:
        status_obj = TreatmentCompletionStatus.objects.filter(name__iexact=status_name.strip()).first()

    if not status_obj:
        return JsonResponse({'success': False, 'error': "Please select a valid treatment completion status."}, status=400)

    # Update appointment status
    appt.completion_status = status_obj
    if status_obj.is_completed:
        appt.status = 'Completed'
    else:
        appt.status = status_obj.name

    if remarks is not None:
        appt.remarks = remarks.strip()

    if fractions_treated_raw:
        try:
            f_val = int(fractions_treated_raw)
            if f_val >= 1:
                appt.fractions_treated = f_val
        except (ValueError, TypeError):
            pass

    appt.save()

    # Recalculate fractions so next application is App (x+1)
    recalculate_patient_brachy_fractions(appt.patient)
    appt.refresh_from_db()

    # Update parent treatment schedule if present
    if appt.schedule:
        total_course_appts = appt.schedule.brachy_appointments.count()
        completed_course_appts = appt.schedule.brachy_appointments.filter(status='Completed').count()
        if total_course_appts > 0 and completed_course_appts == total_course_appts:
            appt.schedule.status = 'Completed'
            appt.schedule.actual_treatment_status = status_obj
            first_appt = appt.schedule.brachy_appointments.order_by('appointment_date').first()
            last_appt = appt.schedule.brachy_appointments.order_by('-appointment_date').first()
            if first_appt:
                appt.schedule.actual_start_date = first_appt.appointment_date
            if last_appt:
                appt.schedule.actual_end_date = last_appt.appointment_date
            appt.schedule.save()

    AuditLog.objects.create(
        user=request.user,
        action="BRACHY_TREATMENT_STATUS_UPDATE",
        mr_no=str(appt.patient.mr_no),
        details=f"Recorded Brachytherapy treatment status '{status_obj.name}' (Fractions: {appt.fractions_treated}) for {appt.patient.pt_name} (MR: {appt.patient.mr_no}) on {appt.appointment_date.strftime('%d-%b-%Y')} - Application {appt.fraction_number}.",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    return JsonResponse({
        'success': True,
        'message': f"Brachytherapy status '{status_obj.name}' saved successfully for Application {appt.fraction_number}.",
        'appointment_id': appt.id,
        'status': appt.status,
        'status_name': status_obj.name,
        'is_completed': status_obj.is_completed,
        'fraction_number': appt.fraction_number,
        'fractions_treated': appt.fractions_treated or 1,
        'remarks': appt.remarks or '',
    })


@login_required
def brachy_appointment_incomplete_prior_api(request, appointment_id):
    """
    API endpoint returning uncompleted prior brachytherapy applications for the patient of this appointment.
    """
    appt = get_object_or_404(BrachytherapyAppointment, id=appointment_id)
    _, has_unfinalized_prev, incomplete_prior_list = get_appointment_brachy_fraction_info(appt)
    return JsonResponse({
        'success': True,
        'appointment_id': appt.id,
        'patient_id': appt.patient_id,
        'patient_name': appt.patient.pt_name,
        'mr_no': appt.patient.mr_no,
        'has_unfinalized_prev': has_unfinalized_prev,
        'incomplete_prior': incomplete_prior_list,
    })


@login_required
def brachy_edit_appointment_api(request, appointment_id):
    """
    API to update details of an individual Brachytherapy appointment (applicator, applicator type, anesthesia, machine, notes, date).
    Permits clinicians to edit the details of each applicator appointment later.
    """
    if not is_admin_or_subadmin(request.user):
        return JsonResponse({'success': False, 'error': "Access restricted. Only Admin and SubAdmin can edit brachytherapy application appointments."}, status=403)

    appt = get_object_or_404(BrachytherapyAppointment, id=appointment_id)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': "Invalid request method."}, status=405)

    applicator_id = request.POST.get('applicator_id')
    anesthesia_id = request.POST.get('anesthesia_id')
    machine_id = request.POST.get('machine_id')
    remarks = request.POST.get('remarks')
    new_date_raw = request.POST.get('appointment_date')

    updated_fields = []

    if applicator_id:
        app_obj = BrachyApplicator.objects.filter(id=applicator_id).first()
        if app_obj:
            appt.applicator_type = app_obj
            updated_fields.append(f"Applicator: {app_obj.name}")

    if anesthesia_id:
        ana_obj = BrachyAnesthesia.objects.filter(id=anesthesia_id).first()
        if ana_obj:
            appt.anesthesia_type = ana_obj
            updated_fields.append(f"Anaesthesia: {ana_obj.name}")

    if remarks is not None:
        appt.remarks = remarks.strip()
        updated_fields.append("Remarks")

    if new_date_raw:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
            try:
                new_d = datetime.strptime(new_date_raw.strip(), fmt).date()
                appt.appointment_date = new_d
                updated_fields.append(f"Date: {new_d.strftime('%d-%b-%Y')}")
                break
            except ValueError:
                continue

    if machine_id and appt.schedule:
        m_obj = Machine.objects.filter(id=machine_id, is_active=True).first()
        if m_obj:
            appt.schedule.machine = m_obj
            appt.schedule.save(update_fields=['machine'])
            updated_fields.append(f"Machine: {m_obj.name}")

    appt.save()

    AuditLog.objects.create(
        user=request.user,
        action="BRACHY_APPOINTMENT_EDIT",
        mr_no=str(appt.patient.mr_no),
        details=f"Updated Brachy Appointment #{appt.id} (Fraction {appt.fraction_number}) for {appt.patient.pt_name}: {', '.join(updated_fields)}",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    return JsonResponse({
        'success': True,
        'message': f"Appointment #{appt.id} details updated successfully.",
        'applicator_name': appt.applicator_type.name if appt.applicator_type else '-',
        'applicator_type': (appt.applicator_type.applicator_type or appt.applicator_type.name) if appt.applicator_type else '-',
        'anesthesia_name': appt.anesthesia_type.name if appt.anesthesia_type else '-',
        'machine_name': appt.schedule.machine.name if (appt.schedule and appt.schedule.machine) else '-',
    })


@login_required
def brachy_search_patients_api(request):
    """
    Search registered patients by Hospital / MR number or Name for direct scheduling from the Brachy calendar.
    Accepts:
      q: search string (e.g. 20260001 or 'Jayamma')
      date or target_date: target date string 'YYYY-MM-DD'
    """
    if not can_schedule_patient(request.user) and not can_edit_patient(request.user):
        return JsonResponse({'success': False, 'error': "Access restricted. Scheduling permission required."}, status=403)

    query = request.GET.get('q', '').strip()
    target_date = request.GET.get('date', '').strip() or request.GET.get('target_date', '').strip()

    if not query:
        return JsonResponse({'success': True, 'patients': []})

    if query.isdigit():
        patients_qs = Patient.objects.filter(Q(mr_no=int(query)) | Q(phone_no__icontains=query))
    else:
        patients_qs = Patient.objects.filter(
            Q(pt_name__icontains=query) |
            Q(diagnosis__icontains=query) |
            Q(rt_no__icontains=query)
        )

    patients_qs = patients_qs.select_related('primary_consultant').prefetch_related('brachy_appointments__completion_status')[:15]

    results = []
    for p in patients_qs:
        appts = list(p.brachy_appointments.all().order_by('fraction_number'))
        total_apps = len(appts)
        fractions_del = sum(a.fractions_treated or 1 for a in appts if a.status == 'Completed' or (a.completion_status and a.completion_status.is_completed))
        if appts:
            parts = [f"Total: {total_apps} Apps, {fractions_del} Fr delivered"]
            for a in appts:
                st_name = a.completion_status.name if a.completion_status else a.status
                parts.append(f"App {a.fraction_number}: {st_name} ({a.appointment_date.strftime('%d-%b')})")
            brachy_summary = " | ".join(parts)
        else:
            brachy_summary = "No Brachytherapy scheduled yet"

        unit_str = str(p.unit).replace('Unit', '').strip() if p.unit else "I"
        if not unit_str:
            unit_str = "I"

        sched_url = f"/scheduler/patient/{p.id}/brachytherapy/"
        if target_date:
            sched_url += f"?target_date={target_date}"

        results.append({
            'id': p.id,
            'mr_no': p.mr_no,
            'name': p.pt_name,
            'rt_no': p.rt_no or '-',
            'age_sex': p.age_sex_display,
            'unit': unit_str,
            'diagnosis': p.diagnosis or 'Not specified',
            'consultant': p.primary_consultant.name if p.primary_consultant else 'Unassigned',
            'brachy_summary': brachy_summary,
            'has_uncompleted': any(a.completion_status is None and a.status == 'Scheduled' for a in appts),
            'schedule_url': sched_url,
        })

    return JsonResponse({'success': True, 'patients': results})





def to_title_case_doctor(name_str):
    if not name_str:
        return ''
    parts = name_str.strip().split()
    formatted = []
    for p in parts:
        p_clean = p.strip()
        if p_clean.upper() in ['DR', 'DR.', 'DOCTOR']:
            formatted.append('Dr.')
        elif p_clean.upper() in ['PROF', 'PROF.', 'PROFESSOR']:
            formatted.append('Prof.')
        elif len(p_clean) == 1:
            formatted.append(p_clean.upper())
        elif len(p_clean) == 2 and p_clean.endswith('.'):
            formatted.append(p_clean.upper())
        else:
            formatted.append(p_clean.capitalize())
    return ' '.join(formatted)


@login_required
def brachy_ot_list_print_view(request):
    """
    Renders browser-optimised HTML print view of the Brachytherapy OT List.
    Sample design:
      - Printed as two lists (top and bottom) on an A4 paper with a cutting divider line.
      - Institution header:
          Line 1: KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY, BANGALORE.
          Line 2: DEPARTMENT OF RADIATION ONCOLOGY UNIT-III (or dynamically selected Unit)
          Line 3: OT LIST AS ON DD/MM/YYYY
      - Table columns:
          Sl. No | NAME | IP NO | DIAGNOSIS | PROCEDURE | ANAESTHESIA | CONSULTANT
      - Inline editable template: user can edit any cell or header before printing.
      - Real-time bidirectional synchronization between top and bottom halves.
      - Footer:
          Left: COPY TO (1-The Prof. & HOD., Radiation Physics, 2. THE PROF & HOD RADIATION PHYSICS, 3.S/N. Brachytherapy, 4.Office copy)
          Right: Signing Authority and Designation (similar designs as EBRT list).
    """
    date_str = request.GET.get('date', '').strip()
    today = timezone.localdate()
    target_date = today
    if date_str:
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y'):
            try:
                target_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                continue

    appts = BrachytherapyAppointment.objects.filter(
        appointment_date=target_date
    ).exclude(status='Cancelled').select_related(
        'patient', 'patient__primary_consultant',
        'applicator_type', 'anesthesia_type',
        'completion_status',
        'schedule', 'schedule__machine'
    ).order_by('fraction_number', 'patient__pt_name')

    config = PrintTemplateConfig.get_active()

    # Unit resolution
    unit_param = request.GET.get('unit', '').strip().upper()
    if unit_param and unit_param != 'ALL':
        clean_u = unit_param.replace('UNIT', '').replace('-', '').strip()
        appts = [a for a in appts if str(a.patient.unit or '').replace('Unit', '').replace('-', '').strip() == clean_u]
        unit_disp = f"UNIT-{clean_u}"
    else:
        units_present = list(set(str(a.patient.unit or '').replace('Unit', '').replace('-', '').strip() for a in appts if a.patient.unit))
        if len(units_present) == 1 and units_present[0]:
            unit_disp = f"UNIT-{units_present[0]}"
        else:
            unit_disp = "UNIT-III"

    rows = []
    for idx, a in enumerate(appts, 1):
        pt_name = (a.patient.pt_name or '').strip().upper()
        ip_no = str(a.patient.mr_no or '').strip()
        diagnosis = (a.patient.diagnosis or 'CA CERVIX').strip().upper()

        # Procedure acronym & session
        app_name = ''
        if a.applicator_type:
            app_name = (a.applicator_type.applicator_type or a.applicator_type.name or '').strip().upper()

        if 'INTERSTITIAL' in app_name:
            proc = 'ISBT'
        elif 'INTRACAVITARY' in app_name:
            proc = 'ICBT'
        elif 'CYLINDER' in app_name or 'VAGINAL' in app_name or 'SORBO' in app_name:
            proc = 'IV SORBO'
        elif app_name:
            proc = app_name
        else:
            proc = 'BRACHYTHERAPY'

        mach_name = ''
        if a.schedule and a.schedule.machine:
            mach_name = a.schedule.machine.name.upper()
        elif a.schedule and a.schedule.machine_display:
            mach_name = a.schedule.machine_display.upper()

        if ('HDR' in mach_name or 'GAMMAMED' in mach_name or 'BEBIG' in mach_name or 'SAGINOVA' in mach_name) and proc in ['ICBT', 'ISBT']:
            if 'HDR' not in proc:
                proc = f"{proc} HDR"

        frac = a.fraction_number or 1
        if frac == 1:
            ord_str = "1ST SESSION"
        elif frac == 2:
            ord_str = "2ND SESSION"
        elif frac == 3:
            ord_str = "3RD SESSION"
        else:
            ord_str = f"{frac}TH SESSION"

        procedure_disp = f"{proc} {ord_str}"

        # Anesthesia
        ana_name = a.anesthesia_type.name.lower().strip() if a.anesthesia_type else ''
        if not ana_name or 'none' in ana_name or 'local' in ana_name or ana_name == 'la' or 'not required' in ana_name:
            anaesthesia_disp = "NOT REQUIRED"
        else:
            anaesthesia_disp = "REQUIRED"

        # Consultant
        cons_raw = a.patient.primary_consultant.name if a.patient.primary_consultant else 'UNASSIGNED'
        cons_clean = re.sub(r'^(DR\.?|PROF\.?)\s*', '', cons_raw.strip(), flags=re.IGNORECASE).strip().upper()
        consultant_disp = f"DR {cons_clean}"

        rows.append({
            'sl_no': idx,
            'id': a.id,
            'patient_name': pt_name,
            'ip_no': ip_no,
            'diagnosis': diagnosis,
            'procedure': procedure_disp,
            'anaesthesia': anaesthesia_disp,
            'consultant': consultant_disp,
            'unit': a.patient.unit or 'III',
        })

    target_date_formatted = target_date.strftime('%d/%m/%Y')
    header_line_1 = config.brachy_ot_header_line_1
    header_line_2 = (config.brachy_ot_header_line_2 or 'DEPARTMENT OF RADIATION ONCOLOGY {unit}').replace('{unit}', unit_disp)
    header_line_3 = (config.brachy_ot_header_line_3 or 'OT LIST AS ON {date}').replace('{date}', target_date_formatted)

    raw_signing_authority = request.GET.get('signing_authority') or config.brachy_ot_default_signing_authority
    signing_authority = to_title_case_doctor(raw_signing_authority)
    designation = request.GET.get('designation') or config.brachy_ot_default_designation

    raw_consultant_options = get_consultant_signing_options(raw_signing_authority)
    consultant_options = [to_title_case_doctor(opt) for opt in raw_consultant_options]
    designations = list(Designation.objects.filter(is_active=True).order_by('display_order', 'name'))

    context = {
        'target_date': target_date,
        'target_date_iso': target_date.isoformat(),
        'target_date_formatted': target_date_formatted,
        'header_line_1': header_line_1,
        'header_line_2': header_line_2,
        'header_line_3': header_line_3,
        'copy_to_text': config.brachy_ot_copy_to,
        'signing_authority': signing_authority,
        'designation': designation,
        'department_line': config.brachy_ot_department_line,
        'consultant_options': consultant_options,
        'designations': designations,
        'selected_unit': unit_disp,
        'rows': rows,
        'count': len(rows),
        'config': config,
    }
    return render(request, 'scheduler/brachy_ot_list_print.html', context)


# ==============================================================================
# TELETHERAPY BULK DATA IMPORT & EXPORT (ADMIN ONLY)
# ==============================================================================

def normalize_import_header(h):
    if not h:
        return ''
    return re.sub(r'[\s_]+', '_', str(h)).strip('_').lower()


def process_teletherapy_import(file_obj, filename, current_user):
    """
    Parses and imports Teletherapy patient and treatment schedule data from Excel (.xlsx/.xls) or CSV.
    Enforces de-duplication:
      - Patients are de-duplicated by MR_No. Existing patients have missing/empty demographics updated.
      - Schedules are de-duplicated by (patient, treatment_type='Teletherapy', machine, shift, date_of_start, site_of_treatment).
      - Multi-course support: one MR_No can have multiple Teletherapy courses; if a row has distinct machine/shift/date_of_start
        for the same MR_No, it creates an additional course linked under the same patient record.
    Returns:
      stats: dict(total_rows, patients_created, patients_updated, schedules_created, schedules_updated, schedules_skipped)
      errors: list of dicts with row number, mr_no, error message
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ''

    raw_headers = []
    data_rows = []

    if ext in ('.xlsx', '.xls'):
        wb = openpyxl.load_workbook(file_obj, data_only=True)
        sheet = wb.active
        max_col = sheet.max_column
        raw_headers = [sheet.cell(1, c).value for c in range(1, max_col + 1)]
        for r in range(2, sheet.max_row + 1):
            row_vals = [sheet.cell(r, c).value for c in range(1, max_col + 1)]
            if any(v is not None and str(v).strip() != '' for v in row_vals):
                data_rows.append((r, row_vals))
    else:
        if hasattr(file_obj, 'read'):
            content = file_obj.read()
        else:
            content = file_obj
        if isinstance(content, bytes):
            try:
                text_content = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                text_content = content.decode('latin-1')
        else:
            text_content = str(content)

        reader = csv.reader(io.StringIO(text_content))
        all_csv_rows = list(reader)
        if all_csv_rows:
            raw_headers = all_csv_rows[0]
            for r_idx, row_vals in enumerate(all_csv_rows[1:], start=2):
                if any(str(v).strip() != '' for v in row_vals):
                    data_rows.append((r_idx, row_vals))

    if not raw_headers:
        return {'total_rows': 0, 'patients_created': 0, 'patients_updated': 0,
                'schedules_created': 0, 'schedules_updated': 0, 'schedules_skipped': 0}, [{'row': 1, 'mr_no': '-', 'error': 'The uploaded file is empty or missing headers.'}]

    norm_headers = [normalize_import_header(h) for h in raw_headers]

    header_aliases = {
        'mr_no': ['mr_no', 'mrno', 'mr_num', 'hospital_mr_no', 'hospital_mr_number', 'mr'],
        'pt_name': ['pt_name', 'patient_name', 'patientname', 'name'],
        'age': ['age'],
        'sex': ['sex', 'gender'],
        'date_of_sim': ['date_of_sim', 'dateofsim', 'sim_date', 'simulation_date'],
        'diagnosis': ['diagnosis', 'diag'],
        'site_of_treatment': ['site_of_treatment', 'site_of__treatment', 'site', 'treatment_site'],
        'laterality': ['laterality'],
        'phone_no': ['phone_no', 'phoneno', 'phone', 'mobile', 'contact_no'],
        'intent': ['intent', 'treatment_intent'],
        'unit': ['unit', 'clinical_unit'],
        'primary_consultant': ['primary_consultant', 'consultant', 'doctor', 'primary_doctor'],
        'machine': ['machine', 'machine_name'],
        'date_of_start': ['date_of_start', 'dateofstart', 'start_date', 'treatment_start'],
        'shift': ['shift'],
        'dose_planned': ['dose_planned', 'dose', 'total_dose'],
        'no_of_fractions': ['no_of_fractions', 'fractions', 'fractions_planned', 'fractions_count'],
        'technique': ['technique', 'treatment_technique'],
        'jr_resident': ['jr_resident', 'resident', 'junior_resident'],
        'simulated_for': ['simulated_for', 'sim_for', 'simulatedfor'],
        'rt_no': ['rt_no', 'rtno', 'rt_number'],
    }

    col_index_map = {}
    for standard_col, aliases in header_aliases.items():
        for idx, nh in enumerate(norm_headers):
            if nh in aliases:
                col_index_map[standard_col] = idx
                break

    if 'mr_no' not in col_index_map:
        return {'total_rows': 0, 'patients_created': 0, 'patients_updated': 0,
                'schedules_created': 0, 'schedules_updated': 0, 'schedules_skipped': 0}, [{'row': 1, 'mr_no': '-', 'error': "Required column 'MR_No' not found in file headers. Please ensure the template column headers are present."}]

    # Pre-fetch lookup caches for optimal bulk processing speed
    machines = {m.name.lower(): m for m in Machine.objects.all()}
    shifts = {s.name.lower(): s for s in Shift.objects.all()}
    techniques = {t.name.lower(): t for t in Technique.objects.all()}
    special_techs = {st.name.lower(): st for st in SpecialTechnique.objects.all()}
    sim_fors = {sf.name.lower(): sf for sf in SimulatedFor.objects.all()}
    intents = {i.name.lower(): i for i in TreatmentIntent.objects.all()}
    consultants = {c.name.lower(): c for c in Consultant.objects.all()}

    def parse_date_cell(val):
        if not val:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        s = str(val).strip()
        if not s:
            return None
        for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%Y/%m/%d', '%d-%b-%Y', '%d-%b-%y', '%d %b %Y', '%d %B %Y'):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
        return None

    def clean_phone_str(val):
        if val is None:
            return ''
        s = str(val).strip()
        if s.endswith('.0'):
            s = s[:-2]
        return s

    def clean_decimal_val(val):
        if val is None or val == '':
            return None
        try:
            return Decimal(str(val).strip())
        except Exception:
            return None

    def clean_int_val(val):
        if val is None or val == '':
            return None
        try:
            return int(float(str(val).strip()))
        except Exception:
            return None

    def norm_unit_str(val):
        if not val:
            return 'I'
        u = str(val).strip().upper()
        if u in ('1', 'I'):
            return 'I'
        elif u in ('2', 'II'):
            return 'II'
        elif u in ('3', 'III'):
            return 'III'
        return 'I'

    def get_or_create_doc(c_name):
        if not c_name:
            return None
        c_clean = c_name.strip()
        c_lower = c_clean.lower()
        if c_lower in consultants:
            return consultants[c_lower]
        c_no_dr = re.sub(r'^dr\.?\s*', '', c_lower)
        for k, v in consultants.items():
            if c_no_dr and (c_no_dr in k or k in c_lower):
                return v
        c = Consultant.objects.create(name=c_clean, display_order=99)
        consultants[c_lower] = c
        return c

    def get_or_create_intent_obj(i_name):
        if not i_name:
            return None
        i_clean = i_name.strip()
        i_lower = i_clean.lower()
        if i_lower in intents:
            return intents[i_lower]
        i = TreatmentIntent.objects.create(name=i_clean.capitalize(), display_order=99)
        intents[i_lower] = i
        return i

    def get_or_create_sim_for_obj(sf_name):
        if not sf_name:
            return None
        sf_clean = sf_name.strip()
        sf_lower = sf_clean.lower()
        if sf_lower in sim_fors:
            return sim_fors[sf_lower]
        sf = SimulatedFor.objects.create(name=sf_clean.capitalize(), display_order=99)
        sim_fors[sf_lower] = sf
        return sf

    def get_shift_obj(sh_name):
        if not sh_name:
            return None
        sh_clean = str(sh_name).strip().upper()
        if sh_clean in ('IL', '2', 'II'):
            sh_clean = 'II'
        elif sh_clean in ('1', 'I'):
            sh_clean = 'I'
        elif sh_clean in ('3', 'III'):
            sh_clean = 'III'
        return shifts.get(sh_clean.lower())

    def get_mach_obj(m_name):
        if not m_name:
            return None
        m_lower = str(m_name).strip().lower()
        if m_lower in machines:
            return machines[m_lower]
        for k, v in machines.items():
            if m_lower in k or k in m_lower:
                return v
        return None

    def resolve_tech(t_name):
        if not t_name:
            return None, None
        t_clean = str(t_name).strip()
        t_lower = t_clean.lower()
        if t_lower in special_techs:
            return None, special_techs[t_lower]
        if t_lower in techniques:
            return techniques[t_lower], None
        for k, v in special_techs.items():
            if k in t_lower or t_lower in k:
                return None, v
        for k, v in techniques.items():
            if k in t_lower or t_lower in k:
                return v, None
        t_obj = Technique.objects.create(name=t_clean, display_order=99)
        techniques[t_lower] = t_obj
        return t_obj, None

    def get_cell(row_vals, col_key):
        idx = col_index_map.get(col_key)
        if idx is not None and idx < len(row_vals):
            return row_vals[idx]
        return None

    stats = {
        'total_rows': len(data_rows),
        'patients_created': 0,
        'patients_updated': 0,
        'schedules_created': 0,
        'schedules_updated': 0,
        'schedules_skipped': 0,
    }
    errors = []

    today = timezone.localdate()

    for r_num, row_vals in data_rows:
        mr_val = get_cell(row_vals, 'mr_no')
        mr_no = clean_int_val(mr_val)
        if not mr_no:
            errors.append({'row': r_num, 'mr_no': str(mr_val or '-'), 'error': 'Missing or invalid MR_No.'})
            continue

        try:
            pt_name_raw = get_cell(row_vals, 'pt_name')
            pt_name = str(pt_name_raw).strip() if pt_name_raw is not None else ''
            if not pt_name:
                pt_name = f"Patient {mr_no}"

            age = clean_int_val(get_cell(row_vals, 'age'))
            sex_raw = str(get_cell(row_vals, 'sex') or '').strip().upper()
            sex = 'M' if sex_raw.startswith('M') else ('F' if sex_raw.startswith('F') else 'O')
            diag = str(get_cell(row_vals, 'diagnosis') or '').strip() or 'Carcinoma'
            site = str(get_cell(row_vals, 'site_of_treatment') or '').strip() or 'Others'
            laterality = str(get_cell(row_vals, 'laterality') or '').strip()
            phone = clean_phone_str(get_cell(row_vals, 'phone_no'))
            unit_val = norm_unit_str(get_cell(row_vals, 'unit'))
            rt_no = str(get_cell(row_vals, 'rt_no') or '').strip()
            consultant_raw = str(get_cell(row_vals, 'primary_consultant') or '').strip()
            consultant_obj = get_or_create_doc(consultant_raw)

            sim_date = parse_date_cell(get_cell(row_vals, 'date_of_sim'))
            start_date = parse_date_cell(get_cell(row_vals, 'date_of_start'))
            mach_obj = get_mach_obj(get_cell(row_vals, 'machine'))
            shift_obj = get_shift_obj(get_cell(row_vals, 'shift'))
            dose = clean_decimal_val(get_cell(row_vals, 'dose_planned'))
            fractions = clean_int_val(get_cell(row_vals, 'no_of_fractions'))
            tech_obj, spec_tech_obj = resolve_tech(get_cell(row_vals, 'technique'))
            jr_res = str(get_cell(row_vals, 'jr_resident') or '').strip()
            sim_for_obj = get_or_create_sim_for_obj(str(get_cell(row_vals, 'simulated_for') or '').strip())
            intent_obj = get_or_create_intent_obj(str(get_cell(row_vals, 'intent') or '').strip())

            # 1. Patient Demographics & De-duplication
            patient, pt_created = Patient.objects.get_or_create(
                mr_no=mr_no,
                defaults={
                    'pt_name': pt_name,
                    'age': age,
                    'sex': sex,
                    'diagnosis': diag,
                    'site_of_treatment': site,
                    'phone_no': phone,
                    'unit': unit_val,
                    'rt_no': rt_no,
                    'primary_consultant': consultant_obj,
                    'created_by': current_user if current_user and current_user.is_authenticated else None,
                }
            )

            if pt_created:
                stats['patients_created'] += 1
            else:
                p_changed = False
                if pt_name and (not patient.pt_name or patient.pt_name.lower().startswith('patient ')):
                    patient.pt_name = pt_name; p_changed = True
                if age is not None and not patient.age:
                    patient.age = age; p_changed = True
                if phone and not patient.phone_no:
                    patient.phone_no = phone; p_changed = True
                if rt_no and not patient.rt_no:
                    patient.rt_no = rt_no; p_changed = True
                if unit_val and patient.unit != unit_val:
                    patient.unit = unit_val; p_changed = True
                if diag and (not patient.diagnosis or patient.diagnosis == 'Carcinoma'):
                    patient.diagnosis = diag; p_changed = True
                if site and (not patient.site_of_treatment or patient.site_of_treatment == 'Others'):
                    patient.site_of_treatment = site; p_changed = True
                if consultant_obj and not patient.primary_consultant:
                    patient.primary_consultant = consultant_obj; p_changed = True
                if p_changed:
                    patient.save()
                    stats['patients_updated'] += 1

            # 2. Teletherapy Course De-duplication & Multi-Course per MR No Support
            notes_parts = []
            if laterality:
                notes_parts.append(f"Laterality: {laterality}")
            notes_val = "\n".join(notes_parts) if notes_parts else None

            existing_courses = list(patient.schedules.filter(treatment_type='Teletherapy'))
            matching_course = None

            for ec in existing_courses:
                if start_date and ec.date_of_start:
                    if ec.date_of_start == start_date:
                        mach_match = (not mach_obj or not ec.machine or ec.machine_id == mach_obj.id)
                        shift_match = (not shift_obj or not ec.shift or ec.shift_id == shift_obj.id)
                        site_match = (not site or not ec.site_of_treatment or ec.site_of_treatment.lower() == site.lower())
                        if mach_match and shift_match and site_match:
                            matching_course = ec
                            break
                elif not start_date and not ec.date_of_start:
                    if ec.date_of_sim == sim_date:
                        site_match = (not site or not ec.site_of_treatment or ec.site_of_treatment.lower() == site.lower())
                        if site_match:
                            matching_course = ec
                            break

            if matching_course:
                # De-duplication: Course already exists! Update missing fields if any
                c_changed = False
                if mach_obj and not matching_course.machine:
                    matching_course.machine = mach_obj; c_changed = True
                if shift_obj and not matching_course.shift:
                    matching_course.shift = shift_obj; c_changed = True
                if dose is not None and matching_course.dose_planned is None:
                    matching_course.dose_planned = dose; c_changed = True
                if fractions is not None and matching_course.no_of_fractions is None:
                    matching_course.no_of_fractions = fractions; c_changed = True
                if tech_obj and not matching_course.technique:
                    matching_course.technique = tech_obj; c_changed = True
                if spec_tech_obj and not matching_course.special_technique:
                    matching_course.special_technique = spec_tech_obj; c_changed = True
                if intent_obj and not matching_course.intent:
                    matching_course.intent = intent_obj; c_changed = True
                if sim_for_obj and not matching_course.simulated_for:
                    matching_course.simulated_for = sim_for_obj; c_changed = True
                if jr_res and not matching_course.jr_resident:
                    matching_course.jr_resident = jr_res; c_changed = True
                if notes_val and not matching_course.notes:
                    matching_course.notes = notes_val; c_changed = True
                if c_changed:
                    matching_course.save()
                    stats['schedules_updated'] += 1
                else:
                    stats['schedules_skipped'] += 1
            else:
                # New Teletherapy Course for this patient
                sched_status = 'Scheduled'
                if start_date:
                    temp_s = TreatmentSchedule(
                        patient=patient, treatment_type='Teletherapy',
                        date_of_start=start_date, no_of_fractions=fractions
                    )
                    calc_end = temp_s.calculate_end_date() if fractions else None
                    if (calc_end and calc_end < today) or (start_date < today):
                        sched_status = 'Completed'
                    else:
                        sched_status = 'Scheduled'
                else:
                    sched_status = 'Draft'

                TreatmentSchedule.objects.create(
                    patient=patient,
                    treatment_type='Teletherapy',
                    date_of_sim=sim_date,
                    date_of_start=start_date,
                    dose_planned=dose,
                    no_of_fractions=fractions,
                    technique=tech_obj,
                    special_technique=spec_tech_obj,
                    simulated_for=sim_for_obj,
                    intent=intent_obj,
                    machine=mach_obj,
                    shift=shift_obj,
                    site_of_treatment=site,
                    jr_resident=jr_res,
                    notes=notes_val,
                    status=sched_status,
                    created_by=current_user if current_user and current_user.is_authenticated else None,
                )
                stats['schedules_created'] += 1

        except Exception as e:
            errors.append({'row': r_num, 'mr_no': str(mr_no), 'error': str(e)})

    return stats, errors


@login_required
def teletherapy_import_view(request):
    """
    Teletherapy Data Bulk Import View (Admin Only).
    Allows Administrators to upload Excel (.xlsx, .xls) or CSV (.csv) files matching the 21-column template.
    Avoids duplicate patients and courses; links multiple courses under the same MR_No.
    """
    is_ajax = (request.headers.get('x-requested-with') == 'XMLHttpRequest' or 
               request.content_type == 'application/json' or
               request.POST.get('is_ajax') == '1')

    if not is_admin_only(request.user):
        err_msg = "Access restricted. Only Administrators can import data into the database."
        if is_ajax:
            return JsonResponse({'success': False, 'error': err_msg}, status=403)
        messages.error(request, err_msg)
        return redirect('dashboard')

    stats = None
    errors = None

    if request.method == 'POST':
        uploaded_file = request.FILES.get('import_file')
        if not uploaded_file:
            err_msg = "Please select a valid Excel (.xlsx, .xls) or CSV (.csv) file to import."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('teletherapy_import')

        filename = uploaded_file.name
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ('.xlsx', '.xls', '.csv'):
            err_msg = f"Unsupported file format '{ext}'. Please upload an Excel (.xlsx, .xls) or CSV (.csv) file."
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=400)
            messages.error(request, err_msg)
            return redirect('teletherapy_import')

        try:
            stats, errors = process_teletherapy_import(uploaded_file, filename, request.user)

            AuditLog.objects.create(
                user=request.user,
                action="TELETHERAPY_DATA_IMPORT",
                mr_no="BULK_IMPORT",
                details=f"Imported file '{filename}': {stats['patients_created']} pts created, {stats['patients_updated']} pts updated, {stats['schedules_created']} courses created, {stats['schedules_updated']} courses updated, {len(errors)} errors.",
                ip_address=request.META.get('REMOTE_ADDR')
            )

            msg = (f"Import completed successfully! Processed {stats['total_rows']} rows: "
                   f"{stats['patients_created']} new patients created, {stats['patients_updated']} patients updated, "
                   f"{stats['schedules_created']} new courses created, {stats['schedules_updated']} courses updated, "
                   f"{stats['schedules_skipped']} courses unchanged.")
            if errors:
                msg += f" Encountered {len(errors)} row warning(s)."
            messages.success(request, msg)

            if is_ajax:
                return JsonResponse({
                    'success': True,
                    'message': msg,
                    'stats': stats,
                    'errors': errors,
                })

        except Exception as e:
            err_msg = f"Import processing failed: {str(e)}"
            if is_ajax:
                return JsonResponse({'success': False, 'error': err_msg}, status=500)
            messages.error(request, err_msg)

    # Context for GET or rendered POST
    recent_imports = AuditLog.objects.filter(action="TELETHERAPY_DATA_IMPORT").order_by('-log_datetime')[:5]

    template_columns = [
        {'name': 'MR_No', 'required': True, 'desc': 'Unique Patient Hospital MR Number (Integer)'},
        {'name': 'Pt_name', 'required': True, 'desc': 'Patient Full Name'},
        {'name': 'Age', 'required': False, 'desc': 'Patient Age in years'},
        {'name': 'Sex', 'required': True, 'desc': "M (Male), F (Female), or O (Other)"},
        {'name': 'Date_Of_sim', 'required': False, 'desc': 'Date of Simulation (YYYY-MM-DD)'},
        {'name': 'Diagnosis', 'required': False, 'desc': 'Primary Oncological Diagnosis'},
        {'name': 'Site_of _treatment', 'required': False, 'desc': 'Anatomical Treatment Site (Brain, Breast, Pelvis, etc.)'},
        {'name': 'Laterality', 'required': False, 'desc': 'Central, Left, Right, Bilateral'},
        {'name': 'Phone_no', 'required': False, 'desc': 'Patient Contact Phone Number'},
        {'name': 'Intent', 'required': False, 'desc': 'Radical, Palliative, Adjuvant, Neo-adjuvant'},
        {'name': 'Unit', 'required': False, 'desc': 'Clinical Unit: I, II, or III'},
        {'name': 'Primary Consultant', 'required': False, 'desc': 'Treating Consultant Doctor (e.g. Dr. Khaleel)'},
        {'name': 'Machine', 'required': False, 'desc': 'Target Machine (e.g. ELEKTA - LA2, Varian - DBX)'},
        {'name': 'Date_of_start', 'required': False, 'desc': 'Scheduled Treatment Start Date (YYYY-MM-DD)'},
        {'name': 'Shift', 'required': False, 'desc': 'Treatment Shift: I, II, or III'},
        {'name': 'Dose_planned', 'required': False, 'desc': 'Total planned radiation dose in Gy'},
        {'name': 'No_of_fractions', 'required': False, 'desc': 'Total planned number of fractions'},
        {'name': 'Technique', 'required': False, 'desc': 'IMRT, 3D CRT, VMAT, SRS, SBRT, 2D'},
        {'name': 'Jr_Resident', 'required': False, 'desc': 'Assigned Junior Resident Doctor'},
        {'name': 'Simulated_for', 'required': False, 'desc': 'Simulated machine group: Elekta, Varian, Cobalt'},
        {'name': 'RT_No', 'required': False, 'desc': 'Radiotherapy Department Registration Number'},
    ]

    context = {
        'template_columns': template_columns,
        'recent_imports': recent_imports,
        'stats': stats,
        'errors': errors,
    }
    return render(request, 'scheduler/teletherapy_import.html', context)


@login_required
def teletherapy_export_view(request):
    """
    Exports Teletherapy patient and treatment schedule data as a CSV file matching
    the exact 21-column template:
    MR_No,Pt_name,Age,Sex,Date_Of_sim,Diagnosis,Site_of _treatment,Laterality,Phone_no,
    Intent,Unit,Primary Consultant,Machine,Date_of_start,Shift,Dose_planned,
    No_of_fractions,Technique,Jr_Resident,Simulated_for,RT_No
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators can export data.")
        return redirect('dashboard')

    schedules_qs = TreatmentSchedule.objects.filter(treatment_type='Teletherapy').select_related(
        'patient', 'patient__primary_consultant', 'machine', 'shift', 'technique',
        'special_technique', 'simulated_for', 'intent'
    ).order_by('date_of_start', 'patient__mr_no')

    # Optional machine filter
    machine_id = request.GET.get('machine_id')
    if machine_id:
        schedules_qs = schedules_qs.filter(machine_id=machine_id)

    # Optional date filters
    date_from_str = request.GET.get('date_from')
    date_to_str = request.GET.get('date_to')
    if date_from_str:
        try:
            d_from = datetime.strptime(date_from_str, '%Y-%m-%d').date()
            schedules_qs = schedules_qs.filter(date_of_start__gte=d_from)
        except ValueError:
            pass
    if date_to_str:
        try:
            d_to = datetime.strptime(date_to_str, '%Y-%m-%d').date()
            schedules_qs = schedules_qs.filter(date_of_start__lte=d_to)
        except ValueError:
            pass

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    filename = f"teletherapy_export_{timezone.localdate().strftime('%Y-%m-%d')}.csv"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    headers = [
        'MR_No', 'Pt_name', 'Age', 'Sex', 'Date_Of_sim', 'Diagnosis',
        'Site_of _treatment', 'Laterality', 'Phone_no', 'Intent', 'Unit',
        'Primary Consultant', 'Machine', 'Date_of_start', 'Shift',
        'Dose_planned', 'No_of_fractions', 'Technique', 'Jr_Resident',
        'Simulated_for', 'RT_No'
    ]
    writer.writerow(headers)

    for s in schedules_qs:
        p = s.patient
        tech_name = ''
        if s.special_technique and s.special_technique.name.lower() != 'none':
            tech_name = s.special_technique.name
        elif s.technique:
            tech_name = s.technique.name
        
        laterality = ''
        if s.notes:
            m = re.search(r'Laterality:\s*([^\n\r]+)', s.notes, re.IGNORECASE)
            if m:
                laterality = m.group(1).strip()

        row = [
            p.mr_no,
            p.pt_name,
            p.age if p.age is not None else '',
            p.sex,
            s.date_of_sim.strftime('%Y-%m-%d') if s.date_of_sim else '',
            p.diagnosis or '',
            s.site_of_treatment or p.site_of_treatment or '',
            laterality,
            p.phone_no or '',
            s.intent.name if s.intent else '',
            p.unit or '',
            p.primary_consultant.name if p.primary_consultant else '',
            s.machine.name if s.machine else '',
            s.date_of_start.strftime('%Y-%m-%d') if s.date_of_start else '',
            s.shift.name if s.shift else '',
            s.dose_planned if s.dose_planned is not None else '',
            s.no_of_fractions if s.no_of_fractions is not None else '',
            tech_name,
            s.jr_resident or '',
            s.simulated_for.name if s.simulated_for else '',
            p.rt_no or '',
        ]
        writer.writerow(row)

    return response


@login_required
def teletherapy_import_template_view(request):
    """
    Downloads a blank sample CSV template with the exact 21 columns and sample rows.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators can download templates.")
        return redirect('dashboard')

    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="Teletherapy_Import_Template.csv"'

    writer = csv.writer(response)
    headers = [
        'MR_No', 'Pt_name', 'Age', 'Sex', 'Date_Of_sim', 'Diagnosis',
        'Site_of _treatment', 'Laterality', 'Phone_no', 'Intent', 'Unit',
        'Primary Consultant', 'Machine', 'Date_of_start', 'Shift',
        'Dose_planned', 'No_of_fractions', 'Technique', 'Jr_Resident',
        'Simulated_for', 'RT_No'
    ]
    writer.writerow(headers)
    sample_row_1 = [
        '20240024292', 'REKHA', '36', 'F', '2025-02-14', 'ASTROCYTOMA',
        'Brain', 'Central', '9535600246', 'Adjuvant', 'I',
        'Dr. Khaleel', 'ELEKTA - LA2', '2025-03-11', 'I',
        '60', '30', 'IMRT', 'Dr. Resident',
        'Elekta', '4428'
    ]
    sample_row_2 = [
        '20230022427', 'gangamma', '53', 'F', '2025-02-25', 'recurrent ca r breast',
        'Breast', 'Right', '9743762778', 'Adjuvant', 'II',
        'Dr. Thejaswini', 'ELEKTA - LA4', '2025-03-03', 'I',
        '40', '15', 'IMRT', 'dr jameema',
        'Elekta', '4491'
    ]
    writer.writerow(sample_row_1)
    writer.writerow(sample_row_2)
    return response
