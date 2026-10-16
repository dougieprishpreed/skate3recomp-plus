#pragma once
#include <atomic>
#include <filesystem>
#include <functional>
#include <string>
#include <rex/rex_app.h>

namespace skate3 {
bool ExtractDlcForSetup(const std::filesystem::path& source,
                        const std::filesystem::path& stage, uint32_t expected_title,
                        std::atomic<uint64_t>& copied, std::string& error);
bool IsNewSanVanInstalled(const std::filesystem::path& app_root,
                          const std::filesystem::path& content_root);
// settings_path: the user settings.toml that receives the install-time choices
// (the New San Vanelona sky).
void ShowNewSanVanSetup(rex::ui::WindowedAppContext& context, rex::ui::ImGuiDrawer* drawer,
                        rex::PathConfig paths, const std::filesystem::path& settings_path,
                        std::function<void(rex::PathConfig)> resume);
bool RunNewSanVanSetup(rex::ui::WindowedAppContext& context, rex::ui::Window* window,
                       rex::ui::ImGuiDrawer* drawer, const rex::PathConfig& paths,
                       const std::filesystem::path& settings_path);
}
