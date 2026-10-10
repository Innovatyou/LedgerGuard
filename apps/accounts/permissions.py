"""
Permission catalogue and default role definitions.

Permissions are action codes evaluated per institution. Roles are bundles of
codes; institutions may define additional roles. A user's effective
permissions in an institution are the union of the codes of all their active
memberships there.
"""

PERMISSIONS = {
    # Organisation & users
    "org.manage": "Manage institution settings, branches and departments",
    "users.manage": "Manage users, roles and memberships",
    "users.view": "View users and memberships",
    # Accounting
    "coa.view": "View chart of accounts",
    "coa.manage": "Create and edit chart of accounts",
    "journal.view": "View journals and ledger",
    "journal.create": "Create journal drafts",
    "journal.approve": "Approve journals",
    "journal.post": "Post approved journals",
    "journal.post_backdated": "Post journals dated before the backdating limit",
    "journal.reverse": "Request reversal of posted journals",
    "period.manage": "Create financial periods",
    "period.close": "Close financial periods",
    "period.reopen": "Reopen closed financial periods",
    # Imports
    "import.view": "View import batches and source transactions",
    "import.upload": "Upload and commit source files",
    "import.profile_manage": "Manage import profiles",
    # Reconciliation
    "recon.view": "View reconciliations",
    "recon.rule_manage": "Edit reconciliation matching rules",
    "recon.run": "Create and run reconciliations",
    "recon.investigate": "Investigate exceptions, accept/reject matches",
    "recon.approve": "Approve reconciliations",
    "recon.reopen": "Reopen approved reconciliations",
    # Settlements
    "settlement.view": "View settlement reconciliation",
    # Suspense
    "suspense.view": "View suspense items",
    "suspense.manage": "Create, assign and investigate suspense items",
    "suspense.approve": "Approve suspense resolutions",
    "suspense.configure": "Configure aging buckets and escalation rules",
    # Audit
    "audit.view": "View audit engagements, workpapers and findings",
    "audit.manage": "Plan engagements, prepare workpapers and raise findings",
    "audit.review": "Review and sign off workpapers, close findings",
    "audit.respond": "Respond to findings and update corrective actions",
    # Reporting
    "report.view": "View financial and operational reports",
    "report.sensitive": "View sensitive reports",
    "report.export": "Export reports",
    "activity.view": "View activity (audit trail) logs",
    "integration.manage": "Configure integrations",
    # Configuration control
    "config.approve": "Approve chart-of-accounts and matching-rule changes",
    "journal.recurring_manage": "Create and maintain recurring journal templates",
    # Fixed assets
    "asset.view": "View the fixed-asset register",
    "asset.manage": "Add and maintain fixed assets",
    "asset.depreciate": "Prepare depreciation runs",
    "asset.dispose": "Prepare asset disposals",
    # Loans and deposits portfolio controls
    "portfolio.view": "View loan and deposit control schedules",
    "portfolio.manage": "Upload portfolio snapshots and configure classification",
    # Regulatory workpapers
    "regulatory.view": "View regulatory workpapers",
    "regulatory.manage": "Configure templates and prepare regulatory workpapers",
    "regulatory.review": "Review and sign off regulatory workpapers",
    # Integrations
    "integration.view": "View integration endpoints and logs",
}

ALL = set(PERMISSIONS)
_VIEW = {code for code in PERMISSIONS if code.endswith(".view")}

