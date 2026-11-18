#!/usr/bin/env python3
"""Port Skate 2's single-player Own the Spot goals into a New San Van goals package.

Dev-only data pipeline (reports/log264_skate2_ots_port.md has the evidence).
Reads the user's original Skate 2 data and Skate 3's own data, both read only,
and installs a separate DLC-style content package next to the New San Van menu
package (tools/build_nsv_dlc.py), laid out like the retail Maloof Money Cup
DLC:

  data/db/dlc_nsvgoals.vaultlist.xml       lists the two vaults below
  data/db/dlc_nsvgoals_local_data_framework challenge_local_data group records
  data/db/challengebanks/dlc_nsvgoals       challenges, challenge_global_data,
                                            challenge_objective(s_group) records
  data/db/challenge_local_data/<goal>.vlt   per-goal local data (loaded on demand)
  data/content/missions/<goal>/             the goal's Skate 2 mission hull
                                            (cSim_Global.xsf + catalogs, renamed)
  data/stream/<goal>.xml, data/boundary/<goal>.xml
  data/content/global_locators/BAM/DIST_DownTown/*.rx2  Skate 2 locator arenas
  data/scene/scene_050.bin, ice_050.bin      Skate 3's newest scene table and ICE
                                            database (base or DLC, as the game picks
                                            them) plus each goal's Skate 2 intro
                                            cutscene, appended (skate2_goal_intros.py)
  data/scene/anim/OTS_*.abin                 Skate 2 animations two intros use
  data/audio/english/*.sns, nsvgoals_resident.big   the intros' lines (Reda and
                                            post-produced effects) and their headers,
                                            registered by aud_bigfiles/nsv_goals_audio
  data/fe/languages/<lang>/LANGUAGE_<Lang>_nsvgoals_skate3ng-dlc_nsvgoals.BIN

Record structure (retail DLC convention, see Maloof's challengebanks/dlc_mmcn):
a self-contained bank re-exports the three 'default' records, a group root
'nsv_goals' (DLC content id 0 = base game) and a type group
'nsv_goals_own_the_spots' cloned from Skate 3's own_the_spots (state graph
challenge_stategraph/ownthespot_shared_mlui). Each goal clones Skate 3's
ots_dwtn_01 family (challenges, challenge_global_data, objectives groups
<goal>, <goal>_0 = own it, <goal>_1 = killed it, challenge_objective <goal>,
<goal>_0, challenge_local_data <goal>) and substitutes the Skate 2 goal's
values. World is the Downtown template (world/dist_downtown). Each goal's
intro event names its Skate 2 intro cutscene, which plays with Reda's line and
its Skate 2 subtitle. Skate 2 Lua, the killed-it/outro cutscenes and
screenshot references are not carried over.

NAMING CONTRACT (src/ depends on it): every collection this package adds is
named nsv_*: nsv_goals, nsv_goals_own_the_spots and nsv_ots_<number>_<spot>
(+ _0/_1 for objectives), e.g. f_1a_01_ots_pj01_books_sp -> nsv_ots_01_pj01_books
(goal_name(); hull folder names must stay within 30 characters).

Staging (mods/skate2/goals-v1): manifest.json, goals.txt (collection keys for
the runtime), loose/ (the package files as loose data) and GOALS_COMPLETE_V1.

Usage (repository root):
  python tools/build_skate2_goals.py [--root out/build/relwithdebinfo]
         [--source "../Skate 2/data"] [--native-data <root>/game/data] [--uninstall]
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import mmap
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_nsv_dlc as dlc                        # noqa: E402  verified vault/label/EB3 writers
import skate2_goal_intros as intros                # noqa: E402  ICE scenes, resident archives
import skate2_teleports as teleports              # noqa: E402  teleport trigger hulls, locator moves
from attrib_vault import Vault as VaultReader     # noqa: E402
from inspect_sfil import assets, dictionary       # noqa: E402
from vlt64 import h64                             # noqa: E402
from world_archive import decode_archive_payload, entries  # noqa: E402

REPO = HERE.parent
PACK = 'nsvgoals'
PACKAGE_ID = b'NSVMOD-GOALS-SK2-OTS'                  # 20 bytes -> 42-char folder like retail
PACKAGE_DIR = PACKAGE_ID.hex().upper() + '45'
DISPLAY_NAME = 'New San Van Goals (Skate 2)'
VERSION_FILE = b'692631:sk8build'
STAGE = 'goals-v1'
MARKER = 'GOALS_COMPLETE_V1'
ROOT = 'nsv_goals'
TYPE_GROUP = 'nsv_goals_own_the_spots'
PREFIX = 'nsv_ots_'
HULL_NAME_MAX = 30
BANK_VAULT = 'dlc_nsvgoals'                           # data/db/challengebanks/
FRAMEWORK_VAULT = 'dlc_nsvgoals_local_data_framework'
VAULTLIST = f'data/db/dlc_{PACK}.vaultlist.xml'
LOCATOR_DIR = 'data/content/global_locators/BAM/DIST_DownTown'
WORLD = 'dist_downtown'                               # NSV's Downtown template world
WORLD_KEY = 0x4773016A41B2E05E
DLC_CONTENT_ID = 0                                    # 1 << id is the content mask; 0 = base game
# Vers stamps of the retail Maloof vaults with the same role.
STAMP_BANK = 0xF28330A780B82E96
STAMP_FRAMEWORK = 0x3B58AE2DDE869217
S2_GROUP = 'ownthespot_shared_mlui'                   # Skate 2 single-player OTS parent
S2_ONLINE_GROUP = 'spot_battle'
# Intro cutscenes (skate2_goal_intros.py): merged scene table and ICE database numbered above
# every retail file (Play DLC = 006), Reda's lines registered like Maloof's (aud_bigfiles).
SCENE_NUMBER = 50
SCENE_FILE = f'data/scene/scene_{SCENE_NUMBER:03d}.bin'
ICE_FILE = f'data/scene/ice_{SCENE_NUMBER:03d}.bin'
AUDIO_DIR = 'data/audio/english'
AUDIO_FOLDER = AUDIO_DIR + '/'                         # aud_bigfiles folder, as Maloof's
RESIDENT = f'{PACK}_resident.big'                  # loose in AUDIO_DIR, like dlc_mmc_resident.big
AUDIO_COLLECTION = 'nsv_goals_audio'
# Retail DLC bank layout (Maloof, Danny Way): loose <NAME>.sns streams and a resident archive of their
# .snr headers in data/audio/english/, record flagged DLC so the boot loader (sub_824A47F8) skips it.
# A world's DLC bank loads through its aud_worlddata (sub_824A92D8): resident = folder + resident name
# through the package-aware loader, then sub_824A5A08(..., folder, 1). New San Van borrows Downtown's
# world record, so src/ loads this record the same way when New San Van loads. Log 348: a base-style
# bank (an archive inside this package) crashed: base banks are opened from the disc. Retail slots:
# nis 0, post 1, DLC 4 to 6; 3 is unused.
AUDIO_SLOT = 3
REDA = 40                                             # Skate 2 speaker id of Reda's cutscene lines
A_AUD_FOLDER = 0x16CEC9E052A8B9BA                     # aud_bigfiles: English archive or DLC folder
A_AUD_FRENCH = 0x08CD09BE8454C028                     # French archive (base banks only)
A_AUD_GERMAN = 0x533AB8B141F52ECB                     # German archive (base banks only)
A_AUD_RESIDENT = 0x12DFB2BDD9356640                   # resident archive of .snr headers
A_AUD_RESIDENT_B = 0x1179AE4EB7022F99                 # resident name again (retail DLC sets all three)
A_AUD_RESIDENT_C = 0xCBDF09A8387553E2
A_AUD_DLC = 0xDAB4F65F7D523075                        # bool, set by every retail DLC record
A_AUD_RESIDENT_BANK = 0x83A22DD9978C45DC              # bool, set by nis/post/DLC (resident banks)
A_AUD_SLOT = 0x3C336B8972F26C0F                       # Int32 slot
# In-game teleport markers (skate2_teleports.py, reports/log268_nsv_teleports.md): Skate 3's base
# 'teleports' challenge group (ChallengeType 45), one child per marker. Each marker's trigger
# volume comes from a Skate 2 online-only hull with one trigger volume, moved to a 3 m box at
# the start; its start and exit locators are unused Skate 2 locator groups (no child records)
# in the packaged Waterfront arena, moved to the points below. Positions are Skate 2 / New San
# Van world coordinates (x, y, z); facing is an (x, z) direction.
TELEPORT_GROUP = 'nsv_teleports'
TELEPORT_ARENA = 'DIST_Waterfr_[0xe727c0a5617e429c].rx2'
A_TELEPORT_TRIGGER = 0xDA9330F647B19DF2               # TeleporterTrigger (tTriggerVolumeInstanceID)
A_TELEPORT_EXIT = 0xE7D448870EC1A6E9                  # TeleportExitLocation (tWorldLocationID array)
# Global data bool (name unknown; 'default' true, the 'teleports' and goal groups false). True makes
# the challenge manager skip a challenge when it registers it (sub_82BCF538: no listeners, no Global
# list) and drops it from challenge lists (sub_82562C78). Skate 3's own Downtown teleport
# (tele_dwtn_to_dwtnskpk: Location, hull, Title, World only) inherits false from 'teleports' and its
# hull loads with Downtown (log 348); markers here inherit the same (round 270 set it true: never
# registered). src/ keeps every package challenge off maps other than New San Van (goals.txt keys).
A_FREESKATE_ACTIVE = 0xFD40E4A31027C984
A_IGNORE_PREREQ = 0x136812614CD4BCC7                 # IgnorePrereqForGlobalStart (bool)
BOOL_TRUE = 0x01000000                                # inline EA::Reflection::Bool: first byte (default's FD40)
# No ship marker (user, log 345): the Poop Deck Pool goal already takes you onto the deck.
TELEPORTS = (
    dict(name='nsv_tp_lighthouse', source_hull='ol_fa_planb_04_accumulation_gr',
         title_english='Lighthouse Lookout', title_skate2=None,
         # The tower across from the lighthouse, NOT the lighthouse (it has stairs). User, log 347
         # Ctrl+Insert capture: skater at (1003.7, -56.5, 880.4) facing +z, "directly above where I am
         # standing, facing the same direction". Collision: ground -57.2, platform top -48.69
         # (underside -48.94) spanning x +-3 m and z -1.5..+4 m, steep roof from about -45.
         # Unused Skate 2 online Hall of Meat groups WITH child records (moved along), like Skate 3's
         # teleport_dwtn_01_toDwtnskpk_locator_01 (::spawn_01); the childless ones never streamed in.
         start_locator='ol_hom_02_wf03_spiresteps_challengelocator', start=(1003.7, -57.15, 880.4),
         start_facing=(0.0, 1.0),
         exit_locator='ol_hom_04_wf03_rockdrop_challengelocator', exit=(1003.7, -48.64, 881.2),
         exit_facing=(0.0, 1.0)),
)
TELEPORT_BOX = (1.5, 3.0)                             # trigger half width, height
EXCLUDE = {
    'ots_test01': 'Skate 2 test goal (borrows test_spot_photo01 locators; no world data of its own)',
    'f_1a_25_ots_dlc01_load': 'Skate 2 DLC goal (Community Centre map); its locator and hull are not '
                              'in the base Skate 2 data',
}
# Online props hulls left out: Skate 2 online places them without the world's own movable objects,
# and these two would sit on top of one in New San Van (distances from the installed map's cells).
PROPS_SKIP = {
    'f_1a_09_ots_ur06_curves_props': 'its dumpster is 1.0 m from the world dumpster it stands in for',
    'f_1a_05_ots_pj05_fountain_prop': 'its rail is 1.5 m from a world bench',
}

# Classes
CH, GD, LD = h64('challenges'), h64('challenge_global_data'), h64('challenge_local_data')
OBJ, OG, SG = h64('challenge_objective'), h64('challenge_objectives_group'), h64('challenge_stategraph')
CLASS_NAMES = {CH: 'challenges', GD: 'challenge_global_data', LD: 'challenge_local_data',
               OBJ: 'challenge_objective', OG: 'challenge_objectives_group', SG: 'challenge_stategraph'}
assert GD == 0xA4F35B159D7432B5

# Attributes without a known name (hashes observed in retail records).
A_GLOBAL_REF = 0x24491BF00316AA45      # challenges -> challenge_global_data (RefSpec)
A_LOCAL_REF = 0xD7BB2B160CAF8ED6       # challenges -> challenge_local_data (tVaultedRefSpec)
A_CONTENT_ID = 0x8C5669E5A6864831      # challenges group root: DLC content id (UInt64)
A_REWARD = 0x2E4824C81FDAE87C          # global: reward amount (UInt32, = own-it reward)
A_OWN_REWARD = 0x67699C4B88712759      # global: own-it reward (tObservatoryProgressionReward)
A_KILL_REWARD = 0x047ED7FFBB024AB8     # global: killed-it reward (4x own-it in retail)
A_KILL_GROUP = 0x3677267F3AECF0C8      # global: killed-it objectives group (higher score)
A_OWN_GROUP = 0xBE84E2494E2FC3AA       # global: own-it objectives group
A_HULLS = 0x876BF0819AA58858           # global: required challenge hulls (mission collections)
A_SCORE_VOLUME = 0x39BFD2E87C39C3CE    # local: scoring volume (= OTSScoringBoundary in Skate 3)
A_FLOAT_8022 = 0x8022BA64CB8E431D      # Skate 2 challenges / Skate 3 global data float (10 by default)
A_LOCAL_9DDC = 0x9DDC567540219ACC      # local main bool; Maloof keeps it on its OTS type group
A_LOCAL_C961 = 0xC961A236FC566532      # local main text (team indication message)
A_LOCAL_5A37 = 0x5A3736171601C40C      # local main killed-it effect
A_ACTIVATION = 0xD7332063BE5CC0EB      # objective trigger activation mode

# Fixed sizes of non-array data blobs (each checked against its retail span in Source.entry_value).
TYPE_SIZE = {
    'Attrib::RefSpec': 24, 'AttribSysUtils::tVaultedRefSpec': 32, 'EA::Reflection::UInt64': 8,
    'Sk8::Challenge::tTriggerVolumeInstanceID': 16, 'Observatory::tObservatoryProgressionReward': 16,
    'Observatory::tObservatoryProgressionRewardReturns': 24, 'Sk8::Challenge::tChallengeIconDefinition': 24,
    'Sk8::Challenge::tObjectiveDefinition': 20, 'Sk8::Challenge::tObjectiveTriggers': 20,
    'LuaState::tCompiledLua': 12, 'Sk8::Challenge::tHighlightDefinition': 144,
    'Sk8::tNISPlaybackDefinition': 64, 'Sk8::Challenge::tKilledItEffect': 16, 'Math::Vector3': 16,
    'Sk8::Challenge::tObjectiveFlow': 8, 'Sk8::Challenge::tSpeechInfo': 8,
}
CLASS_REFSPEC_TYPES = ('world', 'map_category', 'livingworld_categorygroups', 'progression_group',
                       'fe_map_icons', 'progression_stategraph', 'progression_state', 'progression_handler')
TYPE_LIST = list(TYPE_SIZE) + [f'Attrib::Gen::ClassRefSpec_{c}' for c in CLASS_REFSPEC_TYPES] + [
    'EA::Reflection::Text', 'EA::Reflection::Bool', 'EA::Reflection::Int32', 'EA::Reflection::UInt32',
    'EA::Reflection::Float', 'EA::Reflection::UInt8', 'EA::Reflection::Int16', 'Sk8::Audio::eSk8Characters',
    'Sk8::Challenge::tLocationID', 'Sk8::Challenge::tRequiredChallengeHull', 'Sk8::Challenge::tVisualEditorData',
    'Sk8::Challenge::tChallengePresentationEvent', 'Sk8::Challenge::tDMOResetInfo', 'Sk8::FE::tFEScreenShot',
    'Sk8::Challenge::tCompetitorInfo', 'Sk8::Challenge::tGlobalSpawnCharacterInfo',
    'Sk8::Challenge::eChallengeTypes', 'Sk8::HUD::eHUDComponent']
T = {name: h64(name) for name in TYPE_LIST}
TYPE_NAMES = {k: v for v, k in T.items()}


def align(n, a):
    return (n + a - 1) & ~(a - 1)


# ---- archives ----------------------------------------------------------------------
def read_archive(path, want=lambda name: True):
    """Decoded payloads of a BIG4 or retail-family EB3 archive, by path."""
    with Path(path).open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        if raw[:4] == b'EB\0\3' and struct.unpack_from('>I', raw, 8)[0] == 0x00500600:
            return read_dlc_archive(bytes(raw), want)
        return {e.name: decode_archive_payload(bytes(raw[e.offset:e.offset + e.size]))
                for e in entries(raw) if want(e.name)}


def read_archive_raw(path, want):
    """Undecoded payloads of a BIG4 archive (for entries decode_archive_payload cannot read)."""
    with Path(path).open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        return {e.name: bytes(raw[e.offset:e.offset + e.size]) for e in entries(raw) if want(e.name)}


def read_dlc_archive(data, want=lambda name: True):
    """Strict reader for the DLC EB3 family 0x00500600 written by build_nsv_dlc.eb3_archive.

    Every entry's name and hash order are checked; only wanted payloads are decoded.
    """
    magic, count, family, names_at, names_size, meta, _, total, _ = struct.unpack_from('>4s8I', data, 0)
    if magic != b'EB\0\3' or family != 0x00500600 or total != len(data):
        raise ValueError('not a DLC EB3 archive')
    width, dwidth, ndirs = meta >> 24, (meta >> 16) & 255, meta & 0xFFFF
    dirs_at = align(names_at + count * width, 16)
    dirs = [data[dirs_at + i * dwidth:dirs_at + (i + 1) * dwidth].split(b'\0', 1)[0].decode('ascii')
            for i in range(ndirs)]
    out, previous = {}, -1
    for i in range(count):
        block, stored, size, hashed = struct.unpack_from('>4I', data, 48 + 16 * i)
        slot = data[names_at + i * width:names_at + (i + 1) * width]
        name = dirs[struct.unpack_from('>H', slot)[0]] + '/' + slot[2:].split(b'\0', 1)[0].decode('ascii')
        if hashed != dlc.path_hash(name) or hashed <= previous:
            raise ValueError(f'DLC archive hash mismatch: {name}')
        previous = hashed
        if want(name):
            payload = data[block << 6:(block << 6) + (stored or size)]
            out[name] = intros.decode_payload(payload) if stored else payload
    return out


# ---- RW4 locators and trigger volumes -------------------------------------------------
def _cstr(data, at):
    return data[at:data.index(b'\0', at)].decode('latin1')


def eb0009_locators(arena):
    """LOCATIONDESCDATA groups and records: [(kind, name, parent, 16 floats)]."""
    out = []
    for ent in dictionary(arena):
        if ent['kind'] != 0xEB0009:
            continue
        o = ent['pointer']
        groups, records = struct.unpack_from('>II', arena, o)
        g0 = o + 0x20
        r0 = g0 + groups * 0x80
        for i in range(groups):
            g = g0 + i * 0x80
            out.append(('G', _cstr(arena, o + struct.unpack_from('>I', arena, g + 0x68)[0]),
                        _cstr(arena, o + struct.unpack_from('>I', arena, g + 0x6C)[0]),
                        struct.unpack_from('>16f', arena, g)))
        for i in range(records):
            r = r0 + i * 0x70
            out.append(('R', _cstr(arena, o + struct.unpack_from('>I', arena, r + 0x60)[0]),
                        _cstr(arena, o + struct.unpack_from('>I', arena, r + 0x64)[0]),
                        struct.unpack_from('>16f', arena, r)))
    return out


def eb0019_volumes(arena):
    """Trigger volume instance ids (magic 0x46DB86E5, 0xF0-byte volumes from +0x10)."""
    out = []
    for ent in dictionary(arena):
        if ent['kind'] != 0xEB0019:
            continue
        o = ent['pointer']
        magic, _, count = struct.unpack_from('>III', arena, o)
        if magic != 0x46DB86E5:
            raise ValueError('unexpected trigger volume magic')
        out += [struct.unpack_from('>Q', arena, o + 0x10 + 0xF0 * i + 0xC8)[0] for i in range(count)]
    return out


def cell_contents(xsf):
    locators, volumes = [], []
    for _, _, (arena, _) in assets(xsf):
        locators += eb0009_locators(arena)
        volumes += eb0019_volumes(arena)
    return locators, volumes


def loc_xml(groups, empty_category):
    """XML LocationTable as retail DLC packages ship it next to their arenas (groups, sorted)."""
    category = '<Category />' if empty_category else '<Category></Category>'
    lines = ['<LocationTable>']
    for _, name, _, m in sorted(groups, key=lambda g: g[1]):
        lines += ['    <Location>', f'        <Name>{name}</Name>', f'        {category}',
                  '        <Transform>' + ', '.join(f'{x:f}' for x in m) + '</Transform>', '    </Location>']
    lines.append('</LocationTable>')
    return ('\r\n'.join(lines) + '\r\n').encode('ascii')


# ---- source vaults ------------------------------------------------------------------
@dataclass
class Blob:
    raw: bytes
    ptrs: list = field(default_factory=list)       # [(offset, string)]


@dataclass
class Rec:
    cls: str
    coll: str
    parent: int
    entries: list                                   # [(key, type key, flags, ('int'|'str'|'blob', value))]
    layout: Blob | None = None
    layout_types: list = field(default_factory=list)
    source: str = ''


class Source:
    def __init__(self, vlt, binf, label):
        self.v, self.bin, self.label = VaultReader(vlt, binf), binf, label
        self.stre = struct.unpack_from('>I', binf, 4)[0]
        self.records = {(r.cls, r.collection): r for r in self.v.records()}
        bounds = {len(binf)}
        for r in self.records.values():
            if r.layout is not None:
                bounds.add(r.layout)
            bounds.update(e.target for e in r.entries if e.target is not None)
        bounds.update(t for t in self.v.pointers.values() if t >= self.stre)
        self.bounds = sorted(bounds)
        self.bin_ptrs = sorted((o, t) for (space, o), t in self.v.pointers.items() if space == 'bin')
        self.ptr_offsets = [o for o, _ in self.bin_ptrs]

    def record(self, cls, coll):
        key = (cls if isinstance(cls, int) else h64(cls), coll if isinstance(coll, int) else h64(coll))
        if key not in self.records:
            raise KeyError(f'{self.label}: missing {cls}/{coll}')
        return self.records[key]

    def string(self, at):
        return _cstr(self.bin, at)

    def span(self, at):
        return self.bounds[bisect.bisect_right(self.bounds, at)] - at

    def blob(self, at, size):
        raw = bytearray(self.bin[at:at + size])
        ptrs = []
        lo = bisect.bisect_left(self.ptr_offsets, at)
        hi = bisect.bisect_left(self.ptr_offsets, at + size)
        for off, target in self.bin_ptrs[lo:hi]:
            if target >= self.stre:
                raise ValueError(f'{self.label}: data pointer at {off:#x} needs an explicit schema')
            ptrs.append((off - at, self.string(target)))
            raw[off - at:off - at + 4] = bytes(4)
        return Blob(bytes(raw), ptrs)

    def entry_value(self, e):
        tname = TYPE_NAMES.get(e.type, f'#{e.type:016x}')
        if e.target is None:
            return ('int', e.value)
        if e.flags & 0x4000:                        # inline char*
            if e.target >= self.stre:
                raise ValueError(f'{self.label}: inline {tname} points at data')
            return ('str', self.string(e.target))
        if e.flags & 0x0200:                        # array: u16 count, capacity, element size
            n, cap, size = struct.unpack_from('>HHH', self.bin, e.target)
            length = 8 + max(n, cap) * size
        elif tname in TYPE_SIZE:
            length = TYPE_SIZE[tname]
        elif tname.startswith('Attrib::Gen::ClassRefSpec_'):
            length = 16
        else:
            raise ValueError(f'{self.label}: no size for data type {tname}')
        if length > self.span(e.target):
            raise ValueError(f'{self.label}: {tname} blob exceeds its retail span')
        return ('blob', self.blob(e.target, length))

    def clone(self, cls, coll, new_coll, parent, set_=None, drop=(), add=()):
        """Copy a record; set_ {key: value} replaces values, add [(key, type, flags, value)] appends."""
        r = self.record(cls, coll)
        set_ = dict(set_ or {})
        layout_types = []
        if r.layout is not None:
            # Only challenge_local_data has fixed fields here: one eSk8Characters (types[0]).
            if r.cls != LD or r.types[0] != T['Sk8::Audio::eSk8Characters']:
                raise ValueError(f'{self.label}: unexpected fixed layout on {cls}/{coll}')
            layout_types = [r.types[0]]
        out = []
        for e in r.entries:
            if e.key in drop:
                continue
            value = set_.pop(e.key) if e.key in set_ else self.entry_value(e)
            out.append((e.key, e.type, e.flags, value))
        if set_:
            raise KeyError(f'{self.label}: {cls}/{coll} lacks attributes {[hex(k) for k in set_]}')
        out += list(add)
        layout = None
        if r.layout is not None:
            layout = self.blob(r.layout, min(8, self.span(r.layout)))
        name = cls if isinstance(cls, str) else CLASS_NAMES[cls]
        return Rec(name, new_coll, parent, out, layout, layout_types, f'{self.label}:{name}/{coll}')


def vault_pair(files, stem, label):
    return Source(files[f'{stem}.vlt'], files[f'{stem}.bin'], label)


# ---- value builders ---------------------------------------------------------------------
def v_str(text):
    return ('str', text)


def v_int(value):
    return ('int', value)


def v_float(bits):
    return ('int', bits)


def refspec(cls, coll):
    return ('blob', Blob(struct.pack('>QQQ', h64(cls), h64(coll), 0)))


def refspec_array(pairs):
    raw = struct.pack('>HHHH', len(pairs), len(pairs), 24, 0)
    raw += b''.join(struct.pack('>QQQ', h64(c), h64(n), 0) for c, n in pairs)
    return ('blob', Blob(raw))


def vaulted(cls, coll):
    path = f'{cls}\\{coll}.vlt'
    return ('blob', Blob(struct.pack('>QQQ', h64(cls), h64(coll), 0) + bytes(8), [(24, path)]))


def trigger(volume_id, debug):
    return ('blob', Blob(bytes(8) + struct.pack('>Q', volume_id), [(0, debug)]))


def reward(amount):
    return ('blob', Blob(struct.pack('>IIII', 0, amount, 0, 0)))


def hull_array(names):
    raw = struct.pack('>HHHH', len(names), len(names), 4, 0) + bytes(4 * len(names))
    return ('blob', Blob(raw, [(8 + 4 * i, n) for i, n in enumerate(names)]))


def world_ref(key):
    return ('blob', Blob(struct.pack('>QQ', key, 0)))


def visual_indicators(template, names):
    """Skate 3 indicator elements (ribbon arrow, default colour) at each Skate 2 locator."""
    head, elem = template.raw[:8], template.raw[8:88]
    elem_ptrs = [(o - 8, s) for o, s in template.ptrs if 8 <= o < 88]
    if struct.unpack_from('>HHH', head) [2] != 0x50 or [o for o, _ in elem_ptrs] != [8, 24]:
        raise ValueError('unexpected visual indicator template')
    raw = struct.pack('>HHHH', len(names), len(names), 0x50, 0) + elem * len(names)
    ptrs = []
    for i, name in enumerate(names):
        ptrs += [(8 + 80 * i + 8, elem_ptrs[0][1]), (8 + 80 * i + 24, name)]
    return ('blob', Blob(raw, ptrs))


def intro_event(template, scene):
    """Skate 3 ots_dwtn_01 intro event {5, 'ots_dwtn_01_intro', ''} naming another scene."""
    if template.raw != struct.pack('>HHHH4I', 1, 1, 16, 0, 5, 0, 0, 0) or \
            [o for o, _ in template.ptrs] != [12, 16] or template.ptrs[1][1] != '':
        raise ValueError('unexpected intro presentation event template')
    return ('blob', Blob(template.raw, [(12, scene), (16, '')]))


def audio_record(collections):
    """aud_bigfiles record for the package's cutscene lines, shaped like Maloof's DLC record.

    Cloned from Skate 3's own 'nis' bank (skatercollections) with Maloof's attributes: folder,
    the resident name three times, the DLC flag, the resident-bank flag and a free slot.
    """
    nis = collections.record('aud_bigfiles', 'nis')
    by_key = {e.key: e for e in nis.entries}
    if A_AUD_DLC in by_key or A_AUD_RESIDENT_BANK not in by_key or A_AUD_RESIDENT not in by_key:
        raise ValueError("Skate 3's nis bank record changed shape")
    name, flag = by_key[A_AUD_RESIDENT], by_key[A_AUD_RESIDENT_BANK]
    return collections.clone('aud_bigfiles', 'nis', AUDIO_COLLECTION, h64('default'), set_={
        A_AUD_FOLDER: v_str(AUDIO_FOLDER), A_AUD_RESIDENT: v_str(RESIDENT), A_AUD_SLOT: v_int(AUDIO_SLOT)},
        drop={A_AUD_FRENCH, A_AUD_GERMAN},
        add=[(A_AUD_RESIDENT_B, name.type, name.flags, v_str(RESIDENT)),
             (A_AUD_RESIDENT_C, name.type, name.flags, v_str(RESIDENT)),
             (A_AUD_DLC, flag.type, flag.flags, collections.entry_value(flag))])


# ---- Skate 2 goals ----------------------------------------------------------------------
@dataclass
class Goal:
    source: str
    name: str
    hull_source: str
    title_label: str
    nis_number: int
    own: int
    kill: int
    float_8022: int | None
    location: str
    trigger: tuple
    scoring: tuple
    indicators: list
    spot_volumes: list
    stream_source: str = ''
    twins: list = field(default_factory=list)
    locator_file: str = ''
    notes: list = field(default_factory=list)
    intro: str = ''                                 # Skate 2 intro scene (IntroPresentationEvents)
    props_sources: list = field(default_factory=list)   # Skate 2 props hulls of the online twin

    @property
    def props(self):
        """Packaged props hull names (<= 30 characters): nsv_otsp_<number>[b...]."""
        number = self.name.split('_')[2]
        return [f'nsv_otsp_{number}' + ('' if i == 0 else chr(ord('a') + i)) for i in range(len(self.props_sources))]

    @property
    def title(self):
        return f'ID_CHALLENGE_{self.name.upper()}_TITLE'

    @property
    def desc(self):
        return f'ID_CHALLENGE_{self.name.upper()}_DESC'

    @property
    def rewards(self):
        own = 1875 if self.kill <= 1100 else 2500 if self.kill <= 1600 else 3125
        return own, own * 4


def goal_name(source):
    """nsv_ots_<number>_<spot>: f_1a_14_ots_sh05_claustrophobia -> nsv_ots_14_sh05_claustrophobia.

    The name is also the goal's mission hull folder. Hull names stay within 30 characters:
    Skate 2's own mission folders are cut at 30 (ol_fa_dtw_04_accumulation_peac) and Skate 3's
    longest is 29, so the full Skate 2 name (up to 39 characters with the prefix) is not used.
    """
    parts = source.split('_')
    if parts[:2] != ['f', '1a'] or parts[3] != 'ots':
        raise ValueError(f'unexpected Skate 2 Own the Spot name {source}')
    name = PREFIX + '_'.join(parts[2:3] + parts[4:-1 if parts[-1] == 'sp' else None])
    if len(name) > HULL_NAME_MAX:
        raise ValueError(f'{name}: longer than {HULL_NAME_MAX} characters')
    return name


def s2_goals(s2, missions, misc):
    """Every Skate 2 single-player Own the Spot goal and its world data, resolved."""
    names = {}
    for (cls, coll), r in s2.records.items():
        for e in r.entries:
            if e.key == h64('Name') and e.target is not None:
                names[(cls, coll)] = s2.string(e.target)

    def chain(cls, coll):
        while coll:
            r = s2.records.get((cls, coll))
            if r is None:
                return
            yield r
            coll = r.parent

    def attr(cls, coll, key, inherit=True):
        for r in chain(cls, coll):
            for e in r.entries:
                if e.key == key:
                    return e
            if not inherit:
                return None
        return None

    def volume(e):
        debug = s2.v.pointers.get(('bin', e.target))
        return (struct.unpack_from('>Q', s2.bin, e.target + 8)[0], s2.string(debug) if debug is not None else '')

    def string_array(e, stride, offset):
        n = struct.unpack_from('>H', s2.bin, e.target)[0]
        out = []
        for i in range(n):
            t = s2.v.pointers.get(('bin', e.target + 8 + stride * i + offset))
            out.append(s2.string(t) if t is not None else '')
        return out

    group = h64(S2_GROUP)
    online_by_volume = {}
    online_hulls = {}
    for (cls, coll), r in s2.records.items():
        if cls != CH or not any(p.collection == h64(S2_ONLINE_GROUP) for p in chain(CH, r.parent)):
            continue
        for key in (h64('OTSScoringBoundary'), h64('ScoringBoundary'), h64('OTSTriggerBoundary')):
            e = attr(CH, coll, key, inherit=False)
            if e is not None:
                online_by_volume.setdefault(volume(e)[0], []).append(names[(CH, coll)])
        e = attr(CH, coll, A_HULLS)
        online_hulls[names[(CH, coll)]] = string_array(e, 4, 0) if e else []
    hulls_present = {n.split('/')[3] for n in missions}
    goals, excluded = [], {}
    for (cls, coll), r in sorted(s2.records.items(), key=lambda kv: names.get(kv[0], '')):
        if cls != CH or r.parent != group:
            continue
        source = names[(cls, coll)]
        if source in EXCLUDE:
            excluded[source] = EXCLUDE[source]
            continue
        trig = volume(attr(CH, coll, h64('OTSTriggerBoundary')))
        score = volume(attr(CH, coll, h64('OTSScoringBoundary')))
        hull_entry = attr(CH, coll, A_HULLS)
        hulls = sorted(set(string_array(hull_entry, 4, 0))) if hull_entry else []
        # Hull-less Skate 2 goals use the mission collection named after the goal.
        candidates = hulls or [source[:-3] if source.endswith('_sp') else source]
        if len(candidates) != 1 or candidates[0] not in hulls_present:
            raise ValueError(f'{source}: unresolved mission hull {candidates}')
        location = s2.string(attr(GD, coll, h64('Location')).target)
        vis = attr(CH, coll, h64('VisualIndicators'))
        spot = attr(LD, coll, 0x0A67561520C85C04, inherit=False)
        float_8022 = attr(CH, coll, A_FLOAT_8022, inherit=False)
        title = s2.string(attr(CH, coll, h64('Title')).target)
        number = int(source.split('_')[2])
        goal = Goal(source=source, name=goal_name(source),
                    hull_source=candidates[0], title_label=title, nis_number=number,
                    own=attr(CH, coll, h64('PointRequirement')).value,
                    kill=attr(CH, coll, h64('KilledItScore')).value,
                    float_8022=float_8022.value if float_8022 else None, location=location,
                    trigger=trig, scoring=score,
                    indicators=[n for n in (string_array(vis, 80, 24) if vis else []) if n],
                    spot_volumes=[struct.unpack_from('>Q', s2.bin, spot.target + 8 + 16 * i + 8)[0]
                                  for i in range(struct.unpack_from('>H', s2.bin, spot.target)[0])]
                    if spot else [])
        # Stream tiles and boundary polygon: Skate 2 ships them for the online twin.
        # Prefer the online twin scoring in this goal's scoring volume, then the hull's own name.
        twins = sorted(set(online_by_volume.get(score[0], [])))
        goal.twins = twins
        for name in twins + [goal.hull_source, goal.hull_source + '_ol']:
            if f'data/stream/{name}.xml' in misc and f'data/boundary/{name}.xml' in misc:
                goal.stream_source = name
                break
        else:
            raise ValueError(f'{source}: no stream/boundary XML (twins {twins})')
        # Props: the online twin requires a second hull with the spot's movable objects (the
        # Poop Deck sun lounger, the Lilypads benches...). The single-player record leaves it out
        # (the boat pool's lists its main hull twice where the twin names the props), so the
        # Poop Deck gap could not be cleared (log 345). Load the twin's props with the goal.
        props = {h for t in twins for h in online_hulls.get(t, []) if h.endswith(('_props', '_prop'))}
        if props - hulls_present:
            raise ValueError(f'{source}: props hull(s) {sorted(props - hulls_present)} missing')
        goal.props_sources = sorted(props - set(PROPS_SKIP))
        goal.notes += [f'props hull {h} left out: {PROPS_SKIP[h]}' for h in sorted(props & set(PROPS_SKIP))]
        # Intro cutscene: one presentation event {5, scene name, ''} (the Skate 3 shape too).
        intro = attr(CH, coll, h64('IntroPresentationEvents'), inherit=False)
        if intro is None or struct.unpack_from('>H', s2.bin, intro.target)[0] != 1 or \
                struct.unpack_from('>I', s2.bin, intro.target + 8)[0] != 5:
            raise ValueError(f'{source}: unexpected intro presentation events')
        goal.intro = s2.string(s2.v.pointers[('bin', intro.target + 12)])
        goals.append(goal)
    return goals, excluded


# ---- records ------------------------------------------------------------------------------
def build_records(main, framework, local_template, goals):
    """(bank records, framework records, {goal: local record}) following the Maloof layout."""
    TG = TYPE_GROUP
    bank = [main.clone('challenges', 'default', 'default', 0),
            main.clone('challenge_objective', 'default', 'default', 0),
            main.clone('challenge_global_data', 'default', 'default', 0)]
    # Group root (DLC convention: own root carrying the content id, parent 'default').
    bank.append(main.clone('challenges', 'main', ROOT, h64('default'), set_={
        h64('Name'): v_str(ROOT), A_GLOBAL_REF: refspec('challenge_global_data', ROOT),
        A_LOCAL_REF: vaulted('challenge_local_data', ROOT)}, drop={h64('StateGraph')},
        add=[(A_CONTENT_ID, T['EA::Reflection::UInt64'], 0x0000,
              ('blob', Blob(struct.pack('>Q', DLC_CONTENT_ID))))]))
    bank.append(main.clone('challenge_objective', 'main', ROOT, h64('default'),
                           set_={h64('Name'): v_str(ROOT)}))
    bank.append(Rec('challenge_global_data', ROOT, h64('default'), [], source='empty (Maloof dlc_mmcn)'))
    bank.append(main.clone('challenge_objectives_group', 'main', ROOT, h64('default')))
    # Own the Spot type group (Skate 3 own_the_spots, renamed under the root).
    bank.append(main.clone('challenges', 'own_the_spots', TG, h64(ROOT), set_={
        h64('Name'): v_str(TG), A_GLOBAL_REF: refspec('challenge_global_data', TG),
        A_LOCAL_REF: vaulted('challenge_local_data', TG)}))
    bank.append(main.clone('challenge_global_data', 'own_the_spots', TG, h64(ROOT)))
    bank.append(main.clone('challenge_objective', 'own_the_spots', TG, h64(ROOT),
                           set_={h64('Name'): v_str(TG)}))
    bank.append(main.clone('challenge_objectives_group', 'own_the_spots', TG, h64(ROOT)))

    keep_root = {A_LOCAL_C961, A_LOCAL_5A37}
    root_local = framework.record('challenge_local_data', 'main')
    fw = [framework.clone('challenge_local_data', 'main', ROOT, h64('default'),
                          drop={e.key for e in root_local.entries if e.key not in keep_root})]
    main_9ddc = next(e for e in root_local.entries if e.key == A_LOCAL_9DDC)
    fw.append(framework.clone('challenge_local_data', 'own_the_spots', TG, h64(ROOT),
                              add=[(A_LOCAL_9DDC, main_9ddc.type, main_9ddc.flags, v_int(main_9ddc.value))]))

    vi_entry = next(e for e in local_template.record(LD, 'ots_dwtn_01').entries
                    if e.key == h64('VisualIndicators'))
    vi_template = local_template.entry_value(vi_entry)[1]
    intro_entry = next(e for e in local_template.record(LD, 'ots_dwtn_01').entries
                       if e.key == h64('IntroPresentationEvents'))
    intro_template = local_template.entry_value(intro_entry)[1]
    locals_ = {}
    for g in goals:
        n = g.name
        own_reward, kill_reward = g.rewards
        bank.append(main.clone('challenges', 'ots_dwtn_01', n, h64(TG), set_={
            h64('Name'): v_str(n), A_GLOBAL_REF: refspec('challenge_global_data', n),
            A_LOCAL_REF: vaulted('challenge_local_data', n)}))
        add = []
        if g.float_8022 is not None:
            add.append((A_FLOAT_8022, T['EA::Reflection::Float'], 0x4000, v_float(g.float_8022)))
        bank.append(main.clone('challenge_global_data', 'ots_dwtn_01', n, h64(TG), set_={
            A_REWARD: v_int(own_reward),
            h64('Description'): v_str(g.desc),
            A_OWN_REWARD: reward(own_reward),
            h64('Location'): v_str(g.location),
            h64('MapStartLocation'): v_str(g.location),
            A_KILL_GROUP: refspec_array([('challenge_objectives_group', n + '_1')]),
            A_OWN_GROUP: refspec_array([('challenge_objectives_group', n + '_0')]),
            h64('OTSTriggerBoundary'): trigger(*g.trigger),
            A_KILL_REWARD: reward(kill_reward),
            A_HULLS: hull_array([n] + g.props),
            h64('SetManualAndWalkAsConnector'): v_int(0),     # Skate 2 OTS: false
            h64('Title'): v_str(g.title),
            h64('World'): world_ref(WORLD_KEY)}, add=add))
        bank.append(main.clone('challenge_objectives_group', 'ots_dwtn_01', n, h64(TG)))
        for suffix, points in (('_0', g.own), ('_1', g.kill)):
            bank.append(main.clone('challenge_objectives_group', 'ots_dwtn_01' + suffix, n + suffix, h64(n), set_={
                h64('Objectives'): refspec_array([('challenge_objective', n + '_0')]),
                h64('PointRequirement'): v_int(points)}))
        bank.append(main.clone('challenge_objective', 'ots_dwtn_01', n, h64(TG), set_={h64('Name'): v_str(n)}))
        bank.append(main.clone('challenge_objective', 'ots_dwtn_01_0', n + '_0', h64(n),
                               set_={h64('Name'): v_str(n + '_0')}))
        set_local = {A_SCORE_VOLUME: trigger(*g.scoring), h64('OTSScoringBoundary'): trigger(*g.scoring),
                     h64('StartLocation'): v_str(g.location),
                     h64('IntroPresentationEvents'): intro_event(intro_template, g.intro)}
        drop = set()
        if g.indicators:
            set_local[h64('VisualIndicators')] = visual_indicators(vi_template, g.indicators)
        else:
            drop.add(h64('VisualIndicators'))
        locals_[n] = local_template.clone('challenge_local_data', 'ots_dwtn_01', n, h64(TG),
                                          set_=set_local, drop=drop)
    return bank, fw, locals_


def write_vault(name, records, stamp):
    vault = dlc.Vault(name)
    for rec in records:
        types = list(rec.layout_types)
        for _, tkey, _, _ in rec.entries:
            if tkey not in types:
                types.append(tkey)
        rows = []
        for key, tkey, flags, (kind, value) in rec.entries:
            if kind == 'int':
                stored = value
            elif kind == 'str':
                stored = vault.string(value)
            else:
                stored = vault.blob(value.raw, [(o, vault.string(s)) for o, s in value.ptrs])
            rows.append((key, types.index(tkey), flags, stored))
        layout = None
        if rec.layout is not None:
            layout = vault.blob(rec.layout.raw, [(o, vault.string(s)) for o, s in rec.layout.ptrs])
        vault.record(rec.cls, rec.coll, rec.parent, types, rows, layout=layout)
    vlt, binf = vault.build()
    vlt = vlt[:8] + struct.pack('>Q', stamp) + vlt[16:]
    return vlt, binf


# ---- text ---------------------------------------------------------------------------------
def label_table(pairs, pack):
    """build_nsv_dlc.label_table with the package name as a parameter."""
    order = sorted(pairs)
    strings, offs = bytearray(), {}
    for name in order:
        offs[name] = len(strings)
        strings += pairs[name] + b'\0'
    table = b''.join(struct.pack('<II', dlc.label_id(n), offs[n]) for n in sorted(order, key=dlc.label_id))
    body = struct.pack('<III', len(order), 28, 28 + len(table)) + pack.encode().ljust(16, b'\0')
    body += table + strings
    body += bytes(-(len(body) + 8) % 4)
    return struct.pack('<II', 0x00039000, len(body)) + body


def histogram(strings):
    """Character histogram next to a label table: u16 slot c = c for every code >= 0x80 used.

    Reproduces all nine retail Maloof histograms; ASCII-only tables give build_nsv_dlc.EMPTY_HISTOGRAM.
    """
    used = {c for text in strings for c in text if c >= 0x80}
    return struct.pack('<III', 0x00039001, 6148, 256) + struct.pack('<3072H', *(c if c in used else 0
                                                                              for c in range(3072)))


def read_label_table(data):
    magic, _ = struct.unpack_from('<II', data, 0)
    if magic != 0x00039000:
        raise ValueError('not a label table')
    count, table, strings = struct.unpack_from('<III', data, 8)
    out = {}
    for i in range(count):
        lid, off = struct.unpack_from('<II', data, 8 + table + 8 * i)
        start = 8 + strings + off
        out[lid] = data[start:data.index(b'\0', start)]
    return out


EUROPEAN = ('English', 'French', 'German', 'Italian', 'Spanish', 'Dutch')


def description_cap(native_boot):
    """Longest challenge description Skate 3 ships in any European language (its UI shows these)."""
    labels = read_label_table(native_boot['data/fe/languages/labels/LANGUAGE_Labels_Global_skate3ng.BIN'])
    ids = [k for k, v in labels.items() if v.startswith(b'ID_CHALLENGE_') and v.endswith(b'_DESC')]
    cap = 0
    for lang in EUROPEAN:
        table = read_label_table(native_boot[f'data/fe/languages/{lang.lower()}/LANGUAGE_{lang}_Global_skate3ng.BIN'])
        cap = max([cap] + [len(table.get(k, b'')) for k in ids])
    return cap


def shorten(text, cap):
    """Whole sentences of the text up to cap bytes (the text itself if it fits or cannot be cut)."""
    if len(text) <= cap:
        return text
    cuts = [i + 1 for i in range(len(text)) if text[i:i + 1] in b'.!?' and
            (i + 1 == len(text) or text[i + 1:i + 2] == b' ') and i + 1 <= cap]
    return text[:cuts[-1]] if cuts else text


def goal_texts(goals, misc_boot, cap):
    """{language: {label: text}}: Skate 2's own title and Reda's spot intro line(s) as description.

    European languages from Skate 2's Global tables (English where a line is missing), cut at a
    sentence end to fit the longest Skate 3 challenge description; Largest = the longest of those;
    Japanese empty like every retail Skate 3 Japanese string; Labels = the label names.
    """
    tables = {}
    for lang in EUROPEAN:
        path = f'data/fe/languages/{lang.lower()}/LANGUAGE_{lang}_Global_skate2ng.BIN'
        tables[lang] = read_label_table(misc_boot[path])
    texts = {lang: {} for lang in dlc.LANGUAGES}
    en = tables['English']
    for g in goals:
        nis = [f'ID_NISSUBTITLE_F_1A_{g.nis_number:02d}_OTS_{k:02d}' for k in (1, 2)]
        for lang in EUROPEAN:
            t = tables[lang]

            def text(label):
                return t.get(dlc.label_id(label)) or en.get(dlc.label_id(label)) or b''
            title = text(g.title_label)
            desc = shorten(b' '.join(text(label) for label in nis if text(label)), cap)
            if not title or not desc:
                raise ValueError(f'{g.source}: missing {lang} text')
            if len(desc) > cap:
                g.notes.append(f'{lang} description {len(desc)} bytes (over {cap}, no sentence cut)')
            texts[lang][g.title], texts[lang][g.desc] = title, desc
        for label in (g.title, g.desc):
            texts['Largest'][label] = max((texts[lang][label] for lang in EUROPEAN), key=len)
            texts['Japanese'][label] = b''
            texts['Labels'][label] = label.encode()
    return texts


# ---- teleport markers ---------------------------------------------------------------------
def teleport_box(t):
    half, height = TELEPORT_BOX
    x, y, z = t['start']
    return (x - half, y - 0.3, z - half), (x + half, y - 0.3 + height, z + half)


def build_teleports(main, framework, s2_missions, arena_data, s2_boot):
    """(bank records, framework records, {name: local record}, files, moved arena, texts, manifest).

    Records follow Danny Way's DLC teleports, built on Skate 3's base 'teleports' group: group
    nsv_teleports under the package root; per marker challenges (Name, refs), global data
    (Location, RequiredChallengeHull, Title, World) and local data (TeleporterTrigger,
    TeleportExitLocation).
    """
    TG = TELEPORT_GROUP
    # The challenge manager starts a teleport (and streams its hull) only if
    # IgnorePrereqForGlobalStart is set or its prerequisite check (the career's
    # record of the challenge, [manager+0x127A8] vtable +4) passes; that check knows
    # Skate 3's teleports but not a package one (log 351: nsv_tp_lighthouse "filtered
    # out by the game" by the manager 82BD19B4 and the map 8254E034, never streamed).
    # Skate 3 sets the bool only on 'default' (false); the marker should always be live.
    prereq = next(e for e in main.record(GD, 'default').entries if e.key == A_IGNORE_PREREQ)
    bank = [main.clone('challenges', 'teleports', TG, h64(ROOT), set_={
        h64('Name'): v_str(TG), A_GLOBAL_REF: refspec('challenge_global_data', TG),
        A_LOCAL_REF: vaulted('challenge_local_data', TG)}),
        main.clone('challenge_global_data', 'teleports', TG, h64(ROOT),
                   add=[(A_IGNORE_PREREQ, prereq.type, prereq.flags, v_int(BOOL_TRUE))])]
    fw = [framework.clone('challenge_local_data', 'teleports', TG, h64(ROOT))]
    template_local = framework.record('challenge_local_data', 'teleports')
    exit_entry = next(e for e in template_local.entries if e.key == A_TELEPORT_EXIT)
    trigger_entry = next(e for e in template_local.entries if e.key == A_TELEPORT_TRIGGER)
    exit_template = framework.entry_value(exit_entry)[1]
    if len(exit_template.raw) != 40 or [o for o, _ in exit_template.ptrs] != [32]:
        raise ValueError('unexpected TeleportExitLocation template')
    gd = {e.key: e for e in main.record(GD, 'ots_dwtn_01').entries}
    group_gd = {e.key: e for e in main.record(GD, 'teleports').entries}
    if group_gd[A_FREESKATE_ACTIVE].value != 0:
        raise ValueError("Skate 3's teleports group changed shape")
    native = {e.key for e in main.record(GD, 'tele_dwtn_to_dwtnskpk').entries}
    if native != {h64('Location'), A_HULLS, h64('Title'), h64('World')}:
        raise ValueError("Skate 3's Downtown teleport changed shape")
    moves, files, locals_, labels, report = {}, [], {}, {}, []
    english = read_label_table(s2_boot['data/fe/languages/english/LANGUAGE_English_Global_skate2ng.BIN'])
    tables = {lang: read_label_table(s2_boot[f'data/fe/languages/{lang.lower()}/LANGUAGE_{lang}_Global_skate2ng.BIN'])
              for lang in EUROPEAN}
    for t in TELEPORTS:
        n = t['name']
        if not n.startswith('nsv_') or len(n) > HULL_NAME_MAX:
            raise ValueError(f'{n}: teleport names follow the hull naming contract')
        lo, hi = teleport_box(t)
        prefix = f'data/content/missions/{t["source_hull"]}/'
        xsf, volume_id = teleports.move_trigger_hull(s2_missions[prefix + 'cSim_Global.xsf'], lo, hi)
        for path in sorted(p for p in s2_missions if p.startswith(prefix)):
            leaf = path[len(prefix):]
            if leaf.startswith(t['source_hull'] + '_'):
                leaf = n + leaf[len(t['source_hull']):]
            files.append((f'data/content/missions/{n}/{leaf}', xsf if leaf == 'cSim_Global.xsf' else s2_missions[path]))
        moves[t['start_locator']] = teleports.facing_matrix(t['start'], t['start_facing'])
        moves[t['exit_locator']] = teleports.facing_matrix(t['exit'], t['exit_facing'])
        title = f'ID_CHALLENGE_{n.upper()}_TITLE'
        bank.append(main.clone('challenges', 'teleports', n, h64(TG), set_={
            h64('Name'): v_str(n), A_GLOBAL_REF: refspec('challenge_global_data', n),
            A_LOCAL_REF: vaulted('challenge_local_data', n)}, drop={h64('StateGraph')}))
        # The same four attributes as tele_dwtn_to_dwtnskpk; the rest come from the group.
        bank.append(Rec('challenge_global_data', n, h64(TG), [
            (h64('Location'), gd[h64('Location')].type, gd[h64('Location')].flags, v_str(t['start_locator'])),
            (A_HULLS, gd[A_HULLS].type, gd[A_HULLS].flags, hull_array([n])),
            (h64('Title'), gd[h64('Title')].type, gd[h64('Title')].flags, v_str(title)),
            (h64('World'), gd[h64('World')].type, gd[h64('World')].flags, world_ref(WORLD_KEY))],
            source='teleport (tele_dwtn_to_dwtnskpk shape)'))
        raw = bytearray(exit_template.raw)
        struct.pack_into('>Q', raw, 16, WORLD_KEY)
        debug = f'BAM|{n}_volume|0x{volume_id:016x}'
        locals_[n] = Rec('challenge_local_data', n, h64(TG), [
            (A_TELEPORT_TRIGGER, trigger_entry.type, trigger_entry.flags, trigger(volume_id, debug)),
            (A_TELEPORT_EXIT, exit_entry.type, exit_entry.flags, ('blob', Blob(bytes(raw), [(32, t['exit_locator'])])))],
            source='teleport (Danny Way DLC shape)')
        for lang in dlc.LANGUAGES:
            labels.setdefault(lang, {})
        for lang in EUROPEAN:
            text = None
            if t['title_skate2']:
                text = tables[lang].get(dlc.label_id(t['title_skate2'])) or english.get(dlc.label_id(t['title_skate2']))
            labels[lang][title] = text or t['title_english'].encode('latin1')
        labels['Largest'][title] = max((labels[lang][title] for lang in EUROPEAN), key=len)
        labels['Japanese'][title] = b''
        labels['Labels'][title] = title.encode()
        report.append(dict(name=n, collection_key=f'{h64(n):016X}', title_label=title,
                           title=labels['English'][title].decode('latin1'), hull=n, source_hull=t['source_hull'],
                           volume_id=f'{volume_id:016X}', trigger_box=[list(lo), list(hi)],
                           start_locator=t['start_locator'], start=list(t['start']),
                           exit_locator=t['exit_locator'], exit=list(t['exit']), world=f'{WORLD_KEY:016X}'))
    moved = teleports.move_locator_groups(arena_data, moves)
    return bank, fw, locals_, files, moved, labels, report


# ---- intro cutscenes ----------------------------------------------------------------------
SCENE_PATTERN = r'data/scene/(scene|ice)_(\d{3})\.bin'


def newest_scene_files(native, root):
    """Skate 3's scene table and ICE database the game would load: the largest number among the
    base files and every installed content package except this one (retail DLC ship supersets)."""
    import re
    pattern = re.compile(SCENE_PATTERN)
    best = {}

    def offer(kind, number, origin, data):
        if kind not in best or number > best[kind][0]:
            best[kind] = (number, origin, data)
    offer('scene', 0, 'big/miscboot.big', read_archive(
        native / 'big/miscboot.big', lambda n: n == 'data/scene/scene_000.bin')['data/scene/scene_000.bin'])
    offer('ice', 0, 'big/scene.big', intros.decode_payload(read_archive_raw(
        native / 'big/scene.big', lambda n: n == 'data/scene/ice_000.bin')['data/scene/ice_000.bin']))
    packages = root / '0000000000000000' / f'{dlc.TITLE_ID:08X}' / '00000002'
    for big in sorted(packages.glob('*/*.big')) if packages.is_dir() else []:
        if big.parent.name == PACKAGE_DIR:
            continue
        raw = big.read_bytes()
        if raw[:4] != b'EB\0\3' or struct.unpack_from('>I', raw, 8)[0] != 0x00500600:
            continue
        for name, data in read_dlc_archive(raw, lambda n: pattern.fullmatch(n) is not None).items():
            kind, number = pattern.fullmatch(name).groups()
            offer(kind, int(number), f'{big.parent.name}/{big.name}', data)
    if max(best['scene'][0], best['ice'][0]) >= SCENE_NUMBER:
        raise ValueError(f'an installed scene file is numbered {SCENE_NUMBER} or higher')
    return best['scene'], best['ice']


def build_intros(source, goals, newest, s2_boot):
    """(files, {language: {label: text}}, manifest) for the goals' Skate 2 intro cutscenes.

    The merged scene table and ICE database keep every Skate 3 record and object byte for byte
    and append each goal's Skate 2 intro scene (skate2_goal_intros.merge). The lines those scenes
    reference (Reda's speech, speaker 40, and post-produced effects, speaker 99) come from
    Skate 2's English nis.big and post.big as loose .sns streams plus one resident archive of
    their .snr headers; file names are upper case so the subtitle label Skate 3 derives from
    them (SPEECH_SCRIPTED_<NAME>) matches the labels written here (Skate 2's NIS subtitle text).
    """
    import re
    (scene_n, scene_origin, scene_data), (ice_n, ice_origin, ice_data) = newest
    s2_scene = read_archive(source / 'big/miscboot.big',
                            lambda n: n == 'data/scene/scene_000.bin')['data/scene/scene_000.bin']
    s2_ice = read_archive(source / 'big/scene.big', lambda n: n == 'data/scene/ice_000.bin')['data/scene/ice_000.bin']
    scene, ice, report = intros.merge(scene_data, ice_data, s2_scene, s2_ice, [g.intro for g in goals])
    files = [(SCENE_FILE, scene), (ICE_FILE, ice)]

    anims = sorted({s for r in report.values() for a in r['assets'] for s in a
                    if s.startswith('scene/anim/') and s.endswith('.abin')})
    got = read_archive(source / 'big/scene.big', lambda n: n[5:] in anims) if anims else {}
    for a in anims:
        if 'data/' + a not in got:
            raise ValueError(f'Skate 2 cutscene animation {a} not found')
        files.append(('data/' + a, got['data/' + a]))

    def resident(archive, name):
        inner = read_archive(source / archive, lambda n: n == name)[name]
        with mmap.mmap(-1, len(inner)) as m:
            m.write(inner)
            return {e.name: bytes(m[e.offset:e.offset + e.size]) for e in entries(m)}
    banks = (('audio/english/nis.big', resident('audio/english/nis.big', 'nisresident.big')),
             ('audio/post.big', resident('audio/post.big', 'postresident.big')))
    tables = {lang: read_label_table(s2_boot[f'data/fe/languages/{lang.lower()}/LANGUAGE_{lang}_Global_skate2ng.BIN'])
              for lang in EUROPEAN}
    texts = {lang: {} for lang in dlc.LANGUAGES}
    streams, headers, goal_lines = {}, [], {}
    for g in goals:
        lines = []
        for line, speaker in report[g.intro]['lines']:
            # Speech: <speaker>_<line>_*.snr in nis.big; effects (no speaker word): 99_<line>_* in post.big.
            nis_hits = [(banks[0][0], n) for n in banks[0][1] if speaker is not None and
                        re.fullmatch(rf'{speaker}_{line}_.*\.snr', n)]
            hits = nis_hits or [(banks[1][0], n) for n in banks[1][1] if re.fullmatch(rf'99_{line}_.*\.snr', n)]
            if len(hits) != 1:
                raise ValueError(f'{g.intro}: line {line} (speaker {speaker}) matches {hits}')
            archive, snr = hits[0]
            speaker = int(snr.split('_', 1)[0])
            upper = snr[:-4].upper()
            if upper not in streams:
                streams[upper] = (archive, snr[:-4] + '.sns')
                headers.append((f'./{upper}.snr', dict(banks)[archive][snr]))
            lines.append(dict(line=line, speaker=speaker, file=f'{AUDIO_DIR}/{upper}.sns', source=f'{archive}:{snr[:-4]}.sns'))
            if speaker == REDA:
                label = intros.subtitle_label(upper)
                nis = [f'ID_NISSUBTITLE_F_1A_{g.nis_number:02d}_OTS_{k:02d}' for k in (1, 2)]
                for lang in EUROPEAN:
                    t, en = tables[lang], tables['English']
                    parts = [t.get(dlc.label_id(x)) or en.get(dlc.label_id(x)) for x in nis]
                    texts[lang][label] = b' '.join(p for p in parts if p)
                    if not texts[lang][label]:
                        raise ValueError(f'{g.intro}: no {lang} subtitle text')
                texts['Largest'][label] = max((texts[lang][label] for lang in EUROPEAN), key=len)
                texts['Japanese'][label] = b''
                texts['Labels'][label] = label.encode()
                lines[-1]['subtitle_label'] = label
        if not any(x['speaker'] == REDA for x in lines):
            raise ValueError(f'{g.intro}: no line by Reda')
        goal_lines[g.name] = lines
    bank_streams = []
    for archive in sorted({a for a, _ in streams.values()}):
        want = {n for a, n in streams.values() if a == archive}
        got = read_archive(source / archive, lambda n: n in want)
        for upper, (a, n) in streams.items():
            if a == archive:
                bank_streams.append((f'{AUDIO_DIR}/{upper}.sns', got[n]))
    headers.sort()
    bank_streams.sort()
    # Maloof's layout: the resident archive of headers and the streams, loose.
    files.append((f'{AUDIO_DIR}/{RESIDENT}', intros.resident_archive(headers)))
    files += bank_streams
    manifest = dict(
        scene_file=SCENE_FILE, ice_file=ICE_FILE,
        native_scene=dict(number=scene_n, origin=scene_origin, bytes=len(scene_data)),
        native_ice=dict(number=ice_n, origin=ice_origin, bytes=len(ice_data)),
        audio=dict(collection=AUDIO_COLLECTION, slot=AUDIO_SLOT, folder=AUDIO_FOLDER, resident=RESIDENT,
                   streams=len(streams)),
        animations=['data/' + a for a in anims],
        goals={g.name: dict(scene=g.intro, **report[g.intro], audio=goal_lines[g.name]) for g in goals})
    return files, texts, manifest


# ---- package ------------------------------------------------------------------------------
def gather(source, native):
    s2db = read_archive(source / 'db/db.big', lambda n: n.startswith('data/db/ChallengeCore.'))
    s2 = vault_pair(s2db, 'data/db/ChallengeCore', 'skate2:ChallengeCore')
    teleport_hulls = tuple(f'/missions/{t["source_hull"]}/' for t in TELEPORTS)
    s2_missions = read_archive(source / 'content/missions.big',
                               lambda n: '/missions/f_1a_' in n or any(h in n for h in teleport_hulls))
    s2_misc = read_archive(source / 'big/miscload.big',
                           lambda n: n.startswith(('data/stream/f_1a_', 'data/boundary/f_1a_')))
    s2_locators = read_archive(source / 'big/globallocators.big')
    s2_boot = read_archive(source / 'big/miscboot.big', lambda n: n.startswith('data/fe/languages/')
                           and '_Global_' in n)
    s3db = read_archive(native / 'big/db.big', lambda n: n.endswith(('.vlt', '.bin')))
    s3_boot = read_archive(native / 'big/miscboot.big', lambda n: n.startswith('data/fe/languages/')
                           and '_Global_' in n)
    return s2, s2_missions, s2_misc, s2_locators, s2_boot, s3db, s3_boot


def build_package(source, native, root=None):
    s2, s2_missions, s2_misc, s2_locators, s2_boot, s3db, s3_boot = gather(source, native)
    main = vault_pair(s3db, 'data/db/challengebanks/main', 'skate3:challengebanks/main')
    framework = vault_pair(s3db, 'data/db/challenge_local_data_framework', 'skate3:challenge_local_data_framework')
    local_template = vault_pair(s3db, 'data/db/challenge_local_data/ots_dwtn_01', 'skate3:ots_dwtn_01')
    collections = vault_pair(s3db, 'data/db/skatercollections', 'skate3:skatercollections')
    goals, excluded = s2_goals(s2, s2_missions, s2_misc)
    newest = newest_scene_files(native, root or native.parent.parent)
    intro_files, intro_texts, intro_manifest = build_intros(source, goals, newest, s2_boot)

    # Locators: each goal's challenge locator lives in one Skate 2 global arena.
    arenas = {}
    for path, data in s2_locators.items():
        arenas[path.rsplit('/', 1)[1]] = (data, eb0009_locators(data))
    files, manifest_goals = [], []
    used_arenas = set()
    hull_cells = {}
    for g in goals:
        homes = [a for a, (_, locs) in arenas.items() if any(k == 'G' and n == g.location for k, n, _, _ in locs)]
        if len(homes) != 1:
            raise ValueError(f'{g.source}: challenge locator {g.location} in {homes}')
        g.locator_file = homes[0]
        used_arenas.add(homes[0])
        prefix = f'data/content/missions/{g.hull_source}/'
        cell = s2_missions[prefix + 'cSim_Global.xsf']
        hull_cells[g.name] = cell_contents(cell)
        for path in sorted(p for p in s2_missions if p.startswith(prefix)):
            leaf = path[len(prefix):]
            if leaf.startswith(g.hull_source + '_'):
                leaf = g.name + leaf[len(g.hull_source):]
            files.append((f'data/content/missions/{g.name}/{leaf}', s2_missions[path]))
        groups = [x for x in hull_cells[g.name][0] if x[0] == 'G']
        files.append((f'data/content/missions/{g.name}/{g.name}_Sim.loc', loc_xml(groups, True)))
        files.append((f'data/stream/{g.name}.xml', s2_misc[f'data/stream/{g.stream_source}.xml']))
        files.append((f'data/boundary/{g.name}.xml', s2_misc[f'data/boundary/{g.stream_source}.xml']))
        for props, source_hull in zip(g.props, g.props_sources):
            props_prefix = f'data/content/missions/{source_hull}/'
            for path in sorted(p for p in s2_missions if p.startswith(props_prefix)):
                leaf = path[len(props_prefix):]
                if leaf.startswith(source_hull + '_'):
                    leaf = props + leaf[len(source_hull):]
                files.append((f'data/content/missions/{props}/{leaf}', s2_missions[path]))
            # No stream/boundary XML: both games key those by challenge, not by props hull.
        # Skate 2 spot volumes stay out of the records (Skate 3 OTS never sets them).
        missing = [v for v in g.spot_volumes if v not in hull_cells[g.name][1]]
        if missing:
            g.notes.append('Skate 2 spot volume(s) ' + ', '.join(f'{v:016X}' for v in missing) +
                           ' are not in the goal hull (Skate 2 data quirk)')
    # Teleport markers: trigger hulls plus start/exit locators moved within the Waterfront arena.
    if TELEPORT_ARENA not in used_arenas:
        raise ValueError(f'teleport locators need {TELEPORT_ARENA} in the package')
    tp_bank, tp_fw, tp_locals, tp_files, moved_arena, tp_texts, tp_manifest = build_teleports(
        main, framework, s2_missions, arenas[TELEPORT_ARENA][0], s2_boot)
    arenas[TELEPORT_ARENA] = (moved_arena, eb0009_locators(moved_arena))
    files += tp_files
    for name in sorted(used_arenas):
        data, locs = arenas[name]
        files.append((f'{LOCATOR_DIR}/{name}', data))
        files.append((f'{LOCATOR_DIR}/{name[:-4]}.loc', loc_xml([x for x in locs if x[0] == 'G'], False)))

    bank, fw, locals_ = build_records(main, framework, local_template, goals)
    bank += tp_bank
    fw += tp_fw
    locals_.update(tp_locals)
    fw.append(audio_record(collections))
    bank_vlt, bank_bin = write_vault(BANK_VAULT, bank, STAMP_BANK)
    fw_vlt, fw_bin = write_vault(FRAMEWORK_VAULT, fw, STAMP_FRAMEWORK)
    endc = b'EndC' + struct.pack('>I', 16) + bytes(8)
    db = [(f'data/db/{FRAMEWORK_VAULT}.vlt', fw_vlt), (f'data/db/{FRAMEWORK_VAULT}.bin', fw_bin),
          (f'data/db/{FRAMEWORK_VAULT}.opt', endc),
          (f'data/db/challengebanks/{BANK_VAULT}.vlt', bank_vlt),
          (f'data/db/challengebanks/{BANK_VAULT}.bin', bank_bin),
          (f'data/db/challengebanks/{BANK_VAULT}.opt', endc)]
    for name, rec in locals_.items():
        vlt, binf = write_vault(name, [rec], STAMP_BANK)
        db += [(f'data/db/challenge_local_data/{name}.vlt', vlt), (f'data/db/challenge_local_data/{name}.bin', binf)]
    vaultlist = ('<?xml version="1.0" encoding="utf-8"?>\r\n<VaultList>\r\n'
                 f'  <VaultFile>{FRAMEWORK_VAULT}.vlt</VaultFile>\r\n'
                 f'  <VaultFile>challengebanks\\{BANK_VAULT}.vlt</VaultFile>\r\n</VaultList>').encode()
    texts = goal_texts(goals, s2_boot, description_cap(s3_boot))
    for lang, labels in intro_texts.items():
        texts[lang].update(labels)
    for lang, labels in tp_texts.items():
        texts[lang].update(labels)
    lang_files = []
    for lang in dlc.LANGUAGES:
        folder = f'data/fe/languages/{lang.lower()}'
        lang_files.append((f'{folder}/LANGUAGE_{lang}_{PACK}_skate3ng-dlc_{PACK}.BIN', label_table(texts[lang], PACK)))
        lang_files.append((f'{folder}/LANGUAGE_{lang}_HISTOGRAM_skate3ng-dlc_{PACK}.BIN',
                           histogram(texts[lang].values())))
    files = ([(f'./{PACK}.ver', VERSION_FILE), (VAULTLIST, vaultlist)] + db + files + intro_files + lang_files)

    for g in goals:
        cell_locs, volumes = hull_cells[g.name]
        _, gl = arenas[g.locator_file]
        loc = next(m for k, n, _, m in gl if k == 'G' and n == g.location)
        spawns = [dict(name=n, position=[round(x, 4) for x in m[12:15]]) for k, n, p, m in gl
                  if k == 'R' and n.startswith(g.location + '::')]
        manifest_goals.append(dict(
            source=g.source, name=g.name, collection_key=f'{h64(g.name):016X}',
            title_label=g.title, title=texts['English'][g.title].decode('latin1'),
            description_label=g.desc, description=texts['English'][g.desc].decode('latin1'),
            skate2_title_label=g.title_label, skate2_hull=g.hull_source,
            props_hulls=[dict(name=p, skate2_hull=s) for p, s in zip(g.props, g.props_sources)],
            skate2_stream_boundary_xml=g.stream_source, skate2_online_twins=g.twins,
            own_it_points=g.own, killed_it_points=g.kill, rewards=list(g.rewards),
            float_8022=struct.unpack('>f', struct.pack('>I', g.float_8022))[0] if g.float_8022 else None,
            location=dict(name=g.location, arena=g.locator_file, position=[round(x, 4) for x in loc[12:15]],
                          spawns=spawns),
            start_location=g.location, map_start_location=g.location,
            visual_indicators=[dict(name=n, position=[round(x, 4) for x in next(
                m for k, nn, _, m in cell_locs if nn == n)[12:15]]) for n in g.indicators],
            trigger_boundary=dict(id=f'{g.trigger[0]:016X}', debug=g.trigger[1]),
            scoring_boundary=dict(id=f'{g.scoring[0]:016X}', debug=g.scoring[1]),
            skate2_spot_volumes=[f'{v:016X}' for v in g.spot_volumes],
            hull_volumes=[f'{v:016X}' for v in volumes],
            hull_locators=sorted({n for _, n, _, _ in cell_locs}),
            files=sorted(p for p, _ in files if f'/{g.name}/' in p or p.endswith(f'/{g.name}.xml')
                         or p.endswith(f'/{g.name}.vlt') or p.endswith(f'/{g.name}.bin')),
            notes=g.notes))
    manifest = dict(
        format='NSV_GOALS_MANIFEST_V1', package=dict(
            id=PACKAGE_ID.decode(), directory=PACKAGE_DIR, archive=f'{PACK}_00000000.big',
            display_name=DISPLAY_NAME, title_id=f'{dlc.TITLE_ID:08X}', vaultlist=VAULTLIST,
            vaults=[f'data/db/{FRAMEWORK_VAULT}.vlt', f'data/db/challengebanks/{BANK_VAULT}.vlt'],
            dlc_content_id=DLC_CONTENT_ID),
        naming_contract='every added collection name starts with nsv_',
        world=dict(name=WORLD, key=f'{WORLD_KEY:016X}'),
        groups=dict(root=ROOT, root_key=f'{h64(ROOT):016X}', own_the_spots=TYPE_GROUP,
                    own_the_spots_key=f'{h64(TYPE_GROUP):016X}',
                    state_graph='challenge_stategraph/ownthespot_shared_mlui'),
        global_locator_arenas=[f'{LOCATOR_DIR}/{n}' for n in sorted(used_arenas)],
        excluded=excluded, goals=manifest_goals, intros=intro_manifest,
        teleports=dict(group=TELEPORT_GROUP, group_key=f'{h64(TELEPORT_GROUP):016X}', arena=TELEPORT_ARENA,
                       markers=tp_manifest),
        files=[dict(path=p, bytes=len(d), sha256=hashlib.sha256(d).hexdigest()) for p, d in files])
    context = dict(main=main, framework=framework, s3db=s3db, goals=goals, hull_cells=hull_cells,
                   arenas={n: arenas[n] for n in used_arenas}, texts=texts, bank=bank, fw=fw, locals=locals_,
                   newest=newest, intros=intro_manifest, teleports=tp_manifest)
    return files, manifest, context


# ---- validation -----------------------------------------------------------------------------
def s3_collections(s3db):
    """{(class, collection)} over every Skate 3 vault in db.big."""
    known = set()
    for path in s3db:
        if path.endswith('.vlt') and path[:-4] + '.bin' in s3db:
            try:
                v = VaultReader(s3db[path], s3db[path[:-4] + '.bin'])
            except ValueError:
                continue
            known.update((r.cls, r.collection) for r in v.records())
    return known


def blob_refs(raw, classes):
    """(class, collection) pairs in a data blob, at 4-byte steps (RefSpec layout)."""
    out = set()
    for i in range(0, len(raw) - 15, 4):
        c, n = struct.unpack_from('>QQ', raw, i)
        if c in classes and n:
            out.add((c, n))
    return out


CLASS_REF = 'Attrib::Gen::ClassRefSpec_'
KNOWN_NAMES = {h64(n): n for n in list(CLASS_NAMES.values()) + list(CLASS_REFSPEC_TYPES) + [
    'bam', 'dynamicobjects', 'default', 'arrow', 'ribbon_indicator', 'fe_locations']}


def entry_refs(binf, e, length, classes):
    """References held by a data entry: RefSpec-shaped pairs plus ClassRefSpec_<class> collections."""
    tname = TYPE_NAMES.get(e.type, '')
    raw = binf[e.target:e.target + max(16, length)]
    refs = blob_refs(raw, classes)
    if tname.startswith(CLASS_REF):
        coll = struct.unpack_from('>Q', raw)[0]
        if coll:
            refs.add((h64(tname[len(CLASS_REF):]), coll))
    return refs


def ref_name(ref):
    return '/'.join(KNOWN_NAMES.get(k, f'{k:016X}') for k in ref)


def validate(files, manifest, context):
    """Offline checks; returns a list of findings (raises on any hard failure)."""
    paths = [p for p, _ in files]
    data = dict(files)
    if len(set(paths)) != len(paths):
        raise ValueError('duplicate package paths')
    findings = []
    # 1. Vaults read back, record for record.
    vaults = {}
    for stem, recs in [(f'data/db/challengebanks/{BANK_VAULT}', context['bank']),
                       (f'data/db/{FRAMEWORK_VAULT}', context['fw'])] + \
            [(f'data/db/challenge_local_data/{n}', [r]) for n, r in context['locals'].items()]:
        v = VaultReader(data[stem + '.vlt'], data[stem + '.bin'])
        got = {(r.cls, r.collection): r for r in v.records()}
        if len(got) != len(recs):
            raise ValueError(f'{stem}: {len(got)} records read back, {len(recs)} written')
        for rec in recs:
            r = got[(h64(rec.cls), h64(rec.coll))]
            if r.parent != rec.parent or len(r.entries) != len(rec.entries):
                raise ValueError(f'{stem}: {rec.cls}/{rec.coll} did not read back')
            for e in r.entries:
                if e.flags & 0x4000 and e.target is not None and v.string(e.target) is None and \
                        v.bin[e.target:e.target + 1] != b'\0':
                    raise ValueError(f'{stem}: unreadable string in {rec.cls}/{rec.coll}')
        vaults[stem] = (v, recs)
    findings.append(f'{len(vaults)} vaults read back ({sum(len(r) for _, r in vaults.values())} records)')

    # 2. Naming contract and key collisions with Skate 3's own collections.
    s3 = s3_collections(context['s3db'])
    new = set()
    for _, recs in vaults.values():
        for rec in recs:
            key = (h64(rec.cls), h64(rec.coll))
            if rec.coll != 'default':
                if not rec.coll.startswith('nsv_'):
                    raise ValueError(f'naming contract: {rec.cls}/{rec.coll}')
                if key in s3:
                    raise ValueError(f'collection collides with Skate 3: {rec.cls}/{rec.coll}')
            new.add(key)
    findings.append(f'{len(new)} collections, all added ones named nsv_*, no key collides with Skate 3')

    # 3. Parents resolve at construction (base vaults, then vaultlist order, then on-demand locals).
    loaded = set(s3)
    for stem in [f'data/db/{FRAMEWORK_VAULT}', f'data/db/challengebanks/{BANK_VAULT}'] + \
            [f'data/db/challenge_local_data/{n}' for n in context['locals']]:
        v, _ = vaults[stem]
        for r in v.records():
            if r.parent and (r.cls, r.parent) not in loaded:
                raise ValueError(f'{stem}: parent {r.parent:016X} of {r.collection:016X} is not loaded first')
            loaded.add((r.cls, r.collection))

    # 4. Every RefSpec in the new records resolves (or is inherited unchanged from its retail template).
    classes = {c for c, _ in s3 | new}
    template_refs = set()
    for src in (context['main'], context['framework']):
        for r in src.records.values():
            for e in r.entries:
                if e.target is not None and e.target >= src.stre:
                    template_refs |= entry_refs(src.bin, e, src.span(e.target), classes)
    unresolved, retail_unresolved = set(), set()
    preloaded = {(h64(r.cls), h64(r.coll)) for r in context['fw'] + context['bank']}
    for stem, (v, recs) in vaults.items():
        stre = struct.unpack_from('>I', v.bin, 4)[0]
        for r in v.records():
            for e in r.entries:
                if e.target is None or e.target < stre:
                    continue
                tname = TYPE_NAMES.get(e.type, '')
                raw = v.bin[e.target:e.target + 64]
                if e.key == A_TELEPORT_EXIT:
                    continue          # (fe_locations class, world key, locator name): checked in step 8
                refs = entry_refs(v.bin, e, _blob_len(v, e), classes)
                for ref in refs:
                    if ref in s3 or ref in new:
                        continue
                    (retail_unresolved if ref in template_refs else unresolved).add(ref)
                if tname == 'AttribSysUtils::tVaultedRefSpec':
                    # The vault file is only read when the collection is not loaded yet
                    # (retail DLC roots name vaults that do not exist, like Maloof's).
                    cls, coll = struct.unpack_from('>QQ', raw)
                    path = v.string(v.pointers[('bin', e.target + 24)])
                    want = f'{CLASS_NAMES.get(cls, "?")}\\'
                    if not path.startswith(want) or h64(path[len(want):-4]) != coll:
                        raise ValueError(f'{stem}: vaulted path {path} does not name its collection')
                    db_path = 'data/db/' + path.replace('\\', '/')
                    if db_path not in data and db_path not in context['s3db'] and (cls, coll) not in preloaded | s3:
                        raise ValueError(f'{stem}: {path} is neither packaged, in Skate 3 nor preloaded')
    if unresolved:
        raise ValueError('unresolved references: ' + ', '.join(ref_name(r) for r in sorted(unresolved)))
    findings.append('every RefSpec resolves to the package or Skate 3 db.big' +
                    (f' except {len(retail_unresolved)} copied unchanged from the retail templates '
                     '(equally unresolved there): ' + ', '.join(ref_name(r) for r in sorted(retail_unresolved))
                     if retail_unresolved else ''))

    # 5. Locators and trigger volumes referenced by each goal exist in the packaged files.
    gl_groups = {}
    for name, (_, locs) in context['arenas'].items():
        for k, n, _, _ in locs:
            if k == 'G':
                gl_groups.setdefault(n, name)
    for g in context['goals']:
        cell_locs, volumes = context['hull_cells'][g.name]
        if g.location not in gl_groups:
            raise ValueError(f'{g.name}: locator {g.location} not packaged')
        names = {n for _, n, _, _ in cell_locs}
        for n in g.indicators:
            if n not in names:
                raise ValueError(f'{g.name}: indicator locator {n} not in its hull')
        for vid, _ in (g.trigger, g.scoring):
            if vid not in volumes:
                raise ValueError(f'{g.name}: volume {vid:016X} not in its hull')
        if f'data/content/missions/{g.name}/cSim_Global.xsf' not in data:
            raise ValueError(f'{g.name}: hull not packaged')
        for props in g.props:
            if len(props) > HULL_NAME_MAX or f'data/content/missions/{props}/cSim_Global.xsf' not in data:
                raise ValueError(f'{g.name}: props hull {props} not packaged')
    n_props = sum(len(g.props) for g in context['goals'])
    findings.append(f'{len(context["goals"])} goals: challenge locators in {len(gl_groups)} packaged groups, '
                    f'indicator locators and trigger/scoring volumes in each goal hull, {n_props} props hulls')

    # 5b. Geometry: each challenge locator lies inside the goal's boundary polygon and stream tiles.
    for g in context['goals']:
        _, locs = context['arenas'][g.locator_file]
        m = next(m for k, n, _, m in locs if k == 'G' and n == g.location)
        x, z = m[12], m[14]
        polygon = xml_points(data[f'data/boundary/{g.name}.xml'], 'Point')
        stream = data[f'data/stream/{g.name}.xml']
        tiles = xml_points(stream, 'Center') + xml_points(stream, 'Tile')
        if not point_in_polygon(x, z, polygon):
            raise ValueError(f'{g.name}: challenge locator ({x:.1f}, {z:.1f}) outside its boundary')
        if not (min(t[0] for t in tiles) - 200 <= x <= max(t[0] for t in tiles) + 200 and
                min(t[1] for t in tiles) - 200 <= z <= max(t[1] for t in tiles) + 200):
            raise ValueError(f'{g.name}: challenge locator outside its stream tiles')
    findings.append('every challenge locator lies inside its boundary polygon and stream tiles')

    # 6. Labels present in every language table.
    for p, d in files:
        if '/fe/languages/' in p and '_HISTOGRAM_' not in p:
            table = read_label_table(d)
            lang = p.split('/')[3]
            for g in context['goals']:
                for label in (g.title, g.desc):
                    text = table.get(dlc.label_id(label))
                    if text is None or (not text and lang != 'japanese'):
                        raise ValueError(f'{p}: label {label} missing')
    findings.append('title/description labels present in all 9 language tables (Japanese empty, as retail)')

    # 7. Intro cutscenes: superset files, scenes, records, audio and subtitles.
    (_, _, native_scene), (_, _, native_ice) = context['newest']
    scene, ice = data[SCENE_FILE], data[ICE_FILE]
    added = len(context['intros']['goals'])
    intros.verify_superset(native_scene, native_ice, scene, ice, added)
    records, _ = intros.read_scene_table(scene)
    by_name = {intros.scene_name(r): r for r in records}
    ids = {intros.object_id(o) for _, objs in intros.read_ice(ice) for o in objs}
    resident = intros.read_resident(data[f'{AUDIO_DIR}/{RESIDENT}'])
    tables = {p.split('/')[3]: read_label_table(d) for p, d in files
              if '/fe/languages/' in p and '_HISTOGRAM_' not in p}
    for g in context['goals']:
        r = by_name.get(g.intro)
        if r is None or intros.scene_type(r) != intros.SPOT_INTRO or intros.scene_id(r) not in ids:
            raise ValueError(f'{g.name}: intro scene {g.intro} missing or not a spot intro')
        v, _ = vaults[f'data/db/challenge_local_data/{g.name}']
        e = next(e for r2 in v.records() for e in r2.entries if e.key == h64('IntroPresentationEvents'))
        if v.string(v.pointers[('bin', e.target + 12)]) != g.intro:
            raise ValueError(f'{g.name}: intro event does not name {g.intro}')
        for line in context['intros']['goals'][g.name]['audio']:
            stream = line['file']
            name = './' + stream.rsplit('/', 1)[1][:-4] + '.snr'
            if stream not in data or len(resident.get(name, b'')) != intros.SNR_SIZE:
                raise ValueError(f'{g.name}: line {line["file"]} not packaged with its header')
            label = line.get('subtitle_label')
            for lang, table in (tables.items() if label else ()):
                text = table.get(dlc.label_id(label))
                if text is None or (not text and lang != 'japanese'):
                    raise ValueError(f'{g.name}: subtitle {label} missing in {lang}')
    audio_files = {p for p in paths if p.startswith(AUDIO_DIR + '/')}
    streams = sorted(p for p in audio_files if p.endswith('.sns'))
    if len(resident) != len(streams) or audio_files != set(streams) | {f'{AUDIO_DIR}/{RESIDENT}'} or \
            {'./' + p.rsplit('/', 1)[1][:-4] + '.snr' for p in streams} != set(resident):
        raise ValueError('resident headers and streams differ')
    for a in context['intros']['animations']:
        if a not in data:
            raise ValueError(f'animation {a} not packaged')
    fw_v, _ = vaults[f'data/db/{FRAMEWORK_VAULT}']
    audio = next(r for r in fw_v.records() if r.cls == h64('aud_bigfiles'))
    values = {e.key: (fw_v.string(e.target) if e.target is not None else e.value) for e in audio.entries}
    if values.get(A_AUD_FOLDER) != AUDIO_FOLDER or values.get(A_AUD_SLOT) != AUDIO_SLOT or \
            {values.get(k) for k in (A_AUD_RESIDENT, A_AUD_RESIDENT_B, A_AUD_RESIDENT_C)} != {RESIDENT} or \
            A_AUD_FRENCH in values or A_AUD_GERMAN in values or not values.get(A_AUD_DLC):
        raise ValueError('audio bank record did not read back')
    # 8. Teleport markers: hull volume = trigger, locators moved in the packaged arena, records.
    arena = data[f'{LOCATOR_DIR}/{TELEPORT_ARENA}']
    groups = {n: m for k, n, _, m in eb0009_locators(arena) if k == 'G'}
    goal_locators = {g.location for g in context['goals']}
    world_known = (h64('world'), WORLD_KEY) in s3
    for t, rep in zip(TELEPORTS, context['teleports']):
        n = t['name']
        xsf = data.get(f'data/content/missions/{n}/cSim_Global.xsf')
        if xsf is None:
            raise ValueError(f'{n}: trigger hull not packaged')
        volumes = cell_contents(xsf)[1]
        if volumes != [int(rep['volume_id'], 16)]:
            raise ValueError(f'{n}: hull volumes {volumes} differ from the trigger')
        for locator, pos in ((t['start_locator'], t['start']), (t['exit_locator'], t['exit'])):
            m = groups.get(locator)
            if m is None or max(abs(a - b) for a, b in zip(m[12:15], pos)) > 1e-3:
                raise ValueError(f'{n}: locator {locator} not at {pos}')
            if locator in goal_locators:
                raise ValueError(f'{n}: locator {locator} is a goal start')
        v, _ = vaults[f'data/db/challenge_local_data/{n}']
        (lr,) = list(v.records())
        by_key = {e.key: e for e in lr.entries}
        trig, ext = by_key[A_TELEPORT_TRIGGER], by_key[A_TELEPORT_EXIT]
        if struct.unpack_from('>Q', v.bin, trig.target + 8)[0] != int(rep['volume_id'], 16):
            raise ValueError(f'{n}: TeleporterTrigger does not name the hull volume')
        if struct.unpack_from('>QQ', v.bin, ext.target + 8) != (teleports.FE_LOCATIONS_CLASS, WORLD_KEY) or \
                v.string(v.pointers[('bin', ext.target + 32)]) != t['exit_locator']:
            raise ValueError(f'{n}: TeleportExitLocation does not name the exit locator')
    if not world_known:
        raise ValueError('world/dist_downtown missing from Skate 3 db.big')
    findings.append(f'{len(TELEPORTS)} teleport markers: trigger volumes in their hulls, start/exit locators moved '
                    f'in {TELEPORT_ARENA}, records name them; world dist_downtown resolves')

    n = context['intros']
    findings.append(f'{added} intro scenes appended to {n["native_scene"]["origin"]} scene_'
                    f'{n["native_scene"]["number"]:03d} / ice_{n["native_ice"]["number"]:03d} with every native '
                    f'record and object unchanged; {len(streams)} lines (slot {AUDIO_SLOT}), '
                    f'{len(n["animations"])} animations, subtitles in every language table')
    return findings


def xml_points(raw, tag):
    import re
    return [tuple(float(c) for c in m.split(b',')) for m in re.findall(rf'<{tag}>([^<]*)</{tag}>'.encode(), raw)]


def point_in_polygon(x, z, polygon):
    inside = False
    for (x1, z1), (x2, z2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (z1 > z) != (z2 > z) and x < x1 + (z - z1) * (x2 - x1) / (z2 - z1):
            inside = not inside
    return inside


def _blob_len(v, e):
    if e.flags & 0x0200:
        n, cap, size = struct.unpack_from('>HHH', v.bin, e.target)
        return 8 + max(n, cap) * size
    return TYPE_SIZE.get(TYPE_NAMES.get(e.type, ''), 16)


def catalog_ids(path, data):
    """Cell ids of a CMAP catalog (.xsm rows of 104 bytes after a 32-byte header)."""
    if not path.endswith('.xsm') or data[:4] != b'CMAP':
        return set()
    count = struct.unpack_from('>I', data, 16)[0]
    return {struct.unpack_from('>Q', data, 32 + 104 * i)[0] for i in range(count)}


def collision_report(native, files):
    """Goal trigger volume ids, SFIL asset GUIDs and cell ids against Skate 3's own missions."""
    s3m = read_archive(native / 'content/missions.big', lambda n: n.endswith(('.xsf', '.xsm')))
    s3_volumes, s3_guids, s3_cells = set(), set(), set()
    for path, data in s3m.items():
        s3_cells |= catalog_ids(path, data)
        if path.endswith('.xsf'):
            for guid, _, (arena, _) in assets(data):
                s3_guids.add(guid)
                s3_volumes.update(eb0019_volumes(arena))
    volumes, guids, cells = set(), set(), set()
    for path, data in files:
        cells |= catalog_ids(path, data)
        if path.endswith('.xsf'):
            for guid, _, (arena, _) in assets(data):
                guids.add(guid)
                volumes.update(eb0019_volumes(arena))
    return dict(volumes=sorted(volumes & s3_volumes), guids=sorted(guids & s3_guids),
                cells=sorted((cells & s3_cells) - {0}),
                counts=(len(volumes), len(s3_volumes), len(guids), len(s3_guids), len(cells), len(s3_cells)))


# ---- install --------------------------------------------------------------------------------
def install(root, files, manifest):
    content = root / '0000000000000000' / f'{dlc.TITLE_ID:08X}'
    package = content / '00000002' / PACKAGE_DIR
    header = content / 'Headers' / '00000002' / f'{PACKAGE_DIR}.header'
    stage = root / 'mods' / 'skate2' / STAGE
    (stage / MARKER).unlink(missing_ok=True)
    archive = dlc.eb3_archive(files)
    if read_dlc_archive(archive) != {p: d for p, d in files}:
        raise ValueError('archive does not read back')
    package.mkdir(parents=True, exist_ok=True)
    (package / f'{PACK}_00000000.big').write_bytes(archive)
    data = bytearray(332)                      # XCONTENT_AGGREGATE_DATA + native-endian license mask
    struct.pack_into('>II', data, 0, 1, 2)     # HDD, marketplace content
    struct.pack_into('>I', data, 0x140, dlc.TITLE_ID)
    struct.pack_into('<I', data, 0x148, 0xFFFFFFFF)
    data[8:0x108] = DISPLAY_NAME.encode('utf-16-be').ljust(0x100, b'\0')
    data[0x108:0x134] = PACKAGE_DIR.encode('ascii').ljust(0x2C, b'\0')
    header.parent.mkdir(parents=True, exist_ok=True)
    header.write_bytes(bytes(data))
    if (stage / 'loose').exists():
        shutil.rmtree(stage / 'loose')
    for path, payload in files:
        target = stage / 'loose' / path.lstrip('./')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    manifest = dict(manifest, archive_bytes=len(archive),
                    archive_sha256=hashlib.sha256(archive).hexdigest())
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='ascii')
    lines = ['NSV_GOALS_V1', str(len(manifest['goals'])),
             f'root {h64(ROOT):016X} {ROOT}', f'group {h64(TYPE_GROUP):016X} {TYPE_GROUP}']
    lines += [f"goal {g['collection_key']} {g['name']} {g['source']}" for g in manifest['goals']]
    # Every other challenge collection of the package: src/ keeps all of them off other maps.
    lines.append(f'group {h64(TELEPORT_GROUP):016X} {TELEPORT_GROUP}')
    # exit=: the teleport's destination locator. Its World is Downtown's (NSV's template), so src/
    # tells this in-city hop from a Skate 3 Locations pick in Downtown (which must reload) by name.
    lines += [f"teleport {h64(t['name']):016X} {t['name']} exit={t['exit_locator']}" for t in TELEPORTS]
    # intro <scene id> <goal> speaker= line=: Reda's line in each Skate 2 intro. Skate 3's cutscene player
    # reads a scene's speech from ICE channels 138-140 at scene start (sub_82826C98); the Skate 2 intro
    # objects leave them empty (log 356), so src/ hands it this line instead.
    for name, intro in sorted(manifest['intros']['goals'].items()):
        speech = [a for a in intro['audio'] if a['speaker'] == REDA]
        if len(speech) != 1:
            raise ValueError(f'{name}: expected one Reda line in its intro, got {len(speech)}')
        lines.append(f"intro {intro['id']} {name} speaker={REDA} line={speech[0]['line']}")
    (stage / 'goals.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
    (stage / MARKER).write_bytes(b'NSV Skate 2 Own the Spot goals package\n')
    return package, header, stage, archive


def uninstall(root):
    content = root / '0000000000000000' / f'{dlc.TITLE_ID:08X}'
    package = content / '00000002' / PACKAGE_DIR
    header = content / 'Headers' / '00000002' / f'{PACKAGE_DIR}.header'
    stage = root / 'mods' / 'skate2' / STAGE
    shutil.rmtree(package, ignore_errors=True)
    header.unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)
    print(f'removed {package}, {header.name}, {stage}')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--root', type=Path, default=REPO / 'out' / 'build' / 'relwithdebinfo')
    ap.add_argument('--source', type=Path, default=REPO.parent / 'Skate 2' / 'data')
    ap.add_argument('--native-data', type=Path, help='Skate 3 data directory (default: <root>/game/data)')
    ap.add_argument('--uninstall', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='build and validate without installing')
    args = ap.parse_args()
    if args.uninstall:
        uninstall(args.root)
        return
    native = args.native_data or args.root / 'game' / 'data'
    if not (args.source / 'db/db.big').is_file() or not (native / 'big/db.big').is_file():
        ap.error('original Skate 2 data and Skate 3 game data are required')
    files, manifest, context = build_package(args.source, native, args.root)
    findings = validate(files, manifest, context)
    clash = collision_report(native, files)
    for kind in ('volumes', 'guids', 'cells'):
        if clash[kind]:
            raise ValueError(f'{kind} shared with Skate 3 missions: ' + ', '.join(f'{v:016X}' for v in clash[kind]))
    n = clash['counts']
    findings.append(f'no collision with Skate 3 missions: trigger volumes {n[0]} vs {n[1]}, '
                    f'SFIL asset GUIDs {n[2]} vs {n[3]}, catalog cell ids {n[4]} vs {n[5]}')
    manifest['validation'] = findings
    for line in findings:
        print('check:', line)
    if args.dry_run:
        print(f'dry run: {len(files)} files, {len(manifest["goals"])} goals')
        return
    package, header, stage, archive = install(args.root, files, manifest)
    print(f'installed {package.name}/{PACK}_00000000.big ({len(archive)} bytes, {len(files)} files, '
          f'{len(manifest["goals"])} goals); header {header.name}; staging {stage}')


if __name__ == '__main__':
    main()
