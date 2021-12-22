import asyncio
import logging
from typing import List

from app.api.console_log import ConsoleLog
from app.config import server
from tracardi.event_server.utils.memory_cache import MemoryCache, CacheItem
from tracardi_dot_notation.dot_accessor import DotAccessor

from tracardi.domain.event_payload_validator import EventPayloadValidator
from tracardi.domain.value_object.bulk_insert_result import BulkInsertResult

from tracardi.domain.console import Console
from tracardi.service.event_validator import validate
from tracardi.domain.event import Event, VALIDATED, ERROR, WARNING, OK
from tracardi.domain.profile import Profile
from app.api.track.service.merging import merge
from app.api.track.service.segmentation import segment
from tracardi.domain.session import Session
from tracardi.domain.value_object.tracker_payload_result import TrackerPayloadResult
from tracardi.config import tracardi
from tracardi.exceptions.exception import UnauthorizedException, StorageException, FieldTypeConflictException
from tracardi.process_engine.rules_engine import RulesEngine
from tracardi.domain.value_object.collect_result import CollectResult
from tracardi.domain.payload.tracker_payload import TrackerPayload
from tracardi.service.storage.driver import storage
from tracardi.service.storage.factory import StorageFor, StorageForBulk
from tracardi.service.storage.drivers.elastic.debug_info import save_debug_info
from tracardi.service.storage.helpers.source_cacher import source_cache

logger = logging.getLogger('app.api.track.service.tracker')
cache = MemoryCache()


async def _persist(console_log: ConsoleLog, session: Session, events: List[Event],
                   tracker_payload: TrackerPayload, profile: Profile = None) -> CollectResult:
    # Save profile
    try:
        if profile.operation.new:
            save_profile_result = await storage.driver.profile.save_profile(profile)
        else:
            save_profile_result = BulkInsertResult()
    except StorageException as e:
        raise FieldTypeConflictException("Could not save profile. Error: {}".format(e.message), rows=e.details)

    # Save session
    try:
        persist_session = False if tracker_payload.is_disabled('saveSession') else True
        save_session_result = await storage.driver.session.save_session(session, profile, persist_session)
    except StorageException as e:
        raise FieldTypeConflictException("Could not save session. Error: {}".format(e.message), rows=e.details)

    # Save events
    try:
        persist_events = False if tracker_payload.is_disabled('saveEvents') else True

        # Set statuses
        log_event_journal = console_log.get_indexed_event_journal()
        for event in events:
            if event.id in log_event_journal:
                log = log_event_journal[event.id]
                if log.is_error():
                    event.metadata.status = ERROR
                    continue
                elif log.is_warning():
                    event.metadata.status = WARNING
                    continue
                else:
                    event.metadata.status = OK

        save_events_result = await storage.driver.event.save_events(events, persist_events)
    except StorageException as e:
        raise FieldTypeConflictException("Could not save event. Error: {}".format(e.message), rows=e.details)

    return CollectResult(
        session=save_session_result,
        profile=save_profile_result,
        events=save_events_result
    )


async def validate_events_json_schemas(events, profile, session):
    for event in events:
        dot = DotAccessor(
            profile=profile,
            session=session,
            payload=None,
            event=event,
            flow=None
        )
        event_type = dot.event['type']

        if event_type not in cache:
            event_payload_validator = await storage.driver.validation_schema.load_schema(
                dot.event['type'])  # type: EventPayloadValidator
            cache[event_type] = CacheItem(data=event_payload_validator, ttl=server.event_validator_ttl)

        validation_data = cache[event_type].data
        if validation_data is not None:
            event.metadata.status = VALIDATED
            validate(dot, validator=validation_data)


