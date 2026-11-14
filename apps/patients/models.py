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
    age = models.PositiveIntegerField(blank=True, null=True, verbose_name="Age")
    sex = models.CharField(max_length=10, choices=SEX_CHOICES, verbose_name="Sex")
    diagnosis = models.CharField(max_length=255, verbose_name="Diagnosis")
    site_of_treatment = models.CharField(max_length=100, blank=True, null=True, verbose_name="Site of Treatment")
    primary_consultant = models.ForeignKey(Consultant, on_delete=models.SET_NULL, null=True, blank=True, related_name="patients", verbose_name="Primary Consultant")
    phone_no = models.CharField(max_length=50, blank=True, null=True, verbose_name="Phone Number")

    # Audit & Tracking
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="registered_patients")

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
