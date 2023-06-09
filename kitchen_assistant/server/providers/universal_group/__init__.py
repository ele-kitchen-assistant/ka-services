"""
Universal Group Player provider.

This is more like a "virtual" player provider,
allowing the user to create player groups from all players known in the system.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kitchen_assistant.common.models.config_entries import (
    CONF_ENTRY_FLOW_MODE,
    CONF_ENTRY_GROUPED_POWER_ON,
    CONF_ENTRY_HIDE_GROUP_MEMBERS,
    CONF_ENTRY_OUTPUT_CHANNELS,
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
)
from kitchen_assistant.common.models.enums import (
    ConfigEntryType,
    PlayerFeature,
    PlayerState,
    PlayerType,
)
from kitchen_assistant.common.models.player import DeviceInfo, Player
from kitchen_assistant.common.models.queue_item import QueueItem
from kitchen_assistant.constants import CONF_GROUPED_POWER_ON
from kitchen_assistant.server.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from kitchen_assistant.common.models.config_entries import ProviderConfig
    from kitchen_assistant.common.models.provider import ProviderManifest
    from kitchen_assistant.server import MusicAssistant
    from kitchen_assistant.server.models import ProviderInstanceType


CONF_GROUP_MEMBERS = "group_members"

CONF_ENTRY_OUTPUT_CHANNELS_FORCED_STEREO = ConfigEntry.from_dict(
    {
        **CONF_ENTRY_OUTPUT_CHANNELS.to_dict(),
        "hidden": True,
        "default_value": "stereo",
        "value": "stereo",
    }
)
CONF_ENTRY_FORCED_FLOW_MODE = ConfigEntry.from_dict(
    {**CONF_ENTRY_FLOW_MODE.to_dict(), "hidden": True, "default_value": True, "value": True}
)
# ruff: noqa: ARG002


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    prov = UniversalGroupProvider(mass, manifest, config)
    await prov.handle_setup()
    return prov


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # ruff: noqa: ARG001
    all_players = tuple(
        ConfigValueOption(x.display_name, x.player_id)
        for x in mass.players.all(True, True, False)
        if x.player_id != instance_id
    )
    return (
        ConfigEntry(
            key=CONF_GROUP_MEMBERS,
            type=ConfigEntryType.STRING,
            label="Group members",
            default_value=[],
            options=all_players,
            description="Select all players you want to be part of this group",
            multi_value=True,
        ),
    )


class UniversalGroupProvider(PlayerProvider):
    """Base/builtin provider for universally grouping players."""

    prev_sync_leaders: tuple[str] | None = None

    async def handle_setup(self) -> None:
        """Handle async initialization of the provider."""
        self.player = Player(
            player_id=self.instance_id,
            provider=self.domain,
            type=PlayerType.GROUP,
            name=self.name,
            available=True,
            powered=False,
            device_info=DeviceInfo(model=self.manifest.name, manufacturer="Music Assistant"),
            # TODO: derive playerfeatures from (all) underlying child players
            supported_features=(
                PlayerFeature.POWER,
                PlayerFeature.PAUSE,
                PlayerFeature.VOLUME_SET,
                PlayerFeature.VOLUME_MUTE,
                PlayerFeature.SET_MEMBERS,
            ),
            active_source=self.instance_id,
            group_childs=self.config.get_value(CONF_GROUP_MEMBERS),
        )
        self.mass.players.register_or_update(self.player)

    async def unload(self) -> None:
        """Handle close/cleanup of the provider."""
        self.mass.players.remove(self.instance_id)

    def get_player_config_entries(self, player_id: str) -> tuple[ConfigEntry]:  # noqa: ARG002
        """Return all (provider/player specific) Config Entries for the given player (if any)."""
        return (
            CONF_ENTRY_HIDE_GROUP_MEMBERS,
            CONF_ENTRY_GROUPED_POWER_ON,
            CONF_ENTRY_OUTPUT_CHANNELS_FORCED_STEREO,
            CONF_ENTRY_FORCED_FLOW_MODE,
        )

    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player."""
        # forward command to player and any connected sync child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True, skip_sync_childs=True):
                if member.state == PlayerState.IDLE:
                    continue
                tg.create_task(self.mass.players.cmd_stop(member.player_id))

    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY command to given player."""
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True, skip_sync_childs=True):
                tg.create_task(self.mass.players.cmd_play(member.player_id))

    async def cmd_play_media(
        self,
        player_id: str,
        queue_item: QueueItem,
        seek_position: int = 0,
        fade_in: bool = False,
        flow_mode: bool = False,
    ) -> None:
        """Send PLAY MEDIA command to given player.

        This is called when the Queue wants the player to start playing a specific QueueItem.
        The player implementation can decide how to process the request, such as playing
        queue items one-by-one or enqueue all/some items.

            - player_id: player_id of the player to handle the command.
            - queue_item: the QueueItem to start playing on the player.
            - seek_position: start playing from this specific position.
            - fade_in: fade in the music at start (e.g. at resume).
        """
        # send stop first
        await self.cmd_stop(player_id)
        # power ON
        await self.cmd_power(player_id, True)
        # issue sync command (just in case)
        await self._sync_players()
        # forward command to all (powered) group child's
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True, skip_sync_childs=True):
                player_prov = self.mass.players.get_player_provider(member.player_id)
                tg.create_task(
                    player_prov.cmd_play_media(
                        member.player_id,
                        queue_item=queue_item,
                        seek_position=seek_position,
                        fade_in=fade_in,
                        flow_mode=flow_mode,
                    )
                )

    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player."""
        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(only_powered=True, skip_sync_childs=True):
                tg.create_task(self.mass.players.cmd_pause(member.player_id))

    async def cmd_power(self, player_id: str, powered: bool) -> None:
        """Send POWER command to given player."""
        if self.player.powered == powered:
            return  # nothing to do
        group_power_on = self.mass.config.get_player_config_value(player_id, CONF_GROUPED_POWER_ON)
        if powered and not group_power_on:
            return  # nothing to do

        async def set_child_power(child_player: Player) -> None:
            await self.mass.players.cmd_power(child_player.player_id, powered)
            # set optimistic state on child player to prevent race conditions in other actions
            child_player.powered = powered

        async with asyncio.TaskGroup() as tg:
            for member in self._get_active_members(
                only_powered=not powered, skip_sync_childs=False
            ):
                tg.create_task(set_child_power(member))

        self.player.powered = powered
        self.mass.players.update(self.instance_id)
        if powered:
            # sync all players on power on
            await self._sync_players()

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player."""

    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME MUTE command to given player."""

    async def poll_player(self, player_id: str) -> None:
        """Poll player for state updates."""
        self.update_attributes()
        self.mass.players.update(player_id, skip_forward=True)

    def update_attributes(self) -> None:
        """Update player attributes."""
        all_members = self._get_active_members(only_powered=False, skip_sync_childs=False)
        self.player.group_childs = list(x.player_id for x in all_members)
        # read the state from the first powered child player
        for member in all_members:
            if member.synced_to:
                continue
            if not member.powered:
                continue
            if member.state not in (PlayerState.PLAYING, PlayerState.PAUSED):
                continue
            self.player.current_item_id = member.current_item_id
            self.player.current_url = member.current_url
            self.player.elapsed_time = member.elapsed_time
            self.player.elapsed_time_last_updated = member.elapsed_time_last_updated
            self.player.state = member.state
            break
        else:
            self.player.state = PlayerState.IDLE
            self.player.current_item_id = None
            self.player.current_url = None

    def on_child_state(self, player_id: str, child_player: Player, changed_keys: set[str]) -> None:
        """Call when the state of a child player updates."""
        # TODO: handle a sync leader powerin off
        powered_players = self._get_active_members(True, False)
        if "powered" in changed_keys:
            if child_player.powered and self.player.state == PlayerState.PLAYING:
                # a child player turned ON while the group player is already playing
                # we need to resync/resume
                self.mass.create_task(self.mass.players.queues.resume, player_id)
            elif not child_player.powered and len(powered_players) == 0:
                # the last player of a group turned off
                # turn off the group
                self.mass.create_task(self.cmd_power, player_id, False)
        self.update_attributes()
        self.mass.players.update(player_id, skip_forward=True)

    def _get_active_members(
        self, only_powered: bool = False, skip_sync_childs: bool = True
    ) -> list[Player]:
        """Get (child) players attached to a grouped player."""
        child_players: list[Player] = []
        conf_members: list[str] = self.config.get_value(CONF_GROUP_MEMBERS)
        ignore_ids = set()
        for child_id in conf_members:
            if child_player := self.mass.players.get(child_id, False):
                if not (not only_powered or child_player.powered):
                    continue
                if child_player.synced_to and skip_sync_childs:
                    continue
                allowed_sources = [child_player.player_id, self.instance_id] + conf_members
                if child_player.active_source not in allowed_sources:
                    # edge case: the child player has another group already active!
                    continue
                if child_player.synced_to and child_player.synced_to not in conf_members:
                    # edge case: the child player is already synced to another player
                    continue
                child_players.append(child_player)
                # handle edge case where a group is in the group and both the group
                # and (one of its) child's are added to this universal group.
                if child_player.type == PlayerType.GROUP:
                    ignore_ids.update(
                        x for x in child_player.group_childs if x != child_player.player_id
                    )
        return [x for x in child_players if x.player_id not in ignore_ids]

    async def _sync_players(self) -> None:
        """Sync all (possible) players."""
        sync_leaders = set()
        # TODO: sort members on sync master priority attribute ?
        for member in self._get_active_members(only_powered=True):
            if member.synced_to is not None:
                continue
            if not member.can_sync_with:
                continue
            # check if we can join this player to an already chosen sync leader
            if existing_leader := next(
                (x for x in member.can_sync_with if x in sync_leaders), None
            ):
                await self.mass.players.cmd_sync(member.player_id, existing_leader)
                # set optimistic state to prevent race condition in play media
                member.synced_to = existing_leader
                continue
            # pick this member as new sync leader
            sync_leaders.add(member.player_id)
        self.prev_sync_leaders = tuple(sync_leaders)
