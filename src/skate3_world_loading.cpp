#include "generated/skate3_init.h"
#include "native/skate3_native_guest_read.h"
#include "skate3_world_loading.h"
#include "native/skate3_native_world_policy.h"
#include "skate3_cheats.h"
#include "skate3_pedestrian_speech.h"
#include "skate3_menu_probe.h"
#include "skate3_perf_recorder.h"
#include "skate3_freeze_capture.h"
#include "skate3_release_budget.h"
#include "skate3_stream_pump.h"
#include "skate3_sim_extent.h"
#include "skate3_stream_heap.h"
#include "skate3_texture_identity.h"
#include "skate3_texture_lookup.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include <rex/cvar.h>
#include <rex/system/kernel_state.h>
#include <rex/logging.h>
#include <rex/filesystem/devices/host_path_device.h>
#include <rex/filesystem/vfs.h>

REXCVAR_DEFINE_BOOL(skate3_world_load_probe, false, "Skate 3",
                    "Log world loader configuration, state transitions and asset requests, "
                    "plus New San Van world streaming: game state and event transitions "
                    "(world-transition), streamed cell activation (world-cell-activation), "
                    "allocation and BAM read samples, proxy collection state and the "
                    "per-world audio paint/ambience detail");
REXCVAR_DEFINE_BOOL(skate3_skate2_enabled, true, "Skate 3",
                    "Enable New San Van map loading; restart required");

REXCVAR_DEFINE_BOOL(skate3_skate2_render_gate_test, true, "Skate 3",
                    "Allow New San Van camera residency to render during gameplay");
REXCVAR_DEFINE_BOOL(skate3_skate2_release_budget, true, "Skate 3",
                    "Bounded extra deferred-release processing when New San Van collision loading is fenced");
REXCVAR_DEFINE_BOOL(skate3_skate2_sim_priority, true, "Skate 3",
                    "Service pending New San Van collision changes before presentation unloads");
REXCVAR_DEFINE_BOOL(skate3_skate2_stream_pump, true, "Skate 3",
                    "Advance ready streaming handoffs within a bounded update while New San Van collision waits");
REXCVAR_DEFINE_BOOL(skate3_skate2_early_gather, true, "Skate 3",
                    "Gather current New San Van residency requests before the gameplay scheduler selects work");
REXCVAR_DEFINE_BOOL(skate3_skate2_release_background, true, "Skate 3",
                    "Drain eligible New San Van cleanup within the existing budget before collision needs it");
REXCVAR_DEFINE_BOOL(skate3_skate2_asset_budget, true, "Skate 3",
                    "Advance ready New San Van asset submissions within a bounded time budget");
REXCVAR_DEFINE_BOOL(skate3_skate2_sim_grid_extent, true, "Skate 3",
                    "Match collision neighbor query extent to New San Van's 200-unit grid; reload map required");
REXCVAR_DEFINE_BOOL(skate3_skate2_stream_heap_test, true, "Skate 3",
                    "64 MiB streaming heap for New San Van residency; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_audio_heap, true, "Skate 3",
                    "34 MiB audio heap for New San Van's added sound banks and voices; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_texture_identity, true, "Skate 3",
                    "Translate New San Van low-detail texture binding identities; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_world_support, true, "Skate 3",
                    "Load the complete prepared New San Van map; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_original_proxy, true, "Skate 3",
                    "Load prepared original Skate 2 distant proxies with guest visibility switching; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_original_sky_water, true, "Skate 3",
                    "Load Skate 2's original skydome and bay/open ocean models in New San Van; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_sky, true, "Skate 3",
                    "New San Vanelona sky: true uses Skate 2's original skybox and clouds, false draws "
                    "Skate 3's default skybox and clouds on the same dome (native renderer). Skate 2's "
                    "distant backdrop and bay stay in both; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_night, false, "Skate 3",
                    "New San Vanelona at night: the Sanitarium's night sky and moon on the same dome, "
                    "and the world's baked light dimmed to the Sanitarium's night levels (native "
                    "renderer). Takes priority over skate3_skate2_sky; needs the Sanitarium download "
                    "content; restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_world_audio, true, "Skate 3",
                    "Give New San Van and the Skate 2 DLC maps Skate 2's own ambient sound emitters "
                    "(tools/build_skate2_audio.py); restart required");
REXCVAR_DEFINE_BOOL(skate3_skate2_painted_audio, true, "Skate 3",
                    "Translate New San Van's painted audio (ambience beds, sound zones, reverb) "
                    "so Skate 3's audio plays it (tools/build_skate2_audio.py); restart required");

REXCVAR_DEFINE_BOOL(skate3_skate2_livingworld_probe, false, "Skate 3",
                    "Log New San Van census, road activation, actor creation and navigation");
REXCVAR_DEFINE_BOOL(skate3_skate2_original_pedestrians, true, "Skate 3",
                    "Enable original New San Van pedestrians when prepared navigation is installed");
REXCVAR_DEFINE_BOOL(skate3_skate2_original_vehicles, true, "Skate 3",
                    "Load the original vehicle roster on New San Van roads; restart required");

namespace {
// Populated during Install before guest execution; immutable thereafter.
std::unordered_set<uint64_t> bam_lighting_ids;
std::atomic<bool> skate2_ready{false};
std::atomic<bool> load_requested{false};
std::atomic<bool> skate2_loading{false};
std::atomic<bool> bam_gameplay{false};
std::atomic<uint64_t> render_world_identity{0};
std::atomic<uint64_t> indoor_world_identity{0};
// Debug-only snapshots; never retain or dereference a guest resource unguarded.
std::atomic<uint8_t*> freeze_base{nullptr};
std::atomic<uint32_t> freeze_gameplay{0}, freeze_stream_manager{0}, freeze_pause_result{~0u};
std::atomic<uint64_t> template_world{0};
// Steady-clock ticks after which an armed template_world whose world load
// never started is dropped, so a later retail load of it stays retail.
std::atomic<int64_t> template_deadline{0};
// Raw +40/+48 before a Locations-menu load, restored if it never starts.
std::atomic<uint64_t> menu_previous_loaded{0}, menu_previous_world{0};
std::atomic<uint64_t> bam_proxy_target_world{0};
bool bam_navigation_ready = false;  // Required for imported pedestrian census.
std::vector<bool> skate2_navigation_ready, skate2_actor_streams;  // Immutable after Install.
bool bam_proxy_ready = false;  // Installed before guest execution.
std::atomic<uint32_t> bam_proxy_manager{0};
std::atomic<uint64_t> bam_model_target_world{0};
bool bam_models_ready = false;  // Installed before guest execution.
// skate3_skate2_sky=false / skate3_skate2_night, latched at startup: the
// native renderer draws the Skate 2 sky dome with Skate 3's or the
// Sanitarium's night sky textures (PrepareSky).
bool bam_skate3_sky = false;  // Installed before guest execution.
bool bam_night_sky = false;   // Installed before guest execution.
std::array<std::atomic<uint64_t>, 3> view_stage_calls{};
// Skate 2 world streams New San Van can run: index 0 is BAM, then the DLC
// maps from tools/build_skate2_dlc_maps.py. Filled during Install before
// guest execution; immutable thereafter.
std::vector<std::string> skate2_streams;
// The stream the armed template load turns into, and the one running now.
std::atomic<int> requested_stream{0};
std::atomic<int> loaded_stream{-1};
bool LivingWorldStream(int stream) {
  return stream >= 0 && stream < int(skate2_actor_streams.size()) && skate2_actor_streams[stream];
}
bool StreamNavigationReady(int stream) {
  return stream >= 0 && stream < int(skate2_navigation_ready.size()) && skate2_navigation_ready[stream];
}
// City plus the three prepared Classic DLC maps; excludes retail/other DLC.
bool LivingWorldProbe() {
  return REXCVAR_GET(skate3_skate2_livingworld_probe) && skate2_loading.load() &&
         LivingWorldStream(loaded_stream.load());
}
std::array<std::atomic<uint64_t>, 2> lw_samples{}, lw_selections{}, lw_selected{};
std::array<std::atomic<uint64_t>, 2> lw_factories{}, lw_created{};
bool lw_assets_ready = false;  // Immutable after Install.
std::atomic<bool> lw_load_group_pending{false}, lw_load_group_loaded{false};
std::atomic<uint64_t> lw_template_group_key{0};
std::unordered_map<uint32_t, uint64_t> lw_paint_values;
std::unordered_set<uint32_t> lw_ped_paint_values;
bool lw_ped_assets_ready = false;
std::unordered_set<uint64_t> lw_texture_ids;
std::unordered_set<std::string> lw_traffic_banks;
std::atomic<bool> lw_original_audio{false};
// Traffic banks are global, not reloaded by the audio-world setter. Only
// transitions into/out of the original traffic world request a replacement.
std::atomic<uint64_t> lw_audio_generation{0};
struct VehicleFactoryTrace {
  const char* stage = "closest-road";
  uint32_t lane_queries = 0, available_lanes = 0, attrib_cache = 0;
  uint32_t closest = 0, superroad = 0;
  float route_distance = -1.f, route_length = -1.f, road_normal_y = 0.f;
  uint32_t lookup_count = 0, lookup_pointer = 0;
};
thread_local VehicleFactoryTrace* lw_factory_trace = nullptr;
constexpr uint64_t kOriginalVehiclesGroup = 0xB1560C92E651080D;
bool OriginalVehicles() {
  return lw_assets_ready && REXCVAR_GET(skate3_skate2_original_vehicles) &&
         skate2_loading.load() && LivingWorldStream(loaded_stream.load());
}
bool PreparedPedestriansFor(int stream) {
  return lw_assets_ready && lw_ped_assets_ready && StreamNavigationReady(stream) &&
         LivingWorldStream(stream) && REXCVAR_GET(skate3_skate2_original_pedestrians);
}
bool PreparedPedestrians() {
  const int stream = loaded_stream.load();
  return PreparedPedestriansFor(stream < 0 ? 0 : stream);
}
bool OriginalPedestrians() {
  return skate2_loading.load() && PreparedPedestriansFor(loaded_stream.load());
}
uint64_t OriginalActorGroup() {
  if (PreparedPedestriansFor(requested_stream.load()))
    return REXCVAR_GET(skate3_skate2_original_vehicles) ? 0x8D5FB7F6957AA24D : 0x9757A109BBCB975F;
  return kOriginalVehiclesGroup;
}
std::atomic<uint64_t> lw_road_fixups{0}, lw_road_requests{0}, lw_nav_queries{0}, lw_nav_invalid{0};
bool LivingWorldLogSample(uint64_t n) { return n <= 16 || (n & (n - 1)) == 0; }

// Presentation templates are process-wide metadata. Imported records remain
// isolated; only their native vehicle bind-pose cache is prepared alongside
// the native templates. Actor/census/audio enablement still requires BAM.
std::atomic<uint32_t> lw_pose_manager{0}, lw_pose_prepared_manager{0};
thread_local bool lw_pose_query = false;
thread_local uint32_t lw_pose_models = 0, lw_pose_matches = 0, lw_pose_misses = 0;
thread_local std::string lw_pose_recipe;

bool TrafficPresentationProbe() {
  // A retail control sample after visiting BAM is logging only.
  return lw_assets_ready && REXCVAR_GET(skate3_skate2_livingworld_probe) &&
         (LivingWorldProbe() || (!skate2_loading.load() && lw_audio_generation.load() != 0));
}

int StreamIndex(const std::string& name) {
  for (size_t i = 0; i < skate2_streams.size(); ++i) {
    if (skate2_streams[i] == name) return int(i);
  }
  return -1;
}
// Skate 2 emitter maps per stream (tools/build_skate2_audio.py worlds.txt),
// by the role prefix of the template district's map names. Filled during
// Install; immutable thereafter.
constexpr std::array<std::string_view, 5> kAudioRoles = {"sfx", "music", "reverb", "crowds",
                                                         "speakers"};
std::unordered_map<std::string, std::array<std::string, 5>> skate2_audio_maps;
// The stream whose maps the audio world currently loading takes, or -1.
std::atomic<int> audio_stream{-1};
uint64_t dlc_ambience_key = 0, dlc_downtown_ambience_key = 0;  // prepared audio metadata
// Skate 2 WorldPainter translation (worldpainter.txt from
// tools/build_skate2_audio.py; formats in tools/skate2_worldpainter.py):
// Skate 2 eLayerID -> Skate 3 layer key, Skate 2 paint value -> Skate 3
// record key, and the record names for the log. Filled during Install;
// immutable thereafter. The guest key table is created once, on the first
// translated dictionary.
std::unordered_map<uint32_t, uint64_t> paint_layers;
std::unordered_map<uint32_t, uint64_t> paint_values;
std::unordered_map<uint64_t, std::string> paint_names;
std::unordered_map<uint32_t, uint32_t> paint_slots;  // paint value -> guest key
uint32_t paint_zero_slot = 0;                         // the zero key (no value)
std::once_flag paint_table_once;
std::atomic<uint32_t> paint_layer_count{0}, paint_dictionary_count{0};
constexpr uint32_t kWorldState = 0x830B7AE8;
constexpr uint64_t kNewSanVanWorld = 0xF715A377BE05BD01;  // h64("dist_newsanvan")
constexpr uint64_t kDowntownWorld = 0x4773016A41B2E05E;   // h64("dist_downtown")

// The BAM template hook needs the base stream layout; the three city
// districts have it, and Downtown is the template used since log012.
bool IsCityDistrict(uint64_t world) {
  return world == kDowntownWorld || world == 0xA8B1F47090679026ull ||  // dist_industrial
         world == 0xCB473E68A2EAFABAull;                              // dist_university
}
constexpr std::array<const char*, 4> kSkyWaterFiles = {
    "S2_DIST_skybox.rx2", "S2_DIST_skybox_Textures.rx2",
    "S2_DIST_Water.rx2", "S2_DIST_Water_Textures.rx2"};

std::string GuestString(uint8_t* base, uint32_t address) {
  std::string result;
  for (uint32_t i = 0; i < 256 && address >= 0x10000 && address <= 0xffffff00; ++i) {
    char value = 0;
    if (!skate3::native_scene::GuestTryCopy(&value, base + address + i, 1)) {
      return "<unreadable>";
    }
    if (!value) return result;
    result.push_back(value >= 32 && value < 127 ? value : '?');
  }
  return "<invalid or unterminated>";
}

bool GuestU32(uint8_t* base, uint32_t address, uint32_t& value) {
  if (address < 0x10000 || address > 0xfffffffb ||
      !skate3::native_scene::GuestTryCopy(&value, base + address, sizeof(value))) return false;
  value = __builtin_bswap32(value);
  return true;
}

uint64_t WorldField(uint8_t* base, uint32_t offset) {
  uint64_t value;
  std::memcpy(&value, base + kWorldState + offset, 8);
  return __builtin_bswap64(value);
}

bool GuestU64(uint8_t* base, uint32_t address, uint64_t& value) {
  uint32_t high = 0, low = 0;
  if (!GuestU32(base, address, high) || !GuestU32(base, address + 4, low)) return false;
  value = (uint64_t(high) << 32) | low;
  return true;
}

void StoreGuestU32(uint8_t* base, uint32_t address, uint32_t value) {
  value = __builtin_bswap32(value);
  std::memcpy(base + address, &value, 4);
}

// Career autosave while New San Van is loaded. Every career autosave trigger
// tests the game object's autosave switch ([0x83084690]+35) before it queues a
// career save (request kind 0): the cMsgInitAutosave handler 825DE0B8 (posted
// by the ICE TriggerAutoSave action and career events) and the event triggers
// 82544828, 8260BCF8, 8260D158, 82656198, 826E4FC0 and 8272F0F8. With the
// switch clear each completes without a save, as after "continue without
// saving". A profile load sets it (8251A940, 82710570); sign-out and profile
// resets clear it together with +34 and +48. A career saved in New San Van
// keeps New San Van's coordinates, and the next session starts there inside
// Skate 3's Downtown, the template district: a void. The switch is held clear
// while New San Van or a Skate 2 DLC map is loaded or armed and handed back
// once gameplay runs in a Skate 3 world. Photo, replay and park saves (kinds
// 1, 3, 4) do not test it and still save.
constexpr uint32_t kGameObject = 0x83084690;
std::mutex autosave_mutex;
struct AutosaveHold {
  bool active = false;
  uint8_t enabled = 0;  // the game's own value, handed back on return
  uint8_t profile = 0;  // +34 as last seen
};
AutosaveHold autosave_hold;  // guarded by autosave_mutex

bool CareerAutosaveHeld() {
  std::lock_guard lock(autosave_mutex);
  return autosave_hold.active;
}

void HoldCareerAutosave(uint8_t* base, bool may_release) {
  const bool hold = skate2_loading.load() || template_world.load() != 0;
  std::lock_guard lock(autosave_mutex);
  if (!hold && !autosave_hold.active) return;
  uint32_t game = 0;
  if (!GuestU32(base, kGameObject, game) || game < 0x10000 || game > 0xFFFFFF00) return;
  uint8_t* flags = base + game;
  if (autosave_hold.active) {
    // The game's own changes while held: a profile reset clears +34 with +35;
    // a profile load or the boot flow sets +35.
    if (autosave_hold.profile && !flags[34]) autosave_hold.enabled = 0;
    if (flags[35]) autosave_hold.enabled = flags[35];
    autosave_hold.profile = flags[34];
  }
  if (hold) {
    if (!autosave_hold.active) {
      autosave_hold = {true, flags[35], flags[34]};
      REXLOG_INFO("nsv-autosave: career autosave held off while New San Van is loaded (was {})",
                  flags[35] ? "on" : "off");
    }
    flags[35] = 0;
  } else if (may_release) {
    flags[35] = autosave_hold.enabled;
    autosave_hold.active = false;
    REXLOG_INFO("nsv-autosave: career autosave handed back in Skate 3 ({})",
                autosave_hold.enabled ? "on" : "off");
  }
}

// 827B9CF0 creates per-model vehicle animation/bind-pose records. Its normal
// query sees descendants of the native vehicles root, never our isolated root.
// Call the same native builder with that one query redirected to the imported
// root. The builder refuses duplicate keys; neither native records nor the
// native VehiclesDB (byte-identical in both titles) are replaced.
void PrepareOriginalVehiclePoses(PPCContext& context, uint8_t* base, uint32_t manager) {
  if (!manager || lw_pose_query || lw_pose_prepared_manager.load() == manager ||
      !lw_assets_ready || !REXCVAR_GET(skate3_skate2_original_vehicles)) return;
  auto find = context;
  find.r3.u64 = 0x4E740A6670F241A7;  // livingworld_models
  find.r4.u64 = kOriginalVehiclesGroup;  // same name; distinct class
  __imp__sub_82B69B08(find, base);
  if (!find.r3.u32) return;  // DLC vault not registered yet; retry on its asset callback.
  uint32_t before = 0, after = 0;
  GuestU32(base, manager + 0x2ACC, before);
  auto call = context;
  call.r3.u32 = manager;
  lw_pose_models = lw_pose_matches = lw_pose_misses = 0;
  lw_pose_query = true;
  __imp__sub_827B9CF0(call, base);
  lw_pose_query = false;
  GuestU32(base, manager + 0x2ACC, after);
  const bool complete = lw_pose_models != 0 && lw_pose_matches == lw_pose_models && lw_pose_misses == 0;
  if (complete) lw_pose_prepared_manager.store(manager);
  static std::atomic<uint64_t> attempts{0};
  if (complete || LivingWorldLogSample(++attempts))
    REXLOG_INFO("skate2-lw: original vehicle poses manager={:08X} descendants={} matched={} missed={} cache={}->{} complete={}",
              manager, lw_pose_models, lw_pose_matches, lw_pose_misses, before, after, complete);
}

// New San Van's painted audio (tools/build_skate2_audio.py). The Skate 2
// ambience beds Skate 3 does not ship are served where Skate 3 streams any
// bed its own archive lacks: data\audio\<n>_dlc_ambience.snr (resident
// header) and .sns (stream), sub_824D3990, the path its DLC parks use for
// beds 24 and 25. worldpainter.txt is the paint translation the cell-load
// hooks (sub_82964BC8 and friends) apply.
void InstallPaintedAudio(rex::filesystem::VirtualFileSystem* vfs,
                         const std::filesystem::path& audio) {
  int beds = 0;
  std::error_code ec;
  for (const auto& entry : std::filesystem::directory_iterator(audio / "beds", ec)) {
    const std::string name = entry.path().filename().string();
    const auto underscore = name.find('_');
    const std::string_view suffix =
        underscore == std::string::npos ? std::string_view() : std::string_view(name).substr(underscore);
    const bool bed = entry.is_regular_file() && underscore > 0 && underscore < 8 &&
        std::all_of(name.begin(), name.begin() + underscore,
                    [](char c) { return c >= '0' && c <= '9'; }) &&
        (suffix == "_dlc_ambience.snr" || suffix == "_dlc_ambience.sns");
    if (!bed) continue;
    const std::string guest = "\\Device\\Harddisk0\\Partition1\\data\\audio\\" + name;
    if (vfs->RegisterSymbolicLink(guest, "skate2audio:\\beds\\" + name) && vfs->ResolvePath(guest)) {
      ++beds;
    } else {
      REXLOG_WARN("skate2-audio: ambience bed {} not mounted", name);
    }
  }
  std::ifstream file(audio / "worldpainter.txt");
  std::string line;
  while (std::getline(file, line)) {
    std::istringstream fields(line.substr(0, line.find('#')));
    std::string kind, key_text, name;
    uint32_t id = 0;
    if (!(fields >> kind >> id >> key_text >> name)) continue;
    char* end = nullptr;
    const uint64_t key = std::strtoull(key_text.c_str(), &end, 16);
    if (key == 0 || !end || *end != '\0' || (kind != "layer" && kind != "value")) {
      REXLOG_WARN("skate2-audio: invalid worldpainter.txt line '{}'", line);
      continue;
    }
    (kind == "layer" ? paint_layers : paint_values)[id] = key;
    paint_names[key] = name;
    if (name == "aud_wp_ambiences/nsv_old_town") dlc_ambience_key = key;
    if (name == "aud_wp_ambiences/nsv_dt_main") dlc_downtown_ambience_key = key;
  }
  if (paint_layers.empty() || paint_values.empty()) {
    REXLOG_WARN("skate2-audio: {} has no paint translation; run tools/build_skate2_audio.py",
                (audio / "worldpainter.txt").string());
    paint_layers.clear();
    paint_values.clear();
  }
  REXLOG_INFO("skate2-audio: painted audio ready: {} layers, {} paint values, {} loose bed files",
              paint_layers.size(), paint_values.size(), beds);
}
// Unique directories isolate original dependencies from the retail archives.
// Validate real file opens before enabling census. Every refusal is explicit.
void InstallOriginalLivingWorld(rex::filesystem::VirtualFileSystem* vfs,
                               const std::filesystem::path& folder) {
  if (!REXCVAR_GET(skate3_skate2_original_vehicles) &&
      !REXCVAR_GET(skate3_skate2_original_pedestrians)) {
    REXLOG_INFO("skate2-lw: original actors disabled by settings");
    return;
  }
  const auto refuse = [&](const std::string& reason) {
    REXLOG_ERROR("skate2-lw: original traffic install refused: {}; folder={}", reason, folder.string());
  };
  if (!std::filesystem::is_regular_file(folder / "LIVINGWORLD_COMPLETE_V1")) {
    refuse("missing completion marker"); return;
  }
  std::ifstream ids(folder / "texture_ids.txt");
  std::string magic, token;
  uint32_t count = 0;
  if (!(ids >> magic >> count) || magic != "NSV_LW_TEXTURE_IDS_V1" || !count || count > 4096) {
    refuse("invalid texture inventory header"); return;
  }
  std::unordered_set<uint64_t> parsed;
  for (uint32_t i = 0; i < count; ++i) {
    uint64_t id = 0;
    if (!(ids >> token) || token.size() != 16) { refuse("truncated texture inventory"); return; }
    const auto result = std::from_chars(token.data(), token.data() + token.size(), id, 16);
    if (result.ec != std::errc{} || result.ptr != token.data() + token.size() || !id ||
        !parsed.insert(id).second) { refuse("invalid/duplicate texture ID"); return; }
  }
  if (ids >> token) { refuse("trailing texture inventory data"); return; }
  auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2lw:", folder / "loose", true);
  if (!device->Initialize() || !vfs->RegisterDevice(std::move(device))) {
    refuse("could not register original asset device"); return;
  }
  for (const char* relative : {"data\\content\\nsv_s2_vehicle", "data\\content\\nsv_s2_livingworld",
       "data\\content\\recipe\\nsv_s2_vehicle", "data\\content\\recipe\\nsv_s2_livingworld",
       "data\\audio\\nsv_s2_traffic", "data\\audio\\nsv_s2_speech"}) {
    if (std::string_view(relative) == "data\\audio\\nsv_s2_speech" &&
        !std::filesystem::is_directory(folder / "loose" / relative)) continue;
    const std::string guest = "\\Device\\Harddisk0\\Partition1\\" + std::string(relative);
    if (!vfs->RegisterSymbolicLink(guest, "skate2lw:\\" + std::string(relative)) || !vfs->ResolvePath(guest)) {
      refuse("asset directory did not resolve: " + guest); return;
    }
  }
  std::ifstream inventory(folder / "runtime_files.txt");
  uint32_t files = 0;
  if (!(inventory >> magic >> files) || (magic != "NSV_LW_FILES_V1" && magic != "NSV_LW_FILES_V2") || !files || files > 4096) {
    refuse("invalid runtime file inventory"); return;
  }
  const bool complete_actor_inventory = magic == "NSV_LW_FILES_V2";
  std::unordered_set<std::string> traffic_banks, speech_files;
  for (uint32_t i = 0; i < files; ++i) {
    std::string relative, header;
    uint64_t size = 0;
    if (!(inventory >> relative >> size >> header) || relative.find("..") != std::string::npos ||
        !relative.starts_with("data/") || !size || header.size() != 16) {
      refuse("invalid runtime file inventory entry"); return;
    }
    std::replace(relative.begin(), relative.end(), '/', '\\');
    const std::string guest = "\\Device\\Harddisk0\\Partition1\\" + relative;
    rex::filesystem::File* file = nullptr;
    rex::filesystem::FileAction action{};
    const auto status = vfs->OpenFile(nullptr, guest, rex::filesystem::FileDisposition::kOpen,
        rex::filesystem::FileAccess::kGenericRead, false, true, &file, &action);
    std::array<uint8_t, 8> actual{};
    size_t read = 0;
    uint64_t expected = 0;
    const auto hex = std::from_chars(header.data(), header.data() + header.size(), expected, 16);
    bool valid = hex.ec == std::errc{} && hex.ptr == header.data() + header.size() &&
        status == 0 && file && file->entry()->absolute_path() == "skate2lw:\\" + relative &&
        file->entry()->size() == size && file->ReadSync(actual, 0, &read) == 0 && read == 8;
    for (size_t j = 0; j < actual.size(); ++j) valid = valid && actual[j] == uint8_t(expected >> (56 - 8 * j));
    if (file) file->Destroy();
    if (!valid) { refuse("file open/header/size check failed: " + guest); return; }
    const std::string prefix = "data\\audio\\nsv_s2_traffic\\";
    if (relative.starts_with(prefix)) traffic_banks.insert(relative.substr(prefix.size()));
    const std::string speech_prefix = "data\\audio\\nsv_s2_speech\\";
    if (relative.starts_with(speech_prefix)) speech_files.insert(relative.substr(speech_prefix.size()));
  }
  if ((inventory >> token) || traffic_banks.size() != 10) { refuse("incomplete traffic banks"); return; }
  std::ifstream file(folder / "worldpainter.txt");
  std::string line;
  std::unordered_map<uint32_t, uint64_t> values;
  std::unordered_map<uint64_t, std::string> names;
  std::unordered_set<uint32_t> ped_values;
  while (std::getline(file, line)) {
    std::istringstream fields(line.substr(0, line.find('#')));
    std::string kind, key_text, name;
    uint32_t id = 0;
    if (!(fields >> kind >> id >> key_text >> name) || kind != "value" ||
        !name.starts_with("nsv_s2_") ||
        (!name.ends_with("_vehicles_census") && !name.ends_with("_peds_census"))) continue;
    if (name.ends_with("_peds_census")) ped_values.insert(id);
    uint64_t key = 0;
    const auto result = std::from_chars(key_text.data(), key_text.data() + key_text.size(), key, 16);
    if (result.ec != std::errc{} || result.ptr != key_text.data() + key_text.size() ||
        !key || paint_values.contains(id) || !values.emplace(id, key).second) {
      refuse("invalid/conflicting census value: " + line); return;
    }
    names.emplace(key, name);
  }
  if (values.size() != 18 || ped_values.size() != 10) {
    refuse("incomplete original actor census manifest: values=" + std::to_string(values.size()) +
           " pedestrians=" + std::to_string(ped_values.size()) + " (expected 18/10)"); return;
  }
  lw_ped_assets_ready = complete_actor_inventory;
  lw_ped_paint_values = std::move(ped_values);
  lw_texture_ids = std::move(parsed);
  lw_traffic_banks = std::move(traffic_banks);
  lw_paint_values = values;
  paint_values.insert(values.begin(), values.end());
  paint_names.insert(names.begin(), names.end());
  paint_layers[2] = 0x4CAF11B5298E3919;
  paint_names[0x4CAF11B5298E3919] = "livingworld_npc_census";
  paint_layers[5] = 0x7FC53B5B51129A55;  // livingworld_vehicle_census
  paint_names[0x7FC53B5B51129A55] = "livingworld_vehicle_census";
  lw_assets_ready = true;
  if (speech_files == std::unordered_set<std::string>{"livingworld_Events.evt",
      "livingworldhdr.big", "livingworldsth.big", "samples.big"})
    skate3::pedestrian_speech::Install(folder);
  REXLOG_INFO("skate2-lw: original vehicle assets ready; census_values={} texture_files={} density=original",
              values.size(), lw_texture_ids.size());
  REXLOG_INFO("skate2-lw: dependency opens verified={} traffic_banks={} pedestrians_ready={} navigation_ready={}",
              files, lw_traffic_banks.size(), PreparedPedestrians(), bam_navigation_ready);
}

}  // namespace

