"""Merge the library/ideas and proactive/feed migration branches."""

from typing import Sequence, Union

revision: str = "0017_merge_features"
down_revision: Union[str, Sequence[str], None] = ("0015_library_ideas", "0016_proactive_feed")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
