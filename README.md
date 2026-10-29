# Sanalogx — Hospital Pharmacy Stock Optimization

> **Tagline:** "Smarter Pharmacy. Optimized Stock. Better Care."  
> **Secondary Tagline:** "Intelligent Hospital Pharmacy Stock Optimization Platform"

---

## Project Overview
**Sanalogx** is a real-world healthcare SaaS & pharmacy inventory intelligence platform designed to eliminate stock shortages, prevent overstocking, automate expiry tracking, and analyze supplier price trends.

---

## Technology Stack
- **Backend:** Python 3.12+, Django 5.x, Django REST Framework, Gunicorn
- **Database:** MySQL 8.x, `mysqlclient` & `pymysql`
- **AI & Analytics:** Pandas, NumPy, Scikit-learn, Joblib
- **Background Tasks:** Celery, Redis
- **Reporting & Barcode:** ReportLab, OpenPyXL, `qrcode`, `python-barcode`
- **API Documentation:** `drf-spectacular` (OpenAPI 3.0 / Swagger UI)
- **Frontend & Design:** HTML5, CSS3 (Healthcare Light Theme), JavaScript ES6+, Bootstrap 5, Chart.js, Font Awesome 6, SweetAlert2

---

## Application Roles & Access Control
Sanalogx strictly enforces **two application roles**:

1. **Admin**
   - Governance role created via Django superuser mechanism.
   - Accesses restricted Admin Login at `/auth/admin-login/`.
   - Manages Pharmacist registration approvals, vendor price intelligence, and system configurations.
   - *No public registration.*

2. **Pharmacist**
   - Public registration at `/auth/register/`.
   - Registration status initializes to **`PENDING`**.
   - Requires explicit Admin review & approval before gaining login access to the Pharmacist portal.

---

## Initial Project Structure
```
Sanalogx/
├── backend/
│   ├── venv/
│   ├── config/
│   │   ├── settings.py
│   │   ├── urls.py
│   │   ├── asgi.py
│   │   └── wsgi.py
│   ├── accounts/          # Custom User model (Admin & Pharmacist roles, Pending/Approved/Rejected workflows)
│   ├── medicines/         # Medicine master & catalog
│   ├── inventory/         # Stock & batch management
│   ├── suppliers/         # Vendor & supplier comparison
│   ├── purchases/         # Purchase orders & procurement
│   ├── sales/             # Prescription sales & billing
│   ├── analytics/         # Inventory valuation & demand analytics
│   ├── ai_engine/         # Demand forecasting & stock risk models
│   ├── notifications/     # Expiry & reorder alert notifications
│   ├── reports/           # PDF & Excel report generators
│   ├── templates/         # UI templates (Landing page, auth pages, admin approval portal)
│   ├── static/            # Healthcare light CSS theme & JS modules
│   ├── media/             # Document & medicine image uploads
│   ├── manage.py
│   ├── requirements.txt
│   └── .env               # Environment variables (DB, secrets, Redis)
├── frontend/
├── .gitignore
└── README.md
```

---

## Quick Setup & Verification

### 1. Activate Environment & Dependencies
```bash
cd Sanalogx/backend
.\venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Database & Migrations
Ensure MySQL 8.x is running and credentials are set in `backend/.env`.
```bash
python manage.py makemigrations
python manage.py migrate
```

### 3. Run Development Server
```bash
python manage.py runserver 127.0.0.1:8000
```
- **Public Home Page:** [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Pharmacist Login:** [http://127.0.0.1:8000/auth/login/](http://127.0.0.1:8000/auth/login/)
- **Pharmacist Register:** [http://127.0.0.1:8000/auth/register/](http://127.0.0.1:8000/auth/register/)
- **Admin Login:** [http://127.0.0.1:8000/auth/admin-login/](http://127.0.0.1:8000/auth/admin-login/)
- **API Documentation (Swagger UI):** [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/)