async def track_event(tracker_payload: TrackerPayload, ip: str):
    tracker_payload.metadata.ip = ip

    try:
        source = await source_cache.validate_source(source_id=tracker_payload.source.id)
    except ValueError as e:
        raise UnauthorizedException(e)

    # Get session
    if tracker_payload.session.id is None:
        raise UnauthorizedException("Session must be set.")

    # Load session from storage
    session = await StorageFor(tracker_payload.session).index("session").load(Session)  # type: Session

    # Get profile
    profile, session = await tracker_payload.get_profile_and_session(session,
                                                                     storage.driver.profile.load_merged_profile)

    # Get events
    events = tracker_payload.get_events(session, profile)

    # Validates json schemas of events, throws exception if data is not valid
    await validate_events_json_schemas(events, profile, session)

    debug_info_by_event_type_and_rule_name = None
    segmentation_result = None

    console_log = ConsoleLog()
    rules_engine = RulesEngine(
        session,
        profile,
        events_rules=storage.driver.rule.load_rules(events),
        console_log=console_log
    )

    post_invoke_events = None
    try:

        # Invoke rules engine
        debug_info_by_event_type_and_rule_name, ran_event_types, console_log, post_invoke_events = await rules_engine.invoke(
            storage.driver.flow.load_production_flow,
            tracker_payload.source.id)

        # Segment
        segmentation_result = await segment(rules_engine.profile,
                                            ran_event_types,
                                            storage.driver.segment.load_segment_by_event_type)

    except Exception as e:
        message = 'Rules engine or segmentation returned an error `{}`'.format(str(e))
        console_log.append(
            Console(
                profile_id=rules_engine.profile.id,
                origin='profile',
                class_name='RulesEngine',
                module='tracker',
                type='error',
                message=message
            )
        )
        logger.error(message)

    save_tasks = []
    try:
        # Merge
        profiles_to_disable = await merge(rules_engine.profile, limit=2000)
        if profiles_to_disable is not None:
            task = asyncio.create_task(
                StorageForBulk(profiles_to_disable).index('profile').save())
            save_tasks.append(task)
    except Exception as e:
        message = 'Profile merging returned an error `{}`'.format(str(e))
        logger.error(message)
        console_log.append(
            Console(
                profile_id=rules_engine.profile.id,
                origin='profile',
                class_name='merge',
                module='app.api.track.service',
                type='error',
                message=message
            )
        )

    # Must be the last operation
    try:
        if rules_engine.profile.operation.needs_update():
            await storage.driver.profile.save_profile(profile)
    except Exception as e:
        message = "Profile update returned an error: `{}`".format(str(e))
        console_log.append(
            Console(
                profile_id=rules_engine.profile.id,
                origin='profile',
                class_name='tracker',
                module='tracker',
                type='error',
                message=message
            )
        )
        logger.error(message)

    try:

        # Save debug info
        save_tasks.append(
            asyncio.create_task(
                storage.driver.debug_info.save_debug_info(
                    debug_info_by_event_type_and_rule_name)))

        # Run tasks
        await asyncio.gather(*save_tasks)

    except Exception as e:
        message = "Error during debug info or disabling profiles.: `{}`".format(str(e))
        logger.error(message)
        console_log.append(
            Console(
                profile_id=rules_engine.profile.id,
                origin='profile',
                class_name='tracker',
                module='tracker',
                type='error',
                message=message
            )
        )

    finally:
        # todo maybe persisting profile is not necessary - it is persisted right after workflow - see above
        # TODO notice that profile is saved only when it's new change it when it need update
        # Save profile, session, events

        # Synchronize post invoke events. Replace events with events changed by WF.
        # Events are saved only if marked in event.update==true
        if post_invoke_events is not None:
            synced_events = []
            for ev in events:
                if ev.update is True and ev.id in post_invoke_events:
                    synced_events.append(post_invoke_events[ev.id])
                else:
                    synced_events.append(ev)

            events = synced_events

        collect_result = await _persist(console_log, session, events, tracker_payload, profile)

        # Save console log
        if console_log:
            save_tasks.append(asyncio.create_task(StorageForBulk(console_log).index('console-log').save()))

    # Prepare response
    result = {}

    # Debugging
    # todo save result to different index
    if not tracardi.track_debug and not tracker_payload.is_disabled('debugger'):
        debug_result = TrackerPayloadResult(**collect_result.dict())
        debug_result = debug_result.dict()
        debug_result['execution'] = debug_info_by_event_type_and_rule_name
        debug_result['segmentation'] = segmentation_result
        debug_result['logs'] = console_log
        result['debugging'] = debug_result

    # Add profile to response
    if tracker_payload.return_profile():
        result["profile"] = profile.dict(
            exclude={
                "traits": {"private": ...},
                "pii": ...,
                "operation": ...
            })
    else:
        result["profile"] = profile.dict(include={"id": ...})

    # Add source to response
    result['source'] = source.dict(include={"consent": ...})

    return result
