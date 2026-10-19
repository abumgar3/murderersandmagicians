
"""gRPC networking layer for Murderers and Magicians."""

import logging
import queue
import time
from concurrent import futures

import grpc

import m_m_pb2
import m_m_pb2_grpc

HOST = "127.0.0.1"
PORT = 50051
SERVER_ADDRESS = f"{HOST}:{PORT}"


class GameService(m_m_pb2_grpc.GameServiceServicer):
    def __init__(self, game):
        self.game = game

    def Join(self, request, context):
        return self.game.register_player(request.player_name)

    def Leave(self, request, context):
        ok, message = self.game.leave_player(request.session_id)
        return m_m_pb2.MessageResponse(
            accepted=ok, message=message
        )

    def SendMessage(self, request, context):
        ok, message = self.game.chat(
            request.session_id, request.text
        )
        return m_m_pb2.MessageResponse(
            accepted=ok, message=message
        )

    def SubmitAction(self, request, context):
        ok, message = self.game.submit_action(
            request.session_id,
            request.action,
            request.target_name,
        )
        return m_m_pb2.MessageResponse(
            accepted=ok, message=message
        )

    def StreamMessages(self, request, context):
        messages = self.game.subscribe(request.session_id)
        if messages is None:
            context.abort(
                grpc.StatusCode.NOT_FOUND, "Unknown session."
            )

        try:
            while context.is_active():
                try:
                    message = messages.get(timeout=0.5)
                except queue.Empty:
                    continue
                yield message
        finally:
            self.game.unsubscribe(request.session_id, messages)


def create_server(game):
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=32)
    )
    m_m_pb2_grpc.add_GameServiceServicer_to_server(
        GameService(game), server
    )

    if server.add_insecure_port(SERVER_ADDRESS) == 0:
        raise RuntimeError(
            f"Could not bind to {SERVER_ADDRESS}"
        )

    return server


def connect(timeout=2.0):
    channel = grpc.insecure_channel(SERVER_ADDRESS)

    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
    except grpc.FutureTimeoutError as exc:
        channel.close()
        raise ConnectionError(
            "No game server is running. Ask the moderator to start it."
        ) from exc

    return channel, m_m_pb2_grpc.GameServiceStub(channel)
