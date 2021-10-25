""" Connections protocol from Indy HIPE 0031
    https://github.com/hyperledger/indy-hipe/tree/master/text/0031-connection-protocol
"""
import asyncio
from asyncio.futures import Future
from base64 import urlsafe_b64decode
import json
import logging
from typing import Callable, Dict, Iterable, Optional

from aries_staticagent import Connection as AsaPyConn, Message, crypto
from aries_staticagent.connection import Target
from aries_staticagent.dispatcher import Dispatcher, Handler
from aries_staticagent.message import MsgType
from aries_staticagent.module import Module, ModuleRouter
from statemachine import State, StateMachine


LOGGER = logging.getLogger(__name__)


class ConnectionNotFound(Exception):
    """Raised when connection for message not found."""


class Connection(AsaPyConn):
    """Wrapper around Static Agent library connection to provide state."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.state: str = "null"
        self._completed: Future = asyncio.get_event_loop().create_future()
        self.multiuse: bool = False

    @property
    def is_completed(self):
        return self._completed.done()

    def complete(self):
        """Complete this connection"""
        self._completed.set_result(self)

    async def completion(self) -> "Connection":
        """Await connection completion.

        For invitation connections, the connection is replaced after receiving
        a connection request. This will return the completed connection.
        """
        return await self._completed

    def from_invite(self, invite: "Connection"):
        """Transfer state from invite connection to relationship connection."""
        self.state = invite.state
        self._completed = invite._completed


class ConnectionMachine(StateMachine):
    null = State("null", initial=True)
    invite_sent = State("invite_sent")
    invite_received = State("invited")
    request_sent = State("request_sent")
    request_received = State("requested")
    response_sent = State("response_sent")
    response_received = State("responded")
    complete = State("complete")

    send_invite = null.to(invite_sent)
    receive_request = invite_sent.to(request_received)
    send_response = request_received.to(response_sent)

    receive_invite = null.to(invite_received)
    send_request = invite_received.to(request_sent)
    receive_response = request_sent.to(response_received)
    send_ping = response_received.to(complete) | complete.to.itself()
    receive_ping = response_sent.to(complete) | complete.to.itself()
    send_ping_response = complete.to.itself()
    receive_ping_response = complete.to.itself()


class Connections(Module):
    """Module for Connection Protocol"""

    doc_uri = "did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/"
    protocol = "connections"
    version = "1.0"
    route = ModuleRouter()

    def __init__(self, endpoint=None, connections=None):
        super().__init__()
        self.endpoint = endpoint
        self.connections: Dict[str, Connection] = connections if connections else {}

        # We want each connection created by this module to share the same routes
        self.dispatcher = Dispatcher()
        self.dispatcher.add_handlers(
            [Handler(msg_type, func) for msg_type, func in self.routes.items()]
        )

        self.mediator_connection: Optional[Connection] = None
        self._mediator_connection_event = asyncio.Event()
        self.agent_connection: Optional[Connection] = None
        self.agent_invitation: Optional[str] = None

    def _recipients_from_packed_message(self, packed_message: bytes) -> Iterable[str]:
        """
        Inspect the header of the packed message and extract the recipient key.
        """
        try:
            wrapper = json.loads(packed_message)
        except Exception as err:
            raise ValueError("Invalid packed message") from err

        recips_json = crypto.b64_to_bytes(wrapper["protected"], urlsafe=True).decode(
            "ascii"
        )
        try:
            recips_outer = json.loads(recips_json)
        except Exception as err:
            raise ValueError("Invalid packed message recipients") from err

        return [recip["header"]["kid"] for recip in recips_outer["recipients"]]

    def connections_for_message(self, packed_message: bytes) -> Iterable[Connection]:
        recipients = self._recipients_from_packed_message(packed_message)
        connections = [
            self.connections[recip] for recip in recipients if recip in self.connections
        ]
        if not connections:
            raise ConnectionNotFound(
                f"No connections for message with recipients: {recipients}"
            )
        return connections

    def route_method(self, msg_type: str) -> Callable:
        """Register route decorator."""

        def register_route_dec(func):
            self.dispatcher.add_handler(Handler(MsgType(msg_type), func))
            return func

        return register_route_dec

    def route_module(self, module: Module):
        """Register a module for routing."""
        handlers = [Handler(msg_type, func) for msg_type, func in module.routes.items()]
        return self.dispatcher.add_handlers(handlers)

    async def handle_message(self, packed_message: bytes) -> Optional[bytes]:
        response = []
        for conn in self.connections_for_message(packed_message):
            LOGGER.debug(
                "Handling message with connection using verkey: %s", conn.verkey_b58
            )
            with conn.session(response.append) as session:
                await session.handle(packed_message)

        if response:
            return response.pop()

        return None

    async def mediator_invite_received(self) -> Connection:
        """Await event notifying that mediator invite has been received."""
        await self._mediator_connection_event.wait()
        if not self.mediator_connection:
            raise RuntimeError("Mediator connection event triggered without set")
        return self.mediator_connection

    async def receive_mediator_invite(self, invite: str) -> Connection:
        """Receive mediator invitation."""
        self.mediator_connection = await self.receive_invite_url(invite, endpoint="")
        self._mediator_connection_event.set()
        return self.mediator_connection

    def create_invitation(self, *, multiuse: bool = False):
        """Create and return an invite."""
        connection = Connection.random(dispatcher=self.dispatcher)
        connection.multiuse = multiuse
        self.connections[connection.verkey_b58] = connection
        ConnectionMachine(connection).send_invite()
        invitation = Message.parse_obj(
            {
                "@type": self.type("invitation"),
                "label": "proxy-mediator",
                "recipientKeys": [connection.verkey_b58],
                "serviceEndpoint": self.endpoint,
                "routingKeys": [],
            }
        )
        invitation_url = "{}?c_i={}".format(
            self.endpoint, crypto.bytes_to_b64(invitation.serialize().encode())
        )
        LOGGER.debug("Created invitation: %s", invitation_url)
        return connection, invitation_url

    async def receive_invite_url(self, invite: str, **kwargs):
        """Process an invitation from a URL."""
        invite_msg = Message.parse_obj(
            json.loads(urlsafe_b64decode(invite.split("c_i=")[1]))
        )
        return await self.receive_invite(invite_msg, **kwargs)

    async def receive_invite(self, invite: Message, *, endpoint: str = None):
        """Process an invitation."""
        LOGGER.debug("Received invitation: %s", invite.pretty_print())
        invitation_key = invite["recipientKeys"][0]
        new_connection = Connection.random(
            target=Target(
                their_vk=invite["recipientKeys"][0],
                endpoint=invite["serviceEndpoint"],
            ),
            dispatcher=self.dispatcher,
        )
        ConnectionMachine(new_connection).receive_invite()

        self.connections[new_connection.verkey_b58] = new_connection
        self.connections[invitation_key] = new_connection
        request = Message.parse_obj(
            {
                "@type": self.type("request"),
                "label": "proxy-mediator",
                "connection": {
                    "DID": new_connection.did,
                    "DIDDoc": {
                        "@context": "https://w3id.org/did/v1",
                        "id": new_connection.did,
                        "publicKey": [
                            {
                                "id": new_connection.did + "#keys-1",
                                "type": "Ed25519VerificationKey2018",
                                "controller": new_connection.did,
                                "publicKeyBase58": new_connection.verkey_b58,
                            }
                        ],
                        "service": [
                            {
                                "id": new_connection.did + "#indy",
                                "type": "IndyAgent",
                                "recipientKeys": [new_connection.verkey_b58],
                                "routingKeys": [],
                                "serviceEndpoint": endpoint
                                if endpoint is not None
                                else self.endpoint,
                            }
                        ],
                    },
                },
            }
        )
        LOGGER.debug("Sending request: %s", request.pretty_print())
        ConnectionMachine(new_connection).send_request()
        await new_connection.send_async(request, return_route="all")

        return new_connection

    @route
    async def request(self, msg: Message, conn):
        """Process a request."""
        LOGGER.debug("Received request: %s", msg.pretty_print())
        assert msg.mtc.recipient

        # Pop invite connection
        invite_connection = self.connections.pop(msg.mtc.recipient)

        # Push back on if multiuse
        if invite_connection.multiuse:
            self.connections[invite_connection.verkey_b58] = invite_connection

        ConnectionMachine(invite_connection).receive_request()

        # Create relationship connection
        connection = Connection.random(
            Target(
                endpoint=msg["connection"]["DIDDoc"]["service"][0]["serviceEndpoint"],
                recipients=msg["connection"]["DIDDoc"]["service"][0]["recipientKeys"],
            ),
            dispatcher=self.dispatcher,
        )

        # Update connections
        self.connections[connection.verkey_b58] = connection
        connection.from_invite(invite_connection)

        # Prepare response
        connection_block = {
            "DID": connection.did,
            "DIDDoc": {
                "@context": "https://w3id.org/did/v1",
                "id": connection.did,
                "publicKey": [
                    {
                        "id": connection.did + "#keys-1",
                        "type": "Ed25519VerificationKey2018",
                        "controller": connection.did,
                        "publicKeyBase58": connection.verkey_b58,
                    }
                ],
                "service": [
                    {
                        "id": connection.did + ";indy",
                        "type": "IndyAgent",
                        "recipientKeys": [connection.verkey_b58],
                        "routingKeys": [],
                        "serviceEndpoint": self.endpoint,
                    }
                ],
            },
        }

        ConnectionMachine(connection).send_response()

        response = Message.parse_obj(
            {
                "@type": self.type("response"),
                "~thread": {"thid": msg.id, "sender_order": 0},
                "connection~sig": crypto.sign_message_field(
                    connection_block,
                    invite_connection.verkey_b58,
                    invite_connection.sigkey,
                ),
            }
        )
        LOGGER.debug("Sending response: %s", response.pretty_print())
        LOGGER.debug(
            "Unsigned connection object: %s", json.dumps(connection_block, indent=2)
        )
        await connection.send_async(response)
        connection.complete()

    @route
    async def response(self, msg: Message, conn):
        """Process a response."""
        LOGGER.debug("Received response: %s", msg.pretty_print())
        their_conn_key = msg["connection~sig"]["signer"]
        connection = self.connections.pop(their_conn_key)
        ConnectionMachine(connection).receive_response()

        signer, connection_block = crypto.verify_signed_message_field(
            msg["connection~sig"]
        )
        LOGGER.debug(
            "Unpacked connection object: %s", json.dumps(connection_block, indent=2)
        )

        # Update connection
        assert connection.target
        connection.target.update(
            recipients=connection_block["DIDDoc"]["service"][0]["recipientKeys"],
            endpoint=connection_block["DIDDoc"]["service"][0]["serviceEndpoint"],
        )
        connection.complete()

        ping = Message.parse_obj(
            {
                "@type": self.type(protocol="trust_ping", name="ping"),
                "~thread": {"thid": msg.id},
            }
        )
        LOGGER.debug("Sending ping: %s", ping.pretty_print())
        ConnectionMachine(connection).send_ping()
        await connection.send_async(ping, return_route="all")

    @route("did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/trust_ping/1.0/ping")
    async def ping(self, msg: Message, conn):
        """Process a trustping."""
        LOGGER.debug("Received trustping: %s", msg.pretty_print())
        assert msg.mtc.recipient
        connection = self.connections[msg.mtc.recipient]
        ConnectionMachine(connection).receive_ping()
        response = Message.parse_obj(
            {
                "@type": self.type(protocol="trust_ping", name="ping_response"),
                "~thread": {"thid": msg.id},
            }
        )
        LOGGER.debug("Sending ping response: %s", response.pretty_print())
        ConnectionMachine(connection).send_ping_response()
        await conn.send_async(response)

    @route("did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/trust_ping/1.0/ping_response")
    async def ping_response(self, msg: Message, conn):
        """Process a trustping."""
        LOGGER.debug("Received trustping response: %s", msg.pretty_print())
        assert msg.mtc.recipient
        connection = self.connections[msg.mtc.recipient]
        ConnectionMachine(connection).receive_ping_response()