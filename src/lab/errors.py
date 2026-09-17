"""Redacted failures shared by interactive and scripted operations."""


class LabError(Exception):
    """Report an actionable error with a stable process exit code.

    :param message: Public diagnostic containing no credentials or secret bodies.
    :param code: Process exit code defined by the command contract.
    """

    def __init__(self, message: str, code: int = 4, *, target=None, data=None) -> None:
        super().__init__(message)
        self.code = code
        self.target = target
        self.data = data
