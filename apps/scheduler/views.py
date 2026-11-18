from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
from datetime import date, datetime, timedelta
from decimal import Decimal

from .models import (
    Machine, Shift, Technique, SpecialTechnique, SimulatedFor,
    TreatmentSite, TreatmentIntent, MachineMapping, TreatmentSchedule, Holiday, EmergencyReason,
    BrachyApplicator, BrachyAnesthesia, BrachytherapyAppointment, TreatmentCompletionStatus
)
import json
import calendar
from apps.patients.models import Patient, DEFAULT_SITES
from apps.core.models import AuditLog
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

    # Handle specific course selection or adding a new course
    req_schedule_id = request.GET.get('schedule_id') or request.POST.get('schedule_id')
    is_new_course = (request.GET.get('new_course') == '1') or (request.POST.get('new_course') == '1')

    if req_schedule_id:
        existing_schedule = patient.schedules.filter(id=req_schedule_id).first()
    elif is_new_course:
        existing_schedule = None
    else:
        existing_schedule = patient.schedules.filter(status__in=['Draft', 'Pending', 'Scheduled']).first() or patient.schedules.first()

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
        if prev_course_id and prev_course_needs_completion:
            prev_s = patient.schedules.filter(id=prev_course_id).first()
            if prev_s and not prev_s.actual_treatment_status:
                prev_comp = request.POST.get('prev_is_completed', '').strip().lower()
                prev_st_date = parse_d(request.POST.get('prev_actual_start_date', '').strip())
                prev_end_date = parse_d(request.POST.get('prev_actual_end_date', '').strip())
                prev_st_id = request.POST.get('prev_completion_status_id', '').strip()
                prev_notes = request.POST.get('prev_status_notes', '').strip()

                if prev_comp == 'yes':
                    comp_obj = TreatmentCompletionStatus.objects.filter(is_completed=True).first()
                    if not comp_obj:
                        comp_obj, _ = TreatmentCompletionStatus.objects.get_or_create(
                            name='Completed', defaults={'is_completed': True, 'display_order': 1}
                        )
                    prev_s.actual_treatment_status = comp_obj
                    prev_s.status = 'Completed'
                    prev_s.actual_start_date = prev_st_date or prev_s.date_of_start
                    prev_s.actual_end_date = prev_end_date or prev_s.treatment_end_date
                elif prev_comp == 'no':
                    if prev_st_id:
                        prev_s.actual_treatment_status = TreatmentCompletionStatus.objects.filter(id=prev_st_id).first()
                    else:
                        prev_s.actual_treatment_status = TreatmentCompletionStatus.objects.filter(is_completed=False).first()
                    if prev_st_date:
                        prev_s.actual_start_date = prev_st_date
                    if prev_end_date:
                        prev_s.actual_end_date = prev_end_date
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

        if is_completed_val == 'yes':
            comp_status = TreatmentCompletionStatus.objects.filter(is_completed=True).first()
            if not comp_status:
                comp_status, _ = TreatmentCompletionStatus.objects.get_or_create(
                    name='Completed', defaults={'is_completed': True, 'display_order': 1}
                )
            schedule.actual_treatment_status = comp_status
            schedule.status = 'Completed'
            schedule.actual_start_date = act_start or schedule.actual_start_date or schedule.date_of_start
            schedule.actual_end_date = act_end or schedule.actual_end_date or schedule.treatment_end_date
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

    if not can_schedule_patient(request.user):
        messages.error(request, "Access restricted. You do not have permission to modify treatment schedules.")
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
    existing_appts = BrachytherapyAppointment.objects.filter(
        patient=patient,
        status='Scheduled'
    ).select_related('applicator_type', 'anesthesia_type').order_by('appointment_date')

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

    today = timezone.localdate()

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

        # Create BrachytherapyAppointment records
        created_appts = []
        curr_frac = existing_appts.count() + 1
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
    try:
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
        'existing_appts_count': existing_appts.count(),
        'calendar': calendar_data,
        'user_is_consultant': user_is_consultant,
        'user_is_resident': user_is_res,
        'active_consultants': active_consultants,
        'current_user_name': request.user.get_full_name() or request.user.username,
        'today': today,
    }
    return render(request, 'scheduler/brachy_schedule_patient.html', context)


