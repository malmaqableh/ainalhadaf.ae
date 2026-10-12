<!-- Build prompt for the Ain Alhadaf ERP. Replace the [BRACKETED] fields before use. -->

# ROLE
You are a senior full-stack architect and lead Laravel engineer who has shipped production ERP systems for companies in the UAE and the GCC. You are also a product designer who builds premium admin interfaces. You write clean, secure, tested, maintainable code, and you explain key decisions briefly.

# PROJECT
Build "Ain Alhadaf ERP": a complete, production-ready ERP platform for Ain Alhadaf (عين الهدف), a company in the United Arab Emirates. The company's public website is https://ainalhadaf.ae. The ERP will run the company's daily operations from a single admin panel.

# COMPANY CONTEXT
- What the company does: [DESCRIBE WHAT AIN ALHADAF DOES: industry, customer type]
- Main products / services: [LIST MAIN PRODUCTS / SERVICES]
- Locations / branches: [EMIRATES / BRANCHES]
- Expected users / departments: [NUMBER OF USERS AND DEPARTMENTS]
- Brand colors: [BRAND COLORS FROM THE LOGO / WEBSITE]
- Base currency: AED (multi-currency supported)
- Languages: Arabic and English, with full RTL support
- Timezone: Asia/Dubai

# STEP 0: REVIEW THE WEBSITE FIRST
Before writing any code, review https://ainalhadaf.ae and give me a short summary of:
1. What the company sells or does, and who its customers are.
2. Every form, inquiry flow or data point on the site that the ERP should receive (contact forms, quote requests, bookings, job applications, newsletter sign-ups).
3. The brand identity: logo, colors, fonts and tone. The admin panel must feel like part of the same brand.
4. Which modules below matter most for this business, and which can wait for a later phase.
If you cannot access the website, say so and ask me for the details. Do not guess.

# TECH STACK
- Backend: latest stable Laravel, PHP 8.3+, MySQL 8
- Admin UI: Blade + Livewire 3 + Alpine.js + Tailwind CSS (if you recommend an alternative, justify it first)
- Charts: ApexCharts
- Data tables: server-side pagination, sorting, filtering, column toggles, saved views, bulk actions, export to Excel / PDF / CSV
- Auth and access: secure login with 2FA (TOTP), spatie/laravel-permission for roles and permissions, spatie/laravel-activitylog for the audit trail
- Queues: Redis + Laravel Horizon; scheduled jobs with the Laravel scheduler
- Files: Laravel filesystem (local + S3-compatible) with spatie/laravel-medialibrary
- PDF: a generator that shapes Arabic text correctly (for example mPDF or Browsershot) for invoices, payslips and reports
- API: REST API with Laravel Sanctum for the website and a future mobile app
- Quality: Pest tests, Laravel Pint, Larastan
- Dev environment: Docker / Laravel Sail, with a README that covers setup

# CORE MODULES
1. Dashboard: live KPIs for revenue, expenses, profit, cash position, receivables and payables aging, sales pipeline, low stock, pending approvals and expiring employee documents. Role-based widgets, date-range filters and comparison with the previous period.
2. CRM: leads (created automatically from ainalhadaf.ae forms through the API), contacts, companies, a Kanban pipeline, activities, follow-up reminders, lead-source tracking and conversion reports.
3. Sales: quotation → sales order → delivery note → tax invoice → payment → credit note. Price lists, discounts, approval rules, recurring invoices and customer statements.
4. Purchasing: suppliers, purchase requests, RFQs, purchase orders, goods received notes, supplier bills, payments and supplier performance.
5. Inventory and warehouses: product and service catalog, units, variants, barcodes, multiple warehouses, stock movements, transfers, adjustments, reorder levels, batch / serial / expiry tracking, and stock valuation (FIFO or weighted average).
6. Accounting and finance: chart of accounts, double-entry journal, general ledger, AR / AP, bank and cash accounts, bank reconciliation, expenses, cost centers, fixed assets and depreciation, exchange rates, trial balance, profit and loss, balance sheet, cash flow and period closing.
7. UAE tax: 5% VAT with standard-rated, zero-rated and exempt categories; tax invoices with TRN and the fields the FTA requires, in Arabic and English; VAT return report; invoice data structured so it can support UAE e-invoicing; corporate tax reporting support. Keep every tax rate and rule configurable rather than hard-coded, and flag anything that should be confirmed with the company's tax advisor.
8. HR and payroll: employees, departments, positions and contracts; a document vault with expiry alerts (Emirates ID, passport, visa, labour card, medical insurance); attendance; leave requests with approvals; payroll with allowances, deductions and overtime; WPS salary file (SIF) export; end-of-service gratuity calculated under UAE Labour Law, with configurable rules; bilingual payslips.
9. Projects and tasks: projects, milestones, tasks, Kanban and Gantt views, time tracking, budget vs actual, and links to invoices.
10. Documents and approvals: central document storage and configurable multi-level approval workflows (for example, a purchase order above a set AED amount needs manager approval).
11. Website integration: a secure API that receives leads, quote requests and job applications from ainalhadaf.ae, plus optional management of website content (services, projects, news) from the ERP.
12. Reports and analytics: filterable reports, saved reports, scheduled email reports and export to Excel / PDF / CSV.
13. Notifications: in-app notification center, email, and drivers ready for WhatsApp / SMS, with per-user preferences.
14. Settings and administration: company profile, branches, fiscal year, currencies, tax settings, document numbering (e.g. INV-2026-0001), email templates, backups, system health, users, roles, permissions, audit log and login history.

