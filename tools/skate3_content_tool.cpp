// Offline extraction harness. Does not create a game runtime or launch guest code.
#include "skate3_iso_installer.h"
#include "skate3_nsv_installer.h"
#include "skate3_setup_sources.h"
#include <iostream>

int wmain(int argc, wchar_t** argv) {
  if (argc != 5) {
    std::cerr << "Usage: skate3_content_tool iso|dlc TITLE_HEX SOURCE NEW_DESTINATION\n";
    return 2;
  }
  const auto mode=std::wstring_view(argv[1]);
  const auto title=static_cast<uint32_t>(std::stoul(argv[2],nullptr,16));
  if (mode==L"scan" || mode==L"folder") {
    std::string message;
    bool ok;
    if (mode==L"scan") {
      std::vector<skate3::SetupDlcSource> found;
      ok=skate3::DiscoverSetupDlc(argv[3],title,found,message);
      for (const auto& source:found) std::cout << source.path.string() << " extracted=" << source.extracted << '\n';
    } else {
      skate3::SetupGameSource source{argv[3],true};
      ok=skate3::ValidateSetupGame(source,title,message);
    }
    std::cout << "valid=" << ok << " " << message << '\n';
    return ok?0:1;
  }
  const std::filesystem::path dest(argv[4]);
  if (std::filesystem::exists(dest)) {
    std::cerr << "Destination must not exist\n"; return 2;
  }

  std::atomic<uint64_t> copied{0}, total{0};
  std::string error;
  if (mode==L"stage-folder") {
    std::vector<skate3::SetupDlcSource> found;
    if (!skate3::DiscoverSetupDlc(argv[3],title,found,error)) { std::cerr<<error; return 1; }
    for (const auto& source:found) if (!skate3::StageSetupDlc(source,dest,title,copied,error)) {
      std::cerr<<error; return 1;
    }
    std::cout<<"Staged "<<found.size()<<" content groups, "<<copied.load()<<" bytes\n"; return 0;
  }
  bool ok = std::wstring_view(argv[1]) == L"iso"
      ? skate3::ExtractXboxIso(argv[3], dest, title, copied, total, error)
      : skate3::ExtractDlcForSetup(argv[3], dest, title, copied, error);
  std::cout << (ok ? "Extracted " : "Failed: ") << (ok ? std::to_string(copied.load()) : error) << '\n';
  return ok ? 0 : 1;
}
