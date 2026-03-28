class OsuError(Exception):
    """Base class for all exceptions in the Akatsuki application."""
    def __init__(self, code: int, error: str, hint: str | None = None, message: str | None = None):
        self.code = code
        self.error = error
        self.hint = hint
        self.message = message
        super().__init__(f"{error}: {message} (Hint: {hint})")