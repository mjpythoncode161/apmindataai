from accounts.models import CompanyProfile


def company_profile(request):
    company = CompanyProfile.get_settings()
    return {
        "company": company,
        "system_name": company.system_label or "MSBC-2025-26",
    }
