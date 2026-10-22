#include "skate3_nsv_installer.h"
#include "skate3_nsv_components.h"
#include "skate3_updater.h"
#include "skate3_install_paths.h"
#include "skate3_setup_sources.h"
#include "skate3_title_update_installer.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <functional>
#include <fstream>
#include <iterator>
#include <memory>
#include <random>
#include <string_view>
#include <thread>
#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/logging.h>
#include <rex/ui/imgui_dialog.h>
#include <rex/ui/windowed_app_context.h>
#include <imgui.h>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <commdlg.h>
#include <shobjidl.h>
#include <gdiplus.h>
#include <rex/ui/window_win.h>
#elif defined(__APPLE__)
#include <SDL3/SDL.h>
#else
#include <gtk/gtk.h>
#endif

// New San Vanelona sky (skate3_world_loading.cpp): true = Skate 2's.
REXCVAR_DECLARE(bool, skate3_skate2_sky);
// New San Vanelona at night (Sanitarium sky); takes priority over the above.
REXCVAR_DECLARE(bool, skate3_skate2_night);
// Startup update check (skate3_updater.cpp).
REXCVAR_DECLARE(bool, skate3_update_check);

namespace skate3 {
#if defined(__APPLE__)
std::filesystem::path PickSetupSourceMacOS(const wchar_t* title, bool folder, bool iso);
#endif
namespace {
namespace fs = std::filesystem;
fs::path Pick(const wchar_t* title, bool folder, bool iso = false) {
  struct BalanceInput {
    ~BalanceInput() {
      auto& io=ImGui::GetIO(); io.AddMouseButtonEvent(0,false);
      io.AddKeyEvent(ImGuiKey_Enter,false); io.AddKeyEvent(ImGuiKey_Space,false);
    }
  } balance;
  #if defined(_WIN32)
  if (folder) {
    IFileOpenDialog* dialog = nullptr;
    const HRESULT initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    fs::path result;
    if (SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_PPV_ARGS(&dialog)))) {
      DWORD options = 0; dialog->GetOptions(&options);
      dialog->SetOptions(options | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST);
      dialog->SetTitle(title);
      if (SUCCEEDED(dialog->Show(GetActiveWindow()))) {
        IShellItem* item = nullptr;
        if (SUCCEEDED(dialog->GetResult(&item))) {
          PWSTR path = nullptr;
          if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path))) {
            result = path; CoTaskMemFree(path);
          }
          item->Release();
        }
      }
      dialog->Release();
    }
    if (SUCCEEDED(initialized)) CoUninitialize();
    return result;
  }
  std::vector<wchar_t> name(32768);
  OPENFILENAMEW ofn{}; ofn.lStructSize = sizeof(ofn); ofn.hwndOwner = GetActiveWindow();
  ofn.lpstrFile = name.data(); ofn.nMaxFile = DWORD(name.size()); ofn.lpstrTitle = title;
  ofn.lpstrFilter = iso ? L"Xbox 360 ISO\0*.iso\0\0" : L"Title update package\0*.*\0\0";
  ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_NOCHANGEDIR | OFN_PATHMUSTEXIST;
  return GetOpenFileNameW(&ofn) ? fs::path(name.data()) : fs::path{};
#elif defined(__APPLE__)
  return PickSetupSourceMacOS(title, folder, iso);
#else
  const auto title_utf8 = rex::path_to_utf8(fs::path(title));
  auto* dialog = gtk_file_chooser_dialog_new(title_utf8.c_str(), nullptr,
      folder ? GTK_FILE_CHOOSER_ACTION_SELECT_FOLDER : GTK_FILE_CHOOSER_ACTION_OPEN,
      "_Cancel", GTK_RESPONSE_CANCEL, "_Select", GTK_RESPONSE_ACCEPT, nullptr);
  if (!dialog) return {};
  if (iso) {
    auto* filter = gtk_file_filter_new();
    gtk_file_filter_set_name(filter, "Xbox 360 ISO");
    gtk_file_filter_add_pattern(filter, "*.iso");
    gtk_file_filter_add_pattern(filter, "*.ISO");
    gtk_file_chooser_add_filter(GTK_FILE_CHOOSER(dialog), filter);
  }
  fs::path result;
  if (gtk_dialog_run(GTK_DIALOG(dialog)) == GTK_RESPONSE_ACCEPT) {
    if (char* name = gtk_file_chooser_get_filename(GTK_FILE_CHOOSER(dialog))) {
      result = name; g_free(name);
    }
  }
  gtk_widget_destroy(dialog);
  while (gtk_events_pending()) gtk_main_iteration_do(FALSE);
  return result;
#endif
}

