from enum import StrEnum


class StreamMode(StrEnum):
    """Transport streaming strategy.

    - RAW: Stream every chunk and item immediately (Web AG-UI, CLI).
    - THROTTLE: Buffer and flush updates at a safe cadence (e.g. 1.5s) to avoid 429.
    - FINAL: No mid-stream message patches. Uses reaction-ACK & typing heartbeat,
             then posts a single finalized response with aggregated cards upon completion.
    """

    RAW = "raw"
    THROTTLE = "throttle"
    FINAL = "final"
