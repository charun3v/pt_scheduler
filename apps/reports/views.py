from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.db.models import Count, Q
from django.utils import timezone
from datetime import date, datetime, timedelta
import calendar
import json
import csv
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from apps.scheduler.models import Machine, TreatmentSchedule, Shift, Holiday
from apps.patients.models import Patient
from apps.core.models import Consultant, AuditLog
from apps.core.permissions import is_admin_or_subadmin, is_admin_only
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
    """
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
    Supports CSV and Excel export.
    """
    year = int(request.GET.get('year', date.today().year))
    available_years = list(range(2020, date.today().year + 2))

    schedules = TreatmentSchedule.objects.filter(
        date_of_start__year=year
    ).exclude(status__in=['Draft', 'Emergency', 'Vacant']).exclude(is_emergency_slot=True).values(
        'patient__unit',
        'date_of_start__month'
    ).annotate(total=Count('id'))

    known_units = list(Patient.objects.exclude(unit__isnull=True).exclude(unit='').values_list('unit', flat=True).distinct())
    if not known_units:
        known_units = ['I', 'II', 'III']
    else:
        for u in ['I', 'II', 'III']:
            if u not in known_units:
                known_units.append(u)
    known_units.sort()

    months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    matrix = {u: {m: 0 for m in range(1, 13)} for u in known_units}

    for row in schedules:
        u_val = row['patient__unit'] or 'I'
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
    Supports CSV and Excel export.
    """
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


@login_required
def emergency_slots_report_view(request):
    """
    Emergency & Vacant Slots Queue.
    Lists all patients assigned to Emergency or Vacant slot filling.
    Allows viewing indication, intended start date, and management.
    """
    schedules = TreatmentSchedule.objects.select_related(
        'patient', 'patient__primary_consultant', 'machine', 'shift', 'technique'
    ).filter(
        Q(is_emergency_slot=True) | Q(status__in=['Emergency', 'Vacant'])
    ).order_by('-intended_start_date', '-created_at')

    export_fmt = request.GET.get('export')
    if export_fmt == 'excel':
        return export_emergency_slots_excel(schedules)
    elif export_fmt == 'csv':
        return export_emergency_slots_csv(schedules)

    context = {
        'schedules': schedules,
        'total_count': schedules.count(),
    }
    return render(request, 'reports/emergency_slots.html', context)


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

