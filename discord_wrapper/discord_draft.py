import asyncio
import json
import logging
import os
import time
import traceback
import urllib.request
import uuid
from io import BytesIO
from typing import (TYPE_CHECKING, Any, Coroutine, Dict, Iterable, List,
                    Optional, Sequence, Set, TypedDict)

import aiohttp
import attr
import cattr
import numpy
from redis.asyncio import Redis
from interactions.client.errors import Forbidden, NotFound
from interactions.models import (ActionRow, Button, ButtonStyle, File, Member,
                         Message, User)
from interactions.models.discord.channel import (TYPE_MESSAGEABLE_CHANNEL, GuildText,
                                         ThreadChannel)
import sentry_sdk
from core_draft import cube

import core_draft.image_fetcher as image_fetcher
from core_draft.cog_exceptions import DMsClosedException, UserFeedbackException
from core_draft.draft import (CARDS_WITH_FUNCTION, Draft, DraftEffect, Stage,
                              player_card_drafteffect)
from core_draft.draft_player import DraftPlayer
from discord_wrapper.components import PAIR_BUTTON, card_buttons
from discord_wrapper.discord_draftbot import BotMember

if TYPE_CHECKING:
    from discord_wrapper.guild import GuildData

EMOJIS_BY_NUMBER = {1: '1⃣', 2: '2⃣', 3: '3⃣', 4: '4⃣', 5: '5⃣'}
NUMBERS_BY_EMOJI = {
    '1⃣': 1, '2⃣': 2, '3⃣': 3, '4⃣': 4, '5⃣': 5,
    '1': 1, '2': 2, '3': 3, '4': 4, '5': 5,
}
DEFAULT_CUBE_CUBECOBRA_ID = "penny_dreadful"

class FetchException(Exception):
    pass

class MessageData(TypedDict):
    row: int
    message: Message
    len: int

