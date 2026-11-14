from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
from datetime import date, datetime, timedelta
from decimal import Decimal

from .models import (
    Machine, Shift, Technique, SpecialTechnique, SimulatedFor,
    TreatmentSite, MachineMapping, TreatmentSchedule
)
from apps.patients.models import Patient, DEFAULT_SITES
from apps.core.models import AuditLog
from apps.core.permissions import is_admin_or_subadmin, can_manage_mappings, can_remove_from_schedule
from apps.core.validators import validate_field_value


@login_required
def schedule_patient_view(request, patient_id):
    """
    Core Radiotherapy Scheduling View.
    Top card shows Patient Registration details:
      - Readonly: MR No, Patient Name, RT No, Sex, Primary Consultant
      - Editable inline: Age, Diagnosis, Site of Treatment, Phone Number
    Scheduling Form captures:
      - Simulation date, Dose, Fractions, Technique, Special Technique,
        Junior Resident, Simulated for, Machine, Shift, Treatment start date, Notes.
    """
    patient = get_object_or_404(Patient, id=patient_id)
    machines = Machine.objects.filter(is_active=True).order_by('display_order', 'name')
    shifts = Shift.objects.filter(is_active=True).order_by('display_order', 'name')
    techniques = Technique.objects.filter(is_active=True).order_by('display_order', 'name')
    special_techniques = SpecialTechnique.objects.filter(is_active=True).order_by('display_order', 'name')
    simulated_for_list = SimulatedFor.objects.filter(is_active=True).order_by('display_order', 'name')
    treatment_sites = TreatmentSite.objects.filter(is_active=True).order_by('display_order', 'name')

    existing_schedule = patient.schedules.first()

    if request.method == 'POST':
        # 1. Update top-card editable patient registration fields
        age_raw = request.POST.get('age', '').strip()
        patient.age = int(float(age_raw)) if age_raw else None
        patient.diagnosis = request.POST.get('diagnosis', patient.diagnosis).strip()
        site_val = request.POST.get('site_of_treatment', patient.site_of_treatment or '').strip()
        patient.site_of_treatment = site_val

        phone_no = request.POST.get('phone_no', '').strip()
        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return redirect('schedule_patient', patient_id=patient.id)
        patient.phone_no = phone_no
        patient.save()

        # 2. Extract scheduling fields
        sim_date_raw = request.POST.get('date_of_sim', '').strip()
        dose_raw = request.POST.get('dose_planned', '').strip()
        fractions_raw = request.POST.get('no_of_fractions', '').strip()
        tech_id = request.POST.get('technique')
        spec_tech_id = request.POST.get('special_technique')
        jr_resident = request.POST.get('jr_resident', '').strip()
        sim_for_id = request.POST.get('simulated_for')
        machine_id = request.POST.get('machine')
        shift_id = request.POST.get('shift')
        start_date_raw = request.POST.get('date_of_start', '').strip()
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

        if sim_date and start_date and start_date < sim_date:
            messages.error(
                request,
                f"Treatment Start Date ({start_date.strftime('%d-%m-%Y')}) cannot be earlier than CT Simulation Date ({sim_date.strftime('%d-%m-%Y')}). Date of start must be on or after Date of simulation."
            )
            return redirect('schedule_patient', patient_id=patient.id)

        dose = Decimal(dose_raw) if dose_raw else None
        fractions = int(float(fractions_raw)) if fractions_raw else None

        tech_obj = Technique.objects.filter(id=tech_id, is_active=True).first() if tech_id else None
        spec_tech_obj = SpecialTechnique.objects.filter(id=spec_tech_id, is_active=True).first() if spec_tech_id else None
        sim_for_obj = SimulatedFor.objects.filter(id=sim_for_id, is_active=True).first() if sim_for_id else None
        mach_obj = Machine.objects.filter(id=machine_id, is_active=True).first() if machine_id else None
        shift_obj = Shift.objects.filter(id=shift_id, is_active=True).first() if shift_id else None

        schedule_action = "created"
        if existing_schedule:
            sched = existing_schedule
            schedule_action = "updated"
        else:
            sched = TreatmentSchedule(patient=patient, created_by=request.user)

        sched.site_of_treatment = site_val
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
        sched.notes = notes
        sched.status = 'Scheduled' if start_date else 'Pending'
        sched.save()

        AuditLog.objects.create(
            user=request.user,
            action=f"SCHEDULE_{schedule_action.upper()}",
            mr_no=str(patient.mr_no),
            details=f"{schedule_action.title()} treatment schedule for {patient.pt_name} on {mach_obj} ({start_date or 'No Start Date'})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(
            request,
            f"Treatment schedule {schedule_action} successfully for {patient.pt_name} (MR: {patient.mr_no})!"
        )
        return redirect('patient_detail', patient_id=patient.id)

    default_technique = techniques.filter(name__iexact='IMRT').first() or techniques.first()
    default_special_technique = special_techniques.filter(name__iexact='None').first() or special_techniques.first()

    context = {
        'patient': patient,
        'schedule': existing_schedule,
        'machines': machines,
        'shifts': shifts,
        'techniques': techniques,
        'special_techniques': special_techniques,
        'default_technique': default_technique,
        'default_special_technique': default_special_technique,
        'simulated_for_list': simulated_for_list,
        'treatment_sites': treatment_sites,
        'today': date.today().isoformat(),
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

    # Search up to 60 days ahead
    for _ in range(60):
        # Skip Sundays (weekday 6)
        if curr_date.weekday() != 6:
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

    machines = Machine.objects.filter(is_active=True).order_by('display_order', 'name')
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

        # Find earliest available free slot (skipping Sundays)
        # Capacity rule: Exactly 1 patient per shift accommodated per day
        curr_date = start_search
        found_slot = None

        for _ in range(60):
            if curr_date.weekday() != 6:  # Skip Sunday
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
    """
    if not can_manage_mappings(request.user):
        messages.error(request, "Access restricted to Administrators / SubAdmins.")
        return redirect('dashboard')

    mappings = MachineMapping.objects.select_related('machine')\
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

