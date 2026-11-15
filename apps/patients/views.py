from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q

from .models import Patient, SEX_CHOICES, DEFAULT_SITES
from apps.core.models import Consultant, AuditLog
from apps.core.permissions import can_edit_locked_identity, can_delete_patient, can_register_patient
from apps.core.validators import validate_field_value


@login_required
def patient_register_view(request):
    """
    Patient Registration View (Stage 1).
    Strictly captures: Patient Name, MR No, RT No, Age, Sex, Diagnosis,
    Primary consultant, Phone Number.
    Site of treatment is captured during Treatment Scheduling (Stage 2).
    """
    consultants = Consultant.objects.filter(is_active=True).order_by('display_order', 'name')

    if request.method == 'POST':
        mr_no_raw = request.POST.get('mr_no', '').strip()
        pt_name = request.POST.get('pt_name', '').strip()
        rt_no = request.POST.get('rt_no', '').strip()
        age_raw = request.POST.get('age', '').strip()
        sex = request.POST.get('sex', '').strip()
        diagnosis = request.POST.get('diagnosis', '').strip()
        consultant_id = request.POST.get('primary_consultant')
        phone_no = request.POST.get('phone_no', '').strip()

        # Configurable field validations for MR No and Phone No
        is_mr_valid, mr_err = validate_field_value('mr_no', mr_no_raw)
        if not is_mr_valid:
            messages.error(request, mr_err)
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
            })

        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
            })

        if not pt_name:
            messages.error(request, "Patient Name is mandatory.")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
            })

        try:
            mr_no = int(mr_no_raw)
        except ValueError:
            messages.error(request, "MR Number must be a valid integer number.")
            return render(request, 'patients/patient_register.html', {
                'consultants': consultants,
                'sex_choices': SEX_CHOICES,
                'form_data': request.POST,
            })

        # Check uniqueness of MR No
        existing = Patient.objects.filter(mr_no=mr_no).first()
        if existing:
            messages.warning(
                request,
                f"Patient with MR No. {mr_no} is already registered ({existing.pt_name}). You can schedule treatment directly below."
            )
            return redirect('schedule_patient', patient_id=existing.id)

        age = int(float(age_raw)) if age_raw else None
        consultant_obj = Consultant.objects.filter(id=consultant_id).first() if consultant_id else None

        patient = Patient.objects.create(
            mr_no=mr_no,
            pt_name=pt_name,
            rt_no=rt_no,
            age=age,
            sex=sex or 'M',
            diagnosis=diagnosis or 'Under Evaluation',
            site_of_treatment=None,  # Moved to Scheduling form
            primary_consultant=consultant_obj,
            phone_no=phone_no,
            created_by=request.user
        )

        AuditLog.objects.create(
            user=request.user,
            action="PATIENT_REGISTER",
            mr_no=str(patient.mr_no),
            details=f"Registered patient {patient.pt_name} (MR: {patient.mr_no})",
            ip_address=request.META.get('REMOTE_ADDR')
        )

        messages.success(request, f"Patient {patient.pt_name} (MR: {patient.mr_no}) registered successfully!")

        # Scheduling choice: schedule now or later
        if 'schedule_now' in request.POST:
            return redirect('schedule_patient', patient_id=patient.id)
        else:
            return redirect('patient_detail', patient_id=patient.id)

    context = {
        'consultants': consultants,
        'sex_choices': SEX_CHOICES,
        'form_data': {},
    }
    return render(request, 'patients/patient_register.html', context)


@login_required
def patient_edit_view(request, patient_id):
    """
    Patient Edit View.
    Rule: MR No and Patient Name are LOCKED. Only Admin / SubAdmin can edit them.
    Age, Diagnosis, Site, Phone, etc. can be edited by authorized users.
    """
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
        is_ph_valid, ph_err = validate_field_value('phone_no', phone_no)
        if not is_ph_valid:
            messages.error(request, ph_err)
            return redirect('patient_edit', patient_id=patient.id)

        # Other clinical fields
        age_raw = request.POST.get('age', '').strip()
        patient.age = int(float(age_raw)) if age_raw else None
        patient.rt_no = request.POST.get('rt_no', '').strip()
        patient.sex = request.POST.get('sex', patient.sex)
        patient.diagnosis = request.POST.get('diagnosis', '').strip()
        patient.site_of_treatment = request.POST.get('site_of_treatment', patient.site_of_treatment)
        patient.phone_no = phone_no

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
        'sex_choices': SEX_CHOICES,
        'site_choices': DEFAULT_SITES,
    }
    return render(request, 'patients/patient_edit.html', context)


@login_required
def patient_detail_view(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    schedules = patient.schedules.select_related(
        'machine', 'shift', 'technique', 'special_technique', 'intent', 'tagged_to'
    ).order_by('id')

    audit_logs = AuditLog.objects.filter(mr_no=str(patient.mr_no)).select_related('user').order_by('-log_datetime')[:50]

    context = {
        'patient': patient,
        'schedules': schedules,
        'audit_logs': audit_logs,
        'can_edit_identity': can_edit_locked_identity(request.user),
    }
    return render(request, 'patients/patient_detail.html', context)


@login_required
def patient_search_view(request):
    query = request.GET.get('q', '').strip()
    status_filter = request.GET.get('status', '').strip()

    patients = Patient.objects.select_related('primary_consultant').prefetch_related('schedules__machine').all()

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
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'query': query,
        'status_filter': status_filter,
        'total_count': paginator.count,
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

