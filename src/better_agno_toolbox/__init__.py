"""Compatibility alias: ``better_agno_toolbox`` is ``agno_harness``.

Existing ``import better_agno_toolbox`` does not need to change.
"""

from agno_harness.compat import bind_alias

bind_alias("better_agno_toolbox", globals())