void skate3::world_loading::Install(rex::filesystem::VirtualFileSystem* vfs,
                                    const std::filesystem::path& app_root) {
  skate2_ready.store(false);
  skate2_streams = {"BAM"};
  if (!vfs || !REXCVAR_GET(skate3_skate2_enabled)) return;
  // The prepared map is the release artifact. Its catalogs, global cells,
  // spatial cells and lighting inventory form one complete mounted directory.
  // An original extraction is a build input, never a runtime fallback.
  const auto bam = app_root / "mods" / "skate2" / "world-support-gvr-v1";
  if (!REXCVAR_GET(skate3_skate2_world_support)) {
    REXLOG_WARN("skate2: prepared New San Van map disabled by settings");
    return;
  }
  constexpr std::array<const char*, 8> map_files = {
      "BAM_Pres.xsm", "BAM_Pres.xst", "cPres_Global.xsf",
      "BAM_Sim.xsm", "BAM_Sim.xst", "cSim_Global.xsf",
      "BAM_Tex.xsm", "BAM_Tex.xst"};
  bool map_ready = std::filesystem::is_regular_file(bam / "BAM_COMPLETE_V2");
  for (const auto* name : map_files) {
    std::ifstream file(bam / name, std::ios::binary);
    std::array<char, 8> header{};
    const std::string_view n(name);
    const auto* magic = n.ends_with(".xsm") ? "CMAP\0\0\0\3" :
        n.ends_with(".xst") ? "ATOC\0\0\0\3" : "SFIL\0\0\0\3";
    map_ready = bool(file.read(header.data(), header.size())) &&
        std::memcmp(header.data(), magic, header.size()) == 0 && map_ready;
  }
  std::unordered_set<uint64_t> lighting;
  std::ifstream ids(bam / "lighting_texture_ids.txt");
  map_ready = skate3::world_loading::ReadBamLightingIds(ids, lighting) && map_ready;
  if (!map_ready) {
    REXLOG_ERROR("skate2: prepared map absent/incomplete at {}; run tools/prepare_skate2_world.py", bam.string());
    return;
  }
  // Census needs the authored water/road/crosswalk policy. Older terrain
  // classification bakes must be regenerated by the installer.
  {
    std::ifstream navigation(bam / "NAVIGATION_COMPLETE_V4");
    std::string magic, extra;
    uint32_t cells = 0, polygons = 0;
    bam_navigation_ready = bool(navigation >> magic >> cells >> polygons) &&
        magic == "NSV_NAVIGATION_V4" && cells > 0 && cells <= 4096 &&
        polygons > 0 && polygons <= 1000000 && !(navigation >> extra);
    if (bam_navigation_ready)
      REXLOG_INFO("skate2-lw: prepared navigation cells={} polygons={} policy=water-pavement-crosswalks-v4", cells, polygons);
    else
      REXLOG_WARN("skate2-lw: classified navigation absent/incomplete; New San Van pedestrians disabled; reinstall New San Van");
  }
  skate2_navigation_ready = {bam_navigation_ready};
  skate2_actor_streams = {true};
  auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2support:", bam, true);
  if (!device->Initialize() || !vfs->RegisterDevice(std::move(device))) {
    REXLOG_ERROR("skate2: could not mount prepared map {}", bam.string());
    return;
  }
  constexpr auto path = "\\Device\\Harddisk0\\Partition1\\data\\content\\world\\stream\\BAM";
  if (!vfs->RegisterSymbolicLink(path, "skate2support:")) {
    REXLOG_ERROR("skate2: prepared map VFS alias could not be registered");
    return;
  }
  // OpenFile, not just ResolvePath: parent-first opens must select this
  // directory for the complete map, including the texture collection.
  for (const auto* name : map_files) {
    rex::filesystem::File* file = nullptr;
    rex::filesystem::FileAction action{};
    const auto status = vfs->OpenFile(nullptr, std::string(path) + "\\" + name,
        rex::filesystem::FileDisposition::kOpen, rex::filesystem::FileAccess::kGenericRead,
        false, true, &file, &action);
    const bool valid = status == 0 && file &&
        file->entry()->absolute_path() == std::string("skate2support:\\") + name &&
        file->entry()->size() == std::filesystem::file_size(bam / name);
    if (file) file->Destroy();
    if (!valid) {
      REXLOG_ERROR("skate2: prepared map file routing check failed: {}", name);
      return;
    }
  }
  if (REXCVAR_GET(skate3_skate2_texture_identity))
    bam_lighting_ids = std::move(lighting);
  REXLOG_INFO("skate2: prepared BAM directory enabled; files={} lighting IDs={}",
              bam.string(), bam_lighting_ids.size());
  if (REXCVAR_GET(skate3_skate2_original_proxy)) {
    const auto proxy = app_root / "mods" / "skate2" / "proxy-v1" /
        "data" / "content" / "world" / "stream" / "PROXY";
    std::ifstream catalog(proxy / "PROXY_Pres.xsm", std::ios::binary);
    std::array<char, 8> h{};
    if (catalog.read(h.data(), h.size()) && std::memcmp(h.data(), "CMAP\0\0\0\3", 8) == 0) {
      auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2proxy:", proxy, true);
      if (device->Initialize() && vfs->RegisterDevice(std::move(device))) {
        constexpr auto proxy_path = "\\Device\\Harddisk0\\Partition1\\data\\content\\world\\skate2_proxy\\PROXY";
        bam_proxy_ready = vfs->RegisterSymbolicLink(proxy_path, "skate2proxy:") &&
            vfs->ResolvePath(std::string(proxy_path) + "\\PROXY_Pres.xsm");
      }
    }
    REXLOG_INFO("skate2: original PROXY loose collection ready={}", bam_proxy_ready);
  }
  if (REXCVAR_GET(skate3_skate2_original_sky_water)) {
    // Skate 2 loads its skydome and ocean as fixed district models; Skate 3
    // loads one data-driven model pair per world (828A1460). A separate route
    // keeps native world/models paths untouched. Unique S2_ basenames matter:
    // the guest derives resource IDs from the basename (82CFF8B0), and native
    // miscload.big has a different DIST_Water.rx2.
    const auto models = app_root / "mods" / "skate2" / "sky-water-v1";
    bool ready = std::filesystem::is_regular_file(models / "SKY_WATER_COMPLETE_V1");
    for (const char* name : kSkyWaterFiles) {
      std::ifstream file(models / name, std::ios::binary);
      std::array<char, 8> h{};
      ready = ready && bool(file.read(h.data(), h.size())) &&
          std::memcmp(h.data(), "\x89RW4xb2\0", 8) == 0;
    }
    std::unordered_set<uint64_t> model_lighting;
    std::ifstream model_ids(models / "lighting_texture_ids.txt");
    ready = ready && skate3::world_loading::ReadBamLightingIds(model_ids, model_lighting);
    if (ready) {
      auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2models:", models, true);
      constexpr auto models_path = "\\Device\\Harddisk0\\Partition1\\data\\content\\world\\skate2_models";
      ready = device->Initialize() && vfs->RegisterDevice(std::move(device)) &&
          vfs->RegisterSymbolicLink(models_path, "skate2models:");
      for (const char* name : kSkyWaterFiles) {
        if (!ready) break;
        rex::filesystem::File* file = nullptr;
        rex::filesystem::FileAction action{};
        const auto status = vfs->OpenFile(nullptr, std::string(models_path) + "\\" + name,
            rex::filesystem::FileDisposition::kOpen, rex::filesystem::FileAccess::kGenericRead,
            false, true, &file, &action);
        ready = status == 0 && file &&
            file->entry()->absolute_path() == std::string("skate2models:\\") + name &&
            file->entry()->size() == std::filesystem::file_size(models / name);
        if (file) file->Destroy();
      }
    }
    // The hashed lighting pages (two with bit62 set) need the same exact-ID
    // binding bridge as BAM's own lighting exports.
    if (ready && REXCVAR_GET(skate3_skate2_texture_identity))
      bam_lighting_ids.insert(model_lighting.begin(), model_lighting.end());
    bam_models_ready = ready;
    bam_night_sky = ready && REXCVAR_GET(skate3_skate2_night);
    bam_skate3_sky = ready && !bam_night_sky && !REXCVAR_GET(skate3_skate2_sky);
    if (ready) {
      REXLOG_INFO("skate2: original skydome/ocean models ready; lighting IDs={}; sky={}",
                  bam_lighting_ids.size(),
                  bam_night_sky    ? "night (Sanitarium sky textures)"
                  : bam_skate3_sky ? "Skate 3 (sky textures)"
                                   : "Skate 2");
    } else {
      REXLOG_WARN("skate2: original skydome/ocean models absent/invalid at {}; run tools/build_skate2_sky_water.py; template world model retained",
                  models.string());
    }
  }
  // Skate 2's DLC maps (tools/build_skate2_dlc_maps.py): each world stream is
  // mounted at data/content/world/stream/<Stream>, the same layout as BAM,
  // and runs in BAM's place for the New San Vanelona entries that name it.
  const auto dlc = app_root / "mods" / "skate2" / "dlc-maps-v1";
  if (std::filesystem::is_regular_file(dlc / "DLC_MAPS_COMPLETE_V1")) {
    std::unordered_set<uint64_t> dlc_lighting;
    std::ifstream dlc_ids(dlc / "lighting_texture_ids.txt");
    auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2dlc:", dlc, true);
    if (skate3::world_loading::ReadBamLightingIds(dlc_ids, dlc_lighting) &&
        device->Initialize() && vfs->RegisterDevice(std::move(device))) {
      std::string mounted;
      for (const auto& entry : std::filesystem::directory_iterator(dlc)) {
        if (!entry.is_directory()) continue;
        const std::string stream = entry.path().filename().string();
        const std::string guest =
            "\\Device\\Harddisk0\\Partition1\\data\\content\\world\\stream\\" + stream;
        std::ifstream catalog(entry.path() / (stream + "_Pres.xsm"), std::ios::binary);
        std::array<char, 8> h{};
        if (stream.size() < 64 && catalog.read(h.data(), h.size()) &&
            std::memcmp(h.data(), "CMAP\0\0\0\3", 8) == 0 &&
            vfs->RegisterSymbolicLink(guest, "skate2dlc:\\" + stream) &&
            vfs->ResolvePath(guest + "\\" + stream + "_Pres.xsm")) {
          skate2_streams.push_back(stream);
          const bool classic = stream == "DLC_School" || stream == "DLC_Community_Center" || stream == "DLC_Parkade";
          uint32_t cells = 0, polygons = 0, census = 0, roads = 0;
          std::string nav_magic, actor_magic, extra;
          std::ifstream nav(entry.path() / "NAVIGATION_COMPLETE_V4");
          std::ifstream actors(entry.path() / "LIVINGWORLD_COMPLETE_V9");
          const bool ready = classic && bool(nav >> nav_magic >> cells >> polygons) &&
              nav_magic == "NSV_NAVIGATION_V4" && cells > 0 && cells <= 4096 &&
              polygons > 0 && polygons <= 1000000 && !(nav >> extra) &&
              bool(actors >> actor_magic >> census >> roads) && actor_magic == "NSV_DLC_LIVINGWORLD_V9" &&
              census > 0 && census <= 4096 && roads > 0 && roads <= 4096 && !(actors >> extra);
          skate2_navigation_ready.push_back(ready);
          skate2_actor_streams.push_back(ready);
          if (classic)
            REXLOG_INFO("skate2-lw: DLC livingworld stream={} ready={} census_cells={} roads={} navigation_cells={} polygons={} policy=water-roadways-crosswalks-v4",
                        stream, ready, census, roads, cells, polygons);
          mounted += (mounted.empty() ? "" : ", ") + stream;
        } else {
          REXLOG_WARN("skate2: DLC map stream {} not mounted", stream);
        }
      }
      // Their hashed lighting pages need the same exact-ID binding bridge.
      if (REXCVAR_GET(skate3_skate2_texture_identity))
        bam_lighting_ids.insert(dlc_lighting.begin(), dlc_lighting.end());
      REXLOG_INFO("skate2: DLC map streams ready: {}", mounted);
    } else {
      REXLOG_WARN("skate2: DLC maps at {} invalid; run tools/build_skate2_dlc_maps.py",
                  dlc.string());
    }
  }
  // Skate 2's world audio maps (tools/build_skate2_audio.py), mounted at
  // data\audio\skate2. While New San Van or a DLC map loads, the audio world
  // takes them in place of the template district's maps (sub_828DCCF8).
  const auto audio = app_root / "mods" / "skate2" / "audio-v1";
  if (REXCVAR_GET(skate3_skate2_world_audio) && std::filesystem::is_directory(audio) &&
      !std::filesystem::is_regular_file(audio / "AUDIO_COMPLETE_V2")) {
    REXLOG_WARN("skate2-audio: {} is incomplete or older than this build; "
                "run tools/build_skate2_audio.py and tools/build_nsv_dlc.py", audio.string());
  }
  if (REXCVAR_GET(skate3_skate2_world_audio) &&
      std::filesystem::is_regular_file(audio / "AUDIO_COMPLETE_V2")) {
    constexpr std::string_view guest = "\\Device\\Harddisk0\\Partition1\\data\\audio\\skate2";
    auto device = std::make_unique<rex::filesystem::HostPathDevice>("skate2audio:", audio, true);
    if (device->Initialize() && vfs->RegisterDevice(std::move(device)) &&
        vfs->RegisterSymbolicLink(std::string(guest), "skate2audio:")) {
      std::ifstream list(audio / "worlds.txt");
      std::string line, ready;
      while (std::getline(list, line)) {
        std::istringstream fields(line.substr(0, line.find('#')));
        std::string stream, assignment;
        if (!(fields >> stream)) continue;
        std::array<std::string, kAudioRoles.size()> maps;
        bool valid = StreamIndex(stream) >= 0;
        while (valid && fields >> assignment) {
          const auto equals = assignment.find('=');
          const auto role = std::find(kAudioRoles.begin(), kAudioRoles.end(),
                                      std::string_view(assignment).substr(0, equals));
          const std::string map = equals == std::string::npos ? "" : assignment.substr(equals + 1);
          std::error_code ec;
          const auto size = std::filesystem::file_size(audio / map, ec);
          // A map is a u32 count and 72-byte records.
          valid = role != kAudioRoles.end() && !map.empty() && map.size() < 64 && !ec &&
                  size >= 4 && (size - 4) % 72 == 0 &&
                  vfs->ResolvePath(std::string(guest) + "\\" + map);
          if (valid) maps[role - kAudioRoles.begin()] = map;
        }
        if (valid && std::none_of(maps.begin(), maps.end(), [](const auto& m) { return m.empty(); })) {
          skate2_audio_maps[stream] = maps;
          ready += (ready.empty() ? "" : ", ") + stream;
        } else {
          REXLOG_WARN("skate2-audio: invalid line '{}'; run tools/build_skate2_audio.py", line);
        }
      }
      REXLOG_INFO("skate2-audio: Skate 2 world audio maps ready: {}", ready);
      if (REXCVAR_GET(skate3_skate2_painted_audio)) {
        InstallPaintedAudio(vfs, audio);
      }
    } else {
      REXLOG_WARN("skate2-audio: could not mount {}", audio.string());
    }
  }
  InstallOriginalLivingWorld(vfs, app_root / "mods" / "skate2" / "livingworld-v1");
  skate2_ready.store(true);
  REXLOG_INFO("skate2: prepared BAM files ready; press Ctrl+F7 during unpaused gameplay; files={}",
              bam.string());
}

uint64_t skate3::world_loading::RenderWorldIdentity() {
  return render_world_identity.load(std::memory_order_acquire);
}

uint32_t skate3::world_loading::NativeIndoorLightingMap(uint64_t world_identity) {
  const uint64_t paired = indoor_world_identity.load(std::memory_order_acquire);
  return (paired >> 3) == world_identity ? uint32_t(paired & 7) : 0;
}

bool skate3::world_loading::IsSkate2Gameplay() {
  return bam_gameplay.load() && skate2_loading.load();
}

// BAM uses the existing proxy lifecycle (828A12B8 / 828A1310 / 828A13B0).
// Only its startup paths differ. The original 828A1058 reads those paths from
// the template world's attributes; Danny Way has no proxy collection at all.
// Reproduce its no-archive startup branch with the normal path formatter and
// collection registrar, leaving load, activation, association and teardown to
// the guest. The pending target is consumed once, not inferred from a stale
// gameplay flag while transitioning back to another map.
extern "C" REX_FUNC(sub_828A1058) {
  const uint64_t target = bam_proxy_target_world.load();
  if (!bam_proxy_ready || !target || target != ctx.r4.u64) {
    __imp__sub_828A1058(ctx, base);
    return;
  }
  const uint32_t object = ctx.r3.u32;
  uint32_t owner = 0, manager = 0, collection_manager = 0, state = 0, archive = 0;
  if (!GuestU32(base, 0x830CEB84, owner) ||
      !GuestU32(base, owner + 61020, manager) ||
      !GuestU32(base, manager + 16, collection_manager) || collection_manager < 0x10000 ||
      !GuestU32(base, manager + 40, state) || state != 8 ||
      !GuestU32(base, object + 1044, archive) || archive != 0 ||
      GuestString(base, manager + 576) != "proxy" || ctx.r1.u32 < 0x10000) {
    bam_proxy_target_world.store(0);
    REXLOG_ERROR("skate2-proxy: startup layout/lifecycle check failed; retaining native startup; manager={:08X} state={} archive={:08X}", manager, state, archive);
    __imp__sub_828A1058(ctx, base);
    return;
  }
  bam_proxy_target_world.store(0);
  REX_STORE_U64(object + 8, target);
  base[object + 16] = 0;  // No BIG mount: VFS provides the verified loose files.
  constexpr char parent[] = "data/content/world/skate2_proxy";
  constexpr char folder[] = "PROXY";
  std::memcpy(base + object + 272, parent, sizeof(parent));
  std::memcpy(base + object + 528, folder, sizeof(folder));
  std::memcpy(base + object + 784, parent, sizeof(parent));
  auto call = ctx;
  const uint32_t frame = (ctx.r1.u32 - 256) & ~15u;
  REX_STORE_U32(frame, ctx.r1.u32);
  call.r1.u64 = frame;
  call.r3.u64 = 0;  // Pres, as in the original startup branch.
  call.r4.u64 = object + 784;
  call.r5.u64 = object + 528;
  call.r6.u64 = manager + 64;
  call.r7.u64 = manager + 320;
  sub_828D8240(call, base);
  call.r3.u64 = frame + 128;
  call.r4.u64 = collection_manager;
  call.r5.u64 = manager + 64;
  call.r6.u64 = manager + 320;
  call.r7.u64 = 0;
  sub_82C97620(call, base);
  const uint64_t registration = REX_LOAD_U64(frame + 128);
  REX_STORE_U64(manager + 32, registration);
  REX_STORE_U32(manager + 40, 0);
  base[object + 1040] = 0;
  bam_proxy_manager.store(manager);
  REXLOG_INFO("skate2-proxy: registered original collection manager={:08X} id={:016X} folder='{}' catalog='{}' suffix='{}'",
      manager, registration, GuestString(base, manager + 64),
      GuestString(base, manager + 320), GuestString(base, manager + 576));
}

// World model component (vtable slots 82315A20..): Start 828A1460 reads three
// world attributes into +16 root ("data/content"), +272 model and +528 texture
// pack names, then queues "<root>/<name>.rx2" for each; OnStarted 828A1830 adds
// the loaded pair to the world (82C9CAB8), Stop 828A18B8 removes it (82C9CB50)
// and Release 828A1940 drops both requests. Every step re-hashes the stored
// names, so substituting them during Start keeps the whole lifecycle coherent.
// New San Van inherits the template world's pair (Danny Way: its sky, trees,
// mountains and mist). Skate 2 instead loads two fixed district models, the
// skydome and the ocean: the real component carries the skydome, and a second
// instance of the same guest code, on a transient guest-stack object holding
// only these fields, carries the ocean through the mirrored lifecycle.
// Skate 3's sky (skate3_skate2_sky=false) and the night sky
// (skate3_skate2_night) add nothing here: the native renderer draws this
// Skate 2 dome with their textures (PrepareSky below).
namespace {
struct ModelNames {
  uint32_t object;
  const char* model;
  const char* textures;
  unsigned written;
};
thread_local ModelNames* model_names = nullptr;
constexpr ModelNames kSkybox{0, "world/skate2_models/S2_DIST_skybox",
                             "world/skate2_models/S2_DIST_skybox_Textures", 0};
constexpr ModelNames kOcean{0, "world/skate2_models/S2_DIST_Water",
                            "world/skate2_models/S2_DIST_Water_Textures", 0};
constexpr uint32_t kModelManagerOwner = 0x830CEB84;  // 828A1460's r31/r29 source
constexpr uint32_t kShadowBytes = 800;               // fields end at +528+256

// Resource IDs: textures before models, as 828A1460 queues them.
struct SkyWaterState {
  uint32_t object = 0;
  uint64_t world = 0;
  std::array<uint64_t, 4> ids{};  // sky textures, sky model, ocean textures, ocean model
  std::array<bool, 4> added{};
  bool pending = false;
  std::chrono::steady_clock::time_point since{};
};
std::mutex sky_water_mutex;
SkyWaterState sky_water;
constexpr std::array<const char*, 4> kSkyWaterRoles = {
    "sky-textures", "sky-model", "ocean-textures", "ocean-model"};

// A guest call frame below the current stack pointer: [frame, frame+32) is
// the callee's linkage area, the shadow object follows it.
struct GuestScratch {
  uint32_t frame, shadow;
};
GuestScratch MakeScratch(uint8_t* base, uint32_t sp) {
  const uint32_t shadow = (sp - kShadowBytes - 64) & ~15u;
  const uint32_t frame = (shadow - 64) & ~15u;
  REX_STORE_U32(frame, sp);
  return {frame, shadow};
}

void WriteShadow(uint8_t* base, uint32_t shadow, uint32_t real, uint64_t world) {
  std::memset(base + shadow, 0, kShadowBytes);
  std::memcpy(base + shadow, base + real, 4);  // vtable word, for completeness
  REX_STORE_U64(shadow + 8, world);
  std::memcpy(base + shadow + 16, base + real + 16, 256);  // same root
  std::memcpy(base + shadow + 272, kOcean.model, std::strlen(kOcean.model) + 1);
  std::memcpy(base + shadow + 528, kOcean.textures, std::strlen(kOcean.textures) + 1);
}

uint64_t ModelId(PPCContext& ctx, uint8_t* base, uint32_t frame, uint32_t out, uint32_t name) {
  auto call = ctx;
  call.r1.u64 = frame;
  call.r3.u64 = out;
  call.r4.u64 = name;
  sub_82CFF8B0(call, base);
  return REX_LOAD_U64(out);
}

bool ModelLoaded(PPCContext& ctx, uint8_t* base, uint32_t frame, uint32_t manager, uint64_t id) {
  auto call = ctx;
  call.r1.u64 = frame;
  call.r3.u64 = manager;
  call.r4.u64 = id;
  sub_82CFD4C8(call, base);
  return call.r3.u32 != 0;
}

bool SkyWaterMirror(uint32_t object) {
  std::lock_guard lock(sky_water_mutex);
  return object != 0 && object == sky_water.object;
}
}  // namespace

// Only overrides while a Start above has published names for this thread;
// otherwise a plain pass-through of the leaf string copy.
extern "C" REX_FUNC(sub_82AE95C0) {
  ModelNames* names = model_names;
  const uint32_t destination = ctx.r3.u32;
  const uint32_t limit = ctx.r5.u32;
  __imp__sub_82AE95C0(ctx, base);
  if (!names || limit < 256) return;
  const char* value = destination == names->object + 272 ? names->model :
      destination == names->object + 528 ? names->textures : nullptr;
  if (!value) return;
  std::memcpy(base + destination, value, std::strlen(value) + 1);
  ++names->written;
}

