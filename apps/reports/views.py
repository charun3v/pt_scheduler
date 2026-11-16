from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Q
from django.utils import timezone
from datetime import date, datetime, timedelta
import calendar
import json
import csv
import re
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from apps.scheduler.models import Machine, TreatmentSchedule, Shift, Holiday
from apps.patients.models import Patient
from apps.core.models import Consultant, AuditLog, Designation, PrintTemplateConfig
from apps.core.permissions import is_admin_or_subadmin, is_admin_only
from django.contrib.auth.models import User
from django.core.paginator import Paginator


@login_required
def machine_daily_list_view(request):
    """
    Treatment Starts by Machine.
    Replicates & enhances Access Queries: Treatment start Query LA1, LA2, LA3, LA4, DBX, DHX, UNIQUE.
    """
    machines = Machine.objects.filter(is_active=True).order_by('display_order')
    selected_machine_id = request.GET.get('machine_id')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    today = date.today()
    if not start_date_str:
        start_date = today
        start_date_str = today.isoformat()
    else:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            start_date = today

    if not end_date_str:
        end_date = start_date + timedelta(days=6)
        end_date_str = end_date.isoformat()
    else:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            end_date = start_date

    schedules = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'shift', 'technique', 'patient__primary_consultant'
    ).filter(date_of_start__range=[start_date, end_date]).exclude(status__in=['Draft', 'Emergency', 'Vacant']).exclude(is_emergency_slot=True)

    selected_machine = None
    if selected_machine_id:
        schedules = schedules.filter(machine_id=selected_machine_id)
        selected_machine = Machine.objects.filter(id=selected_machine_id).first()

    schedules = schedules.order_by('date_of_start', 'shift__display_order', 'patient__pt_name')

    # Excel export
    if request.GET.get('export') == 'excel':
        return export_machine_schedule_excel(schedules, selected_machine, start_date, end_date)

    context = {
        'machines': machines,
        'selected_machine': selected_machine,
        'selected_machine_id': selected_machine_id,
        'start_date_str': start_date_str,
        'end_date_str': end_date_str,
        'schedules': schedules,
        'total_count': schedules.count(),
    }
    return render(request, 'reports/machine_daily_list.html', context)


@login_required
def pending_starts_report_view(request):
    """
    Pending Start Dates Report.
    Replicates Access query & report 'PendingStartDate':
    Patients who have completed simulation but have NO treatment start date set yet.
    """
    start_sim_str = request.GET.get('start_sim') or request.GET.get('start_date')
    end_sim_str = request.GET.get('end_sim') or request.GET.get('end_date')

    query = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'technique', 'patient__primary_consultant'
    ).filter(date_of_start__isnull=True).exclude(status__in=['Draft', 'Emergency', 'Vacant']).exclude(is_emergency_slot=True)

    if start_sim_str:
        try:
            start_sim = datetime.strptime(start_sim_str, "%Y-%m-%d").date()
            query = query.filter(date_of_sim__gte=start_sim)
        except ValueError:
            pass

    if end_sim_str:
        try:
            end_sim = datetime.strptime(end_sim_str, "%Y-%m-%d").date()
            query = query.filter(date_of_sim__lte=end_sim)
        except ValueError:
            pass

    pending_list = query.order_by('date_of_sim', 'patient__pt_name')

    if request.GET.get('export') == 'excel':
        return export_pending_starts_excel(pending_list)

    context = {
        'pending_list': pending_list,
        'total_count': pending_list.count(),
        'start_sim_str': start_sim_str or '',
        'end_sim_str': end_sim_str or '',
    }
    return render(request, 'reports/pending_starts.html', context)


@login_required
def consultant_monthly_stats_view(request):
    """
    Consultant Monthly Starts Statistics.
    Replicates Access Query_RT_Starts_consultant_monthwise pivot.
    Restricted strictly to Admin only.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    year = int(request.GET.get('year', date.today().year))
    available_years = list(range(2020, date.today().year + 2))

    # Aggregate counts by consultant and month
    schedules = TreatmentSchedule.objects.filter(
        date_of_start__year=year,
        patient__primary_consultant__isnull=False
    ).values(
        'patient__primary_consultant__name',
        'date_of_start__month'
    ).annotate(total=Count('id'))

    consultants = Consultant.objects.filter(is_active=True).order_by('name')
    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    # Build matrix
    matrix = {c.name: {m: 0 for m in range(1, 13)} for c in consultants}
    for row in schedules:
        c_name = row['patient__primary_consultant__name']
        m_idx = row['date_of_start__month']
        if c_name in matrix:
            matrix[c_name][m_idx] = row['total']

    # Compute row totals
    data_rows = []
    month_totals = {m: 0 for m in range(1, 13)}
    grand_total = 0

    for c in consultants:
        row_counts = [matrix[c.name][m] for m in range(1, 13)]
        row_sum = sum(row_counts)
        grand_total += row_sum
        for m in range(1, 13):
            month_totals[m] += matrix[c.name][m]
        data_rows.append({
            'consultant': c.name,
            'counts': row_counts,
            'total': row_sum
        })

    # Sort consultants by total descending
    data_rows.sort(key=lambda x: x['total'], reverse=True)
    month_totals_list = [month_totals[m] for m in range(1, 13)]

    # Exports
    export_fmt = request.GET.get('export')
    if export_fmt == 'excel':
        return export_consultant_stats_excel(data_rows, months, month_totals_list, grand_total, year)
    elif export_fmt == 'csv':
        return export_consultant_stats_csv(data_rows, months, month_totals_list, grand_total, year)

    context = {
        'year': year,
        'available_years': available_years,
        'months': months,
        'data_rows': data_rows,
        'month_totals': month_totals_list,
        'grand_total': grand_total,
    }
    return render(request, 'reports/consultant_stats.html', context)


@login_required
def unit_monthly_stats_view(request):
    """
    Clinical Unit Starts Statistics.
    Groups treatment starts by Unit (Unit I, Unit II, Unit III, etc.) month-by-month for the year.
    Supports CSV and Excel export. Strictly restricted to Admin only.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    year = int(request.GET.get('year', date.today().year))
    available_years = list(range(2020, date.today().year + 2))

    schedules = TreatmentSchedule.objects.filter(
        date_of_start__year=year
    ).exclude(status__in=['Draft', 'Emergency', 'Vacant']).exclude(is_emergency_slot=True).values(
        'patient__unit',
        'date_of_start__month'
    ).annotate(total=Count('id'))

    known_units_set = set(['I', 'II', 'III'])
    # Safely query any other distinct units from database without duplicating rows
    db_units = Patient.objects.order_by().exclude(unit__isnull=True).exclude(unit='').values_list('unit', flat=True).distinct()
    for u in db_units:
        if u and str(u).strip():
            known_units_set.add(str(u).strip())
    known_units = sorted(list(known_units_set))

    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    matrix = {u: {m: 0 for m in range(1, 13)} for u in known_units}

    for row in schedules:
        u_val = str(row['patient__unit'] or 'I').strip()
        m_idx = row['date_of_start__month']
        if u_val in matrix:
            matrix[u_val][m_idx] += row['total']
        else:
            matrix[u_val] = {m: 0 for m in range(1, 13)}
            matrix[u_val][m_idx] = row['total']
            if u_val not in known_units:
                known_units.append(u_val)

    data_rows = []
    month_totals = {m: 0 for m in range(1, 13)}
    grand_total = 0

    for u in known_units:
        row_counts = [matrix[u][m] for m in range(1, 13)]
        row_sum = sum(row_counts)
        grand_total += row_sum
        for m in range(1, 13):
            month_totals[m] += matrix[u][m]
        data_rows.append({
            'unit': f"Unit {u}" if not str(u).lower().startswith('unit') else u,
            'raw_unit': u,
            'counts': row_counts,
            'total': row_sum
        })

    data_rows.sort(key=lambda x: str(x['raw_unit']))
    month_totals_list = [month_totals[m] for m in range(1, 13)]

    # Exports
    export_fmt = request.GET.get('export')
    if export_fmt == 'excel':
        return export_unit_stats_excel(data_rows, months, month_totals_list, grand_total, year)
    elif export_fmt == 'csv':
        return export_unit_stats_csv(data_rows, months, month_totals_list, grand_total, year)

    context = {
        'year': year,
        'available_years': available_years,
        'months': months,
        'data_rows': data_rows,
        'month_totals': month_totals_list,
        'grand_total': grand_total,
    }
    return render(request, 'reports/unit_stats.html', context)


