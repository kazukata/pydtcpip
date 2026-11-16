"""Exceptions raised by pydtcpip."""


class PyDTCPIPError(Exception):
    """Base class for exceptions raised by pydtcpip."""


class DLNAError(PyDTCPIPError):
    """Device discovery or processing a device description, SOAP or HTTP response failed."""

    def __init__(
        self, message: str, *, http_status: int | None = None, diagnostic_headers: dict[str, str] | None = None
    ) -> None:
        """
        Initialize the exception.

        Args:
            message (str): Description to show the user
            http_status (int | None): HTTP status returned by the recorder, if the error came from an HTTP response
            diagnostic_headers (dict[str, str] | None): Recorder diagnostic fields from the HTTP response
        """

        self.diagnostic_headers = dict(diagnostic_headers or {})
        if self.diagnostic_headers:
            message += ' ' + '; '.join(f'{name}: {value}' for name, value in self.diagnostic_headers.items())
        super().__init__(message)
        self.http_status = http_status


class UnsupportedActionError(DLNAError):
    """The device's service description does not advertise the requested optional action."""


class ClientNotAllowedError(DLNAError):
    """
    The recorder has not authorized this PC as a media server client.
    Unlike an authentication failure, this can be resolved by allowing the client in the device settings.
    """


class DTCPError(PyDTCPIPError):
    """DTCP-IP authentication, protected packet processing, or Move failed."""


class MoveCommitmentRejectedError(DTCPError):
    """
    The source rejected a Move finalize request.
    If a retry is rejected after an earlier request had an unknown outcome, the Move may already be finalized.
    """

    def __init__(self, message: str, *, may_be_committed: bool) -> None:
        """
        Initialize the exception.

        Args:
            message (str): Description to show the user
            may_be_committed (bool): Whether the source may have finalized the Move
        """

        super().__init__(message)
        self.may_be_committed = may_be_committed
