"""Persist content-free model usage and performance measurements."""

from typing import Sequence, Union

from alembic import op


revision: str = "0014_model_calls"
down_revision: Union[str, None] = "0013_runtime_capabilities"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Statistics survive conversation deletion for the 180-day retention
    # period, so identifiers deliberately have no foreign keys.
    op.execute(
        """
        CREATE TABLE model_calls (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT,
            message_id TEXT,
            purpose TEXT NOT NULL CHECK (purpose IN (
                'chat_round', 'final_answer', 'summary', 'voice_cleanup',
                'decider', 'memory_extract', 'other'
            )),
            model TEXT NOT NULL,
            stream BOOLEAN NOT NULL DEFAULT FALSE,
            prompt_tokens BIGINT NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
            completion_tokens BIGINT NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
            total_tokens BIGINT NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
            cached_tokens BIGINT CHECK (cached_tokens >= 0),
            reasoning_tokens BIGINT CHECK (reasoning_tokens >= 0),
            estimated BOOLEAN NOT NULL DEFAULT FALSE,
            first_token_ms DOUBLE PRECISION CHECK (first_token_ms >= 0),
            duration_ms DOUBLE PRECISION CHECK (duration_ms >= 0),
            tokens_per_sec DOUBLE PRECISION CHECK (tokens_per_sec >= 0),
            status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'cancelled', 'timeout')),
            error_type TEXT,
            tool_calls_count INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls_count >= 0),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    op.execute("CREATE INDEX idx_model_calls_user_created ON model_calls(user_id, created_at)")
    op.execute("CREATE INDEX idx_model_calls_created ON model_calls(created_at)")
    op.execute("CREATE INDEX idx_model_calls_model_created ON model_calls(model, created_at)")
    op.execute("CREATE INDEX idx_model_calls_message ON model_calls(user_id, message_id) WHERE message_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP TABLE model_calls")