@login_required
def simulation_report_view(request):
    """
    Patients Simulated Between Dates Report.
    Filters treatment schedules by CT Simulation Date range.
    Supports CSV and Excel export. Strictly restricted to Admin only.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted to Administrators.")
        return redirect('dashboard')

    today = date.today()
    start_sim_str = request.GET.get('start_sim') or request.GET.get('start_date')
    end_sim_str = request.GET.get('end_sim') or request.GET.get('end_date')

    if not start_sim_str:
        start_sim = today.replace(day=1)
        start_sim_str = start_sim.isoformat()
    else:
        try:
            start_sim = datetime.strptime(start_sim_str, "%Y-%m-%d").date()
        except ValueError:
            start_sim = today.replace(day=1)
            start_sim_str = start_sim.isoformat()

    if not end_sim_str:
        end_sim = today
        end_sim_str = end_sim.isoformat()
    else:
        try:
            end_sim = datetime.strptime(end_sim_str, "%Y-%m-%d").date()
        except ValueError:
            end_sim = today
            end_sim_str = end_sim.isoformat()

    schedules_qs = TreatmentSchedule.objects.select_related(
        'patient', 'patient__primary_consultant', 'machine', 'technique', 'shift'
    ).filter(
        date_of_sim__range=[start_sim, end_sim]
    ).order_by('-date_of_sim', 'patient__pt_name')

    export_fmt = request.GET.get('export')
    if export_fmt == 'excel':
        return export_simulation_report_excel(schedules_qs, start_sim, end_sim)
    elif export_fmt == 'csv':
        return export_simulation_report_csv(schedules_qs, start_sim, end_sim)

    context = {
        'schedules': schedules_qs,
        'total_count': schedules_qs.count(),
        'start_sim_str': start_sim_str,
        'end_sim_str': end_sim_str,
    }
    return render(request, 'reports/simulation_report.html', context)


def format_title_case(text):
    """
    Formats patient name and diagnosis into Title Case while preserving
    standard clinical abbreviations (Ca, RT, LT, BM, SCC, ALL, NHL, PFS, SVCO, DMG, GBS, etc.).
    """
    if not text:
        return '-'
    text = str(text).strip()
    if not text or text == '-':
        return '-'
    acronyms = {
        'ca': 'Ca', 'rt': 'RT', 'lt': 'LT', 'bm': 'BM', 'scc': 'SCC',
        'all': 'ALL', 'nhl': 'NHL', 'pfs': 'PFS', 'svco': 'SVCO',
        'dmg': 'DMG', 'gbs': 'GBS', 'po': 'PO', 'nec': 'NEC',
        'rms': 'RMS', 'fom': 'FOM', 'bot': 'BOT', 'vc': 'VC',
        'rtts': 'RTTs', 'rtt': 'RTT', 'hod': 'HOD', 'hou': 'HOU'
    }
    words = text.split()
    out = []
    for w in words:
        pfx = ''
        sfx = ''
        core = w
        while core and not core[0].isalnum():
            pfx += core[0]
            core = core[1:]
        while core and not core[-1].isalnum():
            sfx = core[-1] + sfx
            core = core[:-1]
        
        low = core.lower()
        if low in acronyms:
            out.append(pfx + acronyms[low] + sfx)
        elif '/' in core:
            parts = [acronyms.get(p.lower(), p.capitalize()) for p in core.split('/')]
            out.append(pfx + '/'.join(parts) + sfx)
        else:
            out.append(pfx + core.capitalize() + sfx)
    return ' '.join(out)


def resolve_signing_authority(raw_name):
    """
    Ensures signing authority always displays the real person name, never a raw username.
    e.g. 'Dr. Bindhuj' or 'bindhuj' -> 'Dr. Bindhu Joseph'.
    """
    if not raw_name or not str(raw_name).strip():
        config = PrintTemplateConfig.get_active()
        raw_name = config.default_signing_authority or "Dr. Naveen T"

    raw_str = str(raw_name).strip()

    # 1. Clean prefix
    clean = raw_str.lower()
    for pfx in ['dr.', 'dr']:
        if clean.startswith(pfx):
            clean = clean[len(pfx):].strip()

    # 2. Known mapping of short username/handle to full doctor name
    user_to_full_name = {
        'bindhuj': 'Dr. Bindhu Joseph',
        'binduv': 'Dr. Bindu V',
        'vlokesh': 'Dr. Lokesh V',
        'thejaswini': 'Dr. Thejaswini',
        'naveent': 'Dr. Naveen T',
        'siddannap': 'Dr. Siddanna P',
        'gov1': 'Dr. Govardhan',
        'khaleeli': 'Dr. Ibrahim Khaleel',
        'nandan': 'Dr. Nanda',
        'aradhanak': 'Dr. Aradhana K',
        'vinayk': 'Dr. Vinay K',
        'shams': 'Dr. Shamsunder',
        'tanvirp': 'Dr. Tanvir Pasha',
        'sridhar': 'Dr. Sridhar',
        'udayk': 'Dr. Uday Kumar',
        'nikhilak': 'Dr. Nikhila K R',
        'sunilr': 'Dr. Sunil R',
        'sanjeetm': 'Dr. Sanjeeth Mandal',
        'ashal': 'Dr. Ashalatha',
        'pawar': 'Dr. Pawar',
        'manjula': 'Dr. Manjula',
        'n3v': 'Dr. Nithin Bhaskar V',
        'rashmi': 'Dr. Rashmi',
        'ajay': 'Dr. Ajay',
    }

    if clean in user_to_full_name:
        return user_to_full_name[clean]

    # 3. Match against User model's username or full name
    matched_user = User.objects.filter(username__iexact=clean).first()
    if not matched_user:
        for u in User.objects.filter(is_active=True):
            full_n = u.get_full_name().strip()
            if full_n and full_n.lower() == clean:
                matched_user = u
                break

    if matched_user:
        full = matched_user.get_full_name().strip()
        if full:
            prefix = matched_user.profile.prefix if hasattr(matched_user, 'profile') and matched_user.profile and matched_user.profile.prefix else 'Dr.'
            return f"{prefix} {full}".strip()

    # 4. Exact match with Consultant name in database
    for c in Consultant.objects.filter(is_active=True).order_by('name'):
        if raw_str.lower() == c.name.strip().lower():
            return c.name.strip()

    # 5. If raw_str already has 2+ words (e.g. 'Dr. Naveen T' or 'Dr. Bindhu Joseph'), keep it
    words = raw_str.split()
    if len(words) >= 2 and any(raw_str.lower().startswith(p) for p in ['dr.', 'dr', 'mr.', 'ms.', 'mrs.']):
        return raw_str

    # 6. Fallback substring match in Consultant table
    for c in Consultant.objects.filter(is_active=True).order_by('name'):
        c_clean = c.name.lower()
        for pfx in ['dr.', 'dr']:
            if c_clean.startswith(pfx):
                c_clean = c_clean[len(pfx):].strip()
        if clean in c_clean or c_clean in clean:
            return c.name.strip()

    return raw_str


def get_consultant_signing_options(default_authority=None):
    """
    Returns a deduplicated list of real person names for signing authority dropdowns/datalists.
    NEVER includes raw usernames or un-named user accounts.
    """
    options = []
    seen = set()

    if default_authority:
        resolved_default = resolve_signing_authority(default_authority)
        if resolved_default and resolved_default.lower() not in seen:
            options.append(resolved_default)
            seen.add(resolved_default.lower())

    for c in Consultant.objects.filter(is_active=True).order_by('name'):
        c_name = c.name.strip()
        if c_name.lower() not in seen:
            options.append(c_name)
            seen.add(c_name.lower())

    consultant_users = User.objects.filter(
        Q(profile__role__role_name__icontains='Consultant') | 
        Q(profile__role__role_description__icontains='Consultant'),
        is_active=True
    ).select_related('profile').order_by('first_name', 'username')

    for u in consultant_users:
        full = u.get_full_name().strip()
        if full:
            prefix = u.profile.prefix if hasattr(u, 'profile') and u.profile and u.profile.prefix else 'Dr.'
            name_str = f"{prefix} {full}".strip()
            if name_str.lower() not in seen:
                options.append(name_str)
                seen.add(name_str.lower())

    return options



@login_required
def emergency_slots_report_view(request):
    """
    Emergency & Vacant Slots Queue.
    Lists all patients assigned to Emergency or Vacant slot filling.
    Allows filtering by Machine (All Machines Consolidated default), Start Date, End Date, Presets.
    Provides instant live search, editing, reverting, and official PDF/browser print designer.
    """
    all_machines = Machine.objects.filter(is_active=True).order_by('display_order')
    all_shifts = Shift.objects.filter(is_active=True).order_by('display_order')

    machine_id = request.GET.get('machine_id', 'all')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    preset = request.GET.get('preset', '')

    today = date.today()
    default_start = get_upcoming_monday(today)
    default_end = add_six_months(default_start)

    start_date = None
    end_date = None

    if preset == 'this_month':
        start_date = today.replace(day=1)
        last_day = calendar.monthrange(today.year, today.month)[1]
        end_date = today.replace(day=last_day)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    elif preset == 'next_30':
        start_date = today
        end_date = today + timedelta(days=30)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    elif preset == 'all_2026':
        start_date = date(2026, 1, 1)
        end_date = date(2026, 12, 31)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    elif preset == 'all':
        start_date = None
        end_date = None
        start_date_str = ''
        end_date_str = ''
    else:
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                start_date_str = start_date.isoformat()
            except ValueError:
                start_date = None
                start_date_str = ''
        else:
            start_date = None
            start_date_str = ''

        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                end_date_str = end_date.isoformat()
            except ValueError:
                end_date = None
                end_date_str = ''
        else:
            end_date = None
            end_date_str = ''

    base_qs = TreatmentSchedule.objects.select_related(
        'patient', 'patient__primary_consultant', 'machine', 'shift', 'technique'
    ).filter(
        Q(is_emergency_slot=True) | Q(status__in=['Emergency', 'Vacant'])
    )

    selected_machine = None
    if machine_id and machine_id != 'all':
        try:
            m_id = int(machine_id)
            base_qs = base_qs.filter(machine_id=m_id)
            selected_machine = Machine.objects.filter(id=m_id).first()
        except (ValueError, TypeError):
            pass

    if start_date and end_date:
        base_qs = base_qs.filter(
            Q(intended_start_date__range=[start_date, end_date]) | Q(intended_start_date__isnull=True)
        )
    elif start_date:
        base_qs = base_qs.filter(
            Q(intended_start_date__gte=start_date) | Q(intended_start_date__isnull=True)
        )
    elif end_date:
        base_qs = base_qs.filter(
            Q(intended_start_date__lte=end_date) | Q(intended_start_date__isnull=True)
        )

    schedules = base_qs.order_by('intended_start_date', 'machine__display_order', 'patient__pt_name')

    export_fmt = request.GET.get('export')
    if export_fmt == 'excel':
        return export_emergency_slots_excel(schedules)
    elif export_fmt == 'csv':
        return export_emergency_slots_csv(schedules)

    # For Print Modal: Consultant options, designations, and template config
    active_print_cfg = PrintTemplateConfig.get_active()
    consultant_options = get_consultant_signing_options(active_print_cfg.default_signing_authority)
    designations = list(Designation.objects.filter(is_active=True).order_by('display_order', 'name'))

    context = {
        'schedules': schedules,
        'total_count': schedules.count(),
        'all_machines': all_machines,
        'all_shifts': all_shifts,
        'selected_machine': selected_machine,
        'selected_machine_id': machine_id,
        'start_date_str': start_date_str,
        'end_date_str': end_date_str,
        'print_default_start': default_start.isoformat(),
        'print_default_end': default_end.isoformat(),
        'preset': preset,
        'consultant_options': consultant_options,
        'designations': designations,
        'print_template_config': active_print_cfg,
    }
    return render(request, 'reports/emergency_slots.html', context)




def chunk_machine_schedules(schedules, max_first=34, max_other=36):
    """
    Paginates schedules per machine dynamically to maximize page space utilization:
    - If total <= max_first (34 rows): 1 single page, page_label is None (no page count displayed).
      Dynamically accommodates maximum entries on a single page without splitting.
    - If total > max_first: dynamically fills Page 1 up to max_first (34) to accommodate maximum entries
      without leaving large empty gaps, and fills subsequent pages up to max_other (36).
      Multi-page sheets display page_label: e.g. '1/2 pages', '2/2 pages' (displayed in header & footer).
    - Every page comfortably accommodates institutional header, title, table,
      notes, copy-to distribution, and signing authority without any cutoffs.
    """
    total = len(schedules)
    if total <= max_first:
        return [{
            'page_num': 1,
            'total_pages': 1,
            'page_label': None,
            'page_label_short': None,
            'page_label_long': None,
            'is_first': True,
            'is_last': True,
            'schedules': schedules,
        }]

    pages_schedules = []
    cur = 0
    page_idx = 0
    while cur < total:
        cap = max_first if page_idx == 0 else max_other
        chunk = schedules[cur:cur + cap]
        pages_schedules.append(chunk)
        cur += len(chunk)
        page_idx += 1

    num_pages = len(pages_schedules)
    result = []
    for idx, p_scheds in enumerate(pages_schedules):
        page_num = idx + 1
        result.append({
            'page_num': page_num,
            'total_pages': num_pages,
            'page_label': f"{page_num}/{num_pages} pages",
            'page_label_short': f"{page_num}/{num_pages}",
            'page_label_long': f"Page {page_num} of {num_pages}",
            'is_first': (page_num == 1),
            'is_last': (page_num == num_pages),
            'schedules': p_scheds,
        })
    return result


def export_machine_schedule_excel(schedules, machine, start_date, end_date):
    wb = openpyxl.Workbook()
    ws = wb.active
    mach_label = machine.name if machine else "All Machines"
    ws.title = "Treatment Starts"

    # Header Title
    ws.merge_cells('A1:I1')
    ws['A1'] = f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - RADIATION ONCOLOGY"
    ws['A1'].font = Font(name="Calibri", size=14, bold=True, color="003366")
    ws['A1'].alignment = Alignment(horizontal="center")

    ws.merge_cells('A2:I2')
    ws['A2'] = f"Treatment Starts Schedule: {mach_label} | {start_date.strftime('%d-%b-%Y')} to {end_date.strftime('%d-%b-%Y')}"
    ws['A2'].font = Font(name="Calibri", size=11, bold=True)
    ws['A2'].alignment = Alignment(horizontal="center")

    headers = [
        "Date of Start", "Shift", "MR No.", "RT No.", "Patient Name",
        "Diagnosis", "Consultant", "Jr. Resident", "Machine"
    ]
    ws.append([])  # blank row
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for s in schedules:
        ws.append([
            s.date_of_start.strftime("%d-%m-%Y") if s.date_of_start else "N/A",
            s.shift.name if s.shift else "N/A",
            s.patient.mr_no,
            s.patient.rt_no or "",
            s.patient.pt_name,
            s.patient.diagnosis,
            s.patient.primary_consultant.name if s.patient.primary_consultant else "",
            s.jr_resident or "",
            s.machine.name if s.machine else ""
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Treatment_Starts_{mach_label}_{start_date}.xlsx"'
    wb.save(response)
    return response


def export_pending_starts_excel(schedules):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Pending Starts"

    ws.merge_cells('A1:I1')
    ws['A1'] = "KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - PENDING TREATMENT STARTS"
    ws['A1'].font = Font(name="Calibri", size=14, bold=True, color="003366")
    ws['A1'].alignment = Alignment(horizontal="center")

    headers = [
        "MR No.", "Patient Name", "Age/Sex", "Date of Sim", "Diagnosis",
        "Site", "Consultant", "Machine", "Phone"
    ]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="856404", end_color="856404", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=3, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for s in schedules:
        ws.append([
            s.patient.mr_no,
            s.patient.pt_name,
            f"{s.patient.age or ''}/{s.patient.sex}",
            s.date_of_sim.strftime("%d-%m-%Y") if s.date_of_sim else "",
            s.patient.diagnosis,
            s.patient.site_of_treatment,
            s.patient.primary_consultant.name if s.patient.primary_consultant else "",
            s.machine.name if s.machine else "",
            s.patient.phone_no or ""
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Pending_Treatment_Starts_{date.today()}.xlsx"'
    wb.save(response)
    return response


@login_required
def manual_grid_ampersand_redirect(request, query_string=''):
    """
    Fallback redirect for malformed URLs that passed '&' instead of '?' in the path.
    E.g. /reports/manual-grid/&start_date=... -> /reports/manual-grid/?start_date=...
    """
    target = '/reports/manual-grid/'
    if query_string:
        target += f'?{query_string}'
    return redirect(target)


def get_upcoming_monday(d):
    """
    Returns upcoming Monday.
    If d is Monday, returns d.
    If d is Tue-Sun, returns the immediately following Monday.
    """
    days_ahead = (0 - d.weekday()) % 7
    return d + timedelta(days=days_ahead)


def add_six_months(d):
    """
    Returns the date exactly 6 months from date d.
    Clamps day to the last valid day of the target month.
    """
    m = d.month - 1 + 6
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


@login_required
def manual_schedule_grid_view(request):
    """
    Manual Treatment Schedule Editor (Excel Spreadsheet Format).
    Replicates Access Query 'Treatment LA1' (and LA2, LA3, DBX, etc.) with direct
    interactive spreadsheet editing for Treatment Start Date and Shift.
    Opened in a new browser tab.
    Strictly restricted to Admin and SubAdmin.
    No daily patient quota / shift restrictions apply here (manual clinical override mode).
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Manual treatment scheduling is only available to Administrators and SubAdministrators.")
        return redirect('dashboard')

    all_machines = Machine.objects.filter(is_active=True).order_by('display_order')
    all_shifts = Shift.objects.filter(is_active=True).order_by('display_order')

    selected_machine_id = request.GET.get('machine_id')
    selected_machine_code = request.GET.get('machine_code')

    selected_machine = None
    if selected_machine_id:
        selected_machine = Machine.objects.filter(id=selected_machine_id).first()
    elif selected_machine_code:
        selected_machine = Machine.objects.filter(code__iexact=selected_machine_code).first()
    elif 'machine_id' not in request.GET and 'machine_code' not in request.GET:
        # Default to ELEKTA - LA1 (matching user requirement for Treatment LA1)
        selected_machine = all_machines.filter(code__iexact='LA1').first() or all_machines.first()

    today = date.today()
    default_start_date = get_upcoming_monday(today)
    default_end_date = add_six_months(default_start_date)

    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')
    include_pending = request.GET.get('include_pending') == '1'
    preset = request.GET.get('preset')

    if preset == 'this_month':
        start_date = today.replace(day=1)
        next_month = (start_date + timedelta(days=32)).replace(day=1)
        end_date = next_month - timedelta(days=1)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    elif preset == 'next_30':
        start_date = today
        end_date = today + timedelta(days=30)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    elif preset == 'all_2026':
        start_date = date(2026, 1, 1)
        end_date = date(2026, 12, 31)
        start_date_str = start_date.isoformat()
        end_date_str = end_date.isoformat()
    else:
        if start_date_str:
            try:
                start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            except ValueError:
                start_date = default_start_date
                start_date_str = start_date.isoformat()
        else:
            start_date = default_start_date
            start_date_str = start_date.isoformat()

        if end_date_str:
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            except ValueError:
                end_date = add_six_months(start_date)
                end_date_str = end_date.isoformat()
        else:
            end_date = add_six_months(start_date)
            end_date_str = end_date.isoformat()

    base_filter = Q()
    if selected_machine:
        base_filter &= Q(machine=selected_machine)

    if include_pending:
        date_filter = Q(date_of_start__range=[start_date, end_date]) | Q(date_of_start__isnull=True)
    else:
        date_filter = Q(date_of_start__range=[start_date, end_date])

    schedules_qs = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'shift', 'technique', 'special_technique', 'patient__primary_consultant'
    ).filter(base_filter & date_filter).exclude(status__in=['Draft', 'Emergency', 'Vacant']).exclude(is_emergency_slot=True).order_by('date_of_start', 'shift__display_order', 'patient__pt_name')

    # Load holidays
    holidays_qs = Holiday.objects.all().order_by('date')
    holiday_map = {h.date.isoformat(): (h.comment or 'Hospital Holiday') for h in holidays_qs}
    holiday_map_json = json.dumps(holiday_map)
    holiday_dates_dict = {h.date: (h.comment or 'Hospital Holiday') for h in holidays_qs}

    schedules_list = list(schedules_qs)
    for s in schedules_list:
        if s.date_of_start and s.date_of_start in holiday_dates_dict:
            s.is_holiday = True
            s.holiday_comment = holiday_dates_dict[s.date_of_start]
        else:
            s.is_holiday = False
            s.holiday_comment = ''

    # BATCH POST SAVE: Process all modified rows submitted via form
    if request.method == 'POST':
        updated_count = 0
        schedule_ids = request.POST.getlist('schedule_id')
        for sid in schedule_ids:
            sched = TreatmentSchedule.objects.filter(id=sid).first()
            if not sched:
                continue

            row_changed = False
            new_date_str = request.POST.get(f'start_date_{sid}', '').strip()
            new_shift_id = request.POST.get(f'shift_{sid}', '').strip()
            new_mach_id = request.POST.get(f'machine_{sid}', '').strip()

            if new_date_str:
                try:
                    new_date = datetime.strptime(new_date_str, "%Y-%m-%d").date()
                    if sched.date_of_sim and new_date < sched.date_of_sim:
                        messages.error(
                            request,
                            f"Cannot set start date for {sched.patient.pt_name}: Start date ({new_date.strftime('%d-%m-%Y')}) cannot be earlier than CT Simulation Date ({sched.date_of_sim.strftime('%d-%m-%Y')}). Date of start must be >= Date of simulation."
                        )
                        continue
                    if sched.date_of_start != new_date:
                        sched.date_of_start = new_date
                        sched.status = 'Scheduled'
                        row_changed = True
                except ValueError:
                    pass
            else:
                if sched.date_of_start is not None:
                    sched.date_of_start = None
                    sched.status = 'Pending'
                    row_changed = True

            if new_shift_id:
                if str(sched.shift_id or '') != new_shift_id:
                    sched.shift_id = int(new_shift_id)
                    row_changed = True
            else:
                if sched.shift_id is not None:
                    sched.shift = None
                    row_changed = True

            if new_mach_id and str(sched.machine_id or '') != new_mach_id:
                sched.machine_id = int(new_mach_id)
                row_changed = True

            if row_changed:
                # No quota restriction applied!
                sched.save()
                updated_count += 1
                AuditLog.objects.create(
                    user=request.user,
                    action="MANUAL_SCHEDULE_UPDATE",
                    mr_no=str(sched.patient.mr_no),
                    details=f"Manual schedule update for {sched.patient.pt_name} (MR: {sched.patient.mr_no}): Machine={sched.machine_display}, Start Date={sched.date_of_start}, Shift={sched.shift_display}. Manual mode (no quota restrictions).",
                    ip_address=request.META.get('REMOTE_ADDR')
                )

        messages.success(request, f"Successfully updated {updated_count} patient schedule(s) in manual mode!")
        redirect_url = request.path + f"?machine_id={selected_machine.id if selected_machine else ''}&start_date={start_date_str}&end_date={end_date_str}"
        if include_pending:
            redirect_url += "&include_pending=1"
        return redirect(redirect_url)

    # Excel export
    if request.GET.get('export') == 'excel':
        return export_manual_schedule_grid_excel(schedules_qs, selected_machine, start_date, end_date)

    # Print template and signing authority options
    active_print_cfg = PrintTemplateConfig.get_active()
    consultant_options = get_consultant_signing_options(active_print_cfg.default_signing_authority)
    designations = list(Designation.objects.filter(is_active=True).order_by('display_order', 'name'))

    context = {
        'all_machines': all_machines,
        'all_shifts': all_shifts,
        'selected_machine': selected_machine,
        'selected_machine_id': selected_machine.id if selected_machine else None,
        'start_date_str': start_date_str,
        'end_date_str': end_date_str,
        'include_pending': include_pending,
        'schedules': schedules_list,
        'total_count': len(schedules_list),
        'preset': preset,
        'holiday_map': holiday_map,
        'holiday_map_json': holiday_map_json,
        'consultant_options': consultant_options,
        'designations': designations,
        'print_template_config': active_print_cfg,
    }
    return render(request, 'reports/manual_schedule_grid.html', context)


