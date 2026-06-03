"""Domain exceptions for betting execution."""


class BetExecutionError(Exception):
    """Base exception for betting execution failures."""
    pass


class OddsChangedError(BetExecutionError):
    """Raised when live odds have moved beyond max_slippage."""
    def __init__(self, expected: float, actual: float, slippage: float):
        self.expected = expected
        self.actual = actual
        self.slippage = slippage
        super().__init__(
            f"Odds changed: expected {expected:.2f}, "
            f"got {actual:.2f} (slippage {slippage:.1%})"
        )


class InsufficientBalanceError(BetExecutionError):
    """Raised when platform balance is insufficient."""
    pass


class AuthenticationError(BetExecutionError):
    """Raised when platform login fails."""
    pass


class PlatformUnavailableError(BetExecutionError):
    """Raised when the platform is down or unreachable."""
    pass
