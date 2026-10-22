"""New San Van component updates and the in-game updater's release rules.

- The C++ (src/skate3_nsv_components.cpp) and Python (tools/nsv_components.py)
  component tables must be identical: the game decides what is out of date,
  the preparation rebuilds it.
- A component update publishes only the rebuilt components and merges the
  install manifest; everything else in the installation is left byte-identical.
- The C++ status logic (v0.2 install -> v0.2a needs only the DLC maps) and the
  updater's version/asset rules are compiled and run as fixtures.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import nsv_components  # noqa: E402
import prepare_new_san_van as install  # noqa: E402
import extract_update_archive  # noqa: E402


def cpp_table():
    text = (ROOT / 'src/skate3_nsv_components.cpp').read_text(encoding='utf8')
    body = text[text.index('static const std::vector<NsvComponent> components = {'):]
    body = body[:body.index('};')]
    entries = []
    for m in re.finditer(r'\{"(\w+)",\s*\{(.*?)\},\s*(true|false),\s*(true|false),\s*\{(.*?)\}\}', body, re.S):
        entries.append(dict(id=m[1], markers=tuple(re.findall(r'"([^"]+)"', m[2])),
                            needs_skate2=m[3] == 'true', needs_skate2_dlc=m[4] == 'true',
                            dependents=tuple(re.findall(r'"([^"]+)"', m[5]))))
    return entries


def compiler():
    return (os.environ.get('CXX') or shutil.which('clang++-20') or shutil.which('clang++')
            or shutil.which('c++'))


def compile_run(test, source, *extra_sources):
    cxx = compiler()
    if not cxx:
        test.skipTest('no C++ compiler')
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        fixture = work / 'fixture.cpp'
        fixture.write_text(source, encoding='utf8')
        binary = work / ('fixture.exe' if os.name == 'nt' else 'fixture')
        subprocess.run([cxx, '-std=c++20', '-O1', '-I', str(ROOT / 'src'), str(fixture),
                        *map(str, extra_sources), '-o', str(binary)], check=True)
        return subprocess.run([str(binary), str(work)], check=True, capture_output=True,
                              text=True).stdout


def make_install(root: Path, content_root: Path, dlc_livingworld='V9'):
    """A complete installation as the given build would have written it."""
    mods = root / 'mods/skate2'
    for c in nsv_components.COMPONENTS:
        for out in c.outputs:
            path = nsv_components.resolve(out, mods, content_root / nsv_components.CONTENT)
            if '.' in Path(out).name and not out.startswith('CONTENT/00000002/'):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'installed ' + out.encode())
            else:
                path.mkdir(parents=True, exist_ok=True)
                (path / 'asset.bin').write_bytes(b'installed ' + out.encode())
        for marker in c.markers:
            marker = marker.replace('LIVINGWORLD_COMPLETE_V9', 'LIVINGWORLD_COMPLETE_' + dlc_livingworld)
            path = nsv_components.resolve(marker, mods, content_root / nsv_components.CONTENT)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b'marker')
    files = {}
    for top in (mods, content_root / nsv_components.CONTENT):
        for p in top.rglob('*'):
            if p.is_file():
                key = p.relative_to(root if top == mods else content_root).as_posix()
                files[key] = dict(bytes=p.stat().st_size, sha256='old')
    (mods / install.MANIFEST).write_text(json.dumps(dict(version=1, files=files)), encoding='utf8')


class ComponentTableTest(unittest.TestCase):
    def test_cpp_and_python_tables_match(self):
        cpp = cpp_table()
        py = [dict(id=c.id, markers=c.markers, needs_skate2=c.needs_skate2,
                   needs_skate2_dlc=c.needs_skate2_dlc, dependents=c.dependents)
              for c in nsv_components.COMPONENTS]
        self.assertEqual(cpp, py)

    def test_markers_match_the_previous_whole_install_check(self):
        # The pre-component installer required exactly these files (v0.2a markers).
        previous = {'world-support-gvr-v1/BAM_COMPLETE_V2', 'world-support-gvr-v1/NAVIGATION_COMPLETE_V4',
                    'proxy-v1/manifest.json', 'sky-water-v1/SKY_WATER_COMPLETE_V1',
                    'jumbotron/attract.s2jv', 'dlc-maps-v1/DLC_MAPS_COMPLETE_V1',
                    *(f'dlc-maps-v1/{s}/NAVIGATION_COMPLETE_V4' for s in nsv_components.DLC_STREAMS),
                    *(f'dlc-maps-v1/{s}/LIVINGWORLD_COMPLETE_V9' for s in nsv_components.DLC_STREAMS),
                    'audio-v1/AUDIO_COMPLETE_V2', 'livingworld-v1/LIVINGWORLD_COMPLETE_V1',
                    'nsv_spawns.txt',
                    'CONTENT/00000002/4E53564D4F442D4E455753414E56414E2D534B3245/newsanvan_00000000.big'}
        self.assertEqual({m for c in nsv_components.COMPONENTS for m in c.markers}, previous)

    def test_closure_and_needs(self):
        self.assertEqual(nsv_components.parse('dlc_maps'), ['dlc_maps'])
        self.assertEqual(nsv_components.parse('audio'), ['audio', 'menu'])
        self.assertEqual(nsv_components.parse('sky'), ['sky', 'city', 'dlc_maps', 'livingworld', 'menu'])
        self.assertEqual(nsv_components.parse('all'), list(nsv_components.ORDER))
        self.assertEqual(nsv_components.needs(['menu']), dict(skate2=False, skate2_dlc=False))
        self.assertEqual(nsv_components.needs(['dlc_maps']), dict(skate2=True, skate2_dlc=True))
        with self.assertRaises(ValueError):
            nsv_components.parse('bake')


class SelectivePublishTest(unittest.TestCase):
    def test_dlc_maps_update_replaces_only_dlc_maps_and_merges_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root, content_root, stage = base / 'app', base / 'user', base / 'stage'
            make_install(root, content_root, dlc_livingworld='V5')
            before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
            before.update({p: p.read_bytes() for p in content_root.rglob('*') if p.is_file()})
            new = stage / 'mods/skate2/dlc-maps-v1'
            for marker in nsv_components.BY_ID['dlc_maps'].markers:
                p = stage / 'mods/skate2' / marker
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'rebuilt')
            (new / 'DLC_School/cell.xsf').write_bytes(b'rebuilt cell')
            install.write_manifest(stage, root, ['dlc_maps'])
            install.publish(stage, root, content_root, ['dlc_maps'])
            mods = root / 'mods/skate2'
            self.assertTrue((mods / 'dlc-maps-v1/DLC_School/LIVINGWORLD_COMPLETE_V9').is_file())
            self.assertFalse((mods / 'dlc-maps-v1/DLC_School/LIVINGWORLD_COMPLETE_V5').exists())
            for p, data in before.items():
                if 'dlc-maps-v1' in p.as_posix() or p.name == install.MANIFEST:
                    continue
                self.assertEqual(p.read_bytes(), data, p)
            manifest = json.loads((mods / install.MANIFEST).read_text(encoding='utf8'))
            self.assertEqual(manifest['version'], 2)
            self.assertIn('dlc_maps', manifest['components'])
            keys = manifest['files']
            self.assertIn('mods/skate2/dlc-maps-v1/DLC_School/cell.xsf', keys)
            self.assertNotIn('mods/skate2/dlc-maps-v1/DLC_School/LIVINGWORLD_COMPLETE_V5', keys)
            self.assertEqual(keys['mods/skate2/audio-v1/asset.bin']['sha256'], 'old')
            self.assertFalse((root / 'install-state/nsv-transaction.json').exists())
            for c in nsv_components.COMPONENTS:
                self.assertEqual(nsv_components.missing_markers(
                    c, mods, content_root / nsv_components.CONTENT), [], c.id)

    def test_menu_update_publishes_its_content_package_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root, content_root, stage = base / 'app', base / 'user', base / 'stage'
            make_install(root, content_root)
            retail = content_root / nsv_components.CONTENT / '00000002/RETAILDLC/data.big'
            retail.parent.mkdir(parents=True)
            retail.write_bytes(b'retail')
            menu = nsv_components.BY_ID['menu']
            for out in menu.outputs:
                p = nsv_components.resolve(out, stage / 'mods/skate2', stage / nsv_components.CONTENT)
                if out.endswith('.txt') or out.endswith('.header'):
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(b'new')
                else:
                    p.mkdir(parents=True, exist_ok=True)
                    (p / 'newsanvan_00000000.big').write_bytes(b'new')
            install.write_manifest(stage, root, ['menu'])
            install.publish(stage, root, content_root, ['menu'])
            self.assertEqual(retail.read_bytes(), b'retail')
            self.assertEqual((root / 'mods/skate2/nsv_spawns.txt').read_bytes(), b'new')

    def test_full_publication_pairs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, root = Path(tmp) / 's', Path(tmp) / 'r'
            for branch in ('00000002', 'Headers/00000002'):
                (stage / install.CONTENT / branch / 'X').mkdir(parents=True)
            pairs = install.publish_pairs(stage, root, root, None)
            expected = [(stage / 'mods/skate2' / n, root / 'mods/skate2' / n)
                        for n in (*install.FOLDERS, 'nsv_spawns.txt', 'install-manifest.json')]
            expected += [(stage / install.CONTENT / b / 'X', root / install.CONTENT / b / 'X')
                         for b in ('00000002', 'Headers/00000002')]
            self.assertEqual(pairs, expected)


class CppStatusTest(unittest.TestCase):
    def test_status_of_v02_complete_and_empty_installs(self):
        source = r'''
#include "skate3_nsv_components.h"
#include <cstdio>
#include <fstream>
namespace fs = std::filesystem;
void touch(const fs::path& p){ fs::create_directories(p.parent_path()); std::ofstream(p) << "x"; }
const char* name(skate3::NsvInstallState s){
  return s==skate3::NsvInstallState::kMissing?"missing":s==skate3::NsvInstallState::kComplete?"complete":"update";}
void show(const char* label,const fs::path& app,const fs::path& user){
  auto s=skate3::GetNewSanVanStatus(app,user);
  std::printf("%s %s [%s] s2=%d dlc=%d\n",label,name(s.state),skate3::JoinNsvComponents(s.components).c_str(),
              s.needs_skate2,s.needs_skate2_dlc);}
int main(int,char** argv){
  fs::path base=argv[1], app=base/"app", user=base/"user";
  show("empty",app,user);
  for (const auto& c: skate3::NsvComponents()) for (const char* m: c.markers) {
    std::string marker=m;
    if (marker.rfind("CONTENT/",0)==0) touch(user/"0000000000000000/454108E6"/marker.substr(8));
    else touch(app/"mods/skate2"/marker);
  }
  show("complete",app,user);
  for (const char* s: {"DLC_School","DLC_Community_Center","DLC_Parkade"}) {
    fs::remove(app/"mods/skate2/dlc-maps-v1"/s/"LIVINGWORLD_COMPLETE_V9");
    touch(app/"mods/skate2/dlc-maps-v1"/s/"LIVINGWORLD_COMPLETE_V5");
  }
  show("v02",app,user);
  fs::remove(app/"mods/skate2/sky-water-v1/SKY_WATER_COMPLETE_V1");
  show("sky",app,user);
  touch(app/"install-state/nsv-transaction.json");
  show("journal",app,user);
}
'''
        out = compile_run(self, source, ROOT / 'src/skate3_nsv_components.cpp')
        lines = dict(line.split(' ', 1) for line in out.strip().splitlines())
        self.assertEqual(lines['empty'], 'missing [] s2=0 dlc=0')
        self.assertEqual(lines['complete'], 'complete [] s2=0 dlc=0')
        self.assertEqual(lines['v02'], 'update [dlc_maps] s2=1 dlc=1')
        self.assertEqual(lines['sky'], 'update [sky,city,dlc_maps,livingworld,menu] s2=1 dlc=1')
        self.assertEqual(lines['journal'], 'missing [] s2=0 dlc=0')


class UpdaterRulesTest(unittest.TestCase):
    def test_versions_assets_and_digests(self):
        source = r'''
#include "skate3_update_version.h"
#include <cstdio>
using namespace skate3::update_version;
int main(int, char**){
  Version v{}, b{};
  std::printf("%d", ParseTag("v0.2a", v)); std::printf(" %d.%d.%d", v[0],v[1],v[2]);
  std::printf(" %d", ParseTag("v0.2", v)); std::printf(" %d.%d.%d", v[0],v[1],v[2]);
  std::printf(" %d", ParseTag("V1.10c", v)); std::printf(" %d.%d.%d", v[0],v[1],v[2]);
  std::printf(" %d%d%d%d", ParseTag("v0.2-dev", v), ParseTag("0.2.1", v), ParseTag("latest", v), ParseTag("v.2", v));
  ParseNumeric("0.2.1", b); ParseTag("v0.2a", v); std::printf(" newer=%d", Newer(v, b));
  ParseTag("v0.2b", v); std::printf(" %d %s %s", Newer(v, b), Display(v).c_str(), Display(Version{0,2,0}).c_str());
  std::printf(" %d", ParseNumeric("0.2a-dev", b));
  std::printf(" w=%d%d%d l=%d%d m=%d%d",
    MatchesPlatform("Skate3Recomp-Plus-v0.2-Windows-x64.zip", Platform::kWindows),
    MatchesPlatform("Skate3Recomp-Plus-win-amd64.zip", Platform::kWindows),
    MatchesPlatform("Skate3Recomp-Plus-v0.2-Linux-x64.zip", Platform::kWindows),
    MatchesPlatform("Skate3Recomp-Plus-v0.2-Linux-x64.zip", Platform::kLinux),
    MatchesPlatform("Skate3Recomp-Plus-v0.2-Linux-x64.zip.sha256", Platform::kLinux),
    MatchesPlatform("Skate3Recomp-Plus-v0.2-macOS-arm64.zip", Platform::kMacOS),
    MatchesPlatform("Skate3Recomp-Plus-symbols-macos-arm64.zip", Platform::kMacOS));
  std::printf(" [%s] [%s]\n",
    Sha256FromDigest("sha256:242088AF958070FF81DC0E79F01B2F8644E2596BF525178DFCB4289685AEF515").c_str(),
    Sha256FromDigest("md5:abc").c_str());
}
'''
        out = compile_run(self, source).strip()
        self.assertEqual(out, '1 0.2.1 1 0.2.0 1 1.10.3 0000 newer=0 1 v0.2b v0.2 0 '
                              'w=110 l=10 m=10 '
                              '[242088af958070ff81dc0e79f01b2f8644e2596bf525178dfcb4289685aef515] []')


class ExtractTest(unittest.TestCase):
    def test_extracts_modes_and_refuses_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            archive = tmp / 'p.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                info = zipfile.ZipInfo('Skate3Recomp-Plus-linux-amd64/skate3')
                info.external_attr = (0o100755 << 16)
                z.writestr(info, b'binary')
                z.writestr('Skate3Recomp-Plus-linux-amd64/installer/tools/x.py', b'print(1)')
            out = tmp / 'out'
            self.assertEqual(extract_update_archive.extract(archive, out), 2)
            binary = out / 'Skate3Recomp-Plus-linux-amd64/skate3'
            self.assertEqual(binary.read_bytes(), b'binary')
            if os.name != 'nt':
                self.assertTrue(os.access(binary, os.X_OK))
            bad = tmp / 'bad.zip'
            with zipfile.ZipFile(bad, 'w') as z:
                z.writestr('../escape.txt', b'x')
            with self.assertRaisesRegex(ValueError, 'unsafe'):
                extract_update_archive.extract(bad, tmp / 'out2')
            self.assertFalse((tmp / 'escape.txt').exists())


class UiWordingTest(unittest.TestCase):
    def test_player_facing_text_never_says_bake(self):
        for name in ('src/skate3_setup_dialog.cpp', 'src/skate3_updater.cpp',
                     'src/skate3_nsv_installer.cpp'):
            text = (ROOT / name).read_text(encoding='utf8')
            strings = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
            offenders = [s for s in strings if re.search(r'\bbak(e|ed|ing)\b|rebake', s, re.I)]
            self.assertEqual(offenders, [], name)


if __name__ == '__main__':
    unittest.main()
