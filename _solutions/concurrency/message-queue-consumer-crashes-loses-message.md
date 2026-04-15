---
layout: solution
title: "Message Queue Consumer Crashes and Loses Message — No Dead Letter Queue"
category: concurrency
description: "Agent consumer dequeues a message, starts processing, crashes mid-way. Message is gone — not requeued, not logged. Work is silently lost. No dead letter queue configured."
tags: [message-queue, dead-letter, consumer, crash, redis, rabbitmq, sqs]
---

# Message Queue Consumer Crashes and Loses Message — No Dead Letter Queue

## Symptom
- Tasks submitted to queue never complete and leave no trace
- Queue depth goes to 0 but expected output never appears
- Consumer crashes → message disappears → no error visible
- After restart, consumer processes new messages but missed ones are gone
- Work items lost under load when multiple consumers compete

## Root Cause
Most queues require explicit acknowledgment after processing. If a consumer crashes before ack-ing, the message can be lost (depending on queue config). Without a dead letter queue (DLQ), failed messages have nowhere to go — they're silently dropped after max retry attempts.

## Fix

### Option 1: Acknowledge only after successful processing

```python
import redis

r = redis.Redis()

def process_with_ack(queue_name: str):
    """Use BRPOPLPUSH for reliable dequeue — message stays in processing list"""
    processing_queue = f"{queue_name}:processing"

    while True:
        # Atomically move from queue to processing list
        message = r.brpoplpush(queue_name, processing_queue, timeout=5)
        if not message:
            continue

        try:
            process_message(message)
            # Only remove from processing after success
            r.lrem(processing_queue, 1, message)

        except Exception as e:
            print(f"Processing failed: {e}")
            # Move to dead letter queue instead of losing it
            r.rpush(f"{queue_name}:dead_letter", message)
            r.lrem(processing_queue, 1, message)
```

### Option 2: AWS SQS with visibility timeout and DLQ

```python
import boto3, json

sqs = boto3.client("sqs", region_name="us-east-1")
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123/agent-tasks"

def consume_with_dlq():
    while True:
        response = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=1,
            VisibilityTimeout=300,  # Message invisible to other consumers for 5min
            WaitTimeSeconds=20      # Long polling
        )

        messages = response.get("Messages", [])
        if not messages:
            continue

        for msg in messages:
            receipt_handle = msg["ReceiptHandle"]
            body = json.loads(msg["Body"])

            try:
                process_task(body)
                # Success: delete from queue
                sqs.delete_message(
                    QueueUrl=QUEUE_URL,
                    ReceiptHandle=receipt_handle
                )
            except Exception as e:
                print(f"Task failed: {e}")
                # Don't delete — SQS will retry (up to maxReceiveCount)
                # After maxReceiveCount, SQS automatically moves to DLQ

# SQS DLQ config (set via AWS console or Terraform):
# RedrivePolicy: {"deadLetterTargetArn": "arn:aws:sqs:...:agent-tasks-dlq", "maxReceiveCount": 3}
```

### Option 3: RabbitMQ with acknowledgments and DLQ

```python
import pika, json

def consume_rabbitmq():
    connection = pika.BlockingConnection(pika.ConnectionParameters("localhost"))
    channel = connection.channel()

    # Declare main queue with DLQ routing
    channel.queue_declare(
        queue="agent_tasks",
        durable=True,
        arguments={
            "x-dead-letter-exchange": "agent_tasks_dlx",
            "x-message-ttl": 3600000,  # 1 hour TTL
            "x-max-length": 10000
        }
    )

    # Declare dead letter exchange and queue
    channel.exchange_declare(exchange="agent_tasks_dlx", exchange_type="direct")
    channel.queue_declare(queue="agent_tasks_dead", durable=True)
    channel.queue_bind("agent_tasks_dead", "agent_tasks_dlx", routing_key="agent_tasks")

    def callback(ch, method, properties, body):
        try:
            task = json.loads(body)
            process_task(task)
            # Explicit ack — message removed from queue
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            print(f"Task failed: {e}")
            # Nack without requeue — sends to DLQ
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    channel.basic_qos(prefetch_count=1)  # One message at a time per consumer
    channel.basic_consume("agent_tasks", callback)
    channel.start_consuming()
```

### Option 4: Recover orphaned messages on startup

```python
import redis, time

def recover_orphaned_messages(queue_name: str, processing_queue: str, max_age_seconds: int = 300):
    """Re-queue messages stuck in processing (consumer crashed mid-process)"""
    r = redis.Redis()
    processing_messages = r.lrange(processing_queue, 0, -1)

    for msg in processing_messages:
        # Check if message has been in processing too long
        # (requires storing timestamp with message)
        try:
            data = json.loads(msg)
            started_at = data.get("processing_started_at", 0)
            if time.time() - started_at > max_age_seconds:
                print(f"Recovering orphaned message: {data.get('id')}")
                r.lrem(processing_queue, 1, msg)
                r.rpush(queue_name, msg)  # Re-queue
        except Exception as e:
            print(f"Could not recover message: {e}")

# Call at consumer startup
recover_orphaned_messages("agent_tasks", "agent_tasks:processing")
```

### Option 5: Monitor DLQ for alerting

```python
async def monitor_dead_letter_queue(dlq_name: str):
    """Alert when messages land in DLQ"""
    r = redis.Redis()

    while True:
        dlq_length = r.llen(dlq_name)
        if dlq_length > 0:
            # Alert and attempt to inspect
            print(f"ALERT: {dlq_length} messages in dead letter queue '{dlq_name}'")
            # Sample the oldest failure
            sample = r.lindex(dlq_name, 0)
            print(f"Sample failed message: {sample}")

        await asyncio.sleep(60)  # Check every minute
```

## Queue Reliability Comparison

| Queue | Auto-retry | DLQ support | Ack required | Best for |
|---|---|---|---|---|
| Redis LPOP | No | Manual | No | Simple queues, low criticality |
| Redis BRPOPLPUSH | Manual | Manual | Manual | Reliable Redis queuing |
| AWS SQS | Yes (up to maxReceiveCount) | Yes (built-in) | Yes (delete) | AWS-native, high scale |
| RabbitMQ | Yes (nack+requeue) | Yes (DLX) | Yes (ack/nack) | Enterprise messaging |
| Celery | Yes (configurable) | Yes | Yes | Python task queues |
| BullMQ | Yes | Yes | Yes | Node.js |

## Expected Token Savings
Debugging lost messages after crash: ~10,000 tokens
DLQ + ack pattern prevents loss: messages are always recoverable

## Environment
- Any agent system using message queues for task distribution
- Source: direct experience with production agent deployments
