from websockets.asyncio.client import connect
from datetime import datetime, timedelta
from contextlib import contextmanager
from confluent_kafka import Producer
from kazoo.client import KazooClient
from kazoo.recipe.election import Election
from protocols.bmp import BMPv3
import rocksdbpy
import ipaddress
import logging
import asyncio
import socket
import signal
import json
import os

# Environment Variables
WEBSOCKET_URI = "wss://ris-live.ripe.net/v1/ws/"
WEBSOCKET_IDENTITY = f"ris-kafka-{socket.gethostname()}"
QUEUE_SIZE = int(os.getenv("RRC_QUEUE_SIZE", 10000))
BUFFER_SIZE = int(os.getenv("RRC_BUFFER_SIZE", 10000))
BATCH_SIZE = int(os.getenv("RRC_BATCH_SIZE", 1000))
ZOOKEEPER_CONNECT = os.getenv("RRC_ZOOKEEPER_CONNECT")
KAFKA_CONNECT = os.getenv("RRC_KAFKA_CONNECT")
LOG_LEVEL = os.getenv("RRC_LOG_LEVEL", "INFO").upper()
RIS_HOST = os.getenv("RRC_HOST").lower()

# Logger
logger = logging.getLogger(__name__)
log_level = os.getenv('LOG_LEVEL', LOG_LEVEL).upper()
logger.setLevel(getattr(logging, log_level, logging.INFO))
ch = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Zookeeper
zk = KazooClient(hosts=ZOOKEEPER_CONNECT)
zk.start()
election = Election(zk, f'collectors/{RIS_HOST}')

# Consumer Task
async def consumer_task(queue, memory):
    """
    A single task that continuously consumes messages from the websocket.

    Args:
        buffer (CircularBuffer): The circular buffer.
        memory (dict): The memory dictionary.
    """
    try:
        async with connect(f"{WEBSOCKET_URI}?client={WEBSOCKET_IDENTITY}") as ws:
            await ws.send(json.dumps({"type": "ris_subscribe", "data": {"host": RIS_HOST, "socketOptions": {"includeRaw": True}}}))
            batch = []
            
            async for data in ws:
                memory['receive_counter'][0] += len(data)
                marshal = json.loads(data)['data']

                if marshal['type'] == "ris_error":
                    raise Exception(f"Websocket error: {marshal['message']}")

                # Allow only specific messages
                # TODO: Implement also "STATE" and "RIS_PEER_STATE"
                #       Blocked by: https://fosstodon.org/@robinroeper/113909690169940714
                if marshal['type'] not in ["UPDATE", "OPEN", "NOTIFICATION", "KEEPALIVE"]:
                    continue

                # Add message to buffer
                batch.append(marshal)

                # Sort and reset batch
                if len(batch) == BATCH_SIZE:                        
                    buffer = sorted(batch, key=lambda item: (
                        int(ipaddress.ip_address(item["peer"])), # Group by peer (same peer together)
                        item["timestamp"], # Within each peer, sort by timestamp (earliest first)
                        int(item["id"].split('-')[-1], 16)  # If multiple messages have the same timestamp, order by sequence_id
                    ))
                    for item in buffer:
                        if memory['is_leader'] and queue.full():
                            raise Exception("Unable to keep up with message influx")
                        elif not memory['is_leader'] and queue.full():
                            queue.get() # Discard oldest message
                        await queue.put(item)
                    batch = []
                
    except asyncio.CancelledError:
        raise
                