rex::ui::ImmediateTexture* Background(rex::ui::ImGuiDrawer* drawer) {
#if defined(_WIN32)
  Gdiplus::GdiplusStartupInput input; ULONG_PTR token = 0;
  if (Gdiplus::GdiplusStartup(&token, &input, nullptr) != Gdiplus::Ok) return {};
  std::vector<uint8_t> pixels; UINT width = 0, height = 0;
  {
    const auto path = SetupResourceRoot() / "installer/title.png";
    Gdiplus::Bitmap bitmap(path.c_str());
    if (bitmap.GetLastStatus() == Gdiplus::Ok) {
      width = bitmap.GetWidth(); height = bitmap.GetHeight();
      if (width && height && width <= 4096 && height <= 4096) {
        Gdiplus::Rect rect(0, 0, width, height); Gdiplus::BitmapData data{};
        if (bitmap.LockBits(&rect, Gdiplus::ImageLockModeRead, PixelFormat32bppARGB, &data) == Gdiplus::Ok) {
          pixels.resize(size_t(width) * height * 4);
          for (UINT y = 0; y < height; ++y) for (UINT x = 0; x < width; ++x) {
            const auto* src = static_cast<uint8_t*>(data.Scan0) + ptrdiff_t(y) * data.Stride + x * 4;
            auto* dst = pixels.data() + (size_t(y) * width + x) * 4;
            dst[0] = src[2]; dst[1] = src[1]; dst[2] = src[0]; dst[3] = src[3];
          }
          bitmap.UnlockBits(&data);
        }
      }
    }
  }
  Gdiplus::GdiplusShutdown(token);
  return pixels.empty() ? nullptr : drawer->CreateImage(width, height, pixels.data());
#elif defined(__APPLE__)
  const auto path = SetupResourceRoot() / "installer/title.png";
  std::ifstream in(path, std::ios::binary);
  std::vector<char> encoded((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
  if (encoded.empty()) return nullptr;
  auto* source = SDL_LoadPNG_IO(SDL_IOFromConstMem(encoded.data(), encoded.size()), true);
  if (!source) return nullptr;
  auto* rgba = SDL_ConvertSurface(source, SDL_PIXELFORMAT_RGBA32);
  SDL_DestroySurface(source);
  if (!rgba) return nullptr;
  std::vector<uint8_t> pixels(size_t(rgba->w) * rgba->h * 4);
  for (int y = 0; y < rgba->h; ++y)
    std::copy_n(static_cast<const uint8_t*>(rgba->pixels) + y * rgba->pitch,
                rgba->w * 4, pixels.data() + size_t(y) * rgba->w * 4);
  auto* image = drawer->CreateImage(rgba->w, rgba->h, pixels.data());
  SDL_DestroySurface(rgba);
  return image;
#else
  const auto path = SetupResourceRoot() / "installer/title.png";
  auto* pixbuf = gdk_pixbuf_new_from_file(path.c_str(), nullptr);
  if (!pixbuf) return nullptr;
  const int w = gdk_pixbuf_get_width(pixbuf), h = gdk_pixbuf_get_height(pixbuf);
  const int channels = gdk_pixbuf_get_n_channels(pixbuf), stride = gdk_pixbuf_get_rowstride(pixbuf);
  const auto* data = gdk_pixbuf_read_pixels(pixbuf);
  std::vector<uint8_t> pixels(size_t(w) * h * 4);
  for (int y = 0; y < h; ++y) for (int x = 0; x < w; ++x) {
    const auto* src = data + y * stride + x * channels;
    auto* dst = pixels.data() + (size_t(y) * w + x) * 4;
    std::copy_n(src, 3, dst); dst[3] = channels == 4 ? src[3] : 255;
  }
  auto* image = drawer->CreateImage(w, h, pixels.data());
  g_object_unref(pixbuf);
  return image;
#endif
}

struct Row { bool ready = false; std::string detail, error; };

class SetupDialog final : public rex::ui::ImGuiDialog {
 public:
  // update: the New San Van components this build needs updated (empty = full setup).
  SetupDialog(rex::ui::ImGuiDrawer* drawer, rex::PathConfig paths, fs::path settings_path,
              std::shared_ptr<bool> finished, std::function<void(bool)> closed = {},
              NsvStatus update = {})
      : ImGuiDialog(drawer), paths_(std::move(paths)), settings_path_(std::move(settings_path)),
        finished_(std::move(finished)), closed_(std::move(closed)), update_(std::move(update)) {
    sky_choice_ = REXCVAR_GET(skate3_skate2_night) ? 2 : REXCVAR_GET(skate3_skate2_sky) ? 0 : 1;
    inputs_.s3 = {paths_.game_data_root, true};
    rows_[0].ready = ValidateSetupGame(inputs_.s3, 0x454108E6, rows_[0].error);
    rows_[0].error.clear();
    if (rows_[0].ready) rows_[0].detail = "Already installed";
    rows_[2].ready = IsTitleUpdateInstalled(paths_.game_data_root);
    if (rows_[2].ready) { rows_[2].detail = "Verified and installed"; inputs_.verified_tu = paths_.game_data_root; }
  }
  void Join() { if (worker_.joinable()) worker_.join(); }
  void Shutdown() {
    Join();
    if (apply_) { apply_(); apply_={}; }
    for (const auto& path:tu_stages_) { std::error_code ec; fs::remove_all(path,ec); }
    tu_stages_.clear();
  }
 protected:
  void OnClose() override {
    Shutdown();
    background_ = nullptr;
    if (closed_) closed_(*finished_);
  }
  void OnDraw(ImGuiIO& io) override {
    const float saved_dpi=ImGui::GetStyle().FontScaleDpi;
    ImGui::GetStyle().FontScaleDpi=1.f;
    if (busy_ && worker_done_.load(std::memory_order_acquire)) {
      Join(); busy_ = false;
      if (apply_) { apply_(); apply_ = {}; }
    }
    if (!background_attempted_) { background_ = Background(imgui_drawer()); background_attempted_ = true; }
    const float scale = std::min(io.DisplaySize.x / 1280.f, io.DisplaySize.y / 720.f);
    const float s = std::max(scale, 0.4f);
    const ImVec2 origin((io.DisplaySize.x - 1280*s)*0.5f, (io.DisplaySize.y - 720*s)*0.5f);
    auto point = [&](float x, float y) { return ImVec2(origin.x+x*s, origin.y+y*s); };
    auto* bg = ImGui::GetBackgroundDrawList();
    if (background_) {
      const float cover = std::max(io.DisplaySize.x/background_->width, io.DisplaySize.y/background_->height);
      ImVec2 size(background_->width*cover, background_->height*cover);
      ImVec2 start((io.DisplaySize.x-size.x)*0.5f, (io.DisplaySize.y-size.y)*0.5f);
      bg->AddImage(reinterpret_cast<ImTextureID>(background_), start,
                   ImVec2(start.x+size.x,start.y+size.y));
    }
    bg->AddRectFilled(ImVec2(0,0), io.DisplaySize, IM_COL32(4,10,17,110));
    bg->AddRectFilledMultiColor(ImVec2(0,0), io.DisplaySize, IM_COL32(4,10,17,60),
        IM_COL32(4,10,17,30), IM_COL32(4,10,17,175), IM_COL32(4,10,17,190));
    ImGui::SetNextWindowPos(ImVec2(0,0)); ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(.93f,.95f,.96f,1));
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(.15f,.22f,.28f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(.23f,.33f,.40f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(.30f,.42f,.48f,1));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6*s);
    ImGui::PushFont(imgui_drawer()->ui_font(), 16*s);
    ImGui::Begin("City setup", nullptr, ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoScrollWithMouse |
                 ImGuiWindowFlags_NoScrollbar);
    auto* draw = ImGui::GetWindowDrawList();
    draw->AddRectFilled(point(66,208), point(1214,686), IM_COL32(10,17,24,235), 14*s);
    draw->AddRect(point(66,208), point(1214,686), IM_COL32(101,131,150,65), 14*s);
    ImGui::SetCursorScreenPos(point(90,225));
    ImGui::PushFont(imgui_drawer()->ui_font_semibold(), 25*s);
    ImGui::TextUnformatted(sky_step_ ? "Choose your sky" : Updating() ? "New San Van update"
                                                                    : "Welcome to New San Van");
    ImGui::PopFont();
    ImGui::SetCursorScreenPos(point(90,262));
    ImGui::TextColored(ImVec4(.65f,.73f,.78f,1), "%s", sky_step_
        ? "Pick the sky over New San Vanelona. You can change it later in Settings > World."
        : Updating() ? "This version improves New San Van. Your city needs a quick update before you skate."
                     : "Choose your files. Installation starts when you're ready.");
    if (sky_step_) {
      // Install step 2: the sky over New San Vanelona (skate3_skate2_sky, and
      // skate3_skate2_night, which needs the Sanitarium download).
      const char* sky_titles[] = {"Skate 2 sky", "Skate 3 sky", "Night"};
      const char* sky_hints[] = {"New San Vanelona's original skybox and clouds",
                                 "Skate 3's skybox and clouds over New San Vanelona",
                                 night_available_
                                     ? "The Sanitarium's night sky and moon, with the city lit for night"
                                     : "Needs the Sanitarium download: add it under Skate 3 DLC first"};
      for (int i=0;i<3;++i) {
        const float y = 306.f + i*74.f;
        const bool enabled = i<2 || night_available_;
        const bool selected = sky_choice_ == i;
        ImGui::PushID(100+i);
        ImGui::SetCursorScreenPos(point(84,y-10));
        ImGui::BeginDisabled(!enabled);
        if (ImGui::InvisibleButton("sky",ImVec2(1112*s,62*s))) sky_choice_ = i;
        const bool hovered = ImGui::IsItemHovered();
        ImGui::EndDisabled();
        if (selected || hovered)
          draw->AddRectFilled(point(84,y-10),point(1196,y+52),
              selected?IM_COL32(51,171,116,38):IM_COL32(101,131,150,30),8*s);
        if (selected) draw->AddRect(point(84,y-10),point(1196,y+52),IM_COL32(51,171,116,150),8*s);
        ImVec2 mark = point(102,y+14);
        if (selected) {
          draw->AddCircleFilled(mark, 10*s, IM_COL32(51,171,116,255));
          draw->AddLine(ImVec2(mark.x-5*s,mark.y),ImVec2(mark.x-1*s,mark.y+4*s),IM_COL32(240,255,246,255),2*s);
          draw->AddLine(ImVec2(mark.x-1*s,mark.y+4*s),ImVec2(mark.x+5*s,mark.y-4*s),IM_COL32(240,255,246,255),2*s);
        } else draw->AddCircle(mark,9*s,enabled?IM_COL32(111,131,146,190):IM_COL32(111,131,146,90),24,1.5f*s);
        ImGui::SetCursorScreenPos(point(122,y));
        ImGui::PushFont(imgui_drawer()->ui_font_semibold(), 18*s);
        if (enabled) ImGui::TextUnformatted(sky_titles[i]);
        else ImGui::TextColored(ImVec4(.93f,.95f,.96f,.45f), "%s", sky_titles[i]);
        ImGui::PopFont();
        draw->AddText(ImGui::GetFont(),15*s,point(122,y+23),
            enabled?IM_COL32(157,179,192,255):IM_COL32(157,179,192,140),sky_hints[i]);
        ImGui::PopID();
      }
      ImGui::SetCursorScreenPos(point(90,544));
      ImGui::PushTextWrapPos(point(1186,0).x);
      ImGui::TextColored(ImVec4(.66f,.75f,.80f,1),
          "Skate 2 and Skate 3 change only the sky; Night also lights the city for night. "
          "The distant mountains, bay and city backdrop stay with every sky.");
      ImGui::PopTextWrapPos();
      ImGui::SetCursorScreenPos(point(694,610));
      if (ImGui::Button("Back",ImVec2(160*s,44*s))) sky_step_ = false;
      ImGui::SameLine(0,10*s);
      ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(.18f,.49f,.37f,1));
      if (ImGui::Button("Install",ImVec2(318*s,44*s))) {
        // Saved before installing; the world loader reads them at startup.
        // Night leaves the day sky choice as it was, as the Settings row does.
        static constexpr const char* kSkyNames[] = {"Skate 2", "Skate 3", "Night"};
        REXCVAR_SET(skate3_skate2_night, sky_choice_ == 2);
        if (sky_choice_ != 2) REXCVAR_SET(skate3_skate2_sky, sky_choice_ == 0);
        rex::cvar::SaveConfigValues(settings_path_, {"skate3_skate2_sky", "skate3_skate2_night"});
        REXLOG_INFO("Setup: New San Vanelona sky = {}", kSkyNames[sky_choice_]);
        sky_step_ = false;
        StartInstall();
      }
      ImGui::PopStyleColor();
      ImGui::End(); ImGui::PopFont(); ImGui::PopStyleVar(); ImGui::PopStyleColor(5);
      ImGui::GetStyle().FontScaleDpi=saved_dpi;
      return;
    }
    const char* titles[] = {"Skate 3", "Skate 2", "Title Update 3", "Skate 3 DLC", "Skate 2 DLC"};
    const char* hints[] = {"Xbox 360 ISO or extracted disc folder", "Xbox 360 ISO or extracted disc folder",
        "Download the update or use your own file", "Optional  /  packages or extracted content",
        "Required  /  Throwback, Maloof and Fantasy Plaza"};
    ImGui::BeginDisabled(busy_ || installed_);
    // A city update shows only the sources the changed parts need.
    std::vector<int> visible;
    for (int i=0;i<5;++i)
      if (!Updating() || (i==1 && update_.needs_skate2) || (i==4 && update_.needs_skate2_dlc))
        visible.push_back(i);
    if (Updating()) {
      ImGui::SetCursorScreenPos(point(90,300.f+visible.size()*57.f+8.f));
      ImGui::PushTextWrapPos(point(1186,0).x);
      ImGui::TextColored(ImVec4(.66f,.75f,.80f,1), "%s", visible.empty()
          ? "No files are needed. Only the parts of the city that changed are updated."
          : "Select the same files you used when you installed. Only the parts of the city that "
            "changed are updated; your Skate 3 files, settings and saves stay as they are.");
      ImGui::PopTextWrapPos();
    }
    for (size_t slot=0;slot<visible.size();++slot) {
      const int i = visible[slot];
      const float y = 300.f + slot*57.f;
      ImGui::PushID(i);
      ImVec2 mark = point(102,y+14);
      if (rows_[i].ready) {
        draw->AddCircleFilled(mark, 10*s, IM_COL32(51,171,116,255));
        draw->AddLine(ImVec2(mark.x-5*s,mark.y),ImVec2(mark.x-1*s,mark.y+4*s),IM_COL32(240,255,246,255),2*s);
        draw->AddLine(ImVec2(mark.x-1*s,mark.y+4*s),ImVec2(mark.x+5*s,mark.y-4*s),IM_COL32(240,255,246,255),2*s);
      } else draw->AddCircle(mark,9*s,IM_COL32(111,131,146,190),24,1.5f*s);
      ImGui::SetCursorScreenPos(point(122,y));
      ImGui::PushFont(imgui_drawer()->ui_font_semibold(), 18*s);
      ImGui::TextUnformatted(titles[i]); ImGui::PopFont();
      const auto& row = rows_[i];
      const std::string text = !row.error.empty() ? row.error : row.detail.empty() ? hints[i] : row.detail;
      ImVec4 clip(point(122,y+23).x,point(122,y+23).y,point(848,y+46).x,point(848,y+46).y);
      draw->AddText(ImGui::GetFont(),15*s,point(122,y+23),
          row.error.empty()?IM_COL32(157,179,192,255):IM_COL32(246,153,134,255), text.c_str(),nullptr,0,&clip);
      if (ImGui::IsMouseHoveringRect(point(122,y+23),point(848,y+46))) ImGui::SetTooltip("%s",text.c_str());
      ImGui::SetCursorScreenPos(point(864,y+5));
      if (i<2) {
        if (ImGui::Button("Select ISO",ImVec2(132*s,34*s))) ChooseGame(i,false);
        ImGui::SameLine(0,8*s);
        if (ImGui::Button("Extracted folder",ImVec2(178*s,34*s))) ChooseGame(i,true);
      } else if (i==2) {
        if (ImGui::Button("Download TU3",ImVec2(154*s,34*s))) ChooseUpdate(true);
        ImGui::SameLine(0,8*s);
        if (ImGui::Button("Select file",ImVec2(156*s,34*s))) ChooseUpdate(false);
      } else {
        if (ImGui::Button("Select folder",ImVec2(260*s,34*s))) ChooseDlc(i);
        if (i==3) {
          ImGui::SameLine(0,8*s);
          if (ImGui::Button("X",ImVec2(50*s,34*s))) { rows_[3]={}; inputs_.dlc3.clear(); }
        }
      }
      if (slot+1<visible.size()) draw->AddLine(point(122,y+51),point(1186,y+51),IM_COL32(73,99,118,75));
      ImGui::PopID();
    }
    ImGui::EndDisabled();
    ImGui::SetCursorScreenPos(point(90,612));
    if (busy_) {
      ImGui::TextUnformatted(status_.c_str());
      ImGui::SetCursorScreenPos(point(90,643));
      auto total=total_.load(), done=copied_.load();
      ImGui::ProgressBar(total ? std::min(1.f,float(done)/float(total)) : -float(ImGui::GetTime()),
                         ImVec2(1096*s,12*s), "");
    } else {
      const bool ready = Updating()
          ? (!update_.needs_skate2 || rows_[1].ready) && (!update_.needs_skate2_dlc || rows_[4].ready)
          : rows_[0].ready && rows_[1].ready && rows_[2].ready && rows_[4].ready && rows_[3].error.empty();
      ImGui::PushTextWrapPos(point(844,0).x);
      ImGui::TextColored(error_.empty()?ImVec4(.66f,.75f,.80f,1):ImVec4(.96f,.60f,.53f,1), "%s",
          error_.empty() ? installed_ ? (Updating() ? "Your city is up to date." : "Your city is ready.")
                                      : ready ? "All required files are ready." : "Add the required files to continue."
                         : error_.c_str());
      ImGui::PopTextWrapPos();
      ImGui::SetCursorScreenPos(point(864,610));
      ImGui::BeginDisabled(!ready && !installed_);
      ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(.18f,.49f,.37f,1));
      if (ImGui::Button(installed_ ? "Start skating" : Updating() ? "Update city" : "Install",ImVec2(318*s,44*s))) {
        if (installed_) { *finished_=true; Close(); }
        else if (Updating()) StartUpdate();
        else {
          // The sky choice, then installation. Night is offered only when the
          // Sanitarium download will be installed (or already is).
          night_available_ = SanitariumAvailable();
          if (sky_choice_ == 2 && !night_available_)
            sky_choice_ = REXCVAR_GET(skate3_skate2_sky) ? 0 : 1;
          sky_step_ = true;
        }
      }
      ImGui::PopStyleColor(); ImGui::EndDisabled();
    }
    ImGui::End(); ImGui::PopFont(); ImGui::PopStyleVar(); ImGui::PopStyleColor(5);
    ImGui::GetStyle().FontScaleDpi=saved_dpi;
  }
 private:
  using Completion=std::function<void()>;
  // Night draws the Sanitarium's sky (player1p_00000000.big): from the Skate 3
  // DLC chosen here, or a package already installed in the content root
  // (where the world loader looks for it).
  bool SanitariumAvailable() const {
    constexpr std::string_view kArchive = "player1p_00000000.big";
    for (const auto& source : inputs_.dlc3)
      for (const auto& name : source.archives)
        if (name == kArchive) return true;
    std::error_code ec;
    const auto root = paths_.user_data_root / "0000000000000000" / "454108E6" / "00000002";
    for (const auto& entry : fs::directory_iterator(root, ec))
      if (fs::is_regular_file(entry.path() / std::string(kArchive), ec)) return true;
    return false;
  }
  bool Updating() const { return !update_.components.empty(); }
  void StartUpdate() {
    const auto input=inputs_; const auto paths=paths_; const auto components=update_.components;
    Job("Updating New San Van. This may take a few minutes...", [this,input,paths,components] {
      std::string error; const bool ok=UpdateNewSanVan(input,paths,components,copied_,total_,error);
      if (!ok) REXLOG_ERROR("New San Van update failed: {}",error);
      else REXLOG_INFO("New San Van update completed ({})",JoinNsvComponents(components));
      return [this,ok,error] { installed_=ok; error_=error; };
    });
  }
  void StartInstall() {
    const auto input=inputs_; const auto paths=paths_;
    Job("Installing your cities. This may take several minutes...", [this,input,paths] {
      std::string error; const bool ok=InstallSetupInputs(input,paths,copied_,total_,error);
      if (!ok) REXLOG_ERROR("Setup failed: {}",error);
      else REXLOG_INFO("Setup completed successfully");
      return [this,ok,error] { installed_=ok; error_=error; };
    });
  }
  void Job(std::string status,std::function<Completion()> work) {
    if (busy_) return;
    error_.clear(); status_=std::move(status); busy_=true; worker_done_=false; copied_=0; total_=0;
    worker_=std::thread([this,work=std::move(work)] {
      try { apply_=work(); }
      catch (const std::exception& e) { std::string error=e.what(); apply_=[this,error] { error_=error; }; }
      worker_done_.store(true,std::memory_order_release);
    });
  }
  void ChooseGame(int row,bool folder) {
    auto path=Pick(row==0?L"Select Skate 3 disc source":L"Select Skate 2 disc source",folder,!folder);
    if (path.empty()) return;
    rows_[row]={};
    Job("Checking disc files...",[this,row,path,folder] {
      SetupGameSource source{path,folder}; std::string error;
      bool ok=ValidateSetupGame(source,row==0?0x454108E6:0x4541087F,error);
      return [this,row,source,ok,error] {
        (row==0?inputs_.s3:inputs_.s2)=source;
        rows_[row]={ok,rex::path_to_utf8(source.path),error};
      };
    });
  }
  void ChooseDlc(int row) {
    auto path=Pick(row==3?L"Select Skate 3 DLC folder (optional)":L"Select Skate 2 DLC folder",true);
    if (path.empty()) return;
    rows_[row]={};
    Job("Scanning DLC folders...",[this,row,path] {
      std::vector<SetupDlcSource> sources; std::string message;
      bool ok=DiscoverSetupDlc(path,row==3?0x454108E6:0x4541087F,sources,message);
      return [this,row,path,sources,ok,message] {
        (row==3?inputs_.dlc3:inputs_.dlc2)=sources;
        rows_[row]={ok,ok?message+"  /  "+rex::path_to_utf8(path):"",ok?"":message};
      };
    });
  }
  void ChooseUpdate(bool download) {
    auto file=download?fs::path{}:Pick(L"Select Title Update 3 package",false);
    if (!download && file.empty()) return;
    rows_[2]={};
    Job(download?"Downloading and verifying Title Update 3...":"Verifying Title Update 3...",[this,file,download] {
      auto stage=ContentInstallRoot(paths_.user_data_root)/"install-state"/("tu-"+std::to_string(std::random_device{}()));
      fs::create_directories(stage.parent_path());
      if (!fs::create_directory(stage)) throw std::runtime_error("Unable to create update staging");
      std::string error;
      const bool ok=(download?DownloadTitleUpdateForSetup(stage,copied_,total_,error):StageTitleUpdateFromFile(file,stage,error)) && IsTitleUpdateInstalled(stage);
      if (!ok) { std::error_code ec; fs::remove_all(stage,ec); }
      return [this,stage,ok,error] {
        if (ok) { inputs_.verified_tu=stage; tu_stages_.push_back(stage); }
        rows_[2]={ok,ok?"Verified Title Update 3":"",error.empty()&&!ok?"The update could not be verified.":error};
      };
    });
  }
  rex::PathConfig paths_;
  fs::path settings_path_;
  std::shared_ptr<bool> finished_;
  std::function<void(bool)> closed_;
  NsvStatus update_;
  SetupInputs inputs_;
  std::array<Row,5> rows_{};
  bool busy_=false,installed_=false,background_attempted_=false;
  bool sky_step_=false,night_available_=false;
  int sky_choice_=0;  // 0 = Skate 2 sky, 1 = Skate 3 sky, 2 = Night
  std::string status_,error_;
  std::thread worker_;
  std::atomic<bool> worker_done_{false};
  std::atomic<uint64_t> copied_{0},total_{0};
  Completion apply_;
  std::vector<fs::path> tu_stages_;
  rex::ui::ImmediateTexture* background_ = nullptr;
};

