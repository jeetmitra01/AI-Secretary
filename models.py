"""
The normalized Email object — the ONLY email shape the rest of the system
ever sees. Connectors translate provider chaos into this; the agent,
summarizer, and meeting extractor never touch a provider API directly.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Email:
    id: str                 # provider's message id (stable, unique per account)
    thread_id: str          # provider's thread/conversation id
    sender: str             # "Display Name <addr@example.com>" or bare address
    recipients: list[str] = field(default_factory=list)
    subject: str = ""
    body_text: str = ""     # ALWAYS plain text — connectors strip HTML
    received_at: datetime | None = None  # ALWAYS timezone-aware UTC
    source: str = ""        # "gmail" | "outlook" — constant per agent
                            # instance, but kept so multi-agent coordinators
                            # can merge records later without guessing

    def brief(self, body_chars: int = 200) -> str:
        """Compact one-email rendering for prompts. Note the delimiters:
        email content is untrusted third-party data, and we mark it as
        such every single time it enters a prompt."""
        ts = self.received_at.isoformat() if self.received_at else "unknown"
        body = self.body_text[:body_chars]
        return (
            f"<email id={self.id!r} from={self.sender!r} at={ts!r}>\n"
            f"Subject: {self.subject}\n"
            f"{body}\n"
            f"</email>"
        )
