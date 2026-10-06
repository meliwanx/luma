"""User preferences, proactive delivery state and sourced feed posts."""

from typing import Sequence, Union

from alembic import op


revision: str = "0016_proactive_feed"
down_revision: Union[str, None] = "0014_model_calls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE proactive_prefs (
            user_id TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            max_per_day INTEGER NOT NULL DEFAULT 2 CHECK (max_per_day BETWEEN 0 AND 5),
            window_start TEXT NOT NULL DEFAULT '09:00',
            window_end TEXT NOT NULL DEFAULT '21:30',
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            topics_like TEXT NOT NULL DEFAULT '' CHECK (length(topics_like) <= 1000),
            topics_avoid TEXT NOT NULL DEFAULT '' CHECK (length(topics_avoid) <= 1000),
            style TEXT NOT NULL DEFAULT '' CHECK (length(style) <= 500),
            feed_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            feed_per_day INTEGER NOT NULL DEFAULT 1 CHECK (feed_per_day BETWEEN 0 AND 3),
            feed_instructions TEXT NOT NULL DEFAULT '' CHECK (length(feed_instructions) <= 2000),
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE proactive_state (
            user_id TEXT PRIMARY KEY,
            local_day TEXT NOT NULL,
            sent_today INTEGER NOT NULL DEFAULT 0 CHECK (sent_today >= 0),
            last_sent_at TEXT,
            last_checked_at TEXT,
            feed_today INTEGER NOT NULL DEFAULT 0 CHECK (feed_today >= 0),
            last_feed_at TEXT
        )
    """)
    op.execute("""
        CREATE TABLE feed_posts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 40),
            body_markdown TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 80),
            topic TEXT NOT NULL,
            icon TEXT NOT NULL CHECK (icon IN (
                'chart','doc','search','calendar','code','mail','spark','book',
                'money','heart','globe','checklist'
            )),
            liked BOOLEAN NOT NULL DEFAULT FALSE,
            dismissed BOOLEAN NOT NULL DEFAULT FALSE,
            deleted_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX idx_feed_posts_user_created ON feed_posts(user_id, created_at)")
    op.execute("ALTER TABLE model_calls DROP CONSTRAINT IF EXISTS model_calls_purpose_check")
    op.execute("""
        ALTER TABLE model_calls ADD CONSTRAINT model_calls_purpose_check CHECK (purpose IN (
            'chat_round','final_answer','summary','voice_cleanup','decider','memory_extract',
            'other','ideas','proactive','feed'
        ))
    """)


def downgrade() -> None:
    op.execute("UPDATE model_calls SET purpose = 'other' WHERE purpose IN ('ideas','proactive','feed')")
    op.execute("ALTER TABLE model_calls DROP CONSTRAINT model_calls_purpose_check")
    op.execute("""
        ALTER TABLE model_calls ADD CONSTRAINT model_calls_purpose_check CHECK (purpose IN (
            'chat_round','final_answer','summary','voice_cleanup','decider','memory_extract','other'
        ))
    """)
    op.execute("DROP TABLE feed_posts")
    op.execute("DROP TABLE proactive_state")
    op.execute("DROP TABLE proactive_prefs")
