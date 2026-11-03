import sqlite3

from cryptography.fernet import Fernet

from app.store import Store


def test_old_databases_keep_their_data(tmp_path):
    """Databases made before the user column was called user_id are upgraded in place."""
    path, key = str(tmp_path / "old.db"), Fernet.generate_key().decode()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE merchants (wa_id TEXT PRIMARY KEY, ref TEXT UNIQUE NOT NULL, public_key_enc TEXT NOT NULL,
            private_key_enc TEXT NOT NULL, default_currency TEXT NOT NULL, webhook_secret_enc TEXT,
            webhook_config_id TEXT, webhook_endpoint TEXT, connected_at REAL NOT NULL);
        CREATE TABLE customers (wa_id TEXT NOT NULL, name_key TEXT NOT NULL, name TEXT NOT NULL, email TEXT,
            phone TEXT, updated_at REAL NOT NULL, PRIMARY KEY (wa_id, name_key));
    """)
    f = Fernet(key.encode())
    db.execute("INSERT INTO merchants VALUES ('tg:7', 'r1', ?, ?, 'INR', NULL, NULL, NULL, 0)",
               (f.encrypt(b"pk").decode(), f.encrypt(b"sk").decode()))
    db.execute("INSERT INTO customers VALUES ('tg:7', 'rahul', 'Rahul', 'r@x.com', '+911234567890', 0)")
    db.commit()
    db.close()

    store = Store(path, key)
    assert store.get_merchant("tg:7").private_key == "sk"
    assert store.get_customer("tg:7", "Rahul")["email"] == "r@x.com"
    Store(path, key)  # opening it again is a no-op
