class BusinessRuleError(Exception):
    """Raised when an operation would violate an accounting or control rule."""

    def __init__(self, message: str, code: str = "business_rule"):
        super().__init__(message)
        self.message = message
        self.code = code


class SegregationOfDutiesError(BusinessRuleError):
    def __init__(self, message: str = "Segregation of duties: you cannot approve your own work."):
        super().__init__(message, code="segregation_of_duties")


class PermissionDeniedError(BusinessRuleError):
    def __init__(self, message: str = "You do not have permission to perform this action."):
        super().__init__(message, code="permission_denied")


class TenantViolationError(BusinessRuleError):
    def __init__(self, message: str = "Records from different institutions cannot be combined."):
        super().__init__(message, code="tenant_violation")