// Startup check for a newer release, and its download/installation. Shown only
// when an update exists (or once the player asks to install it).
struct UpdateOutcome {
  bool done = false;     // the dialog has finished
  bool proceed = false;  // continue starting the game (false: quit, or restart into the update)
};

class UpdateDialog final : public rex::ui::ImGuiDialog {
 public:
  UpdateDialog(rex::ui::ImGuiDrawer* drawer, fs::path settings_path,
               std::shared_ptr<UpdateOutcome> outcome, std::function<void()> closed = {})
      : ImGuiDialog(drawer), settings_path_(std::move(settings_path)), outcome_(std::move(outcome)),
        closed_(std::move(closed)) {
    worker_ = std::thread([this] {
      std::string detail;
      result_ = updater::CheckForUpdate(release_, detail);
      if (result_ != updater::CheckResult::kAvailable && !detail.empty())
        REXLOG_INFO("update: check skipped ({})", detail);
      checked_.store(true, std::memory_order_release);
    });
  }
  ~UpdateDialog() { if (worker_.joinable()) worker_.join(); }

 protected:
  void OnClose() override {
    if (worker_.joinable()) worker_.join();
    background_ = nullptr;
    outcome_->done = true;
    if (closed_) closed_();
  }
  void OnDraw(ImGuiIO& io) override {
    if (state_ == State::kChecking && checked_.load(std::memory_order_acquire)) {
      worker_.join();
      if (result_ == updater::CheckResult::kAvailable) {
        state_ = State::kAvailable;
        in_place_ = updater::CanInstallInPlace(in_place_reason_);
      } else {
        Finish(true);
        return;
      }
    }
    // Nothing is drawn until a newer release is confirmed: up to date, offline
    // or a failed check goes straight on to the game without a flash.
    if (state_ == State::kChecking) return;
    if (state_ == State::kInstalling && installed_.load(std::memory_order_acquire)) {
      worker_.join();
      if (install_ok_) {
        std::string error;
        if (updater::Relaunch(error)) {
          Finish(false);  // the new version starts; this one quits
          return;
        }
        error_ = error;
        state_ = State::kRestartManually;
      } else {
        state_ = State::kFailed;
      }
    }
    const float saved_dpi=ImGui::GetStyle().FontScaleDpi;
    ImGui::GetStyle().FontScaleDpi=1.f;
    if (!background_attempted_) { background_ = Background(imgui_drawer()); background_attempted_ = true; }
    const float scale = std::min(io.DisplaySize.x / 1280.f, io.DisplaySize.y / 720.f);
    const float s = std::max(scale, 0.4f);
    const ImVec2 origin((io.DisplaySize.x - 1280*s)*0.5f, (io.DisplaySize.y - 720*s)*0.5f);
    auto point = [&](float x, float y) { return ImVec2(origin.x+x*s, origin.y+y*s); };
    auto* bg = ImGui::GetBackgroundDrawList();
    if (background_) {
      const float cover = std::max(io.DisplaySize.x/background_->width, io.DisplaySize.y/background_->height);
      ImVec2 size(background_->width*cover, background_->height*cover);
      ImVec2 start((io.DisplaySize.x-size.x)*0.5f, (io.DisplaySize.y-size.y)*0.5f);
      bg->AddImage(reinterpret_cast<ImTextureID>(background_), start, ImVec2(start.x+size.x,start.y+size.y));
    }
    bg->AddRectFilled(ImVec2(0,0), io.DisplaySize, IM_COL32(4,10,17,110));
    ImGui::SetNextWindowPos(ImVec2(0,0)); ImGui::SetNextWindowSize(io.DisplaySize);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(.93f,.95f,.96f,1));
    ImGui::PushStyleColor(ImGuiCol_Button, ImVec4(.15f,.22f,.28f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(.23f,.33f,.40f,1));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, ImVec4(.30f,.42f,.48f,1));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6*s);
    ImGui::PushFont(imgui_drawer()->ui_font(), 16*s);
    ImGui::Begin("Update", nullptr, ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoMove |
                 ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoScrollWithMouse |
                 ImGuiWindowFlags_NoScrollbar);
    DrawPanel(point, s);
    ImGui::End(); ImGui::PopFont(); ImGui::PopStyleVar(); ImGui::PopStyleColor(5);
    ImGui::GetStyle().FontScaleDpi=saved_dpi;
  }

 private:
  enum class State { kChecking, kAvailable, kInstalling, kFailed, kRestartManually };

  template <typename Point>
  void DrawPanel(Point point, float s) {
    auto* draw = ImGui::GetWindowDrawList();
    draw->AddRectFilled(point(66,330), point(1214,620), IM_COL32(10,17,24,235), 14*s);
    draw->AddRect(point(66,330), point(1214,620), IM_COL32(101,131,150,65), 14*s);
    ImGui::SetCursorScreenPos(point(90,350));
    ImGui::PushFont(imgui_drawer()->ui_font_semibold(), 25*s);
    const char* title = state_ == State::kFailed ? "The update couldn't be installed"
                        : state_ == State::kRestartManually ? "Update installed"
                                                            : "Update available";
    ImGui::TextUnformatted(title);
    ImGui::PopFont();
    ImGui::SetCursorScreenPos(point(90,392));
    ImGui::PushTextWrapPos(point(1186,0).x);
    const std::string current = updater::CurrentVersionDisplay();
    if (state_ == State::kAvailable) {
      ImGui::Text("Skate 3 Recompiled+ %s is ready to install. You have %s.", release_.display.c_str(),
                  current.c_str());
      ImGui::Spacing();
      ImGui::TextColored(ImVec4(.66f,.75f,.80f,1), "%s", in_place_
          ? "The game restarts after updating. Your game files, cities, settings and saves stay as "
            "they are. If the new version improves New San Van, it asks for your Skate 2 files "
            "when it starts."
          : in_place_reason_.c_str());
    } else if (state_ == State::kInstalling) {
      const int phase = phase_.load();
      const uint64_t done = done_.load(), total = total_.load();
      if (phase == int(updater::Phase::kDownloading) && total) {
        ImGui::Text("Downloading %s...  %llu of %llu MB", release_.display.c_str(),
                    (unsigned long long)(done >> 20), (unsigned long long)(total >> 20));
      } else {
        ImGui::Text("%s", phase == int(updater::Phase::kDownloading) ? "Downloading..."
                          : phase == int(updater::Phase::kVerifying) ? "Checking the download..."
                                                                    : "Installing...");
      }
      ImGui::SetCursorScreenPos(point(90,450));
      const float fraction = phase == int(updater::Phase::kDownloading) && total
                                 ? std::min(1.f, float(done) / float(total))
                                 : -float(ImGui::GetTime());
      ImGui::ProgressBar(fraction, ImVec2(1096*s,12*s), "");
    } else if (state_ == State::kFailed) {
      ImGui::TextColored(ImVec4(.96f,.60f,.53f,1), "%s", error_.c_str());
      ImGui::Spacing();
      ImGui::TextColored(ImVec4(.66f,.75f,.80f,1),
          "Your installation hasn't changed. You can download the new version from its release page.");
    } else {
      ImGui::TextColored(ImVec4(.66f,.75f,.80f,1), "%s", error_.c_str());
    }
    ImGui::PopTextWrapPos();
    if (state_ == State::kInstalling) return;
    ImGui::SetCursorScreenPos(point(90,556));
    if (state_ == State::kAvailable) {
      if (ImGui::Button("What's new",ImVec2(160*s,44*s))) updater::OpenInBrowser(release_.page_url);
      ImGui::SetCursorScreenPos(point(520,556));
      if (ImGui::Button("Don't ask again",ImVec2(200*s,44*s))) {
        REXCVAR_SET(skate3_update_check, false);
        rex::cvar::SaveConfigValues(settings_path_, {"skate3_update_check"});
        REXLOG_INFO("update: checks turned off (Settings > System > Check for Updates)");
        Finish(true);
        return;
      }
      ImGui::SameLine(0,10*s);
      if (ImGui::Button("Not now",ImVec2(160*s,44*s))) { Finish(true); return; }
      ImGui::SameLine(0,10*s);
      ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(.18f,.49f,.37f,1));
      if (in_place_) {
        if (ImGui::Button("Update now",ImVec2(300*s,44*s))) StartInstall();
      } else if (ImGui::Button("Open download page",ImVec2(300*s,44*s))) {
        updater::OpenInBrowser(release_.page_url);
      }
      ImGui::PopStyleColor();
    } else if (state_ == State::kFailed) {
      if (ImGui::Button("Open download page",ImVec2(240*s,44*s))) updater::OpenInBrowser(release_.page_url);
      ImGui::SetCursorScreenPos(point(864,556));
      ImGui::PushStyleColor(ImGuiCol_Button,ImVec4(.18f,.49f,.37f,1));
      if (ImGui::Button("Continue",ImVec2(318*s,44*s))) Finish(true);
      ImGui::PopStyleColor();
    } else {
      ImGui::SetCursorScreenPos(point(864,556));
      if (ImGui::Button("Quit",ImVec2(318*s,44*s))) Finish(false);
    }
  }
  void StartInstall() {
    state_ = State::kInstalling;
    installed_ = false;
    worker_ = std::thread([this] {
      std::string error;
      install_ok_ = updater::DownloadAndInstall(release_, done_, total_, phase_, error);
      error_ = error;
      installed_.store(true, std::memory_order_release);
    });
  }
  void Finish(bool proceed) {
    outcome_->proceed = proceed;
    Close();
  }

  fs::path settings_path_;
  std::shared_ptr<UpdateOutcome> outcome_;
  std::function<void()> closed_;
  State state_ = State::kChecking;
  std::thread worker_;
  std::atomic<bool> checked_{false}, installed_{false};
  updater::CheckResult result_ = updater::CheckResult::kUnavailable;
  updater::Release release_;
  bool in_place_ = false, install_ok_ = false;
  std::string in_place_reason_, error_;
  std::atomic<uint64_t> done_{0}, total_{0};
  std::atomic<int> phase_{0};
  bool background_attempted_ = false;
  rex::ui::ImmediateTexture* background_ = nullptr;
};

