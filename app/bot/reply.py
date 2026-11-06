from dataclasses import dataclass


@dataclass(frozen=True)
class Reply:
    text: str | None  # None: nothing to say
    buttons: list[tuple[str, str]] | None = None  # (label, callback data), one per row
    ephemeral: bool = False  # group chatter that should clean itself up