@attr.s(auto_attribs=True)
class GuildDraft:
    """
    Discord-aware wrapper for a Draft.
    """
    guild: 'GuildData' = attr.ib(repr=False)
    players: Dict[int, Member | BotMember] = attr.ib(factory=dict)
    uuid: str = ''
    messages_by_player: Dict[int, Dict[int, MessageData]] = attr.ib(factory=dict, repr=False)  # messages_by_player[player_id][message_id] = MessageData
    draft: Optional[Draft] = None
    abandon_votes: Set[int] = attr.ib(factory=set)

    @property
    def start_channel_id(self) -> Optional[int]:
        if self.draft:
            return self.draft.metadata.get('start_channel_id', None)
        return None

    @start_channel_id.setter
    def start_channel_id(self, value: int) -> None:
        if self.draft:
            self.draft.metadata['start_channel_id'] = value
        else:
            raise RuntimeError("Can't set start_channel_id before draft is initialized")

    # @property
    # def challonge_id(self) -> Optional[str]:
    #     return self.draft.metadata.get('challonge_id', None)

    # @challonge_id.setter
    # def challonge_id(self, value: str) -> None:
    #     if self.draft:
    #         self.draft.metadata['challonge_id'] = value
    #     else:
    #         raise RuntimeError("Can't set challonge_id before draft is initialized")

    @property
    def gatherling_id(self) -> Optional[str]:
        val = self.draft.metadata.get('gatherling_id', None)
        if isinstance(val, dict):  # ???
            val = val.get('id', None)
        if isinstance(val, int):
            val = str(val)
        return val

    @gatherling_id.setter
    def gatherling_id(self, value: Optional[str]) -> None:
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str) and value is not None:
            raise RuntimeError(f"Can't set gatherling_id to {value}")
        if self.draft:
            self.draft.metadata['gatherling_id'] = value
        else:
            raise RuntimeError("Can't set gatherling_id before draft is initialized")

    @property
    def name(self) -> str:
        if self.draft.name:
            return self.draft.name
        return self.uuid

    def id(self) -> str:
        return self.uuid

    def id_with_guild(self) -> str:
        return f"{self.guild.name}: {self.uuid}"

    def get_players(self) -> Iterable[Member | BotMember]:
        return self.players.values()

    def has_player(self, player: User | Member) -> bool:
        return player.id in self.players

    def has_message(self, message_id: int) -> bool:
        for _, messages in self.messages_by_player.items():
            if message_id in messages:
                return True
        return False

    def get_pending_players(self) -> List[Member | BotMember]:
        if not self.draft:
            return []
        pending = self.draft.get_pending_players()
        return [self.players[x.id] for x in pending]

    async def get_channel(self) -> Optional[GuildText]:
        if self.start_channel_id is None:
            return None
        return await self.guild.guild.fetch_channel(self.start_channel_id)  # type: ignore

    async def get_thread(self) -> Optional[ThreadChannel]:
        if self.draft is None:
            return None
        return await self.guild.guild.fetch_thread(self.draft.metadata['thread_id'])

    def fill_bots(self, expected_players: int) -> None:
        num_bots = expected_players - len(self.players)
        print(f'Filling with {num_bots} bots')
        for i in range(num_bots):
            self.players[i] = BotMember(self.guild.guild._client, i, self)

    async def start(self, channel: TYPE_MESSAGEABLE_CHANNEL, packs: int, cards: int, cube: str) -> None:
        if not self.uuid:
            self.uuid = str(uuid.uuid4()).replace('-', '')
        card_list = await get_card_list(cube)
        self.draft = Draft(list(self.players.keys()), card_list)
        self.start_channel_id = channel.id
        for p in self.players.values():
            self.messages_by_player[p.id] = {}
        names = ", ".join([p.display_name for p in self.get_players()])
        msg = await channel.send("Starting the draft of https://cubecobra.com/cube/overview/{cube_id} with {p}".format(p=names, cube_id=cube))
        players_to_update = self.draft.start(packs, cards)
        intro = "The draft has started. Pack 1, Pick 1:"
        await asyncio.gather(*[self.send_pack_to_player(intro, p) for p in players_to_update])
        thread = await msg.create_thread(self.uuid)
        self.draft.metadata['thread_id'] = thread.id
        await thread.send(' '.join(p.mention for p in self.players.values()))

    async def pick(self, player_id: int, message_id: int, emoji: str) -> None:
        if message_id is not None and emoji is not None and self.draft is not None:
            page_number = self.messages_by_player[player_id][message_id]["row"]
            item_number = NUMBERS_BY_EMOJI.get(emoji)
            if item_number is None:
                item_number = int(emoji)
            print("Player {u} reacted with {n} for row {i}".format(u=player_id, n=item_number, i=page_number))
            index = item_number + (5 * (page_number - 1))
            await self.pick_by_index(player_id, index)
        else:
            print(f"Missing message_id({message_id} + emoji({emoji})")
            return

    async def pick_by_index(self, player_id: int, index: int) -> None:
        if self.draft is None:
            raise RuntimeError("Can't pick without a draft")
        info = self.draft.pick(player_id, index)
        await self.handle_pick_response(info.updates, player_id, info.draft_effect)


    async def picks(self, messageable: User | Member | BotMember, player_id: int) -> None:
        if not self.draft:
            await messageable.send("The draft hasn't started yet.")
            return

        cards = self.draft.deck_of(player_id)
        if len(cards) == 0:
            await messageable.send(f"[{self.id_with_guild()}] You haven't picked any card yet")
            return
        else:
            await messageable.send(f"[{self.id_with_guild()}] Deck: ")
        for page in range(0, int(len(cards) / 5) + 1):
            row = cards[5 * page: 5 * page + 5]
            if row is not None and len(row) > 0:
                cardlist = list(row)
                image_file = await image_fetcher.download_image_async(cardlist)
                if image_file:
                    await send_image_with_retry(messageable, image_file)
                else:
                    await messageable.send(f"Couldn't download images for {cardlist}")
        await self.send_deckfile_to_player(messageable, player_id)

    async def send_current_pack_to_player(self, intro: str, player_id: int) -> None:
        if self.draft is None:
            return
        player = self.draft.player_by_id(player_id)
        await self.send_pack_to_player(intro, player)

    async def send_pack_to_player(self, intro: str, player: DraftPlayer) -> None:
        if self.draft is None:
            return
        if player.draftbot:
            # todo: send pack to bot
            return
        player_id = player.id
        messageable: Member | BotMember = self.players[player_id]
        self.messages_by_player[player_id].clear()
        try:
            await messageable.send(f"[{self.id_with_guild()}] {intro}")
        except Forbidden as e:
            raise DMsClosedException(messageable, e.response, e.text) from e
        pack = self.draft.pack_of(player_id)
        if pack is None:
            return
        cards = pack.cards
        print(numpy.array(cards))
        rows = numpy.array_split(numpy.array(cards), [5, 10])  # split at positions 5 and 10, defaulting to empty arrays
        i = 1
        for row in rows:
            if row is not None and len(row) > 0:
                image_file = await image_fetcher.download_image_async(row)
                if image_file is None:
                    raise RuntimeError(f"Couldn't download images for {row}")
                cardrow: list[str] = list(row)
                components: list[ActionRow] = card_buttons(cardrow)
                message = await send_image_with_retry(messageable, image_file, components=components)
                if message:
                    self.messages_by_player[player_id][message.id] = {"row": i, "message": message, "len": len(row)}
                i += 1

        if actions := [a for a in player.face_up if a in CARDS_WITH_FUNCTION]:
            emoji_cog = self.guild.guild._client.get_ext('EmojiGuild')
            text = ''.join([f'{await emoji_cog.get_emoji(a)} {a}' for a in actions])

            await messageable.send(f'Optionally activate: {text}', components=await self.conspiracy_buttons(actions))



    async def conspiracy_buttons(self, cards: Iterable[str]) -> List[ActionRow]:
        emoji_cog = self.guild.guild._client.get_ext('EmojiGuild')
        return [ActionRow(
            *[  # type: ignore
                Button(style=ButtonStyle.GREY,
                       label=c,
                       custom_id=f'{i + 1}',
                       emoji=await emoji_cog.get_emoji(c),
                       )
                for i, c in enumerate(cards)
            ],
        )]

    async def handle_pick_response(self, updates: Dict[DraftPlayer, List[str]], player_id: int, effects: List[player_card_drafteffect]) -> None:
        if self.draft is None:
            return
        coroutines: list[Coroutine] = []

        if player_id:
            self.messages_by_player[player_id].clear()

        current_player_has_next_booster = False
        for effect in effects:
            player_name = self.players[effect[0].id].display_name
            text = f'{player_name} drafts {effect[1]} face up'
            if effect[2] == DraftEffect.add_booster_to_draft:
                text += ' and adds a new booster to the draft.'
            for p in self.players.values():
                coroutines.append(p.send(text))
            channel = await self.get_channel()
            if channel is not None:
                revealed_card = await image_fetcher.download_image_async([effect[1]])
                if revealed_card is not None:
                    await channel.send(text, file=File(revealed_card))

        for player, autopicks in updates.items():
            deck = ''
            current_pack = ''

            messageable = self.players[player.id]
            if autopicks:
                autopick_str = ', '.join(autopicks)
                image = await image_fetcher.download_image_async(autopicks)
                file = None
                if image:
                    file = File(image)
                coroutines.append(messageable.send(f'[{self.id_with_guild()}] Autopicks: {autopick_str}', file=file))

            if player.current_pack is not None:
                if player.id == player_id:
                    current_player_has_next_booster = True
                deck = f'Deck: {", ".join(player.deck)}\n'
                current_pack = f'Pack {player.current_pack.number}, Pick {player.current_pack.pick_number}:\n'
                intro = f"{deck}{current_pack}"
                coroutines.append(self.send_pack_to_player(intro, player))

        if not current_player_has_next_booster and not self.draft.is_draft_finished():
            pending = ", ".join([p.display_name for p in self.get_pending_players()])
            coroutines.append(self.players[player_id].send(f"[{self.id_with_guild()}] Waiting for other players to make their picks: {pending}"))

        await asyncio.gather(*coroutines)

        if self.draft.is_draft_finished():
            channel = await self.get_channel()
            if channel is not None:
                await channel.send("Finished the draft with {p}".format(p=", ".join([p.display_name for p in self.get_players()])))

            thread = await self.get_thread()
            if thread is not None:
                await thread.send("Draft finished", components=[PAIR_BUTTON])

            for member in self.players.values():
                await member.send(f"[{self.id_with_guild()}] The draft has finished")
                await self.send_deckfile_to_player(member, member.id)
                if not isinstance(member, BotMember):
                    with open(f'decks/{self.id()}_{member.id}.txt', 'w') as f:
                        f.writelines(generate_file_content(self.draft.deck_of(player_id)))
            self.draft.stage = Stage.draft_complete
            self.messages_by_player.clear()

    async def send_deckfile_to_player(self, messagable: User | Member | BotMember, player_id: int) -> None:
        if self.draft is None:
            return
        content = generate_file_content(self.draft.deck_of(player_id))
        file = BytesIO(bytes(content, 'utf-8'))
        await messagable.send(content=f"[{self.id_with_guild()}] Your deck", file=File(file=file, file_name=f"{self.guild.name}_{time.strftime('%Y%m%d')}.txt"))

    async def save_state(self, redis: Redis) -> None:
        state = json.dumps(cattr.unstructure(self.draft))
        with open(os.path.join('drafts', f'{self.uuid}.json'), 'w') as f:
            f.write(state)
        await redis.set(f'draft:{self.uuid}', state, ex=2419200)

    async def load_state(self, redis: Redis) -> None:
        state = await redis.get(f'draft:{self.uuid}')
        if state is None:
            print(f'{self.uuid} could not be found')
            path = os.path.join('drafts', f'{self.uuid}.json')
            if os.path.exists(path):
                with open(path) as f:
                    state = f.read()
            else:
                return
        try:
            if isinstance(state, bytes):
                state = state.decode()
            self.draft = cattr.structure(json.loads(state), Draft)
        except Exception as e:
            print(f'{self.uuid} failed to reload\n{e}')
            traceback.print_exc()
            sentry_sdk.capture_exception(e)
            return

        if self.draft is None:
            logging.error(f'{self.uuid} failed to reload?', stacklevel=3)
            return
        # await self.guild.guild.query_members(user_ids=self.draft.players)
        for player in self.draft.players:
            try:
                drafter = self.draft.player_by_id(player)
                member: Member | BotMember | None = await self.guild.guild.fetch_member(player)
                if drafter.draftbot or not member:
                    if not drafter.draftbot:
                        logging.warning(f'{self.uuid} failed to reload, {player} not found', stacklevel=3)
                        drafter.draftbot = True
                    member = BotMember(self.guild.guild._client, player, self)
                self.players[player] = member
                self.messages_by_player[player] = dict()
                if drafter.current_pack is not None:
                    await self.send_current_pack_to_player("Bump: ", player)
            except NotFound:
                logging.error(f'{self.uuid} failed to reload, {player} not found', stacklevel=3)
                return
            except DMsClosedException:
                logging.error(f'{self.uuid} failed to reload, {player} closed their DMs', stacklevel=3)
                return

    async def abandon(self, player_id: int) -> bool:
        if self.draft is None:
            return False
        self.abandon_votes.add(player_id)
        needed = min(3, len(self.players))
        if len(self.abandon_votes) >= needed:
            self.guild.drafts_in_progress.remove(self)
            self.draft.stage = Stage.draft_complete
            await self.save_state(self.guild.redis)
            return True
        return False

    async def swap_seats(self, old_player: int, new_player: int) -> bool:
        if not self.draft or old_player not in self.draft.players or new_player in self.draft.players or old_player == new_player:
            return False
        member = await self.guild.guild.fetch_member(new_player)
        if member is None:
            return False
        self.players[new_player] = member
        del self.players[old_player]
        if old_player in self.abandon_votes:
            self.abandon_votes.remove(old_player)
        self.draft.player_by_id(old_player).id = new_player
        index = self.draft.players.index(old_player)
        self.draft.players[index] = new_player
        await self.picks(member, new_player)
        await self.send_current_pack_to_player("Thanks for joining the draft!\n", new_player)
        return True

