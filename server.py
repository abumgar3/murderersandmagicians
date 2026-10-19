
"""Game controller and moderator interface for Murderers and Magicians."""

import logging
import queue
import random
import threading
import time
import uuid

import m_m_pb2
from communications import PORT, create_server


# Game configuration
MIN_PLAYERS = 4
MAX_PLAYERS = 10
ACTION_TIMEOUT = 30
DISCUSSION_TIMEOUT = 60


class Game:
    """Manage players, roles, game phases, and moderator controls."""

    def __init__(self):
        self.lock = threading.RLock()

        # Players are indexed by their unique session IDs.
        self.players = {}
        self.queues = {}

        # Game state
        self.phase = "lobby"
        self.day = 0
        self.murderer = None
        self.magician = None
        self.magician_choice = None
        self.shielded = None
        self.sacrifice = None
        self.deaths_today = []
        self.votes = {}
        self.timer = None
        self.winner = None

    # --------------------------------------------------
    # Messaging
    # --------------------------------------------------

    def _send(self, session, text, sender="Game"):
        """Queue a message for one player."""
        message_queue = self.queues.get(session)

        if message_queue is None:
            return

        message = m_m_pb2.PlayerMessage(
            player_name=sender,
            text=text,
            timestamp=int(time.time()),
        )

        try:
            message_queue.put_nowait(message)
        except queue.Full:
            logging.warning(
                "Message queue is full for session %s", session
            )

    def _private(self, session, text):
        """Send a message to one specific player."""
        self._send(session, text)

    def _broadcast(self, text, sender="Game"):
        """Send a message to all registered players."""
        for session in list(self.players):
            self._send(session, text, sender)

    def _spectators(self, text, sender="Game"):
        """Send a message to all dead players."""
        for session, player in list(self.players.items()):
            if not player["alive"]:
                self._send(session, text, sender)

    # --------------------------------------------------
    # Player registration
    # --------------------------------------------------

    def register_player(self, name):
        """Register a player and return their session information."""
        name = name.strip()

        with self.lock:
            if not name:
                return m_m_pb2.JoinResponse(
                    accepted=False,
                    message="Player name cannot be empty.",
                )

            if self.phase not in ("lobby", "ended"):
                return m_m_pb2.JoinResponse(
                    accepted=False,
                    message="A game is already in progress.",
                )

            if len(self.players) >= MAX_PLAYERS:
                return m_m_pb2.JoinResponse(
                    accepted=False,
                    message="The game is full (10 players maximum).",
                )

            for player in self.players.values():
                if player["name"].casefold() == name.casefold():
                    return m_m_pb2.JoinResponse(
                        accepted=False,
                        message="That player name is already taken.",
                    )

            session_id = str(uuid.uuid4())

            self.players[session_id] = {
                "name": name,
                "role": "Townsperson",
                "alive": True,
                "connected": True,
            }

            self.queues[session_id] = queue.Queue(maxsize=500)

            logging.info("Player joined: %s", name)
            self._broadcast(f"{name} joined the game.")

            return m_m_pb2.JoinResponse(
                accepted=True,
                message=f"Welcome, {name}. Waiting for the moderator.",
                session_id=session_id,
            )

    def _find_player(self, name):
        """Find a connected player by name, ignoring letter case."""
        if not name:
            return None

        for session, player in self.players.items():
            if (
                player["connected"]
                and player["name"].casefold() == name.casefold()
            ):
                return session, player

        return None

    def _living(self):
        """Return the session IDs of living, connected players."""
        return [
            session
            for session, player in self.players.items()
            if player["alive"] and player["connected"]
        ]

    def _living_targets(self, excluded_session):
        """Return the names of living players other than one player."""
        return [
            self.players[session]["name"]
            for session in self._living()
            if session != excluded_session
        ]

    # --------------------------------------------------
    # Player disconnections and chat
    # --------------------------------------------------

    def leave_player(self, session):
        """Mark a disconnected player and handle active game effects."""
        with self.lock:
            player = self.players.get(session)

            if not player or not player["connected"]:
                return False, "Unknown or disconnected player."

            player["connected"] = False
            name = player["name"]

            if self.phase not in ("lobby", "ended") and player["alive"]:
                player["alive"] = False
                self._broadcast(
                    f"{name} disconnected and is now a spectator."
                )

                if (
                    session == self.magician
                    and self.phase in ("magician_action", "magician_target")
                ):
                    self._cancel_timer()
                    self._begin_murderer()

                elif (
                    session == self.murderer
                    and self.phase == "murderer_action"
                ):
                    self._cancel_timer()
                    self._begin_discussion()

                self._check_murderer_win()
            else:
                self._broadcast(f"{name} disconnected.")

            return True, "Disconnected."

    def subscribe(self, session):
        """Return a player's message queue."""
        with self.lock:
            return self.queues.get(session)

    def unsubscribe(self, session, message_queue):
        """Keep the queue if a player temporarily reconnects."""
        pass

    def chat(self, session, text):
        """Handle ordinary player chat."""
        with self.lock:
            player = self.players.get(session)

            if not player or not player["connected"]:
                return False, "Unknown or disconnected player."

            text = text.strip()

            if not text:
                return False, "Message cannot be empty."

            if (
                not player["alive"]
                and self.phase not in ("lobby", "ended")
            ):
                return False, "Spectators cannot send messages."

            if self.phase not in (
                "lobby", "discussion", "vote", "ended"
            ):
                return False, "Chat is unavailable during this phase."

            self._broadcast(
                f"{player['name']}: {text}",
                sender=player["name"],
            )
            return True, "Message sent."

    # --------------------------------------------------
    # Moderator role assignment
    # --------------------------------------------------

    def assign(self, role, name):
        """Assign the Murderer or Magician to a named player."""
        with self.lock:
            
            if self.phase not in ("lobby", "ended"):
                return False, "Roles cannot be changed during a game."

            role = role.capitalize()

            if role not in ("Murderer", "Magician"):
                return False, "Role must be Murderer or Magician."

            found = self._find_player(name)

            if not found:
                return False, "No connected player has that name."

            session, player = found

            other_role_session = (
                self.magician if role == "Murderer"
                else self.murderer
            )

            if other_role_session == session:
                return False, "A player cannot hold both special roles."

            if role == "Murderer":
                previous = self.murderer
                self.murderer = session
            else:
                previous = self.magician
                self.magician = session

            if previous is not None and previous != session:
                self.players[previous]["role"] = "Townsperson"

            player["role"] = role

            self._private(
                session,
                f"The moderator assigned you the role: {role}.",
            )

            return True, f"{player['name']} assigned as {role}."

    def assign_random(self):
        """Randomly assign one Murderer and one Magician."""
        with self.lock:

            if self.phase not in ("lobby", "ended"):
                return False, "Roles cannot be changed during a game."
            
            eligible = [
                session
                for session, player in self.players.items()
                if player["connected"]
            ]

            if len(eligible) < MIN_PLAYERS:
                return False, (
                    "At least four connected players are required."
                )

            self.murderer, self.magician = random.sample(eligible, 2)

            for session in eligible:
                player = self.players[session]
                player["role"] = "Townsperson"

                if session == self.murderer:
                    player["role"] = "Murderer"
                elif session == self.magician:
                    player["role"] = "Magician"

                self._private(
                    session,
                    f"Your role is: {player['role']}.",
                )

            return True, "Murderer and Magician randomly assigned."

    # --------------------------------------------------
    # Start, status, and restart
    # --------------------------------------------------

    def start(self):
        """Validate the lobby and start a new game."""
        with self.lock:
            connected = [
                session
                for session, player in self.players.items()
                if player["connected"]
            ]

            if len(connected) < MIN_PLAYERS:
                return False, (
                    "At least four connected players are required."
                )

            if self.phase not in ("lobby", "ended"):
                return False, "A game is already in progress."

            if self.murderer is None or self.magician is None:
                return False, "Assign both special roles first."

            if self.murderer == self.magician:
                return False, (
                    "The special roles must belong to different players."
                )

            if (
                self.murderer not in connected
                or self.magician not in connected
            ):
                return False, (
                    "Both special-role players must be connected."
                )

            self._cancel_timer()

            self.winner = None
            self.day = 1
            self.deaths_today = []
            self.shielded = None
            self.sacrifice = None
            self.magician_choice = None
            self.votes = {}

            for session, player in self.players.items():
                player["alive"] = player["connected"]
                player["role"] = "Townsperson"

            self.players[self.murderer]["role"] = "Murderer"
            self.players[self.magician]["role"] = "Magician"

            self._broadcast("Murderers and Magicians begins!")

            self._private(
                self.murderer,
                "Your secret role is MURDERER.",
            )
            self._private(
                self.magician,
                "Your secret role is MAGICIAN.",
            )

            self._broadcast(
                f"Day {self.day} begins. The Magician acts first."
            )

            self._begin_magician()
            return True, "Game started."

    def status(self):
        """Return a summary of the current game state."""
        with self.lock:
            living = [
                player["name"]
                for player in self.players.values()
                if player["alive"] and player["connected"]
            ]

            return (
                f"Phase: {self.phase}; "
                f"Day: {self.day}; "
                f"Living ({len(living)}): "
                f"{', '.join(living) or 'none'}; "
                f"Winner: {self.winner or 'none'}"
            )

    def restart(self):
        """Return to the lobby after a completed game."""
        with self.lock:
            if self.phase != "ended":
                return False, "The game must end before restarting."

            self._cancel_timer()

            self.phase = "lobby"
            self.day = 0
            self.murderer = None
            self.magician = None
            self.magician_choice = None
            self.shielded = None
            self.sacrifice = None
            self.deaths_today = []
            self.votes = {}
            self.winner = None

            for player in self.players.values():
                player["role"] = "Townsperson"
                player["alive"] = player["connected"]

            self._broadcast(
                "A new game lobby has opened. The moderator must assign roles."
            )

            return True, "New game lobby opened."

    # --------------------------------------------------
    # Timer management
    # --------------------------------------------------

    def _cancel_timer(self):
        """Cancel the current phase timer, if one exists."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None

    def _schedule(self, seconds, callback):
        """Schedule a callback to run after a specified delay."""
        self._cancel_timer()
        self.timer = threading.Timer(seconds, callback)
        self.timer.daemon = True
        self.timer.start()

    # --------------------------------------------------
    # Magician phase
    # --------------------------------------------------

    def _begin_magician(self):
        """Start the Magician's action phase."""
        if self.phase == "ended":
            return

        if self.magician not in self._living():
            self._begin_murderer()
            return

        targets = self._living_targets(self.magician)

        if not targets:
            self._finish("Magician")
            return

        self.phase = "magician_action"
        self.magician_choice = None
        self.shielded = None

        self._private(
            self.magician,
            "Choose an action using one of these commands:\n"
            "/choose scry\n"
            "/choose shield\n"
            "/choose sacrifice\n"
            f"You have {ACTION_TIMEOUT} seconds.",
        )

        self._private(
            self.magician,
            "Living targets: " + ", ".join(targets),
        )

        self._broadcast("The Magician is taking action.")

        self._schedule(
            ACTION_TIMEOUT,
            self._magician_choice_timeout,
        )

    def _magician_choice_timeout(self):
        """Forfeit the Magician's action if no choice is made."""
        with self.lock:
            if self.phase != "magician_action":
                return

            self._private(
                self.magician,
                "You did not choose an action in time. Your action is forfeited.",
            )

            self._begin_murderer()

    def _magician_target_timeout(self):
        """Forfeit the Magician's action if no target is selected."""
        with self.lock:
            if self.phase != "magician_target":
                return

            self._private(
                self.magician,
                "You did not select a target in time. Your action is forfeited.",
            )

            self._begin_murderer()

    # --------------------------------------------------
    # Murderer phase
    # --------------------------------------------------

    def _begin_murderer(self):
        """Start the Murderer's action phase."""
        if self.phase == "ended":
            return

        if self.murderer not in self._living():
            self._finish("Townspeople")
            return

        targets = self._living_targets(self.murderer)

        if not targets:
            self._finish("Murderer")
            return

        self.phase = "murderer_action"

        self._private(
            self.murderer,
            "Choose a player to kill using /kill PLAYER_NAME.\n"
            f"You have {ACTION_TIMEOUT} seconds.\n"
            "Living targets: " + ", ".join(targets),
        )

        self._broadcast("The Murderer is taking action.")

        self._schedule(
            ACTION_TIMEOUT,
            self._murderer_timeout,
        )

    def _murderer_timeout(self):
        """Choose a random target if the Murderer times out."""
        with self.lock:
            if self.phase != "murderer_action":
                return

            targets = [
                session
                for session in self._living()
                if session != self.murderer
            ]

            if not targets:
                self._finish("Murderer")
                return

            target = random.choice(targets)

            logging.info(
                "Murderer timed out; selected target: %s",
                self.players[target]["name"],
            )

            self._kill(target, "Murderer")

            if self.phase != "ended":
                self._resolve_wake_up()

    # --------------------------------------------------
    # Discussion and voting
    # --------------------------------------------------

    def _begin_discussion(self):
        """Announce deaths and begin the discussion period."""
        if self.phase == "ended":
            return

        self.phase = "discussion"

        dead_names = [
            self.players[session]["name"]
            for session in self.deaths_today
        ]

        if dead_names:
            self._broadcast(
                "Players killed today: " + ", ".join(dead_names)
            )
        else:
            self._broadcast("No one died today.")

        self._broadcast(
            f"Discussion begins. You have {DISCUSSION_TIMEOUT} seconds. "
            "The moderator may type 'advance' to end discussion early."
        )

        self._schedule(
            DISCUSSION_TIMEOUT,
            self._begin_vote,
        )

    def _begin_vote(self):
        """Start voting among all living players."""
        with self.lock:
            if self.phase not in ("discussion", "vote"):
                return

            self.phase = "vote"
            self.votes = {}

            self._broadcast(
                "Voting has begun. Use /vote PLAYER_NAME or /abstain. "
                f"You have {ACTION_TIMEOUT} seconds."
            )

            for session in self._living():
                targets = self._living_targets(session)

                self._private(
                    session,
                    "You may vote for: " + ", ".join(targets),
                )

            self._schedule(
                ACTION_TIMEOUT,
                self._resolve_vote,
            )

    def _resolve_vote(self):
        """Count votes and execute the player with the most votes."""
        with self.lock:
            if self.phase != "vote":
                return

            self._cancel_timer()

            counts = {}

            for target in self.votes.values():
                if target is not None:
                    counts[target] = counts.get(target, 0) + 1

            if not counts:
                self._broadcast(
                    "No votes were cast. Nobody is executed."
                )
                self._after_execution()
                return

            highest = max(counts.values())

            tied = [
                session
                for session, count in counts.items()
                if count == highest
            ]

            if len(tied) > 1:
                names = ", ".join(
                    self.players[session]["name"]
                    for session in tied
                )

                self._broadcast(
                    f"The vote is tied between {names}. Nobody is executed."
                )

                self._after_execution()
                return

            target = tied[0]
            target_name = self.players[target]["name"]
            target_role = self.players[target]["role"]

            self._kill(target, "Townspeople")

            self._broadcast(
                f"{target_name} was executed. "
                f"Their role was {target_role}."
            )

            if target == self.murderer:
                self._finish("Townspeople")
                return

            if self.murderer not in self._living():
                self._finish("Townspeople")
                return

            if self._living() == [self.murderer]:
                self._finish("Murderer")
                return

            self._after_execution()

    def _after_execution(self):
        """Prepare the next day if the game has not ended."""
        if self.phase == "ended":
            return

        self._cancel_timer()
        self.shielded = None
        self.deaths_today = []
        self.day += 1

        self._broadcast(f"Day {self.day} begins.")

        self._begin_magician()

    # --------------------------------------------------
    # Death and victory conditions
    # --------------------------------------------------

    def _kill(self, target, cause):
        """Kill a player, unless a shield blocks the Murderer."""
        if target not in self.players:
            return False

        player = self.players[target]

        if not player["alive"]:
            return False

        if cause == "Murderer" and target == self.shielded:
            self._private(
                target,
                "The Murderer targeted you, but the Magician's shield "
                "protected you.",
            )

            self._spectators(
                f"The Murderer targeted {player['name']}, "
                "but the Magician's shield protected them."
            )

            self._broadcast(
                "The night passed without a death."
            )

            return False

        player["alive"] = False

        if target not in self.deaths_today:
            self.deaths_today.append(target)

        reasons = {
            "Murderer": "killed by the Murderer",
            "Magician": "sacrificed by the Magician",
            "Townspeople": "executed by the Townspeople",
        }

        reason = reasons[cause]

        self._private(
            target,
            f"You were {reason}. You are now a spectator.",
        )

        self._spectators(
            f"{player['name']} was {reason}."
        )

        return True

    def _resolve_wake_up(self):
        """Resolve a deferred Sacrifice and determine the dawn outcome."""
        if self.phase == "ended":
            return

        sacrifice = self.sacrifice
        self.sacrifice = None

        # Resolve Sacrifice only after the Murderer's turn.
        # If the target was already killed, _kill() safely does nothing.
        if sacrifice is not None:
            self._kill(sacrifice, "Magician")

        living = self._living()

        if self.murderer not in living:
            if self.magician in living:
                self._finish("Magician")
            elif len(living) > 0:
                self._finish("Townspeople")
            else:
                self._broadcast(
                    "Everyone dies."
                )
                self._finish("N/A")
        else:
            self._check_murderer_win()
            if self.phase != "ended":
                self._begin_discussion()

    def _check_murderer_win(self):
        """Check whether the Murderer or Townspeople have won."""
        living = self._living()

        if self.murderer not in living:
            self._finish("Magician" if self.winner == "Magician" else "Townspeople")
        elif living == [self.murderer]:
            self._finish("Murderer")

    def _finish(self, winner):
        """End the game and announce the winner."""
        if self.phase == "ended":
            return

        self._cancel_timer()
        self.phase = "ended"
        self.winner = winner

        self._broadcast(f"GAME OVER! Winner: {winner}.")

        logging.info("Game ended. Winner: %s", winner)

    # --------------------------------------------------
    # Player game actions
    # --------------------------------------------------

    def submit_action(self, session, action, target_name=""):
        """Validate and process a player action."""
        with self.lock:
            player = self.players.get(session)

            if not player or not player["connected"]:
                return False, "Unknown or disconnected player."

            if not player["alive"]:
                return False, "Spectators cannot submit actions."

            parts = action.strip().lower().split(maxsplit=1)
            command = parts[0] if parts else ""
            argument = parts[1].strip() if len(parts) > 1 else ""

            target_name = target_name.strip() or argument

            # Magician selects an action.
            if command == "choose":
                if (
                    session != self.magician
                    or self.phase != "magician_action"
                ):
                    return False, "You cannot choose an action now."

                if argument not in ("scry", "shield", "sacrifice"):
                    return False, (
                        "Choose scry, shield, or sacrifice."
                    )

                if argument == "scry":
                    living = self._living()
                    
                    if len(living) == 2 and self.magician in living and self.murderer in living:
                        self._private(session, "Your fate is sealed.")
                    else:
                        townspeople = [
                            player_session
                            for player_session in living
                            if player_session != self.magician
                            and player_session != self.murderer
                        ]
                    
                        if townspeople:
                            chosen = random.choice(townspeople)
                            self._private(
                                session,
                                f"You scry and learn that {self.players[chosen]['name']} is not the Murderer."
                            )
                        else:
                            self._private(session, "But your Inner Eye remained closed.")

                    self._cancel_timer()
                    self._begin_murderer()
                    return True, "Magician action selected."
                
                self.magician_choice = argument
                self.phase = "magician_target"

                self._private(
                    session,
                    f"You chose {argument}. Now use /target PLAYER_NAME. "
                    f"You have {ACTION_TIMEOUT} seconds.",
                )

                self._schedule(
                    ACTION_TIMEOUT,
                    self._magician_target_timeout,
                )

                return True, "Magician action selected."

            # Magician selects a target.
            if command == "target":
                if (
                    session != self.magician
                    or self.phase != "magician_target"
                ):
                    return False, "You cannot select a target now."

                found = self._find_player(target_name)

                if not found:
                    return False, "Target not found."

                target, target_player = found

                if (target == session and not self.magician_choice == "shield") or not target_player["alive"]:
                    return False, "Choose another living player."

                self._cancel_timer()

                if self.magician_choice == "shield":
                    self.shielded = target

                    self._private(
                        session,
                        f"{target_player['name']} is shielded for today.",
                    )

                elif self.magician_choice == "sacrifice":
                    self.sacrifice = target

                    self._private(
                        session,
                        f"You have chosen {target_player['name']} "
                        "for Sacrifice. They will die at dawn.",
                    )

                self._begin_murderer()

                return True, "Magician action resolved."

            # Murderer selects a target.
            if command == "kill":
                if (
                    session != self.murderer
                    or self.phase != "murderer_action"
                ):
                    return False, "You cannot kill during this phase."

                found = self._find_player(target_name)

                if not found:
                    return False, "Target not found."

                target, target_player = found

                if target == session or not target_player["alive"]:
                    return False, "Choose another living player."

                logging.info(
                    "Murderer selected target: %s",
                    target_player["name"],
                )

                self._cancel_timer()
                self._kill(target, "Murderer")

                if self.phase != "ended":
                    self._resolve_wake_up()

                return True, "Murderer action resolved."

            # Voting and abstaining.
            if command in ("vote", "abstain"):
                if self.phase != "vote":
                    return False, "Voting is not open."

                if session in self.votes:
                    return False, "You have already voted."

                if command == "abstain":
                    self.votes[session] = None
                else:
                    found = self._find_player(target_name)

                    if not found:
                        return False, "Target not found."

                    target, target_player = found

                    if target == session or not target_player["alive"]:
                        return False, "Vote for another living player."

                    self.votes[session] = target

                self._spectators(
                    f"{player['name']} submitted a vote."
                )

                if all(
                    living_session in self.votes
                    for living_session in self._living()
                ):
                    self._resolve_vote()

                return True, "Vote recorded."

            return False, (
                "Unknown action. Use /choose, /target, /kill, "
                "/vote, or /abstain."
            )

    # --------------------------------------------------
    # Moderator interface
    # --------------------------------------------------

    def list_players(self):
        """Return a moderator-friendly list of players and roles."""
        with self.lock:
            lines = []

            for player in self.players.values():
                status = (
                    "Disconnected"
                    if not player["connected"]
                    else "Alive"
                    if player["alive"]
                    else "Spectator"
                )

                lines.append(
                    f"{player['name']} — {status} — {player['role']}"
                )

            return "\n".join(lines) or "No players have joined."

    def moderator_advance(self):
        """Advance discussion early; other phases cannot be skipped."""
        with self.lock:
            if self.phase != "discussion":
                return False, (
                    "Only the discussion phase can be advanced manually."
                )

            self._cancel_timer()
            self._begin_vote()

            return True, "Discussion ended. Voting has begun."
    
    def notify_shutdown(self):
        """Notify all connected clients that the server is closing."""
        with self.lock:
            self._broadcast(
                "SERVER_SHUTDOWN: The moderator has closed the game server."
            )
    
    def moderator_command(self, command):
        """Process one command entered by the moderator."""
        command = command.strip()

        if not command:
            return True

        parts = command.split(maxsplit=2)
        action = parts[0].lower()

        if action in ("help", "?"):
            print(
                """
Moderator commands:
  help                         Show this help
  players                      List players and roles
  status                       Show the current game state
  assign murderer NAME         Assign the Murderer
  assign magician NAME         Assign the Magician
  assign random                Randomly assign both roles
  start                        Start the game
  advance                      End discussion early
  restart                      Start a new lobby after a game
  quit                         Stop the server
"""
            )
            return True

        if action == "players":
            print(self.list_players())
            return True

        if action == "status":
            print(self.status())
            return True

        if action == "assign":
            if len(parts) < 2:
                print("Usage: assign murderer NAME | assign magician NAME")
                return True

            role = parts[1].lower()

            if role == "random":
                success, message = self.assign_random()
            elif role in ("murderer", "magician") and len(parts) == 3:
                success, message = self.assign(role, parts[2])
            else:
                print("Usage: assign murderer NAME | assign magician NAME")
                return True

            print(message)
            return True

        if action == "start":
            success, message = self.start()
            print(message)
            return True

        if action == "advance":
            success, message = self.moderator_advance()
            print(message)
            return True

        if action == "restart":
            success, message = self.restart()
            print(message)
            return True

        if action == "quit":
            return False

        print("Unknown command. Type 'help' to see available commands.")
        return True


# --------------------------------------------------
# Server entry point
# --------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    game = Game()
    server = create_server(game)
    server.start()

    logging.info("Murderers and Magicians server listening on 127.0.0.1:%s", PORT)
    logging.info("Type 'help' in this terminal for moderator commands.")
    
    try:
        while True:
            try:
                command = input("Moderator> ")
            except EOFError:
                break

            if not game.moderator_command(command):
                break

    except KeyboardInterrupt:
        print("\nShutting down server...")

    finally:
        # Notify clients before stopping the gRPC server.
        game.notify_shutdown()

        # Give the message streams time to deliver the notification.
        time.sleep(0.5)

        with game.lock:
            game._cancel_timer()

        server.stop(grace=2).wait()
        logging.info("Server stopped.")



if __name__ == "__main__":
    main()
