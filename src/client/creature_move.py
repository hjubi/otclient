"""
Python translation of ProtocolGame::parseCreatureMove and all its dependencies.

Original C++ source: src/client/protocolgameparse.cpp

Execution flow:
  1. parseCreatureMove        – entry point, reads packet and orchestrates move
  2. getMappedThing           – resolves creature/thing from packet data
  3. getPosition              – reads x,y,z from packet
  4. Map.remove_thing         – detaches thing from its current tile
  5. Creature.allow_appear_walk – arms the "start walk animation on next appear"
  6. Map.add_thing            – attaches thing to the new tile
  7. Tile.add_thing           – inserts thing into tile's ordered thing list
  8. Creature.on_appear       – detects walk vs teleport and fires walk()
  9. Creature.walk            – initialises smooth walking animation state
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Constants (src/client/const.h, src/framework/const.h)
# ---------------------------------------------------------------------------

UINT16_MAX = 0xFFFF
UINT8_MAX = 0xFF
BLOCK_SIZE = 32          # Otc::BLOCK_SIZE
MAP_MAX_Z = 15           # default; would normally come from g_gameConfig
RAD_TO_DEC = 180.0 / math.pi


class Direction(IntEnum):
    North = 0
    East = 1
    South = 2
    West = 3
    NorthEast = 4
    SouthEast = 5
    SouthWest = 6
    NorthWest = 7
    InvalidDirection = 8


class Operation(IntEnum):
    ADD = 0
    REMOVE = 1
    UPDATE = 2


# ---------------------------------------------------------------------------
# Position (src/client/position.h + position.cpp)
# ---------------------------------------------------------------------------

@dataclass
class Position:
    x: int = UINT16_MAX
    y: int = UINT16_MAX
    z: int = UINT8_MAX

    def is_map_position(self) -> bool:
        """
        bool Position::isMapPosition() const
        { return ((x >= 0) && (y >= 0) && (x < UINT16_MAX) && (y < UINT16_MAX)
                  && (z <= g_gameConfig.getMapMaxZ())); }
        """
        return (0 <= self.x < UINT16_MAX and
                0 <= self.y < UINT16_MAX and
                self.z <= MAP_MAX_Z)

    def is_valid(self) -> bool:
        return not (self.x == UINT16_MAX and self.y == UINT16_MAX and self.z == UINT8_MAX)

    def is_in_range(self, pos: Position, x_range: int, y_range: int,
                    ignore_z: bool = False) -> bool:
        if not ignore_z and self.z != pos.z:
            return False
        return abs(self.x - pos.x) <= x_range and abs(self.y - pos.y) <= y_range

    @staticmethod
    def get_angle_from_positions(from_pos: Position, to_pos: Position) -> float:
        """
        static double Position::getAngleFromPositions(...)
        Returns angle in radians [0, 2π). Returns -1 if positions are equal.
        """
        dx = to_pos.x - from_pos.x
        dy = to_pos.y - from_pos.y
        if dx == 0 and dy == 0:
            return -1.0
        angle = math.atan2(-dy, dx)
        if angle < 0:
            angle += 2 * math.pi
        return angle

    @staticmethod
    def get_direction_from_positions(from_pos: Position,
                                     to_pos: Position) -> Direction:
        """
        static Otc::Direction Position::getDirectionFromPositions(...)
        Converts angle between two positions into the nearest 8-way direction.
        """
        angle = Position.get_angle_from_positions(from_pos, to_pos) * RAD_TO_DEC

        if angle >= 360 - 22.5 or angle < 22.5:
            return Direction.East
        if 22.5 <= angle < 67.5:
            return Direction.NorthEast
        if 67.5 <= angle < 112.5:
            return Direction.North
        if 112.5 <= angle < 157.5:
            return Direction.NorthWest
        if 157.5 <= angle < 202.5:
            return Direction.West
        if 202.5 <= angle < 247.5:
            return Direction.SouthWest
        if 247.5 <= angle < 292.5:
            return Direction.South
        if 292.5 <= angle < 337.5:
            return Direction.SouthEast
        return Direction.InvalidDirection

    def get_direction_from_position(self, pos: Position) -> Direction:
        return Position.get_direction_from_positions(self, pos)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return NotImplemented
        return self.x == other.x and self.y == other.y and self.z == other.z

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.z))

    def __repr__(self) -> str:
        return f"Position({self.x}, {self.y}, {self.z})"


# ---------------------------------------------------------------------------
# InputMessage (src/framework/net/inputmessage.h)
# ---------------------------------------------------------------------------

class InputMessage:
    """
    Minimal subset of InputMessage that parseCreatureMove and helpers need:
    get_u8, get_u16, get_u32, get_string.
    Data is read from a bytes-like buffer using little-endian format.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._read_pos = 0

    def get_u8(self) -> int:
        value, = struct.unpack_from("<B", self._data, self._read_pos)
        self._read_pos += 1
        return value

    def get_u16(self) -> int:
        value, = struct.unpack_from("<H", self._data, self._read_pos)
        self._read_pos += 2
        return value

    def get_u32(self) -> int:
        value, = struct.unpack_from("<I", self._data, self._read_pos)
        self._read_pos += 4
        return value

    def get_string(self) -> str:
        length = self.get_u16()
        raw = self._data[self._read_pos:self._read_pos + length]
        self._read_pos += length
        return raw.decode("latin-1")

    # peek helpers (used by getMappedThing to read x without consuming)
    def peek_u16(self) -> int:
        value, = struct.unpack_from("<H", self._data, self._read_pos)
        return value


