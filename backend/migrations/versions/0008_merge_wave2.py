"""Merge the independent second-wave migration branches."""

from typing import Sequence, Union


revision: str = "0008_merge_wave2"
down_revision: Union[str, Sequence[str], None] = (
    "0004_stream",
    "0005_memory",
    "0006_scheduler",
    "0007_files",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
