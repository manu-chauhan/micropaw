"""Tests for contacts tool and files tool."""

import asyncio

import pytest

from micropaw.memory.store import MemoryStore
from micropaw.tools.base import ToolContext
from micropaw.tools.contacts_tool import ContactsTool
from micropaw.tools.contacts_tool import set_store as set_contacts_store
from micropaw.tools.files_tool import _FILES_DIR, FilesTool

_ctx = ToolContext(conversation_id="test", user_id="u1", platform="test")


# ---------------------------------------------------------------------------
# Contacts — Store CRUD
# ---------------------------------------------------------------------------

class TestContactsCRUD:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.store = MemoryStore(str(tmp_path / "test.db"))
        asyncio.get_event_loop().run_until_complete(self.store.init())
        yield
        asyncio.get_event_loop().run_until_complete(self.store.close())

    def run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_add_and_get(self):
        cid = self.run(self.store.add_contact("Alice", email="alice@example.com", company="Acme"))
        assert cid >= 1
        contacts = self.run(self.store.get_all_contacts())
        assert len(contacts) == 1
        assert contacts[0]["name"] == "Alice"
        assert contacts[0]["email"] == "alice@example.com"

    def test_search_by_name(self):
        self.run(self.store.add_contact("Bob Smith", email="bob@co.com"))
        self.run(self.store.add_contact("Alice Jones", email="alice@co.com"))
        results = self.run(self.store.search_contacts("bob"))
        assert len(results) == 1
        assert results[0]["name"] == "Bob Smith"

    def test_search_by_email(self):
        self.run(self.store.add_contact("Charlie", email="charlie@mega.io"))
        results = self.run(self.store.search_contacts("mega.io"))
        assert len(results) == 1
        assert results[0]["name"] == "Charlie"

    def test_search_by_company(self):
        self.run(self.store.add_contact("Dave", company="MegaCorp"))
        results = self.run(self.store.search_contacts("megacorp"))
        assert len(results) == 1

    def test_search_no_results(self):
        results = self.run(self.store.search_contacts("nobody"))
        assert results == []

    def test_update(self):
        cid = self.run(self.store.add_contact("Eve"))
        ok = self.run(self.store.update_contact(cid, email="eve@new.com", phone="555-1234"))
        assert ok is True
        contacts = self.run(self.store.get_all_contacts())
        assert contacts[0]["email"] == "eve@new.com"
        assert contacts[0]["phone"] == "555-1234"

    def test_update_nonexistent(self):
        ok = self.run(self.store.update_contact(9999, name="Ghost"))
        assert ok is False

    def test_delete(self):
        cid = self.run(self.store.add_contact("Frank"))
        assert self.run(self.store.delete_contact(cid)) is True
        assert self.run(self.store.get_all_contacts()) == []

    def test_delete_nonexistent(self):
        assert self.run(self.store.delete_contact(9999)) is False


# ---------------------------------------------------------------------------
# Contacts — Tool execute
# ---------------------------------------------------------------------------

class TestContactsTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.store = MemoryStore(str(tmp_path / "test.db"))
        asyncio.get_event_loop().run_until_complete(self.store.init())
        set_contacts_store(self.store)
        self.tool = ContactsTool()
        yield
        set_contacts_store(None)
        asyncio.get_event_loop().run_until_complete(self.store.close())

    def run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_not_initialized(self):
        set_contacts_store(None)
        result = self.run(self.tool.execute({"action": "list"}, _ctx))
        assert "not initialized" in result

    def test_add(self):
        result = self.run(self.tool.execute({"action": "add", "name": "Alice", "email": "a@b.com"}, _ctx))
        assert "Alice" in result
        assert "ID:" in result

    def test_add_no_name(self):
        result = self.run(self.tool.execute({"action": "add"}, _ctx))
        assert "Error" in result

    def test_search(self):
        self.run(self.tool.execute({"action": "add", "name": "Bob", "email": "bob@co.com"}, _ctx))
        result = self.run(self.tool.execute({"action": "search", "query": "bob"}, _ctx))
        assert "Bob" in result
        assert "bob@co.com" in result

    def test_search_no_results(self):
        result = self.run(self.tool.execute({"action": "search", "query": "nobody"}, _ctx))
        assert "No contacts" in result

    def test_list_empty(self):
        result = self.run(self.tool.execute({"action": "list"}, _ctx))
        assert "No contacts" in result

    def test_update(self):
        self.run(self.tool.execute({"action": "add", "name": "Charlie"}, _ctx))
        contacts = self.run(self.store.get_all_contacts())
        cid = contacts[0]["id"]
        result = self.run(self.tool.execute({"action": "update", "contact_id": cid, "email": "c@new.com"}, _ctx))
        assert "updated" in result

    def test_delete(self):
        self.run(self.tool.execute({"action": "add", "name": "Dave"}, _ctx))
        contacts = self.run(self.store.get_all_contacts())
        cid = contacts[0]["id"]
        result = self.run(self.tool.execute({"action": "delete", "contact_id": cid}, _ctx))
        assert "deleted" in result