# ---------------------------------------------------------------------------
# Thing (src/client/thing.h + thing.cpp)  – base class
# ---------------------------------------------------------------------------

class Thing:
    """
    Base class for all map objects (items, creatures, effects, missiles).

    Relevant C++ source:
        src/client/thing.h
        src/client/thing.cpp
    """

    def __init__(self) -> None:
        self._position: Position = Position()
        self._stack_pos: int = -1
        self._client_id: int = 0

    # -- identity helpers (overridden in subclasses) -------------------------
    def get_id(self) -> int:
        return self._client_id

    def is_item(self) -> bool:
        return False

    def is_effect(self) -> bool:
        return False

    def is_missile(self) -> bool:
        return False

    def is_creature(self) -> bool:
        return False

    def get_stack_priority(self) -> int:
        """Returns ordering priority for tile stacking (0=ground … 5=item)."""
        return 5

    def has_elevation(self) -> bool:
        return False

    # -- position ------------------------------------------------------------

    def get_position(self) -> Position:
        return self._position

    def get_server_position(self) -> Position:
        return self._position

    def set_position(self, position: Position, stack_pos: int = 0) -> None:
        """
        void Thing::setPosition(const Position& position, uint8_t stackPos)
        """
        if self._position == position:
            return
        old_pos = Position(self._position.x, self._position.y, self._position.z)
        self._position = Position(position.x, position.y, position.z)
        self.on_position_change(position, old_pos)

    def on_position_change(self, new_pos: Position, old_pos: Position) -> None:
        """Hook – override in subclasses."""

    # -- tile reference ------------------------------------------------------

    def get_tile(self) -> Optional["Tile"]:
        """
        const TilePtr& Thing::getTile()
        { return g_map.getTile(m_position); }
        """
        return g_map.get_tile(self._position)

    # -- stack position ------------------------------------------------------

    def get_stack_pos(self) -> int:
        """
        int Thing::getStackPos()
        """
        if self._position.x == UINT16_MAX and self.is_item():
            return self._position.z
        if self._stack_pos >= 0:
            return self._stack_pos
        print("traceError: got a thing with invalid stackpos")
        return -1

    # -- lifecycle hooks (overridden in subclasses) --------------------------

    def on_appear(self) -> None:
        pass

    def on_disappear(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Creature (src/client/creature.h + creature.cpp)
# ---------------------------------------------------------------------------

class Creature(Thing):
    """
    Represents any living entity on the map (player, monster, NPC).

    Translated methods:
        allowAppearWalk()
        walk(oldPos, newPos)
        stopWalk()
        terminateWalk()
        onAppear()
        onPositionChange(newPos, oldPos)
    """

    def __init__(self) -> None:
        super().__init__()
        self._id: int = 0
        self._name: str = ""

        # walk state
        self._walking: bool = False
        self._walked_pixels: int = 0
        self._walk_offset_x: int = 0
        self._walk_offset_y: int = 0
        self._walk_turn_direction: Direction = Direction.InvalidDirection
        self._walk_finish_anim_scheduled: bool = False
        self._walk_animation_phase: int = 0

        # last step info
        self._last_step_direction: Direction = Direction.InvalidDirection
        self._last_step_from_position: Position = Position()
        self._last_step_to_position: Position = Position()

        # appear / walk flag set by allowAppearWalk()
        self._allow_appear_walk: bool = False

        # old position (updated on tile removal)
        self._old_position: Position = Position()

        # lifecycle
        self._removed: bool = True

    # -- identity ------------------------------------------------------------

    def is_creature(self) -> bool:
        return True

    def get_stack_priority(self) -> int:
        return 4

    def get_id(self) -> int:
        return self._id

    def set_id(self, creature_id: int) -> None:
        self._id = creature_id

    def set_name(self, name: str) -> None:
        self._name = name

    # -- walk API ------------------------------------------------------------

    def allow_appear_walk(self) -> None:
        """void Creature::allowAppearWalk() { m_allowAppearWalk = true; }"""
        self._allow_appear_walk = True

    def walk(self, old_pos: Position, new_pos: Position) -> None:
        """
        void Creature::walk(const Position& oldPos, const Position& newPos)

        Starts the smooth walking animation from oldPos to newPos.
        """
        if old_pos == new_pos:
            return

        self._last_step_direction = old_pos.get_direction_from_position(new_pos)
        self._last_step_from_position = old_pos
        self._last_step_to_position = new_pos

        self._set_direction(self._last_step_direction)

        self._walking = True
        self._walked_pixels = 0
        self._walk_turn_direction = Direction.InvalidDirection

        # Cancel any previously scheduled finish animation
        self._walk_finish_anim_scheduled = False

        # In the real client this schedules nextWalkUpdate() via the
        # dispatcher; here we just record that a walk has started.
        self._next_walk_update()

    def stop_walk(self) -> None:
        """
        void Creature::stopWalk()
        """
        if not self._walking:
            return
        self._terminate_walk()

    def _terminate_walk(self) -> None:
        """
        void Creature::terminateWalk()

        Immediately stops all walking state and schedules phase reset.
        """
        # cancel pending walk-update event (represented as a flag here)

        if self._walk_turn_direction != Direction.InvalidDirection:
            self._set_direction(self._walk_turn_direction)
            self._walk_turn_direction = Direction.InvalidDirection

        # remove from walking tile
        self._walk_offset_x = 0
        self._walk_offset_y = 0
        self._walked_pixels = 0
        self._walking = False

        # schedule animation phase reset (in real client uses g_dispatcher)
        self._walk_finish_anim_scheduled = True  # marks "will reset to phase 0"

    def _next_walk_update(self) -> None:
        """
        void Creature::nextWalkUpdate()

        In the real client this updates pixel offsets each game tick.
        Stub: real implementation uses a scheduled event loop.
        """

    def _set_direction(self, direction: Direction) -> None:
        """Sets creature's facing direction."""
        self._direction = direction

    # -- lifecycle -----------------------------------------------------------

    def on_position_change(self, new_pos: Position, old_pos: Position) -> None:
        """Stores old position before the move."""
        self._old_position = old_pos

    def on_appear(self) -> None:
        """
        void Creature::onAppear()

        Called by Tile::addThing() after the creature is placed on the new tile.
        Decides whether this is a regular walk, a teleport, or first appearance.
        """
        # creature appeared for the first time or came back after being gone
        if self._removed:
            self.stop_walk()
            self._removed = False
            self._on_appear_callback()

        # adjacent move with walk flag set → smooth walk animation
        elif (self._old_position != self._position and
              self._old_position.is_in_range(self._position, 1, 1) and
              self._allow_appear_walk):
            self._allow_appear_walk = False
            self.walk(self._old_position, self._position)
            self._on_walk_callback(self._old_position, self._position)

        # non-adjacent position change → teleport
        elif self._old_position != self._position:
            self.stop_walk()
            self._on_disappear_callback()
            self._on_appear_callback()

        # else: same position, just a direction turn – no special action

    def on_disappear(self) -> None:
        """Called by Tile::removeThing() when the creature leaves a tile."""
        # In the real client this may schedule a disappear event; stub here.
        self._on_disappear_scheduled()

    # -- Lua callbacks (stubs) -----------------------------------------------

    def _on_appear_callback(self) -> None:
        """callLuaField("onAppear")"""

    def _on_disappear_callback(self) -> None:
        """callLuaField("onDisappear")"""

    def _on_walk_callback(self, old_pos: Position, new_pos: Position) -> None:
        """callLuaField("onWalk", oldPos, newPos)"""

    def _on_disappear_scheduled(self) -> None:
        """Schedules disappear handling (real client uses dispatcher)."""


# ---------------------------------------------------------------------------
# TileBlock (src/client/map.h – inner struct)
# ---------------------------------------------------------------------------

class TileBlock:
    """
    struct TileBlock

    A fixed-size block of BLOCK_SIZE×BLOCK_SIZE tiles used to partition the map.
    Tiles within the block are stored in a flat array indexed by local (x%B, y%B).
    """

    def __init__(self) -> None:
        self._tiles: dict[int, "Tile"] = {}

    def _get_index(self, pos: Position) -> int:
        return (pos.y % BLOCK_SIZE) * BLOCK_SIZE + (pos.x % BLOCK_SIZE)

    def get(self, pos: Position) -> Optional["Tile"]:
        return self._tiles.get(self._get_index(pos))

    def get_or_create(self, pos: Position) -> "Tile":
        idx = self._get_index(pos)
        if idx not in self._tiles:
            self._tiles[idx] = Tile(Position(pos.x, pos.y, pos.z))
        return self._tiles[idx]

    def create(self, pos: Position) -> "Tile":
        idx = self._get_index(pos)
        self._tiles[idx] = Tile(Position(pos.x, pos.y, pos.z))
        return self._tiles[idx]


# ---------------------------------------------------------------------------
# Tile (src/client/tile.h + tile.cpp)
# ---------------------------------------------------------------------------

class Tile:
    """
    Represents a single map cell.

    Translated methods:
        addThing(thing, stackPos)
        removeThing(thing)
        getThing(stackPos)
        getThingStackPos(thing)
    """

    TILE_MAX_THINGS = 10  # g_gameConfig.getTileMaxThings() default

    def __init__(self, position: Position) -> None:
        self._position: Position = position
        self._things: list[Thing] = []
        self._effects: list[Thing] = []
        self._draw_elevation: int = 0

    def get_position(self) -> Position:
        return self._position

    def get_ground(self) -> Optional[Thing]:
        """Returns ground item (stack priority 0), or None."""
        for t in self._things:
            if t.get_stack_priority() == 0:
                return t
        return None

    # -- add_thing -----------------------------------------------------------

    def add_thing(self, thing: Thing, stack_pos: int = -1) -> None:
        """
        void Tile::addThing(const ThingPtr& thing, int stackPos)

        Inserts *thing* into the tile's thing list at the correct position
        according to stack priority and stackPos hint.

        Priority order (ascending = bottom of list):
            0 – ground
            1 – ground borders
            2 – bottom / walls
            3 – on top / doors
            4 – creatures  (newer clients: append; older: prepend within group)
            5 – items      (newest last)
        """
        if thing is None:
            return

        if thing.is_effect():
            self._effects.append(thing)
            thing.set_position(self._position)
            thing.on_appear()
            return

        size = len(self._things)
        priority = thing.get_stack_priority()

        if stack_pos < 0 or stack_pos == 255:
            # auto-detect: priorities ≤3 append (ground/borders/walls/ontop),
            # creatures (4) and items (5) prepend within their group.
            # For client version ≥ 854 creatures are stored appended too.
            append = priority <= 3
            # Assume modern client (≥854): creatures also append
            if priority == 4:
                append = not append  # flip: creatures append in modern clients

            for stack_pos in range(size):
                other_priority = self._things[stack_pos].get_stack_priority()
                if append and other_priority > priority:
                    break
                if not append and other_priority >= priority:
                    break
            else:
                stack_pos = size
        elif stack_pos > size:
            stack_pos = size

        self._things.insert(stack_pos, thing)
        self._update_thing_stack_pos()

        # enforce max things limit
        if len(self._things) > self.TILE_MAX_THINGS:
            self.remove_thing(self._things[self.TILE_MAX_THINGS])

        thing.set_position(self._position, stack_pos)
        thing.on_appear()

        # callLuaField("onAddThing", thing) – omitted

    # -- remove_thing --------------------------------------------------------

    def remove_thing(self, thing: Thing) -> bool:
        """
        bool Tile::removeThing(const ThingPtr thing)
        """
        if thing is None:
            return False

        if thing.is_effect():
            try:
                self._effects.remove(thing)
                return True
            except ValueError:
                return False

        try:
            self._things.remove(thing)
        except ValueError:
            return False

        thing._stack_pos = -1

        self._update_thing_stack_pos()

        if thing.has_elevation():
            # recalculate elevation
            self._draw_elevation = 0

        thing.on_disappear()

        # callLuaField("onRemoveThing", thing) – omitted
        return True

    # -- get_thing -----------------------------------------------------------

    def get_thing(self, stack_pos: int) -> Optional[Thing]:
        """
        ThingPtr Tile::getThing(const int stackPos)
        """
        if 0 <= stack_pos < len(self._things):
            return self._things[stack_pos]
        return None

    # -- get_thing_stack_pos -------------------------------------------------

    def get_thing_stack_pos(self, thing: Thing) -> int:
        """
        int Tile::getThingStackPos(const ThingPtr& thing)
        """
        for i, t in enumerate(self._things):
            if t is thing:
                return i
        return -1

    # -- helpers -------------------------------------------------------------

    def _update_thing_stack_pos(self) -> None:
        for i, t in enumerate(self._things):
            t._stack_pos = i


# ---------------------------------------------------------------------------
# Map (src/client/map.h + map.cpp)
# ---------------------------------------------------------------------------

class MapFloor:
    def __init__(self) -> None:
        self.tile_blocks: dict[int, TileBlock] = {}
        self.missiles: list[Thing] = []


class Map:
    """
    Global map manager.

    Translated methods:
        addThing(thing, pos, stackPos)
        removeThing(thing)
        getThing(pos, stackPos)
        getTile(pos)
        getOrCreateTile(pos)
        getCreatureById(id)
        addCreature(creature)
        removeCreatureById(id)
        notificateTileUpdate(pos, thing, operation)
    """

    def __init__(self) -> None:
        self._floors: list[MapFloor] = [MapFloor() for _ in range(MAP_MAX_Z + 1)]
        self._known_creatures: dict[int, Creature] = {}
        self._map_views: list = []    # would hold MapView observers
        self._null_tile: Optional[Tile] = None

    # -- block index ---------------------------------------------------------

    @staticmethod
    def _get_block_index(pos: Position) -> int:
        """
        uint16_t Map::getBlockIndex(const Position& pos)
        { return ((pos.y / BLOCK_SIZE) * (65536 / BLOCK_SIZE)) + (pos.x / BLOCK_SIZE); }
        """
        return ((pos.y // BLOCK_SIZE) * (65536 // BLOCK_SIZE)) + (pos.x // BLOCK_SIZE)

    # -- tile access ---------------------------------------------------------

    def get_tile(self, pos: Position) -> Optional[Tile]:
        """
        const TilePtr& Map::getTile(const Position& pos)
        """
        if not pos.is_map_position():
            return None
        tile_blocks = self._floors[pos.z].tile_blocks
        block_index = self._get_block_index(pos)
        block = tile_blocks.get(block_index)
        if block is not None:
            return block.get(pos)
        return None

    def get_or_create_tile(self, pos: Position) -> Optional[Tile]:
        """
        const TilePtr& Map::getOrCreateTile(const Position& pos)
        """
        if not pos.is_map_position():
            return None
        block_index = self._get_block_index(pos)
        tile_blocks = self._floors[pos.z].tile_blocks
        if block_index not in tile_blocks:
            tile_blocks[block_index] = TileBlock()
        return tile_blocks[block_index].get_or_create(pos)

    # -- thing access --------------------------------------------------------

    def get_thing(self, pos: Position, stack_pos: int) -> Optional[Thing]:
        """
        ThingPtr Map::getThing(const Position& pos, const int16_t stackPos)
        """
        tile = self.get_tile(pos)
        if tile is not None:
            return tile.get_thing(stack_pos)
        return None

    # -- add_thing -----------------------------------------------------------

    def add_thing(self, thing: Thing, pos: Position, stack_pos: int = -1) -> None:
        """
        void Map::addThing(const ThingPtr& thing, const Position& pos,
                           const int16_t stackPos)
        """
        if thing is None:
            return
        if thing.is_item() and thing.get_id() == 0:
            return

        if thing.is_missile():
            self._floors[pos.z].missiles.append(thing)
            thing.set_position(pos)
            thing.on_appear()
            return

        tile = self.get_or_create_tile(pos)
        if tile is not None:
            tile.add_thing(thing, stack_pos)
            self.notificate_tile_update(pos, thing, Operation.ADD)

    # -- remove_thing --------------------------------------------------------

    def remove_thing(self, thing: Thing) -> bool:
        """
        bool Map::removeThing(const ThingPtr& thing)
        """
        if thing is None:
            return False

        if thing.is_missile():
            missiles = self._floors[thing.get_server_position().z].missiles
            try:
                missiles.remove(thing)
                return True
            except ValueError:
                return False

        tile = thing.get_tile()
        if tile is not None:
            if tile.remove_thing(thing):
                self.notificate_tile_update(
                    thing.get_server_position(), thing, Operation.REMOVE)
                return True

        return False

    # -- creature registry ---------------------------------------------------

    def get_creature_by_id(self, creature_id: int) -> Optional[Creature]:
        """
        CreaturePtr Map::getCreatureById(const uint32_t id)
        """
        return self._known_creatures.get(creature_id)

    def add_creature(self, creature: Creature) -> None:
        self._known_creatures[creature.get_id()] = creature

    def remove_creature_by_id(self, creature_id: int) -> None:
        self._known_creatures.pop(creature_id, None)

    # -- notifications -------------------------------------------------------

    def notificate_tile_update(self, pos: Position, thing: Thing,
                               operation: Operation) -> None:
        """
        void Map::notificateTileUpdate(const Position& pos, const ThingPtr& thing,
                                       const Otc::Operation operation)
        """
        if not pos.is_map_position():
            return
        for map_view in self._map_views:
            map_view.on_tile_update(pos, thing, operation)
        # g_minimap.updateTile – omitted


# ---------------------------------------------------------------------------
# Global map singleton (mirrors g_map in the C++ client)
# ---------------------------------------------------------------------------

g_map = Map()


# ---------------------------------------------------------------------------
# ProtocolGame – relevant methods only
# (src/client/protocolgameparse.cpp)
# ---------------------------------------------------------------------------

class ProtocolGame:
    """
    Subset of ProtocolGame that handles creature movement packets.

    Translated methods:
        parseCreatureMove(msg)
        getMappedThing(msg)       → Thing | None
        getPosition(msg)          → Position
    """

    def __init__(self) -> None:
        self._local_player: Optional[Creature] = None

    # -- parseCreatureMove ---------------------------------------------------

    def parse_creature_move(self, msg: InputMessage) -> None:
        """
        void ProtocolGame::parseCreatureMove(const InputMessagePtr& msg)

        Reads a creature-move packet and moves the creature on the map.

        Packet layout (two variants determined by getMappedThing):
          Variant A – thing at position:
            U16 x, U16 y, U8 z, U8 stackpos
          Variant B – creature by id:
            U16 0xffff, U32 creatureId
          Followed by:
            U16 newX, U16 newY, U8 newZ
        """
        thing = self.get_mapped_thing(msg)
        new_pos = self.get_position(msg)

        if thing is None or not thing.is_creature():
            print("traceError: ProtocolGame::parseCreatureMove: "
                  "no creature found to move")
            return

        if not g_map.remove_thing(thing):
            print("traceError: ProtocolGame::parseCreatureMove: "
                  "unable to remove creature")
            return

        creature: Creature = thing  # type: ignore[assignment]
        creature.allow_appear_walk()

        g_map.add_thing(thing, new_pos, -1)

    # -- getMappedThing ------------------------------------------------------

    def get_mapped_thing(self, msg: InputMessage) -> Optional[Thing]:
        """
        ThingPtr ProtocolGame::getMappedThing(const InputMessagePtr& msg) const

        Decodes either a positional reference or a creature-id reference and
        returns the corresponding Thing from the map.

        Positional:    U16 x (≠ 0xffff), U16 y, U8 z, U8 stackpos
        Creature-id:   U16 0xffff,       U32 creatureId
        """
        x = msg.get_u16()
        if x != 0xffff:
            y = msg.get_u16()
            z = msg.get_u8()
            stack_pos = msg.get_u8()

            assert stack_pos != UINT8_MAX, "invalid stackpos in getMappedThing"

            pos = Position(x, y, z)
            thing = g_map.get_thing(pos, stack_pos)
            if thing is not None:
                return thing

            print(f"traceError: no thing at pos:{pos}, stackpos:{stack_pos}")
        else:
            creature_id = msg.get_u32()
            thing = g_map.get_creature_by_id(creature_id)
            if thing is not None:
                return thing

            print(f"traceError: ProtocolGame::getMappedThing: "
                  f"no creature with id {creature_id}")

        return None

    # -- getPosition ---------------------------------------------------------

    @staticmethod
    def get_position(msg: InputMessage) -> Position:
        """
        Position ProtocolGame::getPosition(const InputMessagePtr& msg)
        """
        x = msg.get_u16()
        y = msg.get_u16()
        z = msg.get_u8()
        return Position(x, y, z)


# ---------------------------------------------------------------------------
# Example / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Build a minimal map state:
    #   creature at (100, 100, 7) stackpos 0
    # Send a move packet to (101, 100, 7)

    creature = Creature()
    creature.set_id(1234)
    creature.set_name("Test Creature")
    creature._removed = False  # already known to the client

    start_pos = Position(100, 100, 7)
    g_map.add_creature(creature)
    g_map.add_thing(creature, start_pos, -1)

    # Simulate: creature starts at (100,100,7) and moves to (101,100,7)
    # Packet layout (positional variant):
    #   U16 x=100, U16 y=100, U8 z=7, U8 stackpos=0
    #   U16 newX=101, U16 newY=100, U8 newZ=7

    import io
    buf = struct.pack("<HHBBHHB", 100, 100, 7, 0, 101, 100, 7)
    msg = InputMessage(buf)

    protocol = ProtocolGame()
    protocol.parse_creature_move(msg)

    final_tile = g_map.get_tile(Position(101, 100, 7))
    assert final_tile is not None
    assert final_tile.get_thing(0) is creature
    assert creature._position == Position(101, 100, 7)
    assert creature._walking  # walk animation was started
    print("OK: creature moved from (100,100,7) to (101,100,7) with walk animation")
