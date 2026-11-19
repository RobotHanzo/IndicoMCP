"""Exercise room booking with Indico and the integration test's PostgreSQL server."""

from datetime import time, timedelta

from integration_support import _call
from integration_support import pytestmark as pytestmark


def test_reservation_and_cancellation_persist_and_keep_owner_permissions(indico_app, token):
    from indico.core.db import db
    from indico.modules.rb import rb_settings
    from indico.modules.rb.models.locations import Location
    from indico.modules.rb.models.rooms import Room
    from indico.modules.users import User
    from indico.util.date_time import now_utc, utc_to_server

    with indico_app.app_context():
        owner = User.query.filter_by(first_name='Manager').one()
        location = Location(name='MCP test location')
        room = Room(location=location, owner=owner, building='MCP', floor='1', number='10', capacity=20)
        db.session.add(room)
        rb_settings.set('hide_booking_details', True)
        db.session.flush()
        room_id = room.id
        db.session.commit()
        date = utc_to_server(now_utc()).date() + timedelta(days=2)

    client = indico_app.test_client()
    actor = token['participant']
    other = token['other']
    slot = {'room_id': room_id, 'start': f'{date}T{time(9).isoformat()}', 'end': f'{date}T{time(10).isoformat()}'}
    rooms = _call(client, actor, 'indico_list_rooms', building='MCP')['rooms']
    assert [room['id'] for room in rooms] == [room_id]
    assert not _call(client, actor, 'indico_check_room_availability', **slot)['conflict_booking']

    placed = _call(client, actor, 'indico_create_room_booking', **slot, prebooking=False, confirm=True,
                   reason='MCP booking test')
    booking = placed['booking']
    booking_id = booking['id']
    assert booking['state'] == 'accepted'
    assert placed['booked_occurrences'] == 1
    client = indico_app.test_client()
    assert _call(client, actor, 'indico_check_room_availability', **slot)['conflict_booking']
    assert [item['id'] for item in _call(client, actor, 'indico_list_my_room_bookings')['bookings']] == [booking_id]
    assert not _call(client, other, 'indico_list_my_room_bookings')['bookings']
    read_by_other = _call(client, other, 'indico_get_room_booking', booking_id=booking_id)
    assert 'reason' not in read_by_other['booking'] and not read_by_other['can_cancel']
    denied = _call(client, other, 'indico_cancel_room_booking', booking_id=booking_id, confirm=True, error=True)
    assert denied['error'] == 'Forbidden'

    _call(client, actor, 'indico_cancel_room_booking', booking_id=booking_id, confirm=True)
    client = indico_app.test_client()
    reloaded = _call(client, actor, 'indico_get_room_booking', booking_id=booking_id)['booking']
    assert reloaded['state'] == 'cancelled'
    assert not _call(client, actor, 'indico_check_room_availability', **slot)['conflict_booking']
