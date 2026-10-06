#include "flutter_window.h"

#include <optional>
#include <commdlg.h>
#include <flutter/standard_method_codec.h>

#include "flutter/generated_plugin_registrant.h"

FlutterWindow::FlutterWindow(const flutter::DartProject& project)
    : project_(project) {}

FlutterWindow::~FlutterWindow() {}

bool FlutterWindow::OnCreate() {
  if (!Win32Window::OnCreate()) {
    return false;
  }

  RECT frame = GetClientArea();

  // The size here must match the window dimensions to avoid unnecessary surface
  // creation / destruction in the startup path.
  flutter_controller_ = std::make_unique<flutter::FlutterViewController>(
      frame.right - frame.left, frame.bottom - frame.top, project_);
  // Ensure that basic setup of the controller was successful.
  if (!flutter_controller_->engine() || !flutter_controller_->view()) {
    return false;
  }
  RegisterPlugins(flutter_controller_->engine());
  files_channel_ = std::make_unique<flutter::MethodChannel<flutter::EncodableValue>>(
      flutter_controller_->engine()->messenger(), "luma/files",
      &flutter::StandardMethodCodec::GetInstance());
  files_channel_->SetMethodCallHandler(
      [this](const flutter::MethodCall<flutter::EncodableValue>& call,
             std::unique_ptr<flutter::MethodResult<flutter::EncodableValue>> result) {
        if (call.method_name() != "export") {
          result->NotImplemented();
          return;
        }
        const auto* args = call.arguments()
            ? std::get_if<flutter::EncodableMap>(call.arguments()) : nullptr;
        if (!args) { result->Error("invalid_file", "Invalid file"); return; }
        const auto name_it = args->find(flutter::EncodableValue("filename"));
        const auto bytes_it = args->find(flutter::EncodableValue("bytes"));
        const auto* name = name_it == args->end()
            ? nullptr : std::get_if<std::string>(&name_it->second);
        const auto* bytes = bytes_it == args->end()
            ? nullptr : std::get_if<std::vector<uint8_t>>(&bytes_it->second);
        if (!name || !bytes) { result->Error("invalid_file", "Invalid file"); return; }
        wchar_t path[32768] = {};
        if (MultiByteToWideChar(CP_UTF8, 0, name->c_str(), -1, path, 32768) == 0) {
          result->Error("invalid_file", "Invalid filename"); return;
        }
        OPENFILENAMEW dialog = {};
        dialog.lStructSize = sizeof(dialog);
        dialog.hwndOwner = GetHandle();
        dialog.lpstrFile = path;
        dialog.nMaxFile = 32768;
        dialog.Flags = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;
        if (!GetSaveFileNameW(&dialog)) {
          if (CommDlgExtendedError() != 0) result->Error("export_failed", "File save failed");
          else result->Success(flutter::EncodableValue(false));
          return;
        }
        const HANDLE file = CreateFileW(path, GENERIC_WRITE, 0, nullptr,
                                       CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) {
          result->Error("export_failed", "File save failed"); return;
        }
        DWORD written = 0;
        const bool saved = bytes->size() <= MAXDWORD &&
            WriteFile(file, bytes->data(), static_cast<DWORD>(bytes->size()), &written, nullptr) &&
            written == bytes->size();
        CloseHandle(file);
        if (saved) result->Success(flutter::EncodableValue(true));
        else result->Error("export_failed", "File save failed");
      });
  SetChildContent(flutter_controller_->view()->GetNativeWindow());

  flutter_controller_->engine()->SetNextFrameCallback([&]() {
    this->Show();
  });

  // Flutter can complete the first frame before the "show window" callback is
  // registered. The following call ensures a frame is pending to ensure the
  // window is shown. It is a no-op if the first frame hasn't completed yet.
  flutter_controller_->ForceRedraw();

  return true;
}

void FlutterWindow::OnDestroy() {
  files_channel_ = nullptr;
  if (flutter_controller_) {
    flutter_controller_ = nullptr;
  }

  Win32Window::OnDestroy();
}

LRESULT
FlutterWindow::MessageHandler(HWND hwnd, UINT const message,
                              WPARAM const wparam,
                              LPARAM const lparam) noexcept {
  // Give Flutter, including plugins, an opportunity to handle window messages.
  if (flutter_controller_) {
    std::optional<LRESULT> result =
        flutter_controller_->HandleTopLevelWindowProc(hwnd, message, wparam,
                                                      lparam);
    if (result) {
      return *result;
    }
  }

  switch (message) {
    case WM_FONTCHANGE:
      flutter_controller_->engine()->ReloadSystemFonts();
      break;
  }

  return Win32Window::MessageHandler(hwnd, message, wparam, lparam);
}