@login_required
def manual_schedule_save_row_api(request):
    """
    AJAX endpoint for instant inline cell editing of date_of_start, shift, and machine.
    Strictly restricted to Admin and SubAdmin.
    NO quota / daily starts restrictions applied.
    """
    if not is_admin_or_subadmin(request.user):
        return JsonResponse({'success': False, 'error': 'Access denied. Admin or SubAdmin only.'}, status=403)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid request method.'}, status=405)

    schedule_id = request.POST.get('schedule_id')
    date_str = request.POST.get('date_of_start', '').strip()
    shift_id = request.POST.get('shift_id', '').strip()
    machine_id = request.POST.get('machine_id', '').strip()

    if not schedule_id:
        return JsonResponse({'success': False, 'error': 'schedule_id is required.'}, status=400)

    sched = get_object_or_404(TreatmentSchedule, id=schedule_id)

    if date_str:
        try:
            new_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            if sched.date_of_sim and new_date < sched.date_of_sim:
                return JsonResponse({
                    'success': False,
                    'error': f"Date of start ({new_date.strftime('%d-%m-%Y')}) cannot be earlier than CT Simulation Date ({sched.date_of_sim.strftime('%d-%m-%Y')}). Date of start must be >= Date of simulation."
                }, status=400)
            sched.date_of_start = new_date
            sched.status = 'Scheduled'
        except ValueError:
            return JsonResponse({'success': False, 'error': 'Invalid date format. Use YYYY-MM-DD.'}, status=400)
    else:
        sched.date_of_start = None
        sched.status = 'Pending'

    if shift_id:
        sched.shift_id = int(shift_id)
    else:
        sched.shift = None

    if machine_id:
        sched.machine_id = int(machine_id)

    # Save with NO quota restrictions!
    sched.save()

    AuditLog.objects.create(
        user=request.user,
        action="MANUAL_SCHEDULE_UPDATE",
        mr_no=str(sched.patient.mr_no),
        details=f"Inline manual schedule update for {sched.patient.pt_name} (MR: {sched.patient.mr_no}): Machine={sched.machine_display}, Start Date={sched.date_of_start}, Shift={sched.shift_display}. Manual mode (no quota restrictions).",
        ip_address=request.META.get('REMOTE_ADDR')
    )

    is_holiday = False
    holiday_comment = ''
    if sched.date_of_start:
        h_obj = Holiday.objects.filter(date=sched.date_of_start).first()
        if h_obj:
            is_holiday = True
            holiday_comment = h_obj.comment or 'Hospital Holiday'

    return JsonResponse({
        'success': True,
        'schedule_id': sched.id,
        'patient_mr': sched.patient.mr_no,
        'date_of_start': sched.date_of_start.isoformat() if sched.date_of_start else '',
        'shift_name': sched.shift_display,
        'machine_name': sched.machine_display,
        'status': sched.status,
        'is_holiday': is_holiday,
        'holiday_comment': holiday_comment,
    })


