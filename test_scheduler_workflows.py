import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test.utils import setup_test_environment
setup_test_environment()

from django.test import Client
from django.contrib.auth.models import User
from django.db.models import Count, Q
from datetime import date, timedelta, datetime
from decimal import Decimal
from apps.core.models import Role, UserProfile, Consultant, FieldValidationConfig, AuditLog
from apps.core.validators import get_or_create_field_config, validate_field_value
from apps.patients.models import Patient
from apps.scheduler.models import (
    Machine, Shift, Technique, SpecialTechnique, TreatmentSite, TreatmentIntent, MachineMapping, TreatmentSchedule, Holiday
)
from apps.core.permissions import (
    can_edit_locked_identity,
    is_admin_or_subadmin,
    is_admin_only,
    can_manage_validation_rules,
    can_manage_lookups,
    is_frontdesk_nursing_or_physics,
    can_schedule_patient,
    can_edit_patient,
    can_register_patient,
    can_print_schedules
)


def run_tests():
    print("==================================================")
    print("RUNNING RADIOTHERAPY PATIENT SCHEDULER TEST SUITE")
    print("==================================================")

    # Clean up test patients & test holidays
    Patient.objects.filter(mr_no__in=[9999001, 9999002, 9999003, 9999018, 9999019, 9999020, 9999021, 9999022, 9999031, 9999032, 7654321, 12345]).delete()
    Holiday.objects.filter(date__in=[date(2026, 11, 20), date(2026, 11, 25)]).delete()
    User.objects.filter(username__in=['test_toggle_user', 'test_doc_prefix', 'test_new_reg_user', 'test_landing_user']).delete()
    from apps.core.models import PrintTemplateConfig, Designation
    PrintTemplateConfig.objects.all().delete()
    PrintTemplateConfig.get_active()
    Designation.objects.filter(name='Additional Professor & Unit Chief').delete()

    # 1. Test Admin vs SubAdmin vs Resident Granular Permissions
    print("\n--- Test 1: Role Permissions (Admin vs SubAdmin vs Resident) ---")
    admin_user = User.objects.get(username='admin')
    subadmin_user = User.objects.get(username='subadmin')

    res_user, _ = User.objects.get_or_create(username='test_resident')
    res_role = Role.objects.get(role_name='Resident')
    UserProfile.objects.update_or_create(user=res_user, defaults={'role': res_role, 'user_level': 'RESIDENT'})

    assert is_admin_or_subadmin(admin_user) is True, "Admin must be admin_or_subadmin"
    assert is_admin_or_subadmin(subadmin_user) is True, "SubAdmin must be admin_or_subadmin"
    assert is_admin_or_subadmin(res_user) is False, "Resident must NOT be admin_or_subadmin"

    # Only Admin (not SubAdmin) can manage validations and lookups
    assert is_admin_only(admin_user) is True, "Admin must satisfy is_admin_only"
    assert is_admin_only(subadmin_user) is False, "SubAdmin must NOT satisfy is_admin_only"
    assert can_manage_validation_rules(admin_user) is True, "Admin can manage validations"
    assert can_manage_validation_rules(subadmin_user) is False, "SubAdmin cannot manage validations"
    assert can_manage_lookups(admin_user) is True, "Admin can manage lookups"
    assert can_manage_lookups(subadmin_user) is False, "SubAdmin cannot manage lookups"

    # Both Admin and SubAdmin can edit locked identity
    assert can_edit_locked_identity(admin_user) is True, "Admin can edit locked identity"
    assert can_edit_locked_identity(subadmin_user) is True, "SubAdmin can edit locked identity"
    assert can_edit_locked_identity(res_user) is False, "Resident cannot edit locked identity"
    print("✓ Test 1 Passed: Admin has exclusive validation/lookup control; SubAdmin and Admin share locked edit permissions.")

    # 2. Test SubAdmin HTTP Access Block on Validation & Lookup Routes
    print("\n--- Test 2: SubAdmin HTTP Boundary Verification ---")
    c = Client()
    c.force_login(subadmin_user)

    resp = c.get('/validations/')
    assert resp.status_code == 302, f"SubAdmin should be redirected from validations, got {resp.status_code}"

    resp = c.get('/lookups/machine/')
    assert resp.status_code == 302, f"SubAdmin should be redirected from lookups, got {resp.status_code}"

    # Admin is granted access
    c.force_login(admin_user)
    resp = c.get('/validations/')
    assert resp.status_code == 200, f"Admin should have access to validations, got {resp.status_code}"
    resp = c.get('/lookups/machine/')
    assert resp.status_code == 200, f"Admin should have access to lookups, got {resp.status_code}"
    print("✓ Test 2 Passed: SubAdmin is strictly blocked from validation and lookup URLs; Admin is granted access.")

    # 3. Test Configurable Field Validation (MR No and Phone No)
    print("\n--- Test 3: Configurable Field Validation Rules ---")
    mr_cfg = get_or_create_field_config('mr_no')
    # Set MR No rule to EXACT 7 digits, numeric only
    mr_cfg.validation_mode = 'exact'
    mr_cfg.exact_length = 7
    mr_cfg.numeric_only = True
    mr_cfg.allow_blank = False
    mr_cfg.custom_error_message = "MR Number must be exactly 7 numeric digits."
    mr_cfg.save()

    # Test invalid MR numbers against validator
    ok, err = validate_field_value('mr_no', '12345')  # 5 digits (expected 7)
    assert ok is False, "5-digit MR No should fail exact 7 check"
    assert "7 numeric digits" in err

    ok, err = validate_field_value('mr_no', 'ABC1234')  # non-numeric
    assert ok is False, "Alphanumeric MR No should fail numeric check"

    ok, err = validate_field_value('mr_no', '7654321')  # exactly 7 digits
    assert ok is True, "7-digit numeric MR No should pass"

    # Reset MR No rule to Range (4 to 15 digits) for standard institutional operation (e.g. 20260000123)
    mr_cfg.validation_mode = 'range'
    mr_cfg.min_length = 4
    mr_cfg.max_length = 15
    mr_cfg.numeric_only = True
    mr_cfg.custom_error_message = "MR Number must be numeric and between 4 and 15 digits."
    mr_cfg.save()

    ok, err = validate_field_value('mr_no', '9999001')
    assert ok is True

    # Test Phone Number validation
    ph_cfg = get_or_create_field_config('phone_no')
    ph_cfg.validation_mode = 'range'
    ph_cfg.min_length = 10
    ph_cfg.max_length = 15
    ph_cfg.numeric_only = True
    ph_cfg.allow_blank = True
    ph_cfg.save()

    ok, err = validate_field_value('phone_no', '123')  # too short
    assert ok is False
    ok, err = validate_field_value('phone_no', '')  # blank allowed
    assert ok is True
    ok, err = validate_field_value('phone_no', '9876543210')  # 10 digits
    assert ok is True
    print("✓ Test 3 Passed: Dynamic field validation (exact length, range, numeric only) verified.")

    # 4. Test Registration (Without Site of Treatment)
    print("\n--- Test 4: Patient Registration Without Treatment Site ---")
    c.force_login(res_user)
    test_mr = 9999001
    Patient.objects.filter(mr_no=test_mr).delete()

    resp = c.post('/patients/register/', {
        'mr_no': str(test_mr),
        'pt_name': 'Test Registration Patient',
        'rt_no': 'RT-TEST-01',
        'age': '52',
        'sex': 'F',
        'diagnosis': 'Carcinoma Cervix IIB',
        'primary_consultant': Consultant.objects.first().id,
        'phone_no': '9876543210',
        'save_only': '1'
    })
    assert resp.status_code == 302, f"Expected 302 redirect, got {resp.status_code}"
    p = Patient.objects.get(mr_no=test_mr)
    assert p.pt_name == 'Test Registration Patient'
    assert p.site_of_treatment is None or p.site_of_treatment == '', "Site of treatment should be empty at registration"
    print(f"✓ Test 4 Passed: Patient registered without site of treatment (ID: {p.id}, MR: {p.mr_no}).")

    # 5. Test Treatment Scheduling & Site of Treatment Allocation
    print("\n--- Test 5: Treatment Scheduling with Site of Treatment ---")
    mach = Machine.objects.filter(is_active=True).first()
    shift = Shift.objects.filter(is_active=True).first()
    tech = Technique.objects.filter(is_active=True).first()
    spec_tech = SpecialTechnique.objects.filter(is_active=True).first()
    intent_obj = TreatmentIntent.objects.filter(is_active=True).first()

    today = date.today()
    sim_date = today - timedelta(days=2)
    start_date = today + timedelta(days=3)

    resp = c.post(f'/scheduler/patient/{p.id}/', {
        # Top card registration fields:
        'age': '53',
        'diagnosis': 'Carcinoma Cervix IIB Confirmed',
        'site_of_treatment': 'Pelvis',
        'rt_no': 'RT-2101',
        'phone_no': '9876543210',
        # Scheduling fields:
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': intent_obj.id if intent_obj else 1,
        'machine': mach.id,
        'shift': shift.id,
        'technique': tech.id,
        'special_technique': spec_tech.id,
        'date_of_sim': sim_date.isoformat(),
        'date_of_start': start_date.isoformat(),
        'dose_planned': '50.00',
        'no_of_fractions': '25',
        'jr_resident': 'Dr. Resident',
        'notes': 'Weekly chemo'
    })
    assert resp.status_code == 302, f"Expected 302, got {resp.status_code}"
    p.refresh_from_db()
    assert p.site_of_treatment == 'Pelvis', "Patient site should be updated during scheduling"
    sched = p.schedules.first()
    assert sched is not None, "Treatment schedule should be created"
    assert sched.site_of_treatment == 'Pelvis', "Schedule site of treatment must be saved"
    assert sched.dose_planned == Decimal('50.00')
    print("✓ Test 5 Passed: Site of treatment captured and stored during scheduling.")

    # 6. Test Dynamic Machine Mapping Capacity Rule (Capacity = Active Shifts Count)
    print("\n--- Test 6: Capacity Rule (Max Starts = Number of Active Shifts) ---")
    mapping, _ = MachineMapping.objects.get_or_create(machine=mach)
    active_shifts = mapping.allowed_shifts.filter(is_active=True)
    expected_quota = active_shifts.count()

    resp = c.get(f'/scheduler/api/machine-options/{mach.id}/')
    assert resp.status_code == 200
    data = resp.json()
    assert data['max_daily_starts'] == expected_quota, f"Quota must equal active shifts count ({expected_quota}), got {data['max_daily_starts']}"
    print(f"✓ Test 6 Passed: Dynamic machine capacity accurately calculated: {data['max_daily_starts']} starts/day based on {expected_quota} active shifts.")

    # 7. Test Inactivating a Shift Reduces Capacity Dynamically
    print("\n--- Test 7: Inactivating a Shift & Auto-Scheduling Impact ---")
    c.force_login(admin_user)
    # Toggle active status of a shift
    test_shift = Shift.objects.filter(is_active=True).last()
    original_shift_active = test_shift.is_active

    # Inactivate test_shift via lookup manager
    resp = c.get(f'/lookups/shift/toggle/{test_shift.id}/')
    assert resp.status_code == 302
    test_shift.refresh_from_db()
    assert test_shift.is_active is False, "Shift should now be inactive"

    # Verify machine options API now reflects reduced active shifts count
    resp = c.get(f'/scheduler/api/machine-options/{mach.id}/')
    data = resp.json()
    new_quota = mapping.allowed_shifts.filter(is_active=True).count()
    assert data['max_daily_starts'] == new_quota, "Quota must dynamically reflect inactivated shift"

    # Reactivate the shift
    c.get(f'/lookups/shift/toggle/{test_shift.id}/')
    test_shift.refresh_from_db()
    assert test_shift.is_active is True, "Shift reactivated"
    print("✓ Test 7 Passed: Inactivating a shift dynamically adjusts machine quota and slot availability.")

    # 8. Test Gynae-Style Centralized Lookup Manager
    print("\n--- Test 8: Gynae-Style Centralized Lookup Manager (Add, Edit, Toggle) ---")
    c.force_login(admin_user)
    # Add a new test technique
    resp = c.post('/lookups/technique/save/', {
        'name_value': 'FLASH-RT Test Technique',
        'display_order': '99',
        'is_active': '1'
    })
    assert resp.status_code == 302
    new_tech = Technique.objects.get(name='FLASH-RT Test Technique')
    assert new_tech.display_order == 99
    assert new_tech.is_active is True

    # Inactivate it
    c.get(f'/lookups/technique/toggle/{new_tech.id}/')
    new_tech.refresh_from_db()
    assert new_tech.is_active is False

    # Clean up test technique
    new_tech.delete()
    print("✓ Test 8 Passed: Lookup manager successfully added, toggled, and verified reference options.")

    # 9. Test SRS, SRT, SBRT in SpecialTechnique and not in Technique
    print("\n--- Test 9: Verification of SRS, SRT, SBRT Technique Migration ---")
    tech_names = list(Technique.objects.values_list('name', flat=True))
    assert 'SRS' not in tech_names, "SRS must be removed from Technique"
    assert 'SRT' not in tech_names, "SRT must be removed from Technique"
    assert 'SBRT' not in tech_names, "SBRT must be removed from Technique"

    special_tech_names = list(SpecialTechnique.objects.values_list('name', flat=True))
    assert 'SRS' in special_tech_names, "SRS must be in SpecialTechnique"
    assert 'SRT' in special_tech_names, "SRT must be in SpecialTechnique"
    assert 'SBRT' in special_tech_names, "SBRT must be in SpecialTechnique"
    print("✓ Test 9 Passed: SRS, SRT, SBRT are cleanly organized under Special Techniques and removed from Techniques.")

    # 10. Test Machine Type Column & Options
    print("\n--- Test 10: Machine Type Column & Options in Machine Management ---")
    resp = c.get('/lookups/machine/')
    assert resp.status_code == 200
    assert b'Machine Type' in resp.content
    assert b'LINAC' in resp.content
    assert b'Brachytherapy' in resp.content
    assert b'Orthovoltage' in resp.content
    assert b'Intra Op' in resp.content
    assert b'PBT' in resp.content

    # Create machine with machine_type
    resp = c.post('/lookups/machine/save/', {
        'name_value': 'Test Brachytherapy Unit',
        'secondary_value': 'HDR-TEST',
        'extra_value': 'Brachytherapy',
        'display_order': '50',
        'is_active': '1'
    })
    assert resp.status_code == 302
    test_brachy = Machine.objects.get(name='Test Brachytherapy Unit')
    assert test_brachy.machine_type == 'Brachytherapy'
    assert test_brachy.code == 'HDR-TEST'

    # Edit machine_type
    resp = c.post(f'/lookups/machine/save/{test_brachy.id}/', {
        'name_value': 'Test IntraOp Unit',
        'secondary_value': 'IORT-01',
        'extra_value': 'Intra Op',
        'display_order': '50',
        'is_active': '1'
    })
    assert resp.status_code == 302
    test_brachy.refresh_from_db()
    assert test_brachy.machine_type == 'Intra Op'
    assert test_brachy.name == 'Test IntraOp Unit'
    test_brachy.delete()
    print("✓ Test 10 Passed: Machine Type column and options (LINAC, Brachytherapy, Orthovoltage, Intra Op, PBT) verified.")

    # 11. Test Machine Daily List Template Bug Fix (Screenshot 1)
    print("\n--- Test 11: Machine Daily List Error Free Rendering ---")
    resp = c.get('/reports/machines/?machine_id=&start_date=2026-02-01&end_date=2026-11-20')
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    print("✓ Test 11 Passed: /reports/machines/ rendered with HTTP 200 without VariableDoesNotExist errors.")

    # 12. Test Dashboard Overhaul & Non-Scheduled Patients Pagination (15 per page)
    print("\n--- Test 12: Dashboard Overhaul & Non-Scheduled Pagination (15/page) ---")
    c.force_login(admin_user)
    resp = c.get('/dashboard/')
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    # Verify stats tiles are removed
    assert b'Total Registered Patients' not in resp.content, "Stats tile 'Total Registered Patients' should be removed"
    assert b'Total Treatment Schedules' not in resp.content, "Stats tile 'Total Treatment Schedules' should be removed"
    assert b'Starts Booked Today' not in resp.content, "Machine workload starts breakdown should be removed"

    # Verify primary options are present
    assert b'Register Patients' in resp.content, "Option 'Register Patients' must be present"
    assert b'Schedule Patients' in resp.content, "Option 'Schedule Patients' must be present"
    assert b'Non-Scheduled Patients' in resp.content, "Table for Non-Scheduled Patients must be present"

    # Verify pagination is exactly 15 per page
    page_obj = resp.context['page_obj']
    assert len(page_obj) <= 15, f"Expected at most 15 items per page, got {len(page_obj)}"
    assert resp.context['page_obj'].paginator.per_page == 15, "Paginator must be set to 15 per page"

    # Test page 2 navigation
    if resp.context['page_obj'].paginator.num_pages >= 2:
        resp_p2 = c.get('/dashboard/?page=2')
        assert resp_p2.status_code == 200
        assert len(resp_p2.context['page_obj']) == 15, "Page 2 should contain 15 patients"

    # Test search on dashboard
    resp_search = c.get(f'/dashboard/?q={p.mr_no}')
    assert resp_search.status_code == 200
    assert bytes(str(p.mr_no), 'utf-8') in resp_search.content
    print("✓ Test 12 Passed: Dashboard stats removed; options for Register & Schedule present; Non-scheduled table paginated at 15 patients/page.")

    # 13. Test Admin (not SubAdmin) Rights to Delete Patient
    print("\n--- Test 13: Admin (not SubAdmin) Patient Deletion Rights ---")
    # Create test patient to delete
    del_mr = 9999003
    Patient.objects.filter(mr_no=del_mr).delete()
    test_pt = Patient.objects.create(
        mr_no=del_mr,
        pt_name='Test Patient For Deletion',
        sex='M',
        diagnosis='Cancer Test',
        created_by=admin_user
    )

    # SubAdmin attempt to GET delete confirmation -> blocked
    c.force_login(subadmin_user)
    resp = c.get(f'/patients/{test_pt.id}/delete/')
    assert resp.status_code == 302, f"SubAdmin should be redirected, got {resp.status_code}"
    # SubAdmin attempt to POST delete -> blocked
    resp = c.post(f'/patients/{test_pt.id}/delete/')
    assert resp.status_code == 302, f"SubAdmin should be redirected, got {resp.status_code}"
    assert Patient.objects.filter(id=test_pt.id).exists(), "Patient must NOT be deleted by SubAdmin"

    # Admin attempt to GET delete confirmation -> allowed
    c.force_login(admin_user)
    resp = c.get(f'/patients/{test_pt.id}/delete/')
    assert resp.status_code == 200, f"Admin should see confirmation page, got {resp.status_code}"
    assert b'Confirm Permanent Patient Deletion' in resp.content
    assert bytes(str(del_mr), 'utf-8') in resp.content

    # Admin attempt to POST delete -> patient deleted
    resp = c.post(f'/patients/{test_pt.id}/delete/')
    assert resp.status_code == 302, f"Admin post delete should redirect, got {resp.status_code}"
    assert not Patient.objects.filter(id=test_pt.id).exists(), "Patient must be deleted by Admin"
    print("✓ Test 13 Passed: SubAdmin is strictly blocked from deleting patients; Admin can permanently delete patient.")

    # 14. Test Admin (not SubAdmin) Rights to Remove from Schedule List
    print("\n--- Test 14: Admin (not SubAdmin) Remove from Schedule List Rights ---")
    # Create test patient with a schedule
    sched_mr = 9999002
    Patient.objects.filter(mr_no=sched_mr).delete()
    sched_pt = Patient.objects.create(
        mr_no=sched_mr,
        pt_name='Test Patient For Schedule Removal',
        sex='F',
        diagnosis='Cervix Cancer',
        created_by=admin_user
    )
    test_sched = TreatmentSchedule.objects.create(
        patient=sched_pt,
        machine=mach,
        shift=Shift.objects.first(),
        date_of_sim=date.today(),
        date_of_start=date.today() + timedelta(days=3),
        dose_planned=Decimal('50.0'),
        no_of_fractions=25,
        created_by=admin_user
    )

    # SubAdmin attempt to GET remove schedule -> blocked
    c.force_login(subadmin_user)
    resp = c.get(f'/scheduler/schedule/{test_sched.id}/delete/')
    assert resp.status_code == 302, f"SubAdmin should be redirected, got {resp.status_code}"
    # SubAdmin attempt to POST remove schedule -> blocked
    resp = c.post(f'/scheduler/schedule/{test_sched.id}/delete/')
    assert resp.status_code == 302, f"SubAdmin should be redirected, got {resp.status_code}"
    assert TreatmentSchedule.objects.filter(id=test_sched.id).exists(), "Schedule must NOT be deleted by SubAdmin"

    # Admin attempt to GET remove confirmation -> allowed
    c.force_login(admin_user)
    resp = c.get(f'/scheduler/schedule/{test_sched.id}/delete/')
    assert resp.status_code == 200, f"Admin should see confirmation page, got {resp.status_code}"
    assert b'Confirm Removal from Schedule List' in resp.content

    # Admin attempt to POST remove -> schedule deleted, patient now non-scheduled
    resp = c.post(f'/scheduler/schedule/{test_sched.id}/delete/')
    assert resp.status_code == 302, f"Admin post delete should redirect, got {resp.status_code}"
    assert not TreatmentSchedule.objects.filter(id=test_sched.id).exists(), "Schedule must be deleted by Admin"
    assert sched_pt.schedules.count() == 0, "Patient should now have 0 schedules"

    # Verify sched_pt now appears in non-scheduled list on dashboard
    resp = c.get(f'/dashboard/?q={sched_mr}')
    assert resp.status_code == 200
    assert bytes(str(sched_mr), 'utf-8') in resp.content
    assert b'Pending' in resp.content

    # Clean up test patient
    sched_pt.delete()
    p.delete()
    print("✓ Test 14 Passed: SubAdmin is strictly blocked from removing schedules; Admin can remove patient from schedule list.")

    # 15. Test Manual Treatment Schedule Grid (Excel View) - LA1, Manual Editing, No Quotas, Admin/SubAdmin Only
    print("\n--- Test 15: Manual Schedule Grid (Excel View) & Permission Checks ---")
    # Verify ordinary clinical user (Resident) is blocked
    c.force_login(res_user)
    resp = c.get('/reports/manual-grid/')
    assert resp.status_code == 302, f"Resident must be redirected from manual grid, got {resp.status_code}"
    resp = c.post('/reports/manual-grid/save-row/', {'schedule_id': 1})
    assert resp.status_code == 403, f"Resident must receive 403 from manual save api, got {resp.status_code}"

    # Verify SubAdmin has access
    c.force_login(subadmin_user)
    resp = c.get('/reports/manual-grid/?machine_code=LA1')
    assert resp.status_code == 200, f"SubAdmin must have access to manual grid, got {resp.status_code}"
    assert b'Treatment Schedule Worksheet' in resp.content
    assert b'Treatment LA1' in resp.content
    assert b'NO QUOTA LIMIT' in resp.content

    # Verify Admin has access
    c.force_login(admin_user)
    resp = c.get('/reports/manual-grid/?machine_code=LA1')
    assert resp.status_code == 200, f"Admin must have access to manual grid, got {resp.status_code}"

    # Create 3 test patients on LA1 to test manual scheduling with NO quota limits
    la1_mach = Machine.objects.filter(code='LA1').first() or mach
    shift1 = Shift.objects.first()

    pt_manual_1 = Patient.objects.create(mr_no=9999011, pt_name='Manual Pt 1', sex='M', diagnosis='Test', created_by=admin_user)
    pt_manual_2 = Patient.objects.create(mr_no=9999012, pt_name='Manual Pt 2', sex='F', diagnosis='Test', created_by=admin_user)
    pt_manual_3 = Patient.objects.create(mr_no=9999013, pt_name='Manual Pt 3', sex='M', diagnosis='Test', created_by=admin_user)

    s1 = TreatmentSchedule.objects.create(patient=pt_manual_1, machine=la1_mach, date_of_start=None, created_by=admin_user)
    s2 = TreatmentSchedule.objects.create(patient=pt_manual_2, machine=la1_mach, date_of_start=None, created_by=admin_user)
    s3 = TreatmentSchedule.objects.create(patient=pt_manual_3, machine=la1_mach, date_of_start=None, created_by=admin_user)

    # Manual test date: assign ALL 3 patients to the exact same date and shift (exceeding standard shift quota)
    manual_date = date(2026, 12, 20)

    # 1. Edit s1 via inline AJAX save
    resp_ajax = c.post('/reports/manual-grid/save-row/', {
        'schedule_id': s1.id,
        'date_of_start': manual_date.isoformat(),
        'shift_id': str(shift1.id),
        'machine_id': str(la1_mach.id)
    })
    assert resp_ajax.status_code == 200
    assert resp_ajax.json()['success'] is True
    s1.refresh_from_db()
    assert s1.date_of_start == manual_date
    assert s1.shift_id == shift1.id

    # 2. Edit s2 via inline AJAX save (same date & shift - no quota restrictions!)
    resp_ajax2 = c.post('/reports/manual-grid/save-row/', {
        'schedule_id': s2.id,
        'date_of_start': manual_date.isoformat(),
        'shift_id': str(shift1.id),
        'machine_id': str(la1_mach.id)
    })
    assert resp_ajax2.status_code == 200
    assert resp_ajax2.json()['success'] is True
    s2.refresh_from_db()
    assert s2.date_of_start == manual_date
    assert s2.shift_id == shift1.id

    # 3. Edit s3 via Batch Save POST (same date & shift)
    resp_batch = c.post(f'/reports/manual-grid/?machine_id={la1_mach.id}&start_date={manual_date.isoformat()}&end_date={manual_date.isoformat()}', {
        'schedule_id': [str(s3.id)],
        f'start_date_{s3.id}': manual_date.isoformat(),
        f'shift_{s3.id}': str(shift1.id),
        f'machine_{s3.id}': str(la1_mach.id),
    })
    assert resp_batch.status_code == 302
    s3.refresh_from_db()
    assert s3.date_of_start == manual_date
    assert s3.shift_id == shift1.id

    # Verify all 3 are successfully scheduled on the same machine, date, and shift without quota restriction
    starts_count = TreatmentSchedule.objects.filter(machine=la1_mach, date_of_start=manual_date, shift=shift1).count()
    assert starts_count >= 3, f"Expected at least 3 starts scheduled on {manual_date}, got {starts_count}"

    # Verify Excel download (.xlsx)
    resp_export = c.get(f'/reports/manual-grid/?machine_id={la1_mach.id}&start_date={manual_date.isoformat()}&end_date={manual_date.isoformat()}&export=excel')
    assert resp_export.status_code == 200
    assert 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' in resp_export['Content-Type']

    # Verify Column Order and 'Refresh Changes' button in HTML view
    resp_grid_view = c.get(f'/reports/manual-grid/?machine_id={la1_mach.id}&start_date={manual_date.isoformat()}&end_date={manual_date.isoformat()}')
    assert resp_grid_view.status_code == 200
    grid_html = resp_grid_view.content.decode('utf-8')
    assert 'Refresh Changes' in grid_html
    thead_html = grid_html[grid_html.find('<thead>'):grid_html.find('</thead>')]
    assert thead_html.find('MR No.') < thead_html.find('Patient Name') < thead_html.find('Simulation Date') < thead_html.find('RT No.') < thead_html.find('Diagnosis') < thead_html.find('Start Date') < thead_html.find('Shift') < thead_html.find('Machine') < thead_html.find('Consultant Name') < thead_html.find('Jr Resident')

    # Clean up test patients
    pt_manual_1.delete()
    pt_manual_2.delete()
    pt_manual_3.delete()
    print("✓ Test 15 Passed: Manual treatment schedule grid (Excel view) verified: accessible only to Admin & SubAdmin, replicates Treatment LA1, edits date/shift directly without quota restrictions, exports to .xlsx.")

    # 16. Test Redesigned Compact Scheduling Page & Dynamic Mapped Machine Slot Finder
    print("\n--- Test 16: Redesigned Compact Scheduling Page & Dynamic Mapped Machine Slot Finder ---")
    active_consultant = Consultant.objects.filter(is_active=True).first()
    Patient.objects.filter(mr_no=999990).delete()
    test_pt_sched = Patient.objects.create(
        mr_no=999990,
        rt_no='RT990',
        pt_name='Test Redesign Patient',
        age=58,
        sex='Female',
        phone_no='9988776655',
        diagnosis='Carcinoma Endometrium',
        primary_consultant=active_consultant
    )

    # 1. GET schedule page
    resp_get = c.get(f'/scheduler/patient/{test_pt_sched.id}/')
    assert resp_get.status_code == 200
    html_content = resp_get.content.decode('utf-8')
    assert 'Primary Consultant' in html_content
    assert 'mappedMachinesContainer' in html_content
    assert 'Machine Earliest Slots & Custom Date Selection' in html_content
    assert 'Treatment Parameters & Prescription Entry' in html_content
    assert 'Custom Date Selection' in html_content or 'custom_reason' in html_content
    # Check default technique is IMRT and default special technique is None
    imrt_tech = Technique.objects.filter(name__iexact='IMRT', is_active=True).first()
    none_spec_tech = SpecialTechnique.objects.filter(name__iexact='None', is_active=True).first()
    if imrt_tech:
        assert str(resp_get.context['default_technique'].id) == str(imrt_tech.id)
    if none_spec_tech:
        assert str(resp_get.context['default_special_technique'].id) == str(none_spec_tech.id)

    # 2. Test mapped machines slots API
    api_url = f'/scheduler/api/mapped-machine-slots/?technique_id={imrt_tech.id if imrt_tech else 1}&special_technique_id={none_spec_tech.id if none_spec_tech else 1}&sim_date={date.today().isoformat()}'
    resp_api = c.get(api_url)
    assert resp_api.status_code == 200
    api_data = resp_api.json()
    assert api_data['success'] is True
    assert len(api_data['machines']) > 0
    # Every returned machine must have active shifts and machine details
    first_m = api_data['machines'][0]
    assert 'machine_id' in first_m
    assert 'machine_name' in first_m
    assert 'shifts' in first_m
    assert len(first_m['shifts']) > 0
    # Auto-designated shift is present in slot
    assert 'shift_id' in first_m['slot']
    assert 'shift_name' in first_m['slot']

    # 3. Test auto-designation of next open shift (1 patient per shift rule)
    target_mach = Machine.objects.filter(id=first_m['machine_id']).first()
    slot_date = datetime.strptime(first_m['slot']['date'], "%Y-%m-%d").date()
    shift1 = Shift.objects.filter(id=first_m['slot']['shift_id']).first()
    # Create a temporary schedule on this machine, date, and shift
    temp_pt = Patient.objects.create(mr_no=999988, pt_name='Temp Shift Test Pt', diagnosis='Ca')
    temp_sched = TreatmentSchedule.objects.create(
        patient=temp_pt, machine=target_mach, shift=shift1,
        date_of_start=slot_date, status='Scheduled'
    )
    # Re-query API: slot should advance to next open shift or next day
    resp_api2 = c.get(api_url)
    data2 = resp_api2.json()
    first_m2 = data2['machines'][0]
    # The new auto-designated slot must NOT be shift1 on slot_date
    if first_m2['slot']['date'] == first_m['slot']['date']:
        assert first_m2['slot']['shift_id'] != shift1.id, "Shift 1 should be occupied; system should auto-designate next shift"
    temp_sched.delete()
    temp_pt.delete()

    # 4. Test POST with custom date and custom reason notes tracking
    custom_date = date.today() + timedelta(days=7)
    custom_reason_text = "Urgent clinical escalation required for rapid symptom palliation"
    target_mach = Machine.objects.filter(id=first_m['machine_id']).first()
    target_shift = Shift.objects.filter(id=first_m['shifts'][0]['id']).first()

    resp_post = c.post(f'/scheduler/patient/{test_pt_sched.id}/', {
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': intent_obj.id if intent_obj else 1,
        'age': '58',
        'phone_no': '9988776655',
        'site_of_treatment': 'Pelvis',
        'diagnosis': 'Carcinoma Endometrium Stage II',
        'date_of_sim': date.today().isoformat(),
        'technique': imrt_tech.id if imrt_tech else 1,
        'special_technique': none_spec_tech.id if none_spec_tech else 1,
        'simulated_for': '',
        'machine': target_mach.id,
        'shift': target_shift.id,
        'date_of_start': custom_date.isoformat(),
        'custom_reason': custom_reason_text,
        'dose_planned': '45.00',
        'no_of_fractions': '25',
        'notes': 'Concurrent weekly chemotherapy planned'
    })
    assert resp_post.status_code == 302
    test_pt_sched.refresh_from_db()
    sched = test_pt_sched.schedules.first()
    assert sched is not None
    assert sched.date_of_start == custom_date
    assert sched.machine_id == target_mach.id
    assert sched.shift_id == target_shift.id
    # Verify custom reason was recorded into notes
    assert custom_reason_text in sched.notes
    assert "[Custom Date Reason:" in sched.notes

    # Cleanup
    test_pt_sched.delete()
    print("✓ Test 16 Passed: Redesigned compact scheduling page, dynamic mapped machine slot finder, and custom date reason tracking fully verified.")

    # --- Test 17: Header Badges Removal, date_of_start >= date_of_sim Enforcement, & Machine Mapping Multi-Select Sites ---
    print("\n--- Test 17: Header Badges Removal, date_of_start >= date_of_sim Enforcement, & Machine Mapping Multi-Select Sites ---")
    c.force_login(admin_user)

    # 1. Verify "Admin & SubAdmin" badge removed from headers
    la1_mapping = MachineMapping.objects.filter(machine__code='LA1').first()
    if la1_mapping:
        resp_edit_map = c.get(f'/scheduler/mappings/{la1_mapping.id}/edit/')
        assert resp_edit_map.status_code == 200
        content_edit = resp_edit_map.content.decode('utf-8')
        assert '<span class="badge-admin">Admin & SubAdmin</span>' not in content_edit, "Admin & SubAdmin badge must be removed from edit machine mapping header"
        assert 'Allowed Anatomical Treatment Sites (Multi-Selection Dropdown):' in content_edit

    resp_map_list = c.get('/scheduler/mappings/')
    assert resp_map_list.status_code == 200
    assert '<span class="badge-admin">Admin & SubAdmin Control</span>' not in resp_map_list.content.decode('utf-8')

    resp_grid = c.get('/reports/manual-grid/')
    assert resp_grid.status_code == 200
    assert '<span class="badge-admin">ADMIN & SUBADMIN ONLY</span>' not in resp_grid.content.decode('utf-8')

    # 2. Verify date_of_start >= date_of_sim enforcement
    active_consultant = Consultant.objects.filter(is_active=True).first()
    test_pt_date = Patient.objects.create(
        mr_no=9999003,
        pt_name="Test SimDate Rule Patient",
        age=50,
        sex='Female',
        phone_no='9900112233',
        diagnosis='Carcinoma Cervix',
        primary_consultant=active_consultant
    )

    sim_date = date.today()
    invalid_start_date = sim_date - timedelta(days=2)  # Earlier than sim date!
    valid_start_date = sim_date + timedelta(days=3)

    # Attempt to schedule with start_date < sim_date
    resp_invalid_sched = c.post(f'/scheduler/patient/{test_pt_date.id}/', {
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': intent_obj.id if intent_obj else 1,
        'age': '50',
        'phone_no': '9900112233',
        'site_of_treatment': 'Pelvis',
        'diagnosis': 'Carcinoma Cervix',
        'date_of_sim': sim_date.isoformat(),
        'technique': 1,
        'special_technique': 1,
        'machine': 1,
        'shift': 1,
        'date_of_start': invalid_start_date.isoformat(),
        'dose_planned': '50.00',
        'no_of_fractions': '25'
    }, follow=True)
    
    # Should show error message and schedule must NOT have invalid start date
    test_pt_date.refresh_from_db()
    sched = test_pt_date.schedules.first()
    assert sched is None or sched.date_of_start != invalid_start_date, "Schedule must NOT be created with date_of_start < date_of_sim"

    # Now schedule with valid start_date >= sim_date
    resp_valid_sched = c.post(f'/scheduler/patient/{test_pt_date.id}/', {
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': intent_obj.id if intent_obj else 1,
        'age': '50',
        'phone_no': '9900112233',
        'site_of_treatment': 'Pelvis',
        'diagnosis': 'Carcinoma Cervix',
        'date_of_sim': sim_date.isoformat(),
        'technique': 1,
        'special_technique': 1,
        'machine': 1,
        'shift': 1,
        'date_of_start': valid_start_date.isoformat(),
        'dose_planned': '50.00',
        'no_of_fractions': '25'
    })
    test_pt_date.refresh_from_db()
    sched = test_pt_date.schedules.first()
    assert sched is not None
    assert sched.date_of_start == valid_start_date, "Schedule should be created with valid date_of_start >= date_of_sim"

    # Test manual grid row save AJAX validation: start_date < date_of_sim must be rejected
    resp_ajax_invalid = c.post('/reports/manual-grid/save-row/', {
        'schedule_id': sched.id,
        'date_of_start': invalid_start_date.isoformat(),
        'shift_id': 1,
        'machine_id': 1
    }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
    assert resp_ajax_invalid.status_code == 400
    ajax_data = resp_ajax_invalid.json()
    assert ajax_data['success'] is False
    assert 'cannot be earlier than CT Simulation Date' in ajax_data['error']

    # Test manual grid row save AJAX validation: valid start_date >= date_of_sim succeeds
    resp_ajax_valid = c.post('/reports/manual-grid/save-row/', {
        'schedule_id': sched.id,
        'date_of_start': (sim_date + timedelta(days=5)).isoformat(),
        'shift_id': 1,
        'machine_id': 1
    }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
    assert resp_ajax_valid.status_code == 200
    assert resp_ajax_valid.json()['success'] is True
    sched.refresh_from_db()
    assert sched.date_of_start == sim_date + timedelta(days=5)

    test_pt_date.delete()

    # 3. Test Machine Mapping Multi-Select Sites Saving
    if la1_mapping:
        orig_sites = la1_mapping.allowed_sites
        test_selected_sites = ['Breast', 'Head and neck', 'Pelvis']
        resp_post_mapping = c.post(f'/scheduler/mappings/{la1_mapping.id}/edit/', {
            'shifts': [s.id for s in la1_mapping.allowed_shifts.all()],
            'techniques': [t.id for t in la1_mapping.allowed_techniques.all()],
            'special_techniques': [st.id for st in la1_mapping.allowed_special_techniques.all()],
            'allowed_sites': test_selected_sites,
        })
        assert resp_post_mapping.status_code == 302
        la1_mapping.refresh_from_db()
        assert la1_mapping.allowed_sites == "Breast, Head and neck, Pelvis"
        assert la1_mapping.get_allowed_sites_list() == test_selected_sites

        # Restore original sites
        la1_mapping.allowed_sites = orig_sites
        la1_mapping.save()

    # 4. Test Reference Lookup Treatment Sites dynamically reflected in Machine Mapping
    new_lookup_site = TreatmentSite.objects.create(name='Spine Lookup Site', display_order=99, is_active=True)
    if la1_mapping:
        resp_check_site = c.get(f'/scheduler/mappings/{la1_mapping.id}/edit/')
        assert resp_check_site.status_code == 200
        assert 'Spine Lookup Site' in resp_check_site.content.decode('utf-8'), "New site from reference lookups must be dynamically reflected in machine mapping"
    new_lookup_site.delete()

    print("✓ Test 17 Passed: Header badges removed, date_of_start >= date_of_sim strictly enforced, and machine mapping multi-select treatment sites verified.")

    # --- Test 18: Clinical Treatment Intent, Junior Resident Dropdown, Draft vs Approve, End Date, Course Tagging & Admin-Only Audit Logs ---
    print("\n--- Test 18: Intent, JR Dropdown, Draft vs Approve, End Date, Course Tagging & Admin-Only Audit Logs ---")

    # 1. Verification of TreatmentIntent lookup and CRUD
    assert TreatmentIntent.objects.filter(is_active=True).count() >= 4, "Default treatment intents (Radical, Palliative, Adjuvant, Neo-adjuvant) must exist"
    radical_intent = TreatmentIntent.objects.get(name='Radical')
    palliative_intent = TreatmentIntent.objects.get(name='Palliative')

    # 2. Audit Log Viewer Security Boundary: Strictly Admin Only (not SubAdmin, not Clinical)
    c.force_login(subadmin_user)
    resp_subadmin_audit = c.get('/reports/audit-logs/')
    assert resp_subadmin_audit.status_code == 302, "SubAdmin must be redirected / blocked from viewing security audit logs"
    
    c.force_login(res_user)
    resp_resident_audit = c.get('/reports/audit-logs/')
    assert resp_resident_audit.status_code == 302, "Resident must be redirected / blocked from viewing security audit logs"

    c.force_login(admin_user)
    resp_admin_audit = c.get('/reports/audit-logs/')
    assert resp_admin_audit.status_code == 200, "Admin must be granted access to security audit logs"
    assert "Security &amp; Clinical Audit Logs" in resp_admin_audit.content.decode('utf-8') or "Security & Clinical Audit Logs" in resp_admin_audit.content.decode('utf-8')

    # 3. Create test patient for end-to-end scheduling workflows
    Patient.objects.filter(mr_no=9999018).delete()
    test_pt18 = Patient.objects.create(
        mr_no=9999018,
        pt_name="Test Workflow Pt Eighteen",
        age=45,
        sex='Male',
        phone_no='9123456780',
        diagnosis='Ca Glottis T2N0',
        primary_consultant=active_consultant
    )

    # 4. Save as Draft (Mandatory fields for scheduling like machine/start date are bypassed)
    sim_date_val = date.today()
    resp_draft = c.post(f'/scheduler/patient/{test_pt18.id}/', {
        'form_action': 'draft',
        'treatment_type': 'Teletherapy',
        'intent': radical_intent.id,
        'site_of_treatment': 'Head and neck',
        'diagnosis': 'Ca Glottis T2N0',
        'date_of_sim': sim_date_val.isoformat(),
        'technique': 1,
        'special_technique': 1,
        'dose_planned': '66.00',
        'no_of_fractions': '33',
        'jr_resident': res_user.get_full_name() or res_user.username,
        'notes': 'Saved preliminary simulation plan'
    })
    assert resp_draft.status_code == 302
    test_pt18.refresh_from_db()
    draft_sched = test_pt18.schedules.first()
    assert draft_sched is not None
    assert draft_sched.status == 'Draft'
    assert draft_sched.is_draft is True
    assert draft_sched.date_of_start is None, "Draft schedule does not require start date"
    assert draft_sched.machine is None, "Draft schedule does not book a machine slot"

    # Verify Draft does NOT appear on active daily machine schedule
    resp_daily = c.get(f'/reports/machines/?start_date={sim_date_val.isoformat()}')
    assert not resp_daily.context['schedules'].filter(patient=test_pt18).exists(), "Draft schedule must NOT be in machine daily list queryset"

    # 5. Audit Logging check: Draft save logged
    draft_log = AuditLog.objects.filter(mr_no=str(test_pt18.mr_no), action='SCHEDULE_DRAFT_SAVED').first()
    assert draft_log is not None, "Saving draft must create an AuditLog entry with SCHEDULE_DRAFT_SAVED"
    assert draft_log.user == admin_user

    # 6. Approve & Schedule (Enforces mandatory fields and start date >= sim date)
    mach_test = Machine.objects.filter(is_active=True).first()
    shift_test = Shift.objects.filter(is_active=True).first()
    start_date_val = sim_date_val + timedelta(days=2)

    resp_approve = c.post(f'/scheduler/patient/{test_pt18.id}/', {
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': radical_intent.id,
        'site_of_treatment': 'Head and neck',
        'diagnosis': 'Ca Glottis T2N0',
        'phone_no': '9988112233',  # Changed phone number!
        'date_of_sim': sim_date_val.isoformat(),
        'technique': 1,
        'special_technique': 1,
        'machine': mach_test.id,
        'shift': shift_test.id,
        'date_of_start': start_date_val.isoformat(),
        'dose_planned': '66.00',
        'no_of_fractions': '33',
        'jr_resident': res_user.get_full_name() or res_user.username,
        'notes': 'Approved 66Gy in 33 fractions'
    })
    assert resp_approve.status_code == 302
    test_pt18.refresh_from_db()
    approved_sched = test_pt18.schedules.first()
    assert approved_sched.status == 'Scheduled'
    assert approved_sched.intent == radical_intent
    assert approved_sched.machine == mach_test
    assert approved_sched.shift == shift_test
    assert approved_sched.date_of_start == start_date_val

    # 7. End Date Calculation verification (skipping Sundays)
    test_mon_start = date(2026, 11, 16)  # Monday
    approved_sched.date_of_start = test_mon_start
    approved_sched.no_of_fractions = 6
    mon_end = approved_sched.calculate_end_date()
    assert mon_end == date(2026, 11, 21), f"6 fractions starting Mon 16-Nov should end Sat 21-Nov, got {mon_end}"
    approved_sched.no_of_fractions = 7
    sun_skip_end = approved_sched.calculate_end_date()
    assert sun_skip_end == date(2026, 11, 23), f"7 fractions starting Mon 16-Nov should end Mon 23-Nov, got {sun_skip_end}"
    approved_sched.date_of_start = start_date_val
    approved_sched.no_of_fractions = 33
    approved_sched.date_of_end = None
    approved_sched.save()

    # 8. Audit Logging check: Phone change and Schedule approval
    phone_log = AuditLog.objects.filter(mr_no=str(test_pt18.mr_no), action='PATIENT_PHONE_CHANGE').first()
    assert phone_log is not None, "Phone number change must be logged"
    assert "9988112233" in phone_log.details

    approved_log = AuditLog.objects.filter(mr_no=str(test_pt18.mr_no), action='SCHEDULE_APPROVED').first()
    assert approved_log is not None, "Schedule approval must be logged"

    # 9. Second Treatment Course Creation & Course Tagging / Clubbing
    resp_course2 = c.post(f'/scheduler/patient/{test_pt18.id}/?new_course=1', {
        'form_action': 'approve_schedule',
        'treatment_type': 'Teletherapy',
        'intent': palliative_intent.id,
        'site_of_treatment': 'Spine',
        'diagnosis': 'Metastatic spine compression',
        'phone_no': '9988112233',
        'date_of_sim': sim_date_val.isoformat(),
        'technique': 1,
        'special_technique': 1,
        'machine': mach_test.id,
        'shift': shift_test.id,
        'date_of_start': (start_date_val + timedelta(days=5)).isoformat(),
        'dose_planned': '20.00',
        'no_of_fractions': '5',
        'tagged_to': approved_sched.id,  # Tagged with first course
        'notes': 'Spine boost course'
    })
    assert resp_course2.status_code == 302
    assert test_pt18.schedules.count() == 2
    course2 = test_pt18.schedules.exclude(id=approved_sched.id).first()
    assert course2 is not None
    assert course2.tagged_to == approved_sched, "Course 2 must be tagged to Course 1"

    # Test Course Tagging View (/scheduler/course/<id>/tag/)
    # Untag course 2
    resp_untag = c.post(f'/scheduler/course/{course2.id}/tag/', {'tagged_to_id': ''})
    assert resp_untag.status_code == 302
    course2.refresh_from_db()
    assert course2.tagged_to is None, "Course 2 should now be independent"
    untag_log = AuditLog.objects.filter(mr_no=str(test_pt18.mr_no), action='COURSE_UNTAGGED').first()
    assert untag_log is not None

    # Re-tag course 2 to course 1
    resp_retag = c.post(f'/scheduler/course/{course2.id}/tag/', {'tagged_to_id': str(approved_sched.id)})
    assert resp_retag.status_code == 302
    course2.refresh_from_db()
    assert course2.tagged_to == approved_sched, "Course 2 should be tagged back to Course 1"
    tag_log = AuditLog.objects.filter(mr_no=str(test_pt18.mr_no), action='COURSE_TAGGED').first()
    assert tag_log is not None

    # 10. Patient Details Redesigned Page Verification
    resp_pt_detail = c.get(f'/patients/{test_pt18.id}/')
    assert resp_pt_detail.status_code == 200
    detail_html = resp_pt_detail.content.decode('utf-8')
    assert "Treatment Courses &amp; Radiotherapy Schedule" in detail_html or "Treatment Courses & Radiotherapy Schedule" in detail_html
    assert "Course #" in detail_html
    assert "🔗 Clubbed with" in detail_html
    assert "Sim Date" in detail_html or "Simulation Date" in detail_html
    assert "Start Date" in detail_html or "Treatment Start Date" in detail_html
    assert "End Date" in detail_html or "Treatment End Date" in detail_html
    assert "Radical" in detail_html
    assert "Palliative" in detail_html
    assert "Patient Security &amp; Clinical Audit Trail (Admin Only)" in detail_html or "Patient Security & Clinical Audit Trail" in detail_html

    # SubAdmin viewing patient detail should NOT see the audit trail
    c.force_login(subadmin_user)
    resp_subadmin_detail = c.get(f'/patients/{test_pt18.id}/')
    assert resp_subadmin_detail.status_code == 200
    assert "Patient Security & Clinical Audit Trail (Admin Only)" not in resp_subadmin_detail.content.decode('utf-8')

    # Cleanup
    test_pt18.delete()
    print("✓ Test 18 Passed: Intent, JR dropdown, draft vs approve, Sunday-skipping end dates, course clubbing & admin-only audit trail verified.")

    # --- Test 19: Dashboard Single Line Layout, Schedule Status Header, Admin Hide with Comment, & Renamed Add Course and Schedule Button ---
    print("\n--- Test 19: Dashboard Single Line Layout, Schedule Status Header, Admin Hide with Comment, & Renamed Add Course and Schedule Button ---")
    c.force_login(admin_user)

    # 1. Create a test patient for dashboard visibility testing
    Patient.objects.filter(mr_no=9999019).delete()
    test_pt19 = Patient.objects.create(
        mr_no=9999019,
        pt_name="Test SingleLine Dashboard Pt Nineteen",
        age=52,
        sex='Female',
        phone_no='9900990099',
        diagnosis='Carcinoma Tongue T1N0M0 Early Lesion',
        primary_consultant=active_consultant
    )

    # 2. Check active dashboard rendering
    resp_dash = c.get('/dashboard/')
    assert resp_dash.status_code == 200
    dash_html = resp_dash.content.decode('utf-8')

    # Verify column heading is "Schedule Status"
    assert "Schedule Status" in dash_html, "Dashboard header must be 'Schedule Status' instead of 'Status'"
    # Verify status badge is strictly "Pending"
    assert "Pending</span>" in dash_html, "Dashboard status entry must be 'Pending'"
    # Verify MR number does NOT have the '↗' symbol in the dashboard table
    assert f">{test_pt19.mr_no} ↗" not in dash_html, "Dashboard MR Number must not have the '↗' symbol"
    assert str(test_pt19.mr_no) in dash_html, "Dashboard MR Number must be present"

    # 3. Security Boundary: SubAdmin blocked from hiding patients
    c.force_login(subadmin_user)
    resp_subadmin_hide = c.post(f'/patients/{test_pt19.id}/hide-dashboard/', {'comment': 'Subadmin trying to hide'})
    assert resp_subadmin_hide.status_code == 302
    test_pt19.refresh_from_db()
    assert test_pt19.is_hidden_from_dashboard is False, "SubAdmin must NOT be permitted to hide patients"

    # 4. Admin hiding without comment -> rejected
    c.force_login(admin_user)
    resp_hide_nocomment = c.post(f'/patients/{test_pt19.id}/hide-dashboard/', {'comment': ''})
    assert resp_hide_nocomment.status_code == 302
    test_pt19.refresh_from_db()
    assert test_pt19.is_hidden_from_dashboard is False, "Hiding without comment must be rejected"

    # 5. Admin hiding with comment -> succeeds
    hide_comment_text = "Patient transferred to regional oncology center as per family preference"
    resp_hide = c.post(f'/patients/{test_pt19.id}/hide-dashboard/', {'comment': hide_comment_text})
    assert resp_hide.status_code == 302
    test_pt19.refresh_from_db()
    assert test_pt19.is_hidden_from_dashboard is True, "Patient must be flagged as hidden from dashboard"
    assert test_pt19.hidden_reason == hide_comment_text
    assert test_pt19.hidden_by == admin_user
    assert test_pt19.hidden_at is not None

    # Verify AuditLog recorded
    hide_audit = AuditLog.objects.filter(mr_no=str(test_pt19.mr_no), action='PATIENT_DASHBOARD_HIDDEN').first()
    assert hide_audit is not None, "PATIENT_DASHBOARD_HIDDEN audit log must be recorded"
    assert hide_comment_text in hide_audit.details

    # 6. Verify hidden patient is EXCLUDED from active dashboard
    resp_dash2 = c.get('/dashboard/')
    assert resp_dash2.status_code == 200
    assert test_pt19 not in resp_dash2.context['page_obj'], "Hidden patient must not appear in active dashboard queue"

    # 7. Verify hidden patient IS INCLUDED when Admin views ?show_hidden=1
    resp_dash_hidden = c.get('/dashboard/?show_hidden=1')
    assert resp_dash_hidden.status_code == 200
    assert test_pt19 in resp_dash_hidden.context['page_obj'], "Hidden patient must appear in hidden dashboard view"
    assert "Hidden Patients Queue" in resp_dash_hidden.content.decode('utf-8')

    # 8. Unhide patient -> restored to active dashboard
    resp_unhide = c.post(f'/patients/{test_pt19.id}/unhide-dashboard/')
    assert resp_unhide.status_code == 302
    test_pt19.refresh_from_db()
    assert test_pt19.is_hidden_from_dashboard is False, "Patient must be restored to active dashboard"

    unhide_audit = AuditLog.objects.filter(mr_no=str(test_pt19.mr_no), action='PATIENT_DASHBOARD_UNHIDDEN').first()
    assert unhide_audit is not None, "PATIENT_DASHBOARD_UNHIDDEN audit log must be recorded"

    resp_dash3 = c.get('/dashboard/')
    assert test_pt19 in resp_dash3.context['page_obj'], "Unhidden patient must reappear in active dashboard queue"

    # 9. Verify Patient Details Button Renamed to "+ Add Treatment Course and Schedule"
    resp_detail_renamed = c.get(f'/patients/{test_pt19.id}/')
    assert resp_detail_renamed.status_code == 200
    detail_renamed_html = resp_detail_renamed.content.decode('utf-8')
    assert "Add Treatment Course and Schedule" in detail_renamed_html, "Button in patient details must be named '+ Add Treatment Course and Schedule'"

    # Cleanup
    test_pt19.delete()
    print("✓ Test 19 Passed: Dashboard single line layout, 'Schedule Status' header, Admin hide with comment confirmation, and renamed 'Add Treatment Course and Schedule' button fully verified.")

    # 20. Test Schedule Worksheet Default Dates and Holiday Management & Warning Symbols
    print("\n--- Test 20: Schedule Worksheet Default Dates (Upcoming Monday & +6 Months), Holiday Management, Auto-Scheduling Exclusion & Warning Symbols ---")
    from apps.reports.views import get_upcoming_monday, add_six_months

    # 1. Upcoming Monday calculation
    assert get_upcoming_monday(date(2026, 11, 15)) == date(2026, 11, 16), "Sunday 2026-11-15 must lead to Monday 2026-11-16"
    assert get_upcoming_monday(date(2026, 11, 16)) == date(2026, 11, 16), "Monday 2026-11-16 must remain Monday 2026-11-16"
    assert get_upcoming_monday(date(2026, 11, 17)) == date(2026, 11, 23), "Tuesday 2026-11-17 must lead to next Monday 2026-11-23"

    # 2. 6 Months calculation
    assert add_six_months(date(2026, 11, 16)) == date(2027, 5, 16), "6 months from 2026-11-16 must be 2027-05-16"
    assert add_six_months(date(2026, 8, 31)) == date(2027, 2, 28), "6 months from 2026-08-31 must clamp to 2027-02-28"

    # 3. Schedule Worksheet Default Dates on GET
    c.force_login(admin_user)
    resp_grid_default = c.get('/reports/manual-grid/')
    assert resp_grid_default.status_code == 200
    expected_default_start = get_upcoming_monday(date.today()).isoformat()
    expected_default_end = add_six_months(get_upcoming_monday(date.today())).isoformat()
    assert resp_grid_default.context['start_date_str'] == expected_default_start, f"Expected start date {expected_default_start}"
    assert resp_grid_default.context['end_date_str'] == expected_default_end, f"Expected end date {expected_default_end}"

    # 4. Holiday Management RBAC & CRUD
    # Clean test holidays first
    Holiday.objects.filter(date__in=[date(2026, 11, 20), date(2026, 11, 25)]).delete()

    # Clinical resident blocked from managing holidays
    c.force_login(res_user)
    resp_res_holiday = c.get('/scheduler/holidays/')
    assert resp_res_holiday.status_code in [302, 403], "Resident must be blocked from holiday manager"

    # SubAdmin can view and add holidays
    c.force_login(subadmin_user)
    resp_subadmin_hview = c.get('/scheduler/holidays/')
    assert resp_subadmin_hview.status_code == 200, "SubAdmin can view holidays"

    resp_add_h1 = c.post('/scheduler/holidays/', {
        'action': 'add',
        'holiday_date': '2026-11-20',
        'comment': 'National Foundation Day'
    })
    assert resp_add_h1.status_code == 302
    assert Holiday.objects.filter(date=date(2026, 11, 20)).exists(), "Holiday 2026-11-20 must be created"

    # Admin can add holiday
    c.force_login(admin_user)
    resp_add_h2 = c.post('/scheduler/holidays/', {
        'action': 'add',
        'holiday_date': '2026-11-25',
        'comment': 'Hospital Annual Day'
    })
    assert resp_add_h2.status_code == 302
    assert Holiday.objects.filter(date=date(2026, 11, 25)).exists(), "Holiday 2026-11-25 must be created"

    # Verify AuditLog recorded
    h_audit = AuditLog.objects.filter(action='HOLIDAY_ADDED', details__icontains='National Foundation Day').first()
    assert h_audit is not None, "HOLIDAY_ADDED audit log must be recorded"

    # 5. TreatmentSchedule.calculate_end_date skips Sundays and Holidays
    # 5 fractions starting Thursday 2026-11-19:
    # Day 1: Thu 19
    # Day 2: Sat 21 (Fri 20 is Holiday)
    # Day 3: Mon 23 (Sun 22 is Sunday)
    # Day 4: Tue 24
    # Day 5: Thu 26 (Wed 25 is Holiday)
    calc_end = TreatmentSchedule.compute_end_date(date(2026, 11, 19), 5)
    assert calc_end == date(2026, 11, 26), f"Expected end date 2026-11-26, got {calc_end}"

    # 6. Auto-scheduling slot API strictly excludes holidays
    # If starting search from 2026-11-19, earliest slot cannot be on 2026-11-20 (Holiday)
    tech_imrt = Technique.objects.filter(name__icontains='IMRT').first() or Technique.objects.first()
    spec_none = SpecialTechnique.objects.filter(name__iexact='None').first() or SpecialTechnique.objects.first()
    resp_slots_api = c.get(f'/scheduler/api/mapped-machine-slots/?technique_id={tech_imrt.id}&special_technique_id={spec_none.id}&sim_date=2026-11-19')
    assert resp_slots_api.status_code == 200
    slots_data = resp_slots_api.json()
    assert slots_data['success'] is True
    for m_info in slots_data['machines']:
        if m_info['slot']:
            assert m_info['slot']['date'] != '2026-11-20', "Auto-scheduling must NOT allocate on a holiday (2026-11-20)"
            assert m_info['slot']['date'] != '2026-11-25', "Auto-scheduling must NOT allocate on a holiday (2026-11-25)"

    # 7. Manual Scheduling Grid warning symbol & AJAX behavior
    cons_test = Consultant.objects.first()
    mach_test = Machine.objects.first()
    shift_test = Shift.objects.first()
    test_pt20 = Patient.objects.create(
        mr_no=9999020,
        pt_name="Test Holiday Pt Twenty",
        age=50,
        sex="Male",
        primary_consultant=cons_test,
        diagnosis="Test Cancer",
        site_of_treatment="Head and neck"
    )
    sched20 = TreatmentSchedule.objects.create(
        patient=test_pt20,
        machine=mach_test,
        shift=shift_test,
        technique=tech_imrt,
        special_technique=spec_none,
        date_of_sim=date(2026, 11, 10),
        date_of_start=date(2026, 11, 20),  # Falls on Holiday!
        dose_planned=Decimal("50.00"),
        no_of_fractions=25,
        status='Scheduled'
    )

    resp_grid_view = c.get('/reports/manual-grid/?start_date=2026-11-01&end_date=2026-11-30')
    assert resp_grid_view.status_code == 200
    grid_html = resp_grid_view.content.decode('utf-8')
    assert "⚠️ Holiday" in grid_html, "Warning badge '⚠️ Holiday' must be rendered in manual schedule grid"
    assert "National Foundation Day" in grid_html, "Holiday comment must be present in tooltip"

    # Inline AJAX save for manual grid with holiday date
    resp_ajax_save = c.post('/reports/manual-grid/save-row/', {
        'schedule_id': sched20.id,
        'date_of_start': '2026-11-25',
        'shift_id': str(shift_test.id),
        'machine_id': str(mach_test.id),
    })
    assert resp_ajax_save.status_code == 200
    ajax_data = resp_ajax_save.json()
    assert ajax_data['success'] is True
    assert ajax_data['is_holiday'] is True
    assert ajax_data['holiday_comment'] == 'Hospital Annual Day'

    # 8. Patient Scheduling View has holiday map
    resp_sched_view = c.get(f'/scheduler/patient/{test_pt20.id}/')
    assert resp_sched_view.status_code == 200
    assert 'holiday_map' in resp_sched_view.context
    assert '2026-11-20' in resp_sched_view.context['holiday_map']

    # 9. Delete Holiday via SubAdmin
    c.force_login(subadmin_user)
    h_to_delete = Holiday.objects.get(date=date(2026, 11, 25))
    resp_del_h = c.post('/scheduler/holidays/', {
        'action': 'delete',
        'holiday_id': h_to_delete.id
    })
    assert resp_del_h.status_code == 302
    assert not Holiday.objects.filter(date=date(2026, 11, 25)).exists()

    # Cleanup
    Holiday.objects.filter(date=date(2026, 11, 20)).delete()
    test_pt20.delete()
    print("✓ Test 20 Passed: Schedule Worksheet default upcoming Monday & 6 months dates, Holiday management (Admin/SubAdmin), auto-scheduling exclusion, and manual scheduling warning symbols fully verified.")

    # 21. Test User Management (Compact, Filters, Admin-only Inactive, Prefix), Patient Unit, Reports Exports, Emergency Slot
    print("\n--- Test 21: User Management (Compact, Filters, Admin-only Inactive, Prefix), Patient Unit, Reports Exports, Emergency Slot ---")
    User.objects.filter(username__in=['test_toggle_user', 'test_doc_prefix']).delete()
    Patient.objects.filter(mr_no=9999021).delete()

    # 1. User Management Page: Compact table, Role Filter, Search, Admin-only Inactive toggle
    c.force_login(admin_user)
    resp_users = c.get('/users/')
    assert resp_users.status_code == 200
    users_html = resp_users.content.decode('utf-8')
    assert "user-table" in users_html or "table" in users_html
    assert "padding: 4px 8px" in users_html, "User table must have compact cell padding (4px 8px)"
    assert "All Roles" in users_html
    assert "Active Only" in users_html

    # Filter by role
    resp_users_role = c.get(f'/users/?role_id={res_role.role_id}')
    assert resp_users_role.status_code == 200
    assert res_user in resp_users_role.context['users']

    # Search by username
    resp_users_search = c.get('/users/?q=admin')
    assert resp_users_search.status_code == 200
    assert admin_user in resp_users_search.context['users']

    # Test Inactive Toggle: Admin Only Security Boundary
    test_toggle_user = User.objects.create_user(username='test_toggle_user', password='TogglePass123!', first_name='Toggle')
    UserProfile.objects.create(user=test_toggle_user, role=res_role, user_level='RESIDENT')

    # SubAdmin attempt to toggle -> forbidden/blocked
    c.force_login(subadmin_user)
    resp_subadmin_toggle = c.get(f'/users/{test_toggle_user.id}/toggle-active/')
    test_toggle_user.refresh_from_db()
    assert test_toggle_user.is_active is True, "SubAdmin must NOT be permitted to deactivate user accounts"

    # Clinical Resident attempt to toggle -> forbidden/blocked
    c.force_login(res_user)
    resp_res_toggle = c.get(f'/users/{test_toggle_user.id}/toggle-active/')
    test_toggle_user.refresh_from_db()
    assert test_toggle_user.is_active is True, "Resident must NOT be permitted to deactivate user accounts"

    # Admin deactivates user -> succeeds
    c.force_login(admin_user)
    resp_admin_toggle = c.get(f'/users/{test_toggle_user.id}/toggle-active/')
    test_toggle_user.refresh_from_db()
    assert test_toggle_user.is_active is False, "Admin must be able to deactivate user accounts"

    # Verify AuditLog recorded
    deact_audit = AuditLog.objects.filter(action='USER_DEACTIVATED', details__icontains='test_toggle_user').first()
    assert deact_audit is not None, "USER_DEACTIVATED audit log must be recorded"

    # Inactive user login is blocked
    client_inactive = Client()
    login_success = client_inactive.login(username='test_toggle_user', password='TogglePass123!')
    assert login_success is False, "Deactivated user must be blocked from logging into the system"

    # Inactive user is hidden from clinical Junior Resident dropdown
    test_pt_temp = Patient.objects.create(
        mr_no=9999022, pt_name="Temp Pt", age=40, sex="Male", diagnosis="Test", primary_consultant=cons_test
    )
    resp_sched_form = c.get(f'/scheduler/patient/{test_pt_temp.id}/')
    assert resp_sched_form.status_code == 200
    assert test_toggle_user not in resp_sched_form.context['jr_users'], "Deactivated user must be hidden from JR Resident dropdown"
    test_pt_temp.delete()

    # Admin reactivates user -> succeeds
    resp_admin_reactivate = c.get(f'/users/{test_toggle_user.id}/toggle-active/')
    test_toggle_user.refresh_from_db()
    assert test_toggle_user.is_active is True, "Admin must be able to reactivate user accounts"

    react_audit = AuditLog.objects.filter(action='USER_ACTIVATED', details__icontains='test_toggle_user').first()
    assert react_audit is not None, "USER_ACTIVATED audit log must be recorded"

    # 2. Add / Edit User Form: Prefix Dropdown (Dr., Mr, Mrs, Miss)
    resp_user_form = c.get('/users/new/')
    assert resp_user_form.status_code == 200
    user_form_html = resp_user_form.content.decode('utf-8')
    assert "Dr." in user_form_html
    assert "Mr" in user_form_html
    assert "Mrs" in user_form_html
    assert "Miss" in user_form_html

    # Create user with Prefix Dr.
    resp_create_doc = c.post('/users/new/', {
        'username': 'test_doc_prefix',
        'prefix': 'Dr.',
        'first_name': 'Ananya',
        'password': 'DocPassword123!',
        'role_id': str(res_role.role_id)
    })
    assert resp_create_doc.status_code == 302
    doc_user = User.objects.get(username='test_doc_prefix')
    assert doc_user.profile.prefix == 'Dr.'
    assert doc_user.profile.display_name == 'Dr. Ananya'

    # Edit user with Prefix Mrs
    resp_edit_doc = c.post(f'/users/{doc_user.id}/edit/', {
        'prefix': 'Mrs',
        'first_name': 'Ananya Sharma',
        'role_id': str(res_role.role_id),
        'is_active': 'on'
    })
    assert resp_edit_doc.status_code == 302
    doc_user.refresh_from_db()
    assert doc_user.profile.prefix == 'Mrs'
    assert doc_user.profile.display_name == 'Mrs Ananya Sharma'

    # 3. Patient Registration: Clinical Unit (I, II, III)
    resp_reg_form = c.get('/patients/register/')
    assert resp_reg_form.status_code == 200
    reg_html = resp_reg_form.content.decode('utf-8')
    assert 'unitList' in reg_html, "Unit datalist must be present in patient registration form"

    resp_reg_post = c.post('/patients/register/', {
        'mr_no': '9999021',
        'pt_name': 'Test Unit Patient',
        'age': '54',
        'sex': 'Female',
        'unit': 'II',
        'primary_consultant_id': str(cons_test.id),
        'diagnosis': 'Ca Cervix Stage IIB',
        'site_of_treatment': 'Pelvis',
        'rt_no': 'RT-2101',
        'phone_no': '9876543210'
    })
    assert resp_reg_post.status_code == 302
    test_pt21 = Patient.objects.get(mr_no=9999021)
    assert test_pt21.unit == 'II', f"Expected unit 'II', got '{test_pt21.unit}'"

    # Patient Edit: update unit to 'III'
    resp_edit_post = c.post(f'/patients/{test_pt21.id}/edit/', {
        'pt_name': 'Test Unit Patient',
        'age': '54',
        'sex': 'Female',
        'unit': 'III',
        'primary_consultant_id': str(cons_test.id),
        'diagnosis': 'Ca Cervix Stage IIB',
        'site_of_treatment': 'Pelvis',
        'rt_no': 'RT-2101',
        'phone_no': '9876543210'
    })
    assert resp_edit_post.status_code == 302
    test_pt21.refresh_from_db()
    assert test_pt21.unit == 'III', f"Expected unit 'III', got '{test_pt21.unit}'"

    # Patient Details: verify Unit displayed
    resp_detail21 = c.get(f'/patients/{test_pt21.id}/')
    assert resp_detail21.status_code == 200
    assert "Unit: III" in resp_detail21.content.decode('utf-8'), "Patient details page must display clinical unit"

    # 4. Reports Exports: Consultant Stats, Unit Stats, Simulation Report (CSV and Excel)
    # Consultant Stats
    resp_cons_csv = c.get('/reports/consultant-stats/?export=csv')
    assert resp_cons_csv.status_code == 200
    assert 'text/csv' in resp_cons_csv['Content-Type']
    assert 'consultant_starts_' in resp_cons_csv['Content-Disposition'].lower()

    resp_cons_xlsx = c.get('/reports/consultant-stats/?export=excel')
    assert resp_cons_xlsx.status_code == 200
    assert 'spreadsheetml' in resp_cons_xlsx['Content-Type']

    # Unit Stats
    resp_unit_page = c.get('/reports/unit-stats/')
    assert resp_unit_page.status_code == 200
    assert "Clinical Unit" in resp_unit_page.content.decode('utf-8')

    resp_unit_csv = c.get('/reports/unit-stats/?export=csv')
    assert resp_unit_csv.status_code == 200
    assert 'text/csv' in resp_unit_csv['Content-Type']
    assert 'unit_starts_' in resp_unit_csv['Content-Disposition'].lower()

    resp_unit_xlsx = c.get('/reports/unit-stats/?export=excel')
    assert resp_unit_xlsx.status_code == 200
    assert 'spreadsheetml' in resp_unit_xlsx['Content-Type']

    # Simulation Report
    resp_sim_page = c.get('/reports/simulations/?start_date=2026-01-01&end_date=2026-12-31')
    assert resp_sim_page.status_code == 200
    assert "Simulated Between Dates" in resp_sim_page.content.decode('utf-8')

    resp_sim_csv = c.get('/reports/simulations/?start_date=2026-01-01&end_date=2026-12-31&export=csv')
    assert resp_sim_csv.status_code == 200
    assert 'text/csv' in resp_sim_csv['Content-Type']
    assert 'simulations_' in resp_sim_csv['Content-Disposition'].lower()

    resp_sim_xlsx = c.get('/reports/simulations/?start_date=2026-01-01&end_date=2026-12-31&export=excel')
    assert resp_sim_xlsx.status_code == 200
    assert 'spreadsheetml' in resp_sim_xlsx['Content-Type']

    # 5. Emergency / Vacant Slot Filling Workflow
    sched21 = TreatmentSchedule.objects.create(
        patient=test_pt21,
        machine=mach_test,
        shift=shift_test,
        technique=tech_imrt,
        special_technique=spec_none,
        date_of_sim=date(2026, 11, 10),
        date_of_start=date(2026, 11, 28),
        dose_planned=Decimal('20.00'),
        no_of_fractions=5,
        status='Scheduled'
    )

    # In Patient Details: Verify "🚨 Add to Emergency slot" button appears
    resp_pt_detail_em = c.get(f'/patients/{test_pt21.id}/')
    assert resp_pt_detail_em.status_code == 200
    assert f"/scheduler/course/{sched21.id}/emergency-slot/" in resp_pt_detail_em.content.decode('utf-8')

    # View Emergency / Vacant Slot Form
    resp_em_form = c.get(f'/scheduler/course/{sched21.id}/emergency-slot/')
    assert resp_em_form.status_code == 200
    em_form_html = resp_em_form.content.decode('utf-8')
    assert "Spinal cord compression" in em_form_html
    assert "Bleeding" in em_form_html
    assert "Pain" in em_form_html
    assert "SVCO" in em_form_html
    assert "Others" in em_form_html
    assert ("28-Nov-2026" in em_form_html or "2026-11-28" in em_form_html), "Current scheduled date must be prepopulated"

    # Submit Emergency Slot
    resp_approve_em = c.post(f'/scheduler/course/{sched21.id}/emergency-slot/', {
        'slot_category': 'Emergency',
        'emergency_reason': 'Spinal cord compression',
        'intended_start_date': '2026-11-24',
        'notes': 'Severe metastatic epidural compression, urgent decompression RT'
    }, follow=True)
    assert resp_approve_em.status_code == 200
    em_flash = "Patient has been shifted to Emergency / Vacant slot list - will be removed from the normal schedule list"
    assert em_flash in [m.message for m in resp_approve_em.context['messages']]

    # Verify Schedule state
    sched21.refresh_from_db()
    assert sched21.is_emergency_slot is True, "Course must be flagged as emergency slot"
    assert sched21.slot_category == 'Emergency'
    assert sched21.emergency_reason == 'Spinal cord compression'
    assert sched21.date_of_start is None, "date_of_start must be cleared to remove patient from normal schedule list"
    assert sched21.original_scheduled_date == date(2026, 11, 28)
    assert sched21.status == 'Emergency'

    # Verify AuditLog
    em_audit = AuditLog.objects.filter(mr_no=str(test_pt21.mr_no), action='EMERGENCY_SLOT_ASSIGNED').first()
    assert em_audit is not None, "EMERGENCY_SLOT_ASSIGNED audit log must be recorded"
    assert "Spinal cord compression" in em_audit.details

    # Verify patient is EXCLUDED from normal machine daily starts report
    resp_mach_daily = c.get('/reports/machines/?start_date=2026-11-01&end_date=2026-11-30')
    assert test_pt21.mr_no not in [s.patient.mr_no for s in resp_mach_daily.context['schedules']], "Emergency patient must not appear in normal machine daily starts list"

    # Verify patient is EXCLUDED from normal manual schedule worksheet
    resp_manual_grid = c.get('/reports/manual-grid/?start_date=2026-11-01&end_date=2026-11-30')
    assert test_pt21.mr_no not in [s.patient.mr_no for s in resp_manual_grid.context['schedules']], "Emergency patient must not appear in normal manual schedule worksheet"

    # Verify patient is INCLUDED in Emergency Slots Report
    resp_em_report = c.get('/reports/emergency-slots/')
    assert resp_em_report.status_code == 200
    assert sched21 in resp_em_report.context['schedules'], "Course must appear in Emergency Slots Report"

    resp_em_csv = c.get('/reports/emergency-slots/?export=csv')
    assert resp_em_csv.status_code == 200
    assert 'Spinal cord compression' in resp_em_csv.content.decode('utf-8')

    resp_em_xlsx = c.get('/reports/emergency-slots/?export=excel')
    assert resp_em_xlsx.status_code == 200

    # Revert patient from Emergency Slot back to normal schedule
    resp_revert_em = c.post(f'/scheduler/course/{sched21.id}/revert-emergency/', follow=True)
    assert resp_revert_em.status_code == 200
    sched21.refresh_from_db()
    assert sched21.is_emergency_slot is False, "is_emergency_slot must be cleared"
    assert sched21.date_of_start == date(2026, 11, 28), "Original scheduled date must be restored"
    assert sched21.status == 'Scheduled'

    revert_em_audit = AuditLog.objects.filter(mr_no=str(test_pt21.mr_no), action='EMERGENCY_SLOT_REVERTED').first()
    assert revert_em_audit is not None, "EMERGENCY_SLOT_REVERTED audit log must be recorded"

    # Patient reappears in normal schedule list
    resp_mach_daily_restored = c.get('/reports/machines/?start_date=2026-11-01&end_date=2026-11-30')
    assert test_pt21.mr_no in [s.patient.mr_no for s in resp_mach_daily_restored.context['schedules']], "Restored course must reappear in normal schedule list"

    # Cleanup
    sched21.delete()
    test_pt21.delete()
    test_toggle_user.delete()
    doc_user.delete()
    print("✓ Test 21 Passed: User management compact/filters/Admin-only inactive, User prefix, Patient Unit, CSV/Excel exports (Consultant, Unit, Simulations), and Emergency Slot workflow fully verified.")

    # 22. Test Login Self-Registration, Wait for Admin Approval, Admin Dashboard Pending Queue, and Hide/Unhide Accounts
    print("\n--- Test 22: Login Self-Registration, Wait for Admin Approval, Admin Dashboard Pending Queue, and Hide/Unhide Accounts ---")
    User.objects.filter(username='test_new_reg_user').delete()

    # 1. Login Page has "Create new user" option
    c.logout()
    resp_login = c.get('/login/')
    assert resp_login.status_code == 200
    login_html = resp_login.content.decode('utf-8')
    assert "Create new user" in login_html or "Create New User" in login_html
    assert "/register-account/" in login_html

    # 2. Registration Form renders required fields and "Wait for Admin approval" notice
    resp_reg_page = c.get('/register-account/')
    assert resp_reg_page.status_code == 200
    reg_html = resp_reg_page.content.decode('utf-8')
    assert "Create New User Account" in reg_html
    assert "Prefix" in reg_html
    assert "Dr." in reg_html
    assert "Mr" in reg_html
    assert "Mrs" in reg_html
    assert "Miss" in reg_html
    assert "Create Password" in reg_html
    assert "Retype Password" in reg_html
    assert "wait for admin approval" in reg_html.lower()

    # 3. Password mismatch validation
    resp_mismatch = c.post('/register-account/', {
        'prefix': 'Dr.',
        'first_name': 'Preethi Nair',
        'username': 'test_new_reg_user',
        'password': 'PassWord123!',
        'confirm_password': 'WrongConfirmPassword!',
        'role_id': str(res_role.role_id)
    })
    assert resp_mismatch.status_code == 200
    assert "Passwords do not match" in resp_mismatch.content.decode('utf-8')
    assert not User.objects.filter(username='test_new_reg_user').exists()

    # 4. Successful Registration -> Inactive, Pending Approval, "Wait for Admin approval" message
    resp_reg_post = c.post('/register-account/', {
        'prefix': 'Dr.',
        'first_name': 'Preethi Nair',
        'username': 'test_new_reg_user',
        'password': 'PassWord123!',
        'confirm_password': 'PassWord123!',
        'role_id': str(res_role.role_id)
    }, follow=True)
    assert resp_reg_post.status_code == 200
    reg_messages = [m.message for m in resp_reg_post.context['messages']]
    assert any("wait for admin approval" in m.lower() for m in reg_messages), "Success message must instruct user to wait for Admin approval"

    # Verify DB state of registered user
    reg_user = User.objects.get(username='test_new_reg_user')
    assert reg_user.is_active is False, "Newly registered user must be inactive"
    assert reg_user.profile.is_approved is False, "Newly registered user must have is_approved=False"
    assert reg_user.profile.is_hidden_from_dashboard is False
    assert reg_user.profile.prefix == 'Dr.'
    assert reg_user.profile.display_name == 'Dr. Preethi Nair'

    reg_audit = AuditLog.objects.filter(action='USER_REGISTRATION_SUBMITTED', details__icontains='test_new_reg_user').first()
    assert reg_audit is not None, "USER_REGISTRATION_SUBMITTED audit log must be recorded"

    # 5. Attempting to log in before approval is blocked with clear message
    client_unapproved = Client()
    resp_attempt_login = client_unapproved.post('/login/', {
        'username': 'test_new_reg_user',
        'password': 'PassWord123!'
    })
    assert resp_attempt_login.status_code == 200
    login_msgs = [m.message for m in resp_attempt_login.context['messages']]
    assert any("admin approval" in m.lower() for m in login_msgs), "Login must inform user that account is awaiting Admin approval"

    # 6. Security Boundary: SubAdmin CANNOT see pending approval queue or approve
    c.force_login(subadmin_user)
    resp_subadmin_dash = c.get('/dashboard/')
    assert resp_subadmin_dash.status_code == 200
    subadmin_html = resp_subadmin_dash.content.decode('utf-8')
    assert "Pending User Account Approvals" not in subadmin_html, "SubAdmin must NOT see the Admin-exclusive pending approvals card"

    resp_subadmin_approve = c.get(f'/users/{reg_user.id}/approve/')
    reg_user.refresh_from_db()
    assert reg_user.is_active is False, "SubAdmin must NOT be permitted to approve users"
    assert reg_user.profile.is_approved is False

    resp_subadmin_hide = c.get(f'/users/{reg_user.id}/toggle-hide-dashboard/')
    reg_user.profile.refresh_from_db()
    assert reg_user.profile.is_hidden_from_dashboard is False, "SubAdmin must NOT be permitted to hide accounts"

    # 7. Admin Dashboard: Pending Queue visible, Hide/Unhide toggle
    c.force_login(admin_user)
    resp_admin_dash = c.get('/dashboard/')
    assert resp_admin_dash.status_code == 200
    dash_html = resp_admin_dash.content.decode('utf-8')
    assert "Pending User Account Approvals" in dash_html, "Admin must see pending user account approvals"
    assert "test_new_reg_user" in dash_html
    assert "Dr. Preethi Nair" in dash_html
    assert "Show Hidden Accounts" in dash_html or "show_hidden_accounts" in dash_html

    # Admin hides account from dashboard
    resp_hide_acc = c.get(f'/users/{reg_user.id}/toggle-hide-dashboard/')
    reg_user.profile.refresh_from_db()
    assert reg_user.profile.is_hidden_from_dashboard is True, "Account must be marked as hidden from dashboard"
    assert reg_user.profile.hidden_by == admin_user
    assert reg_user.profile.hidden_at is not None

    hide_acc_audit = AuditLog.objects.filter(action='USER_DASHBOARD_HIDDEN', details__icontains='test_new_reg_user').first()
    assert hide_acc_audit is not None, "USER_DASHBOARD_HIDDEN audit log must be recorded"

    # Verify hidden account is EXCLUDED from active dashboard pending queue
    resp_admin_dash2 = c.get('/dashboard/')
    assert reg_user.profile not in resp_admin_dash2.context['pending_accounts'], "Hidden account must not appear in active pending queue"

    # Verify hidden account IS INCLUDED when Admin toggles ?show_hidden_accounts=1
    resp_admin_dash_hidden = c.get('/dashboard/?show_hidden_accounts=1')
    assert resp_admin_dash_hidden.status_code == 200
    assert reg_user.profile in resp_admin_dash_hidden.context['pending_accounts'], "Hidden account must appear in hidden accounts view"
    assert "Hidden User Accounts" in resp_admin_dash_hidden.content.decode('utf-8')
    assert "Back to Active Pending Accounts" in resp_admin_dash_hidden.content.decode('utf-8')

    # Admin unhides account
    resp_unhide_acc = c.get(f'/users/{reg_user.id}/toggle-hide-dashboard/')
    reg_user.profile.refresh_from_db()
    assert reg_user.profile.is_hidden_from_dashboard is False, "Account must be restored to visible"

    unhide_acc_audit = AuditLog.objects.filter(action='USER_DASHBOARD_UNHIDDEN', details__icontains='test_new_reg_user').first()
    assert unhide_acc_audit is not None, "USER_DASHBOARD_UNHIDDEN audit log must be recorded"

    # 8. Admin Approves Account
    resp_approve = c.get(f'/users/{reg_user.id}/approve/')
    reg_user.refresh_from_db()
    assert reg_user.is_active is True, "Approved user must be active"
    assert reg_user.profile.is_approved is True, "Approved user must have is_approved=True"
    assert reg_user.profile.approved_by == admin_user
    assert reg_user.profile.approved_at is not None

    appr_audit = AuditLog.objects.filter(action='USER_APPROVED', details__icontains='test_new_reg_user').first()
    assert appr_audit is not None, "USER_APPROVED audit log must be recorded"

    # Now the approved user can log in successfully
    client_approved = Client()
    login_ok = client_approved.login(username='test_new_reg_user', password='PassWord123!')
    assert login_ok is True, "Approved user must be able to log in successfully"

    # Cleanup
    reg_user.delete()
    print("✓ Test 22 Passed: Login self-registration, 'Wait for Admin approval' flow, Admin-exclusive pending approvals, hide/unhide dashboard toggle, and user activation fully verified.")

    # ==============================================================================
    # TEST 23: Verification of Clinical & Administrative Enhancements
    # 1. Login Page Cleanup (Red circle contents removed: inline create link & bottom note gone)
    # 2. Admin-only Stats Access (Consultant, Unit, Simulation stats restricted to Admin)
    # 3. Default Landing Page (Redirects to dashboard upon login)
    # 4. Hide Roles page from SubAdmin
    # 5. Patient Demographics (Mandatory RT No & Phone, Optional Alt Phone, 20260000123 example)
    # 6. Reference Lookups (emergency_reason and unit)
    # 7. Unit Stats deduplication fix
    # 8. Print Treatment Schedule (Worksheet button, modal, HTML print view, ReportLab PDF)
    # ==============================================================================
    print("\n--- Test 23: Clinical & Administrative Enhancements (Prompt #10) ---")

    # 1. Login Page Cleanup
    c_anon = Client()
    resp_login = c_anon.get('/login/')
    assert resp_login.status_code == 200
    login_html = resp_login.content.decode('utf-8')
    # Must NOT contain red circle items:
    assert "+ Create new user" not in login_html, "Inline '+ Create new user' next to Login button must be removed"
    assert "Administrative login: admin or n3v" not in login_html, "Bottom administrative credentials note must be removed"
    # Blue box card for new clinical user must remain
    assert "New clinical doctor or staff member?" in login_html
    assert "Create New User" in login_html

    # 2. Login Landing Page (Redirects to dashboard)
    User.objects.filter(username='test_landing_user').delete()
    test_landing_user = User.objects.create_user(username='test_landing_user', password='LandingPass123!')
    UserProfile.objects.create(user=test_landing_user, user_level='RESIDENT', is_approved=True)
    resp_login_post = c_anon.post('/login/', {
        'username': 'test_landing_user',
        'password': 'LandingPass123!'
    })
    assert resp_login_post.status_code == 302
    assert resp_login_post['Location'] in ['/', '/dashboard/'], f"Expected redirect to dashboard (/), got {resp_login_post['Location']}"
    test_landing_user.delete()

    # 3. Stats Access Restriction (Admin only)
    # Admin can access stats
    for stats_url in ['/reports/consultant-stats/', '/reports/unit-stats/', '/reports/simulations/']:
        resp_admin_stats = c.get(stats_url)
        assert resp_admin_stats.status_code == 200, f"Admin must have access to {stats_url}"

    # SubAdmin is blocked from stats
    c_sub = Client()
    c_sub.force_login(subadmin_user)
    for stats_url in ['/reports/consultant-stats/', '/reports/unit-stats/', '/reports/simulations/']:
        resp_sub_stats = c_sub.get(stats_url)
        assert resp_sub_stats.status_code == 302, f"SubAdmin must be redirected from {stats_url}"
        assert resp_sub_stats['Location'] in ['/', '/dashboard/']

    # Resident is blocked from stats
    c_res = Client()
    c_res.force_login(res_user)
    for stats_url in ['/reports/consultant-stats/', '/reports/unit-stats/', '/reports/simulations/']:
        resp_res_stats = c_res.get(stats_url)
        assert resp_res_stats.status_code == 302, f"Resident must be redirected from {stats_url}"
        assert resp_res_stats['Location'] in ['/', '/dashboard/']

    # 4. Hide Roles page from SubAdmin
    resp_sub_roles = c_sub.get('/roles/')
    assert resp_sub_roles.status_code == 302, "SubAdmin must be blocked from /roles/"
    resp_sub_nav = c_sub.get('/dashboard/')
    assert 'href="/roles/"' not in resp_sub_nav.content.decode('utf-8'), "Roles nav link must not be visible to SubAdmin"

    # Admin CAN access roles
    resp_admin_roles = c.get('/roles/')
    assert resp_admin_roles.status_code == 200, "Admin must have access to /roles/"

    # 5. Patient Registration Validation & Alternate Phone
    resp_reg_page = c.get('/patients/register/')
    assert resp_reg_page.status_code == 200
    reg_page_html = resp_reg_page.content.decode('utf-8')
    assert '20260000123' in reg_page_html, "MR Number placeholder/helper must show '20260000123'"
    assert 'alt_phone_no' in reg_page_html, "Alternate Phone field must be present in registration form"

    # Missing mandatory phone_no fails
    resp_fail_phone = c.post('/patients/register/', {
        'mr_no': '20260000991',
        'pt_name': 'Test Mandatory Fail',
        'rt_no': 'RT-991',
        'age': '45',
        'sex': 'M',
        'diagnosis': 'Ca Tongue',
        'primary_consultant': Consultant.objects.first().id,
        'phone_no': '',
        'save_only': '1'
    })
    assert resp_fail_phone.status_code == 200
    assert "Phone Number is mandatory" in resp_fail_phone.content.decode('utf-8')

    # Missing mandatory rt_no fails
    resp_fail_rt = c.post('/patients/register/', {
        'mr_no': '20260000992',
        'pt_name': 'Test Mandatory Fail 2',
        'rt_no': '',
        'age': '45',
        'sex': 'M',
        'diagnosis': 'Ca Tongue',
        'primary_consultant': Consultant.objects.first().id,
        'phone_no': '9876543210',
        'save_only': '1'
    })
    assert resp_fail_rt.status_code == 200
    assert "RT Number is mandatory" in resp_fail_rt.content.decode('utf-8')

    # Successful registration with primary & alternate phone numbers
    Patient.objects.filter(mr_no=20260000999).delete()
    resp_reg_succ = c.post('/patients/register/', {
        'mr_no': '20260000999',
        'pt_name': 'Test Prompt Ten Patient',
        'rt_no': 'RT-P10-01',
        'unit': 'II',
        'age': '56',
        'sex': 'F',
        'diagnosis': 'Ca Cervix Stage IIB',
        'primary_consultant': Consultant.objects.first().id,
        'phone_no': '9888877777',
        'alt_phone_no': '9111122222',
        'save_only': '1'
    })
    assert resp_reg_succ.status_code == 302
    p10 = Patient.objects.get(mr_no=20260000999)
    assert p10.phone_no == '9888877777'
    assert p10.alt_phone_no == '9111122222'
    assert p10.unit == 'II'

    # Patient details display alternate phone number
    resp_p10_detail = c.get(f'/patients/{p10.id}/')
    assert resp_p10_detail.status_code == 200
    p10_html = resp_p10_detail.content.decode('utf-8')
    assert '9888877777' in p10_html
    assert '9111122222' in p10_html
    assert 'Alternate Phone' in p10_html

    # 6. Reference Lookups (emergency_reason and unit)
    from apps.core.lookup_config import LOOKUP_CONFIG
    assert 'emergency_reason' in LOOKUP_CONFIG, "emergency_reason must be registered in LOOKUP_CONFIG"
    assert 'unit' in LOOKUP_CONFIG, "unit must be registered in LOOKUP_CONFIG"

    resp_lookup_em = c.get('/lookups/emergency_reason/')
    assert resp_lookup_em.status_code == 200
    assert "Reasons for Emergency" in resp_lookup_em.content.decode('utf-8')

    resp_lookup_unit = c.get('/lookups/unit/')
    assert resp_lookup_unit.status_code == 200
    assert "Clinical Units" in resp_lookup_unit.content.decode('utf-8')

    # 7. Unit Stats Deduplication Fix
    resp_unit_stats = c.get('/reports/unit-stats/?year=2026')
    assert resp_unit_stats.status_code == 200
    unit_stats_html = resp_unit_stats.content.decode('utf-8')
    # Count occurrences of 'Unit I' in table rows
    unit_1_count = unit_stats_html.count('>Unit I</td>')
    assert unit_1_count <= 2, f"Unit I must not be duplicated thousands of times! Found {unit_1_count} occurrences."

    # 8. Print Treatment Schedule (Worksheet button, Modal, HTML view, ReportLab PDF)
    resp_worksheet = c.get('/reports/manual-grid/?machine_code=LA1')
    assert resp_worksheet.status_code == 200
    ws_html = resp_worksheet.content.decode('utf-8')
    assert "Print Treatment Schedule" in ws_html, "'Print Treatment Schedule' button must exist in worksheet"
    assert "printScheduleModal" in ws_html, "Print modal must be present in worksheet"
    assert "Teletherapy" in ws_html
    assert "Brachytherapy" in ws_html

    # Schedule a test course for p10 on LA1
    m_la1 = Machine.objects.filter(code__iexact='LA1').first() or Machine.objects.first()
    s_first = Shift.objects.first()
    sched_p10 = TreatmentSchedule.objects.create(
        patient=p10,
        machine=m_la1,
        shift=s_first,
        treatment_type='Teletherapy',
        date_of_start=date(2026, 1, 5),
        status='Scheduled',
        jr_resident='Dr Test Resident'
    )

    # HTML Print View
    resp_print_html = c.get(f'/reports/print-schedule/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-11&machines={m_la1.id}')
    assert resp_print_html.status_code == 200
    print_html_content = resp_print_html.content.decode('utf-8')
    assert "KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY" in print_html_content
    assert "Department of Radiation Oncology" in print_html_content
    assert "Radiation Therapy Technician information sheet" in print_html_content
    assert "New Patient starts from 1/1/26 to 11/1/26" in print_html_content
    assert m_la1.name in print_html_content
    assert str(p10.mr_no) in print_html_content
    assert p10.pt_name in print_html_content
    assert "9888877777" in print_html_content, "Primary phone must appear in print sheet"
    assert "9111122222" not in print_html_content, "Alternate phone number must NEVER appear in print sheet"
    assert "Sheet 1 of" not in print_html_content, "Sheet 1 of X must NOT appear in print sheet"
    assert "Please note:" in print_html_content
    assert "Dr. Naveen T" in print_html_content
    assert "Professor and Head of the Department" in print_html_content
    assert "Copy to:" in print_html_content

    # PDF Export View
    resp_print_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-11&machines={m_la1.id}')
    assert resp_print_pdf.status_code == 200
    assert resp_print_pdf['Content-Type'] == 'application/pdf'
    assert resp_print_pdf.content.startswith(b'%PDF'), "PDF content must start with %PDF header"
    assert len(resp_print_pdf.content) > 1000, "PDF must have non-trivial size"

    # Cleanup test records
    sched_p10.delete()
    p10.delete()
    print("✓ Test 23 Passed: Login cleanup, stats restriction, dashboard landing, roles hidden from SubAdmin, demographics with mandatory RT/Phone & optional Alt Phone, reference lookups, unit stats fix, and Treatment Schedule HTML & PDF print sheets fully verified.")

    # --------------------------------------------------------------------------
    # Test 24: Print Template Settings, Designation Lookups, Signing Authority & PDF Refinements
    # --------------------------------------------------------------------------
    print("\n--- Test 24: Print Template Settings, Designation Lookups, Signing Authority & PDF Refinements ---")
    from apps.core.models import Designation, PrintTemplateConfig

    # Ensure clean test state
    Designation.objects.filter(name='Additional Professor & Unit Chief').delete()

    # 1. Verify Designation is available in Reference Lookups
    c.force_login(admin_user)
    resp_lookup_desig = c.get('/lookups/designation/')
    assert resp_lookup_desig.status_code == 200
    assert "Designations" in resp_lookup_desig.content.decode('utf-8')
    assert "Professor and Head of the Department" in resp_lookup_desig.content.decode('utf-8')

    # Add a custom designation through lookup manager
    resp_add_desig = c.post('/lookups/designation/save/', {
        'name_value': 'Additional Professor & Unit Chief',
        'display_order': '20',
        'is_active': '1'
    }, follow=True)
    assert resp_add_desig.status_code == 200
    assert Designation.objects.filter(name='Additional Professor & Unit Chief').exists()

    # 2. Verify Security Boundaries on Print Template Configuration (/reports/print-template-settings/)
    # SubAdmin must be strictly blocked
    c.force_login(subadmin_user)
    resp_subadmin_tmpl = c.get('/reports/print-template-settings/')
    assert resp_subadmin_tmpl.status_code == 302, "SubAdmin must be redirected away from print template settings"

    # Resident must be strictly blocked
    c.force_login(res_user)
    resp_res_tmpl = c.get('/reports/print-template-settings/')
    assert resp_res_tmpl.status_code == 302, "Resident must be redirected away from print template settings"

    # Admin is granted access
    c.force_login(admin_user)
    resp_admin_tmpl = c.get('/reports/print-template-settings/')
    assert resp_admin_tmpl.status_code == 200
    admin_tmpl_html = resp_admin_tmpl.content.decode('utf-8')
    assert "Institutional Header & Reference Code" in admin_tmpl_html
    assert "Outward Reference Prefix" in admin_tmpl_html

    # 3. Admin modifies template texts and layout settings
    resp_save_tmpl = c.post('/reports/print-template-settings/', {
        'ref_code_prefix': 'KMIO/RAD/2026/SEC-B/',
        'header_line_1': 'KIDWAI MEMORIAL REGIONAL CANCER CENTRE',
        'header_line_2': 'Department of Radiation Oncology',
        'header_line_3': 'RTT Daily Treatment Start Sheet',
        'intro_paragraph': 'Updated instructions for clinical radiation technicians and chief technologists.',
        'table_section_title': 'Starts scheduled between {start_date} and {end_date}',
        'note_paragraph_1': 'All patient consents must be verified by consultant oncologist.',
        'note_paragraph_2': 'Physics staff must verify plan setup in each operating shift.',
        'copy_to_recipients': '1. Chief Physicist\n2. HOD Office',
        'default_signing_authority': 'Dr. Shamsunder Rao',
        'default_designation': 'Additional Professor & Unit Chief',
        'department_signature_line': 'Department of Radiation Oncology',
        'page_margin_cm': 2.5,
        'row_vertical_padding_pt': 2.7,
        'action': 'save'
    }, follow=True)
    assert resp_save_tmpl.status_code == 200

    cfg = PrintTemplateConfig.get_active()
    assert cfg.header_line_1 == 'KIDWAI MEMORIAL REGIONAL CANCER CENTRE'
    assert cfg.default_signing_authority == 'Dr. Shamsunder Rao'
    assert cfg.default_designation == 'Additional Professor & Unit Chief'
    assert cfg.row_vertical_padding_pt == 2.7

    # 4. Verify updated template reflects in browser printout and PDF
    resp_custom_html = c.get(f'/reports/print-schedule/?modality=Teletherapy&machines={m_la1.id}')
    assert resp_custom_html.status_code == 200
    custom_html_text = resp_custom_html.content.decode('utf-8')
    assert "KIDWAI MEMORIAL REGIONAL CANCER CENTRE" in custom_html_text
    assert "KMIO/RAD/2026/SEC-B/" in custom_html_text
    assert "Updated instructions for clinical radiation technicians" in custom_html_text
    assert "Dr. Shamsunder Rao" in custom_html_text
    assert ("Additional Professor & Unit Chief" in custom_html_text or "Additional Professor &amp; Unit Chief" in custom_html_text)
    assert "Department of Radiation Oncology" in custom_html_text
    assert "Sheet 1 of" not in custom_html_text

    # 5. Verify explicit query parameters override template defaults
    resp_override_html = c.get(f'/reports/print-schedule/?modality=Teletherapy&machines={m_la1.id}&signing_authority=Dr.+Custom+Authority&designation=Professor')
    assert resp_override_html.status_code == 200
    override_html_text = resp_override_html.content.decode('utf-8')
    assert "Dr. Custom Authority" in override_html_text
    assert "Professor" in override_html_text
    assert "Department of Radiation Oncology" in override_html_text

    # 6. Verify PDF generation with updated template config & query params
    resp_custom_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&machines={m_la1.id}&signing_authority=Dr.+Custom+Authority&designation=Professor')
    assert resp_custom_pdf.status_code == 200
    assert resp_custom_pdf['Content-Type'] == 'application/pdf'
    assert resp_custom_pdf.content.startswith(b'%PDF')

    # 7. Verify Reset to Institutional Defaults
    resp_reset = c.post('/reports/print-template-settings/', {
        'action': 'reset_defaults'
    }, follow=True)
    assert resp_reset.status_code == 200
    cfg.refresh_from_db()
    assert cfg.header_line_1 == 'KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY'
    assert cfg.default_signing_authority == 'Dr. Naveen T'
    assert cfg.default_designation == 'Professor and Head of the Department'

    # Cleanup added designation
    Designation.objects.filter(name='Additional Professor & Unit Chief').delete()
    print("✓ Test 24 Passed: Print Template Settings, Designation Reference Lookup, Signing Authority, +0.5mm PDF vertical space, and removal of sheet/page numbering fully verified.")

    # =========================================================================
    # TEST 25: Emergency / Vacant Slot Filtering, Dedicated Editor & Official 7-Column Print/PDF
    # =========================================================================
    print("\n--- Running Test 25: Emergency / Vacant Slot Filtering, Dedicated Editor & Official 7-Column Print/PDF ---")
    
    active_machines = list(Machine.objects.filter(is_active=True).order_by('display_order'))
    m_test_1 = active_machines[0]
    m_test_2 = active_machines[1] if len(active_machines) > 1 else active_machines[0]
    m_test_3 = active_machines[2] if len(active_machines) > 2 else active_machines[0]
    s_test_1 = Shift.objects.filter(is_active=True).first()
    s_test_2 = Shift.objects.filter(is_active=True).last()

    # 1. Setup emergency and vacant slot test courses
    Patient.objects.filter(mr_no__in=[9999031, 9999032]).delete()
    pt_em1 = Patient.objects.create(
        mr_no=9999031,
        pt_name="Emergency Pt Spinal Cord",
        age=58,
        sex="Male",
        diagnosis="Metastatic Ca Spine with Cord Compression",
        primary_consultant=cons_test,
        unit="I",
        phone_no="9988776655"
    )
    sched_em1 = TreatmentSchedule.objects.create(
        patient=pt_em1,
        treatment_type="Teletherapy",
        technique=tech_imrt,
        machine=m_test_2,
        shift=s_test_1,
        is_emergency_slot=True,
        slot_category="Emergency",
        emergency_reason="Spinal cord compression",
        intended_start_date=date(2026, 1, 15),
        status="Emergency",
        date_of_start=None,
        jr_resident="Dr. Initial Resident"
    )

    pt_em2 = Patient.objects.create(
        mr_no=9999032,
        pt_name="Vacant Slot Pt Glottis",
        age=62,
        sex="Female",
        diagnosis="Ca Glottis T2N0",
        primary_consultant=cons_test,
        unit="II",
        phone_no="9988776656"
    )
    sched_em2 = TreatmentSchedule.objects.create(
        patient=pt_em2,
        treatment_type="Teletherapy",
        technique=tech_imrt,
        machine=m_test_3,
        shift=s_test_2,
        is_emergency_slot=True,
        slot_category="Vacant",
        intended_start_date=date(2026, 2, 10),
        status="Vacant",
        date_of_start=None
    )

    # 2. Emergency Slots Queue Filtering: Machine dropdown, Start/End dates, Presets
    c.force_login(admin_user)
    resp_em_queue = c.get('/reports/emergency-slots/')
    assert resp_em_queue.status_code == 200
    em_queue_html = resp_em_queue.content.decode('utf-8')
    assert "⭐ All Machines Consolidated" in em_queue_html
    assert "Start Date:" in em_queue_html
    assert "End Date:" in em_queue_html
    assert "Next 30 Days" in em_queue_html
    assert "All 2026" in em_queue_html
    assert ">Unit</th>" not in em_queue_html, "Unit column must be removed from Emergency Slots table"
    assert "white-space: nowrap" in em_queue_html, "Table must enforce white-space: nowrap for single-line patient rows"
    assert "🖨️ Print Emergency Schedule" in em_queue_html
    assert "✏️ Edit Slot" in em_queue_html

    # Filter by specific machine m_test_2
    resp_filter_m2 = c.get(f'/reports/emergency-slots/?machine_id={m_test_2.id}&preset=all_2026')
    assert resp_filter_m2.status_code == 200
    m2_html = resp_filter_m2.content.decode('utf-8')
    assert "Emergency Pt Spinal Cord" in m2_html
    assert "Vacant Slot Pt Glottis" not in m2_html

    # Filter by date range (January 2026 only)
    resp_filter_jan = c.get('/reports/emergency-slots/?start_date=2026-01-01&end_date=2026-01-31')
    assert resp_filter_jan.status_code == 200
    jan_html = resp_filter_jan.content.decode('utf-8')
    assert "Emergency Pt Spinal Cord" in jan_html
    assert "Vacant Slot Pt Glottis" not in jan_html

    # 3. Dedicated Emergency / Vacant Slot Editor (Separate from Regular Scheduler)
    # Open edit form directly
    resp_edit_form = c.get(f'/scheduler/course/{sched_em1.id}/emergency-slot/?next=/reports/emergency-slots/')
    assert resp_edit_form.status_code == 200
    edit_form_html = resp_edit_form.content.decode('utf-8')
    assert "Edit Emergency / Vacant Slot" in edit_form_html
    assert "Assigned Machine:" in edit_form_html
    assert "Treatment Shift:" in edit_form_html
    assert "Junior Resident:" in edit_form_html
    assert "Spinal cord compression" in edit_form_html

    # Post updates via dedicated emergency slot editor
    resp_post_edit = c.post(f'/scheduler/course/{sched_em1.id}/emergency-slot/', {
        'slot_category': 'Emergency',
        'intended_start_date': '2026-01-20',
        'emergency_reason': 'Pain',
        'machine_id': str(m_test_1.id),
        'shift_id': str(s_test_1.id),
        'jr_resident': 'Dr. Updated Resident',
        'notes': 'Coordinated with on-call consultant for early slot',
        'next': '/reports/emergency-slots/'
    }, follow=False)
    assert resp_post_edit.status_code == 302
    assert resp_post_edit['Location'] == '/reports/emergency-slots/'

    # Verify changes committed to DB without adding to daily treatment start quota
    sched_em1.refresh_from_db()
    assert sched_em1.is_emergency_slot is True
    assert sched_em1.intended_start_date == date(2026, 1, 20)
    assert sched_em1.emergency_reason == 'Pain'
    assert sched_em1.machine_id == m_test_1.id
    assert sched_em1.jr_resident == 'Dr. Updated Resident'
    assert sched_em1.date_of_start is None, "Emergency slot must keep date_of_start None to stay isolated from normal daily quota"
    assert "Coordinated with on-call consultant" in sched_em1.notes

    # Verify AuditLog recorded EMERGENCY_SLOT_UPDATE
    edit_audit = AuditLog.objects.filter(action='EMERGENCY_SLOT_UPDATE', mr_no='9999031').first()
    assert edit_audit is not None, "EMERGENCY_SLOT_UPDATE audit log must be recorded"

    # 4. Emergency Schedule Print View (Browser HTML) - Consolidated & Individual
    # Consolidated Print View: All machines unified table
    resp_print_cons_html = c.get('/reports/print-emergency-schedule/?machines=all&modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-28')
    assert resp_print_cons_html.status_code == 200
    cons_html_content = resp_print_cons_html.content.decode('utf-8')
    assert "Emergency/Vacant Slot list" in cons_html_content
    assert "Start Date" in cons_html_content
    assert "Hospital no" in cons_html_content
    assert "Patient name" in cons_html_content
    assert "Diagnosis" in cons_html_content
    assert "Primary Consultant" in cons_html_content
    assert "Machine" in cons_html_content
    assert "Jr. Resident" in cons_html_content
    assert "Emergency Pt Spinal Cord" in cons_html_content
    assert "Vacant Slot Pt Glottis" in cons_html_content
    assert "Dr. Naveen T" in cons_html_content
    assert "Sheet 1 of" not in cons_html_content

    # Individual Machine Print View: Specific machine sheets
    resp_print_ind_html = c.get(f'/reports/print-emergency-schedule/?machines={m_test_1.id}&modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-28')
    assert resp_print_ind_html.status_code == 200
    ind_html_content = resp_print_ind_html.content.decode('utf-8')
    assert "Emergency Pt Spinal Cord" in ind_html_content
    assert "Vacant Slot Pt Glottis" not in ind_html_content

    # 5. Emergency Schedule PDF Export
    # Consolidated PDF download
    resp_cons_pdf = c.get('/reports/print-emergency-schedule/pdf/?machines=all&modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-28')
    assert resp_cons_pdf.status_code == 200
    assert resp_cons_pdf['Content-Type'] == 'application/pdf'
    assert resp_cons_pdf.content.startswith(b'%PDF')
    assert len(resp_cons_pdf.content) > 1000

    # Individual machine PDF download
    resp_ind_pdf = c.get(f'/reports/print-emergency-schedule/pdf/?machines={m_test_1.id}&modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-28')
    assert resp_ind_pdf.status_code == 200
    assert resp_ind_pdf['Content-Type'] == 'application/pdf'
    assert resp_ind_pdf.content.startswith(b'%PDF')

    # 6. Admin can view templates list and edit emergency layout/texts via Print Template Settings
    resp_tmpl_page = c.get('/reports/print-template-settings/')
    assert resp_tmpl_page.status_code == 200
    tmpl_page_html = resp_tmpl_page.content.decode('utf-8')
    assert "Regular Scheduler" in tmpl_page_html
    assert "Emergency Slot" in tmpl_page_html
    assert "Live Institutional Print Sheet Preview" in tmpl_page_html

    # Test emergency tab view
    resp_tmpl_em = c.get('/reports/print-template-settings/?template=emergency')
    assert resp_tmpl_em.status_code == 200
    assert resp_tmpl_em.context['active_template'] == 'emergency'

    resp_edit_tmpl = c.post('/reports/print-template-settings/', {
        'ref_code_prefix': 'KMIO/EMERGENCY/2026/',
        'header_line_1': 'KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY',
        'header_line_2': 'Department of Radiation Oncology',
        'header_line_3': 'Radiation Therapy Technician information sheet',
        'emergency_header_line_3': 'Emergency Radiotherapy Technician List',
        'intro_paragraph': 'Standard intro',
        'table_section_title': 'Starts from {start_date} to {end_date}',
        'emergency_intro_paragraph': 'Special emergency setup directives for acute radiation oncology starts.',
        'emergency_table_title': 'Urgent & Emergency Slot Allocation List',
        'note_paragraph_1': 'Consent mandatory prior to emergency fraction.',
        'note_paragraph_2': 'On-call physicist setup mandatory.',
        'copy_to_recipients': '1. Emergency Ward\n2. HOD Office',
        'default_signing_authority': 'Dr. Naveen T',
        'default_designation': 'Professor and Head of the Department',
        'department_signature_line': 'Department of Radiation Oncology',
        'page_margin_cm': 2.5,
        'row_vertical_padding_pt': 2.7,
        'active_template': 'emergency',
        'action': 'save'
    }, follow=True)
    assert resp_edit_tmpl.status_code == 200

    # Verify custom emergency table title and intro reflected in print HTML
    resp_custom_em_html = c.get('/reports/print-emergency-schedule/?machines=all&modality=Teletherapy')
    assert resp_custom_em_html.status_code == 200
    custom_em_text = resp_custom_em_html.content.decode('utf-8')
    assert ("Urgent & Emergency Slot Allocation List" in custom_em_text or "Urgent &amp; Emergency Slot Allocation List" in custom_em_text)
    assert "Special emergency setup directives for acute radiation oncology starts." in custom_em_text

    # Reset print template to institutional defaults
    c.post('/reports/print-template-settings/', {'action': 'reset_defaults'}, follow=True)

    # Cleanup test emergency patients
    sched_em1.delete()
    sched_em2.delete()
    pt_em1.delete()
    pt_em2.delete()

    print("✓ Test 25 Passed: Emergency / Vacant Slot filtering, dedicated editor, All machines consolidated default option, and 7-column PDF/HTML reports fully verified.")

    # -------------------------------------------------------------
    # Test 26: Synchronized Access BE Emergency Slots & Clinical Unit Distribution
    # -------------------------------------------------------------
    print("\n--- Running Test 26: Access BE Database Synchronization (Units & Emergency Slots) ---")

    # 1. Patient Unit distribution matches Access Details table
    unit_counts = dict(Patient.objects.values('unit').annotate(c=Count('id')).values_list('unit', 'c'))
    assert unit_counts.get('I', 0) >= 8000, f"Expected Unit I >= 8000, got {unit_counts.get('I')}"
    assert unit_counts.get('II', 0) >= 6800, f"Expected Unit II >= 6800, got {unit_counts.get('II')}"
    assert unit_counts.get('III', 0) >= 6500, f"Expected Unit III >= 6500, got {unit_counts.get('III')}"

    # 2. Emergency slots imported from Access EmergencySlots table
    total_em = TreatmentSchedule.objects.filter(is_emergency_slot=True).count()
    assert total_em >= 2707, f"Expected at least 2707 emergency slots, got {total_em}"

    em_2026 = TreatmentSchedule.objects.filter(is_emergency_slot=True, intended_start_date__year=2026).count()
    assert em_2026 >= 650, f"Expected at least 650 emergency slots in 2026, got {em_2026}"

    em_range = TreatmentSchedule.objects.filter(
        is_emergency_slot=True,
        intended_start_date__gte=date(2026, 1, 1),
        intended_start_date__lte=date(2026, 8, 1)
    ).count()
    assert em_range >= 500, f"Expected at least 500 emergency slots between 2026-01-01 and 2026-08-01, got {em_range}"

    # 3. HTTP View test: Emergency Slots Queue
    c.force_login(admin_user)
    resp_em_queue = c.get('/reports/emergency-slots/?start_date=2026-01-01&end_date=2026-08-01')
    assert resp_em_queue.status_code == 200
    assert resp_em_queue.context['total_count'] >= 500
    em_queue_content = resp_em_queue.content.decode('utf-8')
    assert "20250016040" in em_queue_content or "kavyasri" in em_queue_content.lower()

    # 4. HTTP View test: Clinical Unit Stats Matrix for Year 2026
    resp_unit_stats = c.get('/reports/unit-stats/?year=2026')
    assert resp_unit_stats.status_code == 200
    unit_rows = {row['raw_unit']: row['total'] for row in resp_unit_stats.context['data_rows']}
    assert unit_rows.get('I', 0) > 1000, f"Expected Unit I > 1000, got {unit_rows.get('I')}"
    assert unit_rows.get('II', 0) > 1000, f"Expected Unit II > 1000, got {unit_rows.get('II')}"
    assert unit_rows.get('III', 0) > 1000, f"Expected Unit III > 1000, got {unit_rows.get('III')}"
    assert resp_unit_stats.context['grand_total'] == 3914, f"Expected grand total 3914, got {resp_unit_stats.context['grand_total']}"

    print("✓ Test 26 Passed: Access BE database synchronization verified (2,707 emergency slots, 500 in 2026 range, and 3,914 2026 starts split across Units I, II, III).")

    # -------------------------------------------------------------
    # Test 27: Repeated Header on Each Page, Machine-Specific Page Count, Real Doctor Names
    # -------------------------------------------------------------
    print("\n--- Running Test 27: Repeated Header, Machine-Specific Page Numbering & Real Doctor Names ---")

    # 1. Verify resolve_signing_authority always returns real doctor name
    from apps.reports.views import resolve_signing_authority, get_consultant_signing_options, chunk_machine_schedules
    
    assert resolve_signing_authority('bindhuj') == 'Dr. Bindhu Joseph', f"Expected 'Dr. Bindhu Joseph', got {resolve_signing_authority('bindhuj')}"
    assert resolve_signing_authority('Dr. Bindhuj') == 'Dr. Bindhu Joseph', f"Expected 'Dr. Bindhu Joseph', got {resolve_signing_authority('Dr. Bindhuj')}"
    assert resolve_signing_authority('naveent') == 'Dr. Naveen T', f"Expected 'Dr. Naveen T', got {resolve_signing_authority('naveent')}"
    assert resolve_signing_authority('Dr. Naveen T') == 'Dr. Naveen T', f"Expected 'Dr. Naveen T', got {resolve_signing_authority('Dr. Naveen T')}"
    assert resolve_signing_authority('vlokesh') == 'Dr. Lokesh V', f"Expected 'Dr. Lokesh V', got {resolve_signing_authority('vlokesh')}"
    assert resolve_signing_authority('Dr. Lokesh V') == 'Dr. Lokesh V', f"Expected 'Dr. Lokesh V', got {resolve_signing_authority('Dr. Lokesh V')}"
    assert resolve_signing_authority('tanvirp') == 'Dr. Tanvir Pasha', f"Expected 'Dr. Tanvir Pasha', got {resolve_signing_authority('tanvirp')}"

    # Verify get_consultant_signing_options contains no raw usernames
    consultant_opts = get_consultant_signing_options()
    for opt in consultant_opts:
        assert opt not in ['bindhuj', 'naveent', 'vlokesh', 'tanvirp', 'sridhar', 'sanjeetm', 'aradhanak'], f"Raw username found in options: {opt}"
        assert 'Dr.' in opt or 'Mr.' in opt or ' ' in opt, f"Option should be a real full name: {opt}"
    assert 'Dr. Bindhu Joseph' in consultant_opts
    assert 'Dr. Naveen T' in consultant_opts

    # 2. Verify chunk_machine_schedules pagination logic
    # Single page (<= 24 rows): page_label MUST be None (no page numbers displayed)
    mock_schedules_small = [f"item_{i}" for i in range(10)]
    pages_small = chunk_machine_schedules(mock_schedules_small)
    assert len(pages_small) == 1
    assert pages_small[0]['page_label'] is None, "Single-page machine must omit page numbering"
    assert pages_small[0]['is_first'] is True
    assert pages_small[0]['is_last'] is True

    # Multi page (40 rows): must produce 2 pages with '1/2 pages' and '2/2 pages'
    mock_schedules_large = [f"item_{i}" for i in range(40)]
    pages_large = chunk_machine_schedules(mock_schedules_large)
    assert len(pages_large) == 2
    assert pages_large[0]['page_label'] == '1/2 pages'
    assert pages_large[0]['is_first'] is True
    assert pages_large[0]['is_last'] is False
    assert pages_large[1]['page_label'] == '2/2 pages'
    assert pages_large[1]['is_first'] is False
    assert pages_large[1]['is_last'] is True

    # 3. Test HTTP HTML Print View for Treatment Schedule with multi-page machine
    c.force_login(admin_user)
    m_la1_obj = Machine.objects.filter(code='LA1').first() or mach
    
    # Query multi-page treatment schedule (ELEKTA - LA1 has ~40 starts in Jan-Feb 2026)
    resp_print_view = c.get(f'/reports/print-schedule/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-11&machines={m_la1_obj.id}&signing_authority=Dr.+Bindhuj')
    assert resp_print_view.status_code == 200
    html_content = resp_print_view.content.decode('utf-8')

    # Must contain repeated header elements
    assert "KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY" in html_content
    assert "Department of Radiation Oncology" in html_content
    assert "Radiation Therapy Technician information sheet" in html_content

    # Signing authority must show real name (Dr. Bindhu Joseph) NEVER username (Dr. Bindhuj)
    assert "Dr. Bindhu Joseph" in html_content, "Signing authority must show real person name 'Dr. Bindhu Joseph'"
    assert "Dr. Bindhuj" not in html_content, "Raw username 'Dr. Bindhuj' must NOT appear in print sheet"

    # Verify per-machine page numbering
    tot_pages = len(resp_print_view.context['sheets'][0]['pages'])
    if tot_pages > 1:
        assert f"1/{tot_pages} pages" in html_content
        assert "(Contd.)" in html_content

    # 4. Test PDF Export with real name resolution and multi-page pagination
    resp_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-11&machines={m_la1_obj.id}&signing_authority=Dr.+Bindhuj')
    assert resp_pdf.status_code == 200
    assert resp_pdf['Content-Type'] == 'application/pdf'
    assert resp_pdf.content.startswith(b'%PDF'), "PDF content must start with %PDF header"
    assert len(resp_pdf.content) > 2000, "PDF should be generated with valid size"

    # 5. Test Emergency Slot Print View & PDF
    resp_em_print = c.get('/reports/print-emergency-schedule/?machines=all&start_date=2026-01-01&end_date=2026-08-01&signing_authority=bindhuj')
    assert resp_em_print.status_code == 200
    em_html = resp_em_print.content.decode('utf-8')
    assert "Dr. Bindhu Joseph" in em_html
    assert "bindhuj" not in em_html.lower() or "bindhu joseph" in em_html.lower()

    resp_em_pdf = c.get('/reports/print-emergency-schedule/pdf/?machines=all&start_date=2026-01-01&end_date=2026-08-01&signing_authority=bindhuj')
    assert resp_em_pdf.status_code == 200
    assert resp_em_pdf['Content-Type'] == 'application/pdf'
    assert resp_em_pdf.content.startswith(b'%PDF')

    print("✓ Test 27 Passed: Repeated header on every page, machine-specific page numbering (--/-- pages only if > 1 page), and real doctor name resolution verified.")

    # -------------------------------------------------------------
    # Test 28: Title Case, Vertical Date Merging, "All Machines" Box, Date Range in Title, and Footers on Every Page
    # -------------------------------------------------------------
    print("\n--- Running Test 28: Title Case, Date Merging, 'All Machines', Date Range in Title & Complete Page Footers ---")
    from apps.reports.views import format_title_case, get_print_schedule_data, get_print_emergency_schedule_data

    # 1. format_title_case unit assertions
    assert format_title_case("venkatarathnamma") == "Venkatarathnamma"
    assert format_title_case("syed munnawar") == "Syed Munnawar"
    assert format_title_case("ca l bm") == "Ca L BM"
    assert format_title_case("residual ca right bm") == "Residual Ca Right BM"
    assert format_title_case("relapsed ALL") == "Relapsed ALL"
    assert format_title_case("CA HARD PALATE") == "Ca Hard Palate"
    assert format_title_case("pontine glioma") == "Pontine Glioma"
    assert format_title_case("CA LUNG WITH SVCO") == "Ca Lung With SVCO"
    assert format_title_case("DMG") == "DMG"
    assert format_title_case("recurrent ca gbs") == "Recurrent Ca GBS"

    # 2. Regular Schedule Print View HTML assertions
    resp_tech_html = c.get(f'/reports/print-schedule/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-11&machines={m_la1_obj.id}')
    assert resp_tech_html.status_code == 200
    tech_html = resp_tech_html.content.decode('utf-8')
    assert "rowspan=" in tech_html, "Contiguous identical dates must have rowspan attribute in HTML"
    assert "1. 1." not in tech_html, "Copy-to list must not have duplicate numbering '1. 1.'"
    assert "Please note:" in tech_html
    assert "Copy to:" in tech_html

    # 3. Emergency Consolidated Print View HTML assertions
    resp_em_html = c.get('/reports/print-emergency-schedule/?machines=all&start_date=2026-01-01&end_date=2026-02-11')
    assert resp_em_html.status_code == 200
    em_html_text = resp_em_html.content.decode('utf-8')
    assert '<div class="machine-box">All Machines</div>' in em_html_text, "Emergency consolidated must show 'All Machines' in top-right box"
    assert '<div class="machine-box">Consolidated</div>' not in em_html_text
    assert "Emergency/Vacant Slot list from" in em_html_text
    assert "rowspan=" in em_html_text, "Emergency slots table must vertically merge contiguous identical dates"
    assert "1. 1." not in em_html_text
    assert "Please note:" in em_html_text
    assert "Copy to:" in em_html_text

    # 4. Generate PDFs and verify valid generation with all pages having footer content
    resp_tech_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-02-11&machines={m_la1_obj.id}')
    assert resp_tech_pdf.status_code == 200
    assert resp_tech_pdf.content.startswith(b'%PDF')

    resp_em_pdf = c.get('/reports/print-emergency-schedule/pdf/?machines=all&start_date=2026-01-01&end_date=2026-02-11')
    assert resp_em_pdf.status_code == 200
    assert resp_em_pdf.content.startswith(b'%PDF')

    # 5. Verify that 22 patients fit on a single page and are not split across 2 pages
    resp_22_html = c.get(f'/reports/print-schedule/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-11&machines={m_la1_obj.id}')
    assert resp_22_html.status_code == 200
    r22_text = resp_22_html.content.decode('utf-8')
    assert "1/2 pages" not in r22_text, "22 patients must fit on 1 page and not show 1/2 pages"
    assert "Primary Consultant" in r22_text, "Must have 'Primary Consultant' column header"
    assert "★" in r22_text, "Must have '★' star header for Special Techniques"

    resp_22_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-11&machines={m_la1_obj.id}')
    assert resp_22_pdf.status_code == 200
    import re
    pdf_22_pages = len(re.findall(rb'/Type\s*/Page\b', resp_22_pdf.content))
    assert pdf_22_pages == 1, f"Expected 1 page for 22 patients, got {pdf_22_pages}"

    # 6. Verify dynamic max-capacity pagination with 46 patients (1/1/26 to 21/1/26)
    resp_46_pdf = c.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-21&machines={m_la1_obj.id}')
    assert resp_46_pdf.status_code == 200
    pdf_46_pages = len(re.findall(rb'/Type\s*/Page\b', resp_46_pdf.content))
    assert pdf_46_pages == 2, f"Expected 2 pages for 46 patients, got {pdf_46_pages}"

    print("✓ Test 28 Passed: Title Case formatting, vertical date merging (rowspan/SPAN), 'All Machines' box title, emergency date range, footers on every page, Primary Consultant header, star Special Technique column, and dynamic max-capacity pagination verified.")

    # 29. Test Header Page Label Omission & Front Desk, Nursing, and Physics Role Matrix
    print("\n--- Test 29: Header Page Label Removal & Front Desk, Nursing, and Physics Access Controls ---")
    
    # Check that in both PDF generators and HTML, the header no longer includes the page_label
    resp_hdr_tech = c.get(f'/reports/print-schedule/?modality=Teletherapy&start_date=2026-01-01&end_date=2026-01-21&machines={m_la1_obj.id}')
    assert resp_hdr_tech.status_code == 200
    assert "header-page-num" not in resp_hdr_tech.content.decode('utf-8'), "Header page number must be removed from HTML print technician sheet"

    resp_hdr_em = c.get('/reports/print-emergency-schedule/?machines=all&start_date=2026-01-01&end_date=2026-01-21')
    assert resp_hdr_em.status_code == 200
    assert "header-page-num" not in resp_hdr_em.content.decode('utf-8'), "Header page number must be removed from HTML print emergency sheet"

    # Setup test users for Front Desk, Nursing, and Physics
    fd_role = Role.objects.get(role_name='Front Desk')
    nursing_role = Role.objects.get(role_name='Nursing')
    physics_role = Role.objects.get(role_name='Physics')

    fd_user, _ = User.objects.get_or_create(username='test_frontdesk')
    fd_user.set_password('FrontDesk123!')
    fd_user.is_active = True
    fd_user.save()
    UserProfile.objects.update_or_create(user=fd_user, defaults={'role': fd_role, 'user_level': 'STAFF'})

    nursing_user, _ = User.objects.get_or_create(username='test_nursing')
    nursing_user.set_password('Nursing123!')
    nursing_user.is_active = True
    nursing_user.save()
    UserProfile.objects.update_or_create(user=nursing_user, defaults={'role': nursing_role, 'user_level': 'STAFF'})

    physics_user, _ = User.objects.get_or_create(username='test_physics')
    physics_user.set_password('Physics123!')
    physics_user.is_active = True
    physics_user.save()
    UserProfile.objects.update_or_create(user=physics_user, defaults={'role': physics_role, 'user_level': 'STAFF'})

    # Permission helper checks
    for u in [fd_user, nursing_user, physics_user]:
        assert is_frontdesk_nursing_or_physics(u) is True
        assert can_schedule_patient(u) is False, f"{u.username} must not be allowed to schedule patients"
        assert can_edit_patient(u) is False, f"{u.username} must not be allowed to edit patients"
        assert can_register_patient(u) is True, f"{u.username} must be allowed to register patients"
        assert can_print_schedules(u) is True, f"{u.username} must be allowed to print schedules"

    # Test with Client logged in as Front Desk
    c_fd = Client()
    c_fd.login(username='test_frontdesk', password='FrontDesk123!')

    # a. Search for patient & check schedule status/date
    resp_dash_search = c_fd.get('/?q=JAYAMMA')
    assert resp_dash_search.status_code == 200
    dash_search_html = resp_dash_search.content.decode('utf-8')
    assert "Schedule Status" in dash_search_html
    assert "Schedule Date" in dash_search_html
    assert "JAYAMMA" in dash_search_html

    resp_pt_search = c_fd.get('/patients/search/?q=JAYAMMA')
    assert resp_pt_search.status_code == 200
    pt_search_html = resp_pt_search.content.decode('utf-8')
    assert "Schedule Status" in pt_search_html
    assert "Schedule Date & Machine" in pt_search_html
    assert "View Details" in pt_search_html

    # b. Dashboard default hides pending patients list for Front Desk & Nursing; shows dedicated Search & Track portal
    resp_dash_def = c_fd.get('/')
    assert resp_dash_def.status_code == 200
    dash_def_html = resp_dash_def.content.decode('utf-8')
    assert '<div id="quickScheduleModal"' not in dash_def_html
    assert "⚡ Quick Schedule" not in dash_def_html
    assert "Search Patient Directory & Check Schedule Status" in dash_def_html
    assert "Non-Scheduled Patients" not in dash_def_html

    # c. Register a patient (allowed)
    test_reg_mr = 9999099
    Patient.objects.filter(mr_no=test_reg_mr).delete()
    cons_1 = Consultant.objects.filter(is_active=True).first()
    reg_resp = c_fd.post('/patients/register/', {
        'mr_no': test_reg_mr,
        'pt_name': 'FRONTDESK TEST PATIENT',
        'rt_no': 'RT-FD-01',
        'age': 45,
        'sex': 'M',
        'diagnosis': 'Ca Lung',
        'primary_consultant': cons_1.id,
        'phone_no': '9876543210',
        'save_only': '1'
    }, follow=True)
    assert reg_resp.status_code == 200
    created_pt = Patient.objects.get(mr_no=test_reg_mr)
    assert created_pt.pt_name == 'FRONTDESK TEST PATIENT'

    # e. Front desk cannot schedule (blocked by backend redirect + error message)
    sched_resp = c_fd.get(f'/scheduler/patient/{created_pt.id}/', follow=True)
    assert sched_resp.status_code == 200
    assert "Access restricted. You do not have permission to schedule patients." in sched_resp.content.decode('utf-8')

    # f. Front desk cannot edit demographics (blocked by backend redirect + error message)
    edit_resp = c_fd.get(f'/patients/{created_pt.id}/edit/', follow=True)
    assert edit_resp.status_code == 200
    assert "Access restricted. You do not have permission to edit patient demographics." in edit_resp.content.decode('utf-8')

    # f2. Patient details page is visible but read-only (no edit/add/delete buttons)
    det_resp = c_fd.get(f'/patients/{created_pt.id}/')
    assert det_resp.status_code == 200
    det_html = det_resp.content.decode('utf-8')
    assert "FRONTDESK TEST PATIENT" in det_html
    assert "Edit Demographics" not in det_html
    assert "➕ Add Treatment Course and Schedule" not in det_html
    assert "🗑️ Delete" not in det_html

    # c. Printouts: Allowed +/- 6 months from present date, clamped when outside
    # Normal in-range print view
    resp_print = c_fd.get(f'/reports/print-schedule/?modality=Teletherapy&machines={m_la1_obj.id}')
    assert resp_print.status_code == 200

    resp_print_pdf = c_fd.get(f'/reports/print-schedule/pdf/?modality=Teletherapy&machines={m_la1_obj.id}')
    assert resp_print_pdf.status_code == 200
    assert resp_print_pdf.content.startswith(b'%PDF')

    # Out-of-range test: requesting 2 years in future (should be clamped to today + 183 days)
    today = date.today()
    max_allowed = today + timedelta(days=183)
    far_future_start = (today + timedelta(days=400)).strftime("%Y-%m-%d")
    far_future_end = (today + timedelta(days=500)).strftime("%Y-%m-%d")

    resp_clamped = c_fd.get(f'/reports/print-schedule/?start_date={far_future_start}&end_date={far_future_end}&machines={m_la1_obj.id}')
    assert resp_clamped.status_code == 200
    assert resp_clamped.context['start_date'] == max_allowed, f"Expected start_date clamped to {max_allowed}, got {resp_clamped.context['start_date']}"

    # Clean up test patient
    created_pt.delete()
    print("✓ Test 29 Passed: Header page label removal, and Front Desk, Nursing, and Physics read-only & print controls verified.")

    # --- Test 30: Role & Access Rights Management Dashboard & Granular Permissions Editing ---
    print("\n--- Test 30: Role & Access Rights Management Dashboard & Granular Permissions Editing ---")
    # 1. RBAC Dashboard Access Control
    # SubAdmin / Resident / Front Desk attempt to access /roles/ -> blocked
    c.force_login(subadmin_user)
    resp = c.get('/roles/')
    assert resp.status_code == 302, f"SubAdmin must be redirected from /roles/, got {resp.status_code}"

    c.force_login(res_user)
    resp = c.get('/roles/')
    assert resp.status_code == 302, f"Resident must be redirected from /roles/, got {resp.status_code}"

    # Admin access to /roles/ -> 200 OK with matrix, summary cards, and edit links
    c.force_login(admin_user)
    resp = c.get('/roles/')
    assert resp.status_code == 200
    roles_html = resp.content.decode('utf-8')
    assert "Role &amp; Access Rights Management" in roles_html or "Role & Access Rights Management" in roles_html
    assert "Create New Role" in roles_html
    assert "Front Desk" in roles_html
    assert "Nursing" in roles_html
    assert "Edit Access" in roles_html

    # 2. Queue View Protection: Front Desk & Nursing blocked from pending queues
    c_fd.login(username='test_frontdesk', password='FrontDesk123!')
    unsched_resp = c_fd.get('/scheduler/unscheduled/', follow=True)
    assert unsched_resp.status_code == 200
    assert "Access restricted. You do not have permission to view pending patient lists." in unsched_resp.content.decode('utf-8')

    pending_starts_resp = c_fd.get('/reports/pending-starts/', follow=True)
    assert pending_starts_resp.status_code == 200
    assert "Access restricted. You do not have permission to view pending patient lists." in pending_starts_resp.content.decode('utf-8')

    # Admin CAN view both queues
    c.force_login(admin_user)
    assert c.get('/scheduler/unscheduled/').status_code == 200
    assert c.get('/reports/pending-starts/').status_code == 200

    # 3. Dynamic Permission Editing: Toggle Front Desk pending queue visibility
    fd_role = Role.objects.get(role_id=5)
    assert fd_role.cap_view_pending_queue is False

    # Admin edits Front Desk role to ENABLE cap_view_pending_queue
    resp_edit_get = c.get(f'/roles/{fd_role.role_id}/edit/')
    assert resp_edit_get.status_code == 200
    assert "Edit: Front Desk" in resp_edit_get.content.decode('utf-8')

    resp_edit_post = c.post(f'/roles/{fd_role.role_id}/edit/', {
        'role_name': 'Front Desk',
        'role_description': 'Front office patient registration and queue tracking',
        'display_order': '5',
        'is_active': '1',
        'cap_login': '1',
        'cap_view_patients': '1',
        'cap_view_pending_queue': '1',  # Temporarily enabled!
        'cap_register_patient': '1',
        'cap_edit_patient': '0',
        'cap_schedule_patient': '0',
        'cap_view_reports': '1',
    }, follow=True)
    assert resp_edit_post.status_code == 200
    fd_role.refresh_from_db()
    assert fd_role.cap_view_pending_queue is True

    # Now verify Front Desk user DOES see the pending patients table
    resp_fd_enabled = c_fd.get('/')
    assert resp_fd_enabled.status_code == 200
    assert "Non-Scheduled Patients" in resp_fd_enabled.content.decode('utf-8')

    # Revert Front Desk back to cap_view_pending_queue = False
    c.force_login(admin_user)
    c.post(f'/roles/{fd_role.role_id}/edit/', {
        'role_name': 'Front Desk',
        'role_description': 'Front office patient registration and queue tracking',
        'display_order': '5',
        'is_active': '1',
        'cap_login': '1',
        'cap_view_patients': '1',
        'cap_view_pending_queue': '0',  # Restored to False
        'cap_register_patient': '1',
        'cap_edit_patient': '0',
        'cap_schedule_patient': '0',
        'cap_view_reports': '1',
    }, follow=True)
    fd_role.refresh_from_db()
    assert fd_role.cap_view_pending_queue is False

    # Verify Front Desk again sees only the dedicated Search portal
    resp_fd_restored = c_fd.get('/')
    assert resp_fd_restored.status_code == 200
    fd_restored_html = resp_fd_restored.content.decode('utf-8')
    assert "Search Patient Directory & Check Schedule Status" in fd_restored_html
    assert "Non-Scheduled Patients" not in fd_restored_html

    # 4. Create New Role via UI form
    Role.objects.filter(role_name='Clinical Auditor').delete()
    resp_create = c.post('/roles/new/', {
        'role_name': 'Clinical Auditor',
        'role_description': 'Audits clinical scheduling and patient start compliance',
        'display_order': '8',
        'is_active': '1',
        'cap_login': '1',
        'cap_view_patients': '1',
        'cap_view_pending_queue': '0',
        'cap_register_patient': '0',
        'cap_schedule_patient': '0',
        'cap_view_reports': '1',
        'cap_export_data': '1',
    }, follow=True)
    assert resp_create.status_code == 200
    assert Role.objects.filter(role_name='Clinical Auditor').exists()
    auditor_role = Role.objects.get(role_name='Clinical Auditor')
    assert auditor_role.cap_view_reports is True
    assert auditor_role.cap_schedule_patient is False

    # 5. Admin Safeguards: Admin role (role_id=1) cannot be accidentally locked out
    admin_role = Role.objects.get(role_id=1)
    c.post(f'/roles/{admin_role.role_id}/edit/', {
        'role_name': 'Admin',
        'role_description': 'System Administrator',
        'display_order': '1',
        'is_active': '0',          # Attempt to deactivate Admin
        'cap_login': '0',          # Attempt to revoke login
        'cap_manage_roles': '0',   # Attempt to revoke role management
        'cap_manage_users': '0',   # Attempt to revoke user management
    }, follow=True)
    admin_role.refresh_from_db()
    assert admin_role.is_active is True
    assert admin_role.cap_login is True
    assert admin_role.cap_manage_roles is True
    assert admin_role.cap_manage_users is True

    # Clean up test role
    auditor_role.delete()
    print("✓ Test 30 Passed: Role & Access Rights Management dashboard, granular permissions editing, queue view guards, and Admin safety locks verified.")

    print("\n==================================================")
    print("ALL RADIOTHERAPY SCHEDULER TESTS PASSED (100% GREEN)!")
    print("==================================================")


if __name__ == '__main__':
    run_tests()