# ---------------------------------------------------------------------------
# Files — Tool execute
# ---------------------------------------------------------------------------

class TestFilesTool:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        self.tool = FilesTool()
        self.test_dir = tmp_path / "files"
        self.test_dir.mkdir()
        self._orig = _FILES_DIR
        # Patch _FILES_DIR to use temp directory
        import micropaw.tools.files_tool as mod
        mod._FILES_DIR = self.test_dir
        yield
        mod._FILES_DIR = self._orig

    def run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_save_and_read(self):
        result = self.run(self.tool.execute({"action": "save", "path": "hello.txt", "content": "world"}, _ctx))
        assert "Saved" in result
        result = self.run(self.tool.execute({"action": "read", "path": "hello.txt"}, _ctx))
        assert result == "world"

    def test_save_subdirectory(self):
        result = self.run(self.tool.execute(
            {"action": "save", "path": "notes/meeting.md", "content": "# Meeting"}, _ctx,
        ))
        assert "Saved" in result
        assert (self.test_dir / "notes" / "meeting.md").exists()

    def test_save_no_path(self):
        result = self.run(self.tool.execute({"action": "save", "content": "data"}, _ctx))
        assert "Error" in result

    def test_save_no_content(self):
        result = self.run(self.tool.execute({"action": "save", "path": "f.txt"}, _ctx))
        assert "Error" in result

    def test_read_not_found(self):
        result = self.run(self.tool.execute({"action": "read", "path": "nope.txt"}, _ctx))
        assert "not found" in result

    def test_list_empty(self):
        result = self.run(self.tool.execute({"action": "list"}, _ctx))
        assert "No files" in result

    def test_list_files(self):
        (self.test_dir / "a.txt").write_text("hello")
        (self.test_dir / "b.txt").write_text("world")
        result = self.run(self.tool.execute({"action": "list"}, _ctx))
        assert "a.txt" in result
        assert "b.txt" in result

    def test_delete(self):
        (self.test_dir / "del.txt").write_text("bye")
        result = self.run(self.tool.execute({"action": "delete", "path": "del.txt"}, _ctx))
        assert "Deleted" in result
        assert not (self.test_dir / "del.txt").exists()

    def test_delete_not_found(self):
        result = self.run(self.tool.execute({"action": "delete", "path": "nope.txt"}, _ctx))
        assert "not found" in result

    def test_path_traversal_blocked(self):
        result = self.run(self.tool.execute({"action": "read", "path": "../../etc/passwd"}, _ctx))
        assert "not found" in result or "Invalid" in result

    def test_save_path_traversal_blocked(self):
        result = self.run(self.tool.execute({"action": "save", "path": "../../../evil.txt", "content": "bad"}, _ctx))
        assert "Invalid" in result or "Error" in result

    def test_large_file_truncated(self):
        big = "x" * 60_000
        (self.test_dir / "big.txt").write_text(big)
        result = self.run(self.tool.execute({"action": "read", "path": "big.txt"}, _ctx))
        assert "truncated" in result
        assert len(result) < 60_000