def export_manual_schedule_grid_excel(schedules, machine, start_date, end_date):
    wb = openpyxl.Workbook()
    ws = wb.active
    mach_label = machine.name if machine else "All Machines"
    ws.title = "Treatment Grid"

    # Header Title
    ws.merge_cells('A1:K1')
    ws['A1'] = "KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - RADIATION ONCOLOGY"
    ws['A1'].font = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
    ws['A1'].fill = PatternFill(start_color="107C41", end_color="107C41", fill_type="solid")
    ws['A1'].alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells('A2:K2')
    ws['A2'] = f"Manual Treatment Schedule Worksheet: {mach_label} | {start_date.strftime('%d-%b-%Y')} to {end_date.strftime('%d-%b-%Y')} | Total: {schedules.count()} Patients"
    ws['A2'].font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    ws['A2'].fill = PatternFill(start_color="0E6333", end_color="0E6333", fill_type="solid")
    ws['A2'].alignment = Alignment(horizontal="center", vertical="center")

    # Column order: MR No, Patient Name, Simulation Date, RT No, Diagnosis, Start Date, Shift, Machine, Consultant Name, Jr Resident
    headers = [
        "#", "MR No.", "Patient Name", "Simulation Date", "RT No.", "Diagnosis",
        "Start Date", "Shift", "Machine", "Consultant Name", "Jr Resident"
    ]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="107C41", end_color="107C41", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    idx = 1
    for s in schedules:
        sim_date_str = s.date_of_sim.strftime("%d-%m-%Y") if s.date_of_sim else "-"
        ws.append([
            idx,
            s.patient.mr_no,
            s.patient.pt_name,
            sim_date_str,
            s.patient.rt_no or "-",
            s.patient.diagnosis or "-",
            s.date_of_start.strftime("%d-%m-%Y") if s.date_of_start else "Pending",
            s.shift_display,
            s.machine_display,
            s.patient.consultant_display,
            s.jr_resident or "-"
        ])
        idx += 1

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 11)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    safe_mach = mach_label.replace(' ', '_').replace('-', '_')
    response['Content-Disposition'] = f'attachment; filename="Treatment_Schedule_{safe_mach}_{start_date}_{end_date}.xlsx"'
    wb.save(response)
    return response


@login_required
def audit_log_report_view(request):
    """
    Security & Clinical Audit Logs Report.
    Strictly restricted to Admin (not SubAdmin).
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted to Administrators. SubAdmins and clinical staff do not have permission to view security audit logs.")
        return redirect('dashboard')

    query = request.GET.get('q', '').strip()
    action_filter = request.GET.get('action', '').strip()
    start_date_str = request.GET.get('start_date', '').strip()
    end_date_str = request.GET.get('end_date', '').strip()

    logs = AuditLog.objects.select_related('user').all().order_by('-log_datetime')

    if query:
        logs = logs.filter(
            Q(mr_no__icontains=query) |
            Q(details__icontains=query) |
            Q(user__username__icontains=query) |
            Q(user__first_name__icontains=query) |
            Q(user__last_name__icontains=query)
        )

    if action_filter:
        logs = logs.filter(action=action_filter)

    if start_date_str:
        try:
            sd = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            logs = logs.filter(log_datetime__date__gte=sd)
        except ValueError:
            pass

    if end_date_str:
        try:
            ed = datetime.strptime(end_date_str, "%Y-%m-%d").date()
            logs = logs.filter(log_datetime__date__lte=ed)
        except ValueError:
            pass

    distinct_actions = AuditLog.objects.values_list('action', flat=True).distinct().order_by('action')

    paginator = Paginator(logs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'query': query,
        'action_filter': action_filter,
        'start_date_str': start_date_str,
        'end_date_str': end_date_str,
        'distinct_actions': distinct_actions,
        'total_logs': logs.count(),
    }
    return render(request, 'reports/audit_logs.html', context)


# ==============================================================================
# EXPORT HELPERS: CONSULTANT STATS, UNIT STATS, SIMULATIONS, EMERGENCY SLOTS
# ==============================================================================

def export_consultant_stats_excel(data_rows, months, month_totals, grand_total, year):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Consultants {year}"

    ws.merge_cells('A1:N1')
    ws['A1'] = f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - CONSULTANT STARTS ({year})"
    ws['A1'].font = Font(name="Calibri", size=13, bold=True, color="003366")
    ws['A1'].alignment = Alignment(horizontal="center")

    headers = ["Primary Consultant"] + months + ["Total"]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=3, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center" if col_num > 1 else "left")

    for r in data_rows:
        ws.append([r['consultant']] + r['counts'] + [r['total']])

    ws.append(["Monthly Totals"] + month_totals + [grand_total])
    total_row_idx = len(data_rows) + 4
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row_idx, column=col_num)
        cell.font = Font(name="Calibri", size=10, bold=True, color="003366")
        cell.fill = PatternFill(start_color="E2EDFA", end_color="E2EDFA", fill_type="solid")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 8)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Consultant_Starts_{year}.xlsx"'
    wb.save(response)
    return response


def export_consultant_stats_csv(data_rows, months, month_totals, grand_total, year):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="Consultant_Starts_{year}.csv"'
    writer = csv.writer(response)
    writer.writerow([f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - CONSULTANT STARTS ({year})"])
    writer.writerow([])
    writer.writerow(["Primary Consultant"] + months + ["Total"])
    for r in data_rows:
        writer.writerow([r['consultant']] + r['counts'] + [r['total']])
    writer.writerow(["Monthly Totals"] + month_totals + [grand_total])
    return response


def export_unit_stats_excel(data_rows, months, month_totals, grand_total, year):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Unit Starts {year}"

    ws.merge_cells('A1:N1')
    ws['A1'] = f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - CLINICAL UNIT STARTS ({year})"
    ws['A1'].font = Font(name="Calibri", size=13, bold=True, color="003366")
    ws['A1'].alignment = Alignment(horizontal="center")

    headers = ["Clinical Unit"] + months + ["Total"]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=3, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center" if col_num > 1 else "left")

    for r in data_rows:
        ws.append([r['unit']] + r['counts'] + [r['total']])

    ws.append(["Monthly Totals"] + month_totals + [grand_total])
    total_row_idx = len(data_rows) + 4
    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=total_row_idx, column=col_num)
        cell.font = Font(name="Calibri", size=10, bold=True, color="003366")
        cell.fill = PatternFill(start_color="E2EDFA", end_color="E2EDFA", fill_type="solid")

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 10)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Unit_Starts_{year}.xlsx"'
    wb.save(response)
    return response


def export_unit_stats_csv(data_rows, months, month_totals, grand_total, year):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="Unit_Starts_{year}.csv"'
    writer = csv.writer(response)
    writer.writerow([f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - CLINICAL UNIT STARTS ({year})"])
    writer.writerow([])
    writer.writerow(["Clinical Unit"] + months + ["Total"])
    for r in data_rows:
        writer.writerow([r['unit']] + r['counts'] + [r['total']])
    writer.writerow(["Monthly Totals"] + month_totals + [grand_total])
    return response


def export_simulation_report_excel(schedules, start_sim, end_sim):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Simulations"

    ws.merge_cells('A1:L1')
    ws['A1'] = f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - PATIENTS SIMULATED BETWEEN DATES"
    ws['A1'].font = Font(name="Calibri", size=13, bold=True, color="003366")
    ws['A1'].alignment = Alignment(horizontal="center")

    ws.merge_cells('A2:L2')
    ws['A2'] = f"Simulation Period: {start_sim.strftime('%d-%b-%Y')} to {end_sim.strftime('%d-%b-%Y')} | Total: {schedules.count()} Patients"
    ws['A2'].font = Font(name="Calibri", size=11, bold=True)
    ws['A2'].alignment = Alignment(horizontal="center")

    headers = [
        "MR No.", "Patient Name", "Age/Sex", "Unit", "Simulation Date",
        "RT No.", "Diagnosis", "Site of Treatment", "Consultant", "Machine",
        "Start Date", "Status"
    ]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="003366", end_color="003366", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=4, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center" if col_num in (1, 3, 4, 5, 6, 11, 12) else "left")

    for s in schedules:
        ws.append([
            s.patient.mr_no,
            s.patient.pt_name,
            f"{s.patient.age or ''}/{s.patient.sex}",
            s.patient.unit or "I",
            s.date_of_sim.strftime("%d-%m-%Y") if s.date_of_sim else "-",
            s.patient.rt_no or "-",
            s.patient.diagnosis,
            s.site_of_treatment or s.patient.site_of_treatment or "-",
            s.patient.primary_consultant.name if s.patient.primary_consultant else "-",
            s.machine.name if s.machine else "Not Assigned",
            s.date_of_start.strftime("%d-%m-%Y") if s.date_of_start else "Pending",
            s.status
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 10)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Simulations_{start_sim}_to_{end_sim}.xlsx"'
    wb.save(response)
    return response


def export_simulation_report_csv(schedules, start_sim, end_sim):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="Simulations_{start_sim}_to_{end_sim}.csv"'
    writer = csv.writer(response)
    writer.writerow([f"KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - PATIENTS SIMULATED ({start_sim} to {end_sim})"])
    writer.writerow([])
    writer.writerow([
        "MR No.", "Patient Name", "Age/Sex", "Unit", "Simulation Date",
        "RT No.", "Diagnosis", "Site of Treatment", "Consultant", "Machine",
        "Start Date", "Status"
    ])
    for s in schedules:
        writer.writerow([
            s.patient.mr_no,
            s.patient.pt_name,
            f"{s.patient.age or ''}/{s.patient.sex}",
            s.patient.unit or "I",
            s.date_of_sim.strftime("%d-%m-%Y") if s.date_of_sim else "-",
            s.patient.rt_no or "-",
            s.patient.diagnosis,
            s.site_of_treatment or s.patient.site_of_treatment or "-",
            s.patient.primary_consultant.name if s.patient.primary_consultant else "-",
            s.machine.name if s.machine else "Not Assigned",
            s.date_of_start.strftime("%d-%m-%Y") if s.date_of_start else "Pending",
            s.status
        ])
    return response


def export_emergency_slots_excel(schedules):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Emergency Slots"

    ws.merge_cells('A1:J1')
    ws['A1'] = "KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - EMERGENCY & VACANT SLOTS QUEUE"
    ws['A1'].font = Font(name="Calibri", size=13, bold=True, color="991B1B")
    ws['A1'].alignment = Alignment(horizontal="center")

    headers = [
        "MR No.", "Patient Name", "Classification", "Intended Start",
        "Emergency Indication / Reason", "Machine", "Shift", "Consultant",
        "Unit", "Status"
    ]
    ws.append([])
    ws.append(headers)

    header_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="B91C1C", end_color="B91C1C", fill_type="solid")

    for col_num in range(1, len(headers) + 1):
        cell = ws.cell(row=3, column=col_num)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center" if col_num in (1, 3, 4, 7, 9, 10) else "left")

    for s in schedules:
        ws.append([
            s.patient.mr_no,
            s.patient.pt_name,
            s.slot_category or s.status,
            s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else "TBD",
            s.emergency_reason or "Emergency Indication",
            s.machine.name if s.machine else "Unassigned",
            s.shift.name if s.shift else "-",
            s.patient.primary_consultant.name if s.patient.primary_consultant else "-",
            s.patient.unit or "I",
            s.status
        ])

    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 10)

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = f'attachment; filename="Emergency_Slots_{date.today()}.xlsx"'
    wb.save(response)
    return response


def export_emergency_slots_csv(schedules):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="Emergency_Slots_{date.today()}.csv"'
    writer = csv.writer(response)
    writer.writerow(["KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY - EMERGENCY & VACANT SLOTS QUEUE"])
    writer.writerow([])
    writer.writerow([
        "MR No.", "Patient Name", "Classification", "Intended Start",
        "Emergency Indication / Reason", "Machine", "Shift", "Consultant",
        "Unit", "Status"
    ])
    for s in schedules:
        writer.writerow([
            s.patient.mr_no,
            s.patient.pt_name,
            s.slot_category or s.status,
            s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else "TBD",
            s.emergency_reason or "Emergency Indication",
            s.machine.name if s.machine else "Unassigned",
            s.shift.name if s.shift else "-",
            s.patient.primary_consultant.name if s.patient.primary_consultant else "-",
            s.patient.unit or "I",
            s.status
        ])
    return response


# ==============================================================================
# RADIATION THERAPY TECHNICIAN INFORMATION SHEET (PRINT & PDF EXPORT)
# ==============================================================================

def get_print_schedule_data(request):
    """
    Shared helper to query and prepare schedule records for printing and PDF generation.
    Filters by Modality (Teletherapy / Brachytherapy), Start Date, End Date, and Selected Machines.
    """
    config = PrintTemplateConfig.get_active()

    modality = request.GET.get('modality', 'Teletherapy')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    signing_authority = request.GET.get('signing_authority')
    if not signing_authority or not signing_authority.strip():
        signing_authority = config.default_signing_authority
    signing_authority = resolve_signing_authority(signing_authority)

    designation = request.GET.get('designation')
    if not designation or not designation.strip():
        designation = config.default_designation
    else:
        designation = designation.strip()

    raw_copy_lines = [line.strip() for line in config.copy_to_recipients.strip().splitlines() if line.strip()]
    copy_to_list = []
    copy_to_list_bare = []
    for idx, line in enumerate(raw_copy_lines):
        bare_line = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
        copy_to_list_bare.append(bare_line)
        copy_to_list.append(f"{idx + 1}. {bare_line}")

    today = date.today()
    default_start = get_upcoming_monday(today)
    default_end = add_six_months(default_start)

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            start_date = default_start
    else:
        start_date = default_start

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            end_date = default_end
    else:
        end_date = default_end

    machine_ids = request.GET.getlist('machines')
    if len(machine_ids) == 1 and ',' in machine_ids[0]:
        machine_ids = [m.strip() for m in machine_ids[0].split(',') if m.strip()]

    if machine_ids:
        selected_machines = list(Machine.objects.filter(id__in=machine_ids, is_active=True).order_by('display_order'))
    else:
        selected_machines = list(Machine.objects.filter(is_active=True).order_by('display_order'))

    schedules_qs = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'shift', 'technique', 'special_technique', 'patient__primary_consultant'
    ).filter(
        machine__in=selected_machines,
        date_of_start__range=[start_date, end_date]
    )

    if modality:
        schedules_qs = schedules_qs.filter(treatment_type=modality)

    schedules_qs = schedules_qs.exclude(
        status__in=['Draft', 'Emergency', 'Vacant']
    ).exclude(
        is_emergency_slot=True
    ).order_by('date_of_start', 'shift__display_order', 'patient__pt_name')

    all_schedules = list(schedules_qs)

    sheets = []
    for m in selected_machines:
        m_schedules = [s for s in all_schedules if s.machine_id == m.id]
        m_pages = chunk_machine_schedules(m_schedules)
        
        # Annotate schedules for HTML display & date merging
        for page in m_pages:
            cur_d = None
            grp_start_s = None
            grp_count = 0
            for s in page['schedules']:
                d_str = s.date_of_start.strftime("%d-%b-%y") if s.date_of_start else '-'
                s.formatted_date = d_str
                s.patient_name_title = format_title_case(s.patient.pt_name)
                s.diagnosis_title = format_title_case(s.patient.diagnosis)
                s.consultant_name_title = s.patient.primary_consultant.name if s.patient.primary_consultant else '-'
                st_val = ''
                if s.special_technique and s.special_technique.name.strip().lower() not in ['none', '', '-']:
                    st_val = s.special_technique.name.strip()
                s.special_technique_display = st_val
                if d_str == cur_d:
                    grp_count += 1
                    s.date_rowspan = 0
                else:
                    if grp_start_s is not None:
                        grp_start_s.date_rowspan = grp_count
                    cur_d = d_str
                    grp_start_s = s
                    grp_count = 1
                    s.date_rowspan = 1
            if grp_start_s is not None:
                grp_start_s.date_rowspan = grp_count

        sheets.append({
            'machine': m,
            'box_title': m.name,
            'schedules': m_schedules,
            'count': len(m_schedules),
            'pages': m_pages,
            'total_pages': len(m_pages),
        })

    now_dt = datetime.now()
    return {
        'modality': modality,
        'start_date': start_date,
        'end_date': end_date,
        'start_date_str': start_date.isoformat(),
        'end_date_str': end_date.isoformat(),
        'start_date_formatted': f"{start_date.day}/{start_date.month}/{start_date.strftime('%y')}",
        'end_date_formatted': f"{end_date.day}/{end_date.month}/{end_date.strftime('%y')}",
        'selected_machines': selected_machines,
        'sheets': sheets,
        'total_patients': len(all_schedules),
        'today_str': today.strftime("%d-%m-%Y"),
        'now_time_str': now_dt.strftime("%H:%M"),
        'now_date_str': now_dt.strftime("%d %B %Y"),
        'config': config,
        'signing_authority': signing_authority,
        'designation': designation,
        'copy_to_list': copy_to_list,
        'copy_to_list_bare': copy_to_list_bare,
    }


def ensure_calibri_registered():
    """
    Registers Calibri and Calibri-Bold fonts in ReportLab if available.
    Returns (font_regular, font_bold): ('Calibri', 'Calibri-Bold') if registered,
    or ('Helvetica', 'Helvetica-Bold') as safe fallback.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    from django.conf import settings

    if 'Calibri' in pdfmetrics.getRegisteredFontNames() and 'Calibri-Bold' in pdfmetrics.getRegisteredFontNames():
        return ('Calibri', 'Calibri-Bold')

    search_dirs = [
        os.path.join(os.path.dirname(__file__), 'fonts'),
        os.path.join(settings.BASE_DIR, 'apps', 'reports', 'fonts'),
        os.path.join(settings.BASE_DIR, 'static', 'fonts'),
        'C:/Windows/Fonts',
        'C:\\Windows\\Fonts',
        '/usr/share/fonts/truetype/msttcorefonts',
        '/usr/share/fonts/truetype',
    ]

    regular_path = None
    bold_path = None
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        reg = os.path.join(d, 'calibri.ttf')
        reg_cap = os.path.join(d, 'Calibri.ttf')
        bld = os.path.join(d, 'calibrib.ttf')
        bld_cap = os.path.join(d, 'CalibriB.ttf')

        target_reg = reg if os.path.exists(reg) else (reg_cap if os.path.exists(reg_cap) else None)
        target_bld = bld if os.path.exists(bld) else (bld_cap if os.path.exists(bld_cap) else None)

        if target_reg and not regular_path:
            regular_path = target_reg
        if target_bld and not bold_path:
            bold_path = target_bld

    if regular_path:
        try:
            if 'Calibri' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('Calibri', regular_path))
            if 'Calibri-Bold' not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont('Calibri-Bold', bold_path or regular_path))
            return ('Calibri', 'Calibri-Bold')
        except Exception:
            return ('Helvetica', 'Helvetica-Bold')

    return ('Helvetica', 'Helvetica-Bold')