extern "C" REX_FUNC(sub_828A1460) {
  const uint32_t object = ctx.r3.u32;
  const uint64_t world = ctx.r4.u64;
  const uint64_t target = bam_model_target_world.load();
  if (!bam_models_ready || !target || target != world) {
    {
      std::lock_guard lock(sky_water_mutex);
      if (sky_water.object == object) {
        // Restarted for another world without Release: stop mirroring.
        REXLOG_WARN("skate2-sky-water: component {:08X} restarted for {:016X}; ocean instance detached",
                    object, world);
        sky_water = {};
      }
    }
    __imp__sub_828A1460(ctx, base);
    return;
  }
  bam_model_target_world.store(0);
  const uint32_t sp = ctx.r1.u32;
  if (sp < 0x10000 || object < 0x10000) {
    REXLOG_ERROR("skate2-sky-water: invalid component/stack; template model retained");
    __imp__sub_828A1460(ctx, base);
    return;
  }
  ModelNames sky = kSkybox;
  sky.object = object;
  model_names = &sky;
  __imp__sub_828A1460(ctx, base);
  model_names = nullptr;
  const auto root = GuestString(base, object + 16);
  const bool sky_ok = sky.written == 2 && GuestString(base, object + 272) == kSkybox.model &&
      GuestString(base, object + 528) == kSkybox.textures;
  REXLOG_INFO("skate2-sky-water: start component={:08X} world={:016X} root='{}' skybox={} model='{}' textures='{}'",
              object, world, root, sky_ok, GuestString(base, object + 272), GuestString(base, object + 528));
  if (!sky_ok) {
    // The template lacks the attribute(s): no names were copied, so nothing
    // was substituted. Leave the original result alone; no ocean mirror.
    REXLOG_ERROR("skate2-sky-water: template world model attributes absent; original S2 sky/ocean not loaded");
    return;
  }
  // Ocean: the same guest Start on a transient object whose names are
  // copied the same way, so its requests use the retail queue arguments.
  const auto scratch = MakeScratch(base, sp);
  WriteShadow(base, scratch.shadow, object, world);
  ModelNames ocean = kOcean;
  ocean.object = scratch.shadow;
  auto call = ctx;
  call.r1.u64 = scratch.frame;
  call.r3.u64 = scratch.shadow;
  call.r4.u64 = world;
  model_names = &ocean;
  __imp__sub_828A1460(call, base);
  model_names = nullptr;
  const bool ocean_ok = ocean.written == 2 &&
      GuestString(base, scratch.shadow + 272) == kOcean.model &&
      GuestString(base, scratch.shadow + 528) == kOcean.textures;
  SkyWaterState state;
  state.object = object;
  state.world = world;
  const uint32_t out = scratch.shadow + kShadowBytes - 16;
  state.ids[0] = ModelId(ctx, base, scratch.frame, out, object + 528);
  state.ids[1] = ModelId(ctx, base, scratch.frame, out, object + 272);
  state.ids[2] = ModelId(ctx, base, scratch.frame, out, scratch.shadow + 528);
  state.ids[3] = ModelId(ctx, base, scratch.frame, out, scratch.shadow + 272);
  REXLOG_INFO("skate2-sky-water: ocean instance queued={} ids sky=({:016X},{:016X}) ocean=({:016X},{:016X})",
              ocean_ok, state.ids[0], state.ids[1], state.ids[2], state.ids[3]);
  if (!ocean_ok) return;
  std::lock_guard lock(sky_water_mutex);
  sky_water = state;
}

// OnStarted: the guest adds only resources that are already resident. Record
// what it (and the mirror) added; anything still loading is added once
// resident by PollSkyWater instead of being silently skipped.
extern "C" REX_FUNC(sub_828A1830) {
  const uint32_t object = ctx.r3.u32;
  const uint32_t sp = ctx.r1.u32;
  const auto saved = ctx;
  __imp__sub_828A1830(ctx, base);
  if (!SkyWaterMirror(object)) return;
  const auto scratch = MakeScratch(base, sp);
  uint64_t world;
  std::array<uint64_t, 4> ids;
  {
    std::lock_guard lock(sky_water_mutex);
    world = sky_water.world;
    ids = sky_water.ids;
  }
  WriteShadow(base, scratch.shadow, object, world);
  auto call = saved;
  call.r1.u64 = scratch.frame;
  call.r3.u64 = scratch.shadow;
  __imp__sub_828A1830(call, base);
  uint32_t manager = 0;
  std::array<bool, 4> loaded{};
  if (GuestU32(base, kModelManagerOwner, manager) && manager >= 0x10000)
    for (size_t i = 0; i < ids.size(); ++i)
      loaded[i] = ModelLoaded(call, base, scratch.frame, manager, ids[i]);
  std::lock_guard lock(sky_water_mutex);
  if (sky_water.object != object) return;
  sky_water.added = loaded;
  sky_water.pending = !(loaded[0] && loaded[1] && loaded[2] && loaded[3]);
  sky_water.since = std::chrono::steady_clock::now();
  REXLOG_INFO("skate2-sky-water: started component={:08X} resident sky=({},{}) ocean=({},{}) pending={}",
              object, loaded[0], loaded[1], loaded[2], loaded[3], sky_water.pending);
}

// Stop removes whatever is resident, for the real pair and the ocean pair.
extern "C" REX_FUNC(sub_828A18B8) {
  const uint32_t object = ctx.r3.u32;
  const uint32_t sp = ctx.r1.u32;
  const auto saved = ctx;
  __imp__sub_828A18B8(ctx, base);
  if (!SkyWaterMirror(object)) return;
  const auto scratch = MakeScratch(base, sp);
  uint64_t world;
  {
    std::lock_guard lock(sky_water_mutex);
    world = sky_water.world;
    sky_water.pending = false;
    sky_water.added = {};
  }
  WriteShadow(base, scratch.shadow, object, world);
  auto call = saved;
  call.r1.u64 = scratch.frame;
  call.r3.u64 = scratch.shadow;
  __imp__sub_828A18B8(call, base);
  REXLOG_INFO("skate2-sky-water: stopped component={:08X} next_world={:016X}", object, saved.r4.u64);
}

extern "C" REX_FUNC(sub_828A1940) {
  const uint32_t object = ctx.r3.u32;
  const uint32_t sp = ctx.r1.u32;
  const auto saved = ctx;
  __imp__sub_828A1940(ctx, base);
  if (!SkyWaterMirror(object)) return;
  const auto scratch = MakeScratch(base, sp);
  uint64_t world;
  {
    std::lock_guard lock(sky_water_mutex);
    world = sky_water.world;
    sky_water = {};
  }
  WriteShadow(base, scratch.shadow, object, world);
  auto call = saved;
  call.r1.u64 = scratch.frame;
  call.r3.u64 = scratch.shadow;
  __imp__sub_828A1940(call, base);
  REXLOG_INFO("skate2-sky-water: released component={:08X}", object);
}

namespace {
// Gameplay-thread follow-up for resources that became resident after
// OnStarted. Bounded: gives up (logged) after two minutes.
void PollSkyWater(PPCContext& ctx, uint8_t* base) {
  std::array<uint64_t, 4> ids;
  std::array<bool, 4> added;
  uint32_t object;
  {
    std::lock_guard lock(sky_water_mutex);
    if (!sky_water.pending) return;
    if (std::chrono::steady_clock::now() - sky_water.since > std::chrono::minutes(2)) {
      sky_water.pending = false;
      REXLOG_WARN("skate2-sky-water: resources still not resident after 2 min sky=({},{}) ocean=({},{})",
                  sky_water.added[0], sky_water.added[1], sky_water.added[2], sky_water.added[3]);
      return;
    }
    ids = sky_water.ids;
    added = sky_water.added;
    object = sky_water.object;
  }
  uint32_t manager = 0;
  if (ctx.r1.u32 < 0x11000 || !GuestU32(base, kModelManagerOwner, manager) || manager < 0x10000) return;
  const uint32_t frame = (ctx.r1.u32 - 256) & ~15u;
  REX_STORE_U32(frame, ctx.r1.u32);
  for (size_t i = 0; i < ids.size(); ++i) {
    if (added[i] || !ModelLoaded(ctx, base, frame, manager, ids[i])) continue;
    {
      std::lock_guard lock(sky_water_mutex);
      if (sky_water.object != object || !sky_water.pending || sky_water.added[i]) return;
      sky_water.added[i] = true;
    }
    auto call = ctx;
    call.r1.u64 = frame;
    call.r3.u64 = manager;
    call.r4.u64 = ids[i];
    sub_82C9CAB8(call, base);  // the add OnStarted would have issued
    REXLOG_INFO("skate2-sky-water: late add {} id={:016X}", kSkyWaterRoles[i], ids[i]);
  }
  std::lock_guard lock(sky_water_mutex);
  if (sky_water.object == object)
    sky_water.pending = !(sky_water.added[0] && sky_water.added[1] &&
                          sky_water.added[2] && sky_water.added[3]);
}
}  // namespace

extern "C" REX_FUNC(sub_8247E520) {
  const uint32_t manager = ctx.r3.u32;
  const bool trace = manager == bam_proxy_manager.load() && REXCVAR_GET(skate3_world_load_probe);
  uint32_t before = 0;
  if (trace) GuestU32(base, manager + 40, before);
  __imp__sub_8247E520(ctx, base);
  if (trace) {
    uint32_t after = 0, total = 0, loaded = 0, activated = 0;
    GuestU32(base, manager + 40, after);
    if (before != after) {
      GuestU32(base, manager + 48, total);
      GuestU32(base, manager + 44, loaded);
      GuestU32(base, manager + 52, activated);
      REXLOG_INFO("skate2-proxy: state {} -> {} collections={} loaded={} activation_counter={}",
          before, after, total, loaded, activated);
    }
  }
}

// Read back the guest association, so a successful proxy load cannot be
// mistaken for successful detail/proxy switching. Sparse diagnostics only.
extern "C" REX_FUNC(sub_8247EF40) {
  const uint32_t manager = ctx.r3.u32;
  const uint64_t detail = ctx.r4.u64;
  const int32_t x = ctx.r5.s32, z = ctx.r6.s32;
  const bool trace = manager == bam_proxy_manager.load() && skate2_loading.load() &&
      REXCVAR_GET(skate3_world_load_probe);
  __imp__sub_8247EF40(ctx, base);
  if (!trace) return;
  static std::atomic<uint32_t> checked{0}, matched{0};
  auto call = ctx;
  const uint32_t frame = (ctx.r1.u32 - 128) & ~15u;
  REX_STORE_U32(frame, ctx.r1.u32);
  call.r1.u64 = frame;
  call.r3.u64 = frame + 80;
  call.r4.u64 = manager;
  call.r5.u64 = detail;
  sub_8247F158(call, base);
  const uint64_t proxy = REX_LOAD_U64(frame + 80);
  // Compare against the same invalid GUID constant as 8247F158.
  const uint64_t invalid = REX_LOAD_U64(0x830BBDD0);
  const auto n = ++checked;
  if (proxy != invalid) ++matched;
  if (n <= 8 || (n & (n - 1)) == 0)
    REXLOG_INFO("skate2-proxy: association checked={} matched={} cell=({},{}) detail={:016X} proxy={:016X}",
        n, matched.load(), x, z, detail, proxy);
}

// Do not gate on gameplay: initial bindings occur on the loading screen,
// and removal notifications can outlive the transition away from BAM.
extern "C" REX_FUNC(sub_8280AC58) {
  const auto source = ctx.r4.u64;
  const bool enabled = REXCVAR_GET(skate3_skate2_enabled) &&
      REXCVAR_GET(skate3_skate2_texture_identity);
  const bool lighting = bam_lighting_ids.contains(source);
  const bool changed = skate3::world_loading::TranslateBamTextureBind(
      enabled, uint32_t(ctx.lr), ctx.r4.u64, ctx.r30.u64, lighting);
  const auto target = ctx.r4.u64;
  // r30 is the caller's saved identity, used immediately after this call
  // to choose the low-detail slot. Translating only r4 would select the
  // right record but incorrectly overwrite its high-detail slot.
  __imp__sub_8280AC58(ctx, base);
  if (changed && REXCVAR_GET(skate3_world_load_probe)) {
    static std::array<std::atomic<uint32_t>, 2> count{};
    const auto n = ++count[lighting ? 1 : 0];
    if (n <= 16 || (n & (n - 1)) == 0)
      REXLOG_INFO("world-texture-identity: bind count={} source={:016X} target={:016X} record={:08X} lighting={}",
                  n, source, target, ctx.r3.u32, lighting);
  }
}

extern "C" REX_FUNC(sub_8280AAF0) {
  const auto source = ctx.r5.u64;
  // Texture removal (8279F188's type-0 record: r4 = the unloaded texture
  // object, r5 = its asset ID before translation): the native registry
  // forgets the object, so a channel the game has not bound yet never
  // resolves to freed memory that a later texture reuses. Only while a
  // Skate 2 world is active; Skate 3 maps keep the registry as it was.
  if (uint32_t(ctx.lr) == 0x8279F2D8 && skate2_loading.load()) {
    skate3::native_scene::OnUnregisterTexture(source, ctx.r4.u32);
  }
  const bool enabled = REXCVAR_GET(skate3_skate2_enabled) &&
      REXCVAR_GET(skate3_skate2_texture_identity);
  const bool changed = skate3::world_loading::TranslateBamTextureUnbind(
      enabled, uint32_t(ctx.lr), ctx.r5.u64, bam_lighting_ids.contains(source));
  {
    skate3::world_loading::TextureLookupResetScope invalidate_index(ctx.r3.u32 + 272);
    __imp__sub_8280AAF0(ctx, base);
  }
  if (changed && REXCVAR_GET(skate3_world_load_probe)) {
    static std::atomic<uint32_t> count{0};
    const auto n = ++count;
    if (n <= 16 || (n & (n - 1)) == 0)
      REXLOG_INFO("world-texture-identity: unbind count={} source={:016X}", n, source);
  }
}

void skate3::world_loading::RequestLoad() {
  if (!skate2_ready.load()) {
    REXLOG_WARN("skate2: shortcut unavailable; prepared BAM files not ready");
    return;
  }
  if (skate2_loading.load() || template_world.load()) {
    REXLOG_WARN("skate2: BAM already active/loading; choose a normal location first");
    return;
  }
  load_requested.store(true);
  REXLOG_INFO("skate2: Ctrl+F7 queued normal gameplay teleport");
}

namespace {
void ArmTemplate(uint64_t world) {
  template_world.store(world);
  lw_load_group_pending.store(lw_assets_ready && world && LivingWorldStream(requested_stream.load()));
  lw_template_group_key.store(world);
  lw_load_group_loaded.store(false);
  // Skate 2's distant city proxies surround BAM only, not the DLC maps.
  bam_proxy_target_world.store(bam_proxy_ready && world && requested_stream.load() == 0 ? world : 0);
  bam_model_target_world.store(bam_models_ready && world ? world : 0);
  template_deadline.store(world ? (std::chrono::steady_clock::now() + std::chrono::seconds(60))
                                      .time_since_epoch().count() : 0);
}
}  // namespace

// Locations-menu pick of a New San Van spot (skate3_nsv_menu.cpp). Chooses the
// world key the retail teleport step runs with: inside New San Van (+40 ==
// +48) on the same Skate 2 stream, the loaded key, which makes it an in-world
// teleport; otherwise a city district armed as BAM template with +40
// invalidated, which makes it the event3 reload that the template hook turns
// into the spot's stream (BAM or a DLC map).
bool skate3::world_loading::ArmMenuSpawn(uint8_t* base, const std::string& label,
                                         const std::string& stream, uint64_t& world_key) {
  if (!skate2_ready.load()) {
    REXLOG_WARN("skate2: New San Van spot '{}' unavailable; prepared BAM files not ready", label);
    return false;
  }
  const int stream_index = StreamIndex(stream);
  if (stream_index < 0) {
    REXLOG_WARN("skate2: '{}' needs world stream {}; run tools/build_skate2_dlc_maps.py",
                label, stream);
    return false;
  }
  if (template_world.load()) {
    REXLOG_WARN("skate2: New San Van load already in progress; ignoring '{}'", label);
    return false;
  }
  const uint64_t loaded = WorldField(base, 40), current = WorldField(base, 48);
  if (skate2_loading.load() && loaded == current && IsCityDistrict(loaded) &&
      loaded_stream.load() == stream_index) {
    world_key = loaded;
    REXLOG_INFO("skate2: Locations menu relocation to '{}' inside {}", label, stream);
    return true;
  }
  requested_stream.store(stream_index);
  world_key = IsCityDistrict(current) ? current
            : IsCityDistrict(loaded) ? loaded : kDowntownWorld;
  uint64_t raw_loaded, raw_world;
  std::memcpy(&raw_loaded, base + kWorldState + 40, 8);
  std::memcpy(&raw_world, base + kWorldState + 48, 8);
  menu_previous_loaded.store(raw_loaded);
  menu_previous_world.store(raw_world);
  const uint64_t invalid = ~uint64_t(0);
  std::memcpy(base + kWorldState + 40, &invalid, 8);
  ArmTemplate(world_key);
  HoldCareerAutosave(base, false);
  REXLOG_INFO("skate2: Locations menu load of '{}' stream={} template={:016X} (world was {:016X})",
              label, stream, world_key, current);
  return true;
}

// Inside BAM the loaded world (+40) is the template district, so a retail
// destination in that district would compare equal and teleport to Skate 3
// coordinates inside New San Van. An invalid +40 forces the full reload.
void skate3::world_loading::BeforeRetailTeleport(uint8_t* base, uint64_t world) {
  if (!skate2_loading.load() || world == kNewSanVanWorld) return;
  lw_load_group_pending.store(false);
  lw_load_group_loaded.store(false);
  const uint64_t invalid = ~uint64_t(0);
  std::memcpy(base + kWorldState + 40, &invalid, 8);
  REXLOG_INFO("skate2: leaving New San Van for world {:016X}; forcing a full reload", world);
}

// Log012 establishes event3 -> 82706F50 -> 82720B50 as the normal menu
// teleport. Run that same branch on the gameplay thread, without replay flags.
extern "C" REX_FUNC(sub_8270C890) {
  if (ctx.r4.u32 == 0 || ctx.r4.u32 == 1) bam_gameplay.store(skate2_loading.load());
  else if (ctx.r4.u32 == 4) bam_gameplay.store(false);
  skate3::freeze_capture::GameplayUpdate freeze_update(ctx.r4.u32 == 1);
  if (ctx.r4.u32 == 1 && skate3::freeze_capture::Enabled() &&
      skate3::world_loading::IsSkate2Gameplay()) {
    freeze_base.store(base);
    freeze_gameplay.store(ctx.r3.u32);
  }
  if (ctx.r4.u32 == 1) PollSkyWater(ctx, base);
  if (ctx.r4.u32 == 1) HoldCareerAutosave(base, true);
  if (ctx.r4.u32 == 1 && template_world.load() && template_deadline.load() &&
      std::chrono::steady_clock::now().time_since_epoch().count() > template_deadline.load()) {
    REXLOG_WARN("skate2: template {:016X} expired without a world load; cleared",
                template_world.load());
    ArmTemplate(0);
    // A declined menu teleport left +40 invalid; put the world state back.
    uint64_t raw_loaded;
    std::memcpy(&raw_loaded, base + kWorldState + 40, 8);
    if (raw_loaded == ~uint64_t(0) && menu_previous_loaded.load()) {
      const uint64_t loaded = menu_previous_loaded.exchange(0);
      const uint64_t world = menu_previous_world.exchange(0);
      std::memcpy(base + kWorldState + 40, &loaded, 8);
      std::memcpy(base + kWorldState + 48, &world, 8);
    }
  }
  if (ctx.r4.u32 == 1 && REXCVAR_GET(skate3_world_load_probe)) {
    static auto last = std::chrono::steady_clock::time_point{};
    const auto now = std::chrono::steady_clock::now();
    if (now - last >= std::chrono::seconds(1)) {
      last = now;
      REXLOG_INFO("world-view: gameplay bam={} cumulative prepare={} sort={} render={}",
                  skate2_loading.load(), view_stage_calls[0].load(),
                  view_stage_calls[1].load(), view_stage_calls[2].load());
    }
  }
  if (ctx.r4.u32 == 1 && load_requested.exchange(false)) {
    uint32_t owner = 0, child = 0, hi = 0, lo = 0;
    std::array<uint8_t, 64> self{};
    std::array<uint8_t, 48> child_bytes{};
    const uint32_t self_address = ctx.r3.u32;
    if (!GuestU32(base, self_address + 4, owner) ||
        !GuestU32(base, owner + 116, child) ||
        !GuestU32(base, kWorldState + 48, hi) ||
        !GuestU32(base, kWorldState + 52, lo) ||
        self_address < 0x10000 || child < 0x10000 ||
        !skate3::native_scene::GuestTryCopy(self.data(), base + self_address, self.size()) ||
        !skate3::native_scene::GuestTryCopy(child_bytes.data(), base + child, child_bytes.size()) ||
        !self[44] || child_bytes[40] || ctx.r1.u32 < 0x11000 ||
        (hi == 0xffffffff && lo == 0xffffffff) || (!hi && !lo)) {
      REXLOG_WARN("skate2: teleport rejected; gameplay loader is not ready");
    } else {
      const uint64_t current = (uint64_t(hi) << 32) | lo;
      const uint64_t world = IsCityDistrict(current) ? current : kDowntownWorld;
      // A_RobsPark from Skate 2 Location XML, reports/skate2_world_locations.json.
      // Candidate spawn: runtime validation of its support geometry is pending.
      constexpr std::array<float, 16> transform = {
        0, 0, -1, 0, 0, 1, 0, 0, 1, 0, 0, 0,
        588.861694f, -54.231647f, 996.504822f, 1
      };
      // Reserve an aligned guest stack frame; callees grow below it. Use a
      // context copy so the intercepted callback's register contract survives.
      auto call = ctx;
      const uint32_t frame = (ctx.r1.u32 - 256) & ~15u;
      call.r1.u64 = frame;
      for (uint32_t i = 0; i < transform.size(); ++i) {
        uint32_t bits;
        std::memcpy(&bits, &transform[i], 4);
        bits = __builtin_bswap32(bits);
        std::memcpy(base + frame + 80 + i * 4, &bits, 4);
      }
      uint64_t previous, previous_world;
      std::memcpy(&previous, base + kWorldState + 40, 8);
      std::memcpy(&previous_world, base + kWorldState + 48, 8);
      // +48 names the world to load: a DLC map or park is replaced by
      // Downtown, whose loader layout the template hook accepts.
      const uint64_t world_be = __builtin_bswap64(world);
      std::memcpy(base + kWorldState + 48, &world_be, 8);
      const uint64_t invalid = ~uint64_t(0);
      std::memcpy(base + kWorldState + 40, &invalid, 8);
      requested_stream.store(0);  // Ctrl+F7 always loads BAM.
      ArmTemplate(world);
      call.r3.u64 = child;
      call.r4.u64 = frame + 80;
      call.r5.s64 = -1;
      call.r6.u64 = 0;
      call.r7.u64 = 0;
      call.r8.u64 = 0;
      call.r9.u64 = 0;
      sub_82706F50(call, base);
      if (call.r3.u32 & 0xff) {
        call.r3.u64 = self_address;
        call.r4.u64 = 0x8270C7D0; // Continuation used by retail event3.
        call.r5.u64 = child;
        sub_82720B50(call, base);
        REXLOG_INFO("skate2: normal teleport started template={:016X} spawn=A_RobsPark (588.862,-54.232,996.505)", world);
        return;
      }
      ArmTemplate(0);
      std::memcpy(base + kWorldState + 40, &previous, 8);
      std::memcpy(base + kWorldState + 48, &previous_world, 8);
      REXLOG_WARN("skate2: normal teleport handler declined transition");
    }
  }
  __imp__sub_8270C890(ctx, base);
}

// cMsgInitAutosave (478AA499), posted by the ICE TriggerAutoSave action and
// career events. Logging only: while the switch is held clear the handler
// takes its own no-autosave branch (see HoldCareerAutosave).
extern "C" REX_FUNC(sub_825DE0B8) {
  if (CareerAutosaveHeld()) {
    static std::atomic<uint32_t> skipped{0};
    const auto n = ++skipped;
    if (n <= 16 || (n & (n - 1)) == 0)
      REXLOG_INFO("nsv-autosave: autosave request {} skipped while New San Van is loaded", n);
  }
  __imp__sub_825DE0B8(ctx, base);
}

// Trace actual state-machine transitions before selecting a replacement for
// the replay-specific self+604 request used in the first shortcut experiment.
extern "C" REX_FUNC(sub_826DFB30) {
  if (REXCVAR_GET(skate3_world_load_probe)) {
    static std::atomic<uint32_t> count{0};
    if (count.fetch_add(1) < 1024) {
      uint32_t previous = 0;
      GuestU32(base, ctx.r3.u32 + 8, previous);
      REXLOG_INFO("world-transition: state object={:08X} old={:08X} next={:08X} enter={} finish={} caller={:08X}",
                  ctx.r3.u32, previous, ctx.r4.u32, ctx.r5.u32, ctx.r6.u32, uint32_t(ctx.lr));
    }
  }
  __imp__sub_826DFB30(ctx, base);
}

extern "C" REX_FUNC(sub_8270B3D0) {
  {
    uint32_t event = 0, type = 0;
    if (GuestU32(base, ctx.r4.u32, event) && GuestU32(base, event + 8, type)) {
      skate3::menu_probe::OnGameplayEvent(base, uint32_t(ctx.lr), ctx.r1.u32, ctx.r3.u32, event,
                                          type);
    }
  }
  if (REXCVAR_GET(skate3_world_load_probe)) {
    static std::atomic<uint32_t> count{0};
    uint32_t event = 0, type = 0;
    if (count.fetch_add(1) < 2048 && GuestU32(base, ctx.r4.u32, event) && GuestU32(base, event + 8, type)) {
      REXLOG_INFO("world-transition: gameplay event={} object={:08X} payload={:08X} caller={:08X}",
                  type, ctx.r3.u32, event, uint32_t(ctx.lr));
    }
  }
  __imp__sub_8270B3D0(ctx, base);
}

extern "C" REX_FUNC(sub_8251C3C8) {
  skate3::menu_probe::OnLocationMenuAction(base, uint32_t(ctx.lr), ctx.r1.u32, ctx.r3.u32);
  if (REXCVAR_GET(skate3_world_load_probe))
    REXLOG_INFO("world-transition: location menu action begin object={:08X}", ctx.r3.u32);
  __imp__sub_8251C3C8(ctx, base);
  if (REXCVAR_GET(skate3_world_load_probe)) {
    for (uint32_t offset : {24u, 32u, 40u, 48u, 56u}) {
      uint32_t hi = 0, lo = 0;
      if (GuestU32(base, kWorldState + offset, hi) && GuestU32(base, kWorldState + offset + 4, lo))
        REXLOG_INFO("world-transition: location field +{}={:08X}{:08X}", offset, hi, lo);
    }
  }
}

// The streaming-readiness update writes renderer+0x347a4. Main render
// 827FF7D0 tests it AND +0x347a5 before sorting/drawing all 3D views. Log015
// shows preparation continues while both later stages stop. The gate
// override experiment ended at log016 (the gate already passes); this hook
// only reports it, so it runs with the load probe alone (the residency
// bypass that skate3_skate2_render_gate_test still enables lives in
// sub_82478D00).
extern "C" REX_FUNC(sub_826D81A8) {
  const uint32_t monitor = ctx.r3.u32;
  __imp__sub_826D81A8(ctx, base);
  if (!REXCVAR_GET(skate3_world_load_probe)) return;
  uint32_t renderer = 0, begin = 0, end = 0, vtable = 0;
  if (!GuestU32(base, 0x83083C5C, renderer) || !renderer) return;
  const uint32_t view = renderer + 3888;
  if (!GuestU32(base, view, vtable) || vtable != 0x823120F8) return;
  std::array<uint8_t, 2> gates{};
  if (!skate3::native_scene::GuestTryCopy(gates.data(), base + renderer + 0x347a4, 2)) return;
  GuestU32(base, view + 16092, begin);
  GuestU32(base, view + 16096, end);
  const bool override_gate = false; // Log016: this streaming gate already passes.
  static auto last = std::chrono::steady_clock::time_point{};
  const auto now = std::chrono::steady_clock::now();
  if (now - last >= std::chrono::seconds(1)) {
    last = now;
    uint32_t waiting = 0;
    GuestU32(base, monitor + 36, waiting);
    REXLOG_INFO("world-render-gate: bam={} gameplay={} streaming={} resources={} visible_bytes={} monitor_wait={} override={}",
                skate2_loading.load(), bam_gameplay.load(), gates[0], gates[1],
                end >= begin ? end - begin : 0, waiting, override_gate);
  }
}

