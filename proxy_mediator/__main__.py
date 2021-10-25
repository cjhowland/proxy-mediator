"""Connections Protocol Starter Kit"""
import asyncio
from contextlib import asynccontextmanager
import logging

from aiohttp import web
from configargparse import ArgumentParser, YAMLConfigFileParser

from . import admin, CONNECTIONS
from .agent import Connections
from .protocols import BasicMessage, CoordinateMediation, Routing


LOGGER = logging.getLogger("proxy_mediator")


def config():
    """Get config"""
    parser = ArgumentParser(
        config_file_parser_class=YAMLConfigFileParser, prog="proxy_mediator"
    )
    parser.add_argument("--port", env_var="PORT", type=str, required=True)
    parser.add_argument(
        "--mediator-invite", env_var="MEDIATOR_INVITE", type=str, required=True
    )
    parser.add_argument("--endpoint", env_var="ENDPOINT", type=str, required=True)
    parser.add_argument("--log-level", env_var="LOG_LEVEL", type=str, default="WARNING")
    args = parser.parse_args()

    # Configure logs
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=args.log_level
    )
    logging.root.warning("Log level set to: %s", args.log_level)

    return args


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
    admin.register_routes(connections, app)

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
    print(f"Starting proxy with endpoint: {args.endpoint}", flush=True)

    connections = Connections(args.endpoint)
    CONNECTIONS.set(connections)
    connections.route_module(BasicMessage())
    coordinate_mediation = CoordinateMediation()
    connections.route_module(coordinate_mediation)
    connections.route_module(Routing())

    async with webserver(args.port, connections) as loop:
        # Connect to mediator by processing passed in invite
        # All these operations must take place without an endpoint
        mediator_connection = await connections.receive_invite_url(
            args.mediator_invite, endpoint=""
        )
        connections.mediator_connection = mediator_connection
        await mediator_connection.completion()

        # Request mediation and send keylist update
        await coordinate_mediation.request_mediation_from_external(mediator_connection)
        await coordinate_mediation.send_keylist_update(
            mediator_connection,
            action="add",
            recipient_key=mediator_connection.verkey_b58,
        )

        # Connect to agent by creating invite and awaiting connection completion
        agent_connection, invite = connections.create_invitation()
        connections.agent_invitation = invite
        print("Invitation URL:", invite, flush=True)
        agent_connection = await agent_connection.completion()
        connections.agent_connection = agent_connection
        print("Connection completed successfully")

        # TODO Start self repairing WS connection to mediator to retrieve
        # messages as a separate task
        await loop()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