def generate_technician_sheet_pdf(data):
    """
    Generates an official KMIO Radiation Therapy Technician Information Sheet PDF
    using ReportLab, matching the layout, typography, and sign-offs of the institutional template.
    Layout, texts, notes, and margins are fully configured via PrintTemplateConfig.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfgen import canvas
    import io

    config = data.get('config') or PrintTemplateConfig.get_active()
    buffer = io.BytesIO()

    margin_cm = config.page_margin_cm or 2.5
    margin_25mm = margin_cm * cm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin_25mm,
        rightMargin=margin_25mm,
        topMargin=margin_25mm,
        bottomMargin=margin_25mm
    )

    canvas_pages_meta = []
    for sheet in data['sheets']:
        for page in sheet['pages']:
            canvas_pages_meta.append({
                'page_label': page['page_label'],
                'box_title': sheet['box_title'],
            })

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for page_idx, state in enumerate(self._saved_page_states):
                self.__dict__.update(state)
                self.saveState()
                self.setFont('Helvetica', 7.5)
                self.setFillColor(colors.HexColor('#222222'))
                self.drawString(margin_25mm, 1.4 * cm, f"Report Generated on:   {data['now_time_str']}    {data['now_date_str']}")
                if page_idx < len(canvas_pages_meta):
                    lbl = canvas_pages_meta[page_idx].get('page_label')
                    if lbl:
                        self.drawRightString(A4[0] - margin_25mm, 1.4 * cm, lbl)
                self.restoreState()
                super().showPage()
            super().save()

    tbl_font, tbl_font_bold = ensure_calibri_registered()

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('KMIO_Title', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, alignment=1, leading=13)
    dept_style = ParagraphStyle('KMIO_Dept', parent=styles['Normal'], fontName='Helvetica-BoldOblique', fontSize=10, alignment=1, leading=12)
    sub_style = ParagraphStyle('KMIO_Sub', parent=styles['Normal'], fontName='Helvetica-Oblique', fontSize=8.5, alignment=1, leading=10)
    box_style = ParagraphStyle('KMIO_Box', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, alignment=1, leading=12)
    left_ref_style = ParagraphStyle('KMIO_LeftRef', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=10)
    date_style = ParagraphStyle('KMIO_Date', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, alignment=2, leading=10)
    page_num_style = ParagraphStyle('KMIO_PageNum', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, alignment=2, leading=9, textColor=colors.HexColor('#334155'))

    intro_style = ParagraphStyle('KMIO_Intro', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=9.5, alignment=4)
    sec_title_style = ParagraphStyle('KMIO_SecTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12, alignment=1)

    th_style = ParagraphStyle('KMIO_TH', parent=styles['Normal'], fontName=tbl_font_bold, fontSize=6.8, leading=8.5, alignment=0)

    note_title_style = ParagraphStyle('KMIO_NoteTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, leading=9)
    note_text_style = ParagraphStyle('KMIO_NoteText', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8.5, alignment=4)

    copy_style = ParagraphStyle('KMIO_Copy', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8.5)
    copy_bold = ParagraphStyle('KMIO_CopyB', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7, leading=9)
    sig_name = ParagraphStyle('KMIO_SigName', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9.5, leading=11, alignment=2)
    sig_title = ParagraphStyle('KMIO_SigTitle', parent=styles['Normal'], fontName='Helvetica-BoldOblique', fontSize=8, leading=10, alignment=2)
    sig_dept = ParagraphStyle('KMIO_SigDept', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, alignment=2)

    story = []
    col_widths = [35, 14, 44, 24, 60, 40, 96, 20, 68, 52.5]

    is_first_page_of_doc = True
    for sheet in data['sheets']:
        for page in sheet['pages']:
            if not is_first_page_of_doc:
                story.append(PageBreak())
            is_first_page_of_doc = False

            # Header block (Repeated on each page)
            right_cell = [
                Paragraph(data['today_str'], date_style),
                Spacer(1, 3),
                Table([[Paragraph(sheet['box_title'], box_style)]], colWidths=[96], style=[
                    ('BOX', (0,0), (-1,-1), 1.2, colors.black),
                    ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                    ('TOPPADDING', (0,0), (-1,-1), 3),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                    ('LEFTPADDING', (0,0), (-1,-1), 2),
                    ('RIGHTPADDING', (0,0), (-1,-1), 2),
                ])
            ]
            if page['page_label']:
                right_cell.append(Spacer(1, 2))
                right_cell.append(Paragraph(page['page_label'], page_num_style))

            header_data = [
                [
                    Paragraph(config.ref_code_prefix, left_ref_style),
                    [
                        Paragraph(config.header_line_1, title_style),
                        Spacer(1, 1),
                        Paragraph(config.header_line_2, dept_style),
                        Spacer(1, 1),
                        Paragraph(config.header_line_3, sub_style)
                    ],
                    right_cell
                ]
            ]
            ht = Table(header_data, colWidths=[100, 253.5, 100])
            ht.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 0),
                ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ]))
            story.append(ht)
            story.append(Spacer(1, 4))

            # Narrative paragraph (page 1 of machine)
            if page['is_first']:
                story.append(Paragraph(config.intro_paragraph, intro_style))
                story.append(Spacer(1, 4))

            # Table section title
            base_title = config.table_section_title.format(
                start_date=data['start_date_formatted'],
                end_date=data['end_date_formatted']
            )
            sec_title = base_title if page['is_first'] else f"{base_title} (Contd.)"
            story.append(Paragraph(sec_title, sec_title_style))
            story.append(Spacer(1, 4))

            # 10-Column Table: Title Casing on Patient name and Diagnosis, Star for Special Technique, Primary Consultant
            th_star_style = ParagraphStyle('KMIO_TH_Star', parent=th_style, alignment=1)
            table_rows = [
                [
                    Paragraph('Date', th_style),
                    Paragraph('Shift', th_style),
                    Paragraph('Hospital no.', th_style),
                    Paragraph('RT No.', th_style),
                    Paragraph('Patient name', th_style),
                    Paragraph('Phone No.', th_style),
                    Paragraph('Diagnosis', th_style),
                    Paragraph('★', th_star_style),
                    Paragraph('Primary Consultant', th_style),
                    Paragraph('Junior Resident', th_style),
                ]
            ]

            if page['schedules']:
                for s in page['schedules']:
                    d_str = s.date_of_start.strftime("%d-%b-%y") if s.date_of_start else '-'
                    sh_str = str(s.shift.name if s.shift else '-').strip()
                    mr_str = str(s.patient.mr_no)
                    rt_str = str(s.patient.rt_no or '-')
                    pt_str = format_title_case(s.patient.pt_name).replace('\n', ' ').strip()[:18]
                    ph_str = str(s.patient.phone_no or '-')
                    diag_str = format_title_case(s.patient.diagnosis).replace('\n', ' ').strip()[:28]
                    st_str = ''
                    if s.special_technique and s.special_technique.name.strip().lower() not in ['none', '', '-']:
                        st_str = s.special_technique.name.strip()
                    cons_str = str(s.patient.primary_consultant.name if s.patient.primary_consultant else '-').replace('\n', ' ').strip()[:20]
                    jr_str = str(s.jr_resident or '-').replace('\n', ' ').strip()[:15]

                    table_rows.append([
                        d_str, sh_str, mr_str, rt_str, pt_str, ph_str, diag_str, st_str, cons_str, jr_str
                    ])
            else:
                table_rows.append([
                    f"No new patient starts scheduled for {sheet['machine'].name} between {data['start_date_formatted']} and {data['end_date_formatted']}.",
                    '', '', '', '', '', '', '', '', ''
                ])

            v_pad = config.row_vertical_padding_pt or 2.7
            pt_table = Table(table_rows, colWidths=col_widths)
            ts = [
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#cbd5e1')),
                ('ALIGN', (0,0), (-1,0), 'LEFT'),
                ('ALIGN', (7,0), (7,-1), 'CENTER'),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#94a3b8')),
                ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#64748b')),
                ('FONTNAME', (0,1), (-1,-1), tbl_font),
                ('FONTSIZE', (0,1), (-1,-1), 6.6),
                ('LEADING', (0,1), (-1,-1), 8),
                ('TOPPADDING', (0,0), (-1,-1), v_pad),
                ('BOTTOMPADDING', (0,0), (-1,-1), v_pad),
                ('LEFTPADDING', (0,0), (-1,-1), 2),
                ('RIGHTPADDING', (0,0), (-1,-1), 2),
            ]
            if not page['schedules']:
                ts.append(('SPAN', (0,1), (-1,1)))
                ts.append(('ALIGN', (0,1), (-1,1), 'CENTER'))
                ts.append(('TOPPADDING', (0,1), (-1,1), 10))
                ts.append(('BOTTOMPADDING', (0,1), (-1,1), 10))
            else:
                # Vertical merge for contiguous identical dates
                date_spans = []
                cur_d = None
                start_r = None
                for idx, s in enumerate(page['schedules']):
                    r_idx = idx + 1
                    d_str = s.date_of_start.strftime("%d-%b-%y") if s.date_of_start else '-'
                    if d_str != cur_d:
                        if cur_d is not None and start_r is not None and (r_idx - 1) > start_r:
                            date_spans.append((start_r, r_idx - 1))
                        cur_d = d_str
                        start_r = r_idx
                if cur_d is not None and start_r is not None and len(page['schedules']) > start_r:
                    date_spans.append((start_r, len(page['schedules'])))

                for (s_row, e_row) in date_spans:
                    ts.append(('SPAN', (0, s_row), (0, e_row)))
                    ts.append(('VALIGN', (0, s_row), (0, e_row), 'MIDDLE'))
                    ts.append(('ALIGN', (0, s_row), (0, e_row), 'CENTER'))
                    for r in range(s_row + 1, e_row + 1):
                        table_rows[r][0] = ''

                for r_idx in range(1, len(table_rows)):
                    if r_idx % 2 == 0:
                        ts.append(('BACKGROUND', (0, r_idx), (-1, r_idx), colors.HexColor('#f8fafc')))

            pt_table.setStyle(TableStyle(ts))
            story.append(pt_table)

            # Append Notes & Signatures on EVERY page
            story.append(Spacer(1, 6))
            note_p1 = config.note_paragraph_1
            note_p2 = config.note_paragraph_2

            copy_to_flowables = [Paragraph('<b>Copy to:</b>', copy_bold)]
            for c_item in data.get('copy_to_list', []):
                copy_to_flowables.append(Paragraph(c_item, copy_style))

            sig_name_val = resolve_signing_authority(data.get('signing_authority') or config.default_signing_authority)
            sig_desig_val = data.get('designation') or config.default_designation
            sig_flowables = [
                Paragraph(sig_name_val, sig_name),
                Paragraph(sig_desig_val, sig_title),
                Paragraph(config.department_signature_line, sig_dept),
            ]

            bottom_table = Table([[copy_to_flowables, sig_flowables]], colWidths=[253.5, 200])
            bottom_table.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'BOTTOM'),
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 0),
                ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ]))

            footer_elements = [
                Paragraph('Please note:', note_title_style),
                Spacer(1, 1),
                Paragraph(note_p1, note_text_style),
                Spacer(1, 2),
                Paragraph(note_p2, note_text_style),
                Spacer(1, 6),
                bottom_table
            ]
            story.append(KeepTogether(footer_elements))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


@login_required
def print_treatment_schedule_view(request):
    """
    Renders browser-optimised HTML print view of the Radiation Therapy Technician Information Sheet.
    Includes print toolbar and @media print page styling.
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Printing treatment schedules is only available to clinical administrators.")
        return redirect('dashboard')
    data = get_print_schedule_data(request)
    return render(request, 'reports/print_technician_sheet.html', data)