// Main-render residency query only. Other users of this function (teleport,
// streaming, simulation) retain their original results. Keep the subsequent
// state16 veto in 827FF648 intact.
extern "C" REX_FUNC(sub_82478D00) {
  const uint32_t caller = uint32_t(ctx.lr);
  const uint32_t position = ctx.r4.u32;
  std::array<float, 3> xyz{};
  if (caller == 0x827FF73C) {
    for (size_t i = 0; i < xyz.size(); ++i) {
      uint32_t bits = 0;
      GuestU32(base, position + uint32_t(i * 4), bits);
      std::memcpy(&xyz[i], &bits, 4);
    }
  }
  __imp__sub_82478D00(ctx, base);
  if (caller != 0x827FF73C) return;
  const bool resident = (ctx.r3.u32 & 255) != 0;
  const bool bypass = !resident && skate2_loading.load() && bam_gameplay.load() &&
      REXCVAR_GET(skate3_skate2_render_gate_test);
  if (bypass) ctx.r3.u64 = 1;
  skate3::cheats::OnBlackScreenResidency(resident || bypass);
  if (REXCVAR_GET(skate3_world_load_probe)) {
    static auto last = std::chrono::steady_clock::time_point{};
    const auto now = std::chrono::steady_clock::now();
    if (now - last >= std::chrono::seconds(1)) {
      last = now;
      REXLOG_INFO("world-camera-residency: bam={} gameplay={} position=({:.3f},{:.3f},{:.3f}) resident={} diagnostic_bypass={}",
                  skate2_loading.load(), bam_gameplay.load(), xyz[0], xyz[1], xyz[2], resident, bypass);
    }
  }
}
namespace {
// Event dispatch is nested and synchronous. Preserve the outer context so a
// pause callback can identify the publisher hidden by its generic dispatch LR.
thread_local uint32_t pause_event_caller = 0;
thread_local uint32_t pause_event_object = 0;
}
extern "C" REX_FUNC(sub_82B93960) {
  const uint32_t saved_caller = pause_event_caller;
  const uint32_t saved_object = pause_event_object;
  pause_event_caller = uint32_t(ctx.lr);
  pause_event_object = ctx.r3.u32;
  uint32_t event_type = 0, event_token = 0;
  GuestU32(base, pause_event_object, event_type);
  GuestU32(base, pause_event_object + 4, event_token);
  if (skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe) &&
      event_type == 0xDDC8C41A && event_token == 0x1E309919) {
    // Follow the guest ABI backchain: each caller's saved LR is at
    // caller_sp-8. This reveals the caller above the event wrapper.
    uint32_t sp = ctx.r1.u32;
    for (uint32_t depth = 0; depth < 12; ++depth) {
      uint32_t parent = 0, ret = 0;
      if (!GuestU32(base, sp, parent) || parent <= sp || parent - sp > 0x10000 ||
          !GuestU32(base, parent - 8, ret)) break;
      REXLOG_INFO("world-pause-backtrace: depth={} return={:08X}", depth, ret);
      sp = parent;
    }
  }
  // The Stacks slow-frame samples include this synchronous event chain.
  // Measure only the outer dispatch on NSV; nested events are included in
  // its time once. Home exports the guest render thread's own frame totals.
  const uint64_t event_began = saved_object == 0 && skate3::world_loading::IsSkate2Gameplay()
      ? skate3::perf_recorder::BeginGuestEvent() : 0;
  __imp__sub_82B93960(ctx, base);
  skate3::perf_recorder::EndGuestEvent(event_began, event_type, event_token,
                                     pause_event_caller);
  pause_event_caller = saved_caller;
  pause_event_object = saved_object;
}
namespace {
void TracePauseStack(uint8_t* base, uint32_t object, const char* operation,
                     uint32_t state, uint32_t caller) {
  if (!skate2_loading.load() || !REXCVAR_GET(skate3_world_load_probe)) return;
  uint32_t begin = 0, end = 0, flags = 0, enabled = 0;
  GuestU32(base, object + 8, begin);
  GuestU32(base, object + 12, end);
  GuestU32(base, object + 256, flags);
  GuestU32(base, object + 260, enabled);
  std::string stack;
  if (end >= begin && end - begin <= 256) {
    for (uint32_t at = begin; at < end; at += 4) {
      uint32_t value = 0;
      if (!GuestU32(base, at, value)) break;
      if (!stack.empty()) stack += ',';
      stack += std::to_string(value);
    }
  }
  REXLOG_INFO("world-pause-stack: operation={} state={} caller={:08X} object={:08X} flags={:08X} enabled={:08X} stack=[{}]",
              operation, state, caller, object, flags, enabled, stack);
}
}
extern "C" REX_FUNC(sub_826DB498) {
  const uint32_t object = ctx.r3.u32, state = ctx.r4.u32, caller = uint32_t(ctx.lr);
  if (state == 11 && skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe)) {
    uint32_t type = 0, token = 0, sender = 0;
    GuestU32(base, pause_event_object, type);
    GuestU32(base, pause_event_object + 4, token);
    GuestU32(base, pause_event_object + 12, sender);
    REXLOG_INFO("world-pause-origin: publisher={:08X} event={:08X} type={:08X} token={:08X} sender={:08X}",
                pause_event_caller, pause_event_object, type, token, sender);
  }
  __imp__sub_826DB498(ctx, base);
  TracePauseStack(base, object, "push", state, caller);
}
extern "C" REX_FUNC(sub_826DB530) {
  const uint32_t object = ctx.r3.u32, state = ctx.r4.u32, caller = uint32_t(ctx.lr);
  __imp__sub_826DB530(ctx, base);
  TracePauseStack(base, object, "remove", state, caller);
}
extern "C" REX_FUNC(sub_824D4EE0) {
  const uint32_t caller = uint32_t(ctx.lr), object = ctx.r3.u32;
  __imp__sub_824D4EE0(ctx, base);
  if (caller == 0x827FF76C) {
    skate3::perf_recorder::SetRenderPaused((ctx.r3.u32 & 255) != 0);
  }
  if (caller != 0x827FF76C || !REXCVAR_GET(skate3_world_load_probe)) return;
  // Render-thread-only state: record edges, not once-per-second samples.
  static bool previous = false;
  static auto began = std::chrono::steady_clock::time_point{};
  static uint32_t blocked_frames = 0;
  const bool blocked = (ctx.r3.u32 & 255) != 0;
  const auto now = std::chrono::steady_clock::now();
  if (blocked) ++blocked_frames;
  if (blocked != previous) {
    if (blocked) began = now;
    const double elapsed = blocked ? 0.0 :
        std::chrono::duration<double, std::milli>(now - began).count();
    REXLOG_INFO("world-render-pause: bam={} blocked={} duration_ms={:.2f} frames={}",
                skate2_loading.load(), blocked, elapsed, blocked_frames);
    TracePauseStack(base, object, blocked ? "render-stop" : "render-resume", 0, caller);
    if (!blocked) blocked_frames = 0;
    previous = blocked;
  }
}

namespace {
std::mutex stream_operation_mutex;
std::unordered_map<uint64_t, std::chrono::steady_clock::time_point> stream_operations;
void TraceStreamOperation(uint64_t id, bool unload, bool complete) {
  using skate3::perf_recorder::Activity;
  skate3::perf_recorder::Count(
      complete ? (unload ? Activity::kStreamUnloadDone : Activity::kStreamLoadDone)
               : (unload ? Activity::kStreamUnload : Activity::kStreamLoad));
  if (!bam_gameplay.load() || !REXCVAR_GET(skate3_world_load_probe)) return;
  const auto now = std::chrono::steady_clock::now();
  std::lock_guard lock(stream_operation_mutex);
  int64_t elapsed = -1;
  if (complete) {
    auto it = stream_operations.find(id);
    if (it != stream_operations.end()) {
      elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - it->second).count();
      stream_operations.erase(it);
    }
  } else {
    if (stream_operations.size() >= 1024) stream_operations.clear();
    stream_operations[id] = now;
  }
  REXLOG_INFO("world-stream-operation: id={:016X} operation={} phase={} elapsed_ms={}",
              id, unload ? "unload" : "load", complete ? "complete" : "start", elapsed);
}
}  // namespace

extern "C" REX_FUNC(sub_82C95F70) {
  if (uint32_t(ctx.lr) == 0x8247A400) TraceStreamOperation(ctx.r4.u64, false, false);
  __imp__sub_82C95F70(ctx, base);
}
extern "C" REX_FUNC(sub_82C96188) {
  if (uint32_t(ctx.lr) == 0x8247A490) TraceStreamOperation(ctx.r4.u64, true, false);
  __imp__sub_82C96188(ctx, base);
}
extern "C" REX_FUNC(sub_8247C628) {
  TraceStreamOperation(ctx.r4.u64, false, true);
  __imp__sub_8247C628(ctx, base);
}
extern "C" REX_FUNC(sub_8247C6C0) {
  TraceStreamOperation(ctx.r4.u64, true, true);
  __imp__sub_8247C6C0(ctx, base);
}

namespace {
// Log034's pauses are mostly BEFORE the Sim load begins. The selectors at
// 824798C8/82479A40 gate on deferred releases, not only the collection queues.
// All snapshots below are read-only and may span concurrent worker updates.
bool StreamTimingEnabled() {
  return skate3::world_loading::IsSkate2Gameplay() &&
         REXCVAR_GET(skate3_world_load_probe);
}
uint32_t StreamWord(uint8_t* base, uint32_t object, uint32_t offset) {
  uint32_t value = ~0u;
  if (object >= 0x10000 && object <= 0xfffe0000)
    GuestU32(base, object + offset, value);
  return value;
}
uint64_t StreamId(uint8_t* base, uint32_t object, uint32_t offset) {
  return (uint64_t(StreamWord(base, object, offset)) << 32) |
         StreamWord(base, object, offset + 4);
}
uint32_t StreamVectorCount(uint8_t* base, uint32_t object,
                           uint32_t offset, uint32_t stride) {
  const auto begin = StreamWord(base, object, offset);
  const auto end = StreamWord(base, object, offset + 4);
  if (begin == ~0u || end < begin || (end - begin) % stride) return ~0u;
  return (end - begin) / stride;
}
struct ReleaseSnapshot {
  uint32_t submitted, processed, retired, epoch, oldest_epoch, final_queue;
};
ReleaseSnapshot ReadReleaseSnapshot(uint8_t* base, uint32_t manager) {
  ReleaseSnapshot s{
      StreamWord(base, manager, 60916), StreamWord(base, manager, 60920),
      StreamWord(base, manager, 60924), StreamWord(base, manager, 60928),
      ~0u, StreamVectorCount(base, manager, 60880, 4)};
  // 82C9D008 tags each release with epoch+3. 82C9D0D8 requires epoch to
  // strictly exceed that tag, and removes at most 20 ring entries per call.
  if (s.processed != ~0u && s.submitted != ~0u && s.processed < s.submitted) {
    const auto tags = StreamWord(base, manager, 60864);
    s.oldest_epoch = StreamWord(base, tags, (s.processed % 9000) * 4);
  }
  return s;
}
double StreamMilliseconds(std::chrono::steady_clock::duration duration) {
  return std::chrono::duration<double, std::milli>(duration).count();
}
template <typename Context>
void ProcessWorldReleases(Context& ctx, uint8_t* base) {
  if (skate3::freeze_capture::Enabled() && skate3::world_loading::IsSkate2Gameplay()) {
    freeze_base.store(base);
    freeze_stream_manager.store(ctx.r3.u32);
  }
  // Only the normal gameplay updater, before its stream scheduler. Other
  // worlds/callers retain the original one-batch behavior.
  if (!skate3::world_loading::IsSkate2Gameplay() ||
      !REXCVAR_GET(skate3_skate2_release_budget) ||
      uint32_t(ctx.lr) != 0x82477A7C) {
    __imp__sub_82C9D0D8(ctx, base);
    return;
  }
  const uint32_t manager = ctx.r3.u32;
  const auto read = [&]() {
    skate3::release_budget::Progress p;
    if (manager < 0x10000 || manager > 0xfffc0000) return p;
    const uint32_t sys = manager + 61008;
    if (StreamWord(base, sys, 16) != manager) return p;
    const auto count = StreamVectorCount(base, sys, 124, 32);
    p.submitted = StreamWord(base, manager, 60916);
    p.processed = StreamWord(base, manager, 60920);
    p.retired = StreamWord(base, manager, 60924);
    const bool background = REXCVAR_GET(skate3_skate2_release_background);
    p.target = background ? p.submitted : StreamWord(base, sys, 71468);
    p.valid = count <= 64 && p.submitted != ~0u && p.processed != ~0u &&
              p.retired != ~0u && p.target != ~0u;
    p.sim_pending = count != 0 || background;
    return p;
  };
  const auto before = read();
  const auto began = std::chrono::steady_clock::now();
  __imp__sub_82C9D0D8(ctx, base);
  if (!before.valid || !before.sim_pending) return;
  const auto elapsed = [&]() {
    return StreamMilliseconds(std::chrono::steady_clock::now() - began);
  };
  const auto result = skate3::release_budget::DrainExtra(before.processed, read, [&]() {
    // Preserve the mandatory call's returned registers. Each extra call uses
    // the same restored guest stack and original manager argument. No guest
    // pointers or contexts survive this invocation.
    auto batch = ctx;
    batch.r3.u64 = manager;
    __imp__sub_82C9D0D8(batch, base);
  }, elapsed);
  if (result.extra_calls && StreamTimingEnabled()) {
    const auto after = read();
    REXLOG_INFO("world-release-budget: manager={:08X} extra_calls={} processed_before={} processed_after={} retired={} target={} elapsed_ms={:.3f} stop={}",
                manager, result.extra_calls, before.processed, after.processed,
                after.retired, after.target, elapsed(), result.stop);
  }
}
template <typename Context, typename Function>
void TraceSimSelector(Context& ctx, uint8_t* base, Function original,
                      const char* selector) {
  const uint32_t sys = ctx.r3.u32;
  if (!StreamTimingEnabled() || ctx.r4.u32 != 1) {
    original(ctx, base);
    return;
  }
  const auto manager = StreamWord(base, sys, 16);
  const auto release = ReadReleaseSnapshot(base, manager);
  const auto target = StreamWord(base, sys, 71468);
  const auto unload_queue = StreamVectorCount(base, sys, 71028, 8);
  const auto unload_id = StreamId(base, sys, 71424);
  // Use the same runtime sentinel as the original comparison (830BBDD0).
  const auto empty_id = StreamId(base, 0x830BBDD0, 0);
  const auto before = std::chrono::steady_clock::now();
  original(ctx, base);
  const auto now = std::chrono::steady_clock::now();
  const bool selected = (ctx.r3.u32 & 255) != 0;
  const char* reason = "selected";
  if (!selected) {
    // Match the original early-return ordering. Remaining rejection is the
    // per-ID busy test; unknown/invalid snapshots must not claim a gate.
    if (unload_queue == ~0u || target == ~0u || release.retired == ~0u ||
        unload_id == ~uint64_t{0} || empty_id == ~uint64_t{0})
      reason = "snapshot-unavailable";
    else if (unload_queue) reason = "unload-queue";
    else if (unload_id != empty_id) reason = "unload-active";
    else if (target > release.retired) reason = "release-fence";
    else reason = "id-busy-or-raced";
  }
  struct Sample {
    uint32_t sys = 0;
    std::string reason;
    std::chrono::steady_clock::time_point last{};
  };
  static thread_local Sample last;
  if (selected || last.sys != sys || last.reason != reason ||
      now - last.last >= std::chrono::milliseconds(100)) {
    REXLOG_INFO("world-sim-select: selector={} sys={:08X} result={} reason={} target={} submitted={} processed={} retired={} epoch={} oldest_epoch={} final_queue={} unload_queue={} unload_id={:016X} call_ms={:.3f}",
                selector, sys, selected, reason, target, release.submitted,
                release.processed, release.retired, release.epoch,
                release.oldest_epoch, release.final_queue, unload_queue,
                unload_id, StreamMilliseconds(now - before));
    last = {sys, reason, now};
  }
}
template <typename Context, typename Function>
void TraceReleasePhase(Context& ctx, uint8_t* base, Function original,
                       const char* phase) {
  if (!StreamTimingEnabled()) {
    original(ctx, base);
    return;
  }
  const uint32_t manager = ctx.r3.u32;
  const auto before = ReadReleaseSnapshot(base, manager);
  const auto began = std::chrono::steady_clock::now();
  original(ctx, base);
  const auto now = std::chrono::steady_clock::now();
  const auto after = ReadReleaseSnapshot(base, manager);
  struct Sample {
    uint32_t manager = 0;
    bool busy = false;
    uint64_t calls = 0, processed = 0, retired = 0;
    double total_ms = 0, max_ms = 0;
    std::chrono::steady_clock::time_point last{};
  };
  static thread_local std::array<Sample, 2> samples;
  auto& sample = samples[phase[0] == 'p' ? 0 : 1];
  if (sample.manager != manager) sample = {};
  sample.manager = manager;
  ++sample.calls;
  if (before.processed != ~0u && after.processed >= before.processed)
    sample.processed += after.processed - before.processed;
  if (before.retired != ~0u && after.retired >= before.retired)
    sample.retired += after.retired - before.retired;
  const double elapsed = StreamMilliseconds(now - began);
  sample.total_ms += elapsed;
  if (elapsed > sample.max_ms) sample.max_ms = elapsed;
  const bool busy = after.submitted != after.retired || after.final_queue != 0;
  if (elapsed >= 10 || busy != sample.busy ||
      (busy && now - sample.last >= std::chrono::milliseconds(100))) {
    REXLOG_INFO("world-release: phase={} manager={:08X} submitted={} processed={} retired={} epoch={} oldest_epoch={} final_queue={} calls={} processed_delta={} retired_delta={} total_ms={:.3f} max_ms={:.3f}",
                phase, manager, after.submitted, after.processed, after.retired,
                after.epoch, after.oldest_epoch, after.final_queue, sample.calls,
                sample.processed, sample.retired, sample.total_ms, sample.max_ms);
    sample.calls = sample.processed = sample.retired = 0;
    sample.total_ms = sample.max_ms = 0;
    sample.last = now;
  }
  sample.busy = busy;
}
}  // namespace

extern "C" REX_FUNC(sub_824798C8) {
  TraceSimSelector(ctx, base, __imp__sub_824798C8, "nearest");
}
extern "C" REX_FUNC(sub_82479A40) {
  TraceSimSelector(ctx, base, __imp__sub_82479A40, "front");
}

// In normal scheduling, 82479C30 drains Pres unloads before Sim unloads and
// loads. Log036 shows their large release fence still pausing Sim for 200ms+.
// Select the EXISTING Sim work first instead. The common dispatcher reads the
// selected record's operation at +4 (8247A3B8), so it supports either selector.
// This changes priority, not residency, and never interrupts an active load.
extern "C" REX_FUNC(sub_82479B78) {
  const uint32_t sys = ctx.r3.u32, kind = ctx.r4.u32, caller = uint32_t(ctx.lr);
  const auto fallback = [&] { __imp__sub_82479B78(ctx, base); };
  struct PriorityWait {
    uint32_t sys = 0;
    uint64_t id = 0;
    bool exhausted = false;
    std::chrono::steady_clock::time_point began{}, last{};
  };
  static thread_local PriorityWait wait;
  if (!skate3::world_loading::IsSkate2Gameplay() ||
      !REXCVAR_GET(skate3_skate2_sim_priority) || kind != 0 ||
      (caller != 0x8247A22C && caller != 0x8247A30C)) {
    fallback();
    return;
  }
  const auto sim_loads = StreamVectorCount(base, sys, 124, 32);
  const auto sim_unloads = StreamVectorCount(base, sys, 172, 32);
  const auto restricted = StreamWord(base, sys, 71460);
  const auto pressure_flags = StreamWord(base, sys, 71464);
  if (sim_loads && sim_loads <= 64 && StreamTimingEnabled()) {
    static thread_local auto last_consider = std::chrono::steady_clock::time_point{};
    const auto now = std::chrono::steady_clock::now();
    if (now - last_consider >= std::chrono::milliseconds(100)) {
      REXLOG_INFO("world-sim-priority: action=consider caller={:08X} sys={:08X} sim_loads={} sim_unloads={} restricted={} pressure_flags={:08X}",
                  caller, sys, sim_loads, sim_unloads, restricted, pressure_flags);
      last_consider = now;
    }
  }
  if (!sim_loads || sim_loads > 64 || sim_unloads > 64 ||
      restricted != 0 || (pressure_flags >> 24) != 0) {
    wait = {};
    fallback();
    return;
  }
  const auto first = StreamWord(base, sys, 124);
  const auto id = StreamId(base, first, 16);
  if (first < 0x10000 || first > 0xfffe0000 || id == ~uint64_t{0}) {
    wait = {};
    fallback();
    return;
  }
  const auto now = std::chrono::steady_clock::now();
  // A changed request starts a new interval. Clear the request on successful
  // selection so revisiting the same boundary receives a fresh budget.
  if (wait.sys != sys || wait.id != id) wait = {sys, id, false, now, {}};
  if (wait.exhausted || now - wait.began >= std::chrono::milliseconds(250)) {
    if (!wait.exhausted && StreamTimingEnabled())
      REXLOG_INFO("world-sim-priority: id={:016X} action=fallback waited_ms={:.3f}",
                  id, StreamMilliseconds(now - wait.began));
    wait.exhausted = true;
    fallback(); // Bounded deferral; never indefinitely starve normal unloads.
    return;
  }
  ctx.r4.u64 = 1;
  const char* action = "unload-sim";
  if (sim_unloads) {
    __imp__sub_82479B78(ctx, base);
  } else if (StreamWord(base, sys, 71456) != 0) {
    action = "load-sim-nearest";
    sub_824798C8(ctx, base);
  } else {
    action = "load-sim-front";
    sub_82479A40(ctx, base);
  }
  const bool selected = (ctx.r3.u32 & 255) != 0;
  if (StreamTimingEnabled() && (selected ||
      now - wait.last >= std::chrono::milliseconds(100))) {
    REXLOG_INFO("world-sim-priority: id={:016X} action={} selected={} waited_ms={:.3f}",
                id, action, selected, StreamMilliseconds(now - wait.began));
    wait.last = now;
  }
  if (selected && !sim_unloads) wait = {};
}
extern "C" REX_FUNC(sub_82C9D0D8) {
  TraceReleasePhase(ctx, base, [](auto& call, uint8_t* memory) {
    ProcessWorldReleases(call, memory);
  }, "process");
}
extern "C" REX_FUNC(sub_82C9D3F0) {
  TraceReleasePhase(ctx, base, __imp__sub_82C9D3F0, "retire");
}

namespace {
thread_local bool building_bam_sim_neighbors = false;
thread_local uint32_t expanded_sim_queries = 0;
}

extern "C" REX_FUNC(sub_82476158) {
  // Only the first query call per cell, inside 8247CB80. Later iterator
  // calls reuse these stack-local bounds and must not expand them again.
  if (building_bam_sim_neighbors && uint32_t(ctx.lr) == 0x8247CDAC) {
    constexpr std::array<uint32_t, 4> offsets{16, 24, 32, 40};
    std::array<float, 4> bounds{};
    bool valid = true;
    for (unsigned i = 0; i < 4; ++i) {
      uint32_t bits = 0;
      valid &= GuestU32(base, ctx.r3.u32 + offsets[i], bits);
      std::memcpy(&bounds[i], &bits, 4);
    }
    if (valid && skate3::sim_extent::Expand(bounds)) {
      for (unsigned i = 0; i < 4; ++i) {
        uint32_t bits;
        std::memcpy(&bits, &bounds[i], 4);
        bits = __builtin_bswap32(bits);
        std::memcpy(base + ctx.r3.u32 + offsets[i], &bits, 4);
      }
      ++expanded_sim_queries;
    }
  }
  __imp__sub_82476158(ctx, base);
}

// Compare the engine-built neighbor lists in native worlds and BAM. This is
// setup-time only; adapt BAM Sim query extents without changing the index.
extern "C" REX_FUNC(sub_8247CB80) {
  const auto group = ctx.r3.u32;
  const bool previous_scope = building_bam_sim_neighbors;
  const auto previous_count = expanded_sim_queries;
  building_bam_sim_neighbors = skate2_loading.load() &&
      REXCVAR_GET(skate3_skate2_sim_grid_extent) &&
      StreamWord(base, group, 24) == 1 && StreamWord(base, group, 600) == 200;
  expanded_sim_queries = 0;
  __imp__sub_8247CB80(ctx, base);
  const auto adjusted = expanded_sim_queries;
  building_bam_sim_neighbors = previous_scope;
  expanded_sim_queries = previous_count;
  if (adjusted) REXLOG_INFO("world-sim-grid-extent: group={:08X} expanded_queries={} extent=195 grid_step=200", group, adjusted);
  if (!REXCVAR_GET(skate3_world_load_probe)) return;
  const auto kind = StreamWord(base, group, 24);
  const auto begin = StreamWord(base, group, 608);
  const auto end = StreamWord(base, group, 612);
  if (kind > 2 || begin == ~0u || end < begin || (end - begin) % 384 ||
      (end - begin) / 384 > 4096) return;
  const auto count = (end - begin) / 384;
  uint32_t minimum = ~0u, maximum = 0, single = 0, total = 0;
  for (uint32_t i = 0; i < count; ++i) {
    const auto neighbors = StreamWord(base, begin + i * 384, 376);
    if (neighbors > 64) return;
    if (neighbors < minimum) minimum = neighbors;
    if (neighbors > maximum) maximum = neighbors;
    single += neighbors == 1;
    total += neighbors;
  }
  REXLOG_INFO("world-neighbor-config: bam={} group={:08X} kind={} grid_step={} cells={} neighbors_min={} neighbors_max={} single_cell_lists={} neighbor_total={}",
              skate2_loading.load(), group, kind, StreamWord(base, group, 600),
              count, count ? minimum : 0, maximum, single, total);
}

// Capture the worker descriptor and decoder arguments during BAM loading and
// gameplay. Persist low-output evidence before entering the guest decoder.
// Never fake successful decompression or replace the guest allocation.
namespace {
thread_local std::array<uint32_t, 8> decompression_descriptor{};
thread_local bool decompression_descriptor_active = false;
}
extern "C" REX_FUNC(sub_82A86A40) {
  if (!skate2_loading.load() || !REXCVAR_GET(skate3_world_load_probe)) {
    __imp__sub_82A86A40(ctx, base); return;
  }
  const auto previous = decompression_descriptor;
  const auto previous_active = decompression_descriptor_active;
  decompression_descriptor_active = true;
  for (uint32_t i = 0; i < 8; ++i)
    decompression_descriptor[i] = StreamWord(base, ctx.r3.u32, i * 4);
  __imp__sub_82A86A40(ctx, base);
  decompression_descriptor = previous;
  decompression_descriptor_active = previous_active;
}

