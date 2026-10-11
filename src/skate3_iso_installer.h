#pragma once

#include <atomic>
#include <string>
#include <filesystem>
#include <functional>

#include <rex/rex_app.h>

namespace skate3 {

// Shared reader for both installer ISO inputs. The title is verified before writing.
bool ValidateXboxIso(const std::filesystem::path& source, uint32_t title, std::string& error);
bool ExtractXboxIso(const std::filesystem::path& source, const std::filesystem::path& target,
                    uint32_t expected_title, std::atomic<uint64_t>& copied,
                    std::atomic<uint64_t>& total, std::string& error);
bool IsGameInstalled(const std::filesystem::path& game_root);
void ShowRexglueIsoInstallWizard(rex::ui::ImGuiDrawer* drawer, rex::PathConfig runtime_paths,
                                 std::function<void(rex::PathConfig)> complete);
bool RunRexglueIsoInstallWizardBlocking(rex::ui::WindowedAppContext& app_context,
                                        rex::ui::Window* window,
                                        rex::ui::ImGuiDrawer* drawer,
                                        rex::PathConfig runtime_paths,
                                        rex::PathConfig& installed_paths);

}  // namespace skate3
