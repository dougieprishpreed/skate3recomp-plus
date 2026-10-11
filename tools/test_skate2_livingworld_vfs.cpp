// Offline regression test against the production runtime DLL. Never starts the game.
#include <rex/filesystem/vfs.h>
#include <rex/filesystem/devices/host_path_device.h>
#include <algorithm>
#include <array>
#include <fstream>
#include <iostream>
#include <stdexcept>

void Require(bool valid, const std::string& reason) {
  if (!valid) throw std::runtime_error(reason);
}
int main(int argc, char** argv) {
  try {
    Require(argc == 2, "expected livingworld-v1 output folder");
    const std::filesystem::path folder(argv[1]);
    const auto fixture = folder / "vfs-test/retail/data/content/recipe/vehicle";
    std::filesystem::create_directories(fixture);
    std::ofstream(fixture / "native.recipe", std::ios::binary) << "retail";
    const std::string title = "\\Device\\Harddisk0\\Partition1";
    for (bool reverse : {false, true}) {
      rex::filesystem::VirtualFileSystem vfs;
      for (const auto& [name, root] : {std::pair("retail:", folder / "vfs-test/retail"),
                                     std::pair("skate2lw:", folder / "loose")}) {
        auto device = std::make_unique<rex::filesystem::HostPathDevice>(name, root, true);
        Require(device->Initialize() && vfs.RegisterDevice(std::move(device)), "device init");
      }
      vfs.RegisterSymbolicLink(title, "retail:");
      vfs.RegisterSymbolicLink("d:", title);
      const auto broad = [&] { vfs.RegisterSymbolicLink(title + "\\data\\content\\recipe",
                                                       "retail:\\data\\content\\recipe"); };
      if (!reverse) broad();
      for (const char* relative : {"data\\content\\nsv_s2_vehicle", "data\\content\\recipe\\nsv_s2_vehicle",
                                   "data\\content\\nsv_s2_livingworld", "data\\content\\recipe\\nsv_s2_livingworld",
                                   "data\\audio\\nsv_s2_traffic"}) {
        vfs.RegisterSymbolicLink(title + "\\" + relative, "skate2lw:\\" + std::string(relative));
      }
      if (reverse) broad();
      std::string target;
      Require(vfs.FindSymbolicLink(title + "\\data\\content\\recipe\\nsv_s2_vehicle\\test", target) &&
              target == "skate2lw:\\data\\content\\recipe\\nsv_s2_vehicle", "most specific alias");
      Require(vfs.FindSymbolicLink(title + "\\data\\content\\recipe_backup", target) &&
              target == "retail:", "component boundary");
      auto* native = vfs.ResolvePath("D:\\DATA\\CONTENT\\RECIPE\\VEHICLE\\NATIVE.RECIPE");
      Require(native && native->size() == 6, "case-insensitive retail sibling remains accessible");
      std::ifstream inventory(folder / "runtime_files.txt");
      std::string magic, path, hex; unsigned count = 0; uint64_t bytes = 0;
      Require(bool(inventory >> magic >> count) && magic == "NSV_LW_FILES_V2", "inventory");
      for (unsigned i = 0; i < count; ++i) {
        Require(bool(inventory >> path >> bytes >> hex), "inventory row");
        std::replace(path.begin(), path.end(), '/', '\\');
        rex::filesystem::File* file = nullptr; rex::filesystem::FileAction action{};
        auto status = vfs.OpenFile(nullptr, "d:\\" + path, rex::filesystem::FileDisposition::kOpen,
          rex::filesystem::FileAccess::kGenericRead, false, true, &file, &action);
        Require(status == 0 && file, "actual open: " + path);
        std::array<uint8_t,8> actual{}; size_t read = 0;
        auto expected = std::stoull(hex, nullptr, 16);
        bool valid = file->entry()->absolute_path() == "skate2lw:\\" + path &&
                     file->entry()->size() == bytes && file->ReadSync(actual, 0, &read) == 0 && read == 8;
        for (size_t j = 0; j < actual.size(); ++j) valid = valid && actual[j] == uint8_t(expected >> (56 - 8*j));
        file->Destroy(); Require(valid, "header / size / device: " + path);
      }
      std::cout << "PASS: " << count << " actual dependency opens; alias order=" << reverse << '\n';
    }
    return 0;
  } catch (const std::exception& e) { std::cerr << "FAIL: " << e.what() << '\n'; return 1; }
}
