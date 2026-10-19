# Murderers and Magicians



A text-based game inspired by Werewolves. Each player is a separate Linux user on the same machine, and joins the game via terminal. This game uses gRPC to communicate locally.



Roles:

* Murderer - chooses a player to kill each night.
* Magician - chooses between Scry (learn the identity of a random Townsperson), Shield (protect someone from being killed for the rest of the day), and Sacrifice (perform a ritual that kills someone at dawn).
* Townsperson - uncovers the truth of the town, and votes to execute dangerous individuals.
* Spectator - watches events unfold.





Instructions:



1. Copy all game files into each user's directory.
2. Have each user run "python3 install.py".
3. Have the moderator run "python3 server.py" to begin the game.
4. Have players run "python3 client.py" to join the game.



