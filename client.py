
"""Player client for Murderers and Magicians."""

import asyncio
import logging
import threading

import grpc
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

import m_m_pb2
from communications import connect


SHUTDOWN_PREFIX = "SERVER_SHUTDOWN:"


def receive_messages(stub, session_id, loop, shutdown_event):
    """Receive messages without disrupting the player's input prompt."""
    try:
        request = m_m_pb2.StreamRequest(session_id=session_id)

        for message in stub.StreamMessages(request):
            if message.text.startswith(SHUTDOWN_PREFIX):
                print(f"\n{message.text[len(SHUTDOWN_PREFIX):].strip()}")
                loop.call_soon_threadsafe(shutdown_event.set)
                return

            print(f"\n{message.text}")

    except grpc.RpcError as exc:
        logging.info("Message stream ended: %s", exc.details())
        print("\nConnection to the game server was lost.")
    finally:
        loop.call_soon_threadsafe(shutdown_event.set)


async def input_loop(stub, session_id, shutdown_event):
    """Read commands while allowing incoming messages and shutdown."""
    prompt_session = PromptSession()

    with patch_stdout():
        print(
            "\nChat: type ordinary text.\n"
            "Game commands:\n"
            "  /choose scry|shield|sacrifice\n"
            "  /target PLAYER_NAME\n"
            "  /kill PLAYER_NAME\n"
            "  /vote PLAYER_NAME\n"
            "  /abstain\n"
            "  /help\n"
            "  /quit\n"
        )

        while not shutdown_event.is_set():
            prompt_task = asyncio.create_task(
                prompt_session.prompt_async("> ")
            )
            shutdown_task = asyncio.create_task(
                shutdown_event.wait()
            )

            done, pending = await asyncio.wait(
                (prompt_task, shutdown_task),
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

            if shutdown_task in done:
                break

            try:
                text = prompt_task.result().strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not text:
                continue

            if text == "/quit":
                break

            if text == "/help":
                print(
                    "Chat with ordinary text. Use /choose, /target, "
                    "/kill, /vote, or /abstain when appropriate."
                )
                continue

            parts = text.split(maxsplit=1)
            command = parts[0].lower()
            argument = parts[1] if len(parts) > 1 else ""

            action_map = {
                "/choose": "choose",
                "/target": "target",
                "/kill": "kill",
                "/vote": "vote",
                "/abstain": "abstain",
            }

            try:
                if command in action_map:
                    response = stub.SubmitAction(
                        m_m_pb2.ActionRequest(
                            session_id=session_id,
                            action=action_map[command]
                            + (f" {argument}" if argument else ""),
                        ),
                        timeout=5,
                    )
                else:
                    response = stub.SendMessage(
                        m_m_pb2.PlayerMessage(
                            session_id=session_id,
                            text=text,
                        ),
                        timeout=5,
                    )

                if response.message:
                    print(response.message)

            except grpc.RpcError as exc:
                print(f"Connection to server failed: {exc.details()}")
                break


async def async_main():
    logging.basicConfig(level=logging.INFO)

    name = input("Enter your player name: ").strip()
    if not name:
        print("A player name is required.")
        return

    try:
        channel, stub = connect()
    except ConnectionError as exc:
        print(exc)
        return

    session_id = None
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    try:
        response = stub.Join(
            m_m_pb2.JoinRequest(player_name=name),
            timeout=5,
        )

        if not response.accepted:
            print(response.message)
            return

        session_id = response.session_id
        print(response.message)

        listener = threading.Thread(
            target=receive_messages,
            args=(stub, session_id, loop, shutdown_event),
            daemon=True,
        )
        listener.start()

        await input_loop(stub, session_id, shutdown_event)

    except grpc.RpcError as exc:
        print(f"Connection to the server failed: {exc.details()}")
    finally:
        if session_id:
            try:
                stub.Leave(
                    m_m_pb2.SessionRequest(session_id=session_id),
                    timeout=2,
                )
            except grpc.RpcError:
                pass

        channel.close()
        print("Disconnected.")


def main():
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, EOFError):
        print("\nLeaving the game.")


if __name__ == "__main__":
    main()
