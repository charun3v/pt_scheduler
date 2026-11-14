from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.db.models import Count, Q
from django.utils import timezone
from datetime import date, datetime, timedelta
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from apps.scheduler.models import Machine, TreatmentSchedule, Shift
from apps.patients.models import Patient
from apps.core.models import Consultant


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
    ).filter(date_of_start__range=[start_date, end_date])

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
    start_sim_str = request.GET.get('start_sim')
    end_sim_str = request.GET.get('end_sim')

    query = TreatmentSchedule.objects.select_related(
        'patient', 'machine', 'technique', 'patient__primary_consultant'
    ).filter(date_of_start__isnull=True)

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

    context = {
        'year': year,
        'available_years': available_years,
        'months': months,
        'data_rows': data_rows,
        'month_totals': [month_totals[m] for m in range(1, 13)],
        'grand_total': grand_total,
    }
    return render(request, 'reports/consultant_stats.html', context)


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
