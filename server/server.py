#!/usr/bin/env python

import asyncio
import logging
import websockets
import json
import ytmusicapi
import datetime

log = logging.getLogger("youtube-sync")
log.setLevel(logging.DEBUG)
log_handler = logging.StreamHandler()
log_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
log.addHandler(log_handler)
ytmusic = ytmusicapi.YTMusic()


def error_event(error: str, message: str):
    log.warning(f"Error while handling request: {error}: {message}")
    return json.dumps({"event": "error", "error": error, "message": message})


class Guild:
    def __init__(self, id: str):
        self.id = id
        self.users = {}
        self.media_state = {
            "current_time": 0,
            "length": 0,
            "playing": False,
            "queue_index": -1,
        }
        self.last_update_time = datetime.datetime.now()
        self.was_paused = False
        self.queue = []
        log.debug(f"Initialised guild {id}")

    def update_media_state_time(self):
        if self.media_state["playing"] and not self.was_paused:
            self.media_state["current_time"] = min(
                self.media_state["current_time"]
                + int(
                    (datetime.datetime.now() - self.last_update_time).total_seconds()
                ),
                self.media_state["length"],
            )
        self.was_paused = not self.media_state["playing"]
        self.last_update_time = datetime.datetime.now()

    def media_state_event(self) -> str:
        log.debug("Media state event fired")
        self.update_media_state_time()
        return json.dumps({"event": "state", **self.media_state})

    def users_event(self) -> str:
        log.debug("User event fired")
        return json.dumps(
            {
                "event": "users",
                "count": len(self.users),
                "users": list(self.users.values()),
            }
        )

    def queue_event(self) -> str:
        log.debug("Queue event fired")
        return json.dumps({"event": "queue", "queue": self.queue})

    async def notify_all(self, msg: str):
        if self.users:
            await asyncio.wait([asyncio.create_task(u.send(msg)) for u in self.users])

    async def register(self, websocket):
        self.users[websocket] = {"id": hash(websocket), "finished": True}
        log.debug(
            f"New user with id {hash(websocket)} (now {len(self.users)} total) registered in guild {self.id}."
        )
        await self.notify_all(self.users_event())
        await websocket.send(self.queue_event())
        await websocket.send(self.media_state_event())

    async def unregister(self, websocket):
        self.users.pop(websocket, None)
        log.debug(
            f"User id {hash(websocket)} disconnected in guild {self.id}, now {len(self.users)}."
        )
        await self.notify_all(self.users_event())

    async def action_set_profile(self, websocket, data: dict):
        for key in ["name", "identifier", "art"]:
            assert type(key) == str
            if key in data:
                self.users[websocket][key] = data[key]
            else:
                self.users[websocket].pop(key, None)
        log.debug(f"Edited profile of user id {hash(websocket)}.")
        await self.notify_all(self.users_event())

    async def action_play_pause(self, websocket, playing: bool):
        assert type(playing) == bool
        self.media_state["playing"] = playing
        log.debug(
            f"Playback state set to {'playing' if playing else 'paused'} by user id {hash(websocket)}."
        )
        await self.notify_all(self.media_state_event())

    async def action_add(self, websocket, data: dict):
        assert "query" in data or "video_id" in data
        if "url" in data:
            try:
                song = ytmusic.get_song(data["video_id"])["videoDetails"]
            except KeyError:
                return await websocket.send(
                    error_event(
                        "InvalidVideoError",
                        "The video ID provided was not a valid video.",
                    )
                )

            song_metadata = {
                "url": f"https://youtube.com/embed/{song['videoId']}",
                "title": song["title"],
                "artist": song["author"],
                "length": int(song["lengthSeconds"]),
                "art": song["thumbnails"][0]["url"],
            }
        else:
            song = ytmusic.search(data["query"], "songs")[0]
            # TODO: consider using "video" over "song" for greater
            # but lower quality results as fallbacks

            song_metadata = {
                "url": f"https://youtube.com/embed/{song['videoId']}",
                "title": song["title"],
                "artist": ", ".join(a["name"] for a in song["artists"]),
                "length": sum(
                    i * int(p)
                    for i, p in zip(
                        [1, 60, 3600], reversed(song["duration"].split(":"))
                    )
                ),
                "art": song["thumbnails"][0]["url"],
            }

        # play immediately only if we're on last song and (internally) song has ended
        self.update_media_state_time()
        play_immediately = (
            self.media_state["queue_index"] == len(self.queue) - 1
            and self.media_state["current_time"] == self.media_state["length"]
        )

        self.queue.append(song_metadata)
        log.debug(
            f"User id {hash(websocket)} added {song_metadata['title']} ({song_metadata['url']}) to the queue"
        )
        await self.notify_all(self.queue_event())
        if play_immediately:
            log.debug("End of queue, playing newly added song immediately")
            await self.action_jump(websocket, {"index": 1})

    async def action_remove(self, websocket, index: int):
        assert type(index) == int
        assert not index == 0
        try:
            log.debug(
                f"User id {hash(websocket)} removed {self.queue[index]['title']} (index {index}) from the queue"
            )
            del self.queue[index]
        except IndexError:
            return await websocket.send(
                error_event("IndexError", "The index provided is out of bounds.")
            )
        await self.notify_all(self.queue_event())

    async def action_jump(self, websocket, data: dict):
        assert type(data["index"]) == int
        time: int = 0
        if "time" in data:
            assert type(data["time"]) == int
            time = data["time"]
        if not 0 <= self.media_state["queue_index"] + data["index"] < len(self.queue):
            return await websocket.send(
                error_event(
                    "IndexError", "The index provided is out of bounds of the queue."
                )
            )

        video_index = self.media_state["queue_index"] + data["index"]
        if not 0 <= time <= self.queue[video_index]["length"]:
            return await websocket.send(
                error_event(
                    "TimeLimitExceededError",
                    "The seek time specified is greater than the length of the video.",
                )
            )
        log.debug(
            f"User id {hash(websocket)} jumped to {self.queue[video_index]['title']}."
        )

        # reset internal state variables
        for u in self.users:
            self.users[u]["finished"] = False
        self.time_paused = 0
        self.last_update_time = datetime.datetime.now()
        self.media_state = {
            "current_time": time,
            "length": self.queue[video_index]["length"],
            "playing": True,
            "queue_index": video_index,
        }
        log.debug("Reset internal state variables")
        await self.notify_all(self.media_state_event())

    async def action_mark_finished(self, websocket):
        self.users[websocket]["finished"] = True
        log.debug(
            f"User id {hash(websocket)} finished current video, {len(self.users)-sum(self.users[u]['finished'] for u in self.users)} left"
        )
        if sum(self.users[u]["finished"] for u in self.users) == len(self.users):
            for u in self.users:
                self.users[u]["finished"] = False
            log.debug("All users finished")
            if not self.media_state["queue_index"] == len(self.queue) - 1:
                # if there are more items in the queue
                log.debug("Jumping to next video")
                await self.action_jump(None, {"index": 1})
            # otherwise do nothing


