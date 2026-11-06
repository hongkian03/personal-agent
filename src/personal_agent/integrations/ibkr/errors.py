"""Public errors deliberately omit broker responses and credential-bearing URLs."""


class FlexError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