#if defined(_WIN32)
// Runs a startup dialog's message loop until `done` (Windows finalizes paths synchronously).
void RunModal(rex::ui::WindowedAppContext& context, rex::ui::Window* window,
              const std::function<bool()>& done) {
  while (!done() && !context.HasQuitFromUIThread()) {
    context.ExecutePendingFunctionsFromUIThread();
    MSG message;
    while (PeekMessageW(&message,nullptr,0,0,PM_REMOVE)) {
      if (message.message==WM_QUIT) { context.QuitFromUIThread(); break; }
      TranslateMessage(&message); DispatchMessageW(&message);
    }
    if (window) {
      window->RequestPaint();
      if (auto* win=dynamic_cast<rex::ui::Win32Window*>(window))
        RedrawWindow(win->hwnd(),nullptr,nullptr,RDW_INVALIDATE|RDW_UPDATENOW|RDW_NOERASE);
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(16));
  }
}
#endif
}

void ShowNewSanVanSetup(rex::ui::WindowedAppContext& context, rex::ui::ImGuiDrawer* drawer,
                        rex::PathConfig paths, const fs::path& settings_path,
                        std::function<void(rex::PathConfig)> resume) {
  auto finished = std::make_shared<bool>(false);
  auto closed = [&context, paths, resume = std::move(resume)](bool ok) mutable {
    context.CallInUIThreadDeferred([&context, ok, paths, resume = std::move(resume)]() mutable {
      if (ok) resume(std::move(paths));
      else context.QuitFromUIThread();
    });
  };
  new SetupDialog(drawer, std::move(paths), settings_path, finished, std::move(closed));
}