async def send_image_with_retry(user: User | Member | BotMember, image_file: str, text: str = '', **kwargs: Any) -> Message | None:
    text = escape_underscores(text)
    try:
        message = await user.send(file=image_file, content=text, **kwargs)
    except RuntimeError as e:
        if 'lock a bucket' in e.args[0]:
            print('Message failed to send, retrying')
            await asyncio.sleep(1)
            return await send_image_with_retry(user, image_file, text, **kwargs)

    if message and message.attachments and message.attachments[0].size == 0:
        print('Message size is zero so resending')
        await message.delete()
        return await user.send(file=image_file, content=text, **kwargs)
    return message


def escape_underscores(s: str) -> str:
    new_s = ''
    in_url, in_emoji = False, False
    for char in s:
        if char == ':':
            in_emoji = True
        elif char not in 'abcdefghijklmnopqrstuvwxyz_':
            in_emoji = False
        if char == '<':
            in_url = True
        elif char == '>':
            in_url = False
        if char == '_' and not in_url and not in_emoji:
            new_s += '\\_'
        else:
            new_s += char
    return new_s


def generate_file_content(cards: Sequence[str]) -> str:
    return "\n".join(["1 {c}".format(c=card) for card in cards])

async def fetch(session, url: str) -> str:
    async with session.get(url) as response:
        if response.status >= 400:
            raise UserFeedbackException(f"Unable to load cube list from {url}")
        return await response.text()  # type: ignore