@login_required
def print_treatment_schedule_pdf_view(request):
    """
    Streams official KMIO Radiation Therapy Technician Information Sheet PDF download.
    Generated via ReportLab with pixel-perfect fidelity to the institutional template.
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Printing treatment schedules is only available to clinical administrators.")
        return redirect('dashboard')
    data = get_print_schedule_data(request)
    pdf_bytes = generate_technician_sheet_pdf(data)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    start_tag = data['start_date_str']
    end_tag = data['end_date_str']
    response['Content-Disposition'] = f'inline; filename="KMIO_Technician_Information_Sheet_{start_tag}_to_{end_tag}.pdf"'
    return response


@login_required
def print_template_settings_view(request):
    """
    Dedicated view for Administrators to configure institutional printout headers,
    introductory instructions, section titles, clinical notes, copy-to distribution list,
    default signing authority and designations, and layout margins/spacing.
    Strictly restricted to Admin role.
    """
    if not is_admin_only(request.user):
        messages.error(request, "Access restricted. Only Administrators can configure printout templates, layout and texts.")
        return redirect('dashboard')

    config = PrintTemplateConfig.get_active()
    active_template = request.GET.get('template') or request.POST.get('active_template') or 'regular'
    active_template = active_template.strip().lower()
    if active_template not in ['regular', 'emergency']:
        active_template = 'regular'

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset_defaults':
            config.ref_code_prefix = 'KMIO/DRO/        /20___'
            config.header_line_1 = 'KIDWAI MEMORIAL INSTITUTE OF ONCOLOGY'
            config.header_line_2 = 'Department of Radiation Oncology'
            config.header_line_3 = 'Radiation Therapy Technician information sheet'
            config.emergency_header_line_3 = 'Radiation Therapy Technician information sheet'
            config.intro_paragraph = (
                "The scheduled patient lists are handed over to Chief Radiotherapy Technologist RTTs posted in the "
                "respective machines to start radiotherapy for the new patients as scheduled in the table below. "
                "The following are the list of the new patients for whom radiotherapy is to be started on the respective "
                "dates. RTTs are required to perf the necessary procedures as per instructions on the RT chart, "
                "including setup and verification"
            )
            config.table_section_title = "New Patient starts from {start_date} to {end_date}"
            config.emergency_intro_paragraph = (
                "The scheduled patient lists are handed over to Chief Radiotherapy Technologists and RTTs posted in the "
                "respective machines to start radiotherapy for the new patients as scheduled in the table below. "
                "The following are the list of the patients for whom radiotherapy is to be started on an emergency basis on the respective "
                "dates. RTTs are required to perofrm the necessary procedures as per instructions on the RT chart, "
                "including setup and verification"
            )
            config.emergency_table_title = "Emergency/Vacant Slot list"
            config.note_paragraph_1 = (
                "Both the Consultant Radiation oncologist and the Radiation Physicist should have signed and dated the "
                "RT Chart and ensure that informed written consent should be signed by the patient and his/her attendant."
            )
            config.note_paragraph_2 = (
                "The Radiation Oncologist and Physicist are requested to attend to new patients setup as per schedule in each shift. "
                "Time allocation for starting of new patients on RT to be utilised efficiently without disruption to ongoing treatments in the respective machines"
            )
            config.copy_to_recipients = (
                "1. HOD Physics/Faculty concerned for setup verification\n"
                "2. HOU RT I/II/III/ Faculty concerned for setup inspection and verification\n"
                "3. Chief Radiotherapy technologist\n"
                "4. RTT of concerned shift to start the RT for patients mentioned in the above list\n"
                "5. Machine Notice board"
            )
            config.default_signing_authority = "Dr. Naveen T"
            config.default_designation = "Professor and Head of the Department"
            config.department_signature_line = "Department of Radiation Oncology"
            config.page_margin_cm = 2.5
            config.row_vertical_padding_pt = 2.7
            config.save()

            AuditLog.objects.create(
                user=request.user,
                action="PRINT_TEMPLATE_RESET",
                details="Reset printout template, headers, and layout to institutional defaults",
                ip_address=request.META.get('REMOTE_ADDR')
            )
            messages.success(request, "Printout template and layout reset to institutional defaults successfully!")
            return redirect(f"{reverse('print_template_settings')}?template={active_template}")

        # Normal Save
        config.ref_code_prefix = request.POST.get('ref_code_prefix', config.ref_code_prefix).strip()
        config.header_line_1 = request.POST.get('header_line_1', config.header_line_1).strip()
        config.header_line_2 = request.POST.get('header_line_2', config.header_line_2).strip()
        config.header_line_3 = request.POST.get('header_line_3', config.header_line_3).strip()
        config.emergency_header_line_3 = request.POST.get('emergency_header_line_3', config.emergency_header_line_3).strip()
        config.intro_paragraph = request.POST.get('intro_paragraph', config.intro_paragraph).strip()
        config.table_section_title = request.POST.get('table_section_title', config.table_section_title).strip()
        config.emergency_intro_paragraph = request.POST.get('emergency_intro_paragraph', config.emergency_intro_paragraph).strip()
        config.emergency_table_title = request.POST.get('emergency_table_title', config.emergency_table_title).strip()
        config.note_paragraph_1 = request.POST.get('note_paragraph_1', config.note_paragraph_1).strip()
        config.note_paragraph_2 = request.POST.get('note_paragraph_2', config.note_paragraph_2).strip()
        config.copy_to_recipients = request.POST.get('copy_to_recipients', config.copy_to_recipients).strip()
        config.default_signing_authority = resolve_signing_authority(request.POST.get('default_signing_authority', config.default_signing_authority).strip())
        config.default_designation = request.POST.get('default_designation', config.default_designation).strip()
        config.department_signature_line = request.POST.get('department_signature_line', config.department_signature_line).strip()
        try:
            config.page_margin_cm = float(request.POST.get('page_margin_cm', 2.5))
        except (ValueError, TypeError):
            config.page_margin_cm = 2.5
        try:
            config.row_vertical_padding_pt = float(request.POST.get('row_vertical_padding_pt', 2.7))
        except (ValueError, TypeError):
            config.row_vertical_padding_pt = 2.7

        config.save()

        AuditLog.objects.create(
            user=request.user,
            action="PRINT_TEMPLATE_UPDATE",
            details=f"Updated printout template ({active_template}) headers, layout ({config.page_margin_cm}cm, {config.row_vertical_padding_pt}pt) and texts",
            ip_address=request.META.get('REMOTE_ADDR')
        )
        template_label = "Regular Scheduler" if active_template == 'regular' else "Emergency Slot"
        messages.success(request, f"Printout template texts and layout for '{template_label}' successfully saved!")
        return redirect(f"{reverse('print_template_settings')}?template={active_template}")

    # Build consultant choices (real person names only) + Designations
    consultant_options = get_consultant_signing_options(config.default_signing_authority)
    designations = list(Designation.objects.filter(is_active=True).order_by('display_order', 'name'))

    context = {
        'config': config,
        'consultant_options': consultant_options,
        'designations': designations,
        'active_template': active_template,
    }
    return render(request, 'reports/print_template_settings.html', context)


# ==============================================================================
# EMERGENCY & VACANT SLOT SCHEDULE (PRINT & PDF EXPORT)
# ==============================================================================

def get_print_emergency_schedule_data(request):
    """
    Shared helper to query and prepare Emergency / Vacant Slot records for printing and PDF generation.
    Supports:
    - 'All machines consolidated' (first & default): single consolidated sheet of all emergency patients, labeled 'All Machines'.
    - Individual machine selections: separate sheet per selected machine.
    - Start Date & End Date filtering on intended_start_date.
    - Modality (Teletherapy / Brachytherapy).
    - Institutional headers, signing authority, and designation.
    """
    config = PrintTemplateConfig.get_active()

    modality = request.GET.get('modality', 'Teletherapy')
    start_date_str = request.GET.get('start_date')
    end_date_str = request.GET.get('end_date')

    signing_authority = request.GET.get('signing_authority')
    if not signing_authority or not signing_authority.strip():
        signing_authority = config.default_signing_authority
    signing_authority = resolve_signing_authority(signing_authority)

    designation = request.GET.get('designation')
    if not designation or not designation.strip():
        designation = config.default_designation
    else:
        designation = designation.strip()

    raw_copy_lines = [line.strip() for line in config.copy_to_recipients.strip().splitlines() if line.strip()]
    copy_to_list = []
    copy_to_list_bare = []
    for idx, line in enumerate(raw_copy_lines):
        bare_line = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
        copy_to_list_bare.append(bare_line)
        copy_to_list.append(f"{idx + 1}. {bare_line}")

    today = date.today()
    default_start = get_upcoming_monday(today)
    default_end = add_six_months(default_start)

    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except ValueError:
            start_date = default_start
    else:
        start_date = default_start

    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            end_date = default_end
    else:
        end_date = default_end

    machine_param_list = request.GET.getlist('machines')
    if len(machine_param_list) == 1 and ',' in machine_param_list[0]:
        machine_param_list = [m.strip() for m in machine_param_list[0].split(',') if m.strip()]

    # If 'all' or 'consolidated' is selected or nothing selected or machine_id in ['all', 'consolidated'], it is consolidated!
    is_consolidated = ('all' in machine_param_list) or ('consolidated' in machine_param_list) or (not machine_param_list) or (request.GET.get('machine_id') in ['all', 'consolidated'])

    schedules_qs = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'shift', 'technique', 'patient__primary_consultant'
    ).filter(
        Q(is_emergency_slot=True) | Q(status__in=['Emergency', 'Vacant'])
    )

    if modality:
        schedules_qs = schedules_qs.filter(treatment_type=modality)

    if start_date and end_date:
        schedules_qs = schedules_qs.filter(
            Q(intended_start_date__range=[start_date, end_date]) | Q(intended_start_date__isnull=True)
        )

    schedules_qs = schedules_qs.order_by(
        'intended_start_date', 'machine__display_order', 'patient__pt_name'
    )

    all_schedules = list(schedules_qs)

    sheets = []
    if is_consolidated:
        m_pages = chunk_machine_schedules(all_schedules)
        # Annotate schedules for HTML display & date merging
        for page in m_pages:
            cur_d = None
            grp_start_s = None
            grp_count = 0
            for s in page['schedules']:
                d_str = s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else '-'
                s.formatted_date = d_str
                s.patient_name_title = format_title_case(s.patient.pt_name)
                s.diagnosis_title = format_title_case(s.patient.diagnosis)
                s.consultant_name_title = s.patient.primary_consultant.name if s.patient.primary_consultant else '-'
                if d_str == cur_d:
                    grp_count += 1
                    s.date_rowspan = 0
                else:
                    if grp_start_s is not None:
                        grp_start_s.date_rowspan = grp_count
                    cur_d = d_str
                    grp_start_s = s
                    grp_count = 1
                    s.date_rowspan = 1
            if grp_start_s is not None:
                grp_start_s.date_rowspan = grp_count

        sheets.append({
            'is_consolidated': True,
            'machine': None,
            'box_title': 'All Machines',
            'schedules': all_schedules,
            'count': len(all_schedules),
            'pages': m_pages,
            'total_pages': len(m_pages),
        })
    else:
        selected_machines = list(Machine.objects.filter(id__in=machine_param_list, is_active=True).order_by('display_order'))
        for m in selected_machines:
            m_schedules = [s for s in all_schedules if s.machine_id == m.id]
            m_pages = chunk_machine_schedules(m_schedules)
            for page in m_pages:
                cur_d = None
                grp_start_s = None
                grp_count = 0
                for s in page['schedules']:
                    d_str = s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else '-'
                    s.formatted_date = d_str
                    s.patient_name_title = format_title_case(s.patient.pt_name)
                    s.diagnosis_title = format_title_case(s.patient.diagnosis)
                    s.consultant_name_title = s.patient.primary_consultant.name if s.patient.primary_consultant else '-'
                    if d_str == cur_d:
                        grp_count += 1
                        s.date_rowspan = 0
                    else:
                        if grp_start_s is not None:
                            grp_start_s.date_rowspan = grp_count
                        cur_d = d_str
                        grp_start_s = s
                        grp_count = 1
                        s.date_rowspan = 1
                if grp_start_s is not None:
                    grp_start_s.date_rowspan = grp_count

            sheets.append({
                'is_consolidated': False,
                'machine': m,
                'box_title': m.name,
                'schedules': m_schedules,
                'count': len(m_schedules),
                'pages': m_pages,
                'total_pages': len(m_pages),
            })

    now_dt = datetime.now()
    return {
        'modality': modality,
        'start_date': start_date,
        'end_date': end_date,
        'start_date_str': start_date.isoformat(),
        'end_date_str': end_date.isoformat(),
        'start_date_formatted': f"{start_date.day}/{start_date.month}/{start_date.strftime('%y')}",
        'end_date_formatted': f"{end_date.day}/{end_date.month}/{end_date.strftime('%y')}",
        'is_consolidated': is_consolidated,
        'sheets': sheets,
        'total_patients': len(all_schedules),
        'today_str': today.strftime("%d-%m-%Y"),
        'now_time_str': now_dt.strftime("%H:%M"),
        'now_date_str': now_dt.strftime("%d %B %Y"),
        'config': config,
        'signing_authority': signing_authority,
        'designation': designation,
        'copy_to_list': copy_to_list,
        'copy_to_list_bare': copy_to_list_bare,
    }


def generate_emergency_sheet_pdf(data):
    """
    Generates official KMIO Emergency / Vacant Slot list PDF matching the institutional
    template (Template_-_EmergencySlotReport.pdf):
    - 7 Columns: Start Date, Hospital no, Patient name, Diagnosis, Primary Consultant, Machine, Jr. Resident.
    - A4 size with 2.5 cm margins on all sides.
    - Consolidated mode: top-right machine box says 'All Machines'.
    - Machine mode: individual machine sheets with top-right machine box.
    - Vertical date merging for contiguous identical dates.
    - Title Casing on Patient name and Diagnosis.
    - Date range displayed in section title: 'Emergency/Vacant Slot list from {start_date} to {end_date}'.
    - Institutional headers, please note, copy-to distribution, and signing authority on EVERY page.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.pdfgen import canvas
    import io

    config = data.get('config') or PrintTemplateConfig.get_active()
    buffer = io.BytesIO()

    margin_cm = config.page_margin_cm or 2.5
    margin_25mm = margin_cm * cm
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin_25mm,
        rightMargin=margin_25mm,
        topMargin=margin_25mm,
        bottomMargin=margin_25mm
    )

    canvas_pages_meta = []
    for sheet in data['sheets']:
        for page in sheet['pages']:
            canvas_pages_meta.append({
                'page_label': page['page_label'],
                'box_title': sheet['box_title'],
            })

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved_page_states = []

        def showPage(self):
            self._saved_page_states.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            for page_idx, state in enumerate(self._saved_page_states):
                self.__dict__.update(state)
                self.saveState()
                self.setFont('Helvetica', 7.5)
                self.setFillColor(colors.HexColor('#222222'))
                self.drawString(margin_25mm, 1.4 * cm, f"Report Generated on:   {data['now_time_str']}    {data['now_date_str']}")
                if page_idx < len(canvas_pages_meta):
                    lbl = canvas_pages_meta[page_idx].get('page_label')
                    if lbl:
                        self.drawRightString(A4[0] - margin_25mm, 1.4 * cm, lbl)
                self.restoreState()
                super().showPage()
            super().save()

    tbl_font, tbl_font_bold = ensure_calibri_registered()

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('KMIO_Title', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, alignment=1, leading=13)
    dept_style = ParagraphStyle('KMIO_Dept', parent=styles['Normal'], fontName='Helvetica-BoldOblique', fontSize=10, alignment=1, leading=12)
    sub_style = ParagraphStyle('KMIO_Sub', parent=styles['Normal'], fontName='Helvetica-Oblique', fontSize=8.5, alignment=1, leading=10)
    box_style = ParagraphStyle('KMIO_Box', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9.5, alignment=1, leading=11)
    left_ref_style = ParagraphStyle('KMIO_LeftRef', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, leading=10)
    date_style = ParagraphStyle('KMIO_Date', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8.5, alignment=2, leading=10)
    page_num_style = ParagraphStyle('KMIO_PageNum', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, alignment=2, leading=9, textColor=colors.HexColor('#334155'))

    intro_style = ParagraphStyle('KMIO_Intro', parent=styles['Normal'], fontName='Helvetica', fontSize=7, leading=9.5, alignment=4)
    sec_title_style = ParagraphStyle('KMIO_SecTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=10, leading=12, alignment=1)

    th_style = ParagraphStyle('KMIO_TH', parent=styles['Normal'], fontName=tbl_font_bold, fontSize=6.8, leading=8.5, alignment=0)

    note_title_style = ParagraphStyle('KMIO_NoteTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7.5, leading=9)
    note_text_style = ParagraphStyle('KMIO_NoteText', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8.5, alignment=4)

    copy_style = ParagraphStyle('KMIO_Copy', parent=styles['Normal'], fontName='Helvetica', fontSize=6.5, leading=8.5)
    copy_bold = ParagraphStyle('KMIO_CopyB', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=7, leading=9)
    sig_name = ParagraphStyle('KMIO_SigName', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=9.5, leading=11, alignment=2)
    sig_title = ParagraphStyle('KMIO_SigTitle', parent=styles['Normal'], fontName='Helvetica-BoldOblique', fontSize=8, leading=10, alignment=2)
    sig_dept = ParagraphStyle('KMIO_SigDept', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=8, leading=10, alignment=2)

    story = []
    # Usable width: 453.5 pt on A4 with 2.5cm margins
    col_widths = [50, 52, 72, 98, 80, 55, 46.5]

    is_first_page_of_doc = True
    for sheet in data['sheets']:
        for page in sheet['pages']:
            if not is_first_page_of_doc:
                story.append(PageBreak())
            is_first_page_of_doc = False

            # Header block (Repeated on each page, ALWAYS including top-right machine box)
            right_cell = [
                Paragraph(data['today_str'], date_style),
                Spacer(1, 3),
                Table([[Paragraph(sheet['box_title'], box_style)]], colWidths=[96], style=[
                    ('BOX', (0,0), (-1,-1), 1.2, colors.black),
                    ('ALIGN', (0,0), (-1,-1), 'CENTER'),
                    ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                    ('TOPPADDING', (0,0), (-1,-1), 3),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                    ('LEFTPADDING', (0,0), (-1,-1), 2),
                    ('RIGHTPADDING', (0,0), (-1,-1), 2),
                ])
            ]
            if page['page_label']:
                right_cell.append(Spacer(1, 2))
                right_cell.append(Paragraph(page['page_label'], page_num_style))

            header_data = [
                [
                    Paragraph(config.ref_code_prefix, left_ref_style),
                    [
                        Paragraph(config.header_line_1, title_style),
                        Spacer(1, 1),
                        Paragraph(config.header_line_2, dept_style),
                        Spacer(1, 1),
                        Paragraph(config.emergency_header_line_3 or config.header_line_3, sub_style)
                    ],
                    right_cell
                ]
            ]
            ht = Table(header_data, colWidths=[100, 253.5, 100])
            ht.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 0),
                ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ]))
            story.append(ht)
            story.append(Spacer(1, 4))

            # Narrative paragraph (page 1 of machine)
            if page['is_first']:
                intro_text = config.emergency_intro_paragraph or config.intro_paragraph
                story.append(Paragraph(intro_text, intro_style))
                story.append(Spacer(1, 4))

            # Table section title with date range
            em_title_raw = config.emergency_table_title or "Emergency/Vacant Slot list"
            if '{start_date}' in em_title_raw:
                base_title = em_title_raw.format(start_date=data['start_date_formatted'], end_date=data['end_date_formatted'])
            else:
                base_title = f"{em_title_raw} from {data['start_date_formatted']} to {data['end_date_formatted']}"
            sec_title = base_title if page['is_first'] else f"{base_title} (Contd.)"
            story.append(Paragraph(sec_title, sec_title_style))
            story.append(Spacer(1, 4))

            # 7-Column Table: Title Casing on Patient name and Diagnosis
            table_rows = [
                [
                    Paragraph('Start Date', th_style),
                    Paragraph('Hospital no', th_style),
                    Paragraph('Patient name', th_style),
                    Paragraph('Diagnosis', th_style),
                    Paragraph('Primary Consultant', th_style),
                    Paragraph('Machine', th_style),
                    Paragraph('Jr. Resident', th_style),
                ]
            ]

            if page['schedules']:
                for s in page['schedules']:
                    d_str = s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else '-'
                    mr_str = str(s.patient.mr_no)
                    pt_str = format_title_case(s.patient.pt_name).replace('\n', ' ').strip()[:18]
                    diag_str = format_title_case(s.patient.diagnosis).replace('\n', ' ').strip()[:28]
                    cons_str = str(s.patient.primary_consultant.name if s.patient.primary_consultant else '-').replace('\n', ' ').strip()[:20]
                    mach_str = str(s.machine.name if s.machine else 'Unassigned').replace('\n', ' ').strip()[:14]
                    jr_str = str(s.jr_resident or '-').replace('\n', ' ').strip()[:14]

                    table_rows.append([
                        d_str, mr_str, pt_str, diag_str, cons_str, mach_str, jr_str
                    ])
            else:
                label = "Emergency / Vacant slot queue" if sheet.get('is_consolidated') else sheet['machine'].name
                table_rows.append([
                    f"No patients currently assigned to {label} between {data['start_date_formatted']} and {data['end_date_formatted']}.",
                    '', '', '', '', '', ''
                ])

            v_pad = config.row_vertical_padding_pt or 2.7
            pt_table = Table(table_rows, colWidths=col_widths)
            ts = [
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#cbd5e1')),
                ('ALIGN', (0,0), (-1,0), 'LEFT'),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
                ('INNERGRID', (0,0), (-1,-1), 0.5, colors.HexColor('#94a3b8')),
                ('BOX', (0,0), (-1,-1), 1, colors.HexColor('#64748b')),
                ('FONTNAME', (0,1), (-1,-1), tbl_font),
                ('FONTSIZE', (0,1), (-1,-1), 6.6),
                ('LEADING', (0,1), (-1,-1), 8),
                ('TOPPADDING', (0,0), (-1,-1), v_pad),
                ('BOTTOMPADDING', (0,0), (-1,-1), v_pad),
                ('LEFTPADDING', (0,0), (-1,-1), 2),
                ('RIGHTPADDING', (0,0), (-1,-1), 2),
            ]
            if not page['schedules']:
                ts.append(('SPAN', (0,1), (-1,1)))
                ts.append(('ALIGN', (0,1), (-1,1), 'CENTER'))
                ts.append(('TOPPADDING', (0,1), (-1,1), 10))
                ts.append(('BOTTOMPADDING', (0,1), (-1,1), 10))
            else:
                # Vertical merge for contiguous identical dates
                date_spans = []
                cur_d = None
                start_r = None
                for idx, s in enumerate(page['schedules']):
                    r_idx = idx + 1
                    d_str = s.intended_start_date.strftime("%d-%m-%Y") if s.intended_start_date else '-'
                    if d_str != cur_d:
                        if cur_d is not None and start_r is not None and (r_idx - 1) > start_r:
                            date_spans.append((start_r, r_idx - 1))
                        cur_d = d_str
                        start_r = r_idx
                if cur_d is not None and start_r is not None and len(page['schedules']) > start_r:
                    date_spans.append((start_r, len(page['schedules'])))

                for (s_row, e_row) in date_spans:
                    ts.append(('SPAN', (0, s_row), (0, e_row)))
                    ts.append(('VALIGN', (0, s_row), (0, e_row), 'MIDDLE'))
                    ts.append(('ALIGN', (0, s_row), (0, e_row), 'CENTER'))
                    for r in range(s_row + 1, e_row + 1):
                        table_rows[r][0] = ''

                for r_idx in range(1, len(table_rows)):
                    if r_idx % 2 == 0:
                        ts.append(('BACKGROUND', (0, r_idx), (-1, r_idx), colors.HexColor('#f8fafc')))

            pt_table.setStyle(TableStyle(ts))
            story.append(pt_table)

            # Append Notes & Signatures on EVERY page
            story.append(Spacer(1, 6))
            note_p1 = config.note_paragraph_1
            note_p2 = config.note_paragraph_2

            copy_to_flowables = [Paragraph('<b>Copy to:</b>', copy_bold)]
            for c_item in data.get('copy_to_list', []):
                copy_to_flowables.append(Paragraph(c_item, copy_style))

            sig_name_val = resolve_signing_authority(data.get('signing_authority') or config.default_signing_authority)
            sig_desig_val = data.get('designation') or config.default_designation
            sig_flowables = [
                Paragraph(sig_name_val, sig_name),
                Paragraph(sig_desig_val, sig_title),
                Paragraph(config.department_signature_line, sig_dept),
            ]

            bottom_table = Table([[copy_to_flowables, sig_flowables]], colWidths=[253.5, 200])
            bottom_table.setStyle(TableStyle([
                ('VALIGN', (0,0), (-1,-1), 'BOTTOM'),
                ('LEFTPADDING', (0,0), (-1,-1), 0),
                ('RIGHTPADDING', (0,0), (-1,-1), 0),
                ('TOPPADDING', (0,0), (-1,-1), 0),
                ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ]))

            footer_elements = [
                Paragraph('Please note:', note_title_style),
                Spacer(1, 1),
                Paragraph(note_p1, note_text_style),
                Spacer(1, 2),
                Paragraph(note_p2, note_text_style),
                Spacer(1, 6),
                bottom_table
            ]
            story.append(KeepTogether(footer_elements))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


@login_required
def print_emergency_schedule_view(request):
    """
    Browser-optimised HTML print view of the Emergency / Vacant Slot list.
    Supports consolidated list across all machines or individual machine sheets.
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Printing emergency schedules is only available to clinical administrators.")
        return redirect('dashboard')
    data = get_print_emergency_schedule_data(request)
    return render(request, 'reports/print_emergency_sheet.html', data)


@login_required
def print_emergency_schedule_pdf_view(request):
    """
    Streams official KMIO Emergency/Vacant Slot list PDF download.
    Generated via ReportLab with pixel-perfect 7-column fidelity to institutional template.
    """
    if not is_admin_or_subadmin(request.user):
        messages.error(request, "Access restricted. Printing emergency schedules is only available to clinical administrators.")
        return redirect('dashboard')
    data = get_print_emergency_schedule_data(request)
    pdf_bytes = generate_emergency_sheet_pdf(data)
    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    start_tag = data['start_date_str']
    end_tag = data['end_date_str']
    response['Content-Disposition'] = f'inline; filename="KMIO_Emergency_Slot_List_{start_tag}_to_{end_tag}.pdf"'
    return response


