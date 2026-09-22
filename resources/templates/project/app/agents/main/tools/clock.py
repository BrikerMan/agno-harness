"""Sample toolkit.

Extend
------
Copy this file, rename the class, and append an instance in tools/__init__.py.
"""

from agno.tools import Toolkit

from app.services.notes import utc_now


class ClockTools(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="clock", tools=[self.current_time])

    def current_time(self) -> str:
        """Return the current UTC time in ISO format."""
        return utc_now()