guilds = {}


async def counter(websocket, path: str):
    # TODO: consider copying Hoyolab's API body format
    guild_index: int = path.find("?guild=") + len("?guild=")
    if guild_index < len("?guild="):
        # if index not found
        await websocket.send(error_event("GuildError", "Guild not specified in path."))
        return

    # get guild string from url
    guild_id: str = path[guild_index:]

    if not guild_id in guilds:
        # make a new guild if the current one does not exist
        guilds[guild_id] = Guild(guild_id)
    guild: Guild = guilds[guild_id]

    try:
        await guild.register(websocket)
        async for message in websocket:
            try:
                data = json.loads(message)
                action = data["action"]
            except (KeyError, json.decoder.JSONDecodeError):
                await websocket.send(error_event("RequestError", "No action given."))
                continue

            try:
                if action == "set_profile":
                    await guild.action_set_profile(websocket, data)
                elif action == "play" or action == "pause":
                    await guild.action_play_pause(websocket, action == "play")
                elif action == "add":
                    await guild.action_add(websocket, data)
                elif action == "remove":
                    await guild.action_remove(websocket, data["index"])
                elif action == "jump":
                    await guild.action_jump(websocket, data)
                elif action == "finished":
                    await guild.action_mark_finished(websocket)
                else:
                    await websocket.send(
                        error_event("RequestError", "Invalid action given.")
                    )
            except KeyError as e:
                await websocket.send(error_event("RequestError", "Malformed request."))
            except AssertionError as e:
                await websocket.send(error_event("RequestError", "Malformed request."))
            except Exception as e:
                await websocket.send(
                    error_event("Error", "An unexpected error occurred.")
                )
                log.error(e)

    finally:
        await guild.unregister(websocket)
        if not guild.users:
            # if the guild is empty, destroy it
            guilds.pop(guild_id)
            log.debug(f"Popped guild {guild_id}")


start_server = websockets.serve(counter, "localhost", 6789)
log.info("Started websocket server")

asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
log.debug("Closed websocket")