extern "C" REX_FUNC(sub_82A867C8) {
  skate3::perf_recorder::WorkTimelineScope work(skate3::perf_recorder::WorkPhase::kGuestDecompress);
  skate3::perf_recorder::Count(skate3::perf_recorder::Activity::kDecompress);
  if (skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe)) {
    uint32_t header0 = 0, header1 = 0;
    GuestU32(base, ctx.r4.u32, header0);
    GuestU32(base, ctx.r4.u32 + 4, header1);
    const auto& d = decompression_descriptor;
    static std::atomic<uint32_t> decoder_samples{0};
    if (ctx.r3.u32 < 0x20000 || decoder_samples.fetch_add(1) < 64)
    REXLOG_INFO("world-decompress-begin: output={:08X} input={:08X} r5={:08X} r6={:08X} caller={:08X} header={:08X}{:08X} descriptor_valid={} descriptor=[{:08X},{:08X},{:08X},{:08X},{:08X},{:08X},{:08X},{:08X}]",
                ctx.r3.u32, ctx.r4.u32, ctx.r5.u32, ctx.r6.u32, uint32_t(ctx.lr),
                header0, header1, decompression_descriptor_active,
                d[0], d[1], d[2], d[3], d[4], d[5], d[6], d[7]);
    if (ctx.r3.u32 < 0x20000) {
      // Flush a small independent evidence file before entering the failing
      // guest routine; asynchronous logger buffers may not survive a crash.
      static std::mutex evidence_mutex;
      std::lock_guard lock(evidence_mutex);
      std::ofstream evidence("skate2_decompression_failure.txt", std::ios::app);
      evidence << std::hex << "output=" << ctx.r3.u32 << " input=" << ctx.r4.u32
               << " caller=" << uint32_t(ctx.lr) << " header=" << header0 << ',' << header1
               << " descriptor_valid=" << decompression_descriptor_active;
      for (auto word : d) evidence << ' ' << word;
      evidence << std::endl;
      REXLOG_ERROR("world-decompress-invalid-output: output={:08X} input={:08X}", ctx.r3.u32, ctx.r4.u32);
    }
  }
  __imp__sub_82A867C8(ctx, base);
}

// Read-only allocation research before introducing any collision retention.
namespace {
thread_local uint32_t spatial_build_object = 0;
thread_local bool spatial_build_reported = false;
}
extern "C" REX_FUNC(sub_82C1E6C0) {
  if (!skate2_loading.load() || !REXCVAR_GET(skate3_world_load_probe)) {
    __imp__sub_82C1E6C0(ctx, base); return;
  }
  const auto object = ctx.r3.u32;
  const auto sources = StreamVectorCount(base, ctx.r4.u32, 0, 4);
  const auto previous_object = spatial_build_object;
  const auto previous_reported = spatial_build_reported;
  spatial_build_object = object;
  spatial_build_reported = false;
  const auto began = std::chrono::steady_clock::now();
  REXLOG_INFO("world-spatial-build: phase=begin object={:08X} sources={} pool={}",
              object, sources, StreamWord(base, object, 80));
  __imp__sub_82C1E6C0(ctx, base);
  REXLOG_INFO("world-spatial-build: phase=end object={:08X} records={} array={:08X} elapsed_ms={:.3f}",
              object, StreamWord(base, object, 36), StreamWord(base, object, 32),
              StreamMilliseconds(std::chrono::steady_clock::now() - began));
  spatial_build_object = previous_object;
  spatial_build_reported = previous_reported;
}
extern "C" REX_FUNC(sub_82C1E568) {
  if (spatial_build_object && !spatial_build_reported) {
    spatial_build_reported = true;
    const auto count = StreamWord(base, spatial_build_object, 36);
    REXLOG_INFO("world-spatial-build: phase=allocation object={:08X} records={} array_bytes={} array={:08X} destination={:08X} tree={:08X} pool={}",
                spatial_build_object, count, uint64_t(count) * 48,
                StreamWord(base, spatial_build_object, 32), ctx.r3.u32,
                StreamWord(base, spatial_build_object, 8),
                StreamWord(base, spatial_build_object, 80));
  }
  __imp__sub_82C1E568(ctx, base);
}
// The stock gameplay update schedules before gathering the current focus.
// A Pres load can therefore claim the serialized loader on the exact frame a
// Sim boundary is crossed (log039, 06:37:14.581). Refresh through the original
// monitor routine before entering the scheduler's locks, using a separate
// register context. Keep the normal post-scheduler refresh for readiness.
template <typename Context>
void GatherBeforeWorldSchedule(Context& ctx, uint8_t* base) {
  if (uint32_t(ctx.lr) != 0x82477A8C ||
      !skate3::world_loading::IsSkate2Gameplay() ||
      !REXCVAR_GET(skate3_skate2_early_gather)) return;
  const uint32_t sys = ctx.r3.u32;
  const auto before = StreamVectorCount(base, sys, 124, 32);
  auto gather = ctx;
  __imp__sub_8247A4B8(gather, base);
  const auto after = StreamVectorCount(base, sys, 124, 32);
  if (StreamTimingEnabled() && before != after) {
    REXLOG_INFO("world-stream-early-gather: sys={:08X} sim_before={} sim_after={} operation={:016X}",
                sys, before, after, StreamId(base, sys, 71408));
  }
}

// The active XSF path uses a 192-byte request, not the older collection task
// below. 82C96300 owns its lifetime and frees it only after this call returns.
// Original 82C96E58 submits at most 16 records / 4 MiB of reference weight per
// invocation. Continue only on observable progress, preserving its ring checks.
extern "C" REX_FUNC(sub_82C96E58) {
  const uint32_t task = ctx.r3.u32, caller = uint32_t(ctx.lr);
  if (caller != 0x82C96334 || !skate3::world_loading::IsSkate2Gameplay()) {
    __imp__sub_82C96E58(ctx, base);
    return;
  }
  struct Snapshot {
    bool valid = false, waiting = false;
    std::array<uint32_t, 4> fields{};
    bool operator==(const Snapshot&) const = default;
  };
  const auto read = [&]() {
    Snapshot s;
    s.fields = {StreamWord(base, task, 32), StreamWord(base, task, 68),
                StreamWord(base, task, 92), StreamWord(base, task, 96)};
    s.valid = s.fields[0] != ~0u && s.fields[2] != ~0u &&
              s.fields[3] != ~0u && s.fields[3] >= s.fields[2] &&
              s.fields[3] - s.fields[2] < 512;
    // Do not build a large worker backlog. A vanilla call submits <=16;
    // allow additional calls only with fewer than48 outstanding records.
    s.waiting = (s.fields[0] >> 24) == 0 &&
                s.fields[3] - s.fields[2] < 48;
    return s;
  };
  const auto before = read();
  const auto began = std::chrono::steady_clock::now();
  __imp__sub_82C96E58(ctx, base);
  const auto elapsed = [&]() {
    return StreamMilliseconds(std::chrono::steady_clock::now() - began);
  };
  skate3::stream_pump::Result result{};
  if (REXCVAR_GET(skate3_skate2_asset_budget) && before.valid) {
    result = skate3::stream_pump::Run(read, [&]() {
      auto pass = ctx;
      pass.r3.u64 = task;
      __imp__sub_82C96E58(pass, base);
    }, elapsed);
  }
  const auto after = read();
  if (StreamTimingEnabled() && (before != after || elapsed() >= 5)) {
    REXLOG_INFO("world-stream-assets: id={:016X} task={:08X} eof={} completed={} submitted={} done={} extra_calls={} elapsed_ms={:.3f} stop={}",
                StreamId(base, task, 24), task, after.fields[1] == ~0u,
                after.fields[2], after.fields[3], after.fields[0] >> 24,
                result.passes, elapsed(), result.stop);
  }
}

// Capture identity before the completion callback publishes its final counter:
// the request may be reclaimed by its owner immediately after publication.
extern "C" REX_FUNC(sub_82C970E8) {
  if (!StreamTimingEnabled()) {
    __imp__sub_82C970E8(ctx, base);
    return;
  }
  const auto task = StreamWord(base, ctx.r3.u32, 4);
  const auto id = StreamId(base, task, 24);
  const auto completed = StreamWord(base, task, 92);
  const auto submitted = StreamWord(base, task, 96);
  const bool last = completed != ~0u && submitted != ~0u &&
                    completed + 1 == submitted;
  __imp__sub_82C970E8(ctx, base);
  if (last) {
    REXLOG_INFO("world-stream-worker: id={:016X} task={:08X} batch_completed={} submitted_snapshot={}",
                id, task, completed + 1, submitted);
  }
}

// Collection tasks stay alive until 82C97BC0 sees state 7 and destroys them.
// Read only around their normal poll; never advance or wait on a worker here.
extern "C" REX_FUNC(sub_82C98B08) {
  if (!StreamTimingEnabled()) {
    __imp__sub_82C98B08(ctx, base);
    return;
  }
  const uint32_t task = ctx.r3.u32;
  const auto before = StreamWord(base, task, 36);
  const auto began = std::chrono::steady_clock::now();
  __imp__sub_82C98B08(ctx, base);
  const auto now = std::chrono::steady_clock::now();
  const auto after = StreamWord(base, task, 36);
  struct Stage { uint32_t state; std::chrono::steady_clock::time_point since; };
  static thread_local std::unordered_map<uint32_t, Stage> stages;
  if (stages.size() > 256) stages.clear();
  auto [it, inserted] = stages.try_emplace(task, Stage{before, began});
  if (it->second.state != before) it->second = {before, began};
  if (before != after || StreamMilliseconds(now - began) >= 5) {
    REXLOG_INFO("world-stream-task: task={:08X} state_before={} state_after={} observed_stage_ms={:.3f} call_ms={:.3f} path='{}'",
                task, before, after, StreamMilliseconds(now - it->second.since),
                StreamMilliseconds(now - began), GuestString(base, task + 296));
  }
  if (after == 7) stages.erase(it);
  else if (before != after) it->second = {after, now};
}

extern "C" REX_FUNC(sub_82479C30) {
  GatherBeforeWorldSchedule(ctx, base);
  if (!StreamTimingEnabled()) {
    __imp__sub_82479C30(ctx, base);
    return;
  }
  const uint32_t sys = ctx.r3.u32;
  const auto sim_before = StreamVectorCount(base, sys, 124, 32);
  const auto began = std::chrono::steady_clock::now();
  __imp__sub_82479C30(ctx, base);
  const auto now = std::chrono::steady_clock::now();
  const auto sim_after = StreamVectorCount(base, sys, 124, 32);
  struct Sample {
    uint32_t sys = 0;
    bool pending = false;
    double max_call_ms = 0, max_gap_ms = 0;
    std::chrono::steady_clock::time_point previous{}, last{};
  };
  static thread_local Sample sample;
  if (sample.sys != sys) sample = {};
  sample.sys = sys;
  const double elapsed = StreamMilliseconds(now - began);
  const double gap = sample.previous.time_since_epoch().count()
      ? StreamMilliseconds(began - sample.previous) : 0;
  if (elapsed > sample.max_call_ms) sample.max_call_ms = elapsed;
  if (gap > sample.max_gap_ms) sample.max_gap_ms = gap;
  sample.previous = began;
  const bool pending = sim_before != 0 || sim_after != 0;
  if (elapsed >= 10 || pending != sample.pending ||
      (pending && now - sample.last >= std::chrono::milliseconds(100))) {
    const auto manager = StreamWord(base, sys, 16);
    const auto release = ReadReleaseSnapshot(base, manager);
    REXLOG_INFO("world-stream-scheduler: sys={:08X} sim_before={} loads=[{},{},{}] unloads=[{},{},{}] operation={:016X} load_active={:016X} unload_active={:016X} release_target={} retired={} submitted={} processed={} epoch={} max_call_ms={:.3f} max_gap_ms={:.3f}",
                sys, sim_before, StreamVectorCount(base, sys, 108, 32),
                sim_after, StreamVectorCount(base, sys, 140, 32),
                StreamVectorCount(base, sys, 156, 32),
                StreamVectorCount(base, sys, 172, 32),
                StreamVectorCount(base, sys, 188, 32),
                StreamId(base, sys, 71408), StreamId(base, sys, 71416),
                StreamId(base, sys, 71424), StreamWord(base, sys, 71468),
                release.retired, release.submitted, release.processed,
                release.epoch, sample.max_call_ms, sample.max_gap_ms);
    sample.last = now;
    sample.max_call_ms = sample.max_gap_ms = 0;
  }
  sample.pending = pending;
}

// The normal update does scheduler -> monitors -> pause query. A newly gathered
// Sim request or a just-completed callback otherwise waits until the next frame
// to be consumed. Run a bounded number of additional scheduler/monitor passes
// on this same gameplay thread. No simulation update, epoch advance or waits.
extern "C" REX_FUNC(sub_8247A4B8) {
  const uint32_t sys = ctx.r3.u32, caller = uint32_t(ctx.lr);
  __imp__sub_8247A4B8(ctx, base);
  if (caller != 0x82477A94 || !skate3::world_loading::IsSkate2Gameplay() ||
      !REXCVAR_GET(skate3_skate2_stream_pump)) return;
  const uint32_t manager = StreamWord(base, sys, 16);
  // 824783E8 constructs type0 at manager+133000 and type1 at +133004.
  const uint32_t monitor = StreamWord(base, manager, 133004);
  if (StreamWord(base, monitor, 32) != 1 ||
      StreamWord(base, monitor, 28) != 1) return;
  struct Snapshot {
    bool valid = false, waiting = false;
    std::array<uint64_t, 15> fields{};
    bool operator==(const Snapshot&) const = default;
  };
  const auto read = [&]() {
    Snapshot s;
    const auto waiting = StreamWord(base, monitor, 28);
    if (StreamWord(base, monitor, 1400) != 1 || waiting > 1) return s;
    const auto cell = StreamWord(base, monitor, 384);
    s.fields = {StreamWord(base, cell, 116), StreamWord(base, monitor, 340),
                StreamVectorCount(base, sys, 124, 32),
                StreamVectorCount(base, sys, 172, 32),
                StreamVectorCount(base, sys, 71012, 8),
                StreamVectorCount(base, sys, 71028, 8),
                StreamId(base, sys, 71408), StreamId(base, sys, 71416),
                StreamId(base, sys, 71424)};
    for (unsigned i = 0; i < 6; ++i)
      if (s.fields[i] == ~uint32_t{0}) return s;
    // The scheduler's outer queues may be unchanged while its XSF reader
    // advances or worker jobs complete. Include the active request in the
    // progress key; otherwise the bounded pump stops a frame too early.
    // sys+8 is the collection manager; its +4 is the loader (82C97BC0).
    const auto collections = StreamWord(base, sys, 8);
    const auto loader = StreamWord(base, collections, 4);
    const auto task = StreamWord(base, loader, 414864);
    if (task == ~0u) return s;
    s.fields[9] = task;
    if (task) {
      s.fields[10] = StreamWord(base, task, 32); // complete byte
      s.fields[11] = StreamWord(base, task, 48); // reader
      s.fields[12] = StreamWord(base, task, 68); // EOF (-1 is valid)
      s.fields[13] = StreamWord(base, task, 92); // consumed
      s.fields[14] = StreamWord(base, task, 96); // submitted
      if (s.fields[10] == ~uint32_t{0} || s.fields[11] == ~uint32_t{0} ||
          s.fields[13] == ~uint32_t{0} || s.fields[14] == ~uint32_t{0}) return s;
    }
    s.valid = true;
    s.waiting = waiting != 0;
    return s;
  };
  const auto began = std::chrono::steady_clock::now();
  const auto elapsed = [&]() {
    return StreamMilliseconds(std::chrono::steady_clock::now() - began);
  };
  const auto result = skate3::stream_pump::Run(read, [&]() {
    // Use the restored guest stack; retain the first call's returned registers.
    auto pass = ctx;
    pass.r3.u64 = sys;
    sub_82479C30(pass, base);
    pass.r3.u64 = sys;
    __imp__sub_8247A4B8(pass, base);
  }, elapsed);
  if (StreamTimingEnabled()) {
    static thread_local auto last = std::chrono::steady_clock::time_point{};
    const auto now = std::chrono::steady_clock::now();
    if (result.passes > 1 || !read().waiting ||
        now - last >= std::chrono::milliseconds(100)) {
      REXLOG_INFO("world-stream-pump: sys={:08X} passes={} stop={} elapsed_ms={:.3f} sim_wait={}",
                  sys, result.passes, result.stop, elapsed(),
                  StreamWord(base, monitor, 28));
      last = now;
    }
  }
}

// Keep the actual native wait result for the rare freeze snapshot.
extern "C" REX_FUNC(sub_82478B30) {
  const uint32_t manager = ctx.r3.u32;
  __imp__sub_82478B30(ctx, base);
  if (skate3::freeze_capture::Enabled() && skate3::world_loading::IsSkate2Gameplay()) {
    freeze_base.store(base);
    freeze_stream_manager.store(manager);
    freeze_pause_result.store(ctx.r3.u32 & 255);
  }
}

// 82477A68 posts cMsgPauseSim(state 11) from 82478B30. That query
// consumes +28 of the monitors updated here. Capture their actual cell lists,
// rather than the deferred event dispatch stack. Never change readiness.
extern "C" REX_FUNC(sub_8247DF98) {
  const uint32_t monitor = ctx.r3.u32;
  __imp__sub_8247DF98(ctx, base);
  if (!REXCVAR_GET(skate3_world_load_probe)) return;
  uint32_t kind = 0, waiting = 0, count = 0, ready = 0, bytes = 0;
  if (!GuestU32(base, monitor + 32, kind) || kind > 2 ||
      !GuestU32(base, monitor + 28, waiting) ||
      !GuestU32(base, monitor + 1400, count) || count > 64) return;
  const bool bam = bam_gameplay.load();
  if (!bam && kind != 1) return;
  GuestU32(base, monitor + 340, ready);
  GuestU32(base, monitor + 348, bytes);
  // Read-only diagnostic snapshots: the worker may update a cell's status
  // concurrently. The original monitor result remains the authoritative wait.
  struct Cell { uint32_t hi, lo, state, category, bytes; };
  std::array<Cell, 64> cells{};
  uint64_t signature = 14695981039346656037ull;
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t entry = 0, flags = 0;
    auto& c = cells[i];
    if (!GuestU32(base, monitor + 384 + 16 * i, entry) || !entry ||
        !GuestU32(base, entry + 64, c.hi) ||
        !GuestU32(base, entry + 68, c.lo) ||
        !GuestU32(base, entry + 116, flags)) return;
    c.state = (flags >> 8) & 255;
    c.category = flags >> 24;
    GuestU32(base, entry + 104, c.bytes);
    for (auto value : {c.hi, c.lo, c.state})
      signature = (signature ^ value) * 1099511628211ull;
  }
  struct Sample {
    uint32_t waiting = ~0u;
    uint64_t signature = 0;
    std::chrono::steady_clock::time_point last{}, since{};
  };
  static std::mutex mutex;
  static std::unordered_map<uint32_t, Sample> samples;
  const auto now = std::chrono::steady_clock::now();
  std::lock_guard lock(mutex);
  if (samples.size() >= 16 && !samples.contains(monitor)) samples.clear();
  auto& previous = samples[monitor];
  const bool edge = previous.waiting != waiting;
  if (!edge && (signature == previous.signature ||
      now - previous.last < std::chrono::milliseconds(500))) return;
  const auto wait_ms = previous.waiting == 1
      ? std::chrono::duration_cast<std::chrono::milliseconds>(now - previous.since).count() : 0;
  if (edge && waiting) previous.since = now;
  REXLOG_INFO("{}-stream-monitor: kind={} monitor={:08X} wait={} elapsed_ms={} cells={} ready={} pending_assets={}",
              bam ? "world" : "native", kind, monitor, waiting, wait_ms, count, ready, bytes);
  for (uint32_t i = 0; i < count; ++i) {
    const auto& c = cells[i];
    REXLOG_INFO("{}-stream-cell: kind={} id={:08X}{:08X} state={} category={} bytes={}",
                bam ? "world" : "native", kind, c.hi, c.lo, c.state, c.category, c.bytes);
  }
  previous.waiting = waiting;
  previous.signature = signature;
  previous.last = now;
}

// Trace the guest view pipeline upstream of mesh capture. Empty captures
// alone cannot distinguish a stopped view pipeline from empty visibility lists.
namespace {
void TraceWorldView(uint8_t* base, uint32_t view, size_t stage) {
  view_stage_calls[stage].fetch_add(1);
  if (!REXCVAR_GET(skate3_world_load_probe)) return;
  static std::mutex mutex;
  static std::array<std::chrono::steady_clock::time_point, 3> last{};
  std::lock_guard<std::mutex> lock(mutex);
  const auto now = std::chrono::steady_clock::now();
  if (now - last[stage] < std::chrono::seconds(1)) return;
  last[stage] = now;
  uint32_t camera = 0, projection = 0;
  GuestU32(base, view + 8, camera);
  if (camera) GuestU32(base, camera + 0x8c, projection);
  std::array<uint32_t, 3> sizes{};
  size_t i = 0;
  for (uint32_t offset : {20128u, 16092u, 20160u}) {
    uint32_t begin = 0, end = 0;
    if (GuestU32(base, view + offset, begin) && GuestU32(base, view + offset + 4, end) && end >= begin)
      sizes[i] = end - begin;
    ++i;
  }
  REXLOG_INFO("world-view: stage={} bam={} view={:08X} camera={:08X} projection23={:08X} candidate_bytes={} visible_bytes={} sorted_bytes={}",
              stage, skate2_loading.load(), view, camera, projection, sizes[0], sizes[1], sizes[2]);
}
}
extern "C" REX_FUNC(sub_827FA7F0) {
  const uint32_t view = ctx.r3.u32;
  __imp__sub_827FA7F0(ctx, base);
  TraceWorldView(base, view, 0);
}
extern "C" REX_FUNC(sub_827FAAA0) {
  const uint32_t view = ctx.r3.u32;
  __imp__sub_827FAAA0(ctx, base);
  TraceWorldView(base, view, 1);
}
extern "C" REX_FUNC(sub_827FB158) {
  TraceWorldView(base, ctx.r3.u32, 2);
  __imp__sub_827FB158(ctx, base);
}

// World loader setup reads the world attribute collection identified by r4.
// The seven fixed 256-byte fields below are populated by sub_82476608; the
// state machine sub_82476AC8 consumes them before issuing asynchronous reads.
extern "C" REX_FUNC(sub_82476608) {
  const uint32_t loader = ctx.r3.u32;
  const uint64_t world = ctx.r4.u64;
  __imp__sub_82476608(ctx, base);
  skate2_loading.store(false);
  bam_gameplay.store(false);
  if (skate2_ready.load() && world == template_world.load() && world != 0) {
    // These are the actual fixed-size fields filled by the original function.
    // Require the observed retail layout before changing either field.
    if (GuestString(base, loader + 364) == "data/content/world/stream") {
      const auto original_name = GuestString(base, loader + 620);
      const auto district_water = GuestString(base, loader + 1644);
      // The armed Skate 2 stream: BAM, or a DLC map (Install keeps names
      // under 64 characters; the field holds 256).
      const int stream_index =
          std::clamp(requested_stream.load(), 0, int(skate2_streams.size()) - 1);
      const std::string& stream = skate2_streams[stream_index];
      // The existing state-1 empty-archive branch advances to state 4 without
      // mounting a BIG. Streaming then opens the stream's paths via VFS.
      base[loader + 108] = 0;
      std::memcpy(base + loader + 620, stream.c_str(), stream.size() + 1);
      loaded_stream.store(stream_index);
      // The template district's own water model (+1644, textures +1900;
      // Downtown's is world/models/DIST_Water_DownTown) is Skate 3 harbor
      // water: a 20 km plane at y -61.8 that covered every New San Van
      // surface below it (the ocean, beaches, channels, low pools, bowl
      // floors) in black. New San Van's water is Skate 2's own (DIST_Water
      // through the world model component, BAM water meshes). Empty fields
      // take the retail no-water path (state 4 -> 8), as worlds without a
      // district water model do.
      base[loader + 1644] = 0;
      base[loader + 1900] = 0;
      skate2_loading.store(true);
      template_world.store(0);
      template_deadline.store(0);
      menu_previous_loaded.store(0);
      REXLOG_INFO("skate2: shortcut loader={:08X} template='{}' -> {} (district water '{}' not loaded)",
                  loader, original_name, stream, district_water);
      HoldCareerAutosave(base, false);
    } else {
      ArmTemplate(0);
      REXLOG_ERROR("skate2: unexpected world configuration; leaving loader unchanged");
    }
  } else {
    bam_proxy_target_world.store(0);
    bam_model_target_world.store(0);
  }
  // Publish after the destination/layout checks, independently of the later
  // gameplay callback. RenderScene can keep running throughout a map load.
  const uint64_t serial = (render_world_identity.load(std::memory_order_relaxed) & ~uint64_t(1)) + 2;
  const bool nsv = skate2_loading.load();
  const uint64_t identity = serial | uint64_t(nsv);
  uint32_t indoor_map = 0;
  const int stream = loaded_stream.load();
  if (nsv && stream >= 0 && stream < int(skate2_streams.size())) {
    indoor_map = skate2_streams[stream] == "BAM" ? 1
               : skate2_streams[stream] == "DLC_Community_Center" ? 2
               : skate2_streams[stream] == "DLC_Rob_Fantasy_Plaza" ? 3
               : skate2_streams[stream] == "DLC_School" ? 4
               : skate2_streams[stream] == "DLC_Parkade" ? 5 : 0;
  }
  // Publish the paired selection first: an old identity can only see no
  // lighting mask while the next map is being configured, never its mask.
  indoor_world_identity.store((identity << 3) | indoor_map, std::memory_order_release);
  render_world_identity.store(identity, std::memory_order_release);
  if (!REXCVAR_GET(skate3_world_load_probe)) return;
  REXLOG_INFO("world-load: configure loader={:08X} world={:016X}", loader, world);
  for (uint32_t offset : {108u, 364u, 620u, 1132u, 1388u, 1644u, 1900u}) {
    REXLOG_INFO("world-load: field +{}='{}'", offset, GuestString(base, loader + offset));
  }
}

// Audio side of a world load. The game-state thunk 826DC808 passes the world
// key; this reads the world's aud_worlddata reference into the audio world
// (manager+488, key +272, state +280), whose per-frame update 824A24F8 then
// loads that record's emitter maps through 828DCCF8. It runs before the
// stream loader is configured (run 120: the map request precedes 82476608 by
// about 1 s), so a New San Van load is still the armed template here.
extern "C" REX_FUNC(sub_82489490) {
  const uint64_t world = ctx.r4.u64;
  const bool original = lw_assets_ready && REXCVAR_GET(skate3_skate2_original_vehicles) &&
      world != 0 && world == template_world.load() && LivingWorldStream(requested_stream.load());
  if (lw_original_audio.exchange(original) != original) {
    const auto generation = ++lw_audio_generation;
    REXLOG_INFO("skate2-lw: traffic audio transition requested generation={} source={}",
                generation, original ? "original" : "retail");
  }
  __imp__sub_82489490(ctx, base);
  int stream = -1;
  if (world != 0 && world == template_world.load()) {
    const int requested = requested_stream.load();
    if (requested >= 0 && requested < int(skate2_streams.size()) &&
        skate2_audio_maps.contains(skate2_streams[requested])) {
      stream = requested;
    }
  }
  audio_stream.store(stream);
  if (!skate2_audio_maps.empty()) {
    REXLOG_INFO("skate2-audio: world {:016X} audio: {}", world,
                stream >= 0 ? "Skate 2 maps of " + skate2_streams[stream] : std::string("retail"));
  }
}