DEFAULT_ROLES = {
    "super_admin": ("Super Administrator", ALL),
    "institution_admin": (
        "Institution Administrator",
        {
            "org.manage",
            "users.manage",
            "users.view",
            "activity.view",
            "integration.manage",
            "coa.view",
            "import.profile_manage",
            "recon.rule_manage",
            "suspense.configure",
            "report.view",
        },
    ),
    "cfo": (
        "Chief Financial Officer",
        _VIEW
        | {
            "journal.approve",
            "journal.post",
            "journal.post_backdated",
            "journal.reverse",
            "period.manage",
            "period.close",
            "period.reopen",
            "recon.approve",
            "recon.reopen",
            "suspense.approve",
            "report.sensitive",
            "report.export",
            "coa.manage",
            "audit.respond",
        },
    ),
    "finance_manager": (
        "Finance Manager",
        _VIEW
        | {
            "coa.manage",
            "journal.create",
            "journal.approve",
            "journal.post",
            "journal.reverse",
            "period.manage",
            "period.close",
            "recon.approve",
            "suspense.approve",
            "report.export",
            "audit.respond",
        },
    ),
    "accountant": (
        "Accountant",
        {
            "coa.view",
            "journal.view",
            "journal.create",
            "import.view",
            "import.upload",
            "recon.view",
            "suspense.view",
            "suspense.manage",
            "report.view",
            "report.export",
            "settlement.view",
            "audit.respond",
        },
    ),
    "recon_officer": (
        "Reconciliation Officer",
        {
            "coa.view",
            "import.view",
            "import.upload",
            "recon.view",
            "recon.run",
            "recon.investigate",
            "suspense.view",
            "suspense.manage",
            "settlement.view",
            "report.view",
            "report.export",
            "journal.view",
            "journal.create",
        },
    ),
    "recon_reviewer": (
        "Reconciliation Reviewer",
        {
            "coa.view",
            "import.view",
            "recon.view",
            "recon.investigate",
            "recon.approve",
            "suspense.view",
            "suspense.approve",
            "settlement.view",
            "report.view",
            "report.export",
            "journal.view",
        },
    ),
    "settlement_officer": (
        "Settlement Officer",
        {
            "coa.view",
            "import.view",
            "import.upload",
            "recon.view",
            "recon.run",
            "recon.investigate",
            "settlement.view",
            "suspense.view",
            "suspense.manage",
            "report.view",
        },
    ),
    "internal_auditor": (
        "Internal Auditor",
        _VIEW - {"users.view"} | {"audit.manage", "audit.review", "report.export", "report.sensitive"},
    ),
    "external_auditor": (
        "External Auditor",
        {
            "coa.view",
            "journal.view",
            "import.view",
            "recon.view",
            "suspense.view",
            "settlement.view",
            "audit.view",
            "report.view",
            "report.export",
        },
    ),
    "compliance_officer": (
        "Compliance Officer",
        {
            "coa.view",
            "journal.view",
            "recon.view",
            "suspense.view",
            "settlement.view",
            "audit.view",
            "report.view",
            "report.sensitive",
            "report.export",
            "activity.view",
        },
    ),
    "branch_manager": (
        "Branch Manager",
        {"coa.view", "journal.view", "recon.view", "suspense.view", "settlement.view", "report.view", "audit.respond"},
    ),
    "management_viewer": (
        "Management Viewer",
        {"report.view", "recon.view", "suspense.view", "settlement.view"},
    ),
}


# Permissions added after the initial role design, granted on top of the sets above.
_ADDITIONS = {
    "institution_admin": {"integration.view"},
    "cfo": {"config.approve", "asset.dispose", "regulatory.review", "portfolio.manage"},
    "finance_manager": {
        "config.approve",
        "journal.recurring_manage",
        "asset.manage",
        "asset.depreciate",
        "asset.dispose",
        "portfolio.manage",
        "regulatory.manage",
        "regulatory.review",
    },
    "accountant": {
        "asset.view",
        "asset.manage",
        "asset.depreciate",
        "portfolio.view",
        "regulatory.view",
        "regulatory.manage",
    },
    "recon_officer": {"portfolio.view"},
    "recon_reviewer": {"config.approve", "portfolio.view"},
    "external_auditor": {"asset.view", "portfolio.view", "regulatory.view"},
    "compliance_officer": {"asset.view", "portfolio.view", "regulatory.view", "regulatory.review"},
    "branch_manager": {"asset.view", "portfolio.view"},
}
for _code, _extra in _ADDITIONS.items():
    _name, _perms = DEFAULT_ROLES[_code]
    DEFAULT_ROLES[_code] = (_name, set(_perms) | _extra)
DEFAULT_ROLES["super_admin"] = ("Super Administrator", set(ALL))


def validate_codes(codes):
    unknown = set(codes) - ALL
    if unknown:
        raise ValueError(f"Unknown permission codes: {', '.join(sorted(unknown))}")
    return sorted(set(codes))