bool RunNewSanVanSetup(rex::ui::WindowedAppContext& context,rex::ui::Window* window,
                       rex::ui::ImGuiDrawer* drawer,const rex::PathConfig& paths,
                       const std::filesystem::path& settings_path) {
#if defined(_WIN32)
  auto finished=std::make_shared<bool>(false);
  auto* dialog=new SetupDialog(drawer,paths,settings_path,finished);
  RunModal(context,window,[&] { return *finished; });
  if (!*finished) dialog->Shutdown();
  return *finished;
#else
  return false;
#endif
}

void ShowNewSanVanUpdate(rex::ui::WindowedAppContext& context, rex::ui::ImGuiDrawer* drawer,
                         rex::PathConfig paths, const fs::path& settings_path, NsvStatus status,
                         std::function<void(rex::PathConfig)> resume) {
  auto finished = std::make_shared<bool>(false);
  auto closed = [&context, paths, resume = std::move(resume)](bool ok) mutable {
    context.CallInUIThreadDeferred([&context, ok, paths, resume = std::move(resume)]() mutable {
      if (ok) resume(std::move(paths));
      else context.QuitFromUIThread();
    });
  };
  new SetupDialog(drawer, std::move(paths), settings_path, finished, std::move(closed),
                  std::move(status));
}