// Use the audio controller's own thread and native release queue. An audio
// world change does not normally reload traffic's global banks. Release their
// controller reference once, wait for all engines/effects to become unused,
// then arm the existing per-frame loader. Never replace live graph data.
// Subsequent retail worlds restore the retail banks through that same loader.
extern "C" REX_FUNC(sub_824854A8) {
  static uint64_t applied = 0, waiting_frames = 0;
  static bool releasing = false;
  static uint32_t owner = 0, bank_manager = 0;
  const uint64_t generation = lw_audio_generation.load();
  const uint32_t controller = ctx.r3.u32;
  if (generation != applied && controller) {
    uint32_t manager = 0;
    if (GuestU32(base, 0x830CFDCC, manager) && manager) {
      if (owner != controller || bank_manager != manager) {
        owner = controller; bank_manager = manager; releasing = false;
      }
      if (!releasing) {
        auto call = ctx;
        call.r3.u32 = controller;
        __imp__sub_82487408(call, base);
        releasing = true;
        waiting_frames = 0;
        REXLOG_INFO("skate2-lw: traffic audio releasing previous banks generation={}", generation);
      }
      uint32_t occupied = 0, references = 0;
      bool readable = true;
      for (uint32_t group : {9u, 10u}) {
        const uint32_t count = group == 9 ? 8u : 3u;
        for (uint32_t index = 0; index < count; ++index) {
          const uint32_t slot = manager + 12 + (40 * group + index) * 100;
          uint32_t state = 0, refs = 0;
          readable &= GuestU32(base, slot, state) && GuestU32(base, slot + 8, refs);
          occupied += state != 0;
          references += refs;
        }
      }
      if (readable && occupied == 0 && references == 0) {
        // 824854A8 calls 82487168(r4=0) when the traffic phase is 2.
        StoreGuestU32(base, controller + 0x254, 2);
        applied = generation;
        releasing = false;
        REXLOG_INFO("skate2-lw: traffic audio reload armed generation={} source={}",
                    generation, lw_original_audio.load() ? "original" : "retail");
      } else if (LivingWorldLogSample(++waiting_frames)) {
        REXLOG_INFO("skate2-lw: traffic audio waiting generation={} readable={} occupied={} refs={}",
                    generation, readable, occupied, references);
      }
    }
  }
  __imp__sub_824854A8(ctx, base);
}

// Traffic's normal loader uses S3's BIG size index to pack a shared arena.
// Original banks have different sizes. Queue the native owning-buffer loader
// instead, in the same controller slots. Its worker sizes the real file;
// the normal traffic teardown (82487408 -> 828DC760) releases these buffers.
// CSI callback names and consumed engine tuning are shared by both titles;
// retain the native callback registry and load the original sound graphs/data.
extern "C" REX_FUNC(sub_828DC380) {
  const uint32_t group = ctx.r7.u32, index = ctx.r8.u32;
  if (!lw_original_audio.load() || !lw_assets_ready || (group != 9 && group != 10)) {
    __imp__sub_828DC380(ctx, base); return;
  }
  const std::string original = GuestString(base, ctx.r4.u32);
  constexpr std::string_view prefix = "data/audio/";
  const std::string name = original.starts_with(prefix) ? original.substr(prefix.size()) : std::string();
  if (!lw_traffic_banks.contains(name)) { __imp__sub_828DC380(ctx, base); return; }
  const uint32_t manager = ctx.r3.u32;
  const uint32_t slot = manager + 12 + (40 * group + index) * 100;
  uint32_t state = 0, references = 0;
  if (index >= 40 || !GuestU32(base, slot, state) || !GuestU32(base, slot + 8, references) ||
      state != 0 || references != 0) {
    REXLOG_ERROR("skate2-lw: original traffic bank refused: slot still active group={} index={} state={} refs={}",
                 group, index, state, references);
    __imp__sub_828DC380(ctx, base); return;
  }
  const std::string path = "data/audio/nsv_s2_traffic/" + name;
  const uint32_t guest = rex::system::kernel_memory()->SystemHeapAlloc(uint32_t(path.size() + 1), 16);
  if (!guest) { REXLOG_ERROR("skate2-lw: traffic bank path allocation failed"); __imp__sub_828DC380(ctx, base); return; }
  std::memcpy(base + guest, path.c_str(), path.size() + 1);
  base[slot + 0x58] = 0;  // Owned by the native file loader, not the packed arena.
  ctx.r4.u32 = guest;
  ctx.r5.u32 = group;
  ctx.r6.u32 = index;
  ctx.r7.u32 = 1;  // Native ABK graph/data bank.
  ctx.r8.u32 = 0;
  __imp__sub_828DC290(ctx, base);
  // The job copies its path into the native descriptor before returning.
  rex::system::kernel_memory()->SystemHeapFree(guest);
  if (REXCVAR_GET(skate3_skate2_livingworld_probe))
    REXLOG_INFO("skate2-lw: original traffic bank queued group={} index={} path='{}' allocation=owned", group, index, path);
}

// Report successful native graph/data registration, separately from queuing.
extern "C" REX_FUNC(sub_828DC660) {
  const uint32_t slot = ctx.r4.u32;
  const std::string path = GuestString(base, slot + 12);
  __imp__sub_828DC660(ctx, base);
  if (REXCVAR_GET(skate3_skate2_livingworld_probe) && path.starts_with("data/audio/nsv_s2_traffic/")) {
    uint32_t state = 0, data = 0, bytes = 0;
    GuestU32(base, slot, state); GuestU32(base, slot + 0x4C, data); GuestU32(base, slot + 0x50, bytes);
    REXLOG_INFO("skate2-lw: original traffic bank registered path='{}' state={} data={:08X} bytes={} owned={}",
                path, state, data, bytes, base[slot + 0x58] == 0);
  }
}

extern "C" REX_FUNC(sub_828DC760) {
  const uint32_t group = ctx.r4.u32, index = ctx.r5.u32;
  const uint32_t slot = ctx.r3.u32 + 12 + (40 * group + index) * 100;
  const bool traffic = (group == 9 || group == 10) && index < 40;
  const std::string path = traffic ? GuestString(base, slot + 12) : std::string();
  __imp__sub_828DC760(ctx, base);
  if (path.starts_with("data/audio/nsv_s2_traffic/")) {
    uint32_t state = 0, refs = 0;
    GuestU32(base, slot, state); GuestU32(base, slot + 8, refs);
    REXLOG_INFO("skate2-lw: original traffic bank release group={} index={} state={} refs={}", group, index, state, refs);
  }
}

// Emitter map load from the audio world update (return 824A266C): r4 is the
// formatted "data\audio\<map>" in the audio world's own 256-byte buffer.
// For New San Van's audio world each template map is replaced by the Skate 2
// map of the same role (the name prefix: sfx_, music_, reverb_, crowds_,
// speakers_).
extern "C" REX_FUNC(sub_828DCCF8) {
  const int stream = audio_stream.load();
  if (stream >= 0 && uint32_t(ctx.lr) == 0x824A266C) {
    constexpr std::string_view kPrefix = "data\\audio\\";
    const uint32_t path = ctx.r4.u32;
    const std::string original = GuestString(base, path);
    const auto underscore = original.find('_', kPrefix.size());
    if (original.starts_with(kPrefix) && original.ends_with(".ems") &&
        underscore != std::string::npos) {
      const auto role = std::find(
          kAudioRoles.begin(), kAudioRoles.end(),
          std::string_view(original).substr(kPrefix.size(), underscore - kPrefix.size()));
      if (role != kAudioRoles.end()) {
        const std::string replacement =
            "data\\audio\\skate2\\" +
            skate2_audio_maps.at(skate2_streams[stream])[role - kAudioRoles.begin()];
        std::memcpy(base + path, replacement.c_str(), replacement.size() + 1);
        if (REXCVAR_GET(skate3_world_load_probe)) REXLOG_INFO("skate2-audio: {} -> {}", original, replacement);
      }
    }
  }
  __imp__sub_828DCCF8(ctx, base);
}

// ---- Skate 2 WorldPainter layers (tools/skate2_worldpainter.py) -------------
// New San Van's cells keep Skate 2's paint data, which Skate 3 ignores: a
// Skate 2 layer (EB000F: u32 quadtree, u32 dictionary, u64 key) is keyed by
// its eLayerID ((id << 32) | 0x03E387C2), a Skate 3 one by h64 of its name;
// a Skate 2 dictionary value (EB0011) is one word, a paint value index, while
// Skate 3 reads two words at each entry, (word1 << 32) | word0 = h64 of the
// record painted (sub_82C0EAC0). The RW fixups below run as each cell loads;
// after the game's own fixup they rename the translated layers and point each
// Skate 2 dictionary entry at its Skate 3 key in a guest table. Both forms are
// unmistakable (no Skate 3 key carries the Skate 2 asset type; a Skate 3
// dictionary holds two words per value), so Skate 3 cells are never touched.
// A Skate 2 dictionary keeps its own values in entry order after the entry
// table, so a refix after a relocation (unfix 82964A50 turns the redirected
// entries into stray offsets) is translated again from them.
namespace {
constexpr uint32_t kSkate2LayerType = 0x03E387C2;

void CreatePaintTable(uint8_t* base) {
  const uint32_t count = uint32_t(paint_values.size()) + 1;
  const uint32_t table = rex::system::kernel_memory()->SystemHeapAlloc(count * 8, 16);
  if (table == 0) {
    REXLOG_ERROR("skate2-audio: paint key table allocation failed; painted audio disabled");
    return;
  }
  std::memset(base + table, 0, count * 8);
  paint_zero_slot = table;
  uint32_t slot = table + 8;
  for (const auto& [value, key] : paint_values) {
    // Skate 3's dictionary order: the low word, then the high word.
    StoreGuestU32(base, slot, uint32_t(key));
    StoreGuestU32(base, slot + 4, uint32_t(key >> 32));
    paint_slots[value] = slot;
    slot += 8;
  }
}

void TranslatePaintLayer(uint8_t* base, uint32_t layer) {
  uint64_t key = 0;
  if (paint_layers.empty() || !GuestU64(base, layer + 8, key) ||
      uint32_t(key) != kSkate2LayerType) {
    return;
  }
  const uint32_t source_layer = uint32_t(key >> 32);
  const auto it = paint_layers.find(source_layer);
  if (it == paint_layers.end() || (source_layer == 5 && !OriginalVehicles()) ||
      (source_layer == 2 && !OriginalPedestrians())) return;
  StoreGuestU32(base, layer + 8, uint32_t(it->second >> 32));
  StoreGuestU32(base, layer + 12, uint32_t(it->second));
  const uint32_t n = paint_layer_count.fetch_add(1) + 1;
  if (REXCVAR_GET(skate3_world_load_probe) && (n <= 8 || (n & (n - 1)) == 0)) {
    const auto name = paint_names.find(it->second);
  REXLOG_INFO("skate2-audio: paint layer {:02X} -> {} ({} translated)", key >> 32,
                name != paint_names.end() ? name->second : std::string(), n);
  }
}

void TranslatePaintDictionary(uint8_t* base, uint32_t dictionary) {
  uint32_t words = 0, count = 0, table = 0, values = 0;
  if (paint_layers.empty() || !GuestU32(base, dictionary, words) ||
      !GuestU32(base, dictionary + 4, count) || count == 0 || count > 64 || words != count ||
      !GuestU32(base, dictionary + 12, table) || !GuestU32(base, dictionary + 16, values) ||
      table != dictionary + 0x14 || values != table + 8 * count) {
    return;
  }
  std::call_once(paint_table_once, CreatePaintTable, base);
  if (paint_zero_slot == 0) return;
  for (uint32_t i = 0; i < count; ++i) {
    uint32_t value = 0;
    GuestU32(base, values + 4 * i, value);
    const auto it = paint_slots.find(value);
    const bool allowed = !lw_paint_values.contains(value) ||
        (lw_ped_paint_values.contains(value) ? OriginalPedestrians() : OriginalVehicles());
    StoreGuestU32(base, table + 8 * i, allowed && it != paint_slots.end() ? it->second : paint_zero_slot);
    StoreGuestU32(base, table + 8 * i + 4, 2);
  }
  paint_dictionary_count.fetch_add(1);
}
}  // namespace

// EB000F (WorldPainter layer) fixups: quadtree and dictionary references to
// pointers. 82964B58 is the variant taking the arena in r5.
extern "C" REX_FUNC(sub_82964BC8) {
  const uint32_t layer = ctx.r3.u32;
  __imp__sub_82964BC8(ctx, base);
  TranslatePaintLayer(base, layer);
}

extern "C" REX_FUNC(sub_82964B58) {
  const uint32_t layer = ctx.r3.u32;
  __imp__sub_82964B58(ctx, base);
  TranslatePaintLayer(base, layer);
}

// EB0011 (WorldPainter dictionary) fixup: the entry table and value offsets to
// pointers (82964AA0 tail-calls it).
extern "C" REX_FUNC(sub_82964AA8) {
  const uint32_t dictionary = ctx.r3.u32;
  __imp__sub_82964AA8(ctx, base);
  TranslatePaintDictionary(base, dictionary);
}

// Keep the original cache initialization and destruction ownership. Extra
// entries have only imported model keys; retail entity selection is untouched.
extern "C" REX_FUNC(sub_827B9CF0) {
  const uint32_t manager = ctx.r3.u32;
  lw_pose_manager.store(manager);
  lw_pose_prepared_manager.store(0);
  __imp__sub_827B9CF0(ctx, base);
  PrepareOriginalVehiclePoses(ctx, base, manager);
}

extern "C" REX_FUNC(sub_827B9488) {
  uint32_t manager = ctx.r3.u32;
  lw_pose_manager.compare_exchange_strong(manager, 0);
  manager = ctx.r3.u32;
  lw_pose_prepared_manager.compare_exchange_strong(manager, 0);
  __imp__sub_827B9488(ctx, base);
}

extern "C" REX_FUNC(sub_828DA2C8) {
  const uint32_t output = ctx.r5.u32;
  const bool imported = lw_pose_query && ctx.r3.u64 == 0x4E740A6670F241A7 &&
                        ctx.r4.u64 == 0x8BE1D151F9DA2D29 && uint32_t(ctx.lr) == 0x827B9D40;
  if (imported) ctx.r4.u64 = kOriginalVehiclesGroup;
  __imp__sub_828DA2C8(ctx, base);
  if (imported) {
    uint32_t begin = 0, end = 0;
    if (GuestU32(base, output, begin) && GuestU32(base, output + 4, end) && end >= begin)
      lw_pose_models = (end - begin) / 8;
  }
}

extern "C" REX_FUNC(sub_823C3B00) {
  const bool pose = lw_pose_query && uint32_t(ctx.lr) == 0x827B9E70;
  const std::string name = pose ? GuestString(base, ctx.r4.u32) : std::string();
  // Andale identifiers pack six characters per word. Prefixing a filename
  // changes every pose lookup. Keep namespaced file/resource identities and
  // strip the prefix only for the native vehicle animation database lookup.
  if (pose && name.starts_with("nsv_s2_")) {
    lw_pose_recipe = name;
    ctx.r4.u32 += 7;
  } else if (pose) {
    lw_pose_recipe.clear();
  }
  __imp__sub_823C3B00(ctx, base);
}

extern "C" REX_FUNC(sub_82D196A8) {
  const bool pose = lw_pose_query && uint32_t(ctx.lr) == 0x827B9E7C && !lw_pose_recipe.empty();
  __imp__sub_82D196A8(ctx, base);
  if (pose) {
    if (ctx.r3.u32) ++lw_pose_matches; else ++lw_pose_misses;
    if (REXCVAR_GET(skate3_skate2_livingworld_probe))
    REXLOG_INFO("skate2-lw: original vehicle pose recipe='{}' animation={:08X} found={}",
                lw_pose_recipe, ctx.r3.u32, ctx.r3.u32 != 0);
  }
}

// This callback runs on the native presentation resource path. If the DLC
// vault arrived after manager initialization, prepare its metadata here,
// before its first original resource is handed to the presentation cache.
extern "C" REX_FUNC(sub_827B87B0) {
  if (OriginalVehicles()) PrepareOriginalVehiclePoses(ctx, base, lw_pose_manager.load());
  __imp__sub_827B87B0(ctx, base);
}

template <typename Function>
void TraceVehicleModelCache(PPCContext& ctx, uint8_t* base, Function original, const char* kind) {
  const uint64_t key = ctx.r4.u64;
  const bool trace = TrafficPresentationProbe();
  original(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> samples{0};
    const auto n = ++samples;
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: vehicle presentation cache world={} kind={} n={} key={:016X} model={:08X}",
                  OriginalVehicles() ? "NSV" : "retail", kind, n, key, ctx.r3.u32);
  }
}
extern "C" REX_FUNC(sub_827B8928) { TraceVehicleModelCache(ctx, base, __imp__sub_827B8928, "animated"); }
extern "C" REX_FUNC(sub_827B8990) { TraceVehicleModelCache(ctx, base, __imp__sub_827B8990, "static"); }

template <typename Function>
void TraceVehiclePresentation(PPCContext& ctx, uint8_t* base, Function original, const char* kind) {
  const bool trace = TrafficPresentationProbe();
  const uint32_t request = ctx.r4.u32;
  uint64_t key = 0, cache_key = 0;
  if (trace) {
    GuestU64(base, request + 0x30, key); GuestU64(base, request + 0x38, cache_key);
  }
  if (OriginalVehicles()) PrepareOriginalVehiclePoses(ctx, base, ctx.r3.u32);
  original(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> samples{0};
    const auto n = ++samples;
    uint32_t lod = 0, count = 0;
    if (ctx.r3.u32 && GuestU32(base, ctx.r3.u32 + 16, lod) && lod < 3)
      GuestU32(base, ctx.r3.u32 + (lod + 5) * 4, count);
    if (ctx.r3.u32 || LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: vehicle presentation create world={} kind={} n={} key={:016X} cache_key={:016X} entity={:08X} lod={} instances={}",
                  OriginalVehicles() ? "NSV" : "retail", kind, n, key, cache_key, ctx.r3.u32, lod, count);
  }
}
extern "C" REX_FUNC(sub_827BC080) { TraceVehiclePresentation(ctx, base, __imp__sub_827BC080, "animated"); }
extern "C" REX_FUNC(sub_827BC4C8) { TraceVehiclePresentation(ctx, base, __imp__sub_827BC4C8, "static"); }

// Observe the mandatory ID+name external bindings, not just bank state=ready.
template <typename Function>
void TraceTrafficBinding(PPCContext& ctx, uint8_t* base, Function original, const char* kind) {
  uint32_t name_ptr = 0, ids = 0;
  const bool trace = lw_original_audio.load() && REXCVAR_GET(skate3_skate2_livingworld_probe);
  if (trace) { GuestU32(base, ctx.r4.u32, name_ptr); GuestU32(base, ctx.r4.u32 + 4, ids); }
  const std::string name = trace ? GuestString(base, name_ptr) : std::string();
  original(ctx, base);
  if (trace && (name.starts_with("TRAFFIC_") || name.starts_with("Grab_") || name == "Skid_ControlDelayGrab"))
    REXLOG_INFO("skate2-lw: traffic binding kind={} name='{}' ids={:08X} result={}", kind, name, ids, ctx.r3.s32);
}

extern "C" REX_FUNC(sub_828E3250) { TraceTrafficBinding(ctx, base, __imp__sub_828E3250, "cue"); }
extern "C" REX_FUNC(sub_828E3148) { TraceTrafficBinding(ctx, base, __imp__sub_828E3148, "callback"); }

// An allocated engine object does not prove its cue instance was registered.
// Keep this read-only and scoped to the native vehicle constructor's callsites.
template <typename Function>
void TraceTrafficCue(PPCContext& ctx, uint8_t* base, Function original, const char* operation, uint32_t caller) {
  const bool trace = TrafficPresentationProbe() && uint32_t(ctx.lr) == caller;
  const uint32_t output = ctx.r5.u32;
  original(ctx, base);
  if (trace) {
    uint32_t instance = 0;
    GuestU32(base, output, instance);
    REXLOG_INFO("skate2-lw: traffic cue world={} operation={} result={} instance={:08X}",
                OriginalVehicles() ? "NSV" : "retail", operation, ctx.r3.s32, instance);
  }
}
extern "C" REX_FUNC(sub_828E2B48) { TraceTrafficCue(ctx, base, __imp__sub_828E2B48, "register", 0x824D5BD8); }
extern "C" REX_FUNC(sub_828E2AF0) { TraceTrafficCue(ctx, base, __imp__sub_828E2AF0, "retry", 0x824D5C24); }

extern "C" REX_FUNC(sub_828E2D18) {
  const bool trace = TrafficPresentationProbe() && uint32_t(ctx.lr) == 0x824D6BA8;
  const uint32_t instance = ctx.r3.u32, parameters = ctx.r4.u32;
  std::array<uint32_t, 4> values{};
  if (trace) {
    GuestU32(base, parameters, values[0]);       // engine RPM
    GuestU32(base, parameters + 4, values[1]);   // spatial control
    GuestU32(base, parameters + 0x34, values[2]); // car speed
    GuestU32(base, parameters + 0x38, values[3]); // engine selector
  }
  __imp__sub_828E2D18(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> samples[2]{};
    const bool imported = OriginalVehicles();
    const auto n = ++samples[imported ? 1 : 0];
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: traffic cue update world={} n={} instance={:08X} rpm={} spatial={} speed={} selector={}",
                  imported ? "NSV" : "retail", n, instance, values[0], values[1], values[2], values[3]);
  }
}

extern "C" REX_FUNC(sub_824D6110) {
  const uint32_t audio = ctx.r3.u32;
  const bool trace = TrafficPresentationProbe();
  uint32_t before = 0;
  if (trace) GuestU32(base, audio + 0x2C, before);
  __imp__sub_824D6110(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> samples{0};
    const auto n = ++samples;
    uint32_t engine = 0, index = 0, sim = 0, state = 0, enabled = 0;
    uint64_t tuning = 0;
    GuestU32(base, audio + 0x2C, engine); GuestU32(base, audio + 0x30, index);
    GuestU32(base, audio + 0x1C, sim); GuestU32(base, audio + 0x10, state);
    if (state) GuestU32(base, state + 0x34, enabled);
    if (sim) GuestU64(base, sim + 0xA8, tuning);
    if ((engine && !before) || LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: vehicle audio world={} n={} module={:08X} sim={:08X} enabled={} tuning={:016X} engine={:08X} index={}",
                  OriginalVehicles() ? "NSV" : "retail", n, audio, sim, (enabled >> 24) != 0, tuning, engine, index);
  }
}

// A pending, explicitly armed imported actor stream load is consumed once. Do not infer a
// New San Van request from a stale active-world flag during a retail load.
extern "C" REX_FUNC(sub_826B0B28) {
  if (lw_assets_ready && (REXCVAR_GET(skate3_skate2_original_vehicles) || PreparedPedestriansFor(requested_stream.load())) &&
      ctx.r4.u64 == lw_template_group_key.load() && lw_load_group_pending.exchange(false)) {
    const uint64_t group = OriginalActorGroup();
    PPCContext find = ctx;
    find.r3.u64 = 0x5684E15FE43FBD88;  // livingworld_load_groups
    find.r4.u64 = group;
    __imp__sub_82B69B08(find, base);
    const bool found = find.r3.u32 != 0;
    lw_load_group_loaded.store(found);
    if (found) ctx.r4.u64 = group;
    REXLOG_INFO("skate2-lw: original actor load group key={:016X} found={} collection={:08X}",
                group, found, find.r3.u32);
  }
  __imp__sub_826B0B28(ctx, base);
}

// Keep schema parsing and asset IDs intact. At the shared filename formatter,
// move only explicitly namespaced recipes/models and inventoried S2 textures
// into the unique mounted folders. Full-path asset keys see the adapted path.
// Observe the same inherited family attribute used by 826AFCB8. A correct
// vehicle record reads enum 3 through its linked parent chain; a missing
// parent caused the native fallback to request pedestrian recipe paths.
extern "C" REX_FUNC(sub_826AFCB8) {
  if (LivingWorldProbe()) {
    uint32_t collection = 0, name_field = 0, recipe_pointer = 0;
    GuestU32(base, ctx.r4.u32, collection);
    if (GuestU32(base, ctx.r4.u32 + 4, name_field) && name_field)
      GuestU32(base, name_field, recipe_pointer);
    const std::string recipe = GuestString(base, recipe_pointer);
    if (collection && recipe.starts_with("nsv_s2_")) {
      auto call = ctx;
      call.r3.u32 = collection;
      call.r4.u64 = 0xF6897CC7B637850C;
      call.r5.u32 = 0;
      __imp__sub_82B72420(call, base);
      uint32_t family = 0, parent = 0, depth = 0;
      const bool found = call.r3.u32 && GuestU32(base, call.r3.u32, family);
      for (uint32_t node = collection; depth < 32 && GuestU32(base, node + 0x10, parent) && parent; node = parent)
        ++depth;
      REXLOG_INFO("skate2-lw: model recipe='{}' inherited_family={} found={} parent_depth={}",
                  recipe, family, found, depth);
    }
  }
  __imp__sub_826AFCB8(ctx, base);
}

extern "C" REX_FUNC(sub_82AEABB0) {
  const uint32_t output = ctx.r3.u32, capacity = ctx.r4.u32;
  const uint32_t caller = uint32_t(ctx.lr);
  const uint64_t texture = ctx.r7.u64;
  const uint32_t format = ctx.r5.u32;
  const bool actor_path = format == 0x820BF804 || format == 0x820B49CC ||
                          format == 0x820BFE34 || format == 0x820BFDD4;
  __imp__sub_82AEABB0(ctx, base);
  if (!actor_path || !lw_assets_ready || capacity == 0 || capacity > 4096 || ctx.r3.s32 < 0) return;
  std::string path = GuestString(base, output);
  std::string adapted = path;
  for (const char* family : {"vehicle", "livingworld"}) {
    const std::string recipe = "data\\content/recipe/" + std::string(family) + "/nsv_s2_";
    const std::string model = "data\\content\\" + std::string(family) + "\\model\\nsv_s2_";
    const std::string tex = "data\\content\\" + std::string(family) + "\\texture\\";
    if (path.starts_with(recipe)) adapted.insert(std::string("data\\content/recipe/").size(), "nsv_s2_");
    else if (path.starts_with(model)) adapted.insert(std::string("data\\content\\").size(), "nsv_s2_");
    else if (caller == 0x82C26BC8 && path.starts_with(tex) && lw_texture_ids.contains(texture))
      adapted.insert(std::string("data\\content\\").size(), "nsv_s2_");
  }
  if (adapted == path || adapted.size() + 1 > capacity) return;
  std::memcpy(base + output, adapted.c_str(), adapted.size() + 1);
  ctx.r3.u32 = uint32_t(adapted.size());
  static std::atomic<uint64_t> requests{0};
  const auto n = ++requests;
  if (LivingWorldLogSample(n)) REXLOG_INFO("skate2-lw: original asset path n={} '{}'", n, adapted);
}

