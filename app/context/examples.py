ESTIMATION_EXAMPLES: list[dict] = [
    {
        "meeting_summary": (
            "A client running a mid-sized warehouse needs a web-based inventory "
            "management system. Key requirements include live stock level tracking, "
            "automatic reorder notifications triggered when quantities drop under a "
            "user-defined limit, role-based permissions (admin, warehouse manager, "
            "viewer), the ability to import and export inventory records via CSV or "
            "Excel, and a metrics dashboard covering turnover rate, total stock value, "
            "and low-stock counts. The solution must connect to their existing "
            "PostgreSQL database hosted on AWS. A mobile app is out of scope - "
            "warehouse staff will access the platform through tablets using the browser."
        ),
        "estimation": """\
## Inventory Management Web Platform

### Task Breakdown

| Task | Hours | Cost (USD) |
|------|------:|------------|
| Discovery, requirements analysis, and solution architecture | 16 | 1,000 |
| Database schema modeling and migration scripts | 12 | 750 |
| Login system and role-based permissions | 20 | 1,250 |
| REST API for products and stock management | 24 | 1,500 |
| Automatic reorder alert service | 16 | 1,000 |
| CSV/Excel data import and export | 14 | 875 |
| Analytics dashboard with KPIs and visual charts | 20 | 1,250 |
| Frontend: inventory list, search, and filtering | 24 | 1,500 |
| Frontend: admin console and user administration | 12 | 750 |
| AWS PostgreSQL connection and production deployment | 10 | 625 |
| Testing: unit, integration, and end-to-end | 20 | 1,250 |
| Code review, quality assurance, and defect resolution | 12 | 750 |

### Totals

- **Total hours:** 200
- **Total cost:** 12,500 USD

### Recommended Team

- 1 Senior Backend Developer (technical lead)
- 1 Mid-level Full-Stack Developer
- 1 QA Engineer (part-time, engaged during the final 3 weeks)

### Estimated Duration

**10 weeks** with a two-developer team.""",
    },
    {
        "meeting_summary": (
            "A B2B startup wants to launch a SaaS product that helps small and medium "
            "businesses keep track of their software subscriptions. The MVP must "
            "include: sign-up and company registration, a dashboard listing all active "
            "subscriptions with renewal dates and monthly costs, the ability to create, "
            "modify, and cancel subscriptions, Stripe integration for payments and "
            "invoice generation, email reminders sent 30/7/1 days before a renewal, and "
            "an admin area for managing customer accounts. The stack should be a REST "
            "API with a React front end. The client intends to ship an MVP first and "
            "iterate afterward."
        ),
        "estimation": """\
## SaaS Subscription Management Platform (MVP)

### Task Breakdown

| Task | Hours | Cost (USD) |
|------|------:|------------|
| System architecture and initial project scaffolding | 12 | 750 |
| User sign-up and JWT-based authentication | 16 | 1,000 |
| Company onboarding workflow | 12 | 750 |
| Subscription data model and CRUD API | 20 | 1,250 |
| Dashboard: active subscriptions and spend reporting | 24 | 1,500 |
| Stripe payments and invoice generation | 28 | 1,750 |
| Renewal reminder email service | 16 | 1,000 |
| Customer management admin panel | 20 | 1,250 |
| React UI: pages, forms, and client-side routing | 40 | 2,500 |
| API documentation with OpenAPI/Swagger | 6 | 375 |
| Testing: unit, integration, and Stripe sandbox scenarios | 24 | 1,500 |
| Deployment, CI/CD setup, and staging environment | 16 | 1,000 |
| Security review and hardening | 10 | 625 |

### Totals

- **Total hours:** 244
- **Total cost:** 15,250 USD

### Recommended Team

- 1 Senior Full-Stack Developer (lead)
- 1 Mid-level Backend Developer
- 1 Mid-level Frontend Developer
- 1 QA Engineer (part-time, engaged during the final 4 weeks)

### Estimated Duration

**12 weeks** with a three-person core team.""",
    },
    {
        "meeting_summary": (
            "A regional healthcare network is redesigning its patient portal to improve "
            "usability and accessibility. The current portal suffers from poor "
            "navigation, inconsistent visual design, and fails WCAG 2.1 AA standards. "
            "The client wants a full UX audit, user research with patients and staff, "
            "information architecture, wireframes, high-fidelity mockups, and a design "
            "system that developers can reuse. The portal must support appointment "
            "booking, prescription refills, lab results viewing, and secure messaging. "
            "Deliverables should be handed off in Figma with documented components and "
            "accessibility annotations."
        ),
        "estimation": """\
## UX/UI Redesign - Healthcare Patient Portal

### Task Breakdown

| Task | Hours | Cost (USD) |
|------|------:|------------|
| Stakeholder interviews and discovery workshops | 16 | 1,200 |
| UX audit of existing portal and heuristic evaluation | 14 | 1,050 |
| User research: patient and staff interviews, surveys | 20 | 1,500 |
| Information architecture and user flow mapping | 16 | 1,200 |
| Low-fidelity wireframes for core flows | 24 | 1,800 |
| High-fidelity UI mockups (desktop and tablet) | 32 | 2,400 |
| Accessibility review and WCAG 2.1 AA compliance pass | 18 | 1,350 |
| Design system: components, tokens, and documentation | 28 | 2,100 |
| Interactive prototype for usability testing | 16 | 1,200 |
| Usability testing sessions and analysis | 20 | 1,500 |
| Design handoff, Figma organization, and dev Q&A | 12 | 900 |
| Revisions based on feedback and final polish | 14 | 1,050 |

### Totals

- **Total hours:** 230
- **Total cost:** 17,250 USD

### Recommended Team

- 1 Senior UX Designer (lead)
- 1 UI Designer
- 1 UX Researcher (part-time, research and testing phases)
- 1 Accessibility Consultant (part-time)

### Estimated Duration

**9 weeks** with a two-designer core team plus part-time specialists.""",
    },
    {
        "meeting_summary": (
            "A fintech company has a React Native mobile app (iOS and Android) that "
            "handles personal budgeting and bank account aggregation. They need an "
            "independent QA engagement to stabilize the release process. Scope includes: "
            "a manual test pass of all critical user journeys, building an automated "
            "regression suite (Detox or Appium), API testing with Postman/Newman or "
            "REST Assured, performance testing of key endpoints, a security smoke test, "
            "and a CI pipeline that runs the automated suite on every pull request. They "
            "want a test strategy document and clear bug reports with severity ratings."
        ),
        "estimation": """\
## QA & Test Automation - Fintech Mobile App

### Task Breakdown

| Task | Hours | Cost (USD) |
|------|------:|------------|
| Test strategy, scope definition, and risk assessment | 14 | 1,050 |
| Manual testing of critical user journeys | 30 | 2,250 |
| Test case authoring and maintenance plan | 18 | 1,350 |
| Automated regression suite setup (Detox/Appium) | 32 | 2,400 |
| Automated test scripts for core flows | 40 | 3,000 |
| API testing: collection, assertions, and CI integration | 20 | 1,500 |
| Performance testing of key API endpoints | 16 | 1,200 |
| Security smoke testing (OWASP Mobile Top 10) | 14 | 1,050 |
| CI/CD integration: automated suite on pull requests | 16 | 1,200 |
| Bug reporting, triage, and regression verification | 22 | 1,650 |
| Test summary reports and release readiness sign-off | 10 | 750 |
| Knowledge transfer and QA documentation | 12 | 900 |

### Totals

- **Total hours:** 244
- **Total cost:** 18,300 USD

### Recommended Team

- 1 Senior QA Engineer (lead, automation-focused)
- 1 Mid-level QA Engineer (manual and API testing)
- 1 Performance/Security Tester (part-time, specialized phases)

### Estimated Duration

**11 weeks** with a two-QA core team plus part-time specialists.""",
    },
]


def format_examples_for_prompt(examples: list[dict]) -> str:
    """Format estimation examples into a string suitable for injection into a system prompt."""
    parts: list[str] = []
    for i, example in enumerate(examples, start=1):
        parts.append(
            f"--- EXAMPLE {i} ---\n"
            f"Meeting Summary:\n{example['meeting_summary']}\n\n"
            f"Estimation:\n{example['estimation']}\n"
        )
    return "\n".join(parts)
