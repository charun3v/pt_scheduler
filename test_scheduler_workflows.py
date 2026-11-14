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
from datetime import date, timedelta
from decimal import Decimal
from apps.core.models import Role, UserProfile, Consultant, FieldValidationConfig
from apps.core.validators import get_or_create_field_config, validate_field_value
from apps.patients.models import Patient
from apps.scheduler.models import (
    Machine, Shift, Technique, SpecialTechnique, TreatmentSite, MachineMapping, TreatmentSchedule
)
from apps.core.permissions import (
    can_edit_locked_identity,
    is_admin_or_subadmin,
    is_admin_only,
    can_manage_validation_rules,
    can_manage_lookups
)


def run_tests():
    print("==================================================")
    print("RUNNING RADIOTHERAPY PATIENT SCHEDULER TEST SUITE")
    print("==================================================")

    # Clean up test patients
    Patient.objects.filter(mr_no__in=[9999001, 9999002, 9999003, 7654321, 12345]).delete()

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

    # Reset MR No rule to Range (4 to 10 digits) for standard institutional operation
    mr_cfg.validation_mode = 'range'
    mr_cfg.min_length = 4
    mr_cfg.max_length = 10
    mr_cfg.numeric_only = True
    mr_cfg.custom_error_message = "MR Number must be numeric and between 4 and 10 digits."
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

    today = date.today()
    sim_date = today - timedelta(days=2)
    start_date = today + timedelta(days=3)

    resp = c.post(f'/scheduler/patient/{p.id}/', {
        # Top card registration fields:
        'age': '53',
        'diagnosis': 'Carcinoma Cervix IIB Confirmed',
        'site_of_treatment': 'Pelvis',
        'phone_no': '9876543210',
        # Scheduling fields:
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
    assert b'Not Scheduled' in resp.content

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

    # 3. Test POST with custom date and custom reason notes tracking
    custom_date = date.today() + timedelta(days=7)
    custom_reason_text = "Urgent clinical escalation required for rapid symptom palliation"
    target_mach = Machine.objects.filter(id=first_m['machine_id']).first()
    target_shift = Shift.objects.filter(id=first_m['shifts'][0]['id']).first()

    resp_post = c.post(f'/scheduler/patient/{test_pt_sched.id}/', {
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
    print("\n==================================================")
    print("ALL RADIOTHERAPY SCHEDULER TESTS PASSED (100% GREEN)!")
    print("==================================================")


if __name__ == '__main__':
    run_tests()
