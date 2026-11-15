from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from apps.core.models import Consultant


SEX_CHOICES = [
    ('F', 'Female'),
    ('M', 'Male'),
    ('O', 'Others'),
]

DEFAULT_SITES = [
    ('Abdomen', 'Abdomen'),
    ('Brain', 'Brain'),
    ('Breast', 'Breast'),
    ('Extremities', 'Extremities'),
    ('Head and neck', 'Head and neck'),
    ('Pelvis', 'Pelvis'),
    ('Thorax', 'Thorax'),
    ('Others', 'Others'),
]


class Patient(models.Model):
    """
    Core Patient Registration Record.
    MR_No is unique. Once registered, MR_No and pt_name can only be edited by Admin/SubAdmin.
    """
    mr_no = models.BigIntegerField(unique=True, db_index=True, verbose_name="Hospital / MR Number")
    pt_name = models.CharField(max_length=255, verbose_name="Patient Name")
    rt_no = models.CharField(max_length=100, blank=True, null=True, verbose_name="RT Number")
    unit = models.CharField(max_length=50, blank=True, null=True, default="I", verbose_name="Clinical Unit")
    age = models.PositiveIntegerField(blank=True, null=True, verbose_name="Age")
    sex = models.CharField(max_length=10, choices=SEX_CHOICES, verbose_name="Sex")
    diagnosis = models.CharField(max_length=255, verbose_name="Diagnosis")
    site_of_treatment = models.CharField(max_length=100, blank=True, null=True, verbose_name="Site of Treatment")
    primary_consultant = models.ForeignKey(Consultant, on_delete=models.SET_NULL, null=True, blank=True, related_name="patients", verbose_name="Primary Consultant")
    phone_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="Phone Number")
    alt_phone_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="Alternate Phone Number")

    # Audit & Tracking
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="registered_patients")

    # Dashboard Visibility Control (Admin only)
    is_hidden_from_dashboard = models.BooleanField(default=False, db_index=True, verbose_name="Hide from Main Dashboard")
    hidden_reason = models.TextField(blank=True, null=True, verbose_name="Reason / Comment for Hiding")
    hidden_at = models.DateTimeField(blank=True, null=True, verbose_name="Hidden At")
    hidden_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="hidden_dashboard_patients", verbose_name="Hidden By")

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['mr_no']),
            models.Index(fields=['pt_name']),
        ]

    def __str__(self):
        return f"{self.pt_name} (MR: {self.mr_no})"

    @property
    def has_schedule(self):
        return self.schedules.exists()

    @property
    def latest_schedule(self):
        return self.schedules.first()

    @property
    def consultant_display(self):
        return self.primary_consultant.name if self.primary_consultant else '-'

    @property
    def age_sex_display(self):
        age_str = str(self.age) if self.age is not None else '-'
        return f"{age_str}/{self.sex}"

    @property
    def sim_date(self):
        """Simulation date from associated treatment schedules."""
        for s in self.schedules.all():
            if s.date_of_sim:
                return s.date_of_sim
        return None

    @property
    def sim_date_display(self):
        sd = self.sim_date
        return sd.strftime("%d-%m-%Y") if sd else '-'


class ClinicalUnit(models.Model):
    """Radiation Oncology Clinical Units (e.g. Unit I, Unit II, Unit III)."""
    name = models.CharField(max_length=50, unique=True, verbose_name="Clinical Unit")
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name
