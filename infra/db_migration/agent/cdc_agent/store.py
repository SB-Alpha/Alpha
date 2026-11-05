"""Job state kept in a compacted Kafka topic (key = job_id).

Agents are stateless: every read replays the topic, so any of the 3 agents
returns the same view and a restarted agent picks up where another left off.
"""

import json
import uuid

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from cdc_agent.config import Settings
from cdc_agent.models import Job


def read_topic(settings: Settings, topic: str) -> dict[str, bytes | None]:
    """Replay a compacted topic: latest value per key, None for a deletion."""
    consumer = Consumer({
        "bootstrap.servers": settings.kafka_bootstrap,
        "group.id": f"alpha-cdc-agent-read-{uuid.uuid4()}",
        "enable.auto.commit": False,
        "enable.partition.eof": True,
    })
    records: dict[str, bytes | None] = {}
    try:
        low, high = consumer.get_watermark_offsets(TopicPartition(topic, 0), timeout=10)
        if high <= low:
            return records
        consumer.assign([TopicPartition(topic, 0, low)])
        while True:
            msg = consumer.poll(5)
            if msg is None:
                raise RuntimeError(f"timed out reading {topic}")
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    break
                raise KafkaException(msg.error())
            records[msg.key().decode()] = msg.value()
            if msg.offset() + 1 >= high:
                break
    finally:
        consumer.close()
    return records


class JobStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._topic = settings.state_topic
        self._producer = Producer({
            "bootstrap.servers": settings.kafka_bootstrap,
            "acks": "all",
            "enable.idempotence": True,
        })

    def ensure_topic(self) -> None:
        self.create_topics([self._topic], compact=True)

    def create_topics(self, names: list[str], compact: bool = False) -> None:
        """Create missing topics with the cluster's replication settings."""
        admin = AdminClient({"bootstrap.servers": self._settings.kafka_bootstrap})
        existing = admin.list_topics(timeout=10).topics
        rf = self._settings.state_replication_factor
        config = {"min.insync.replicas": str(max(1, rf - 1))}
        if compact:
            config["cleanup.policy"] = "compact"
        topics = [NewTopic(n, num_partitions=1, replication_factor=rf, config=config)
                  for n in names if n not in existing]
        if not topics:
            return
        for future in admin.create_topics(topics).values():
            try:
                future.result()
            except KafkaException as exc:
                if "TOPIC_ALREADY_EXISTS" not in str(exc):
                    raise

    def put(self, job: Job) -> None:
        self._producer.produce(self._topic, key=job.job_id, value=job.model_dump_json())
        if self._producer.flush(10) != 0:
            raise RuntimeError("job state was not acknowledged by Kafka")

    def all(self) -> dict[str, Job]:
        records = read_topic(self._settings, self._topic)
        return {key: Job.model_validate(json.loads(value)) for key, value in records.items() if value is not None}

    def get(self, job_id: str) -> Job | None:
        return self.all().get(job_id)