bool RunNewSanVanUpdate(rex::ui::WindowedAppContext& context, rex::ui::Window* window,
                        rex::ui::ImGuiDrawer* drawer, const rex::PathConfig& paths,
                        const std::filesystem::path& settings_path, NsvStatus status) {
#if defined(_WIN32)
  auto finished=std::make_shared<bool>(false);
  auto* dialog=new SetupDialog(drawer,paths,settings_path,finished,{},std::move(status));
  RunModal(context,window,[&] { return *finished; });
  if (!*finished) dialog->Shutdown();
  return *finished;
#else
  return false;
#endif
}

void ShowUpdateCheck(rex::ui::WindowedAppContext& context, rex::ui::ImGuiDrawer* drawer,
                     const fs::path& settings_path, std::function<void()> proceed) {
  auto outcome = std::make_shared<UpdateOutcome>();
  auto closed = [&context, outcome, proceed = std::move(proceed)]() mutable {
    context.CallInUIThreadDeferred([&context, outcome, proceed = std::move(proceed)]() mutable {
      if (outcome->proceed) proceed();
      else context.QuitFromUIThread();
    });
  };
  new UpdateDialog(drawer, settings_path, outcome, std::move(closed));
}

bool RunUpdateCheck(rex::ui::WindowedAppContext& context, rex::ui::Window* window,
                    rex::ui::ImGuiDrawer* drawer, const std::filesystem::path& settings_path) {
#if defined(_WIN32)
  auto outcome=std::make_shared<UpdateOutcome>();
  new UpdateDialog(drawer,settings_path,outcome);
  RunModal(context,window,[&] { return outcome->done; });
  return outcome->done && outcome->proceed;
#else
  return true;
#endif
}
}
