"""New San Van data components: what each one publishes, how it is versioned and what it needs.

The installed data is split into independently replaceable components. Each component's
version is the set of completion markers its builder writes; a release that changes a
builder's output bumps that builder's marker (for example LIVINGWORLD_COMPLETE_V9 -> V10) in
the builder, here, and in src/skate3_nsv_components.cpp (tools/test_nsv_components.py keeps
the two tables identical). The game then finds that component out of date at startup and
prepares only it (plus anything derived from it), asking only for the original files that
component needs.
"""
from __future__ import annotations

from dataclasses import dataclass, field

CONTENT = '0000000000000000/454108E6'
MENU_PACKAGE = '4E53564D4F442D4E455753414E56414E2D534B3245'
DLC_STREAMS = ('DLC_School', 'DLC_Community_Center', 'DLC_Parkade')


@dataclass(frozen=True)
class Component:
    id: str
    # Published paths: relative to <root>/mods/skate2, or to the content root for CONTENT/...
    outputs: tuple[str, ...]
    # Files that must exist for the installed component to be current (same roots).
    markers: tuple[str, ...]
    needs_skate2: bool = False
    needs_skate2_dlc: bool = False
    # Components built from this one's output: rebuilt whenever this one is.
    dependents: tuple[str, ...] = field(default_factory=tuple)


COMPONENTS: tuple[Component, ...] = (
    Component('sky', ('sky-water-v1',), ('sky-water-v1/SKY_WATER_COMPLETE_V1',),
              needs_skate2=True, dependents=('city', 'dlc_maps')),
    Component('city', ('world-support-gvr-v1',),
              ('world-support-gvr-v1/BAM_COMPLETE_V2', 'world-support-gvr-v1/NAVIGATION_COMPLETE_V4'),
              needs_skate2=True, dependents=('livingworld',)),
    Component('skyline', ('proxy-v1',), ('proxy-v1/manifest.json',), needs_skate2=True),
    Component('dlc_maps', ('dlc-maps-v1',),
              ('dlc-maps-v1/DLC_MAPS_COMPLETE_V1',
               *(f'dlc-maps-v1/{s}/NAVIGATION_COMPLETE_V4' for s in DLC_STREAMS),
               *(f'dlc-maps-v1/{s}/LIVINGWORLD_COMPLETE_V9' for s in DLC_STREAMS)),
              needs_skate2=True, needs_skate2_dlc=True),
    Component('audio', ('audio-v1',), ('audio-v1/AUDIO_COMPLETE_V2',),
              needs_skate2=True, needs_skate2_dlc=True, dependents=('menu',)),
    Component('livingworld', ('livingworld-v1',), ('livingworld-v1/LIVINGWORLD_COMPLETE_V1',),
              needs_skate2=True, dependents=('menu',)),
    Component('jumbotron', ('jumbotron',), ('jumbotron/attract.s2jv',), needs_skate2=True),
    Component('menu', ('nsv_spawns.txt', f'CONTENT/00000002/{MENU_PACKAGE}',
                       f'CONTENT/Headers/00000002/{MENU_PACKAGE}.header'),
              ('nsv_spawns.txt', f'CONTENT/00000002/{MENU_PACKAGE}/newsanvan_00000000.big')),
)
BY_ID = {c.id: c for c in COMPONENTS}
ORDER = tuple(c.id for c in COMPONENTS)


def closure(ids) -> list[str]:
    """Requested components plus everything derived from them, in build order."""
    wanted = set()
    pending = list(ids)
    while pending:
        cid = pending.pop()
        if cid not in BY_ID:
            raise ValueError(f'Unknown New San Van component: {cid}')
        if cid not in wanted:
            wanted.add(cid)
            pending.extend(BY_ID[cid].dependents)
    return [cid for cid in ORDER if cid in wanted]


def parse(text: str) -> list[str]:
    if text.strip() in ('', 'all'):
        return list(ORDER)
    return closure(t.strip() for t in text.split(',') if t.strip())


def needs(ids) -> dict[str, bool]:
    return dict(skate2=any(BY_ID[i].needs_skate2 for i in ids),
                skate2_dlc=any(BY_ID[i].needs_skate2_dlc for i in ids))


def resolve(path: str, mods, content):
    """A component path under the installation (mods = <root>/mods/skate2)."""
    return content / path[len('CONTENT/'):] if path.startswith('CONTENT/') else mods / path


def missing_markers(component: Component, mods, content) -> list[str]:
    return [m for m in component.markers if not resolve(m, mods, content).is_file()]
