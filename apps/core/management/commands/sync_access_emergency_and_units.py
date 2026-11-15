import os
import pyodbc
from datetime import datetime, date
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.core.models import Consultant
from apps.patients.models import Patient
from apps.scheduler.models import (
    Machine, Technique, SpecialTechnique, TreatmentIntent, TreatmentSchedule
)


class Command(BaseCommand):
    help = "Syncs Patient Unit data from Details and imports EmergencySlots from Access database"

    def add_arguments(self, parser):
        parser.add_argument(
            '--db-path',
            type=str,
            default=r"D:\n3v\My projects\Databases\Scheduler\Base files\EBRT scheduler - BE.accdb",
            help="Path to Access BE database file"
        )
        parser.add_argument(
            '--pwd',
            type=str,
            default="LAsch123",
            help="Password for Access BE database"
        )

    def handle(self, *args, **options):
        db_path = options['db_path']
        pwd = options['pwd']

        self.stdout.write(self.style.SUCCESS(f"Connecting to Access DB: {db_path}"))
        conn_str = f"Driver={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={db_path};PWD={pwd};"
        conn = pyodbc.connect(conn_str, autocommit=True)
        cursor = conn.cursor()

        def clean_str(val):
            if val is None:
                return ""
            s = str(val).strip()
            return s if s != "None" else ""

        def clean_phone(val):
            if val is None:
                return ""
            s = str(val).strip()
            if s.endswith(".0"):
                s = s[:-2]
            return s

        def clean_int(val):
            if val is None:
                return None
            try:
                return int(float(val))
            except Exception:
                return None

        def clean_decimal(val):
            if val is None:
                return None
            try:
                return Decimal(str(val))
            except Exception:
                return None

        def parse_date(val):
            if isinstance(val, (datetime, date)):
                return val.date() if isinstance(val, datetime) else val
            return None

        def normalize_unit(val):
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

        # =========================================================================
        # PHASE 1: SYNC PATIENT UNITS FROM DETAILS TABLE
        # =========================================================================
        self.stdout.write("--- Phase 1: Syncing Patient Units from Details ---")
        cursor.execute("SELECT ID, MR_No, Unit FROM Details WHERE MR_No IS NOT NULL ORDER BY ID ASC")
        rows = cursor.fetchall()

        unit_by_mr = {}
        for r in rows:
            mr = clean_int(r[1])
            if mr:
                unit_by_mr[mr] = normalize_unit(r[2])

        self.stdout.write(f"Loaded {len(unit_by_mr)} distinct MR_No units from Details table.")

        patient_map = {p.mr_no: p for p in Patient.objects.all()}
        updated_patients = []
        for mr, u_val in unit_by_mr.items():
            if mr in patient_map:
                p = patient_map[mr]
                if p.unit != u_val:
                    p.unit = u_val
                    updated_patients.append(p)

        if updated_patients:
            self.stdout.write(f"Updating unit values for {len(updated_patients)} patients...")
            with transaction.atomic():
                Patient.objects.bulk_update(updated_patients, ['unit'], batch_size=2000)
            self.stdout.write(self.style.SUCCESS(f"Successfully updated unit for {len(updated_patients)} patients!"))
        else:
            self.stdout.write("All existing patient units are already synchronized.")

        # =========================================================================
        # PHASE 2: IMPORT EMERGENCY SLOTS FROM EmergencySlots TABLE
        # =========================================================================
        self.stdout.write("--- Phase 2: Importing Emergency Slots ---")

        # Build lookup maps
        consultant_map = {c.name.strip().lower(): c for c in Consultant.objects.all()}
        # Also build alias map for partial consultant names
        for c in Consultant.objects.all():
            c_simple = c.name.lower().replace("dr.", "").replace("dr", "").strip()
            consultant_map[c_simple] = c

        machine_map = {}
        for m in Machine.objects.all():
            machine_map[m.name.lower()] = m
            machine_map[m.code.lower()] = m
            # also short codes
            clean_m = m.name.lower().replace("elekta - ", "").replace("varian - ", "").replace("tele-", "").strip()
            machine_map[clean_m] = m

        def resolve_machine(m_val):
            if not m_val:
                return None
            m_str = str(m_val).strip().lower()
            if m_str in machine_map:
                return machine_map[m_str]
            clean_m = m_str.replace("elekta - ", "").replace("varian - ", "").replace("tele-", "").replace("la-", "la").strip()
            if clean_m in machine_map:
                return machine_map[clean_m]
            if "la1" in clean_m or "la-1" in clean_m:
                return machine_map.get("la1") or machine_map.get("elekta - la1")
            if "la2" in clean_m or "la-2" in clean_m:
                return machine_map.get("la2") or machine_map.get("elekta - la2")
            if "la3" in clean_m or "la-3" in clean_m:
                return machine_map.get("la3") or machine_map.get("elekta - la3")
            if "la4" in clean_m or "la-4" in clean_m:
                return machine_map.get("la4") or machine_map.get("elekta - la4")
            if "dbx" in clean_m:
                return machine_map.get("dbx") or machine_map.get("varian - dbx")
            if "dhx" in clean_m:
                return machine_map.get("dhx") or machine_map.get("varian - dhx")
            if "unique" in clean_m or "unq" in clean_m:
                return machine_map.get("unique") or machine_map.get("varian - unique")
            if "cobalt" in clean_m:
                return machine_map.get("tele-cobalt") or machine_map.get("cobalt")
            for k, v in machine_map.items():
                if k in m_str:
                    return v
            return None

        tech_map = {t.name.strip().lower(): t for t in Technique.objects.all()}
        intent_map = {i.name.strip().lower(): i for i in TreatmentIntent.objects.all()}
        special_tech_none = SpecialTechnique.objects.filter(name__iexact='None').first()

        cursor.execute("""
            SELECT ID, MR_No, Pt_name, Age, Sex, Date_Of_sim, Diagnosis,
                   [Site_of _treatment], Laterality, Phone_no, Intent, Unit,
                   [Primary Consultant], Machine, Date_as_per_slot, Date_of_intended_start,
                   Dose_planned, No_of_fractions, Technique, Jr_Resident,
                   [Emergency Indication], Notes
            FROM EmergencySlots
            ORDER BY ID ASC
        """)
        em_rows = cursor.fetchall()
        self.stdout.write(f"Retrieved {len(em_rows)} records from EmergencySlots table.")

        # Check for missing patients
        new_patients = []
        for r in em_rows:
            mr = clean_int(r[1])
            if mr and mr not in patient_map:
                pt_name = clean_str(r[2]) or f"Patient {mr}"
                age = clean_int(r[3])
                sex_raw = clean_str(r[4]).upper()
                sex = 'M' if sex_raw.startswith('M') else ('F' if sex_raw.startswith('F') else 'O')
                diag = clean_str(r[6]) or "Carcinoma"
                site = clean_str(r[7]) or "Others"
                phone = clean_phone(r[9])
                u_val = normalize_unit(r[11])

                doc_name = clean_str(r[12]).lower()
                consultant_obj = consultant_map.get(doc_name)
                if not consultant_obj:
                    doc_simple = doc_name.replace("dr.", "").replace("dr", "").strip()
                    consultant_obj = consultant_map.get(doc_simple)

                p_new = Patient(
                    mr_no=mr,
                    pt_name=pt_name,
                    age=age,
                    sex=sex,
                    diagnosis=diag,
                    site_of_treatment=site,
                    primary_consultant=consultant_obj,
                    phone_no=phone,
                    unit=u_val
                )
                patient_map[mr] = p_new
                new_patients.append(p_new)

        if new_patients:
            self.stdout.write(f"Creating {len(new_patients)} missing patients from EmergencySlots demographics...")
            with transaction.atomic():
                Patient.objects.bulk_create(new_patients, batch_size=1000, ignore_conflicts=True)
            # Re-fetch created patient instances to ensure PKs are set
            patient_map = {p.mr_no: p for p in Patient.objects.all()}

        # Clear previously imported emergency slots if replacing
        self.stdout.write("Refreshing emergency slots from Access...")
        TreatmentSchedule.objects.filter(is_emergency_slot=True).delete()

        sched_to_create = []
        for r in em_rows:
            mr = clean_int(r[1])
            if not mr or mr not in patient_map:
                continue

            p = patient_map[mr]
            intended_start = parse_date(r[15])
            original_slot_date = parse_date(r[14])
            sim_date = parse_date(r[5])
            dose = clean_decimal(r[16])
            fractions = clean_int(r[17])
            mach_obj = resolve_machine(r[13])

            t_name = clean_str(r[18]).lower()
            tech_obj = tech_map.get(t_name)

            i_name = clean_str(r[10]).lower()
            intent_obj = intent_map.get(i_name)

            jr_res = clean_str(r[19])
            reason = clean_str(r[20]) or 'Others'
            notes = clean_str(r[21])
            site = clean_str(r[7]) or p.site_of_treatment

            sched_to_create.append(TreatmentSchedule(
                patient=p,
                treatment_type='Teletherapy',
                intent=intent_obj,
                site_of_treatment=site,
                date_of_sim=sim_date,
                dose_planned=dose,
                no_of_fractions=fractions,
                technique=tech_obj,
                special_technique=special_tech_none,
                jr_resident=jr_res,
                machine=mach_obj,
                intended_start_date=intended_start,
                original_scheduled_date=original_slot_date,
                date_of_start=None,  # Crucial: Emergency queue stays isolated from normal quota
                is_emergency_slot=True,
                slot_category='Emergency',
                emergency_reason=reason,
                notes=notes,
                status='Emergency'
            ))

        if sched_to_create:
            self.stdout.write(f"Bulk creating {len(sched_to_create)} emergency treatment schedules...")
            with transaction.atomic():
                TreatmentSchedule.objects.bulk_create(sched_to_create, batch_size=2000)
            self.stdout.write(self.style.SUCCESS(f"Successfully created {len(sched_to_create)} emergency slot schedules!"))
        else:
            self.stdout.write("No emergency slots to create.")

        conn.close()

        # Final verification summary
        from django.db.models import Count
        p_unit_counts = dict(Patient.objects.values('unit').annotate(total=Count('id')).values_list('unit', 'total'))
        em_count = TreatmentSchedule.objects.filter(is_emergency_slot=True).count()
        em_2026_count = TreatmentSchedule.objects.filter(is_emergency_slot=True, intended_start_date__year=2026).count()
        em_range_count = TreatmentSchedule.objects.filter(
            is_emergency_slot=True,
            intended_start_date__gte=date(2026, 1, 1),
            intended_start_date__lte=date(2026, 8, 1)
        ).count()

        self.stdout.write(self.style.SUCCESS(
            f"\n=== Sync Verification Summary ===\n"
            f"Total Patients in SQLite: {Patient.objects.count()}\n"
            f"Patient Unit Distribution: {p_unit_counts}\n"
            f"Total Emergency Slots: {em_count}\n"
            f"Emergency Slots in 2026: {em_2026_count}\n"
            f"Emergency Slots (2026-01-01 to 2026-08-01): {em_range_count}\n"
            f"================================="
        ))