// EB0013 is the RAIN road AI network. Observe raw counts before its original
// pointer fixup; this neither changes the payload nor registers extra roads.
extern "C" REX_FUNC(sub_82962890) {
  const bool trace = LivingWorldProbe();
  uint32_t intersections = 0, roads = 0, superroads = 0;
  if (trace) {
    GuestU32(base, ctx.r3.u32 + 0x20, intersections);
    GuestU32(base, ctx.r3.u32 + 0x24, roads);
    GuestU32(base, ctx.r3.u32 + 0x28, superroads);
  }
  __imp__sub_82962890(ctx, base);
  if (trace) {
    const auto n = ++lw_road_fixups;
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: RAIN fixup n={} intersections={} roads={} superroads={} result={}",
                  n, intersections, roads, superroads, ctx.r3.u32);
  }
}

// A return from activation is not proof that every link registered successfully.
extern "C" REX_FUNC(sub_826B1B28) {
  const bool trace = LivingWorldProbe();
  const uint64_t asset = ctx.r4.u64;
  __imp__sub_826B1B28(ctx, base);
  if (trace) {
    const auto n = ++lw_road_requests;
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: road activation returned n={} asset={:016X}", n, asset);
  }
}

// Target after the game's scaling; kind 2 = pedestrians, 3 = vehicles.
extern "C" REX_FUNC(sub_826B8A28) {
  const bool trace = LivingWorldProbe();
  const uint32_t kind = ctx.r4.u32, output = ctx.r6.u32, category = ctx.r7.u32;
  __imp__sub_826B8A28(ctx, base);
  if ((OriginalVehicles() && kind == 3) || (OriginalPedestrians() && kind == 2)) {
    uint32_t target = 0;
    if (GuestU32(base, output + 0x18, target))
      StoreGuestU32(base, output + 0x18, lw_load_group_loaded.load() ? target : 0u);
  }
  if (trace && (kind == 2 || kind == 3)) {
    uint32_t target = 0;
    uint64_t key = 0;
    const bool readable = GuestU32(base, output + 0x18, target) && GuestU64(base, category, key);
    const auto n = ++lw_samples[kind - 2];
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: census sample kind={} n={} readable={} target={} category={:016X}",
                  kind, n, readable, target, key);
  }
}

// Weighted entity selection: do not force a target or override any refusal.
extern "C" REX_FUNC(sub_826B8B88) {
  const bool trace = LivingWorldProbe();
  const uint32_t kind = ctx.r5.u32, population = ctx.r6.u32, output = ctx.r7.u32;
  __imp__sub_826B8B88(ctx, base);
  if (trace && (kind == 2 || kind == 3)) {
    const bool accepted = ctx.r3.u32 != 0;
    uint64_t entity = 0;
    if (accepted) GuestU64(base, output, entity);
    const auto n = ++lw_selections[kind - 2];
    if (accepted) ++lw_selected[kind - 2];
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: census selection kind={} n={} population={} accepted={} entity={:016X}",
                  kind, n, population, accepted, entity);
  }
}

// Population manager (vtable 8230C638). Its +0xA8 enables gate the spawners:
// 0x20 pedestrian spawning (826B7530 -> 826B9940), 0x10 pedestrian census
// sampling (826B90F8), 0x08 vehicles (826B7760). Read-only diagnostics.
namespace {
std::atomic<uint32_t> lw_population{0};
uint32_t PopulationFlags(uint8_t* base) {
  uint32_t word = 0;
  const uint32_t population = lw_population.load(std::memory_order_relaxed);
  return population && GuestU32(base, population + 0xA8, word) ? word >> 24 : 0x100;
}
template <typename Function>
void TracePopulationEnable(PPCContext& ctx, uint8_t* base, Function original, const char* kind) {
  const bool trace = LivingWorldProbe();
  const uint32_t before = trace ? PopulationFlags(base) : 0, caller = uint32_t(ctx.lr);
  const bool value = (ctx.r4.u32 & 0xFF) != 0;
  original(ctx, base);
  if (!trace) return;
  const uint32_t after = PopulationFlags(base);
  static std::atomic<uint32_t> calls{0};
  const auto n = ++calls;
  if (before != after || n <= 8)
    REXLOG_INFO("skate2-lw: population enable kind={} value={} flags={:02X}->{:02X} caller={:08X} n={}",
                kind, value, before, after, caller, n);
}
}  // namespace
// 826C1348 is exclusively the retail world-wide pedestrian cooldown:
// active skater state +0x30 -> byte +0x71 refills world+0x2163C to 180s;
// 826BDB50 inverts its result through population vfunc +0x18 (826BDF04).
// Run167 froze both census and factories at FF->DF; vehicle spawning kept
// running. New San Van free roaming does not use this global cooldown.
// Keep the skater state, other population/challenge gates, and retail maps.
extern "C" REX_FUNC(sub_826C1348) {
  const bool nsv = OriginalPedestrians();
  const uint32_t world = ctx.r3.u32;
  __imp__sub_826C1348(ctx, base);
  if (!nsv || !(ctx.r3.u32 & 0xFF)) return;
  uint32_t bits = 0;
  if (!GuestU32(base, world + 0x2163C, bits)) return;
  float seconds = 0;
  std::memcpy(&seconds, &bits, sizeof(seconds));
  if (!(seconds > 0 && seconds <= 180.f)) return;
  StoreGuestU32(base, world + 0x2163C, 0);
  ctx.r3.u64 = 0;
  static std::atomic<uint32_t> cleared{0};
  const auto n = ++cleared;
  if (n == 1 || (LivingWorldProbe() && LivingWorldLogSample(n)))
    REXLOG_INFO("skate2-lw: native pedestrian cooldown cleared seconds={:.3f} skater_state={:08X} trigger={} n={} policy=NewSanVan-free-roam",
                seconds, ctx.r9.u32, ctx.r8.u32 & 0xFF, n);
}

extern "C" REX_FUNC(sub_826B83C8) {
  const bool trace = LivingWorldProbe();
  const uint32_t population = ctx.r3.u32;
  uint32_t ceiling = 0;
  uint64_t entity = 0;
  if (trace) { GuestU32(base, population + 0x94, ceiling); GuestU64(base, ctx.r4.u32, entity); }
  __imp__sub_826B83C8(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> requests{0};
    const auto n = ++requests;
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: vehicle population request n={} ceiling={} entity={:016X} actor={:08X}",
                  n, ceiling, entity, ctx.r3.u32);
  }
}

extern "C" REX_FUNC(sub_826B71F0) {
  lw_population.store(ctx.r3.u32, std::memory_order_relaxed);
  __imp__sub_826B71F0(ctx, base);
}
extern "C" REX_FUNC(sub_8269B130) { TracePopulationEnable(ctx, base, __imp__sub_8269B130, "pedestrians"); }
extern "C" REX_FUNC(sub_8269B148) { TracePopulationEnable(ctx, base, __imp__sub_8269B148, "pedestrian-census"); }
extern "C" REX_FUNC(sub_8269B160) { TracePopulationEnable(ctx, base, __imp__sub_8269B160, "vehicles"); }

// Sk8::Challenge::tLivingWorldManagement: 825612C0 applies its six packed
// enables (bit1 pedestrians, bit2 pedestrian census, bit3 vehicles); 82561198
// reads them from a challenge collection, 82561250 restores all six.
extern "C" REX_FUNC(sub_825612C0) {
  const bool trace = LivingWorldProbe();
  uint32_t packed = 0;
  if (trace) GuestU32(base, ctx.r4.u32, packed);
  const uint32_t caller = uint32_t(ctx.lr);
  __imp__sub_825612C0(ctx, base);
  if (trace)
    REXLOG_INFO("skate2-lw: challenge living-world flags value={:02X} pedestrians={} census={} vehicles={} caller={:08X} population_flags={:02X}",
                packed & 0x3F, (packed >> 1) & 1, (packed >> 2) & 1, (packed >> 3) & 1, caller, PopulationFlags(base));
}
extern "C" REX_FUNC(sub_82561198) {
  if (LivingWorldProbe()) {
    uint32_t instance = 0, collection = 0;
    std::array<uint32_t, 8> words{};
    if (GuestU32(base, ctx.r4.u32 + 0x14, instance) && GuestU32(base, instance, collection))
      for (uint32_t i = 0; i < words.size(); ++i) GuestU32(base, collection + i * 4, words[i]);
    REXLOG_INFO("skate2-lw: challenge living-world source collection={:08X} words={:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X} {:08X}",
                collection, words[0], words[1], words[2], words[3], words[4], words[5], words[6], words[7]);
  }
  __imp__sub_82561198(ctx, base);
}

extern "C" REX_FUNC(sub_82E22F30) {
  const bool trace = LivingWorldProbe();
  __imp__sub_82E22F30(ctx, base);
  if (trace) {
    const auto n = ++lw_factories[0];
    if (ctx.r3.u32) ++lw_created[0];
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: pedestrian factory n={} actor={:08X}", n, ctx.r3.u32);
  }
}

// Read-only factory road/distance stages. The old trace called all pre-lane
// failures road-query, which also hid the native 15m route-end rejection.
extern "C" REX_FUNC(sub_826B3B18) {
  auto* trace = uint32_t(ctx.lr) == 0x82C364AC ? lw_factory_trace : nullptr;
  __imp__sub_826B3B18(ctx, base);
  if (trace) { trace->closest = ctx.r3.u32; trace->stage = ctx.r3.u32 ? "route-distance" : "closest-road"; }
}
extern "C" REX_FUNC(sub_826B2BA0) {
  auto* trace = uint32_t(ctx.lr) == 0x82C364FC ? lw_factory_trace : nullptr;
  const uint32_t closest = trace ? trace->closest : 0;
  __imp__sub_826B2BA0(ctx, base);
  if (!trace) return;
  trace->superroad = ctx.r3.u32;
  if (!ctx.r3.u32) { trace->stage = "superroad"; return; }
  uint32_t road = 0, index = 0, resource = 0, segments = 0;
  if (!GuestU32(base, closest, road) || !GuestU32(base, closest + 4, index) ||
      !GuestU32(base, road + 4, resource) || !GuestU32(base, resource + 124, segments)) return;
  const uint32_t segment = segments + (index / 2) * 224;
  GuestU32(base, segment + 68, trace->lookup_count);
  GuestU32(base, segment + 72, trace->lookup_pointer);
  uint32_t lanes = 0, normal_bits = 0;
  if (GuestU32(base, road + 8, lanes) &&
      GuestU32(base, lanes + (index / 2) * 48 + 20, normal_bits))
    std::memcpy(&trace->road_normal_y, &normal_bits, 4);
  uint32_t end_bits = 0, length_bits = 0, road_end_bits = 0, road_length_bits = 0, total_bits = 0;
  if (!GuestU32(base, segment + 208, end_bits) || !GuestU32(base, segment + 64, length_bits) ||
      !GuestU32(base, resource + 144, road_end_bits) || !GuestU32(base, resource + 140, road_length_bits) ||
      !GuestU32(base, ctx.r3.u32 + 44, total_bits)) return;
  float end = 0, length = 0, road_end = 0, road_length = 0;
  std::memcpy(&end, &end_bits, 4); std::memcpy(&length, &length_bits, 4);
  std::memcpy(&road_end, &road_end_bits, 4); std::memcpy(&road_length, &road_length_bits, 4);
  std::memcpy(&trace->route_length, &total_bits, 4);
  trace->route_distance = end - length + road_end - road_length;
}

// Trace actual calls made by the native factory, preserving every refusal.
extern "C" REX_FUNC(sub_82E14928) {
  auto* trace = uint32_t(ctx.lr) == 0x82C365CC ? lw_factory_trace : nullptr;
  __imp__sub_82E14928(ctx, base);
  if (trace) {
    trace->stage = "lane-gap";
    ++trace->lane_queries;
    trace->available_lanes += (ctx.r3.u32 & 255) != 0;
  }
}

extern "C" REX_FUNC(sub_82E0D038) {
  const uint32_t caller = uint32_t(ctx.lr);
  auto* trace = caller == 0x82C36378 || caller == 0x82C366CC ? lw_factory_trace : nullptr;
  __imp__sub_82E0D038(ctx, base);
  if (trace) {
    trace->stage = "actor-create";
    trace->attrib_cache = ctx.r3.u32;
  }
}

extern "C" REX_FUNC(sub_82C36300) {
  const bool trace = LivingWorldProbe();
  const uint32_t parameters = ctx.r4.u32;
  uint64_t key0 = 0, key1 = 0;
  uint32_t road_manager = 0, road_vtable = 0, closest_call = 0, superroad_call = 0;
  uint32_t actor_manager = 0, actor_vtable = 0, create_call = 0;
  uint8_t explicit_placement = 0;
  if (trace) {
    GuestU64(base, parameters + 8, key0); GuestU64(base, parameters + 0x10, key1);
    uint32_t tail = 0;
    if (GuestU32(base, parameters + 0x64, tail)) explicit_placement = uint8_t(tail >> 16);
    if (GuestU32(base, 0x830854C0, road_manager) && GuestU32(base, road_manager, road_vtable)) {
      GuestU32(base, road_vtable + 0x3C, closest_call); GuestU32(base, road_vtable + 0x20, superroad_call);
    }
    if (GuestU32(base, ctx.r3.u32 + 0x234, actor_manager) && GuestU32(base, actor_manager, actor_vtable))
      GuestU32(base, actor_vtable + 0x2C, create_call);
  }
  VehicleFactoryTrace progress;
  auto* previous = lw_factory_trace;
  lw_factory_trace = trace ? &progress : nullptr;
  __imp__sub_82C36300(ctx, base);
  lw_factory_trace = previous;
  if (trace) {
    const auto n = ++lw_factories[1];
    if (ctx.r3.u32) ++lw_created[1];
    static int last_stream = -1;
    static uint32_t stream_successes = 0;
    const int stream = loaded_stream.load();
    if (stream != last_stream) { last_stream = stream; stream_successes = 0; }
    const bool first_success = ctx.r3.u32 && ++stream_successes <= 4;
    uint32_t simulation = 0;
    float x = 0.f, y = 0.f, z = 0.f, up_y = 0.f;
    if (ctx.r3.u32 && GuestU32(base, ctx.r3.u32 + 0xA4, simulation)) {
      uint32_t bits = 0;
      if (GuestU32(base, simulation + 0x80, bits)) std::memcpy(&x, &bits, 4);
      if (GuestU32(base, simulation + 0x84, bits)) std::memcpy(&y, &bits, 4);
      if (GuestU32(base, simulation + 0x88, bits)) std::memcpy(&z, &bits, 4);
      if (GuestU32(base, simulation + 0x64, bits)) std::memcpy(&up_y, &bits, 4);
    }
    if (LivingWorldLogSample(n) || first_success)
      REXLOG_INFO("skate2-lw: vehicle factory n={} actor={:08X} stage={} lanes={}/{} cache={:08X} explicit={} keys={:016X}/{:016X} last_virtual={:08X} closest={:08X} superroad={:08X} create={:08X} stream={} closest_result={:08X} superroad_result={:08X} route_distance={:.2f}/{:.2f} road_normal_y={:.3f} lookup_samples={} lookup={:08X} placement=({:.2f},{:.2f},{:.2f}) up_y={:.3f}",
                  n, ctx.r3.u32, progress.stage, progress.available_lanes, progress.lane_queries,
                  progress.attrib_cache, explicit_placement, key0, key1, ctx.ctr.u32, closest_call, superroad_call, create_call, skate2_streams[stream],
                  progress.closest, progress.superroad, progress.route_distance, progress.route_length,
                  progress.road_normal_y, progress.lookup_count, progress.lookup_pointer, x, y, z, up_y);
  }
}

// Native layer attachment, after version validation. Its result is a bool;
// the outer 82937450 loader instead returns bytes consumed, not acceptance.
extern "C" REX_FUNC(sub_829371F0) {
  const bool trace = StreamNavigationReady(loaded_stream.load()) && LivingWorldProbe();
  uint32_t index = 0, polygon_bytes = 0;
  if (trace) {
    GuestU32(base, ctx.r4.u32 + 4, index);
    GuestU32(base, ctx.r4.u32 + 8, polygon_bytes);
  }
  __imp__sub_829371F0(ctx, base);
  if (trace) {
    static std::atomic<uint64_t> attempts{0}, accepted{0};
    const auto n = ++attempts;
    if ((ctx.r3.u32 & 255) != 0) ++accepted;
    if (!(ctx.r3.u32 & 255) || LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: navigation attach n={} accepted={} result={} layer={} polygon_bytes={}",
                  n, accepted.load(), ctx.r3.u32 & 255, index, polygon_bytes);
  }
}

// Shared with other AI, including skaters. Preserve every result. Return 3
// identifies an invalid NavPower location; other callers can reject for more reasons.
extern "C" REX_FUNC(sub_82C460E0) {
  const bool trace = LivingWorldProbe();
  __imp__sub_82C460E0(ctx, base);
  if (trace) {
    const auto n = ++lw_nav_queries;
    if (ctx.r3.u32 == 3) ++lw_nav_invalid;
    if (LivingWorldLogSample(n))
      REXLOG_INFO("skate2-lw: NavPower query n={} result={}", n, ctx.r3.u32);
  }
}

void LogLivingWorldSummary(uint8_t* base, uint32_t broadcaster) {
  if (!LivingWorldProbe() || !skate3::world_loading::IsSkate2Gameplay()) return;
  static auto next = std::chrono::steady_clock::time_point{};
  const auto now = std::chrono::steady_clock::now();
  if (now < next) return;
  next = now + std::chrono::seconds(5);
  uint64_t peds = 0, vehicles = 0;
  const bool readable = GuestU64(base, broadcaster + 0x820 + 16 * 2, peds) &&
                        GuestU64(base, broadcaster + 0x820 + 16 * 3, vehicles);
  REXLOG_INFO("skate2-lw: active stream={} pedestrians={} vehicles={} navigation_ready={}",
              skate2_streams[loaded_stream.load()], OriginalPedestrians(), OriginalVehicles(),
              StreamNavigationReady(loaded_stream.load()));
  if (OriginalVehicles())
    REXLOG_INFO("skate2-lw: original vehicle traffic; load_group_loaded={} density=original", lw_load_group_loaded.load());
  else
    REXLOG_INFO("skate2-lw: diagnostic only; census translation disabled");
  REXLOG_INFO("skate2-lw: original pedestrians enabled={} load_group_loaded={} (speech reports separately)",
              OriginalPedestrians(), lw_load_group_loaded.load());
  REXLOG_INFO("skate2-lw: census under player readable={} npc={:016X} vehicle={:016X}",
              readable, peds, vehicles);
  // Cumulative process totals across BAM visits, not resident actor counts.
  REXLOG_INFO("skate2-lw: totals RAIN={} road_activation={} samples={}/{} selections={}/{} "
              "selected={}/{} factories={}/{} created={}/{} nav_queries={} nav_invalid={}",
              lw_road_fixups.load(), lw_road_requests.load(), lw_samples[0].load(), lw_samples[1].load(),
              lw_selections[0].load(), lw_selections[1].load(), lw_selected[0].load(), lw_selected[1].load(),
              lw_factories[0].load(), lw_factories[1].load(), lw_created[0].load(), lw_created[1].load(),
              lw_nav_queries.load(), lw_nav_invalid.load());
  uint32_t ceiling = 0, world = 0, pool = 0, resident = 0, density_bits = 0;
  const uint32_t population = lw_population.load(std::memory_order_relaxed);
  GuestU32(base, population + 0x94, ceiling); GuestU32(base, population + 0x8C, density_bits);
  if (GuestU32(base, 0x830CFD94, world) && GuestU32(base, world + 0xD4, world) &&
      GuestU32(base, world + 0xAA0, pool)) GuestU32(base, pool + 0x10, resident);
  float density = 0; std::memcpy(&density, &density_bits, 4);
  uint32_t state_manager = 0, state = 0, cost_bits = 0;
  float native_cost = -1;
  if (GuestU32(base, 0x83083BCC, state_manager) && GuestU32(base, state_manager + 8, state) &&
      state && GuestU32(base, state + 0x7C, cost_bits)) std::memcpy(&native_cost, &cost_bits, 4);
  REXLOG_INFO("skate2-lw: vehicle population ceiling={} resident={} density_scale={:.3f} native_cost={:.3f}",
              ceiling, resident, density, native_cost);
  // Memory category 14 "Audio" (retail AudioHeap, 30 MiB budget) holds speech
  // banks and archives and every audio-core voice. A general heap (vtable
  // 823177E4) keeps capacity/used/peak at +12/+16/+20; others log vtable only.
  const uint32_t pools = StreamWord(base, StreamWord(base, StreamWord(base, 0x830CFEEC, 0), 4), 0xC1B4);
  std::array<uint32_t, 8> audio{};
  for (uint32_t route = 0; route < 2 && pools >= 0x10000 && pools <= 0xfffd0000; ++route) {
    const uint32_t allocator = StreamWord(base, StreamWord(base, pools + 14 * 108, route * 4), 0);
    const uint32_t vtable = StreamWord(base, allocator, 0);
    audio[route * 4] = vtable;
    if (vtable == 0x823177E4)
      for (uint32_t i = 1; i < 4; ++i) audio[route * 4 + i] = StreamWord(base, allocator, 8 + 4 * i);
  }
  REXLOG_INFO("skate2-lw: population_flags={:02X} audio_pool primary={:08X} cap={} used={} peak={} "
              "fallback={:08X} cap={} used={} peak={}",
              PopulationFlags(base), audio[0], audio[1], audio[2], audio[3], audio[4], audio[5], audio[6], audio[7]);
}

// Missing DLC ambience uses the selected existing city record through the
// native WorldPainter result. No extra player, loop, archive or guest write.
extern "C" REX_FUNC(sub_82C0EAC0) {
  const uint32_t layer = ctx.r4.u32;
  __imp__sub_82C0EAC0(ctx, base);
  if (ctx.r3.u64 != 0 || layer != 7 || !skate2_loading.load()) return;
  const int index = loaded_stream.load();
  if (index < 0 || index >= int(skate2_streams.size())) return;
  const uint64_t bed = skate3::native_policy::DlcAmbienceBed(
      skate2_streams[index], dlc_ambience_key, dlc_downtown_ambience_key);
  // These verified records were loaded once from the prepared metadata.
  ctx.r3.u64 = skate3::native_policy::AmbienceFallback(ctx.r3.u64, layer, true,
      bed != 0 && REXCVAR_GET(skate3_skate2_world_audio) &&
      REXCVAR_GET(skate3_skate2_painted_audio), skate2_streams[index], bed);
}

// WorldPainter broadcaster (per frame): each enabled layer's value under the
// player, cached at this+0x820 + 16 * layer (Skate 3 eLayerID: 7 ambience,
// 8 sound zones, 10 reverb). Logs New San Van's audio paint as it changes.
extern "C" REX_FUNC(sub_82C0EA00) {
  const uint32_t self = ctx.r3.u32;
  __imp__sub_82C0EA00(ctx, base);
  LogLivingWorldSummary(base, self);
  if (paint_layers.empty() || !skate3::world_loading::IsSkate2Gameplay()) return;
  static constexpr std::array<std::pair<uint32_t, const char*>, 3> kLayers = {
      {{7, "ambience"}, {8, "sound zone"}, {10, "reverb"}}};
  static std::array<uint64_t, 3> last = {~0ull, ~0ull, ~0ull};
  static uint32_t logged = 0;
  for (size_t i = 0; i < kLayers.size(); ++i) {
    uint64_t value = 0;
    if (!GuestU64(base, self + 0x820 + 16 * kLayers[i].first, value) || value == last[i]) continue;
    last[i] = value;
    if (REXCVAR_GET(skate3_world_load_probe) && logged++ < 500) {
      const auto name = paint_names.find(value);
      char hex[20];
      std::snprintf(hex, sizeof(hex), "%016llX", static_cast<unsigned long long>(value));
      REXLOG_INFO("skate2-audio: painted {} -> {}", kLayers[i].second,
                  value == 0 ? std::string("none")
                  : name != paint_names.end() ? name->second
                                              : std::string(hex));
    }
  }
}

// Ambience bed player: starts bed r4 (the number in an aud_wp_ambiences
// record; Skate 3's own archive or a loose <n>_dlc_ambience bed).
extern "C" REX_FUNC(sub_824D3590) {
  if (!paint_layers.empty() && skate3::world_loading::IsSkate2Gameplay()) {
    static uint32_t logged = 0;
    if (REXCVAR_GET(skate3_world_load_probe) && logged++ < 200) REXLOG_INFO("skate2-audio: ambience bed {}", ctx.r4.s32);
  }
  __imp__sub_824D3590(ctx, base);
}

// Loose bed load (a bed number Skate 3's archive lacks): appends
// data\audio\<n>_dlc_ambience.snr to the bed table (16 spare slots).
extern "C" REX_FUNC(sub_824D3990) {
  const int32_t bed = ctx.r4.s32;
  __imp__sub_824D3990(ctx, base);
  static uint32_t logged = 0;
  if (logged++ < 64) REXLOG_INFO("skate2-audio: ambience bed {} requested (loose bed path)", bed);
}

// Record allocation inputs at the boundary implicated by the 007 crash dump.
// Do not suppress the failure or substitute memory: the request and lookup
// class distinguish an oversized request from earlier guest-memory corruption.
extern "C" REX_FUNC(sub_82990768) {
  if (skate2_loading.load()) {
    const uint32_t size = ctx.r4.u32;
    const uint32_t rounded = (size + 15u) & ~15u;
    uint32_t size_class = 0xffffffff;
    if (rounded && rounded <= 2048) {
      GuestU32(base, 0x8307058c + ((rounded - 1u) / 16u) * 4u, size_class);
    }
    static std::atomic<uint32_t> count{0};
    if ((REXCVAR_GET(skate3_world_load_probe) && count.fetch_add(1) < 64) || !rounded ||
        (rounded <= 2048 && size_class >= 20)) {
      REXLOG_INFO("skate2: small-alloc object={:08X} size={:08X} rounded={} class={:08X} caller={:08X}",
                  ctx.r3.u32, size, rounded, size_class, uint32_t(ctx.lr));
    }
  }
  __imp__sub_82990768(ctx, base);
}

