from typing import NotRequired, TypedDict


class Station(TypedDict):
    id: str
    name: str


class Departure(TypedDict):
    time: str | None
    scheduled: str | None
    delay_min: int
    cancelled: bool
    skipped: bool
    platform: str | None
    train: str
    direction: str | None
    realtime: bool
    realtime_unavailable: NotRequired[bool]


Leg = TypedDict(
    "Leg",
    {
        "train": str,
        "from": str,
        "to": str,
        "departure": str | None,
        "arrival": str | None,
        "delay_min": int,
        "cancelled": bool,
        "skipped": bool,
        "realtime": bool,
    },
)


class Journey(TypedDict):
    departure: str | None
    arrival: str | None
    duration_min: int
    transfers: int
    legs: list[Leg]
    realtime_unavailable: NotRequired[bool]


Disruption = TypedDict(
    "Disruption",
    {
        "effect": str | None,
        "title": str | None,
        "message": str | None,
        "from": str | None,
        "until": str | None,
        "status": NotRequired[str | None],
    },
)


class FeedWarning(TypedDict):
    warning: str
