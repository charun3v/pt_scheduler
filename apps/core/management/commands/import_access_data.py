import os
import pyodbc
from datetime import datetime, date
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from django.db import transaction

from apps.core.models import Role, UserProfile, Consultant
from apps.patients.models import Patient
from apps.scheduler.models import (
    Machine, Shift, Technique, SpecialTechnique, SimulatedFor,
    MachineMapping, TreatmentSchedule
)


class Command(BaseCommand):
    help = "Import data from EBRT scheduler - BE.accdb into Django database"

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

        self.stdout.write("--- Step 1: Initializing Roles & Permissions ---")
        self.init_roles()

        self.stdout.write("--- Step 2: Importing Lookups & Doctors ---")
        consultant_map = self.import_consultants(cursor)
        machine_map = self.import_machines(cursor)
        shift_map = self.import_shifts(cursor)
        tech_map = self.import_techniques(cursor)
        special_tech_map = self.import_special_techniques()
        sim_for_map = self.import_simulated_for(cursor)

        self.stdout.write("--- Step 3: Setting Default Dynamic Machine Mappings ---")
        self.init_machine_mappings(machine_map, shift_map, tech_map, special_tech_map)

        self.stdout.write("--- Step 4: Importing Users ---")
        self.import_users(cursor)

        self.stdout.write("--- Step 5: Importing Patients & Treatment Schedules ---")
        self.import_patients_and_schedules(
            cursor, consultant_map, machine_map, shift_map,
            tech_map, special_tech_map, sim_for_map
        )

        conn.close()
        self.stdout.write(self.style.SUCCESS("All data imported successfully!"))

    def init_roles(self):
        roles_data = [
            {
                'role_id': 1, 'role_name': 'Admin',
                'role_description': 'System Administrator - Full Access',
                'display_order': 1,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': True,
                'cap_schedule_patient': True, 'cap_edit_locked_identity': True,
                'cap_manage_mappings': True, 'cap_manage_lookups': True,
                'cap_manage_users': True, 'cap_manage_roles': True,
                'cap_delete_records': True, 'cap_export_data': True, 'cap_view_reports': True
            },
            {
                'role_id': 2, 'role_name': 'SubAdmin',
                'role_description': 'Sub Administrator - Administrative Control',
                'display_order': 2,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': True,
                'cap_schedule_patient': True, 'cap_edit_locked_identity': True,
                'cap_manage_mappings': True, 'cap_manage_lookups': True,
                'cap_manage_users': True, 'cap_manage_roles': True,
                'cap_delete_records': True, 'cap_export_data': True, 'cap_view_reports': True
            },
            {
                'role_id': 3, 'role_name': 'Consultant',
                'role_description': 'Senior Radiation Oncologist',
                'display_order': 3,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': True,
                'cap_schedule_patient': True, 'cap_edit_locked_identity': False,
                'cap_manage_mappings': False, 'cap_manage_lookups': False,
                'cap_manage_users': False, 'cap_manage_roles': False,
                'cap_delete_records': False, 'cap_export_data': True, 'cap_view_reports': True
            },
            {
                'role_id': 4, 'role_name': 'Resident',
                'role_description': 'Junior Resident Doctor',
                'display_order': 4,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': True,
                'cap_schedule_patient': True, 'cap_edit_locked_identity': False,
                'cap_manage_mappings': False, 'cap_manage_lookups': False,
                'cap_manage_users': False, 'cap_manage_roles': False,
                'cap_delete_records': False, 'cap_export_data': True, 'cap_view_reports': True
            },
            {
                'role_id': 5, 'role_name': 'Front Office',
                'role_description': 'Reception & Registration Desk',
                'display_order': 5,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': True,
                'cap_schedule_patient': False, 'cap_edit_locked_identity': False,
                'cap_manage_mappings': False, 'cap_manage_lookups': False,
                'cap_manage_users': False, 'cap_manage_roles': False,
                'cap_delete_records': False, 'cap_export_data': False, 'cap_view_reports': False
            },
            {
                'role_id': 6, 'role_name': 'Physics',
                'role_description': 'Medical Physicist / Dosimetrist',
                'display_order': 6,
                'cap_login': True, 'cap_view_patients': True, 'cap_register_patient': False,
                'cap_schedule_patient': True, 'cap_edit_locked_identity': False,
                'cap_manage_mappings': False, 'cap_manage_lookups': False,
                'cap_manage_users': False, 'cap_manage_roles': False,
                'cap_delete_records': False, 'cap_export_data': True, 'cap_view_reports': True
            }
        ]
        for rd in roles_data:
            Role.objects.update_or_create(role_id=rd['role_id'], defaults=rd)
        self.stdout.write("Roles configured.")

    def import_consultants(self, cursor):
        consultant_map = {}
        cursor.execute("SELECT [Primary consultant] FROM [Primary consultant] WHERE [Primary consultant] IS NOT NULL")
        idx = 1
        for row in cursor.fetchall():
            name = str(row[0]).strip()
            if name:
                c, _ = Consultant.objects.get_or_create(name=name, defaults={'display_order': idx})
                consultant_map[name.lower()] = c
                idx += 1
        self.stdout.write(f"Imported {len(consultant_map)} Consultants.")
        return consultant_map

    def import_machines(self, cursor):
        machine_map = {}
        cursor.execute("SELECT [Machine] FROM [Machine] WHERE [Machine] IS NOT NULL")
        idx = 1
        for row in cursor.fetchall():
            name = str(row[0]).strip()
            if name:
                # generate code
                code = name.replace("ELEKTA - ", "").replace("Varian - ", "").replace("Tele-", "").strip().upper()
                m, _ = Machine.objects.get_or_create(name=name, defaults={'code': code, 'display_order': idx})
                machine_map[name.lower()] = m
                machine_map[code.lower()] = m
                idx += 1
        self.stdout.write(f"Imported {len(Machine.objects.all())} Machines.")
        return machine_map

    def import_shifts(self, cursor):
        shift_map = {}
        cursor.execute("SELECT [Shift] FROM [Shift] WHERE [Shift] IS NOT NULL")
        idx = 1
        for row in cursor.fetchall():
            name = str(row[0]).strip()
            if name:
                s, _ = Shift.objects.get_or_create(name=name, defaults={'display_order': idx})
                shift_map[name.lower()] = s
                idx += 1
        self.stdout.write(f"Imported {len(shift_map)} Shifts.")
        return shift_map

    def import_techniques(self, cursor):
        tech_map = {}
        cursor.execute("SELECT [Technique] FROM [Technique] WHERE [Technique] IS NOT NULL")
        idx = 1
        for row in cursor.fetchall():
            name = str(row[0]).strip()
            if name:
                t, _ = Technique.objects.get_or_create(name=name, defaults={'display_order': idx})
                tech_map[name.lower()] = t
                idx += 1
        self.stdout.write(f"Imported {len(tech_map)} Techniques.")
        return tech_map

    def import_special_techniques(self):
        spec_list = ['None', 'DIBH', 'TBI', 'TSET', 'SRS', 'SBRT', 'RPM Gating', 'Stereotactic']
        spec_map = {}
        for idx, name in enumerate(spec_list, 1):
            st, _ = SpecialTechnique.objects.get_or_create(name=name, defaults={'display_order': idx})
            spec_map[name.lower()] = st
        self.stdout.write(f"Initialized {len(spec_map)} Special Techniques.")
        return spec_map

    def import_simulated_for(self, cursor):
        sim_map = {}
        cursor.execute("SELECT [Simulated_for] FROM [Simulated_for] WHERE [Simulated_for] IS NOT NULL")
        idx = 1
        for row in cursor.fetchall():
            name = str(row[0]).strip()
            if name:
                sf, _ = SimulatedFor.objects.get_or_create(name=name, defaults={'display_order': idx})
                sim_map[name.lower()] = sf
                idx += 1
        self.stdout.write(f"Imported {len(sim_map)} SimulatedFor options.")
        return sim_map

    def init_machine_mappings(self, machine_map, shift_map, tech_map, special_tech_map):
        shifts = list(Shift.objects.all())
        techniques = list(Technique.objects.all())
        spec_techs = list(SpecialTechnique.objects.all())

        for m in Machine.objects.all():
            mapping, created = MachineMapping.objects.get_or_create(
                machine=m,
                defaults={'max_daily_starts': 3}
            )
            if created:
                mapping.allowed_shifts.set(shifts)
                mapping.allowed_techniques.set(techniques)
                mapping.allowed_special_techniques.set(spec_techs)
        self.stdout.write("Default Machine Mappings initialized.")

    def import_users(self, cursor):
        admin_role = Role.objects.get(role_name='Admin')
        subadmin_role = Role.objects.get(role_name='SubAdmin')
        consultant_role = Role.objects.get(role_name='Consultant')

        # Create master admin
        u_admin, created = User.objects.get_or_create(username='admin', defaults={'is_staff': True, 'is_superuser': True})
        if created:
            u_admin.set_password('admin123')
            u_admin.save()
            UserProfile.objects.create(user=u_admin, role=admin_role, user_level='ADMIN')
            self.stdout.write("Created default user: admin / admin123")

        # Create master subadmin
        u_subadmin, created = User.objects.get_or_create(username='subadmin', defaults={'is_staff': True})
        if created:
            u_subadmin.set_password('subadmin123')
            u_subadmin.save()
            UserProfile.objects.create(user=u_subadmin, role=subadmin_role, user_level='SUBADMIN')
            self.stdout.write("Created default user: subadmin / subadmin123")

        # Import users from Access
        cursor.execute("SELECT [Users], [PWord], [UserLevel] FROM [Users] WHERE [Users] IS NOT NULL")
        for row in cursor.fetchall():
            uname = str(row[0]).strip().lower()
            pword = str(row[1]).strip() if row[1] else 'pass'
            ulevel = str(row[2]).strip().upper() if row[2] else 'USER'

            if not uname:
                continue

            target_role = admin_role if ulevel == 'ADMIN' else consultant_role
            u, u_created = User.objects.get_or_create(username=uname, defaults={'is_staff': (ulevel == 'ADMIN')})
            if u_created:
                u.set_password(pword if pword else 'pass')
                u.save()
                UserProfile.objects.create(user=u, role=target_role, user_level=ulevel)
        self.stdout.write(f"Total Users in system: {User.objects.count()}")

    def import_patients_and_schedules(self, cursor, consultant_map, machine_map, shift_map, tech_map, special_tech_map, sim_for_map):
        cursor.execute("""
            SELECT ID, MR_No, Pt_name, Age, Sex, Date_Of_sim, Diagnosis,
                   [Site_of _treatment], Laterality, Phone_no, Intent, Unit,
                   [Primary Consultant], Machine, Date_of_start, Shift,
                   Dose_planned, No_of_fractions, Technique, Jr_Resident,
                   Notes, Simulated_for, RT_No
            FROM Details
            ORDER BY ID ASC
        """)
        rows = cursor.fetchall()
        self.stdout.write(f"Retrieved {len(rows)} records from Details. Processing...")

        def parse_date(val):
            if isinstance(val, (datetime, date)):
                return val.date() if isinstance(val, datetime) else val
            return None

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

        def clean_decimal(val):
            if val is None:
                return None
            try:
                return Decimal(str(val))
            except Exception:
                return None

        def clean_int(val):
            if val is None:
                return None
            try:
                return int(float(val))
            except Exception:
                return None

        # Build patients dictionary by MR_No
        patient_dict = {}
        schedule_records = []

        none_special_tech = special_tech_map.get('none')

        for r in rows:
            # r indices:
            # 0:ID, 1:MR_No, 2:Pt_name, 3:Age, 4:Sex, 5:Date_Of_sim, 6:Diagnosis
            # 7:Site_of _treatment, 8:Laterality, 9:Phone_no, 10:Intent, 11:Unit
            # 12:Primary Consultant, 13:Machine, 14:Date_of_start, 15:Shift
            # 16:Dose_planned, 17:No_of_fractions, 18:Technique, 19:Jr_Resident
            # 20:Notes, 21:Simulated_for, 22:RT_No
            mr_raw = r[1]
            if mr_raw is None:
                continue
            mr_no = clean_int(mr_raw)
            if not mr_no:
                continue

            pt_name = clean_str(r[2]) or f"Patient {mr_no}"
            age = clean_int(r[3])
            sex_raw = clean_str(r[4]).upper()
            sex = 'M' if sex_raw.startswith('M') else ('F' if sex_raw.startswith('F') else 'O')
            diag = clean_str(r[6]) or "Carcinoma"
            site = clean_str(r[7]) or "Others"
            phone = clean_phone(r[9])
            rt_no = clean_str(r[22])

            doc_name = clean_str(r[12]).lower()
            consultant_obj = consultant_map.get(doc_name)

            if mr_no not in patient_dict:
                patient_dict[mr_no] = Patient(
                    mr_no=mr_no,
                    pt_name=pt_name,
                    rt_no=rt_no,
                    age=age,
                    sex=sex,
                    diagnosis=diag,
                    site_of_treatment=site,
                    primary_consultant=consultant_obj,
                    phone_no=phone
                )

            # Match scheduler fields
            sim_date = parse_date(r[5])
            start_date = parse_date(r[14])
            dose = clean_decimal(r[16])
            fractions = clean_int(r[17])

            m_name = clean_str(r[13]).lower()
            mach_obj = machine_map.get(m_name)
            if not mach_obj and m_name:
                for k, v in machine_map.items():
                    if k in m_name:
                        mach_obj = v
                        break

            sh_name = clean_str(r[15]).lower()
            shift_obj = shift_map.get(sh_name)

            t_name = clean_str(r[18]).lower()
            tech_obj = tech_map.get(t_name)

            sf_name = clean_str(r[21]).lower()
            sim_for_obj = sim_for_map.get(sf_name)

            jr_res = clean_str(r[19])
            notes = clean_str(r[20])

            status = 'Scheduled'
            if start_date:
                status = 'Completed' if start_date < date(2026, 1, 1) else 'Scheduled'
            else:
                status = 'Pending'

            schedule_records.append({
                'mr_no': mr_no,
                'date_of_sim': sim_date,
                'dose_planned': dose,
                'no_of_fractions': fractions,
                'technique': tech_obj,
                'special_technique': none_special_tech,
                'jr_resident': jr_res,
                'simulated_for': sim_for_obj,
                'machine': mach_obj,
                'shift': shift_obj,
                'date_of_start': start_date,
                'notes': notes,
                'status': status
            })

        self.stdout.write(f"Bulk creating {len(patient_dict)} unique Patients...")
        with transaction.atomic():
            Patient.objects.bulk_create(list(patient_dict.values()), batch_size=2000, ignore_conflicts=True)

        self.stdout.write("Fetching created patient IDs for relational linking...")
        all_patients = {p.mr_no: p for p in Patient.objects.all()}

        self.stdout.write(f"Bulk creating {len(schedule_records)} Treatment Schedules...")
        sched_objs = []
        for s in schedule_records:
            p_obj = all_patients.get(s['mr_no'])
            if p_obj:
                sched_objs.append(TreatmentSchedule(
                    patient=p_obj,
                    date_of_sim=s['date_of_sim'],
                    dose_planned=s['dose_planned'],
                    no_of_fractions=s['no_of_fractions'],
                    technique=s['technique'],
                    special_technique=s['special_technique'],
                    jr_resident=s['jr_resident'],
                    simulated_for=s['simulated_for'],
                    machine=s['machine'],
                    shift=s['shift'],
                    date_of_start=s['date_of_start'],
                    notes=s['notes'],
                    status=s['status']
                ))

        with transaction.atomic():
            TreatmentSchedule.objects.bulk_create(sched_objs, batch_size=2000)

        self.stdout.write(self.style.SUCCESS(
            f"Successfully imported {Patient.objects.count()} Patients and {TreatmentSchedule.objects.count()} Treatment Schedules!"
        ))
