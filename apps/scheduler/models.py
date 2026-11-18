from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from apps.patients.models import Patient


MACHINE_TYPE_CHOICES = [
    ('LINAC', 'LINAC'),
    ('Brachytherapy', 'Brachytherapy'),
    ('Orthovoltage', 'Orthovoltage'),
    ('Intra Op', 'Intra Op'),
    ('PBT', 'PBT'),
]


class MachineType(models.Model):
    """
    Reference table for Radiotherapy Machine Types:
    LINAC, Brachytherapy, Orthovoltage, Intra Op, PBT, etc.
    Configurable via Dropdown Options & Reference Tables Manager (tblMachineType)
    with dynamic drag-and-drop ordering and active toggle.
    """
    name = models.CharField(max_length=50, unique=True, verbose_name="Machine Type Name")
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = "Machine Type"
        verbose_name_plural = "Machine Types"

    def __str__(self):
        return self.name


class Machine(models.Model):
    """Linear Accelerators and Teletherapy Machines."""
    TREATMENT_MODE_CHOICES = [
        ('Teletherapy', 'Teletherapy'),
        ('Brachytherapy', 'Brachytherapy'),
        ('Others', 'Others'),
    ]

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True, help_text="Short code e.g. LA1, LA2, DBX")
    machine_type = models.CharField(
        max_length=50,
        choices=MACHINE_TYPE_CHOICES,
        default='LINAC',
        verbose_name="Machine Type",
        help_text="Machine category: LINAC, Brachytherapy, Orthovoltage, Intra Op, PBT"
    )
    treatment_mode = models.CharField(
        max_length=50,
        choices=TREATMENT_MODE_CHOICES,
        default='Teletherapy',
        verbose_name="Treatment Mode",
        help_text="Modality category: Teletherapy, Brachytherapy, Others"
    )
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class Shift(models.Model):
    """Machine Working Shifts."""
    name = models.CharField(max_length=50, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class Technique(models.Model):
    """Standard Radiotherapy Delivery Techniques."""
    name = models.CharField(max_length=50, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class SpecialTechnique(models.Model):
    """Special Radiotherapy Techniques (DIBH, TBI, SRS, SBRT, Gating, etc.)."""
    name = models.CharField(max_length=100, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class SimulatedFor(models.Model):
    """Machine category simulated for (Varian, Elekta, Cobalt, Brachy)."""
    name = models.CharField(max_length=50, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class TreatmentSite(models.Model):
    """Anatomical Sites of Radiotherapy Treatment (e.g. Breast, Pelvis, Head and neck)."""
    name = models.CharField(max_length=100, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class TreatmentIntent(models.Model):
    """Clinical Treatment Intent (e.g. Radical, Palliative, Adjuvant, Neo-adjuvant)."""
    name = models.CharField(max_length=100, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class BrachyApplicator(models.Model):
    """
    Reference table for Brachytherapy Applicators.
    Admin configurable via Reference Tables Manager with drag-and-drop ordering and custom colors.
    Each applicator can be categorized under a clinical applicator type (e.g. Intracavitary, Interstitial, etc.).
    """
    APPLICATOR_TYPE_CHOICES = [
        ('Intracavitary', 'Intracavitary'),
        ('Interstitial', 'Interstitial'),
        ('Intracavitary + Interstitial', 'Intracavitary + Interstitial'),
        ('Intraluminal', 'Intraluminal'),
        ('Surface mould', 'Surface mould'),
        ('Cylindrical / Vaginal cylinder', 'Cylindrical / Vaginal cylinder'),
        ('Permanent Implant', 'Permanent Implant'),
        ('Others', 'Others'),
    ]

    name = models.CharField(max_length=100, unique=True, verbose_name="Applicator Name")
    code = models.CharField(max_length=50, blank=True, null=True, verbose_name="Applicator Code")
    applicator_type = models.CharField(
        max_length=100,
        choices=APPLICATOR_TYPE_CHOICES,
        default='Intracavitary',
        verbose_name="Applicator Type",
        help_text="Clinical applicator category: Intracavitary, Interstitial, Intraluminal, Surface mould, Cylindrical, etc."
    )
    color_code = models.CharField(max_length=20, default="#1e88e5", verbose_name="Calendar Color")
    text_color = models.CharField(max_length=20, default="#ffffff", verbose_name="Text Color")
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = "Brachytherapy Applicator"
        verbose_name_plural = "Brachytherapy Applicators"

    def __str__(self):
        return f"{self.name} ({self.applicator_type})" if self.applicator_type and self.applicator_type != self.name else self.name


class BrachyAnesthesia(models.Model):
    """
    Reference table for Brachytherapy Anaesthesia Types.
    Admin configurable via Reference Tables Manager with drag-and-drop ordering.
    """
    name = models.CharField(max_length=100, unique=True, verbose_name="Anaesthesia Type Name")
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = "Brachytherapy Anaesthesia"
        verbose_name_plural = "Brachytherapy Anaesthesia Options"

    def __str__(self):
        return self.name


class TreatmentCompletionStatus(models.Model):
    """
    Reference table for Course Completion Status:
    Completed, Incomplete, Not Started, Defaulted, Discontinued, etc.
    Configurable via Dropdown Options & Reference Tables Manager.
    """
    name = models.CharField(max_length=100, unique=True, verbose_name="Status Name")
    is_completed = models.BooleanField(default=False, verbose_name="Signifies Course Completed")
    display_order = models.IntegerField(default=1, verbose_name="Display Order")
    is_active = models.BooleanField(default=True, verbose_name="Active")

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = "Treatment Completion Status"
        verbose_name_plural = "Treatment Completion Statuses"

    def __str__(self):
        return self.name


class MachineMapping(models.Model):
    """
    Dynamic Admin Mapping:
    Maps a Machine with its allowed Shifts, Techniques, Special Techniques, and Sites,
    along with its daily new patient start quota (Option A).
    """
    machine = models.OneToOneField(Machine, on_delete=models.CASCADE, related_name='mapping')
    allowed_shifts = models.ManyToManyField(Shift, blank=True, related_name='mapped_machines')
    allowed_techniques = models.ManyToManyField(Technique, blank=True, related_name='mapped_machines')
    allowed_special_techniques = models.ManyToManyField(SpecialTechnique, blank=True, related_name='mapped_machines')
    allowed_sites = models.CharField(
        max_length=500,
        blank=True,
        default="Abdomen, Brain, Breast, Extremities, Head and neck, Pelvis, Thorax, Others",
        help_text="Comma-separated list of allowed anatomical sites"
    )
    max_daily_starts = models.PositiveIntegerField(
        default=3,
        help_text="Maximum new patient treatment starts allowed per day on this machine (Option A Quota)"
    )
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Mapping for {self.machine.name} (Max {self.max_daily_starts} starts/day)"

    def get_allowed_sites_list(self):
        if not self.allowed_sites:
            return []
        return [s.strip() for s in self.allowed_sites.split(',') if s.strip()]


class TreatmentSchedule(models.Model):
    """
    Patient Radiotherapy Scheduling and Slot Allocation Record.
    Supports Teletherapy (EBRT) and Brachytherapy courses, intent, course tagging, and draft status.
    """
    TREATMENT_TYPE_CHOICES = [
        ('Teletherapy', 'Teletherapy (EBRT)'),
        ('Brachytherapy', 'Brachytherapy'),
        ('Others', 'Others'),
    ]

    STATUS_CHOICES = [
        ('Draft', 'Draft (Not Scheduled)'),
        ('Pending', 'Pending Start'),
        ('Scheduled', 'Scheduled'),
        ('Emergency', 'Emergency Slot'),
        ('Vacant', 'Vacant Slot'),
        ('In Progress', 'In Progress'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name='schedules')
    treatment_type = models.CharField(max_length=20, choices=TREATMENT_TYPE_CHOICES, default='Teletherapy', verbose_name="Treatment Modality / Type")
    intent = models.ForeignKey(TreatmentIntent, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="Treatment Intent", related_name='schedules')
    site_of_treatment = models.CharField(max_length=100, blank=True, null=True, verbose_name="Site of Treatment")
    date_of_sim = models.DateField(null=True, blank=True, verbose_name="Date of Simulation")
    dose_planned = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, verbose_name="Dose planned (Gy)")
    no_of_fractions = models.PositiveIntegerField(null=True, blank=True, verbose_name="Number of fractions")
    technique = models.ForeignKey(Technique, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Technique")
    special_technique = models.ForeignKey(SpecialTechnique, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Special Technique")
    applicator_type = models.ForeignKey(BrachyApplicator, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Brachy Applicator Type")
    anesthesia_type = models.ForeignKey(BrachyAnesthesia, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Brachy Anaesthesia Type")
    jr_resident = models.CharField(max_length=150, blank=True, null=True, verbose_name="Junior Resident")
    simulated_for = models.ForeignKey(SimulatedFor, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Simulated For")
    machine = models.ForeignKey(Machine, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Machine")
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.SET_NULL, verbose_name="Shift")
    date_of_start = models.DateField(null=True, blank=True, verbose_name="Date of Treatment Start")
    date_of_end = models.DateField(null=True, blank=True, verbose_name="Date of Treatment End")
    tagged_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='tagged_courses', verbose_name="Tagged to Course")
    course_tag = models.CharField(max_length=100, blank=True, null=True, verbose_name="Course Tag / Group")
    notes = models.TextField(blank=True, null=True, verbose_name="Clinical / Scheduling Notes")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Scheduled')

    # Emergency / Vacant Slot Filling fields
    is_emergency_slot = models.BooleanField(default=False, db_index=True, verbose_name="Is Emergency / Vacant Slot")
    slot_category = models.CharField(max_length=50, blank=True, null=True, choices=[('Emergency', 'Emergency Slot'), ('Vacant', 'Vacant Slot Filling')], verbose_name="Slot Category")
    emergency_reason = models.CharField(max_length=150, blank=True, null=True, verbose_name="Reason for Emergency")
    intended_start_date = models.DateField(blank=True, null=True, verbose_name="Intended Start Date")
    original_scheduled_date = models.DateField(blank=True, null=True, verbose_name="Original Scheduled Date")

    # Actual Treatment Delivery & Status Tracking
    actual_start_date = models.DateField(null=True, blank=True, verbose_name="Actual Start Date")
    actual_end_date = models.DateField(null=True, blank=True, verbose_name="Actual End Date")
    actual_treatment_status = models.ForeignKey(
        TreatmentCompletionStatus, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='schedules', verbose_name="Actual Treatment Status"
    )
    actual_status_notes = models.CharField(max_length=255, blank=True, null=True, verbose_name="Actual Status Remarks")

    # Audit
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="created_schedules")

    class Meta:
        ordering = ['-date_of_start', '-created_at']
        indexes = [
            models.Index(fields=['date_of_start']),
            models.Index(fields=['machine', 'shift', 'date_of_start']),
            models.Index(fields=['date_of_sim']),
        ]

    def __str__(self):
        return f"{self.patient.pt_name} - {self.machine.name if self.machine else 'Unassigned'} ({self.date_of_start or 'No Start Date'})"

    def clean(self):
        super().clean()
        if self.date_of_sim and self.date_of_start and self.date_of_start < self.date_of_sim:
            from django.core.exceptions import ValidationError
            raise ValidationError({'date_of_start': "Treatment Start Date cannot be earlier than CT Simulation Date. Start date must be on or after simulation date."})

    @staticmethod
    def compute_end_date(start_date, no_of_fractions):
        """
        Calculates estimated completion date based on start_date and no_of_fractions,
        skipping Sundays (Monday to Saturday delivery, 1 fraction/day) and scheduled Holidays.
        """
        if not start_date or not no_of_fractions or no_of_fractions <= 0:
            return None

        from datetime import timedelta
        holiday_dates = set(Holiday.objects.values_list('date', flat=True))
        curr = start_date
        fractions_done = 1
        while fractions_done < no_of_fractions:
            curr += timedelta(days=1)
            if curr.weekday() != 6 and curr not in holiday_dates:  # Skip Sunday and Holidays
                fractions_done += 1
        return curr

    def calculate_end_date(self, start_date=None, no_of_fractions=None):
        """
        Calculates estimated completion date for this schedule instance.
        """
        st = start_date or self.date_of_start
        frac = no_of_fractions or self.no_of_fractions
        return self.compute_end_date(st, frac)

    @property
    def treatment_end_date(self):
        return self.date_of_end or self.calculate_end_date()

    @property
    def is_draft(self):
        return self.status == 'Draft'

    def save(self, *args, **kwargs):
        if self.date_of_start and self.no_of_fractions and not self.date_of_end:
            self.date_of_end = self.calculate_end_date()
        super().save(*args, **kwargs)

    @property
    def machine_display(self):
        if self.machine:
            return self.machine.code or self.machine.name
        return '-'

    @property
    def shift_display(self):
        if self.shift:
            return self.shift.name
        return '-'

    @property
    def final_scheduled_date(self):
        """
        Determines the final scheduled date of this treatment course.
        For Brachytherapy: the latest scheduled appointment date.
        For Teletherapy: the scheduled/calculated end date, or start date, or intended start date.
        """
        if self.treatment_type == 'Brachytherapy':
            latest_app = self.brachy_appointments.order_by('-appointment_date').first()
            if latest_app:
                return latest_app.appointment_date
        if self.date_of_end:
            return self.date_of_end
        calc_end = self.calculate_end_date()
        if calc_end:
            return calc_end
        if self.date_of_start:
            return self.date_of_start
        if self.intended_start_date:
            return self.intended_start_date
        return None

    @property
    def is_frozen(self):
        """
        Returns True if more than one month (30 days) has passed since the final scheduled date.
        Frozen courses can only be edited or modified by Administrators.
        """
        f_date = self.final_scheduled_date
        if not f_date:
            return False
        from datetime import date, timedelta
        return date.today() > (f_date + timedelta(days=30))

    def is_editable_by(self, user):
        """
        Returns True if the given user is allowed to edit this schedule.
        If the schedule is frozen (> 1 month past final scheduled date), ONLY Admin can edit it.
        Otherwise, standard scheduling permission rules apply.
        """
        from apps.core.permissions import is_admin_only, can_schedule_patient
        if not can_schedule_patient(user):
            return False
        if self.is_frozen:
            return is_admin_only(user)
        return True


class BrachytherapyAppointment(models.Model):
    """
    Individual Brachytherapy Application / Fraction Appointment.
    Linked to patient and treatment course, displayed on the Visual Monthly Calendar.
    """
    STATUS_CHOICES = [
        ('Scheduled', 'Scheduled'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    ]

    schedule = models.ForeignKey(
        TreatmentSchedule, on_delete=models.CASCADE, related_name='brachy_appointments',
        null=True, blank=True, verbose_name="Treatment Schedule Course"
    )
    patient = models.ForeignKey(
        'patients.Patient', on_delete=models.CASCADE, related_name='brachy_appointments',
        verbose_name="Patient"
    )
    fraction_number = models.PositiveIntegerField(default=1, verbose_name="Fraction / Session Number")
    appointment_date = models.DateField(db_index=True, verbose_name="Appointment Date")
    applicator_type = models.ForeignKey(
        BrachyApplicator, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='appointments', verbose_name="Applicator Type"
    )
    anesthesia_type = models.ForeignKey(
        BrachyAnesthesia, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='appointments', verbose_name="Anaesthesia Details"
    )
    site_of_treatment = models.CharField(max_length=100, blank=True, null=True, verbose_name="Site of Treatment")
    remarks = models.TextField(blank=True, null=True, verbose_name="Additional Remarks")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Scheduled', verbose_name="Appointment Status")

    # Audit & Consultant Authorization
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name='created_brachy_appointments')
    authorized_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='authorized_brachy_appointments', verbose_name="Consultant Authorizer"
    )
    is_consultant_authorized = models.BooleanField(default=False, verbose_name="Authorized by Consultant (>4 fractions)")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['appointment_date', 'fraction_number']
        indexes = [
            models.Index(fields=['appointment_date']),
            models.Index(fields=['patient', 'appointment_date']),
        ]
        verbose_name = "Brachytherapy Appointment"
        verbose_name_plural = "Brachytherapy Appointments"

    def __str__(self):
        return f"{self.patient.pt_name} - {self.appointment_date} (Fr {self.fraction_number})"


class Holiday(models.Model):
    """
    Hospital / Department Scheduled Holidays.
    Configured by Admin & SubAdmin.
    Excluded from auto-scheduling machine slots.
    Allowed with a warning symbol during manual scheduling.
    """
    date = models.DateField(unique=True, db_index=True, verbose_name="Holiday Date")
    comment = models.CharField(max_length=255, blank=True, null=True, verbose_name="Comment / Description")
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ['date']
        verbose_name = "Hospital Holiday"
        verbose_name_plural = "Hospital Holidays"

    def __str__(self):
        desc = f" ({self.comment})" if self.comment else ""
        return f"{self.date.strftime('%d-%b-%Y')}{desc}"


class EmergencyReason(models.Model):
    """Reasons for Emergency Radiotherapy Slots."""
    name = models.CharField(max_length=150, unique=True, verbose_name="Reason for Emergency")
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = "Reason for Emergency"
        verbose_name_plural = "Reasons for Emergency"

    def __str__(self):
        return self.name