# ADMIN PANEL DESIGN: MODERN, BEAUTIFUL, FUTURISTIC
- Overall feel: a premium, futuristic command center. Dark theme by default plus a polished light theme; each user's choice is remembered.
- Surfaces: glassmorphism panels (frosted blur, thin luminous borders), soft gradients and glows built from the Ain Alhadaf brand colors: [BRAND COLORS FROM THE LOGO / WEBSITE].
- Dashboard: bento-grid layout with animated KPI cards, sparklines and smooth chart transitions.
- Navigation: collapsible sidebar grouped by module with clean line icons (Lucide or Heroicons); top bar with global search, notifications, quick-create, language switch and profile menu.
- Command palette (Ctrl / Cmd + K) to jump to any page, record or action.
- Micro-interactions: hover lift, skeleton loaders, smooth page transitions, toast messages, and helpful empty states.
- Typography: a modern bilingual pairing that renders Arabic well (for example IBM Plex Sans Arabic with a matching Latin face); tabular numbers in tables and financial figures.
- Full RTL mirroring for Arabic: layouts, icons, charts and tables, not just translated text.
- Responsive on desktop, tablet and mobile.
- Accessibility: WCAG 2.1 AA contrast, keyboard navigation, visible focus states, and respect for prefers-reduced-motion.
- Design system first: define color tokens, spacing, radius, shadows and reusable Blade / Livewire components (buttons, inputs, selects, tables, modals, drawers, tabs, badges, status pills, cards, charts) before building pages.
- Login screen: a striking branded page with a subtle animated background and the company logo.

# ARCHITECTURE AND DATA
- Modular, domain-based structure (one module per business area) using service / action classes, Form Requests for validation, Policies for authorization, and events / listeners for cross-module workflows.
- Normalized MySQL schema with foreign keys, indexes, soft deletes, timestamps and created_by / updated_by on business records.
- Store money as DECIMAL (or integer minor units), never as float.
- Multi-branch support, designed so multi-company can be added later.
- Every financial document posts its journal entries automatically. Posted entries are immutable; corrections are made by reversal.
- All UI text in Laravel localization files (ar / en); bilingual name fields for products, accounts, customers and similar records.
- Date format DD/MM/YYYY; working week and public holidays configurable.

# SECURITY AND QUALITY
- OWASP Top 10 protections, CSRF, rate limiting, encryption of sensitive fields, a password policy, 2FA, session management and login history by device / IP.
- Role-based access down to action level; users see only their own branch's data unless granted wider access.
- Automated daily database backups; personal data handled in line with the UAE Personal Data Protection Law (PDPL).
- Feature tests for the critical flows of every module; seeders with realistic bilingual demo data.
- Performance: eager loading, caching, queued heavy jobs and indexed queries.

# DELIVERY PLAN
Work in phases and stop for my approval after each one:
- Phase 1: website review summary, confirmed requirements, prioritized module list, database ERD, folder structure, design tokens and a dashboard mockup.
- Phase 2: foundation (auth, 2FA, roles and permissions, settings, main layout, design-system components, dashboard shell, Arabic / English with RTL).
- Phase 3: CRM, Sales, Inventory and Purchasing.
- Phase 4: Accounting and UAE VAT.
- Phase 5: HR and Payroll.
- Phase 6: Projects, Documents and Approvals, Reports, Website API.
- Phase 7: testing, performance, security hardening, a deployment guide (Ubuntu, Nginx, PHP-FPM, MySQL, Redis, Supervisor, SSL) and a user manual.
For each phase, deliver complete working code (no placeholder stubs or TODOs), migrations, seeders, tests, and a short summary of what you built and how to run it.

# RULES
- Do not guess business facts about Ain Alhadaf. If something is unclear, ask me.
- Ask your clarifying questions before Phase 1 if anything critical is missing.
- Explain important architecture choices in one or two sentences.
- Production-quality code: strict types, consistent naming, comments only where they add value.

Start with Step 0, then Phase 1.
