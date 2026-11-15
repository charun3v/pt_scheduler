from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone


class Role(models.Model):
    """Role-Based Access Control (RBAC) Role definition."""
    role_id = models.IntegerField(primary_key=True)
    role_name = models.CharField(max_length=100, unique=True)
    role_description = models.CharField(max_length=255, blank=True, null=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    # Granular Capability Flags
    cap_login = models.BooleanField(default=True)
    cap_view_patients = models.BooleanField(default=True)
    cap_register_patient = models.BooleanField(default=True)
    cap_schedule_patient = models.BooleanField(default=True)
    cap_edit_locked_identity = models.BooleanField(default=False, help_text="Can edit locked MR No and Patient Name")
    cap_manage_mappings = models.BooleanField(default=False, help_text="Can configure machine-shift-technique mappings")
    cap_manage_lookups = models.BooleanField(default=False, help_text="Can edit dropdown options")
    cap_manage_users = models.BooleanField(default=False, help_text="Can add/edit user accounts")
    cap_manage_roles = models.BooleanField(default=False, help_text="Can modify role permissions")
    cap_delete_records = models.BooleanField(default=False, help_text="Can delete patient records")
    cap_export_data = models.BooleanField(default=True, help_text="Can export reports to Excel/PDF")
    cap_view_reports = models.BooleanField(default=True, help_text="Can view clinical & machine start reports")

    class Meta:
        ordering = ['display_order', 'role_name']

    def __str__(self):
        return self.role_name


PREFIX_CHOICES = [
    ('Dr.', 'Dr.'),
    ('Mr', 'Mr'),
    ('Mrs', 'Mrs'),
    ('Miss', 'Miss'),
]


class UserProfile(models.Model):
    """Extends Django auth.User with role and security tracking."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    prefix = models.CharField(max_length=20, choices=PREFIX_CHOICES, default='Dr.', blank=True, null=True, verbose_name="Prefix")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True, related_name='users')
    user_level = models.CharField(max_length=50, blank=True, null=True)
    failed_login_count = models.IntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failed_login = models.DateTimeField(null=True, blank=True)

    # Admin Approval & Dashboard Visibility Tracking
    is_approved = models.BooleanField(default=True, db_index=True, verbose_name="Approved by Admin")
    approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_user_profiles', verbose_name="Approved By")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="Approved At")

    is_hidden_from_dashboard = models.BooleanField(default=False, db_index=True, verbose_name="Hide from Dashboard")
    hidden_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='hidden_user_profiles', verbose_name="Hidden By")
    hidden_at = models.DateTimeField(null=True, blank=True, verbose_name="Hidden At")

    def __str__(self):
        prefix_str = f"{self.prefix} " if self.prefix else ""
        return f"{prefix_str}{self.user.username} ({self.role.role_name if self.role else 'No Role'})"

    @property
    def display_name(self):
        prefix_str = f"{self.prefix} " if self.prefix else ""
        if self.user.first_name:
            return f"{prefix_str}{self.user.first_name}".strip()
        return f"{prefix_str}{self.user.username}".strip()


class Consultant(models.Model):
    """Radiation Oncology Primary Consultants / Faculty."""
    name = models.CharField(max_length=150, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class Designation(models.Model):
    """Staff / Faculty Designations for Official Reports and Sign-offs."""
    name = models.CharField(max_length=150, unique=True)
    display_order = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'tblDesignation'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class PrintTemplateConfig(models.Model):
    """
    Admin-configurable header text, body paragraphs, and layout parameters
    for official Radiotherapy Technician Information Sheets and reports.
    """
    ref_code_prefix = models.CharField(max_length=100, default='KMIO/DRO/        /20___', verbose_name="Outward Reference Prefix")
    header_line_1 = models.CharField(max_length=255, default='KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY', verbose_name="Institute Name (Header 1)")
    header_line_2 = models.CharField(max_length=255, default='Department of Radiation Oncology', verbose_name="Department (Header 2)")
    header_line_3 = models.CharField(max_length=255, default='Radiation Therapy Technician information sheet', verbose_name="Document Title (Header 3)")
    emergency_header_line_3 = models.CharField(
        max_length=255,
        default='Radiation Therapy Technician information sheet',
        verbose_name="Emergency Document Title (Header 3)"
    )
    
    intro_paragraph = models.TextField(
        default=(
            "The scheduled patient lists are handed over to Chief Radiotherapy Technologist RTTs posted in the "
            "respective machines to start radiotherapy for the new patients as scheduled in the table below. "
            "The following are the list of the new patients for whom radiotherapy is to be started on the respective "
            "dates. RTTs are required to perf the necessary procedures as per instructions on the RT chart, "
            "including setup and verification"
        ),
        verbose_name="Introductory Instructions / Notice"
    )
    
    table_section_title = models.CharField(
        max_length=255,
        default="New Patient starts from {start_date} to {end_date}",
        help_text="Variables {start_date} and {end_date} will be dynamically inserted.",
        verbose_name="Section Title Format"
    )
    
    emergency_intro_paragraph = models.TextField(
        default=(
            "The scheduled patient lists are handed over to Chief Radiotherapy Technologists and RTTs posted in the "
            "respective machines to start radiotherapy for the new patients as scheduled in the table below. "
            "The following are the list of the patients for whom radiotherapy is to be started on an emergency basis on the respective "
            "dates. RTTs are required to perofrm the necessary procedures as per instructions on the RT chart, "
            "including setup and verification"
        ),
        verbose_name="Emergency Introductory Instructions / Notice"
    )
    
    emergency_table_title = models.CharField(
        max_length=255,
        default="Emergency/Vacant Slot list",
        verbose_name="Emergency Section Title Format"
    )
    
    note_paragraph_1 = models.TextField(
        default=(
            "Both the Consultant Radiation oncologist and the Radiation Physicist should have signed and dated the "
            "RT Chart and ensure that informed written consent should be signed by the patient and his/her attendant."
        ),
        verbose_name="Important Note 1"
    )
    
    note_paragraph_2 = models.TextField(
        default=(
            "The Radiation Oncologist and Physicist are requested to attend to new patients setup as per schedule in each shift. "
            "Time allocation for starting of new patients on RT to be utilised efficiently without disruption to ongoing treatments in the respective machines"
        ),
        verbose_name="Important Note 2"
    )
    
    copy_to_recipients = models.TextField(
        default=(
            "1. HOD Physics/Faculty concerned for setup verification\n"
            "2. HOU RT I/II/III/ Faculty concerned for setup inspection and verification\n"
            "3. Chief Radiotherapy technologist\n"
            "4. RTT of concerned shift to start the RT for patients mentioned in the above list\n"
            "5. Machine Notice board"
        ),
        help_text="One recipient per line.",
        verbose_name="Copy To Recipients"
    )
    
    default_signing_authority = models.CharField(
        max_length=150,
        default="Dr. Naveen T",
        verbose_name="Default Signing Authority Name"
    )
    
    default_designation = models.CharField(
        max_length=150,
        default="Professor and Head of the Department",
        verbose_name="Default Designation"
    )
    
    department_signature_line = models.CharField(
        max_length=150,
        default="Department of Radiation Oncology",
        verbose_name="Department Line (Fixed in Signature Block)"
    )

    page_margin_cm = models.FloatField(default=2.5, help_text="Page margin in centimeters (A4 paper)")
    row_vertical_padding_pt = models.FloatField(default=2.7, help_text="Cell vertical padding points (0.5mm expanded)")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Printout Template Configuration"
        verbose_name_plural = "Printout Template Configurations"

    def __str__(self):
        return f"Printout Template Configuration ({self.header_line_1})"

    @classmethod
    def get_active(cls):
        obj = cls.objects.first()
        if not obj:
            obj = cls.objects.create()
        return obj


class AuditLog(models.Model):
    """Clinical compliance and user action audit trail."""
    log_datetime = models.DateTimeField(default=timezone.now)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    mr_no = models.CharField(max_length=50, blank=True, null=True)
    details = models.TextField(blank=True, null=True)
    ip_address = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        ordering = ['-log_datetime']

    def __str__(self):
        return f"{self.log_datetime.strftime('%Y-%m-%d %H:%M')} | {self.user} | {self.action}"


class FieldValidationConfig(models.Model):
    """
    Admin-configurable validation rules for patient registration and clinical entry fields.
    Restricted to Administrator role.
    """
    VALIDATION_MODE_CHOICES = [
        ('exact', 'Exact character length (e.g. exactly 7 characters)'),
        ('range', 'Length range (minimum and maximum characters)'),
        ('any', 'Any length (no character count restriction)'),
    ]

    field_name = models.CharField(max_length=50, unique=True, help_text="Field identifier (e.g. 'mr_no', 'phone_no')")
    field_label = models.CharField(max_length=100)
    validation_mode = models.CharField(max_length=20, choices=VALIDATION_MODE_CHOICES, default='range')
    exact_length = models.PositiveIntegerField(null=True, blank=True, help_text="Required length when mode is 'exact'")
    min_length = models.PositiveIntegerField(null=True, blank=True, default=4, help_text="Minimum length when mode is 'range'")
    max_length = models.PositiveIntegerField(null=True, blank=True, default=15, help_text="Maximum length when mode is 'range'")
    numeric_only = models.BooleanField(default=True, help_text="If True, only digits (0-9) are allowed")
    allow_blank = models.BooleanField(default=False, help_text="Whether this field can be left empty")
    custom_regex = models.CharField(max_length=255, blank=True, null=True, help_text="Optional custom regular expression")
    custom_error_message = models.CharField(max_length=255, blank=True, null=True, help_text="Custom failure message shown to users")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['field_name']

    def __str__(self):
        return f"Validation: {self.field_label} ({self.validation_mode})"


class LookupDefault(models.Model):
    """Stores the active default selection ID for each lookup dropdown category."""
    category = models.CharField(max_length=50, unique=True)
    default_id = models.CharField(max_length=50)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.category} -> {self.default_id}"
