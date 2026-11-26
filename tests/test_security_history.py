import pytest
from cryptography.fernet import Fernet

from src.n9n8.database import Database


@pytest.mark.asyncio
async def test_security_event_history_is_scoped_to_telegram_user_and_newest_first(tmp_path):
    db = Database(str(tmp_path / "security-history.sqlite3"), Fernet.generate_key())
    await db.initialize()
    await db.ensure_user(101)
    await db.ensure_user(202)

    await db.security_event(101, "live_entry_rejected", "Token A failed mint authority check.")
    await db.security_event(202, "live_entry_rejected", "Other user's private event.")
    await db.security_event(101, "live_entry_rejected", "Token B failed LP lock check.")

    rows = await db.list_security_events(101, 10)

    assert len(rows) == 2
    assert rows[0]["detail"] == "Token B failed LP lock check."
    assert all("Other user's" not in row["detail"] for row in rows)