# Sender Task
async def sender_task(producer, election, db, queue, memory):
    """
    A single task that continuously sends messages to Kafka.

    Args:
        producer (confluent_kafka.Producer): The Kafka producer.
        db (rocksdbpy.DB): The RocksDB database.
        queue (asyncio.Queue): The message queue.
        memory (dict): The memory dictionary.
    """
    try:
        latest = None
        initialized = False
        produced_size = 0
        reported_size = 0
        batch_size = 0
        seeked = False
        item = None

        # Wait until we acquire leadership
        await asyncio.to_thread(election.run, lambda: None)

        # Register as leader
        memory['is_leader'] = True

        # Delivery Report Callback
        # NOTE: Delivery reports may arrive out of order
        def delivery_report(err, _):
            nonlocal reported_size
            if err is None:
                reported_size += 1
            else:
                raise Exception(f"Message delivery failed: {err}")

        while True:
            # Get details about the last batch
            batch_id = db.get(b'batch_id')

            # If we lost messages since the last execution
            if not initialized:
                initialized = True
                if db.get(b'batch_reported') == b'\x00':
                    if db.get(b'batch_transacting') == b'\x01':
                        # Awaiting in-flight transaction (Softlock)
                        raise Exception("Awaiting in-flight transaction")
                    else:
                        # Process deadlocked
                        raise Exception("Lost continuity")

            # If we need to seek
            if batch_id is not None and not seeked:
                try:
                    while True:
                        item = queue.get_nowait()
                        if item['id'] == batch_id.decode('utf-8'):
                            seeked = True
                            break
                except asyncio.QueueEmpty:
                    raise Exception(f"Unable to locate last message in sequence")
                except Exception as e:
                    raise e
            else:
                seeked = True

            # If not all messages have been reported
            if batch_size == BATCH_SIZE and reported_size < produced_size:
                logger.warning("Kafka message processing is falling behind. Delaying by 200ms")
                await asyncio.sleep(0.20)
                producer.flush()
                continue

            # If all messages have been reported
            if batch_size == BATCH_SIZE and reported_size == produced_size:
                db.set(b"batch_id", item['id'].encode('utf-8'))
                db.set(b"batch_reported", b'\x01')
                reported_size = 0
                produced_size = 0
                batch_size = 0

            # Await next item in the queue
            item = await queue.get()

            # Whether message is metadata
            is_metadata = False

            # Construct the BMP message
            match item['type']:
                case 'UPDATE':
                    message = BMPv3.monitoring_message(
                        peer_ip=item['peer'],
                        peer_asn=int(item['peer_asn']),
                        timestamp=item['timestamp'],
                        bgp_update=bytes.fromhex(item['raw']),
                        collector=RIS_HOST
                    )
                case 'OPEN':
                    message = BMPv3.peer_up_message(
                        peer_ip=item['peer'],
                        peer_asn=int(item['peer_asn']),
                        timestamp=item['timestamp'],
                        bgp_open=bytes.fromhex(item['raw']),
                        collector=RIS_HOST
                    )
                case 'NOTIFICATION':
                    message = BMPv3.peer_down_message(
                        peer_ip=item['peer'],
                        peer_asn=int(item['peer_asn']),
                        timestamp=item['timestamp'],
                        bgp_notification=bytes.fromhex(item['raw']),
                        collector=RIS_HOST
                    )
                case 'KEEPALIVE':
                    message = BMPv3.keepalive_message(
                        peer_ip=item['peer'],
                        peer_asn=int(item['peer_asn']),
                        timestamp=item['timestamp'],
                        bgp_keepalive=bytes.fromhex(item['raw']),
                        collector=RIS_HOST
                    )
                case 'RIS_PEER_STATE':
                    # Internal state of the peer
                    # NOTE: This is not a BGP state!
                    is_metadata = True
                    if item['state'] == 'connected':
                        message = BMPv3.peer_up_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            bgp_open=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                    elif item['state'] == 'down':
                        message = BMPv3.peer_down_message(
                            peer_ip=item['peer'],
                            peer_asn=int(item['peer_asn']),
                            timestamp=item['timestamp'],
                            bgp_notification=bytes.fromhex(item['raw']),
                            collector=RIS_HOST
                        )
                case 'STATE':
                    # Unknown message type
                    # Read more: https://fosstodon.org/@robinroeper/113909690169940714
                    continue
                case _:
                    raise Exception(f"Unexpected type: {item['type']}")
                
            # Set transacting to true
            db.set(b"batch_transacting", b'\x01')

            # Send messages to Kafka
            producer.produce(
                topic=f"{RIS_HOST}.{item['peer_asn'] if not is_metadata else 'meta'}.bmp_raw",
                key=item['id'],
                value=message,
                timestamp=int(item['timestamp'] * 1000),
                callback=delivery_report
            )

            # Set latest delivered item
            latest = item

            # Increment counter for batch
            batch_size += 1

            # Set last delivered to false
            db.set(b"batch_reported", b'\x00')

            # Increment counter for produced
            produced_size += 1

            # Set transacting to false
            db.set(b"batch_transacting", b'\x00')

            # Update the approximated time lag
            memory['time_lag'] = datetime.now() - datetime.fromtimestamp(int(item['timestamp']))

            # Increment sent data size
            memory['send_counter'][0] += len(message)

            # Poll Kafka producer
            producer.poll(0)
    except asyncio.CancelledError:
        # If we are interrupted amid an active message delivery transaction
        if not db.get(b"batch_transacting") == b'\x01':
            # Ensure all messages got delivered
            while db.get(b"batch_reported") == b'\x00':
                # If not all messages have been reported
                if reported_size < produced_size:
                    logger.warning("Messages still in queue or transit. Delaying shutdown by 1000ms")
                    await asyncio.sleep(1)
                    producer.flush()
                    continue

                # If all messages have been reported
                if reported_size == produced_size:
                    logger.info("Outstanding messages delivered")
                    db.set(b"batch_id", latest['id'].encode('utf-8'))
                    db.set(b"batch_reported", b'\x01')
        raise

