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


class Machine(models.Model):
    """Linear Accelerators and Teletherapy Machines."""
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True, help_text="Short code e.g. LA1, LA2, DBX")
    machine_type = models.CharField(
        max_length=50,
        choices=MACHINE_TYPE_CHOICES,
        default='LINAC',
        verbose_name="Machine Type",
        help_text="Machine category: LINAC, Brachytherapy, Orthovoltage, Intra Op, PBT"
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
    ]

    STATUS_CHOICES = [
        ('Draft', 'Draft (Not Scheduled)'),
        ('Pending', 'Pending Start'),
        ('Scheduled', 'Scheduled'),
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

    def calculate_end_date(self):
        """
        Calculates estimated completion date based on date_of_start and no_of_fractions,
        skipping Sundays (Monday to Saturday delivery, 1 fraction/day).
        """
        if not self.date_of_start or not self.no_of_fractions or self.no_of_fractions <= 0:
            return None
        from datetime import timedelta
        curr = self.date_of_start
        fractions_done = 1
        while fractions_done < self.no_of_fractions:
            curr += timedelta(days=1)
            if curr.weekday() != 6:  # Skip Sunday
                fractions_done += 1
        return curr

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
