"""Connections Protocol Starter Kit"""
import argparse
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from aiohttp import web

from aries_staticagent import crypto

from .connections import Connections, Connection, ConnectionMachine
from . import admin
from .protocols import BasicMessage, CoordinateMediation

LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=LOG_LEVEL
)
logging.root.warning("Log level set to: %s", LOG_LEVEL)
LOGGER = logging.getLogger("proxy_mediator")


def config():
    """Get config"""

    def environ_or_required(key):
        if os.environ.get(key):
            return {"default": os.environ.get(key)}
        return {"required": True}

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", **environ_or_required("PORT"))
    parser.add_argument("--replace-keys", action="store_true", dest="replace")
    args = parser.parse_args()
    return args


def store_connection(conn: Connection):
    if hasattr(conn, "state") and (
        conn.state == ConnectionMachine.complete
        or conn.state == ConnectionMachine.response_received
        or conn.state == ConnectionMachine.response_sent
    ):
        assert conn.target
        with open(".keys", "w+") as key_file:
            json.dump(
                {
                    "did": conn.did,
                    "my_vk": conn.verkey_b58,
                    "my_sk": crypto.bytes_to_b58(conn.sigkey),
                    "recipients": [
                        crypto.bytes_to_b58(recip) for recip in conn.target.recipients
                    ]
                    if conn.target.recipients
                    else [],
                    "endpoint": conn.target.endpoint,
                },
                key_file,
            )


def recall_connection():
    if not os.path.exists(".keys"):
        return None
    with open(".keys", "r") as key_file:
        info = json.load(key_file)
        return Connection.from_parts(
            (info["my_vk"], info["my_sk"]),
            recipients=info["recipients"],
            endpoint=info["endpoint"],
        )


@asynccontextmanager
async def webserver(port: int, connections: Connections):
    """Listen for messages and handle using Connections."""

    async def sleep():
        print(
            "======== Running on {} ========\n(Press CTRL+C to quit)".format(port),
            flush=True,
        )
        while True:
            await asyncio.sleep(3600)

    async def handle(request):
        """aiohttp handle POST."""
        packed_message = await request.read()
        LOGGER.debug("Received packed message: %s", packed_message)
        try:
            response = await connections.handle_message(packed_message)
            if response:
                LOGGER.debug("Returning response over HTTP")
                return web.Response(body=response)
        except Exception:
            LOGGER.exception("Failed to handle message")

        raise web.HTTPAccepted()

    app = web.Application()
    app.add_routes([web.post("/", handle)])

    # Setup "Admin" routes
    admin.routes(connections, app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    print("Starting server...", flush=True)
    await site.start()
    try:
        yield sleep
    finally:
        print("Closing server...", flush=True)
        await runner.cleanup()


async def main():
    """Main."""
    args = config()
    endpoint = os.environ.get("ENDPOINT", f"http://localhost:{args.port}")
    print(f"Starting proxy with endpoint: {endpoint}", flush=True)

    connections = Connections(endpoint)
    connections.route_module(BasicMessage())
    connections.route_module(CoordinateMediation())

    async with webserver(args.port, connections) as loop:
        conn, invite = connections.create_invitation()
        print("Invitation URL:", invite, flush=True)
        conn = await conn.completion()
        print("Connection completed successfully")
        await loop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
