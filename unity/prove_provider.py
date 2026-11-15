"""Provider failures that require operator action before Prove can continue."""


class ProveProviderQuotaError(RuntimeError):
    def __init__(self):
        super().__init__(
            'Prove provider quota is exhausted; update the provider limit or credentials, '
            'then resume Prove.'
        )


def quota_exhausted(message: object, code: object = '') -> bool:
    text = f'{message or ""} {code or ""}'.lower()
    if any(value in text for value in (
        'insufficient_quota', 'project_spend_limit_exceeded', 'billing_hard_limit',
        'credit_balance_exhausted',
    )):
        return True
    return (any(value in text for value in ('quota exceeded', 'exceeded your current quota'))
            and any(value in text for value in ('billing', 'plan', 'credit')))
