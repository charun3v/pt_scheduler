# Radiotherapy Patient Scheduler — Django Web Application

Radiation Therapy Patient Scheduling & Machine Allocation System for the **Department of Radiation Oncology, Kidwai Memorial Institute of Oncology (KMIO)**.

Migrated from MS Access (`EBRT scheduler - FE.accdb` and `EBRT scheduler - BE.accdb`) to a high-performance Django web application with Role-Based Access Control (RBAC), dynamic machine-to-shift mappings, two-stage registration-to-scheduling workflow, configurable field validations, and an automated free slot recommendation engine.

---

## 1. Key Features & Clinical Workflows

### 🛡️ Role-Based Access Control (RBAC)
- **Roles**: `Admin`, `SubAdmin`, `Consultant`, `Resident`, `Front Office`, `Physics`.
- **Admin Exclusive Control**:
  - **Configurable Field Validations (`/validations/`)**: Admin (not SubAdmin) configures exact character lengths, numeric constraints, or minimum/maximum ranges for `MR Number` and `Phone Number`.
  - **Gynae-Style Centralized Reference & Lookup Manager (`/lookups/`)**: Admin (not SubAdmin) adds, edits, and inactivates dropdown reference options (Machines, Shifts, Techniques, Special Techniques, Sites, Consultants, Simulated For) with active/inactive toggles and default options.
- **Admin & SubAdmin Shared Control**:
  - Can modify locked patient identity fields (`MR Number` and `Patient Name`).
  - Can configure dynamic Machine $\leftrightarrow$ Shift $\leftrightarrow$ Technique mappings and operating parameters.
  - Can manage user accounts and roles.
- **Clinical Users (Consultants, Residents, Front Office)**:
  - Can register patients, search, and book schedules.
  - Cannot alter locked MR Number or Patient Name once registered.

### 📋 Two-Stage Patient Lifecycle
1. **Stage 1: Patient Registration (`/patients/register/`)**:
   - Captures strictly: `Patient Name`, `MR No.` (unique, validated), `RT No.`, `Age`, `Sex`, `Diagnosis`, `Primary Consultant`, `Phone Number` (validated).
   - `Site of Treatment` has been removed from Registration and moved to Scheduling.
   - Once registered, `MR No` and `Patient Name` are permanently locked for non-admins.
2. **Stage 2: Treatment Scheduling (`/scheduler/patient/<id>/`)**:
   - Can be performed immediately after registration, or later from the **Unscheduled Queue**.
   - **Top Card**: Displays registration details (read-only except `Age`, `Diagnosis`, `Site of Treatment`, and `Phone Number` which can be edited inline and saved).
   - **Scheduling Fields**: Captures `Site of Treatment`, `Simulation Date`, `Machine`, `Shift`, `Technique`, `Special Technique`, `Dose planned (Gy)`, `Number of fractions`, `Junior Resident`, `Simulated For`, `Date of Treatment Start`, and `Notes`.

### ⚙️ Dynamic Machine Mappings & Capacity Formula
- Accessible to Admin & SubAdmin at `/scheduler/mappings/`.
- Dynamically maps each machine with:
  - Allowed Operating Shifts (e.g. Shift I, Shift II, Shift III).
  - Allowed Delivery Techniques (e.g. 2D, 3D CRT, IMRT, VMAT, SBRT, SRS, SRT).
  - Allowed Special Techniques (e.g. DIBH, TBI, TSET, SRS, SBRT, RPM Gating, Stereotactic).
  - Allowed Anatomical Sites.
  - **Capacity Rule**: **Maximum number of new patients allowed for auto scheduling = number of active shifts**.
    *(e.g., if Shift III is not operating for a machine, its auto-scheduling capacity automatically updates from 3 to 2 starts/day).*

### 🔍 Nearest Available Free Slot Recommendation Engine (Option A)
- Evaluates future calendar dates (skipping Sundays).
- Automatically calculates daily machine capacity based on active mapped shifts count.
- When a specific shift is requested, checks that no new start is already booked for that shift (1 start per shift) and machine daily capacity has not been reached.
- Recommends the earliest available date with remaining capacity, plus alternative available dates.
- 1-click **"Apply This Date"** populates the start date field.

### 🗄️ Gynae-Style Centralized Lookup Manager
- Modeled directly after the KMIO Gynae database lookup system.
- Tabbed/Sidebar categories: Machines, Shifts, Techniques, Special Techniques, Sites, Consultants, Simulated For.
- Add, Edit, and Inactivate options with display orders and defaults. Inactivating an option immediately hides it from new clinical entries while preserving historical patient treatment integrity.

---

## 2. Default Login Credentials

| Username | Password | Role | Access Scope |
| :--- | :--- | :--- | :--- |
| **`admin`** | *(Same as Gynae database)* | **Admin** | Full system control, validations, lookup tables |
| **`n3v`** | *(Same as Gynae database)* | **Admin** | Full system control, mappings, lookups |
| **`subadmin`** | `subadmin123` | **SubAdmin** | Administrative control, machine mappings, locked edits |
| **`vlokesh`** | `pass` | Consultant | Clinical registration & scheduling |
| **`thejaswini`**| `pass` | Consultant | Clinical registration & scheduling |
| **`naveent`** | `pass` | Consultant | Clinical registration & scheduling |
| **`siddannap`** | `pass` | Consultant | Clinical registration & scheduling |

---

## 3. How to Run the Application

1. Double-click **`run_server.bat`** (or `Start_Radiotherapy_Scheduler.bat`).
2. The browser will open automatically at:
   `http://127.0.0.1:8000/`
3. Log in with `admin` / your password or your clinical account.