namespace {
std::mutex allocation_evidence_mutex;

// Read-only snapshots: preserve original allocator routing, including fallback.
// The pool registry and 108-byte entries are addressed by 828AAE90/8298E020.
void WriteGuestWords(std::ostream& out, uint8_t* base, const char* label,
                     uint32_t address, uint32_t words) {
  out << '\n' << label << " address=" << std::hex << address;
  for (uint32_t i = 0; i < words; ++i) {
    uint32_t value = 0;
    if (uint64_t(address) + i * 4 > 0xfffffffb ||
        !GuestU32(base, address + i * 4, value)) {
      out << " unreadable@" << i * 4; break;
    }
    if (i % 8 == 0) out << '\n' << address + i * 4 << ':';
    out << ' ' << value;
  }
  out << '\n';
}

void CapturePoolEvidence(uint8_t* base, uint32_t pool, uint32_t size,
                         uint32_t caller, bool failed) {
  if (pool >= 256) return;
  const auto registry = StreamWord(base, 0x830CFEEC, 0);
  const auto manager = StreamWord(base, registry, 4);
  const auto entries = StreamWord(base, manager, 49588);
  if (entries < 0x10000 || entries > 0xfffd0000) return;
  const auto entry = entries + pool * 108;
  std::lock_guard lock(allocation_evidence_mutex);
  std::ofstream out("skate2_pool_evidence.txt", std::ios::app);
  out << std::hex << "pool=" << pool << " size=" << size
      << " caller=" << caller << " failed=" << failed << '\n';
  WriteGuestWords(out, base, "pool-entry", entry, 27);
  for (uint32_t offset : {0u, 4u}) {
    const auto route = StreamWord(base, entry, offset);
    const auto allocator = StreamWord(base, route, 0);
    const auto vtable = StreamWord(base, allocator, 0);
    out << "route_offset=" << offset << " allocator=" << allocator
        << " vtable=" << vtable << " allocate=" << StreamWord(base, vtable, 8) << '\n';
    WriteGuestWords(out, base, "route", route, 12);
    WriteGuestWords(out, base, "allocator", allocator, 1024);
  }
  out << std::endl;
  REXLOG_INFO("world-pool-evidence: pool={} failed={} entry={:08X} file=skate2_pool_evidence.txt",
              pool, failed, entry);
}
}  // namespace

extern "C" REX_FUNC(sub_828AAE90) {
  const auto size = ctx.r3.u32, pool = ctx.r4.u32, caller = uint32_t(ctx.lr);
  const bool probe = skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe);
  if (probe && pool == 11) {
    static std::atomic<bool> captured{false};
    if (!captured.exchange(true)) CapturePoolEvidence(base, pool, size, caller, false);
  }
  if (probe) {
    static std::atomic<uint32_t> count{0};
    if (count.fetch_add(1) < 128) {
      REXLOG_INFO("skate2: alloc size={:08X} pool={} debug_name={:08X} flags={} caller={:08X}",
                  ctx.r3.u32, ctx.r4.u32, ctx.r5.u32, ctx.r6.u32, uint32_t(ctx.lr));
    }
  }
  __imp__sub_828AAE90(ctx, base);
  if (skate2_loading.load() && size && !ctx.r3.u32) {
    static std::atomic<uint32_t> failures{0};
    const auto ordinal = failures.fetch_add(1) + 1;
    if (ordinal <= 8 || (ordinal & (ordinal - 1)) == 0)
      REXLOG_ERROR("world-allocation-failed: size={} pool={} caller={:08X} ordinal={}", size, pool, caller, ordinal);
    static std::array<std::atomic<uint32_t>, 256> pool_failures{};
    if (probe && pool < pool_failures.size() && pool_failures[pool].fetch_add(1) < 2)
      CapturePoolEvidence(base, pool, size, caller, true);
  }
}

// 8298DBC0 copies the static allocator descriptions into the manager, before
// 8298D938 creates backing heaps. Modify only the verified StreamerHeap and
// AudioHeap rows (five words each, 20 bytes apart by allocator ID).
extern "C" REX_FUNC(sub_8298DBC0) {
  const auto expand = [&](uint32_t config, auto rule, bool enabled, const char* message) {
    std::array<uint32_t, 5> row{};
    for (uint32_t i = 0; i < row.size(); ++i) row[i] = StreamWord(base, config, i * 4);
    if (!rule(enabled, row)) return;
    const auto encoded = __builtin_bswap32(row[4]);
    std::memcpy(base + config + 16, &encoded, sizeof(encoded));
    REXLOG_INFO("{}", message);
  };
  const bool skate2 = REXCVAR_GET(skate3_skate2_enabled);
  expand(0x82FDF560, skate3::world_loading::ExpandStreamerHeapConfig,
         skate2 && REXCVAR_GET(skate3_skate2_stream_heap_test),
         "world-stream-heap-config: original_mib=39 requested_mib=64 startup=true");
  expand(0x82FDF5EC, skate3::world_loading::ExpandAudioHeapConfig,
         skate2 && REXCVAR_GET(skate3_skate2_audio_heap),
         "audio-heap-config: original_mib=30 requested_mib=34 startup=true");
  __imp__sub_8298DBC0(ctx, base);
}

// Catch direct virtual calls as well as 828AAE90. The BigFile decoder's
// allocator interface bypassed the narrower probe in log049.
extern "C" REX_FUNC(sub_82990398) {
  const auto allocator = ctx.r3.u32, size = ctx.r4.u32;
  const auto alignment = ctx.r5.u32, flags = ctx.r7.u32, caller = uint32_t(ctx.lr);
  __imp__sub_82990398(ctx, base);
  const bool loading_probe = skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe);
  // New San Van gameplay too: run141 ended in an audio-voice allocation failure.
  const bool gameplay_probe = LivingWorldProbe() && skate3::world_loading::IsSkate2Gameplay();
  if ((loading_probe || gameplay_probe) && size && !ctx.r3.u32) {
    static std::atomic<uint32_t> failures{0};
    const auto ordinal = failures.fetch_add(1) + 1;
    if (ordinal <= 16 || (ordinal & (ordinal - 1)) == 0) {
      REXLOG_ERROR("world-general-allocation-failed: allocator={:08X} size={} alignment={} flags={:08X} caller={:08X} capacity={} used={} peak={} ordinal={}",
                  allocator, size, alignment, flags, caller,
                  StreamWord(base, allocator, 12), StreamWord(base, allocator, 16),
                  StreamWord(base, allocator, 20), ordinal);
    }
    if (loading_probe && ordinal <= 2) CapturePoolEvidence(base, 16, size, caller, true);
  }
}

// rw::audio::core patch creation (System+0x24 allocator). Native callers such
// as the skater splice voice 82488DD0 dereference the result unchecked; run141
// crashed calling through a null patch. Record the failing request.
extern "C" REX_FUNC(sub_82B48C48) {
  const uint32_t type = ctx.r4.u32, modules = ctx.r5.u32, caller = uint32_t(ctx.lr);
  __imp__sub_82B48C48(ctx, base);
  if (!ctx.r3.u32 && skate3::world_loading::IsSkate2Gameplay()) {
    static std::atomic<uint32_t> failures{0};
    const auto ordinal = failures.fetch_add(1) + 1;
    if (ordinal <= 16 || (ordinal & (ordinal - 1)) == 0)
      REXLOG_ERROR("skate2-audio: voice patch allocation failed type={} modules={} caller={:08X} ordinal={}",
                   type, modules, caller, ordinal);
  }
}

// Log048 faults inside the second relocation table of this routine. Capture
// the input BEFORE original in-place fixups, including during the load screen.
extern "C" REX_FUNC(sub_82B1DF50) {
  if (!skate2_loading.load() || !REXCVAR_GET(skate3_world_load_probe)) {
    __imp__sub_82B1DF50(ctx, base); return;
  }
  const auto object = ctx.r3.u32, table = ctx.r4.u32;
  const auto caller = uint32_t(ctx.lr);
  {
    static std::mutex fixup_evidence_mutex;
    std::lock_guard lock(fixup_evidence_mutex);
    std::ofstream out("skate2_fixup_evidence.txt", std::ios::app);
    out << std::hex << "begin object=" << object << " table=" << table << " caller=" << caller << '\n';
    WriteGuestWords(out, base, "header", object, 32);
    WriteGuestWords(out, base, "table-1", table, 64);
    // The original computes delta = table - object[48], then adds the
    // table offsets at52/56. Do not interpret these fields as absolute ptrs.
    const auto delta = table - StreamWord(base, object, 48);
    WriteGuestWords(out, base, "table-2", delta + StreamWord(base, object, 52), 64);
    WriteGuestWords(out, base, "table-3", delta + StreamWord(base, object, 56), 64);
    out << std::endl;
  }
  REXLOG_INFO("world-fixup: phase=begin object={:08X} table={:08X} caller={:08X}", object, table, caller);
  __imp__sub_82B1DF50(ctx, base);
  REXLOG_INFO("world-fixup: phase=end object={:08X}", object);
}

extern "C" REX_FUNC(sub_8298FA10) {
  if (skate2_loading.load() && REXCVAR_GET(skate3_world_load_probe)) {
    const auto path = GuestString(base, ctx.r3.u32 + 12);
    if (path.find("BAM") != std::string::npos) {
      uint32_t state = 0, size = 0, buffer = 0;
      GuestU32(base, ctx.r3.u32 + 268, state);
      GuestU32(base, ctx.r3.u32 + 292, size);
      GuestU32(base, ctx.r3.u32 + 312, buffer);
      std::array<uint32_t, 6> words{};
      for (uint32_t i = 0; i < words.size(); ++i) GuestU32(base, buffer + i * 4, words[i]);
      REXLOG_INFO("skate2: read-complete path='{}' state={} size={} buffer={:08X} header={:08X} {:08X} {:08X} {:08X} {:08X} {:08X}",
                  path, state, size, buffer, words[0], words[1], words[2], words[3], words[4], words[5]);
    }
  }
  __imp__sub_8298FA10(ctx, base);
}

extern "C" REX_FUNC(sub_82476AC8) {
  if (REXCVAR_GET(skate3_world_load_probe)) {
    static std::mutex mutex;
    static std::unordered_map<uint32_t, uint32_t> states;
    uint32_t state;
    if (GuestU32(base, ctx.r3.u32, state)) {
      std::lock_guard lock(mutex);
      auto [it, inserted] = states.try_emplace(ctx.r3.u32, state);
      if (inserted || it->second != state) {
        REXLOG_INFO("world-load: loader={:08X} state={} caller={:08X}",
                    ctx.r3.u32, state, uint32_t(ctx.lr));
        it->second = state;
      }
    }
  }
  __imp__sub_82476AC8(ctx, base);
}

// This guest entry is called at 8247715C with r4 = the constructed .xsm path.
// Trace at the guest boundary: filesystem failures alone omit archive hits.
extern "C" REX_FUNC(sub_82A87508) {
  skate3::perf_recorder::Count(skate3::perf_recorder::Activity::kFileRequest);
  if (REXCVAR_GET(skate3_world_load_probe)) {
    const auto path = GuestString(base, ctx.r4.u32);
    if (path.find("world") != std::string::npos || path.find("BAM") != std::string::npos) {
      static std::atomic<uint32_t> count{0};
      if (count.fetch_add(1, std::memory_order_relaxed) < 4096) {
        REXLOG_INFO("world-load: request path='{}' caller={:08X} object={:08X}",
                    path, uint32_t(ctx.lr), ctx.r3.u32);
      }
    }
  }
  __imp__sub_82A87508(ctx, base);
}

bool skate3::world_loading::OriginalPedestriansActive() { return OriginalPedestrians(); }

// New San Vanelona sky choice (skate3_skate2_sky=false). Skate 2's sky dome is
// Skate 3's DIST_skybox dome (Attrib world/default, University's sky) scaled
// 1.25 with the same topology and per-vertex UVs (log149), so the native
// renderer draws that one Skate 2 dome with Skate 3's two sky textures. No
// second sky model enters the world: build148's extra world-model instance
// left a freed draw context behind on New San Van stream changes. The
// textures are the dome's own diffuse and specular channels, read from the
// game's miscload.big at startup: the sky_gradient_university panorama (Skate
// 3's clouds are painted into it) and the sun gradient. Each pixel buffer is
// copied unchanged, still in its tiled GPU layout, into the 0xA0000000
// physical heap behind a copy of its 52-byte D3D texture header whose fetch
// base address points at it. No guest code references either.
// Night (skate3_skate2_night): the Sanitarium download's DIST_skyboxnightdlc
// is the same dome mesh at Skate 2's own scale with the same UVs (log150), so
// the dome draws its night panorama (sky_sanitariumdlc_night, 2048x512, stars
// over clouds) and moon gradient the same way, read from the installed
// Sanitarium package. The night light is the native renderer's (NightSky()).
namespace {
constexpr uint64_t kSkate3SkyPanorama = 0x3931CD069FD77291ull;  // diffuse
constexpr uint64_t kSkate3SkySun = 0x154156F99C2CF59Dull;       // specular
constexpr uint64_t kNightSkyPanorama = 0x247024E43336E7E7ull;   // diffuse
constexpr uint64_t kNightSkyMoon = 0x08004ED0309060ECull;       // specular
std::atomic<uint32_t> dome_sky_diffuse{0}, dome_sky_specular{0};
std::atomic<bool> night_sky{false};

uint32_t BigEndian32(const uint8_t* p) {
  return uint32_t(p[0]) << 24 | uint32_t(p[1]) << 16 | uint32_t(p[2]) << 8 | p[3];
}

// EA RefPack, as tools/inspect_sfil.py unpack.
bool RefPackExpand(const std::vector<uint8_t>& in, std::vector<uint8_t>& out) {
  if (in.size() < 5 || in[0] != 0x10 || in[1] != 0xFB) return false;
  const size_t size = size_t(in[2]) << 16 | size_t(in[3]) << 8 | in[4];
  out.clear();
  out.reserve(size);
  size_t pos = 5;
  while (pos < in.size()) {
    const uint32_t c = in[pos++];
    size_t literal = 0, length = 0, offset = 0;
    if (c >= 252) {
      literal = c & 3;
    } else if (c >= 224) {
      literal = ((c & 31) << 2) + 4;
    } else if (c >= 192) {
      if (pos + 3 > in.size()) return false;
      const uint32_t a = in[pos], b = in[pos + 1], e = in[pos + 2];
      pos += 3;
      literal = c & 3;
      length = ((c & 12) << 6) + e + 5;
      offset = ((c & 16) << 12) + (a << 8) + b + 1;
    } else if (c >= 128) {
      if (pos + 2 > in.size()) return false;
      const uint32_t a = in[pos], b = in[pos + 1];
      pos += 2;
      literal = a >> 6;
      length = (c & 63) + 4;
      offset = ((a & 63) << 8) + b + 1;
    } else {
      if (pos + 1 > in.size()) return false;
      const uint32_t a = in[pos++];
      literal = c & 3;
      length = ((c & 28) >> 2) + 3;
      offset = ((c & 96) << 3) + a + 1;
    }
    if (pos + literal > in.size() || out.size() + literal + length > size) return false;
    out.insert(out.end(), in.begin() + pos, in.begin() + pos + literal);
    pos += literal;
    if (length && offset > out.size()) return false;
    for (size_t i = 0; i < length; ++i) out.push_back(out[out.size() - offset]);
    if (c >= 252) break;
  }
  return out.size() == size;
}

// One entry of an EB3 archive (tools/world_archive.py eb_entries): 16-byte
// index records {block, stored size when RefPack-compressed, size, djb2 path
// hash}; the entry's name slot (directory index, leaf) confirms the match.
// Download packages use family 0x00500600: the same index and name slots,
// plus a directory tree after the names that this lookup does not need.
bool ReadEb3Entry(const std::filesystem::path& archive, std::string_view name,
                  std::vector<uint8_t>& out) {
  std::ifstream file(archive, std::ios::binary);
  uint8_t header[48];
  if (!file.read(reinterpret_cast<char*>(header), sizeof(header)) ||
      std::memcmp(header, "EB\0\3", 4) != 0) {
    return false;
  }
  const uint32_t count = BigEndian32(header + 4), family = BigEndian32(header + 8);
  const uint32_t names_at = BigEndian32(header + 12), width = BigEndian32(header + 20) >> 24;
  if ((family != 0x00100400 && family != 0x00100600 && family != 0x00500600) || width < 4 ||
      count > 0x100000) {
    return false;
  }
  uint32_t hash = 5381;
  for (const char c : name) hash = hash * 33 + uint8_t(c);
  const std::string_view leaf = name.substr(name.rfind('/') + 1);
  std::vector<uint8_t> index(size_t(count) * 16);
  if (!file.read(reinterpret_cast<char*>(index.data()), std::streamsize(index.size()))) {
    return false;
  }
  for (uint32_t i = 0; i < count; ++i) {
    const uint8_t* entry = index.data() + size_t(i) * 16;
    if (BigEndian32(entry + 12) != hash) continue;
    std::vector<char> slot(width);
    file.seekg(std::streamoff(names_at) + std::streamoff(i) * width);
    if (!file.read(slot.data(), width) ||
        std::string_view(slot.data() + 2, strnlen(slot.data() + 2, width - 2)) != leaf) {
      return false;
    }
    const uint32_t packed = BigEndian32(entry + 4), size = BigEndian32(entry + 8);
    std::vector<uint8_t> raw(packed ? packed : size);
    file.seekg(std::streamoff(BigEndian32(entry)) << ((family >> 8) & 0xFF));
    if (!file.read(reinterpret_cast<char*>(raw.data()), std::streamsize(raw.size()))) {
      return false;
    }
    if (!packed) {
      out = std::move(raw);
      return true;
    }
    return RefPackExpand(raw, out) && out.size() == size;
  }
  return false;
}

// A texture of an RW4 arena by its 64-bit export ID: the export record
// (9B0F1678, ID, AC462E4A, object index) names its 52-byte D3D texture header
// (kind 000200E8), whose fetch base field holds the dictionary index of its
// pixel buffer (kind 00010031) in the resource block after the dictionary.
struct ArenaTexture {
  std::array<uint8_t, 52> header{};
  const uint8_t* pixels = nullptr;
  uint32_t size = 0;
};

bool FindArenaTexture(const std::vector<uint8_t>& arena, uint64_t id, ArenaTexture& out) {
  if (arena.size() < 64 || std::memcmp(arena.data(), "\x89RW4xb2\0", 8) != 0) return false;
  const uint32_t count = BigEndian32(&arena[32]), dictionary = BigEndian32(&arena[48]);
  if (count == 0 || dictionary > arena.size() || size_t(count) * 24 > arena.size() - dictionary) {
    return false;
  }
  const size_t resources = size_t(dictionary) + size_t(count) * 24;
  struct Object {
    uint32_t pointer, size, kind;
  };
  const auto object = [&](uint32_t i) {
    const uint8_t* e = &arena[dictionary + size_t(i) * 24];
    return Object{BigEndian32(e), BigEndian32(e + 8), BigEndian32(e + 20)};
  };
  uint8_t pattern[16] = {0x9B, 0x0F, 0x16, 0x78, 0, 0, 0, 0, 0, 0, 0, 0, 0xAC, 0x46, 0x2E, 0x4A};
  for (int i = 0; i < 8; ++i) pattern[4 + i] = uint8_t(id >> (56 - 8 * i));
  for (size_t at = 0; at + 24 <= arena.size(); at += 4) {
    if (std::memcmp(&arena[at], pattern, sizeof(pattern)) != 0) continue;
    const uint32_t header_index = BigEndian32(&arena[at + 16]);
    if (header_index >= count) return false;
    const Object header = object(header_index);
    if (header.kind != 0x000200E8 || header.size != 52 || header.pointer > arena.size() - 52) {
      return false;
    }
    std::memcpy(out.header.data(), &arena[header.pointer], 52);
    if ((BigEndian32(&out.header[28]) & 3) != 2) return false;  // texture fetch constant
    const uint32_t pixels_index = BigEndian32(&out.header[32]) >> 12;
    if (pixels_index >= count) return false;
    const Object pixels = object(pixels_index);
    if (pixels.kind != 0x00010031 || pixels.size == 0 ||
        pixels.pointer > arena.size() - resources ||
        pixels.size > arena.size() - resources - pixels.pointer) {
      return false;
    }
    out.pixels = &arena[resources + pixels.pointer];
    out.size = pixels.size;
    return true;
  }
  return false;
}

// Pixels go to the 0xA0000000 physical heap (no host offset, so the
// renderer's 0xA0000000|physical reads see them); the header, with its fetch
// base pointed at them, to the system heap. Returns the header address.
uint32_t InstallGuestTexture(const ArenaTexture& texture) {
  auto* memory = rex::system::kernel_memory();
  auto* heap = memory ? memory->LookupHeapByType(true, 64 * 1024) : nullptr;
  uint32_t pixels = 0;
  if (!heap || !heap->AllocSystemHeap(
          texture.size, 64 * 1024,
          rex::memory::kMemoryAllocationReserve | rex::memory::kMemoryAllocationCommit,
          rex::memory::kMemoryProtectRead | rex::memory::kMemoryProtectWrite, false, &pixels)) {
    return 0;
  }
  const uint32_t physical = memory->GetPhysicalAddress(pixels);
  uint8_t* host = memory->TranslateVirtual(pixels);
  if (physical == UINT32_MAX || (physical & 0xFFF) != 0 || (0xA0000000u | physical) != pixels ||
      host != memory->virtual_membase() + pixels) {
    REXLOG_ERROR("skate2-sky: texture memory {:08X} is not on the renderer's physical mirror", pixels);
    return 0;
  }
  std::memcpy(host, texture.pixels, texture.size);
  const uint32_t header = memory->SystemHeapAlloc(52, 32);
  if (!header) return 0;
  std::array<uint8_t, 52> bytes = texture.header;
  const uint32_t fetch1 = (BigEndian32(&bytes[32]) & 0xFFFu) | physical;
  for (int i = 0; i < 4; ++i) bytes[32 + i] = uint8_t(fetch1 >> (24 - 8 * i));
  std::memcpy(memory->TranslateVirtual(header), bytes.data(), bytes.size());
  return header;
}

// The entry from whichever installed download package carries it
// (<content root>/0000000000000000/454108E6/00000002/<package>/*.big).
bool ReadContentEntry(const std::filesystem::path& content_root, std::string_view name,
                      std::vector<uint8_t>& out, std::filesystem::path& found) {
  std::error_code ec;
  std::vector<std::filesystem::path> archives;
  const auto packages = content_root / "0000000000000000" / "454108E6" / "00000002";
  for (const auto& package : std::filesystem::directory_iterator(packages, ec)) {
    if (!package.is_directory(ec)) continue;
    for (const auto& file : std::filesystem::directory_iterator(package.path(), ec)) {
      if (file.is_regular_file(ec) && file.path().extension() == ".big") {
        archives.push_back(file.path());
      }
    }
  }
  std::sort(archives.begin(), archives.end());
  for (const auto& archive : archives) {
    if (ReadEb3Entry(archive, name, out)) {
      found = archive;
      return true;
    }
  }
  return false;
}
}  // namespace

void skate3::world_loading::PrepareSky(const std::filesystem::path& game_data_root,
                                       const std::filesystem::path& content_root) {
  if (!bam_skate3_sky && !bam_night_sky) return;
  const char* sky = bam_night_sky ? "Sanitarium night sky" : "Skate 3 sky";
  std::vector<uint8_t> arena;
  std::filesystem::path archive;
  ArenaTexture panorama, glow;
  bool read = false, found = false;
  if (bam_night_sky) {
    read = ReadContentEntry(content_root,
                            "data/content/world/models/DIST_skyboxnightdlc_Textures.rx2", arena,
                            archive);
    found = read && FindArenaTexture(arena, kNightSkyPanorama, panorama) &&
            FindArenaTexture(arena, kNightSkyMoon, glow);
    if (!read) archive = content_root / "0000000000000000" / "454108E6" / "00000002";
  } else {
    archive = game_data_root / "data" / "big" / "miscload.big";
    read = ReadEb3Entry(archive, "data/content/world/models/DIST_skybox_Textures.rx2", arena);
    found = read && FindArenaTexture(arena, kSkate3SkyPanorama, panorama) &&
            FindArenaTexture(arena, kSkate3SkySun, glow);
  }
  const uint32_t diffuse = found ? InstallGuestTexture(panorama) : 0;
  const uint32_t specular = diffuse ? InstallGuestTexture(glow) : 0;
  if (!specular) {
    REXLOG_WARN("skate2-sky: {} textures unavailable (archive='{}' read={} found={}); "
                "New San Van keeps Skate 2's sky{}", sky, archive.string(), read, found,
                bam_night_sky ? " and daylight" : "");
    return;
  }
  dome_sky_diffuse.store(diffuse, std::memory_order_release);
  dome_sky_specular.store(specular, std::memory_order_release);
  night_sky.store(bam_night_sky, std::memory_order_release);
  REXLOG_INFO("skate2-sky: {} textures ready: panorama header={:08X} ({} bytes), {} header={:08X} "
              "({} bytes) archive='{}'", sky, diffuse, panorama.size,
              bam_night_sky ? "moon" : "sun", specular, glow.size, archive.string());
}

bool skate3::world_loading::DomeSkyTextures(uint32_t* diffuse, uint32_t* specular) {
  *diffuse = dome_sky_diffuse.load(std::memory_order_acquire);
  *specular = dome_sky_specular.load(std::memory_order_acquire);
  return *diffuse != 0 && *specular != 0;
}

bool skate3::world_loading::NightSky() { return night_sky.load(std::memory_order_acquire); }

// Only emitted during a debug freeze capture, after every sampled thread was
// resumed. These guarded, bounded reads may race workers; record their values
// without treating them as authoritative or changing native readiness.
void skate3::world_loading::LogFreezeSnapshot() {
  uint8_t* base = freeze_base.load();
  if (!base || !IsSkate2Gameplay() || !skate3::freeze_capture::Enabled()) return;
  const uint32_t self = freeze_gameplay.load(), manager = freeze_stream_manager.load();
  const int stream = loaded_stream.load();
  REXLOG_WARN("sim-freeze: world stream={} gameplay={:08X} world={:016X} loading={:016X} state_bytes={:08X},{:08X} manager={:08X} native_wait={} snapshot=racy",
              stream >= 0 && stream < int(skate2_streams.size()) ? skate2_streams[stream] : "unknown",
              self, StreamId(base, kWorldState, 48), StreamId(base, kWorldState, 40),
              StreamWord(base, self, 44), StreamWord(base, self, 48), manager, freeze_pause_result.load());
  if (manager < 0x10000 || manager > 0xfffc0000) return;
  const uint32_t sys = manager + 61008;
  const auto release = ReadReleaseSnapshot(base, manager);
  REXLOG_WARN("sim-freeze: streaming sim_loads={} sim_unloads={} operation={:016X} load={:016X} unload={:016X} release_target={} submitted={} processed={} retired={} epoch={} oldest_epoch={}",
              StreamVectorCount(base, sys, 124, 32), StreamVectorCount(base, sys, 172, 32),
              StreamId(base, sys, 71408), StreamId(base, sys, 71416), StreamId(base, sys, 71424),
              StreamWord(base, sys, 71468), release.submitted, release.processed,
              release.retired, release.epoch, release.oldest_epoch);
  for (uint32_t expected = 0; expected < 3; ++expected) {
    const uint32_t monitor = StreamWord(base, manager, 133000 + 4 * expected);
    if (monitor < 0x10000 || monitor > 0xffff0000 || StreamWord(base, monitor, 32) != expected) continue;
    const uint32_t count = StreamWord(base, monitor, 1400);
    REXLOG_WARN("sim-freeze: monitor kind={} wait={} cells={} ready={} bytes={}",
                expected, StreamWord(base, monitor, 28), count,
                StreamWord(base, monitor, 340), StreamWord(base, monitor, 348));
    if (count > 64) continue;
    for (uint32_t i = 0; i < count; ++i) {
      const uint32_t cell = StreamWord(base, monitor, 384 + 16 * i);
      if (cell < 0x10000 || cell > 0xffffff00) continue;
      const uint32_t flags = StreamWord(base, cell, 116);
      REXLOG_WARN("sim-freeze: cell kind={} id={:016X} state={} category={} bytes={}",
                  expected, StreamId(base, cell, 64), (flags >> 8) & 255,
                  flags >> 24, StreamWord(base, cell, 104));
    }
  }
}
