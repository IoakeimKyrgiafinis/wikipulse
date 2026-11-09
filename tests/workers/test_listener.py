"""The listener: notifications on a (fake) Redis channel trigger the handler."""

import fakeredis
import redis

from tests.workers.helpers import (
    PREFIX,
    MemoryStats,
    context,
    edit_by,
    invoker_for,
    key_of,
    local_lake,
    s3_message,
    write_hour,
)
from wikipulse.workers.listener import NotificationListener

CHANNEL = "test:notifications"


class PublishingCatchUp:
    """Stands in for the backfill; publishes messages once the listener is subscribed."""

    def __init__(self, client, messages: list[str]) -> None:
        self.client = client
        self.messages = messages
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        for message in self.messages:
            self.client.publish(CHANNEL, message)


def test_a_notification_triggers_the_stats_row(tmp_path) -> None:
    bucket, store = local_lake(tmp_path)
    edits = [edit_by("a", 1, "P", "Ann"), edit_by("b", 2, "Q", "Bob", is_bot=True)]
    write_hour(store, edits)
    client = fakeredis.FakeRedis(decode_responses=True)
    stats = MemoryStats()
    ctx = context(stats)
    hour = edits[0].event_time.replace(minute=0)
    catch_up = PublishingCatchUp(
        client,
        [
            s3_message("another-bucket", key_of(hour)),  # not ours: ignored
            "garbage",  # malformed: logged and ignored
            s3_message(bucket, key_of(hour)),
            s3_message(bucket, key_of(hour)),  # delivered twice: same row
        ],
    )
    listener = NotificationListener(
        client, CHANNEL, invoker_for(ctx, client), bucket, catch_up=catch_up, poll_timeout_s=0.05
    )
    result = listener.run(max_events=2, idle_exit_s=2)
    assert catch_up.calls == 1
    assert (result.messages, result.events, result.ignored, result.malformed) == (4, 2, 1, 1)
    row = stats.rows[(f"{bucket}/{PREFIX}", hour)]
    assert (row.edits, row.distinct_editors, row.bot_share) == (2, 2, 0.5)
    assert stats.writes == 2 and len(stats.rows) == 1


def test_stops_when_idle_and_on_request(tmp_path) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    listener = NotificationListener(
        client, CHANNEL, invoker_for(context(), client), "b", poll_timeout_s=0.01
    )
    assert listener.run(idle_exit_s=0.05).messages == 0
    assert listener.run(should_stop=lambda: True).messages == 0


class FailingOnce:
    """A Redis client whose first subscription fails as if Redis were down."""

    def __init__(self, real) -> None:
        self.real = real
        self.failed = False

    def pubsub(self, **kwargs):
        pubsub = self.real.pubsub(**kwargs)
        if not self.failed:
            self.failed = True

            def broken(*args, **kw):
                raise redis.ConnectionError("Redis is down")

            pubsub.subscribe = broken
        return pubsub


def test_lost_connection_resubscribes_and_catches_up_again() -> None:
    real = fakeredis.FakeRedis(decode_responses=True)
    catch_ups: list[int] = []
    waits: list[float] = []
    listener = NotificationListener(
        FailingOnce(real),
        CHANNEL,
        invoker_for(context(), real),
        "b",
        catch_up=lambda: catch_ups.append(1),
        poll_timeout_s=0.01,
        sleep=waits.append,
    )
    stats = listener.run(idle_exit_s=0.05)
    assert stats.reconnects == 1 and waits == [1.0]
    assert catch_ups == [1]  # after the successful subscription