async def get_card_list(cube_name: str) -> List[str]:
    if cube_name == '$':
        return get_cards()
    if cube_name is None:
        try:
            return await load_cubecobra_cube(DEFAULT_CUBE_CUBECOBRA_ID)
        except Exception as e:
            print(e)
            return get_cards()
    else:
        return await load_cubecobra_cube(cube_name)


async def load_cubecobra_cube(cubecobra_id: str) -> List[str]:
    try:
        c = await cube.load_cubecobra_cube(cubecobra_id)
        return await c.cardlist()
    except Exception as e:
        sentry_sdk.capture_exception(e)
        return await load_cubecobra_cubelist(cubecobra_id)

async def load_cubecobra_cubelist(cubecobra_id: str) -> List[str]:
    url = f'https://cubecobra.com/cube/api/cubelist/{cubecobra_id}'
    print(f'Async fetching {url}')
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as aios:
            response = (await fetch(aios, url)).split("\n")
            print(response)
            print(f"{type(response)}")
            return response
    except (urllib.error.HTTPError, aiohttp.ClientError) as e:
        raise UserFeedbackException(f"Unable to load cube list from {url}") from e


def get_cards(file_name: str = 'EternalPennyDreadfulCube.txt') -> List[str]:
    with open(file_name) as f:
        return f.read().splitlines()
