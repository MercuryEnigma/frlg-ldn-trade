"""Human-readable identity for the emulated FRLG trade host.

Edit :data:`DEFAULT_TRAINER` to change the trainer presented by Linux.  This
module is the single source of truth for discovery, Pia Session, LinkPlayer,
and trainer-card identity; protocol encoders should consume a ``TrainerProfile``
instead of duplicating trainer fields.
"""

from dataclasses import dataclass

from . import charmap, linkplayer


VERSIONS = {
    "firered": linkplayer.VERSION_FIRE_RED,
    "leafgreen": linkplayer.VERSION_LEAF_GREEN,
}
LANGUAGES = {
    "english": linkplayer.LANGUAGE_ENGLISH,
}


@dataclass(frozen=True)
class TrainerProfile:
    """Editable trainer settings, expressed in game-facing terms."""

    name: str
    tid: int
    sid: int
    gender: int = 0
    version: str = "leafgreen"
    language: str = "english"
    has_national_dex: bool = False
    has_completed_game: bool = False

    def __post_init__(self):
        if not isinstance(self.name, str):
            raise ValueError("trainer name must be a string")
        encoded = charmap.encode(self.name)
        if not self.name or charmap.decode(encoded) != self.name:
            raise ValueError("trainer name contains unsupported Gen III characters")
        if len(encoded) > 7:
            raise ValueError("trainer name must encode to at most 7 Gen III characters")
        if (type(self.tid) is not int or type(self.sid) is not int
                or not 0 <= self.tid <= 0xFFFF or not 0 <= self.sid <= 0xFFFF):
            raise ValueError("TID and SID must each fit in 16 bits")
        if type(self.gender) is not int or self.gender not in (0, 1):
            raise ValueError("gender must be 0 (male) or 1 (female)")
        if self.version not in VERSIONS:
            raise ValueError(f"version must be one of {', '.join(VERSIONS)}")
        if self.language not in LANGUAGES:
            raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
        if type(self.has_national_dex) is not bool:
            raise ValueError("has_national_dex must be a bool")
        if type(self.has_completed_game) is not bool:
            raise ValueError("has_completed_game must be a bool")

    @property
    def discovery_name(self):
        """Trainer name stored in the RFU discovery record."""
        return self.name

    @property
    def discovery_trainer_id(self):
        """Public 16-bit trainer ID stored in the RFU discovery record."""
        return self.tid

    @property
    def session_name(self):
        """UTF-8-compatible player name advertised by the Pia Session layer."""
        return self.name

    @property
    def trainer_id(self):
        """Generation III's combined 32-bit trainer ID (SID:TID)."""
        return (self.sid << 16) | self.tid

    @property
    def progress_flags(self):
        return ((1 if self.has_national_dex else 0)
                | (0x10 if self.has_completed_game else 0))

    def to_link_player(self):
        return linkplayer.LinkPlayer(
            name=self.name,
            trainer_id=self.trainer_id,
            version=VERSIONS[self.version],
            progress_flags=self.progress_flags,
            progress_flags_copy=self.progress_flags,
            gender=self.gender,
            player_id=0,
            language=LANGUAGES[self.language],
        )

    def build_link_player_block(self):
        """Build the host's wire block with robust all-0xFF name padding."""
        return linkplayer.build_block(
            self.to_link_player(), name_pad=linkplayer.HOST_NAME_PAD)

    def build_trainer_card(self, mon_species=None):
        return linkplayer.build_trainer_card(
            self.to_link_player(), mon_species=mon_species,
            name_pad=linkplayer.HOST_NAME_PAD)


# This is the only place users need to edit the emulated host trainer.
DEFAULT_TRAINER = TrainerProfile(
    name="EMU",
    tid=0x8822,
    sid=0x47ED,
    gender=0,
    version="leafgreen",
    language="english",
    has_national_dex=True,
    has_completed_game=True,
)