# Logging Task
async def logging_task(memory):
    """
    A single task that continuously logs the current state of the system.

    Args:
        memory (dict): The memory dictionary.
    """
    try:
        while True:
            timeout = 10 # seconds

            # Calculate kbps
            received_kbps = (memory['receive_counter'][0] * 8) / 1024 / timeout  
            sent_kbps = (memory['send_counter'][0] * 8) / 1024 / timeout

            # Calculate time lag
            h, remainder = divmod(memory['time_lag'].total_seconds(), 3600)
            m, s = divmod(remainder, 60)

            # Log out
            logger.info(f"host={RIS_HOST} leader={memory['is_leader']} receive={received_kbps:.2f} kbps send={sent_kbps:.2f} kbps lag={int(h)}h {int(m)}m {int(s)}s")

            # Reset counters
            memory['receive_counter'][0] = 0 
            memory['send_counter'][0] = 0

            await asyncio.sleep(timeout)
    except asyncio.CancelledError:
        raise

# Signal handler
def handle_shutdown(signum, frame, shutdown_event):
    """
    Signal handler for shutdown.

    Args:
        signum (int): The signal number.
        frame (frame): The signal frame.
        shutdown_event (asyncio.Event): The shutdown event.
    """
    logger.info(f"Signal {signum}. Triggering shutdown...")
    shutdown_event.set()

# Main Coroutine
async def main():
    
    # RocksDB
    db = rocksdbpy.open_default("/var/lib/rocksdb")
    
    # Kafka Producer
    producer = Producer({
        'bootstrap.servers': KAFKA_CONNECT,
        'enable.idempotence': True,
        'acks': 'all',
        'retries': 10,
        'compression.type': 'lz4'
    })

    # Memory
    memory = {
        'receive_counter': [0],  # To store received data size in bytes
        'send_counter': [0],  # To store sent data size in bytes
        'batch_counter': 0, # To store batch counter
        'is_leader': False, # Leader Lock (Leader Election)
        'time_lag': timedelta(0), # Time lag in seconds
    }

    # Queue
    queue = asyncio.Queue(maxsize=QUEUE_SIZE)

    # Tasks
    tasks = []

    try:
        logger.info("Starting up...")

        # Create an event to signal shutdown
        shutdown_event = asyncio.Event()

        # Register SIGTERM handler
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, handle_shutdown, signal.SIGTERM, None, shutdown_event)
        loop.add_signal_handler(signal.SIGINT, handle_shutdown, signal.SIGINT, None, shutdown_event)  # Handle Ctrl+C

        # Start the consumer and sender tasks
        tasks.append(asyncio.create_task(consumer_task(queue, memory)))
        tasks.append(asyncio.create_task(sender_task(producer, election, db, queue, memory)))

        # Start the logging task
        tasks.append(asyncio.create_task(logging_task(memory)))

        # Monitor tasks for exceptions
        for task in tasks:
            task.add_done_callback(lambda t: shutdown_event.set() if t.exception() else None)

        # Wait for the shutdown event
        await shutdown_event.wait()
    finally:
        logger.info("Shutting down...")

        # Cancel all running tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Shutdown Queue
        queue.shutdown()

        # Close RocksDB
        db.close()

        logger.info("Shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
