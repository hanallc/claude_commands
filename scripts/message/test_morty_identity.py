"""Test the configured List producer identity."""
from __future__ import annotations

import os
from dataclasses import replace
from typing import cast
from unittest import mock

from scripts.message import morty_identity as list_identity
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id


class ListProducerIdentityTests(StaffTestCase):
    def configure_owner(self, identity: staff.StaffId) -> None:
        configuration = self.root / "config"
        configuration.mkdir(exist_ok=True)
        _ = (configuration / "console.conf").write_text(f"page.list.owner={identity}\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(configuration)})))

    def test_the_holder_follows_a_rename_and_a_new_conversation(self) -> None:
        first, second = fixture_runtime_id("keeper-first"), fixture_runtime_id("keeper-second")
        row = fixture_member("producer", name="fixture-keeper", runtime_id=first)
        self.configure_owner(row.id)
        self.assertEqual(list_identity.holder(), list_identity.ListProducerHolder(staff.StaffId(first)))
        updated = staff.update_current_session(row.current_session, staff.native_id(second))
        renamed = replace(updated, name="renamed-keeper")
        members = staff.StaffSnapshot((renamed,))
        with mock.patch.object(staff, "snapshot", return_value=members):
            self.assertEqual(list_identity.holder(), list_identity.ListProducerHolder(staff.StaffId(second)))
        self.assertEqual(staff.producer_name(row.id, members=members), renamed.name)

    def test_a_row_of_another_kind_or_id_is_not_the_holder(self) -> None:
        owner = fixture_member("showrunner", name="fixture-owner")
        _ = fixture_member("producer", name="fixture-keeper")
        self.configure_owner(owner.id)
        self.assertEqual(list_identity.holder(), list_identity.NoListProducerHolder())
        self.configure_owner(staff.StaffId(fixture_runtime_id("missing-keeper")))
        self.assertEqual(list_identity.holder(), list_identity.NoListProducerHolder())

    def test_the_owner_id_protects_a_producer_from_retirement(self) -> None:
        row = fixture_member("producer", name="renamed-keeper")
        self.configure_owner(row.id)
        with self.assertRaisesRegex(staff.StaffRefused, "the List producer cannot retire"):
            _ = staff.set_producer_retirement(row.id, staff.RetiredProducer("fixture"))

    def test_an_unreadable_or_absent_record_gives_no_holder(self) -> None:
        row = fixture_member("producer", name="fixture-keeper")
        self.configure_owner(row.id)
        for error in (staff.StaffUnreadable("natedev not reached"), staff.StaffAbsent("no staff record")):
            with (self.subTest(error=type(error).__name__), mock.patch.object(staff, "snapshot", side_effect=error),
                  mock.patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as retained):
                self.assertEqual(list_identity.holder(), list_identity.NoListProducerHolder())
            retained.assert_not_called()
