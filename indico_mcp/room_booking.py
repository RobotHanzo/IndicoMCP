"""Tools for Indico's room-booking system."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, PositiveInt
from werkzeug.exceptions import BadRequest, Conflict, Forbidden, NotFound

PageSize = Annotated[int, Field(ge=1, le=100)]
Offset = Annotated[int, Field(ge=0)]
BookingTime = Annotated[str, Field(description='ISO 8601 date and time in the Indico room-booking timezone')]
BookingState = Literal['pending', 'accepted', 'cancelled', 'rejected']
Repeat = Literal['once', 'daily', 'weekly', 'monthly']
Weekday = Literal['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def _check_access(user):
    from indico.core.config import config
    from indico.modules.rb.util import rb_check_user_access

    if not config.ENABLE_ROOMBOOKING:
        raise NotFound('Room booking is disabled on this Indico instance.')
    if not rb_check_user_access(user):
        raise Forbidden('You do not have access to room booking.')


def _get_room(room_id, *, lock=False):
    from indico.modules.rb.models.rooms import Room

    query = Room.query.filter_by(id=room_id, is_deleted=False)
    if lock:
        query = query.enable_eagerloads(False).with_for_update(of=Room)
    room = query.one_or_none()
    if room is None:
        raise NotFound('The room does not exist.')
    return room


def _get_booking(booking_id, *, lock=False):
    from indico.modules.rb.models.reservations import Reservation
    from indico.modules.rb.models.rooms import Room

    query = Reservation.query.join(Reservation.room).filter(Reservation.id == booking_id, ~Room.is_deleted)
    booking = query.one_or_none()
    if booking is None:
        raise NotFound('The room booking does not exist.')
    if lock:
        # Take locks in room-first order for all booking state changes.
        _get_room(booking.room_id, lock=True)
        booking = query.populate_existing().enable_eagerloads(False).with_for_update(of=Reservation).one_or_none()
        if booking is None:
            raise NotFound('The room booking no longer exists.')
    return booking


def _time_range(start, end, *, single_day):
    def parse(value):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?', value):
            raise BadRequest('Use a local date and time such as 2027-07-12T09:30.')
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise BadRequest('The local date or time is invalid.') from exc

    start_dt = parse(start)
    end_dt = parse(end)
    if start_dt >= end_dt or start_dt.time() >= end_dt.time():
        raise BadRequest('The booking end time must be after the start time on each occurrence date.')
    if single_day and start_dt.date() != end_dt.date():
        raise BadRequest('A one-time booking must start and end on the same date.')
    return start_dt, end_dt


def _require_confirmed(confirm):
    if confirm is not True:
        raise BadRequest('Set confirm to true to authorize this booking change.')


def _describe_booking(booking, user):
    from indico.core.config import config

    result = {
        'id': booking.id,
        'room_id': booking.room_id,
        'room_name': booking.room.full_name,
        'start': booking.start_dt.isoformat(),
        'end': booking.end_dt.isoformat(),
        'timezone': config.DEFAULT_TIMEZONE,
        'state': booking.state.name,
        'repeat_frequency': booking.repeat_frequency.name.lower(),
        'repeat_interval': booking.repeat_interval,
        'recurrence_weekdays': booking.recurrence_weekdays,
        'url': booking.external_details_url,
    }
    if booking.can_see_details(user):
        result['reason'] = booking.booking_reason
        result['booked_for'] = booking.booked_for_name
        result['rejection_reason'] = booking.rejection_reason
    return result


def register_tools(registry):
    from indico.core.config import config
    from indico.core.db import db
    from indico.core.errors import NoReportError
    from indico.modules.rb import rb_settings
    from indico.modules.rb.models.reservation_occurrences import ReservationOccurrence
    from indico.modules.rb.models.reservations import Reservation, ReservationState
    from indico.modules.rb.models.rooms import Room
    from indico.modules.rb.operations.bookings import check_room_available
    from indico.modules.rb.operations.rooms import search_for_rooms
    from indico.modules.rb.schemas import CreateBookingSchema, rooms_schema
    from indico.modules.rb.util import get_prebooking_collisions, is_booking_start_within_grace_period
    from indico.util.date_time import now_utc, utc_to_server
    from marshmallow import ValidationError

    frequencies = {'once': 'NEVER', 'daily': 'DAY', 'weekly': 'WEEK', 'monthly': 'MONTH'}

    @registry.tool()
    def indico_list_rooms(
        user, query: str | None = None, capacity: PositiveInt | None = None, location_id: PositiveInt | None = None,
        building: str | None = None, equipment: list[str] | None = None, favorites_only: bool = False,
        limit: PageSize = 50, offset: Offset = 0,
    ):
        """Find rooms by name, capacity, location, equipment, or your favorites."""
        _check_access(user)
        filters = {'text': query, 'capacity': capacity, 'location_id': location_id, 'building': building,
                   'equipment': list(dict.fromkeys(equipment or ())), 'favorite': favorites_only}
        rooms = search_for_rooms({name: value for name, value in filters.items() if value is not None})
        page = rooms.order_by(Room.id).offset(offset).limit(limit + 1).all()
        return {'rooms': rooms_schema.dump(page[:limit]), 'next_offset': offset + limit if len(page) > limit else None}

    @registry.tool()
    def indico_get_room(user, room_id: PositiveInt):
        """Read room details and the caller's booking permissions."""
        _check_access(user)
        room = _get_room(room_id)
        return {'room': rooms_schema.dump(room, many=False),
                'can_book': room.can_book(user, allow_admin=False),
                'can_prebook': room.can_prebook(user, allow_admin=False),
                'can_moderate': room.can_moderate(user)}

    @registry.tool()
    def indico_check_room_availability(user, room_id: PositiveInt, start: BookingTime, end: BookingTime):
        """Check for bookings or blocked times that overlap a proposed single-day slot."""
        _check_access(user)
        room = _get_room(room_id)
        start_dt, end_dt = _time_range(start, end, single_day=True)
        return {'room_id': room.id, 'timezone': config.DEFAULT_TIMEZONE, **check_room_available(room, start_dt, end_dt)}

    @registry.tool()
    def indico_list_my_room_bookings(user, state: BookingState | None = None, upcoming_only: bool = True,
                                     limit: PageSize = 50, offset: Offset = 0):
        """List reservations you created or that were booked for you."""
        _check_access(user)
        query = (Reservation.query.join(Reservation.room).filter(
            ~Room.is_deleted, db.or_(Reservation.created_by_id == user.id, Reservation.booked_for_id == user.id),
        ))
        if state is not None:
            query = query.filter(Reservation.state == ReservationState[state])
        if upcoming_only:
            query = query.filter(Reservation.end_dt >= utc_to_server(now_utc()).replace(tzinfo=None))
        bookings = query.order_by(Reservation.start_dt, Reservation.id).offset(offset).limit(limit + 1).all()
        return {'bookings': [_describe_booking(booking, user) for booking in bookings[:limit]],
                'next_offset': offset + limit if len(bookings) > limit else None}

    @registry.tool()
    def indico_get_room_booking(user, booking_id: PositiveInt):
        """Read a room booking using Indico's booking-detail visibility rules."""
        _check_access(user)
        booking = _get_booking(booking_id)
        return {'booking': _describe_booking(booking, user), 'can_cancel': booking.can_cancel(user, allow_admin=False),
                'can_approve': booking.can_accept(user), 'can_reject': booking.can_reject(user)}

    @registry.tool()
    def indico_list_room_booking_requests(user, room_id: PositiveInt, limit: PageSize = 50, offset: Offset = 0):
        """List pending requests for a room you can moderate."""
        _check_access(user)
        room = _get_room(room_id)
        if not room.can_moderate(user):
            raise Forbidden('You do not have permission to moderate this room.')
        query = Reservation.query.filter_by(room_id=room.id, state=ReservationState.pending)
        requests = query.order_by(Reservation.start_dt, Reservation.id).offset(offset).limit(limit + 1).all()
        return {'bookings': [_describe_booking(booking, user) for booking in requests[:limit]],
                'next_offset': offset + limit if len(requests) > limit else None}

    @registry.tool(read_only=False, idempotent=True)
    def indico_set_favorite_room(user, room_id: PositiveInt, favorite: bool):
        """Add or remove a room from your favorites."""
        _check_access(user)
        room = _get_room(room_id, lock=True)
        if favorite:
            user.favorite_rooms.add(room)
        else:
            user.favorite_rooms.discard(room)
        return {'room_id': room.id, 'favorite': favorite}

    @registry.tool(read_only=False, open_world=True)
    def indico_create_room_booking(
        user, room_id: PositiveInt, start: BookingTime, end: BookingTime, prebooking: bool, confirm: bool,
        reason: str = '', repeat: Repeat = 'once', repeat_every: PositiveInt = 1,
        weekdays: list[Weekday] | None = None, allow_partial: bool = False,
    ):
        """Reserve a room for yourself or request approval. Recurrences repeat the same local time slot."""
        _check_access(user)
        _require_confirmed(confirm)
        start_dt, end_dt = _time_range(start, end, single_day=repeat == 'once')
        if (repeat in ('once', 'daily') and repeat_every != 1) or (weekdays and repeat != 'weekly'):
            raise BadRequest('The repeat interval or weekdays do not apply to this recurrence.')
        room = _get_room(room_id, lock=True)
        can_book = room.can_prebook if prebooking else room.can_book
        if not can_book(user, allow_admin=False):
            raise Forbidden('You do not have permission to reserve this room in the selected mode.')
        if not is_booking_start_within_grace_period(start_dt, user):
            raise BadRequest('The booking starts in the past or outside the configured grace period.')
        booking_limit = room.booking_limit_days or rb_settings.get('booking_limit')
        if (end_dt.date() - start_dt.date()).days > booking_limit:
            raise BadRequest(f'The booking period exceeds the room limit of {booking_limit} days.')

        values = {'start_dt': start_dt.isoformat(), 'end_dt': end_dt.isoformat(), 'room_id': room.id,
                  'repeat_frequency': frequencies[repeat], 'repeat_interval': 0 if repeat == 'once' else repeat_every,
                  'reason': reason, 'is_prebooking': prebooking}
        if weekdays:
            values['recurrence_weekdays'] = weekdays
        try:
            data = CreateBookingSchema().load(values)
        except ValidationError as exc:
            raise BadRequest(str(exc)) from exc
        data.pop('room_id')
        data.pop('is_prebooking')
        data['booked_for_user'] = user
        repetition = data['repeat_frequency'], data['repeat_interval'], data.get('recurrence_weekdays')
        expected = sum(1 for _ in ReservationOccurrence.iter_start_time(start_dt, end_dt, repetition))
        if not expected:
            raise BadRequest('No requested occurrence falls within the booking period.')
        try:
            booking = Reservation.create_from_data(room, data, user, prebook=prebooking, ignore_admin=True)
        except NoReportError as exc:
            raise Conflict(str(exc)) from exc
        valid = booking.occurrences.filter(ReservationOccurrence.is_valid).count()
        if valid != expected and not allow_partial:
            raise Conflict('Some requested slots are unavailable. Review the calendar before accepting fewer slots.')
        return {'booking': _describe_booking(booking, user), 'booked_occurrences': valid,
                'skipped_occurrences': expected - valid}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_cancel_room_booking(user, booking_id: PositiveInt, confirm: bool):
        """Cancel a booking you created or that was booked for you, using Indico's cancellation window."""
        _check_access(user)
        _require_confirmed(confirm)
        booking = _get_booking(booking_id, lock=True)
        if not booking.is_owned_by(user) and not booking.is_booked_for(user):
            raise Forbidden('You can only cancel your own room bookings with this tool.')
        if not booking.is_cancelled:
            if not booking.can_cancel(user, allow_admin=False):
                raise Conflict('The booking is not eligible for cancellation.')
            booking.cancel(user)
        return {'booking': _describe_booking(booking, user)}

    @registry.tool(read_only=False, destructive=True, idempotent=True, open_world=True)
    def indico_decide_room_booking(
        user, booking_id: PositiveInt, decision: Literal['approve', 'reject'], confirm: bool, reason: str | None = None,
    ):
        """Approve or reject a room booking as a room moderator. Conflicting approvals require the Indico UI."""
        _check_access(user)
        _require_confirmed(confirm)
        booking = _get_booking(booking_id, lock=True)
        if not booking.room.can_moderate(user):
            raise Forbidden('You do not have permission to moderate this room.')
        if decision == 'approve' and not booking.is_accepted:
            if not booking.can_accept(user):
                raise Conflict('Only pending bookings can be approved.')
            if get_prebooking_collisions(booking):
                raise Conflict('The booking overlaps other requests. Review the conflicts in the Indico room calendar.')
            booking.accept(user, reason)
        elif decision == 'reject' and not booking.is_rejected:
            if not booking.can_reject(user):
                raise Conflict('This booking cannot be rejected in its current state.')
            if not reason or not reason.strip():
                raise BadRequest('A reason is required to reject a booking.')
            booking.reject(user, reason)
        return {'booking': _describe_booking(booking, user)}
